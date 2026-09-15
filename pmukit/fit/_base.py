"""Shared plumbing for the block fitters: the result record and the dataset readers.

Nothing here touches physics.  It answers three questions every block asks:

  * which cell of contract 2 does THIS block live on (the spec's axes, reduced from the
    project cell -- a block whose parameters carry no `load_a` is fitted once per corner,
    not once per load, and a block on `temp_cont` is fitted CONTINUOUSLY against the
    temperature sweep instead of once per discrete temperature);
  * is the measurement there at all, and if not, is that "ran and broke" / "never run"
    (`missing=True`, no fabricated parameters) rather than a bad fit;
  * what is the residual, in the one unit the Model screen prints.

No module under `pmukit/fit/` may launch a process: every curve the Model screen draws is
`predict(params, ...)`, a pure analytic evaluation of the fitted numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .. import spec
from ..dataset import CELL_DIMS, cell_key

__all__ = ["BlockFit", "NoData", "block_cell", "read_curve", "read_scalar", "read_over_axis",
           "var_layout", "have", "db_rms", "pct_rms", "missing_fit", "num", "TWO_PI",
           "T_REF_C"]

TWO_PI = 2.0 * np.pi

#: The reference temperature every continuous-temperature law is anchored at, in degC.
#: `vout` and `idc` are the values AT this temperature and `vout_tc` / `ptat_slope` are the
#: slopes away from it, so a parameter table needs no hidden context to be evaluated.
T_REF_C = 25.0


# --------------------------------------------------------------------------- the result


@dataclass
class BlockFit:
    """One fitted block at one cell -- what the Model screen shows and the emitter reads."""

    port: str
    block: str
    cell: dict
    params: dict = field(default_factory=dict)
    """Keys are EXACTLY the names in `spec.block(block, port_type).params`.  A `*_i` name
    holds a python list: the fitter chooses the section count."""
    score: float = float("nan")
    """The block's residual metric; dB for spectra, % for DC quantities."""
    metric: str = ""
    """What `score` measures, one phrase, printed next to the number."""
    n_points: int = 0
    identifiability: dict = field(default_factory=dict)
    """{"cond": float, "sigma": {param: float}, "unidentifiable": [names]} -- REPORTED,
    never fatal: a failing gate means the data cannot determine that number, not that the
    fit crashed."""
    notes: list = field(default_factory=list)
    missing: bool = False
    """The data was never run, or ran and broke.  NOT a bad fit -- `params` stays empty."""

    def to_dict(self) -> dict:
        return {"port": self.port, "block": self.block, "cell": dict(self.cell),
                "params": _jsonable(self.params), "score": _jsonable(self.score),
                "metric": self.metric, "n_points": int(self.n_points),
                "identifiability": _jsonable(self.identifiability),
                "notes": list(self.notes), "missing": bool(self.missing)}

    @classmethod
    def from_dict(cls, d: dict) -> "BlockFit":
        return cls(port=str(d["port"]), block=str(d["block"]), cell=dict(d.get("cell") or {}),
                   params=dict(d.get("params") or {}),
                   score=float(d.get("score", float("nan"))),
                   metric=str(d.get("metric", "")), n_points=int(d.get("n_points", 0)),
                   identifiability=dict(d.get("identifiability") or {}),
                   notes=list(d.get("notes") or []), missing=bool(d.get("missing", False)))

    @property
    def key(self) -> str:
        return f"{self.port}/{self.block}/{cell_key(self.cell)}"


def _jsonable(obj):
    """numpy -> plain python, and NaN/inf -> a string, so the whole FitResult round-trips
    through strict JSON without losing "this number was not determinable"."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        if np.isnan(obj):
            return "nan"
        if np.isinf(obj):
            return "inf" if obj > 0 else "-inf"
    if isinstance(obj, np.ndarray):
        return [_jsonable(v) for v in obj.tolist()]
    return obj


def num(x, default: float = float("nan")) -> float:
    """Inverse of `_jsonable` for one number: "nan"/"inf" come back as floats."""
    if isinstance(x, str):
        low = x.strip().lower()
        if low in ("nan", "inf", "+inf", "-inf"):
            return float(low)
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class NoData(Exception):
    """Internal: the measurement this block needs is not in the dataset.  Callers turn it
    into `BlockFit(missing=True)` -- never into a fabricated parameter."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def missing_fit(port: str, block: str, cell: dict, reason: str,
                metric: str = "") -> BlockFit:
    """The honest empty result: no parameters, `missing=True`, the reason in `notes`."""
    return BlockFit(port=port, block=block, cell=dict(cell), params={},
                    score=float("nan"), metric=metric, n_points=0,
                    notes=[reason], missing=True)


# --------------------------------------------------------------------------- cells


def block_cell(block_name: str, port_type: str, cell: dict) -> dict:
    """Reduce a project cell to the axes THIS block's parameters actually vary over.

    `temp_cont` is deliberately NOT a cell axis: a block that declares it (the rail DC
    table, the bias idc/PTAT law) is fitted CONTINUOUSLY against the temperature sweep
    inside one process corner, which is what keeps cross-PVT interpolation out of the
    model (METHODOLOGY: PVT route A, `.lib` sections).
    """
    blk = spec.block(block_name, port_type)
    axes = {a for p in blk.params for a in p.axes}
    out = {}
    for dim in CELL_DIMS:
        if dim not in cell:
            continue
        if dim == "temp_c" and "temp_c" not in axes:
            continue
        if dim != "temp_c" and dim not in axes:
            continue
        out[dim] = cell[dim]
    return out


def _var_record(ds, var: str) -> dict | None:
    """The contract-2 index record of one variable, or None when it was never declared.

    Reads the dataset's own index (same package, contract 2 shape) and falls back to the
    public `summary()` if that ever changes shape.
    """
    idx = getattr(ds, "_index", None)
    if isinstance(idx, dict) and isinstance(idx.get("variables"), dict):
        rec = idx["variables"].get(var)
        return dict(rec) if isinstance(rec, dict) else None
    try:
        rec = ds.summary()["variables"].get(var)
    except Exception:                                 # noqa: BLE001 -- absence is not an error here
        return None
    return dict(rec) if isinstance(rec, dict) else None


def _split(dims: list) -> tuple[tuple[str, ...], str]:
    cells = tuple(d for d in dims if d in CELL_DIMS)
    tail = [d for d in dims if d not in CELL_DIMS]
    return cells, (tail[-1] if tail else "")


def have(ds, var: str) -> bool:
    """True when the dataset declares `var` at all."""
    return _var_record(ds, var) is not None


def var_layout(ds, var: str):
    """`(cell dims, sweep coordinate name)` of one variable, or None when it is not declared.

    The fitters use it because contract 2 lets the same physical sweep arrive two ways: the
    rail DC table can be a `load_a` CELL axis (one number per load cell) or a trailing sweep
    COORDINATE (one curve per corner).  Both are legitimate; neither may be assumed.
    """
    rec = _var_record(ds, var)
    if rec is None:
        return None
    return _split(list(rec["dims"]))


def _cell_for_var(ds, var: str, cell: dict) -> dict:
    rec = _var_record(ds, var)
    if rec is None:
        raise NoData(f"{var} was never declared in this dataset")
    cells, _ = _split(list(rec["dims"]))
    out = {}
    for dim in cells:
        if dim not in cell:
            raise NoData(f"{var} is stored over {dim} but the fit cell does not name it")
        out[dim] = cell[dim]
    return out


def _psd_to_amplitude(values: np.ndarray, unit: str) -> tuple[np.ndarray, str]:
    """Contract 2 stores noise as a PSD (`V^2/Hz`, `A^2/Hz`); the fit works in AMPLITUDE
    (V/rtHz, A/rtHz) because the log-amplitude domain is what weights the white floor and
    the 1/f tail equally (METHODOLOGY: log-AMPLITUDE noise fit).  A dataset that already
    stores the amplitude (`V/rtHz`) is passed through."""
    u = (unit or "").replace(" ", "").lower()
    if "^2" in u or "**2" in u:
        return np.sqrt(np.maximum(np.asarray(values, float), 0.0)), "amplitude(sqrt of PSD)"
    return np.asarray(values, float), "amplitude(as stored)"


def _same(a, b) -> bool:
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    try:
        return bool(a == b) or bool(np.isclose(float(a), float(b), rtol=1e-9, atol=0.0))
    except (TypeError, ValueError):
        return False


def _is_registered_missing(ds, var: str, sub: dict) -> bool:
    """Tell "ran and broke" from "never run" FOR THIS CELL.

    Both read back as NaN, and the fitter has to report them differently: one is a failure with
    a reason attached, the other is a run nobody scheduled.  `Dataset.missing()` rows are
    `[var, <cell values in the fixed order>, reason]`, so the cell values are matched
    positionally against the dims this variable actually carries.
    """
    cells, _ = _split(list((_var_record(ds, var) or {}).get("dims") or []))
    want = [sub[d] for d in cells if d in sub]
    for row in ds.missing(var):
        values = list(row)[1:-1]
        if len(values) == len(want) and all(_same(a, b) for a, b in zip(values, want)):
            return True
    return False


def read_curve(ds, var: str, cell: dict, *, psd: bool = False):
    """(coordinate, values) of one cell's sweep.  Raises `NoData` for a missing cell.

    `psd=True` converts a `^2/Hz` PSD to the amplitude the noise fitters work in.
    """
    sub = _cell_for_var(ds, var, cell)
    rec = _var_record(ds, var)
    if not ds.has(var, sub):
        why = "registered missing" if _is_registered_missing(ds, var, sub) else "never run"
        raise NoData(f"{var} at {cell_key(sub) or '(single cell)'}: {why}")
    coord = ds.coord(var)
    if coord is None:
        raise NoData(f"{var} has no sweep coordinate; this block needs a curve")
    vals = np.asarray(ds.get(var, sub))
    good = np.isfinite(coord) & np.isfinite(vals.real) & np.isfinite(vals.imag if
                                                                     np.iscomplexobj(vals)
                                                                     else vals)
    coord, vals = np.asarray(coord, float)[good], vals[good]
    if coord.size == 0:
        raise NoData(f"{var} at {cell_key(sub) or '(single cell)'}: every point is NaN")
    if psd:
        vals, _ = _psd_to_amplitude(vals, (rec or {}).get("unit", ""))
    return coord, vals


def read_scalar(ds, var: str, cell: dict) -> float:
    """One cell of a variable with no sweep coordinate."""
    sub = _cell_for_var(ds, var, cell)
    if not ds.has(var, sub):
        why = "registered missing" if _is_registered_missing(ds, var, sub) else "never run"
        raise NoData(f"{var} at {cell_key(sub) or '(single cell)'}: {why}")
    return float(np.asarray(ds.get(var, sub)).reshape(()))


def read_over_axis(ds, var: str, cell: dict, dim: str, port: str):
    """(axis values, one scalar-or-curve per axis point) for a variable stored OVER a cell
    axis -- the rail DC table is a `load_a` cell axis, not a sweep coordinate, so the
    load-regulation curve has to be assembled from the cells."""
    rec = _var_record(ds, var)
    if rec is None:
        raise NoData(f"{var} was never declared in this dataset")
    cells, coord_dim = _split(list(rec["dims"]))
    if dim not in cells:
        raise NoData(f"{var} is not stored over {dim}")
    axis = ds.axis(dim, port) if dim == "load_a" else ds.axis(dim)
    xs, ys = [], []
    for value in axis:
        sub = dict(cell)
        sub[dim] = value
        try:
            if coord_dim:
                _, y = read_curve(ds, var, sub)
            else:
                y = read_scalar(ds, var, sub)
        except NoData:
            continue
        xs.append(float(value))
        ys.append(y)
    if not xs:
        raise NoData(f"{var}: no cell along {dim} holds data")
    return np.asarray(xs, float), ys


# --------------------------------------------------------------------------- metrics


def db_rms(model, gt) -> float:
    """RMS of 20*log10(|model|/|gt|) -- the spectrum metric used everywhere in this tool."""
    m = np.abs(np.asarray(model)) + 1e-300
    g = np.abs(np.asarray(gt)) + 1e-300
    return float(np.sqrt(np.mean((20.0 * np.log10(m / g)) ** 2)))


def pct_rms(model, gt, scale: float | None = None) -> float:
    """RMS error in % of `scale` (default: the mean magnitude of the ground truth)."""
    m = np.asarray(model, float)
    g = np.asarray(gt, float)
    ref = float(scale) if scale else float(np.mean(np.abs(g)))
    return float(np.sqrt(np.mean(((m - g) / (abs(ref) + 1e-300)) ** 2)) * 100.0)
