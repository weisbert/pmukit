"""The desk side of the air gap: rebuild, refit, and compare number by number.

The box has no network and no agent. The only way a result comes home is the user pasting a
digest (contract 5). This module is what the desk does with it:

    pmukit digest import <file>      -> rebuild_dataset()   a contract-2 dataset subset
    pmukit reproduce --from-digest   -> reproduce()         refit here, compare with the box

Two rules govern all of it, and both come from the old repo's experience:

* **What the box dropped is registered as missing, never guessed.** A digest truncated by budget
  names the blocks it left out; those become `missing` rows with that reason, so a fitter looking
  at the rebuilt dataset can tell "the box never sent this" from "the box sent a NaN".
* **The model numbers travel with the digest; the desk does not recompute them to compare.**
  D4 carries ground truth AND model on the same resampled grid precisely because resampling
  changes a refit -- that was measured. So `compare()` puts the box's own numbers next to a desk
  refit and reports the difference; it never silently substitutes one for the other.

Cell convention: a digest curve/transient entry identifies its cell with the string
`dataset.cell_key()` produces (`"tt/25C/vset3/5.0e-04A"`), which `dataset.parse_cell_key` reverses.
A plain dict is accepted too.
"""
from __future__ import annotations

import math
import pathlib

import numpy as np

from . import dataset as ds_mod
from . import jsonio
from .errors import PmuError

# What a rebuilt dataset says about a block the box could not afford to send.
DROPPED_REASON = "dropped by the box's digest budget -- not measured here, not guessed"


def _cell_of(entry: dict, name: str) -> dict:
    raw = entry.get("cell")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            return ds_mod.parse_cell_key(raw)
        except Exception as exc:                       # noqa: BLE001 - re-raised in four parts
            raise PmuError(
                what=f"cannot read the cell of digest series {name!r}: {raw!r}.",
                why=f"A digest cell is the string dataset.cell_key() produces; parsing it failed "
                    f"({exc}).",
                do=["Re-copy the digest part carrying that block without editing it.",
                    "Or export it again from the box -- the cell string is written by the tool."],
                where=f"digest series {name}") from exc
    return {}


def _axes_from(cells: list[dict]) -> dict:
    """The smallest axis set that indexes exactly the cells the digest actually carried."""
    dims: dict = {"process": [], "temp_c": [], "vset": [], "load_a": {}}
    for cell in cells:
        for key in ("process", "temp_c", "vset"):
            v = cell.get(key)
            if v is None:
                continue
            if v not in dims[key]:
                dims[key].append(v)
    return dims


def _port_of(name: str) -> str:
    return name.split(".", 1)[1] if "." in name else name


def rebuild_dataset(payload: dict, path, *, project: str | None = None):
    """Build a contract-2 dataset subset from a parsed digest payload.

    Returns an open `Dataset`; the caller closes it. Only the series the digest actually carried
    become variables. Every block the trailer says was dropped is registered in `missing` for each
    variable it would have filled, with the reason -- so the difference between "the box never
    sent this" and "the box sent a NaN" survives the paste.
    """
    curves = payload.get("curves") or {}
    trans = payload.get("transients") or {}
    meta = payload.get("meta") or {}
    prov = payload.get("provenance") or {}
    name = project or meta.get("project") or "from_digest"

    entries: list[tuple[str, dict, str]] = []        # (variable, entry, coordinate key)
    for var, entry in curves.items():
        entries.append((var, entry, "x"))
    for var, entry in trans.items():
        entries.append((var, entry, "t"))

    cells = [_cell_of(e, v) for v, e, _k in entries]
    dims = _axes_from(cells)
    # per-port load grids, from whatever loads the digest happened to carry
    for (var, _e, _k), cell in zip(entries, cells):
        if "load_a" in cell:
            dims["load_a"].setdefault(_port_of(var), [])
            if cell["load_a"] not in dims["load_a"][_port_of(var)]:
                dims["load_a"][_port_of(var)].append(cell["load_a"])
    for key in ("process", "temp_c", "vset"):
        if not dims[key]:
            dims[key] = ["?"] if key == "process" else [0]
    for port in list(dims["load_a"]):
        dims["load_a"][port].sort()
    if not dims["load_a"]:
        # No series in this digest carried a load, so there is no load axis to declare. An empty
        # {port: [...]} map is rejected by the dataset (per-port grids must be real), and rightly.
        dims.pop("load_a")

    path = pathlib.Path(path)
    ds = ds_mod.Dataset.create(path, project=name,
                               config_sha=str(prov.get("config_sha", "")), dims=dims)

    for (var, entry, coord_key), cell in zip(entries, cells):
        coord = np.asarray([float(v) for v in (entry.get(coord_key) or [])], dtype="float64")
        if coord.size == 0:
            continue
        # The digest carries GROUND TRUTH and MODEL side by side. Only the ground truth belongs in
        # a dataset -- the model is a fit result and travels separately (payload["params"]).
        gt = entry.get("gt")
        if gt is None:
            continue
        values = np.asarray([float(v) for v in gt], dtype="float64")
        cell_dims = tuple(k for k in ("process", "temp_c", "vset", "load_a") if k in cell)
        sweep = "freq_hz" if coord_key == "x" else "time_s"
        ds.declare(var, dims=cell_dims + (sweep,), dtype="float64",
                   coord=coord, coord_name=f"{var}.{sweep}")
        ds.put(var, cell, values)

    for block in (payload.get("dropped") or []):
        _register_dropped(ds, block, entries)
    # A block the box dropped ENTIRELY has no variable in this dataset, so there is nothing to
    # hang a `missing` row on. Record it beside the dataset instead, so the loss is never invisible.
    dropped = dropped_blocks(payload)
    if dropped:
        jsonio.write(path / "dropped.json", {"dropped": dropped})
    return ds


BLOCK_CONTENT = {
    "D1": "the run ledger",
    "D2": "the fitted parameters (without these the desk cannot re-emit the model)",
    "D3": "the grades and the trust tiles",
    "D4": "the ground-truth and model curves",
    "D4rail": "the rail curves (zout, psrr, noise)",
    "D4bias": "the bias curves (idc, admittance, current noise)",
    "D5": "the load-EN transients",
    "D6": "the failed runs' logs",
}


def dropped_blocks(payload: dict) -> list[dict]:
    """What the box's budget cost this digest, named -- one entry per dropped block."""
    return [{"block": b, "would_have_carried": BLOCK_CONTENT.get(b, "(unknown block)"),
             "reason": DROPPED_REASON}
            for b in (payload.get("dropped") or [])]


def _register_dropped(ds, block: str, entries) -> None:
    """Mark, in the rebuilt dataset, what the budget cost us."""
    families = {"D4": ("ac_zout", "ac_psrr", "noise_v", "ac_yout", "noise_i", "dc_iv"),
                "D4rail": ("ac_zout", "ac_psrr", "noise_v"),
                "D4bias": ("ac_yout", "noise_i", "dc_iv"),
                "D5": ("tran_load_on", "tran_load_off", "tran_en")}
    obs = families.get(block)
    if not obs:
        return
    for var in ds.variables():
        if var.split(".", 1)[0] in obs:
            for cell in _all_cells(ds, var):
                if not ds.has(var, cell):
                    ds.mark_missing(var, cell, f"{block}: {DROPPED_REASON}")


def _all_cells(ds, var: str):
    dims, _sweep = ds._dims_of(var)                    # noqa: SLF001 - same package
    import itertools
    axes = [ds.axis(d, _port_of(var)) if d == "load_a" else ds.axis(d) for d in dims]
    for combo in itertools.product(*axes):
        yield dict(zip(dims, combo))


# ------------------------------------------------------------------------------- comparison
def _flatten(params: dict, prefix: str = "") -> dict:
    """{'VDD_A': {'zout': {'Ra': 0.09}}} -> {'VDD_A.zout.Ra': 0.09} for a number-by-number diff."""
    out: dict = {}
    for k, v in (params or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
        else:
            out[key] = v
    return out


def compare(box_params: dict, desk_params: dict, *, rtol: float = 1e-6) -> dict:
    """Put the box's fitted parameters next to a desk refit, number by number.

    Returns {"same", "moved", "only_box", "only_desk", "worst"} where `moved` carries the relative
    difference per parameter. Nothing is averaged away: a report the user can act on names the
    parameter, not a summary statistic.
    """
    a, b = _flatten(box_params), _flatten(desk_params)
    same, moved = [], []
    for key in sorted(set(a) | set(b)):
        if key not in a or key not in b:
            continue
        va, vb = a[key], b[key]
        if isinstance(va, float) and isinstance(vb, float):
            if math.isnan(va) and math.isnan(vb):
                same.append(key)
                continue
            scale = max(abs(va), abs(vb), 1e-30)
            rel = abs(va - vb) / scale
            (same if rel <= rtol else moved).append(key if rel <= rtol else
                                                    {"param": key, "box": va, "desk": vb,
                                                     "rel": rel})
        else:
            (same if va == vb else moved).append(key if va == vb else
                                                 {"param": key, "box": va, "desk": vb,
                                                  "rel": None})
    worst = max((m for m in moved if isinstance(m, dict) and m["rel"] is not None),
                key=lambda m: m["rel"], default=None)
    return {"same": [s for s in same if isinstance(s, str)],
            "moved": [m for m in moved if isinstance(m, dict)],
            "only_box": sorted(set(a) - set(b)),
            "only_desk": sorted(set(b) - set(a)),
            "worst": worst}


def reproduce(payload: dict, *, workdir=None, rtol: float = 1e-6) -> dict:
    """Rebuild the dataset from a digest, refit it here, and compare with the box's numbers.

    The comparison is the product: if the desk reproduces the box's parameters, a local debug
    session is trustworthy; if it does not, the difference is itself the finding (an ill-conditioned
    rail refit diverging off-box is a documented real case, which is exactly why the box's numbers
    travel in the digest instead of being recomputed here).
    """
    box_params = payload.get("params") or {}
    if not box_params:
        raise PmuError(
            what="this digest carries no fitted parameters (block D2).",
            why="D2 is what lets the desk re-emit and compare; the box dropped it to fit the "
                "budget, or it was never selected.",
            do=["Re-export from the box with a larger budget (64 or 128 KB).",
                "Or select D2 explicitly in the Digest screen's block list."],
            where="digest payload")

    root = pathlib.Path(workdir or ".") / "reproduce"
    ds = rebuild_dataset(payload, root / "dataset")
    result = {"dataset": str(root / "dataset"), "variables": ds.variables(),
              "dropped": dropped_blocks(payload),
              "coverage": {v: ds.coverage(v) for v in ds.variables()},
              "missing": ds.missing(), "box_params": box_params}
    try:
        from . import fit as fit_mod                    # lazy: the fitter may not be installed
    except ImportError:
        ds.close()
        result["refit"] = None
        result["note"] = ("pmukit.fit is not installed here, so nothing was refitted. The box's "
                          "parameters above are still complete enough to re-emit the model.")
        return result

    try:
        desk = fit_mod.fit_project(ds, None)
        desk_params = desk.to_dict() if hasattr(desk, "to_dict") else desk
    finally:
        ds.close()
    result["refit"] = desk_params
    result["compare"] = compare(box_params, desk_params, rtol=rtol)
    return result


def summary(result: dict) -> str:
    """The text `pmukit reproduce` prints: what matched, what moved, and what was never sent."""
    lines = [f"dataset rebuilt at {result.get('dataset')}",
             f"variables: {len(result.get('variables') or [])}"]
    missing = result.get("missing") or []
    if missing:
        lines.append(f"{len(missing)} cell(s) the box did not send:")
        for row in missing[:8]:
            lines.append(f"  {row[0]}  {row[-1]}")
        if len(missing) > 8:
            lines.append(f"  ... and {len(missing) - 8} more")
    cmp_ = result.get("compare")
    if cmp_ is None:
        lines.append(result.get("note", "no refit was performed"))
        return "\n".join(lines) + "\n"
    lines.append(f"{len(cmp_['same'])} parameter(s) reproduced, {len(cmp_['moved'])} moved")
    for m in cmp_["moved"][:12]:
        rel = "n/a" if m["rel"] is None else f"{m['rel']:.2e}"
        lines.append(f"  {m['param']:<40s} box {m['box']!r}  desk {m['desk']!r}  rel {rel}")
    if cmp_["only_box"]:
        lines.append(f"only in the box's fit: {', '.join(cmp_['only_box'][:8])}")
    if cmp_["only_desk"]:
        lines.append(f"only in the desk refit: {', '.join(cmp_['only_desk'][:8])}")
    return "\n".join(lines) + "\n"
