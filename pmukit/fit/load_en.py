# from LDO_modeling/harness/fit_iassist.py + harness/fit_ovvdz.py @ d2c5b80
"""The large-signal tier: the droop when the consumer's load turns on, the overshoot when it
turns off.

    i_assist(verr) = iaG * tanh( verr*|verr| / iaV^2 ),        verr = vreg - vout
    discharge      = gate(vov) * srcblk(I) * ovVmax * tanh( (ovR/ovVmax) * I )
        gate   = (vov > ovVdz) ? tanh( (vov - ovVdz)^2 / ovVsc^2 ) : 0     vov = vout - vreg
        srcblk = 0.5 * (1 - tanh(I/ovIsc))

The assist is ODD with `f'(0) = 0` EXACTLY, so Zout, PSRR and noise at the operating point are
bit-identical with it in place -- it is a large-EXCURSION safety net, not a small-signal term.
It replaces a LINEAR over-prediction with the sub-linear class-AB stiffening the silicon shows.

The unload discharge drains the fit inductor.  An OUTPUT-side clamp CANNOT do this job: with a
lossless fit inductor `dI/dt = V_across/L`, so bounding the overshoot small forces a small
inductor voltage and therefore a slow drain -- depth x recovery-time is roughly constant.  The
energy has to be removed AT THE INDUCTOR, through a voltage-BOUNDED reverse EMF (so the
regulation always keeps ~1/Ra conductance and the DC pin can never be lost) that is SOURCE-gated
(so a sustained external sink never engages it).  A stateful / leaky-integral gate was REFUTED
too: the memory lags the nanosecond peak and its transparency is frequency-limited.

**`ovVdz` is a TRANSPARENCY FLOOR, not a droop margin.**  The dump engages for ANY excursion
above `vreg + ovVdz` while branch A sources -- including the positive half of a legitimate
PERIODIC ripple.  Since this model exists to produce spectra, `ovVdz` must sit ABOVE the largest
legitimate periodic excursion or it clips the very spectra it was built for.  Sub-microsecond
settling (which would need a few mV) is the deliberately sacrificed axis.

Everything here is PURE PYTHON: the model's own load-step response is a small KNOWN linear RLC
network (the branch-A ladder plus the node capacitance) with one nonlinear feedback, so it is
SOLVED AS AN ODE rather than re-simulated.  There is no second simulation, and no simulator.

DIRECTION comes from `sign(to_a - from_a)` in the plan's declared event -- NEVER parsed from a
label, which may be opaque.
"""
from __future__ import annotations

import math

import numpy as np

from ._base import (BlockFit, NoData, block_cell, missing_fit, read_curve, var_layout)
from . import zout as zout_block

__all__ = ["fit", "predict", "rail_trace", "predict_dip", "predict_overshoot", "fit_assist",
           "PARAMS"]

PARAMS = ("iaG", "iaV", "ovVdz", "ovR", "ovVmax", "ovVsc", "ovIsc")

#: shipped discharge dynamics (INTERNAL physics, not user knobs)
OVR_DEFAULT = 4e3
OVVMAX_DEFAULT = 2.0
OVVSC_DEFAULT = 8e-3
OVISC_DEFAULT = 1e-3
#: the knee voltage of the assist when a single load event cannot separate the two parameters
IAV_DEFAULT = 0.3
#: the ripple proxy: the largest LEGITIMATE periodic load ripple, as a fraction of the
#: operating load, and the transparency margin on top of it
RIPPLE_FRAC = 0.10
K_MARGIN = 2.5
#: the floor is also where the overshoot settles, so cap it well under the headroom
ALPHA_HEADROOM = 0.5
#: a few mV: safely above the DC load droop and above any sustained-sink droop
VDZ_MIN = 5.0e-3


# --------------------------------------------------------------------------- the ODE


def rail_trace(Ra, sections, Cext, i_from, i_to, iaG, iaV, vreg, t0, edge, tstop,
               discharge=None, t_eval=None):
    """Solve the branch-A ladder + node capacitance + compressive assist [+ the Route-1 unload
    discharge] as an ODE.  `sections` = [(L1,R1), (L2,R2), ...] from the output node inward,
    then Ra from the last node to the regulation reference.  Inductor currents and Vout are the
    states; the internal ladder nodes are solved algebraically at each step.

    `discharge=None` gives the bare (assist-only) network, whose algebraic solve is a plain
    linear KCL.  With the discharge on, the last-node branch law becomes nonlinear and is
    reduced to a SCALAR root in the branch current (an N-dimensional solve there was
    non-convergent and slow; this is robust and fast).
    """
    from scipy.integrate import solve_ivp
    L = np.array([s[0] for s in sections], float)
    G = np.array([1.0 / s[1] for s in sections], float)
    Gra = 1.0 / Ra
    N = len(sections)
    if discharge is not None:
        ovVdz = float(discharge["ovVdz"])
        ovR = float(discharge.get("ovR", OVR_DEFAULT))
        ovVmax = float(discharge.get("ovVmax", OVVMAX_DEFAULT))
        ovVsc = float(discharge.get("ovVsc", OVVSC_DEFAULT))
        ovIsc = float(discharge.get("ovIsc", OVISC_DEFAULT))

    def _emf(I, vov):
        if vov <= ovVdz:
            return 0.0
        g = np.tanh((vov - ovVdz) ** 2 / (ovVsc * ovVsc))
        srcblk = 0.5 * (1.0 - np.tanh(I / ovIsc))
        return g * srcblk * ovVmax * np.tanh((ovR / ovVmax) * I)

    # The internal-node KCL is LINEAR with a CONSTANT matrix: `A V = D ivec + g0 Vo + c`. So it
    # is factored ONCE here instead of assembled and solved at every right-hand-side call (the
    # ODE takes ~4000 steps, and this solve was 70 % of the fit's large-signal time).
    A = np.zeros((N, N))
    D = np.zeros((N, N))
    g0 = np.zeros(N)
    c = np.zeros(N)
    for k in range(N):
        A[k, k] += G[k]
        if k > 0:
            A[k, k - 1] -= G[k]
        else:
            g0[k] += G[k]
        if k < N - 1:
            A[k, k] += G[k + 1]
            A[k, k + 1] -= G[k + 1]
            D[k, k] += 1.0
            D[k, k + 1] -= 1.0
        else:
            A[k, k] += Gra
            D[k, k] += 1.0
            c[k] += Gra * vreg
    Ainv = np.linalg.inv(A)
    P, q, r = Ainv @ D, Ainv @ g0, Ainv @ c          # V = P ivec + q Vo + r
    if discharge is not None:
        # with the dump engaged the last branch is nonlinear: split V[N-1] = a - I_Ra * bb
        A0 = A.copy()
        A0[N - 1, N - 1] -= Gra
        A0inv = np.linalg.inv(A0)
        c0 = c.copy()
        c0[N - 1] -= Gra * vreg
        P0, q0, r0 = A0inv @ D, A0inv @ g0, A0inv @ c0
        e = np.zeros(N)
        e[N - 1] = 1.0
        y = A0inv @ e
        bb = float(y[N - 1])

    def algebraic(ivec, Vo):
        V = P @ ivec + q * Vo + r
        if discharge is None:
            return V
        vov = Vo - vreg
        if vov <= ovVdz:                      # the gate's value AND slope vanish -> linear
            return V
        from scipy.optimize import brentq
        x = P0 @ ivec + q0 * Vo + r0
        a = float(x[N - 1])                   # V[N-1] = a - I_Ra*bb
        I0 = (a - vreg) / (Ra + bb)

        def gfun(I):
            return _emf(I, vov) + (Ra + bb) * I - (a - vreg)
        span = (ovVmax + 1e-3) / (Ra + bb) + 1e-9
        lo_i, hi_i = I0 - span, I0 + span
        tries = 0
        while gfun(lo_i) * gfun(hi_i) > 0 and tries < 40:
            span *= 2.0
            lo_i, hi_i = I0 - span, I0 + span
            tries += 1
        I_Ra = brentq(gfun, lo_i, hi_i, xtol=1e-15, rtol=1e-13) \
            if gfun(lo_i) * gfun(hi_i) <= 0 else I0
        return x - I_Ra * y

    def iload(t):
        return i_from if t < t0 else i_from + (i_to - i_from) * min(1.0, (t - t0) / edge)

    iaV2 = iaV * iaV

    def iassist(Vo):
        verr = vreg - Vo
        return iaG * math.tanh(verr * abs(verr) / iaV2) if iaG > 0 else 0.0

    G0 = float(G[0])

    def f(t, x):
        ivec = x[:N]
        Vo = float(x[N])
        V = algebraic(ivec, Vo)
        out = np.empty(N + 1)
        out[0] = Vo - V[0]
        out[1:N] = V[:-1] - V[1:]
        out[:N] /= L
        out[N] = (iassist(Vo) - iload(t) - ivec[0] - G0 * (Vo - V[0])) / Cext
        return out

    x0 = list(np.full(N, -i_from)) + [vreg - i_from * Ra]
    return solve_ivp(f, (0, t0 + tstop), x0, method="LSODA", max_step=5 * edge,
                     rtol=1e-6, atol=1e-10, t_eval=t_eval)


def predict_dip(Ra, sections, Cext, i_from, i_to, iaG=0.0, iaV=IAV_DEFAULT, vreg=0.8,
                t0=1e-6, edge=1e-9, tstop=1.5e-6) -> float:
    """The rail's load-step droop [mV] (a LOAD step: i_to > i_from).  `iaG = 0` is the bare
    linear baseline."""
    sol = rail_trace(Ra, sections, Cext, i_from, i_to, iaG, iaV, vreg, t0, edge, tstop)
    return (vreg - float(sol.y[len(sections)].min())) * 1e3


def predict_overshoot(Ra, sections, Cext, i_from, i_to, ovVdz, iaG=0.0, iaV=IAV_DEFAULT,
                      vreg=0.8, ovR=OVR_DEFAULT, ovVmax=OVVMAX_DEFAULT, ovVsc=OVVSC_DEFAULT,
                      ovIsc=OVISC_DEFAULT, t0=1e-6, edge=1e-9, tstop=8e-6) -> float:
    """The rail's UNLOAD-overshoot PEAK [mV above vreg] -- the dual of `predict_dip`.

    MONOTONE INCREASING in `ovVdz` (the dump engages later, so the peak is higher), which is
    what makes it invertible against a measured overshoot.  The assist is odd, so it also sinks
    during the overshoot; pass the derived values for fidelity.
    """
    disc = {"ovVdz": ovVdz, "ovR": ovR, "ovVmax": ovVmax, "ovVsc": ovVsc, "ovIsc": ovIsc}
    sol = rail_trace(Ra, sections, Cext, i_from, i_to, iaG, iaV, vreg, t0, edge, tstop,
                     discharge=disc)
    return (float(sol.y[len(sections)].max()) - vreg) * 1e3


def predict(params: dict, *, t, zout, i_from, i_to, vreg, cext=None, edge=1e-9, t0=None,
            **_ignored) -> np.ndarray:
    """Analytic Vout(t) for one declared load event, from the fitted large-signal parameters.

    This is an ODE solve of the model's OWN network, not a simulation of the device: the
    network is the fitted branch-A ladder plus the fitted output capacitance, and the only
    nonlinearities are the two fitted terms.  No process is launched.
    """
    t = np.asarray(t, float)
    Ra = float(zout["Ra"])
    sections = zout_block.sections_of(zout)
    Cext = float(cext if cext is not None else zout["Cout"])
    t0 = float(t0 if t0 is not None else t[0] + 0.1 * (t[-1] - t[0]))
    disc = None
    if float(i_to) < float(i_from) and params.get("ovVdz") is not None:
        disc = {k: float(params.get(k, d)) for k, d in
                (("ovVdz", VDZ_MIN), ("ovR", OVR_DEFAULT), ("ovVmax", OVVMAX_DEFAULT),
                 ("ovVsc", OVVSC_DEFAULT), ("ovIsc", OVISC_DEFAULT))}
    sol = rail_trace(Ra, sections, Cext, float(i_from), float(i_to),
                     float(params.get("iaG") or 0.0), float(params.get("iaV") or IAV_DEFAULT),
                     float(vreg), t0, float(edge), float(t[-1] - t0),
                     discharge=disc, t_eval=np.clip(t, 0.0, None))
    return np.asarray(sol.y[len(sections)], float)


# --------------------------------------------------------------------------- the assist fit


def _grid(lo, hi, n, log=False):
    if log:
        return list(np.exp(np.linspace(np.log(lo), np.log(hi), n)))
    return list(np.linspace(lo, hi, n))


def fit_assist(Ra, sections, Cext, gt_dips, vreg, iaG_range=(5.0e-4, 1.2e-2),
               iaV_range=(0.10, 0.50), n=8, refine=True, t0=1e-6, edge=1e-9, tstop=1.5e-6):
    """Solve `(iaG, iaV)` so the predicted load-step droops match the measured ones.

    `gt_dips = {(i_from, i_to): dip_V}` -- each step carries its OWN baseline, so a mixed
    -baseline coverage set fits correctly with no shared-baseline assumption.  Grid, then local
    refine, then a MINIMAL-INTERVENTION tie-break: the two parameters are partly degenerate (a
    whole valley matches the same droops), so among the solutions within the margin of the best
    RMS the SMALLEST `iaG` -- the gentlest assist -- wins.  With three or more amplitudes it
    also reports a HELD-OUT prediction (fit the outer step sizes, predict the middle).

    With a SINGLE measured droop the pair cannot be separated: `iaV` is held at its default and
    only `iaG` is solved, which is said out loud in the diagnostics.
    """
    targets = sorted(gt_dips.items(), key=lambda kv: kv[0][1] - kv[0][0])
    if not targets:
        return None
    PAIRS = [k for k, _ in targets]
    DI = [to - frm for frm, to in PAIRS]
    GT = [dip * 1e3 for _, dip in targets]

    def dips(iaG, iaV):
        return [predict_dip(Ra, sections, Cext, frm, to, iaG, iaV, vreg,
                            t0=t0, edge=edge, tstop=tstop) for frm, to in PAIRS]

    def rms(d, idx):
        return float(np.sqrt(np.mean([(d[i] - GT[i]) ** 2 for i in idx])))

    def pick(scored):
        best = min(s[3] for s in scored)
        margin = max(1.5, 0.10 * best)
        return min((s for s in scored if s[3] <= best + margin), key=lambda s: s[0])

    if len(targets) == 1:
        bv = IAV_DEFAULT
        scored = [(g, bv, d, rms(d, [0]))
                  for g in _grid(*iaG_range, 3 * n, log=True) for d in [dips(g, bv)]]
        bg, bv, bd, _ = pick(scored)
        held = None
        degenerate = True
    else:
        scored = [(g, v, d, rms(d, range(len(GT))))
                  for g in _grid(*iaG_range, n, log=True) for v in _grid(*iaV_range, n)
                  for d in [dips(g, v)]]
        bg, bv, bd, _ = pick(scored)
        if refine:
            scored += [(g, v, d, rms(d, range(len(GT))))
                       for g in _grid(bg * 0.6, bg * 1.5, 5, log=True)
                       for v in _grid(max(0.05, bv - 0.08), bv + 0.08, 5) for d in [dips(g, v)]]
            bg, bv, bd, _ = pick(scored)
        held = None
        if len(GT) >= 3:
            outer = [0, len(GT) - 1]
            mid = len(GT) // 2
            ho = min(scored, key=lambda s: rms(s[2], outer))
            held = {"fit_di": [DI[i] for i in outer], "predict_di": DI[mid],
                    "pred_dip": ho[2][mid], "gt_dip": GT[mid],
                    "err_pct": 100.0 * (ho[2][mid] - GT[mid]) / GT[mid]}
        degenerate = False
    on_edge = (abs(bg - iaG_range[0]) / bg < 0.02 or abs(bg - iaG_range[1]) / bg < 0.02)
    return {"iaG": bg, "iaV": bv,
            "_diag": {"di": DI, "gt_dip": GT, "model_dip": bd,
                      "rms_mV": rms(bd, range(len(GT))), "held_out": held,
                      "on_boundary": bool(on_edge), "single_event": degenerate}}


def _invert_overshoot(Ra, sections, Cext, i_from, i_to, target_mV, iaG, iaV, vreg, ovd,
                      lo=1.0e-4, hi=0.2):
    """Solve `ovVdz` so the predicted PEAK equals the measured worst-case overshoot.  The peak
    is monotone increasing in `ovVdz`, so bisect; clamp to an endpoint when the target is
    outside the reachable range."""
    def peak(v):
        return predict_overshoot(Ra, sections, Cext, i_from, i_to, v, iaG=iaG, iaV=iaV,
                                 vreg=vreg, **ovd)
    if target_mV <= peak(lo):
        return lo
    if target_mV >= peak(hi):
        return hi
    for _ in range(20):
        mid = 0.5 * (lo + hi)
        pm = peak(mid)
        if abs(pm - target_mV) < 0.3:
            return mid
        lo, hi = (mid, hi) if pm < target_mV else (lo, mid)
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- measurement


def _events(derived, port: str):
    """The plan's DECLARED load events, keyed by observable.  Direction is the SIGN of
    `to_a - from_a`, never the label."""
    out = {}
    if derived is None:
        return out
    ev = ((getattr(derived, "loads", {}) or {}).get(port) or {}).get("events") or []
    for e in ev:
        try:
            frm, to = float(e["from_a"]), float(e["to_a"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(e.get("event") or "")
        out[name] = {"from_a": frm, "to_a": to, "edge_s": float(e.get("edge_s") or 1e-9),
                     "loading": to > frm}
    return out


def _excursion(t, v):
    """(pre-step level, t0, min, max) of one transient.

    The excursions themselves are measured against the REGULATION SETPOINT, not against this
    pre-step level: the pre-step level of an UNLOAD waveform is the LOADED level, which sits
    `i_from * Ra` below the setpoint, and measuring an overshoot from there would inflate it by
    exactly the DC load droop -- and `predict_overshoot` reports its peak above the setpoint.
    """
    t = np.asarray(t, float)
    v = np.asarray(v, float)
    n_pre = max(3, int(0.1 * v.size))
    v_pre = float(np.median(v[:n_pre]))
    dev = np.abs(v - v_pre)
    peak = float(np.max(dev))
    idx = np.nonzero(dev > 0.05 * peak)[0] if peak > 0 else np.array([0])
    t0 = float(t[idx[0]]) if idx.size else float(t[0])
    return v_pre, t0, float(np.min(v)), float(np.max(v))


def _gather(dataset, port: str, var: str, cell: dict):
    """{load_a or None: (t, v)} for one transient observable -- across the load axis when the
    variable carries one, otherwise the single cell."""
    layout = var_layout(dataset, var)
    if layout is None:
        raise NoData(f"{var} was never declared in this dataset")
    cells, _ = layout
    out = {}
    if "load_a" in cells:
        for il in dataset.axis("load_a", port):
            try:
                t, v = read_curve(dataset, var, dict(cell, load_a=float(il)))
            except NoData:
                continue
            out[float(il)] = (t, v)
    else:
        t, v = read_curve(dataset, var, dict(cell))
        out[None] = (t, v)
    if not out:
        raise NoData(f"{var}: no cell at this corner holds a waveform")
    return out


# --------------------------------------------------------------------------- the block


def fit(dataset, port: str, cell: dict, derived=None, *, zout_params=None) -> BlockFit:
    """Fit the large-signal block of one rail at one (corner, temperature) cell."""
    bcell = block_cell("load_en", "rail", cell)
    metric = "load-step droop % error"
    notes: list = []
    on_var, off_var = f"tran_load_on.{port}", f"tran_load_off.{port}"
    try:
        on_waves = _gather(dataset, port, on_var, cell)
    except NoData as exc:
        return missing_fit(port, "load_en", bcell, exc.reason, metric=metric)

    if zout_params is None:
        zf = zout_block.fit(dataset, port, cell, derived)
        if zf.missing:
            return missing_fit(port, "load_en", bcell,
                               "the large-signal replay needs the fitted branch-A ladder, and "
                               "Zout is " + zf.notes[0], metric=metric)
        zout_params = zf.params
        notes.append("Zout was not supplied; it was fitted here from the same cell")
    Ra = float(zout_params["Ra"])
    sections = zout_block.sections_of(zout_params)
    Cext = float(zout_params["Cout"])
    notes.append(f"the replay network is the rail's OWN fitted ladder and output capacitance "
                 f"(Cout = {Cext * 1e12:.3g} pF): the plan characterizes the intrinsic rail, "
                 f"so there is no external transient decap to de-embed")

    ev = _events(derived, port)
    on_ev = ev.get("tran_load_on")
    off_ev = ev.get("tran_load_off")
    if on_ev is None:
        return missing_fit(port, "load_en", bcell,
                           f"{on_var} holds a waveform but the derived config declares no "
                           f"tran_load_on event, so the step currents are unknown",
                           metric=metric)
    if not on_ev["loading"]:
        notes.append(f"the declared tran_load_on event has to_a <= from_a; the DIRECTION is "
                     f"taken from sign(to_a - from_a), not from the label")

    dips = {}
    vreg = None
    t0 = edge = tstop = None
    for il, (t, v) in sorted(on_waves.items(), key=lambda kv: (kv[0] is not None, kv[0])):
        v_pre, t_start, vmin, _ = _excursion(t, v)
        frm = float(on_ev["from_a"])
        to = float(on_ev["to_a"]) if il is None else float(il)
        if vreg is None:
            # back the DC load droop out of the pre-step level to recover the REGULATION
            # SETPOINT, which is what the replay drives and what `predict_dip` measures from
            vreg = v_pre + frm * Ra
            t0 = t_start
            edge = float(on_ev["edge_s"])
            tstop = float(t[-1]) - t0
        if to == frm:
            continue
        dips[(frm, to)] = float(vreg - vmin)
    if not dips:
        return missing_fit(port, "load_en", bcell,
                           f"{on_var}: no usable load step (every waveform has to_a == from_a)",
                           metric=metric)

    res = fit_assist(Ra, sections, Cext, dips, vreg, t0=t0, edge=edge, tstop=tstop)
    if res is None:
        return missing_fit(port, "load_en", bcell, f"{on_var}: the droop fit found no solution",
                           metric=metric)
    diag = res["_diag"]
    if diag["single_event"]:
        notes.append(f"ONE load event is characterized, and (iaG, iaV) are partly degenerate: "
                     f"iaV is held at its default {IAV_DEFAULT} and only iaG is solved. A "
                     f"second step amplitude would separate them.")
    if diag["held_out"]:
        notes.append(f"held out across amplitudes: predicting the middle step from the outer "
                     f"two misses by {diag['held_out']['err_pct']:+.1f} %")
    if diag["on_boundary"]:
        notes.append("iaG landed on its search boundary -- treat the value with suspicion")

    # ---- the unload discharge -------------------------------------------------------
    ovVdz = None
    method = "none"
    headroom_hi = float("inf")
    supply = (getattr(derived, "supply", {}) or {}).get("nominal_v") if derived else None
    if supply is not None and vreg is not None and float(supply) > vreg:
        headroom_hi = ALPHA_HEADROOM * (float(supply) - vreg)
    try:
        off_waves = _gather(dataset, port, off_var, cell)
    except NoData as exc:
        off_waves = {}
        notes.append(f"no unload transient ({exc.reason}); the transparency floor falls back "
                     f"to the ripple proxy")
    overs = {}
    if off_waves and off_ev is not None:
        for il, (t, v) in off_waves.items():
            _, _, _, vmax = _excursion(t, v)
            frm = float(off_ev["from_a"]) if il is None else float(il)
            to = float(off_ev["to_a"])
            if frm == to:
                continue
            overs[(frm, to)] = float(vmax - vreg)        # above the SETPOINT, never the
                                                         # loaded pre-step level
    if overs:
        ovd = {}
        (wf, wt), gt_peak_V = max(overs.items(), key=lambda kv: kv[1])
        op_over_V = max((v for k, v in overs.items() if k != (wf, wt)), default=0.0)
        fit_v = _invert_overshoot(Ra, sections, Cext, wf, wt, gt_peak_V * 1e3,
                                  res["iaG"], res["iaV"], vreg, ovd)
        ovVdz = max(fit_v, op_over_V)
        method = "measured overshoot"
        notes.append(f"ovVdz fitted so the replayed peak matches the MEASURED worst-case "
                     f"overshoot ({gt_peak_V * 1e3:.2f} mV), then raised to at least the "
                     f"largest everyday unload ({op_over_V * 1e3:.2f} mV)")
    else:
        zmax = float(np.max(np.abs(zout_block.predict(zout_params,
                                                      f=np.logspace(1, 9, 401)))))
        i_op = float(on_ev["to_a"])
        di = RIPPLE_FRAC * i_op
        ovVdz = K_MARGIN * zmax * di
        method = "ripple proxy"
        notes.append(f"ovVdz from the ripple proxy: {K_MARGIN} x max|Zout| ({zmax:.4g} ohm) x "
                     f"{RIPPLE_FRAC:.0%} of the operating load ({i_op:.4g} A)")
    capped = "none"
    if ovVdz < VDZ_MIN:
        ovVdz, capped = VDZ_MIN, "floor"
    if ovVdz > headroom_hi:
        ovVdz, capped = headroom_hi, "headroom"
    if capped != "none":
        notes.append(f"ovVdz clamped at the {capped}")
    notes.append("ovVdz is a TRANSPARENCY FLOOR, not a droop margin: below the largest "
                 "legitimate periodic ripple the dump clips the spectra this model exists to "
                 "produce")

    params = {"iaG": float(res["iaG"]), "iaV": float(res["iaV"]), "ovVdz": float(ovVdz),
              "ovR": OVR_DEFAULT, "ovVmax": OVVMAX_DEFAULT, "ovVsc": OVVSC_DEFAULT,
              "ovIsc": OVISC_DEFAULT}
    gt = np.asarray(diag["gt_dip"], float)
    model = np.asarray(diag["model_dip"], float)
    score = float(np.sqrt(np.mean(((model - gt) / np.maximum(np.abs(gt), 1e-30)) ** 2)) * 100.0)
    sigma = {"iaG": 0.0, "iaV": (float("inf") if diag["single_event"] else 0.0),
             "ovVdz": (0.0 if method == "measured overshoot" else float("inf"))}
    unident = [k for k, v in sigma.items() if not np.isfinite(v)]
    if unident:
        notes.append("identifiability: the data does not determine " + ", ".join(unident)
                     + (" (ovVdz came from the ripple proxy, not from a measured overshoot)"
                        if "ovVdz" in unident else ""))
    return BlockFit(port=port, block="load_en", cell=bcell, params=params, score=score,
                    metric=metric, n_points=len(gt),
                    identifiability={"cond": float("nan"), "sigma": sigma,
                                     "unidentifiable": unident},
                    notes=notes)
