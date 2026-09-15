# from LDO_modeling/harness/fit_model.py @ d2c5b80 (fit_all's DC load-regulation branch)
"""The rail DC block: the vout table, its temperature coefficient, dropout and current limit.

    vout(load, T) = vout[load] + vout_tc[load] * (T - T_REF_C)

This is the one place where TEMPERATURE IS CONTINUOUS.  Every small-signal block is fitted at a
DISCRETE temperature inside one process corner, because cross-PVT interpolation is rejected -- a
corner gets its own `.lib` section.  The DC quantities are the documented exception: they are
fitted against the temperature SWEEP so one section covers the whole range, and `vout` is
reported AT `T_REF_C` so the table can be evaluated without hidden context.

`dropout` and `ilimit` are read off the load sweep's collapse knee.  When the sweep never leaves
regulation, or when the supply voltage is not known, they come back as `None` with the reason in
`notes` -- NEVER as a fabricated number.  A synthetic flat DC stand-in that invented a dropout
and a load-regulation slope with no flag at the model boundary is a REJECTED practice.
"""
from __future__ import annotations

import numpy as np

from ._base import (T_REF_C, BlockFit, NoData, block_cell, missing_fit, pct_rms, read_curve,
                    read_over_axis, var_layout)

__all__ = ["fit", "predict", "PARAMS", "T_REF_C"]

PARAMS = ("vout", "vout_tc", "dropout", "ilimit")

#: out of regulation = the output has fallen this far below its regulated level
COLLAPSE_FRAC = 0.05


def predict(params: dict, *, T=T_REF_C, **_ignored) -> np.ndarray:
    """Analytic vout at temperature `T` [degC] for the load this cell was fitted at."""
    T = np.asarray(T, float)
    return float(params["vout"]) + float(params.get("vout_tc") or 0.0) * (T - T_REF_C)


def _vout_vs_load(dataset, port: str, cell: dict, temp_c: float):
    """(load currents, vout) for one corner/temperature, from either encoding of `dc_load`."""
    var = f"dc_load.{port}"
    layout = var_layout(dataset, var)
    if layout is None:
        raise NoData(f"{var} was never declared in this dataset")
    cells, coord = layout
    sub = {k: v for k, v in cell.items() if k in cells}
    if "temp_c" in cells:
        sub["temp_c"] = temp_c
    if coord:
        return read_curve(dataset, var, sub)                  # one curve per corner
    if "load_a" in cells:
        sub.pop("load_a", None)
        return read_over_axis(dataset, var, sub, "load_a", port)
    raise NoData(f"{var} carries neither a load coordinate nor a load axis")


def _temps(dataset) -> list:
    try:
        return [float(t) for t in dataset.axis("temp_c")]
    except Exception:                                         # noqa: BLE001
        return []


def _temp_law(dataset, port: str, cell: dict, notes: list):
    """(vout at T_REF_C, dVout/dT, T points, vout points, source) for this corner/vset/load.

    Priority: the continuous `dc_temp` sweep, then the discrete `dc_load` cells across the
    temperature axis, then a single temperature (slope 0, said out loud).  The (T, vout) pairs
    come back so the block can score its own law against exactly what it was fitted to.
    """
    tvar = f"dc_temp.{port}"
    if var_layout(dataset, tvar) is not None:
        cells, coord = var_layout(dataset, tvar)
        sub = {k: v for k, v in cell.items() if k in cells}
        try:
            if coord:
                T, V = read_curve(dataset, tvar, sub)
            else:
                T, V = read_over_axis(dataset, tvar, sub, "temp_c", port)
                V = np.asarray([float(np.asarray(v).reshape(())) for v in V], float)
            V = np.asarray(V, float)
            if T.size >= 2:
                slope, inter = np.polyfit(T, V, 1)
                return (float(inter + slope * T_REF_C), float(slope), T, V, "dc_temp sweep")
            if T.size == 1:
                notes.append(f"dc_temp holds one point ({T[0]:g} C): vout_tc = 0")
                return float(V[0]), 0.0, T, V, "dc_temp (one point)"
        except NoData:
            pass
    temps = _temps(dataset)
    xs, ys = [], []
    for t in temps:
        try:
            loads, vout = _vout_vs_load(dataset, port, cell, t)
        except NoData:
            continue
        want = float(cell.get("load_a", loads[0]))
        ys.append(float(np.interp(want, loads, np.asarray(vout, float))))
        xs.append(float(t))
    if not xs:
        raise NoData(f"neither dc_temp.{port} nor dc_load.{port} holds data at this corner")
    T = np.asarray(xs, float)
    V = np.asarray(ys, float)
    if T.size == 1:
        notes.append(f"only one characterized temperature ({T[0]:g} C): vout_tc = 0 and vout "
                     f"is reported at that temperature, not at {T_REF_C:g} C")
        return float(V[0]), 0.0, T, V, "dc_load (single temperature)"
    slope, inter = np.polyfit(T, V, 1)
    return (float(inter + slope * T_REF_C), float(slope), T, V,
            "dc_load across the temperature axis")


def _knee(loads, vout, supply, notes):
    """(dropout, ilimit) from the load sweep's collapse knee, or (None, None) with a reason."""
    loads = np.asarray(loads, float)
    vout = np.asarray(vout, float)
    order = np.argsort(loads)
    loads, vout = loads[order], vout[order]
    vreg = float(vout[0])
    if vreg <= 0 or loads.size < 3:
        notes.append("the load sweep is too short to locate a current limit")
        return None, None
    thresh = vreg * (1.0 - COLLAPSE_FRAC)
    below = np.nonzero(vout < thresh)[0]
    if below.size == 0:
        notes.append(f"the current limit was NOT reached in the swept range (vout stays within "
                     f"{COLLAPSE_FRAC * 100:g} % of {vreg:.4f} V up to {loads[-1]:.3g} A): "
                     f"ilimit and dropout are not measured, not zero")
        return None, None
    k = int(below[0])
    if k == 0:
        notes.append("the rail is already out of regulation at the smallest swept load; the "
                     "current limit is below the characterized range")
        return None, float(loads[0])
    x0, x1 = loads[k - 1], loads[k]
    y0, y1 = vout[k - 1], vout[k]
    ilimit = float(x0 + (thresh - y0) * (x1 - x0) / (y1 - y0)) if y1 != y0 else float(x1)
    if supply is None:
        notes.append("the supply voltage is unknown, so the dropout headroom cannot be "
                     "computed from the load sweep")
        return None, ilimit
    dropout = float(supply) - float(np.interp(ilimit, loads, vout))
    return dropout, ilimit


def fit(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the DC block of one rail at one (corner, vset, load) cell."""
    cell = block_cell("dc", "rail", cell)
    notes: list = []
    temps = _temps(dataset)
    tref = min(temps, key=lambda t: abs(t - T_REF_C)) if temps else T_REF_C
    try:
        vout_ref, tc, T, V, src = _temp_law(dataset, port, cell, notes)
    except NoData as exc:
        return missing_fit(port, "dc", cell, exc.reason, metric="vout % RMS")
    n_t = int(np.asarray(T).size)
    notes.append(f"vout_tc from {src} ({n_t} temperature point(s))")

    supply = None
    if derived is not None:
        supply = (getattr(derived, "supply", {}) or {}).get("nominal_v")
    dropout = ilimit = None
    n_points = n_t
    law = {"vout": float(vout_ref), "vout_tc": float(tc)}
    # the block's own residual: the continuous law evaluated by `predict` against exactly the
    # (T, vout) pairs it was fitted to
    score = pct_rms(predict(law, T=T), V, scale=vout_ref)
    try:
        loads, vout = _vout_vs_load(dataset, port, cell, tref)
        vout = np.asarray([float(np.asarray(v).reshape(())) for v in vout], float) \
            if not isinstance(vout, np.ndarray) else np.asarray(vout, float)
        dropout, ilimit = _knee(loads, vout, supply, notes)
        n_points = int(np.asarray(loads).size) + n_t
    except NoData as exc:
        notes.append(f"no load sweep at {tref:g} C ({exc.reason}); dropout and the current "
                     f"limit are not measured")

    params = dict(law,
                  dropout=(None if dropout is None else float(dropout)),
                  ilimit=(None if ilimit is None else float(ilimit)))
    if dropout is None or ilimit is None:
        notes.append("dropout / ilimit are reported as None where they were not measured; the "
                     "emitter must not invent them")
    notes.append("dropout and ilimit have no load axis: they are the same measurement for "
                 "every load cell of this corner")
    sigma = {"vout": 0.0, "vout_tc": (0.0 if n_t > 1 else float("inf"))}
    unident = [] if n_t > 1 else ["vout_tc"]
    return BlockFit(port=port, block="dc", cell=cell, params=params, score=float(score),
                    metric="vout % RMS", n_points=n_points,
                    identifiability={"cond": (1.0 if n_t > 1 else float("inf")),
                                     "sigma": sigma, "unidentifiable": unident},
                    notes=notes)
