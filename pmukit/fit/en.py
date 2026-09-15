"""The enable ramp: how far and how fast each modeled port moves after the EN edge.

    x(t) = 0                                            t < t_delay
           final * step(t - t_delay; t_rise, overshoot)  otherwise

This block is the `en` TIER, and the tier means exactly what it says: usable, NOT signed off.
It guarantees that a consumer bench toggling EN does not blow up -- startup is signed off on the
real LDO, never on this model.

NOT A PORT of the old repo: that repo had no EN block, so nothing here is copied from it.  The
measurement is the standard one (10 % / 90 % crossings of the settled level, overshoot above it)
and the analytic replay is a second-order step whose damping is set by the measured overshoot,
which is the smallest shape that carries all three fitted numbers.

The block belongs to port type `en` because it is the enable pin's behaviour, but the numbers
are per MODELED port: each rail and each bias has its own `tran_en.<port>` waveform, so
`fit(dataset, port, ...)` is called once per port that was measured on the EN edge.  A rail
fills `t_rise`/`v_overshoot`, a bias fills `i_rise`/`i_overshoot`; the other pair stays None.
"""
from __future__ import annotations

import numpy as np

from ._base import BlockFit, NoData, block_cell, missing_fit, pct_rms, read_curve

__all__ = ["fit", "predict", "PARAMS"]

PARAMS = ("t_delay", "t_rise", "v_overshoot", "i_rise", "i_overshoot")

#: 10 % / 90 % of the settled excursion -- the rise time everyone means
LO_FRAC, HI_FRAC = 0.1, 0.9
#: an overshoot below this fraction of the excursion is treated as none (a first-order ramp)
OVERSHOOT_EPS = 1e-3


def _damping(os_frac: float) -> float:
    """Second-order damping ratio from a fractional overshoot."""
    os_frac = float(min(max(os_frac, 1e-6), 0.95))
    ln = np.log(os_frac)
    return float(-ln / np.sqrt(np.pi ** 2 + ln ** 2))


def _shape(tau, t_rise: float, os_frac: float) -> np.ndarray:
    """The normalized step response, 0 at the edge and 1 when settled.

    First order when no overshoot was resolved; otherwise a second-order step whose damping
    comes from the measured overshoot and whose natural frequency comes from the measured rise
    time.  ONE function, used by both the measurement and `predict`, so `t_delay` means the same
    instant in both -- measuring the delay at the 10 % crossing while replaying it as the start
    of the ramp would bake in a fixed offset.
    """
    tau = np.asarray(tau, float)
    x = np.zeros_like(tau)
    on = tau > 0
    if t_rise <= 0:
        x[on] = 1.0
        return x
    if os_frac <= OVERSHOOT_EPS:
        t0 = t_rise / np.log(9.0)                  # 10 % -> 90 % of an exponential = ln(9) tau
        x[on] = 1.0 - np.exp(-tau[on] / t0)
        return x
    z = _damping(os_frac)
    wn = (2.16 * z + 0.6) / t_rise                 # the standard 10-90 % rise-time relation
    rt = np.sqrt(max(1.0 - z * z, 1e-12))
    wd = wn * rt
    e = np.exp(-z * wn * tau[on])
    x[on] = 1.0 - e * (np.cos(wd * tau[on]) + (z / rt) * np.sin(wd * tau[on]))
    return x


def _tau_at(frac: float, t_rise: float, os_frac: float) -> float:
    """How long after the edge the normalized shape first reaches `frac`."""
    if t_rise <= 0:
        return 0.0
    grid = np.linspace(0.0, 6.0 * t_rise, 4001)
    x = _shape(grid, t_rise, os_frac)
    idx = np.nonzero(x >= frac)[0]
    if idx.size == 0:
        return 0.0
    k = int(idx[0])
    if k == 0:
        return 0.0
    x0, x1 = x[k - 1], x[k]
    f = (frac - x0) / (x1 - x0) if x1 != x0 else 0.0
    return float(grid[k - 1] + f * (grid[k] - grid[k - 1]))


def predict(params: dict, *, t, final: float = 1.0, start: float = 0.0, **_ignored):
    """Analytic ramp in the port's own unit.

    `final` and `start` are CONTEXT (the settled level comes from the `dc` block for a rail and
    from the `idc` block for a bias), so this block stays three numbers about the EDGE and does
    not duplicate a level that is fitted elsewhere.
    """
    t = np.asarray(t, float)
    span = float(final) - float(start)
    td = float(params.get("t_delay") or 0.0)
    tr = float(params.get("t_rise") or params.get("i_rise") or 0.0)
    over = params.get("v_overshoot")
    if over is None:
        over = params.get("i_overshoot")
    os_frac = (abs(float(over)) / abs(span)) if (over and span) else 0.0
    return float(start) + span * _shape(t - td, tr, os_frac)


def _measure(t, y):
    """(start, final, t_delay, t_rise, overshoot) of one EN transient.

    The transient's time origin IS the EN edge, so `t_delay` is measured from `t[0]`.
    """
    t = np.asarray(t, float)
    y = np.asarray(y, float)
    n = max(3, int(0.05 * y.size))
    start = float(np.median(y[:n]))
    final = float(np.median(y[-n:]))
    span = final - start
    if span == 0.0:
        raise NoData("the EN transient never moves: start and settled level are identical")
    lo = start + LO_FRAC * span
    hi = start + HI_FRAC * span

    def cross(level):
        rel = (y - start) / span
        want = (level - start) / span
        idx = np.nonzero(rel >= want)[0]
        if idx.size == 0:
            return float(t[-1])
        k = int(idx[0])
        if k == 0:
            return float(t[0])
        y0, y1 = rel[k - 1], rel[k]
        f = (want - y0) / (y1 - y0) if y1 != y0 else 0.0
        return float(t[k - 1] + f * (t[k] - t[k - 1]))

    t10, t90 = cross(lo), cross(hi)
    peak = float(np.max(y)) if span > 0 else float(np.min(y))
    overshoot = max((peak - final) if span > 0 else (final - peak), 0.0)
    t_rise = max(t90 - t10, 0.0)
    # `t_delay` is the EDGE of the replayed shape, not the 10 % crossing: subtract the shape's
    # own time-to-10 % so that `predict` puts the 10 % point back exactly where it was measured.
    os_frac = (overshoot / abs(span)) if span else 0.0
    t_delay = max(t10 - float(t[0]) - _tau_at(LO_FRAC, t_rise, os_frac), 0.0)
    return start, final, t_delay, t_rise, overshoot


def _is_bias(dataset, port: str, derived) -> bool:
    if derived is not None:
        if port in (getattr(derived, "biases", {}) or {}):
            return True
        if port in (getattr(derived, "rails", {}) or {}):
            return False
    rec = getattr(dataset, "_index", {}).get("variables", {}).get(f"tran_en.{port}")
    unit = str((rec or {}).get("unit", "")).strip().upper()
    return unit.startswith("A")


def fit(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the EN ramp of one modeled port at one (corner, temperature) cell."""
    bcell = block_cell("ramp", "en", cell)
    var = f"tran_en.{port}"
    metric = "EN ramp % RMS"
    try:
        t, y = read_curve(dataset, var, dict(cell))
    except NoData as exc:
        return missing_fit(port, "ramp", bcell, exc.reason, metric=metric)
    if t.size < 4:
        return missing_fit(port, "ramp", bcell, f"{var}: only {t.size} usable sample(s)",
                           metric=metric)
    try:
        start, final, t_delay, t_rise, over = _measure(t, y)
    except NoData as exc:
        return missing_fit(port, "ramp", bcell, exc.reason, metric=metric)

    bias = _is_bias(dataset, port, derived)
    params = {"t_delay": float(t_delay),
              "t_rise": (None if bias else float(t_rise)),
              "v_overshoot": (None if bias else float(over)),
              "i_rise": (float(t_rise) if bias else None),
              "i_overshoot": (float(over) if bias else None)}
    model = predict(params, t=t, final=final, start=start)
    score = pct_rms(model, y, scale=(final - start))
    notes = [f"measured on {var}: the transient's time origin is the EN edge",
             f"settled level {final:.6g} (start {start:.6g}); the level itself belongs to the "
             f"{'idc' if bias else 'dc'} block, this block carries only the edge",
             "tier `en`: usable, NOT signed off -- sign startup off on the real LDO"]
    if over <= OVERSHOOT_EPS * abs(final - start):
        notes.append("no overshoot resolved: the replay is a first-order ramp")
    return BlockFit(port=port, block="ramp", cell=bcell, params=params, score=float(score),
                    metric=metric, n_points=int(t.size),
                    identifiability={"cond": 1.0,
                                     "sigma": {"t_delay": 0.0,
                                               ("i_rise" if bias else "t_rise"): 0.0},
                                     "unidentifiable": []},
                    notes=notes)
