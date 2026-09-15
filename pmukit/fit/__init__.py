"""The fitters: measurements in, model parameters out.

One module per block (`zout`, `psrr`, `noise`, `dc`, `load_en` for a rail; `bias` for the four
current-bias blocks; `en` for the enable ramp), each exposing the same two functions::

    fit(dataset, port, cell, derived) -> BlockFit
    predict(params, **kwargs)         -> np.ndarray

`predict` is the load-bearing half.  It is a PURE ANALYTIC evaluation of the fitted parameters --
`predict(params, f=...)` for the spectral blocks, `predict(params, T=...)` for the DC and PTAT
laws, `predict(params, t=...)` for the transient ones.  **The Model screen's curves and scores
are computed from `predict`, never by running a simulator**, and no module under `pmukit/fit/`
may launch a process.

PER-CORNER FITTING, CONTINUOUS TEMPERATURE WITHIN A CORNER.  Every process corner is fitted
SEPARATELY and emitted as its own `.lib` section: cross-PVT interpolation is rejected, because it
is a new overfitting surface on top of one that was already measured at 10-100x held-out error.
Temperature is CONTINUOUS inside a corner for the DC quantities (`vout_tc`, `ptat_slope`, fitted
against the sweep) and DISCRETE for the AC and noise blocks.  Interpolating any other parameter
across temperature is allowed only for a monotone quantity and must be recorded in `notes`.

The numerics are PORTED, not rewritten: they are validated against 14 synthetic LDOs and one real
part.  Each ported module names its source file and commit on its first line.  What changed here
is the DATA CONTRACT (a typed, NaN-honest dataset instead of a string-keyed npz) and the
INTERFACE (one `BlockFit` per block per cell instead of module globals).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import jsonio, spec
from ..dataset import cell_key
from ..errors import PmuError
from . import bias, dc, en, identifiability, load_en, noise, psrr, zout
from ._base import BlockFit, T_REF_C, block_cell, missing_fit, var_layout

__all__ = ["BlockFit", "FitResult", "fit_port", "fit_project", "MODULES", "T_REF_C",
           "zout", "psrr", "noise", "dc", "bias", "load_en", "en", "identifiability"]

#: which module fits which (port_type, block).  Public so the web shell and the emitter can
#: read it instead of mirroring the table and drifting away from it.
MODULES = {
    ("rail", "dc"): dc,
    ("rail", "zout"): zout,
    ("rail", "psrr"): psrr,
    ("rail", "noise"): noise,
    ("rail", "load_en"): load_en,
    ("bias", "idc"): bias,
    ("bias", "yout"): bias,
    ("bias", "noise"): bias,
    ("bias", "psrr"): bias,
    ("en", "ramp"): en,
}
_MODULES = MODULES          # the name this module used before the table was made public


# --------------------------------------------------------------------------- the result


@dataclass
class FitResult:
    """Every fitted block of a project, addressable and losslessly serializable.

    `fits` is keyed by `"<port>/<block>/<cell key>"`, which is `BlockFit.key`; `index()` gives
    the same records under the `(port, block, corner, temp, vset, load)` tuple the digest's
    re-emit walks.  `to_dict()` / `from_dict()` round-trip through JSON without loss, because
    the digest re-emits the model from exactly this and nothing else.
    """

    project: str = ""
    config_sha: str = ""
    dataset_sha: str = ""
    spec_sha: str = spec.SPEC_SHA
    tiers: tuple = ("hb", "ls", "en")
    ports: dict = field(default_factory=dict)          # port -> port type
    fits: dict = field(default_factory=dict)           # key -> BlockFit
    notes: list = field(default_factory=list)

    KIND = "pmukit.fit/1"

    def add(self, bf: BlockFit) -> BlockFit:
        self.fits[bf.key] = bf
        return bf

    def get(self, port: str, block: str, cell: dict | None = None) -> BlockFit | None:
        return self.fits.get(f"{port}/{block}/{cell_key(cell or {})}")

    def for_port(self, port: str) -> list:
        return [bf for bf in self.fits.values() if bf.port == port]

    def index(self) -> dict:
        """`{(port, block, corner, temp, vset, load): BlockFit}` -- the tuple view."""
        out = {}
        for bf in self.fits.values():
            c = bf.cell
            out[(bf.port, bf.block, c.get("process"), c.get("temp_c"), c.get("vset"),
                 c.get("load_a"))] = bf
        return out

    def missing(self) -> list:
        """Every block whose measurement was never run or ran and broke -- NOT a bad fit."""
        return [bf for bf in self.fits.values() if bf.missing]

    def worst(self, block: str | None = None) -> BlockFit | None:
        """The worst-scoring fitted block (a rollup over corners is a `max`, so adding corners
        can only LOWER a grade -- a 'worse' number on richer coverage is not a regression)."""
        cands = [bf for bf in self.fits.values()
                 if not bf.missing and (block is None or bf.block == block)
                 and bf.score == bf.score]
        return max(cands, key=lambda b: b.score) if cands else None

    def to_dict(self) -> dict:
        return {"kind": self.KIND, "project": self.project, "config_sha": self.config_sha,
                "dataset_sha": self.dataset_sha, "spec_sha": self.spec_sha,
                "tiers": list(self.tiers), "ports": dict(self.ports),
                "notes": list(self.notes),
                "fits": {k: v.to_dict() for k, v in sorted(self.fits.items())}}

    @classmethod
    def from_dict(cls, d: dict) -> "FitResult":
        if not isinstance(d, dict) or "fits" not in d:
            raise PmuError(
                what="This is not a serialized FitResult.",
                why="from_dict reads what to_dict wrote: an object with a 'fits' map",
                do=["pass the object produced by FitResult.to_dict()",
                    "or re-run the fit to regenerate it"],
                where="pmukit/fit/__init__.py:FitResult.from_dict",
            )
        out = cls(project=str(d.get("project", "")), config_sha=str(d.get("config_sha", "")),
                  dataset_sha=str(d.get("dataset_sha", "")),
                  spec_sha=str(d.get("spec_sha", "")),
                  tiers=tuple(d.get("tiers") or ("hb", "ls", "en")),
                  ports=dict(d.get("ports") or {}), notes=list(d.get("notes") or []))
        for key, rec in (d.get("fits") or {}).items():
            out.fits[str(key)] = BlockFit.from_dict(rec)
        return out

    def sha(self, n: int = 12) -> str:
        return jsonio.sha(self.to_dict(), n)

    def __iter__(self):
        """The BlockFits in key order.

        The emitter accepts any iterable of BlockFit records, so a FitResult can be handed to it
        directly rather than being unpacked at every call site.
        """
        return iter([self.fits[k] for k in sorted(self.fits)])

    def __len__(self) -> int:
        return len(self.fits)


# --------------------------------------------------------------------------- one port


def _key(port: str, block: str, cell: dict) -> str:
    return f"{port}/{block}/{cell_key(cell)}"


def _zout_at(dataset, port, cell, derived, cache):
    """The Zout block of one cell, taken from the cache when it is already there.

    ONE fitted impedance per cell, shared by PSRR, noise and the large-signal replay: they are
    the same impedance driven by different sources, and letting them see different ones is how
    an emitted model ends up disagreeing with itself about the one physical node.
    """
    key = _key(port, "zout", block_cell("zout", "rail", cell))
    if cache is not None and key in cache:
        return cache[key]
    bf = zout.fit(dataset, port, cell, derived)
    if cache is not None:
        cache[key] = bf
    return bf


def fit_port(dataset, port: str, port_type: str, cell: dict, derived=None,
             tiers=("hb", "ls", "en"), cache=None) -> dict:
    """Fit every block of one port at one cell -> `{block name: BlockFit}`.

    ORDER IS PHYSICS, not convenience: Zout is fitted first and handed to the PSRR, noise and
    large-signal blocks, because all three are the SAME impedance driven by different sources.
    Fitting them against different impedances would let the emitted model disagree with itself
    about the one physical Zout on the one physical node.

    `cache` is a `{BlockFit.key: BlockFit}` map the driver threads through, so a block whose
    cell REPEATS -- one with no load axis, seen once per load -- is fitted once.  The noise bank
    fills it for every load at once, because its corner frequencies are shared across the loads
    and the fit is therefore necessarily joint.
    """
    blocks = [b for b in spec.blocks_for(port_type) if b.tier in tiers]
    out: dict = {}
    zparams = None
    for blk in blocks:
        bcell = block_cell(blk.name, port_type, cell)
        key = _key(port, blk.name, bcell)
        if not blk.params:
            # An emitter constant (the rail's one-way conduction): no observable, so the plan
            # compiler never schedules a run for it and there is nothing to fit.
            out[blk.name] = BlockFit(port=port, block=blk.name, cell=bcell,
                                     params={}, score=float("nan"), metric="", n_points=0,
                                     notes=[f"{blk.name} is an emitter constant: "
                                            + spec.WHAT[(port_type, blk.name)]])
            continue
        mod = _MODULES.get((port_type, blk.name))
        if mod is None:                                   # pragma: no cover - guarded by spec
            continue
        if cache is not None and key in cache:
            bf = cache[key]
            if port_type == "rail" and blk.name == "zout":
                zparams = None if bf.missing else bf.params
            out[blk.name] = bf
            continue

        if port_type == "rail" and blk.name == "zout":
            bf = _zout_at(dataset, port, cell, derived, cache)
            zparams = None if bf.missing else bf.params
        elif port_type == "rail" and blk.name == "psrr":
            bf = mod.fit(dataset, port, cell, derived, zout_params=zparams)
        elif port_type == "rail" and blk.name == "noise":
            # the bank is joint across the loads of this corner: fit it once and cache every
            # load's row instead of repeating the joint solve per load
            try:
                loads = [float(x) for x in dataset.axis("load_a", port)]
            except Exception:                             # noqa: BLE001 -- rail with no loads
                loads = [float(cell["load_a"])] if "load_a" in cell else []
            zmap = {}
            for il in loads:
                zbf = _zout_at(dataset, port, dict(cell, load_a=il), derived, cache)
                if not zbf.missing:
                    zmap[il] = zbf.params
            bank = mod.fit_bank(dataset, port, cell, derived, zout_by_load=zmap or None)
            if cache is not None:
                for row in bank.values():
                    cache[row.key] = row
            bf = bank.get(float(cell.get("load_a", 0.0)))
            if bf is None:
                bf = mod.fit(dataset, port, cell, derived, zout_by_load=zmap or None)
        elif port_type == "rail" and blk.name == "load_en":
            bf = mod.fit(dataset, port, cell, derived, zout_params=zparams)
        elif port_type == "bias":
            bf = mod.fit(dataset, port, cell, derived, block=blk.name)
        else:
            bf = mod.fit(dataset, port, cell, derived)
        if cache is not None:
            cache[bf.key] = bf
        out[blk.name] = bf
    return out


# --------------------------------------------------------------------------- the project


def _ports(dataset, derived) -> dict:
    """`{port: port type}` from the derived config, falling back to what the dataset declares."""
    ports: dict = {}
    if derived is not None:
        for p in (getattr(derived, "rails", {}) or {}):
            ports[p] = "rail"
        for p in (getattr(derived, "biases", {}) or {}):
            ports[p] = "bias"
    if ports:
        return ports
    for var in dataset.variables():
        try:
            observable, port = spec.split_variable(var)
        except PmuError:
            continue
        if observable in ("ac_zout", "dc_load", "tran_load_on", "tran_load_off", "noise_v"):
            ports[port] = "rail"
        elif observable in ("dc_iv", "ac_yout", "noise_i"):
            ports.setdefault(port, "bias")
        else:
            ports.setdefault(port, "rail")
    return ports


def _axis(dataset, name, port=None, default=(None,)):
    try:
        vals = dataset.axis(name, port) if name == "load_a" else dataset.axis(name)
    except Exception:                                     # noqa: BLE001 -- axis may not exist
        return list(default)
    return list(vals) if vals else list(default)


def fit_project(dataset, derived=None, *, tiers=("hb", "ls", "en"), on_event=None,
                ports=None) -> FitResult:
    """Fit every block of every modeled port over every cell of the dataset.

    Each block is fitted ONCE per distinct cell OF ITS OWN granularity (`_base.block_cell`), so
    a block with no `load_a` axis is not refitted per load and a block on `temp_cont` is fitted
    continuously against the temperature sweep instead of once per discrete temperature.

    `on_event(dict)` is called after every block with `{"port", "block", "cell", "score",
    "metric", "missing"}`, so a UI can show progress without this module knowing about a UI.
    """
    if not (hasattr(dataset, "variables") and hasattr(dataset, "axis")):
        raise PmuError(
            what=f"fit_project() needs an open Dataset, got {type(dataset).__name__}.",
            why="the fitters read measurements through contract 2 (Dataset.get / coord / axis); "
                "they never open files or resolve a project directory themselves",
            do=["open it first: Dataset.open($PMUKIT_DATA/<project>/dataset)",
                "then call fit_project(dataset, derived)"],
            where="pmukit/fit/__init__.py:fit_project",
        )
    res = FitResult(project=getattr(dataset, "project", ""),
                    config_sha=getattr(dataset, "config_sha", ""),
                    tiers=tuple(tiers))
    try:
        res.dataset_sha = dataset.sha()
    except Exception:                                     # noqa: BLE001 -- provenance only
        res.dataset_sha = ""
    table = dict(ports) if ports else _ports(dataset, derived)
    res.ports = dict(table)

    processes = _axis(dataset, "process")
    temps = _axis(dataset, "temp_c")
    vsets = _axis(dataset, "vset")

    for port, ptype in sorted(table.items()):
        loads = _axis(dataset, "load_a", port) if ptype == "rail" else [None]
        for proc in processes:
            for temp in temps:
                for vset in vsets:
                    for load in loads:
                        cell = {}
                        if proc is not None:
                            cell["process"] = proc
                        if temp is not None:
                            cell["temp_c"] = temp
                        if vset is not None:
                            cell["vset"] = vset
                        if load is not None:
                            cell["load_a"] = load
                        seen = set(res.fits)
                        fits = fit_port(dataset, port, ptype, cell, derived, tiers,
                                        cache=res.fits)
                        for bf in fits.values():
                            res.add(bf)
                        for key in [k for k in res.fits if k not in seen]:
                            bf = res.fits[key]
                            if on_event is not None:
                                on_event({"port": bf.port, "block": bf.block,
                                          "cell": dict(bf.cell), "score": bf.score,
                                          "metric": bf.metric, "missing": bf.missing})
    _cft_spread_note(res)
    return res


def _cft_spread_note(res: FitResult) -> None:
    """The cross-CORNER half of the feedthrough gate.

    A physical feedthrough capacitance is LOAD-INDEPENDENT, so the per-cell gate (which only
    sees one load) is completed here: when the fitted `c_ft` spreads by more than 20 % across
    the loads of one corner, say so -- the tail that was gated in is probably not a plain jwC.
    """
    by_corner: dict = {}
    for bf in res.fits.values():
        if bf.block != "psrr" or bf.missing or not bf.params.get("c_ft"):
            continue
        key = (bf.port, bf.cell.get("process"), bf.cell.get("temp_c"), bf.cell.get("vset"))
        by_corner.setdefault(key, []).append(float(bf.params["c_ft"]))
    for (port, proc, temp, vset), vals in sorted(by_corner.items(), key=lambda kv: str(kv[0])):
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        if mean > 0 and (max(vals) - min(vals)) / mean > 0.20:
            res.notes.append(
                f"{port} ({proc}/{temp}C/vset{vset}): the fitted feedthrough capacitance "
                f"spreads {100 * (max(vals) - min(vals)) / mean:.0f} % across the load points "
                f"({min(vals) * 1e15:.1f}-{max(vals) * 1e15:.1f} fF). A real feedthrough cap is "
                f"load-independent, so treat this one as unconfirmed.")


def explain_missing(res: FitResult) -> str:
    """One plain line per block whose data was never run -- the honest inventory the Model
    screen shows instead of a number."""
    rows = res.missing()
    if not rows:
        return "Every planned block has data."
    return "\n".join(f"{bf.port}/{bf.block} at {cell_key(bf.cell) or '(single cell)'}: "
                     f"{bf.notes[0] if bf.notes else 'no data'}" for bf in sorted(
                         rows, key=lambda b: b.key))


# re-exported for the tests and the Model screen
__all__ += ["explain_missing", "var_layout", "missing_fit", "block_cell"]
