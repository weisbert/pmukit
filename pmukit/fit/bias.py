# from LDO_modeling/harness/fit_isrc.py @ d2c5b80
"""The current-bias port: its DC I-V and PTAT law, its output admittance, its noise and its
supply-to-current transfer.

    I(Vo, T) = ( idc(T) + g0*(Vo - vc) ) * GATE(Vo)
    idc(T)   = idc + ptat_slope * (T - T_REF_C)
    GATE     = tanh( (max(vhi - Vo, 0)/vknee)^knee_p )   'hi'   compliance CEILING
               tanh( (max(Vo, 0)   /vknee)^knee_p )      'lo'   turn-off knee at ground
               1                                         'none' no knee in the swept range
    Y(s)     = g0*(1 + s/wz)/(1 + s/wp) + s*Cp
    In(f)    = sqrt(white^2 + flicker^2/f)
    gdd(s)   = gdd / (1 + s/(2 pi psrr_pole_hz))

Two shipped bugs are fixed here by construction and must never be re-introduced:

  * **DIRECTION IS DETECTED, NEVER ASSUMED.**  `pol = "source" if Idc(OP) >= 0 else "sink"`,
    taken from the sign of the probe current, and the shape is fitted on `|I|`.  A hardcoded
    "sink" made a reference that SOURCES emit as a sink -- the model DREW the current the real
    reference INJECTS, and the deployed model read the opposite sign of the ground truth across
    the whole I-V sweep.
  * **THE COMPLIANCE KNEE IS ONE-SIDED.**  `max(vhi - Vo, 0)`, never the symmetric
    `sqrt((vhi-Vo)^2 + eps)`: a symmetric gate climbs back to 1 ABOVE the ceiling, so the sink
    spuriously REOPENS to full current where the real device is starved.  It is numerically
    identical below the ceiling, i.e. over the whole characterized range.

`g0` is taken from the DC real part of the AC ADMITTANCE, not from the full-sweep I-V chord.
The chord crosses the turn-off knee and comes out about 225x too steep, which once baked a
29-37 % I-V error into a model whose report read 0.3-1.2 %.  The I-V law and the `yout` block
therefore share ONE conductance -- that is what makes the emitted model equal the graded one.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from . import identifiability as ident
from ._base import (TWO_PI, T_REF_C, BlockFit, NoData, block_cell, db_rms, missing_fit,
                    pct_rms, read_curve, read_over_axis, var_layout)

__all__ = ["fit", "predict", "fit_idc", "fit_yout", "fit_noise", "fit_psrr", "gate",
           "predict_iv", "predict_idc_t", "predict_y", "predict_noise", "predict_psrr",
           "BLOCKS"]

BLOCKS = ("idc", "yout", "noise", "psrr")

#: >= this many UNIQUE temperatures before a quadratic PTAT law is even considered
TEMP_QUAD_MIN_PTS = 5
#: ... and the quadratic must cut the SSE by at least this much
TEMP_QUAD_MIN_GAIN = 0.10
#: ... AND the linear fit must miss by at least this, relative to the mean current: below this
#: the data IS linear and a relative-SSE test would engage curvature on floating-point dust
TEMP_QUAD_RESID_FLOOR = 1e-4

#: a few decades of the admittance curve before a 2nd-order zero is fittable at all
Y_PZ_MIN_PTS = 6
#: adopt the pole-zero ONLY if it cuts the |Y| dB-RMS by at least this much
Y_PZ_KEEP_DB = 0.5
#: wp/wz below this is a degenerate pair -- no real zero
Y_PZ_MIN_SEP = 1.05


# --------------------------------------------------------------------------- the I-V model


def gate(Vo, vknee, knee_p, side, vhi=None):
    """Compliance knee: 1 in saturation, 0 at the compliance limit.

    The knee SIDE is DECOUPLED from the polarity -- a reference can be a current SINK whose
    compliance ceiling sits at the HIGH-Vo rail, not at Vo -> 0.  `knee_p` sets the sharpness
    (about 1 for a simple mirror, much larger for a cascode).
    """
    Vo = np.asarray(Vo, float)
    if side == "none":
        return np.ones_like(Vo)
    if side == "hi":
        arg = (float(vhi) - Vo) / vknee
    else:
        arg = Vo / vknee
    return np.tanh(np.power(np.clip(arg, 0.0, None), knee_p))    # ONE-SIDED, never symmetric


def predict_iv(params: dict, v, *, vc: float, g0: float = 0.0) -> np.ndarray:
    """DC I-V at `T_REF_C`, signed the way the probe reads it.

    `vc` (the pin operating voltage) and `g0` (the conductance MAGNITUDE) are CONTEXT, not
    parameters of this block: `g0` is the same physical number as `yout.g0`, and keeping ONE
    copy of it is what makes the emitted model equal the graded one.  The shape is a magnitude
    -- `pol` carries the sign, exactly as the fit produced it.
    """
    v = np.asarray(v, float)
    sign = 1.0 if str(params.get("pol", "sink")) == "source" else -1.0
    mag = (float(params["idc"]) + abs(float(g0)) * (v - float(vc))) * gate(
        v, float(params["vknee"]), float(params["knee_p"]), str(params["knee_side"]),
        params.get("vhi"))
    return sign * mag


def predict_idc_t(params: dict, T) -> np.ndarray:
    """The continuous temperature law of the bias current, T in degC."""
    return float(params["idc"]) + float(params.get("ptat_slope") or 0.0) * \
        (np.asarray(T, float) - T_REF_C)


def predict_y(params: dict, f) -> np.ndarray:
    """Output admittance Y(s).  The cascode/Wilson 2nd-order zero when one was adopted, else
    the plain `g0 + s*Cp` (byte-identical when `wz`/`wp` are absent or None)."""
    w = TWO_PI * np.asarray(f, float)
    wz, wp = params.get("wz"), params.get("wp")
    g0 = float(params["g0"])
    cp = float(params.get("Cp") or 0.0)
    if wz and wp:
        s = 1j * w
        return g0 * (1.0 + s / float(wz)) / (1.0 + s / float(wp)) + s * cp
    return g0 + 1j * w * cp


def predict_noise(params: dict, f) -> np.ndarray:
    """Output current-noise amplitude In(f) [A/rtHz]."""
    f = np.asarray(f, float)
    return np.sqrt(float(params["white"]) ** 2
                   + float(params.get("flicker") or 0.0) ** 2 / np.maximum(f, 1e-300))


def predict_psrr(params: dict, f) -> np.ndarray:
    """Supply-to-output-current transfer dI/dVsup [S], signed: a pole term plus a feedthrough.

        gdd(s) = gdd / (1 + s/(2 pi psrr_pole_hz))  +  s * c_ft

    The second term is what a falling-only form cannot do: above the pole, a real mirror's
    supply coupling climbs through device overlap capacitance. See `_fit_gdd`.
    """
    f = np.asarray(f, float)
    pole = params.get("psrr_pole_hz")
    if pole:
        out = float(params["gdd"]) / (1.0 + 1j * f / float(pole))
    else:
        out = np.full(f.shape, complex(float(params["gdd"])))
    c_ft = params.get("c_ft")
    if c_ft:
        out = out + 1j * 2.0 * np.pi * f * float(c_ft)
    return out


def predict(params: dict, **kwargs) -> np.ndarray:
    """Dispatch to the right analytic curve for whichever bias block `params` came from."""
    if "gdd" in params:
        return predict_psrr(params, kwargs["f"])
    if "white" in params and "g0" not in params:
        return predict_noise(params, kwargs["f"])
    if "g0" in params and "Cp" in params:
        return predict_y(params, kwargs["f"])
    if "idc" in params:
        if "v" in kwargs:
            return predict_iv(params, kwargs["v"], vc=kwargs["vc"], g0=kwargs.get("g0", 0.0))
        return predict_idc_t(params, kwargs["T"])
    raise KeyError("predict(): these parameters do not belong to any bias block")


# --------------------------------------------------------------------------- knee detection


def _cross_from_top(Vs, Is, level):
    """The Vo on the HIGH-Vo FALLING edge where I crosses `level`.  Walks DOWN from Vo_max and
    interpolates the bracketing pair; when the curve never falls that far inside the sweep it
    returns Vo_max, i.e. a lower bound on the ceiling."""
    for k in range(Vs.size - 1, 0, -1):
        a, b = Is[k - 1], Is[k]
        if (b < level <= a) or (b <= level < a):
            t = (level - a) / (b - a) if b != a else 0.0
            return float(Vs[k - 1] + t * (Vs[k] - Vs[k - 1]))
    return float(Vs[-1])


def _detect_knee(Vs, Is, Iplat):
    """Detect the knee SIDE and parameters from the DATA, never from the polarity.

    Returns `(side, vhi, vknee, knee_p)`.  Robust to the non-monotonic flat-then-collapse curve
    that an interpolation-on-I assumed away -- that assumption was the root cause of a 63 %
    misfit on a real reference.
    """
    a10, a90 = np.arctanh(0.1), np.arctanh(0.9)
    if Iplat <= 0 or Vs.size < 2:
        return "none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0
    # A side is PROPOSED from the endpoint fractions (sensitive to a sharp end-collapse where
    # only the last point hits zero).  Endpoint noise can over-propose on a flat reference --
    # the keep-best-vs-'none' below rejects it on fit quality, so the proposal can stay
    # sensitive here without smoothing away a real sharp collapse.
    flo, fhi = Is[0] / Iplat, Is[-1] / Iplat
    lo_drop, hi_drop = flo < 0.9, fhi < 0.9
    if not lo_drop and not hi_drop:
        return "none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0
    if hi_drop and (not lo_drop or fhi <= flo):
        vhi = _cross_from_top(Vs, Is, 0.02 * Iplat)
        x90 = _cross_from_top(Vs, Is, 0.9 * Iplat)
        x10 = _cross_from_top(Vs, Is, 0.1 * Iplat)
        u90, u10 = vhi - x90, vhi - x10
        if u90 > u10 > 0:
            p = float(np.log(a90 / a10) / np.log(u90 / u10))
            vknee = float(u90 / a90 ** (1.0 / p))
        else:
            p, vknee = 1.0, max(vhi - x90, 0.05)
        return "hi", float(vhi), float(max(vknee, 1e-3)), float(np.clip(p, 0.3, 12.0))
    x10 = float(np.interp(0.1 * Iplat, Is, Vs))
    x90 = float(np.interp(0.9 * Iplat, Is, Vs))
    if x10 > 0 and x90 > x10:
        p = float(np.log(a90 / a10) / np.log(x90 / x10))
        vknee = float(x90 / a90 ** (1.0 / p))
    else:
        p, vknee = 1.0, max(x90, 0.05)
    return "lo", float(Vs[-1]), float(max(vknee, 1e-3)), float(np.clip(p, 0.3, 12.0))


def _fit_iv(Vo, I, vc, g0):
    """ANCHOR the operating point (`idc = |I|(vc)`), take the small-signal conductance from the
    admittance, then DETECT the knee from the data.  Closed form -- no optimizer fragility.

    KEEP-BEST vs NO KNEE: a genuine knee beats 'none' by a wide margin, while a spurious knee
    from endpoint noise, or a high-side collapse that does not COMPLETE inside the sweep, fits
    WORSE than no gate -- which makes the detector self-correcting instead of trusting a
    threshold.
    """
    Vo = np.asarray(Vo, float)
    I = np.asarray(I, float)
    order = np.argsort(Vo)
    Vs, Is = Vo[order], I[order]
    Idc_op = float(np.interp(float(vc), Vs, Is))
    pol = "source" if Idc_op >= 0.0 else "sink"      # DATA-DETECTED, never assumed
    Iabs = np.abs(Is)                                # the shape is fitted on the magnitude
    idc = float(np.interp(float(vc), Vs, Iabs))
    Iplat = float(np.median(np.sort(Iabs)[-8:]))
    side, vhi, vknee, p = _detect_knee(Vs, Iabs, Iplat)

    def _sse(sd, vh, vk, pp):
        m = (idc + g0 * (Vs - vc)) * gate(Vs, vk, pp, sd, vh)
        return float(np.sum((Iabs - m) ** 2))
    cand = [(side, vhi, vknee, p)]
    if side != "none":
        cand.append(("none", float(Vs[-1]), max(float(Vs[-1]), 0.05), 1.0))
    side, vhi, vknee, p = min(cand, key=lambda c: _sse(*c))
    return {"idc": idc, "pol": pol, "knee_side": side, "vhi": float(vhi),
            "vknee": float(vknee), "knee_p": float(p), "iplat": Iplat}


def _fit_temp(temps, idcT, notes):
    """The continuous `idc(T)` law referenced to `T_REF_C`, LINEAR by design.

    The source fitter also carries an opt-in 2nd-order curvature term, gated on >= 5 unique
    temperatures AND a keep-best SSE win AND a physically real linear residual.  This spec has
    no parameter for that curvature (`spec.block("idc","bias")` lists only `ptat_slope`), so
    the gate is still EVALUATED and its verdict reported in `notes` -- the data that would
    justify a spec row, rather than a silently dropped term.
    """
    T = np.asarray(temps, float)
    y = np.asarray(idcT, float)
    Tk = T + 273.15
    b, a = np.polyfit(Tk, y, 1)
    idc_ref = a + b * (T_REF_C + 273.15)
    if np.unique(T).size >= TEMP_QUAD_MIN_PTS:
        x = Tk - (T_REF_C + 273.15)
        c2, c1, c0 = np.polyfit(x, y, 2)
        sse_lin = float(np.sum((y - (a + b * Tk)) ** 2))
        sse_quad = float(np.sum((y - (c0 + c1 * x + c2 * x * x)) ** 2))
        rel = np.sqrt(sse_lin / y.size) / (abs(np.mean(y)) + 1e-30)
        if (np.isfinite(c2) and rel > TEMP_QUAD_RESID_FLOOR
                and sse_quad < (1.0 - TEMP_QUAD_MIN_GAIN) * sse_lin):
            notes.append(f"the PTAT law has real curvature (a quadratic cuts the SSE by "
                         f"{100 * (1 - sse_quad / max(sse_lin, 1e-300)):.0f} %, linear residual "
                         f"{rel * 100:.3f} % of the mean current); this spec carries only the "
                         f"linear ptat_slope, so that curvature is NOT modeled")
    return float(idc_ref), float(b)


# --------------------------------------------------------------------------- admittance


def _fit_admittance(f, Y, g0):
    """`Y(s) = g0*(1+s/wz)/(1+s/wp) + jw*Cp`, fitted to the measured admittance.

    The plain `g0 + s*Cp` form misses the cascode/Wilson SECOND-ORDER zero: real references
    show `Re(Y)` RISING with frequency and an effective output capacitance that DROPS from
    mid-band to high frequency, and one zero/pole pair captures both.  `g0` is ANCHORED (only
    wz, wp, Cp are optimized), `wp` is parameterized as `wz*(1+exp(r))` so `wz < wp` BY
    CONSTRUCTION -- zero before pole, `Re(Y) >= 0`, passive.  KEEP-BEST: the pair is adopted
    only when it beats the baseline by `Y_PZ_KEEP_DB` AND is non-degenerate.
    """
    f = np.asarray(f, float)
    Y = np.atleast_1d(np.asarray(Y, complex))
    ok = np.isfinite(f) & (f > 0) & np.isfinite(Y)
    f, Y = f[ok], Y[ok]
    w = TWO_PI * f
    cp_hf = max(float(Y[-1].imag / w[-1]), 0.0) if f.size and w[-1] > 0 else 0.0
    none = {"wz": None, "wp": None, "Cp": cp_hf}
    if f.size < Y_PZ_MIN_PTS or g0 == 0.0:
        return none
    g0a = abs(g0)
    base_db = db_rms(g0a + 1j * w * cp_hf, Y)

    def _model(x):
        wz = np.exp(x[0])
        wp = wz * (1.0 + np.exp(x[1]))
        cp = np.exp(x[2])
        s = 1j * w
        return g0a * (1.0 + s / wz) / (1.0 + s / wp) + s * cp

    def _resid(x):
        with np.errstate(divide="ignore", invalid="ignore"):
            m = _model(x)
        return 20 * np.log10((np.abs(m) + 1e-30) / (np.abs(Y) + 1e-30))

    cp0 = max(cp_hf, 1e-18)
    best = None
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for fz in (1e3, 1e4, 1e5, 1e6):
            for sep in (3.0, 10.0, 30.0, 100.0):
                try:
                    s = least_squares(_resid, [np.log(TWO_PI * fz), np.log(sep), np.log(cp0)],
                                      method="lm", max_nfev=4000)
                except Exception:                    # noqa: BLE001 -- one start of a grid
                    continue
                db = db_rms(_model(s.x), Y)
                if np.isfinite(db) and (best is None or db < best[0]):
                    best = (db, s.x)
    if best is None:
        return none
    db, x = best
    wz = float(np.exp(x[0]))
    wp = float(wz * (1.0 + np.exp(x[1])))
    cp = float(np.exp(x[2]))
    if not (np.isfinite(wz) and np.isfinite(wp) and np.isfinite(cp)):
        return none
    if (base_db - db) < Y_PZ_KEEP_DB or (wp / wz) < Y_PZ_MIN_SEP:
        return none
    return {"wz": wz, "wp": wp, "Cp": max(cp, 0.0)}


def _fit_noise_law(f, In):
    """`In(f) = sqrt(white^2 + flicker^2/f)` fitted in LOG-AMPLITUDE space.

    A linear least-squares in POWER is dominated by the large low-frequency flicker values and
    barely constrains the small high-frequency floor: it read the white floor 5-30x high and
    scored 9-15 dB off; the log fit recovers the true floor to under 1.5 dB.
    """
    f = np.asarray(f, float)
    In = np.asarray(In, float)
    ok = np.isfinite(f) & np.isfinite(In) & (f > 0) & (In > 0)
    f, In = f[ok], In[ok]
    if f.size < 3:
        iw = float(In.min()) if f.size else 0.0
        kf = float(max((In.max() ** 2 - iw ** 2) * f.min(), 0.0)) if f.size else 0.0
        return {"white": iw, "flicker": float(np.sqrt(kf))}
    order = np.argsort(f)
    f, In = f[order], In[order]
    lz = np.log(In)
    iw0 = max(float(In.min()), 1e-30)
    kf0 = max(float((In[0] ** 2 - iw0 ** 2) * f[0]), iw0 ** 2 * f[0] * 1e-6)

    def _resid(p):
        return np.log(np.sqrt(np.exp(p[0]) ** 2 + np.exp(p[1]) / f)) - lz
    try:
        s = least_squares(_resid, [np.log(iw0), np.log(kf0)], method="lm", max_nfev=10000)
        iw, kf = float(np.exp(s.x[0])), float(np.exp(s.x[1]))
        if not (np.isfinite(iw) and np.isfinite(kf)):
            raise ValueError("non-finite noise fit")
    except Exception:                                # noqa: BLE001 -- fall back to the power LS
        x = 1.0 / f
        coef, *_ = np.linalg.lstsq(np.vstack([np.ones_like(x), x]).T, In ** 2, rcond=None)
        iw, kf = float(np.sqrt(max(coef[0], 0.0))), float(max(coef[1], 0.0))
    return {"white": iw, "flicker": float(np.sqrt(kf))}


# A new degree of freedom is adopted only when it beats the baseline by this much (dB RMS).
# METHODOLOGY's keep-best rule: below the margin the fit falls back byte-identically, so an extra
# knob can never be bought with noise.
KEEP_BEST_DB = 1.0


def _fit_gdd(f, g):
    """Signed low-frequency `gdd`, an optional in-band pole, and an optional feedthrough cap.

    The magnitude is NEVER collapsed: the sign and the phase matter when several references
    share one bias and their ripple currents superpose.

    `c_ft` is a KEEP-BEST degree of freedom (METHODOLOGY: adopt a new DOF only when it beats the
    baseline by a margin, else fall back byte-identically). It exists because a real mirror's
    supply coupling does not only roll off -- above the pole it RISES as j*w*c_ft through device
    overlap capacitance. Measured on the synthetic PMU's PTAT reference: flat at 357 nS to
    ~10 kHz, then 500x up to 173 uS at 1 GHz, i.e. 28 fF. Fitting a falling form to that costs
    ~22 dB, and it is the band that makes VCO spurs.
    """
    f = np.asarray(f, float)
    g = np.asarray(g)
    gdd = float(np.real(g[0]))
    mag = np.abs(g)
    pole = None
    if mag[0] > 0 and mag[-1] < mag[0] / np.sqrt(2):
        pole = float(np.interp(mag[0] / np.sqrt(2), mag[::-1], f[::-1]))
    base = {"gdd": gdd, "psrr_pole_hz": pole, "c_ft": None}
    if len(f) < 4 or mag[0] <= 0:
        return base

    # The feedthrough is read where it dominates: the top of the band, after removing the
    # low-frequency term. A least-squares slope on Im(g - g_lf) vs w is robust to the few points
    # where the two terms are comparable.
    top = f >= f[-1] / 10.0
    if top.sum() < 2:
        return base
    w = 2.0 * np.pi * f[top]
    resid = g[top] - _gdd_lf(base, f[top])
    c_ft = float(np.dot(w, np.imag(resid)) / max(np.dot(w, w), 1e-300))
    if not np.isfinite(c_ft) or c_ft <= 0:
        return base
    cand = dict(base, c_ft=c_ft)
    if _gdd_resid(cand, f, g) < _gdd_resid(base, f, g) - KEEP_BEST_DB:
        return cand
    return base


def _gdd_lf(params: dict, f) -> np.ndarray:
    """The pole-only part of the transfer -- what the block was before `c_ft` existed."""
    f = np.asarray(f, float)
    pole = params.get("psrr_pole_hz")
    if pole:
        return float(params["gdd"]) / (1.0 + 1j * f / float(pole))
    return np.full(f.shape, complex(float(params["gdd"])))


def _gdd_resid(params: dict, f, g) -> float:
    return db_rms(predict_psrr(params, f), g)


# --------------------------------------------------------------------------- dataset access


def _ref_temp(dataset) -> float:
    try:
        temps = [float(t) for t in dataset.axis("temp_c")]
    except Exception:                                # noqa: BLE001
        return T_REF_C
    return min(temps, key=lambda t: abs(t - T_REF_C)) if temps else T_REF_C


def _op_voltage(derived, port: str, v_sweep) -> tuple:
    if derived is not None:
        entry = (getattr(derived, "biases", {}) or {}).get(port) or {}
        vc = entry.get("vcomp_v")
        if isinstance(vc, (int, float)) and np.isfinite(float(vc)):
            return float(vc), f"derived.biases[{port!r}].vcomp_v"
    v = np.asarray(v_sweep, float)
    return float(np.median(v)), "the median of the swept pin voltage (no vcomp_v declared)"


def _g0_from_yout(dataset, port: str, cell: dict):
    """The DC conductance MAGNITUDE, taken from the real part of the AC admittance at its
    lowest frequency -- the same number the `yout` block reports, so the I-V law and the
    admittance can never disagree.  Returns (g0, source) or (None, reason)."""
    var = f"ac_yout.{port}"
    if var_layout(dataset, var) is None:
        return None, f"{var} was never declared"
    try:
        f, Y = read_curve(dataset, var, cell)
    except NoData as exc:
        return None, exc.reason
    g = abs(float(np.real(Y[0])))
    if not np.isfinite(g) or g == 0.0:
        return None, f"{var}: the low-frequency real part is {Y[0]!r}"
    return g, f"{var} at {f[0]:g} Hz"


# --------------------------------------------------------------------------- the blocks


def fit_idc(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the bias DC block: the I-V law, the detected direction and knee, the PTAT slope."""
    bcell = block_cell("idc", "bias", cell)
    var = f"dc_iv.{port}"
    tref = _ref_temp(dataset)
    full = dict(cell)
    full.setdefault("temp_c", tref)
    notes: list = []
    try:
        v, I = read_curve(dataset, var, dict(full, temp_c=tref))
    except NoData as exc:
        return missing_fit(port, "idc", bcell, exc.reason, metric="I-V % of plateau RMS")
    if v.size < 2 or np.ptp(v) <= 0:
        return missing_fit(port, "idc", bcell, f"{var}: the pin-voltage sweep is degenerate",
                           metric="I-V % of plateau RMS")

    vc, vc_src = _op_voltage(derived, port, v)
    g0, g0_src = _g0_from_yout(dataset, port, dict(full))
    if g0 is None:
        # No admittance: use a KNEE-AGNOSTIC chord over the conducting saturation region only.
        # The full-sweep chord crosses the turn-off knee and is about 225x too steep.
        Iabs = np.abs(np.asarray(I, float))
        iplat = float(np.median(np.sort(Iabs)[-8:]))
        sat = Iabs >= 0.5 * iplat
        if int(sat.sum()) >= 2 and np.ptp(v[sat]) > 0:
            g0 = abs(float(np.polyfit(v[sat], Iabs[sat], 1)[0]))
        else:
            g0 = 0.0
        notes.append(f"no AC admittance here ({g0_src}); g0 came from a saturation-region "
                     f"chord, never the full-sweep chord")
    else:
        notes.append(f"g0 anchored to {g0_src} -- the same conductance the yout block reports")
    notes.append(f"operating voltage vc = {vc:.4f} V from {vc_src}")

    iv = _fit_iv(v, I, vc, g0)
    notes.append(f"direction DETECTED from the probe-current sign at the operating point: "
                 f"pol = {iv['pol']}")
    notes.append(f"compliance knee: side = {iv['knee_side']} (one-sided gate; a symmetric gate "
                 f"would let the reference reopen above its ceiling)")

    # PTAT law: the continuous dc_temp sweep if it exists, else the I-V cells across the
    # discrete temperature axis.
    slope = 0.0
    idc_ref = iv["idc"]
    n_t = 1
    tvar = f"dc_temp.{port}"
    got_temp = False
    if var_layout(dataset, tvar) is not None:
        cells, coord = var_layout(dataset, tvar)
        sub = {k: val for k, val in full.items() if k in cells}
        try:
            T, Icurve = read_curve(dataset, tvar, sub) if coord else \
                read_over_axis(dataset, tvar, sub, "temp_c", port)
            Iarr = np.abs(np.asarray(Icurve, float).ravel())
            if Iarr.size >= 2:
                idc_ref, slope = _fit_temp(T, Iarr, notes)
                n_t = int(np.asarray(T).size)
                got_temp = True
                notes.append(f"ptat_slope from the continuous dc_temp sweep ({n_t} points)")
        except NoData:
            pass
    if not got_temp:
        xs, ys = [], []
        try:
            temps = [float(t) for t in dataset.axis("temp_c")]
        except Exception:                            # noqa: BLE001
            temps = []
        for t in temps:
            try:
                vt, It = read_curve(dataset, var, dict(full, temp_c=t))
            except NoData:
                continue
            xs.append(t)
            ys.append(abs(float(np.interp(vc, vt, It))))
        if len(xs) >= 2:
            idc_ref, slope = _fit_temp(xs, ys, notes)
            n_t = len(xs)
            notes.append(f"ptat_slope from the I-V cells across {n_t} discrete temperatures")
        else:
            notes.append("only one characterized temperature: ptat_slope = 0 and idc is the "
                         "value at that temperature, not at %.4g C" % T_REF_C)

    params = {"idc": float(idc_ref), "pol": iv["pol"], "knee_side": iv["knee_side"],
              "vhi": float(iv["vhi"]), "vknee": float(iv["vknee"]),
              "knee_p": float(iv["knee_p"]), "ptat_slope": float(slope)}
    model = predict_iv(dict(params, idc=iv["idc"]), v, vc=vc, g0=g0)
    score = pct_rms(model, np.asarray(I, float), scale=iv["iplat"])

    # With no knee detected (`knee_side == "none"`) the gate is identically 1: vknee / knee_p /
    # vhi are placeholders that neither `predict_iv` nor the emitter reads (va.py writes them
    # only for a real knee).  Gating them flagged three numbers that are not in the model and
    # held every knee-less reference at yellow; only a knee that exists is asked about.
    names = ["idc"] + ([] if iv["knee_side"] == "none" else ["vknee", "knee_p", "vhi"])

    def g(p):
        q = dict(params, idc=p[0], **dict(zip(names[1:], p[1:])))
        return predict_iv(q, v, vc=vc, g0=g0) + 0j
    gate_res = ident.gate(g, names, [iv[n] for n in names])
    gate_res["sigma"]["ptat_slope"] = 0.0 if n_t > 1 else float("inf")
    if n_t <= 1:
        gate_res["unidentifiable"] = list(gate_res["unidentifiable"]) + ["ptat_slope"]
    notes += ident.describe(gate_res)
    return BlockFit(port=port, block="idc", cell=bcell, params=params, score=float(score),
                    metric="I-V % of plateau RMS", n_points=int(v.size),
                    identifiability=gate_res, notes=notes)


def fit_yout(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the bias output admittance."""
    bcell = block_cell("yout", "bias", cell)
    var = f"ac_yout.{port}"
    try:
        f, Y = read_curve(dataset, var, dict(cell))
    except NoData as exc:
        return missing_fit(port, "yout", bcell, exc.reason, metric="|Y| dB RMS")
    notes: list = []
    g0 = abs(float(np.real(Y[0])))
    notes.append(f"g0 = {g0:.4g} S from the real part of the admittance at {f[0]:g} Hz, not "
                 f"from the full-sweep I-V chord (which crosses the turn-off knee)")
    af = _fit_admittance(f, Y, g0)
    params = {"g0": float(g0), "Cp": float(af["Cp"]),
              "wz": (None if af["wz"] is None else float(af["wz"])),
              "wp": (None if af["wp"] is None else float(af["wp"]))}
    if af["wz"]:
        notes.append(f"cascode/Wilson zero adopted: wz = {af['wz'] / TWO_PI:.4g} Hz, "
                     f"wp = {af['wp'] / TWO_PI:.4g} Hz (keep-best, non-degenerate)")
    else:
        notes.append("no second-order zero adopted: the plain g0 + s*Cp form was not beaten "
                     "by the keep-best margin")
    score = db_rms(predict_y(params, f), Y)
    names = ["g0", "Cp"] + ([] if af["wz"] is None else ["wz", "wp"])
    vals = [params["g0"], params["Cp"]] + ([] if af["wz"] is None
                                           else [params["wz"], params["wp"]])

    def g(p, f=f):
        q = dict(params)
        for n, v in zip(names, p):
            q[n] = v
        return predict_y(q, f)
    fe = ident.envelope_grid(f, ident.envelope_band(derived, "freq"))
    gate_res = ident.gate(g, names, vals, envelope=lambda p: g(p, fe))
    notes += ident.describe(gate_res)
    return BlockFit(port=port, block="yout", cell=bcell, params=params, score=float(score),
                    metric="|Y| dB RMS", n_points=int(f.size), identifiability=gate_res,
                    notes=notes)


def fit_noise(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the bias output current noise (white + 1/f), in the log-amplitude domain."""
    bcell = block_cell("noise", "bias", cell)
    var = f"noise_i.{port}"
    try:
        f, In = read_curve(dataset, var, dict(cell), psd=True)
    except NoData as exc:
        return missing_fit(port, "noise", bcell, exc.reason, metric="In dB RMS")
    params = _fit_noise_law(f, In)
    params = {"white": float(params["white"]), "flicker": float(params["flicker"])}
    score = db_rms(predict_noise(params, f), In)
    notes = ["fitted in the LOG AMPLITUDE domain: a power least-squares is dominated by the "
             "flicker tail and reads the white floor 5-30x high",
             "bandgap noise reaches the rails and the biases together, but this spec models "
             "them as independent sources -- a known ~3 dB error on phase noise, not an "
             "oversight"]

    def g(p, f=f):
        return predict_noise({"white": p[0], "flicker": p[1]}, f) + 0j
    fe = ident.envelope_grid(f, ident.envelope_band(derived, "noise"))
    gate_res = ident.gate(g, ["white", "flicker"], [params["white"], params["flicker"]],
                          envelope=lambda p: g(p, fe), off=["white", "flicker"])
    notes += ident.describe(gate_res)
    return BlockFit(port=port, block="noise", cell=bcell, params=params, score=float(score),
                    metric="In dB RMS", n_points=int(f.size), identifiability=gate_res,
                    notes=notes)


def fit_psrr(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the supply-to-bias-current transfer.  Same supply injection as the rail PSRR block:
    one AC run injects at the supply and reads every rail and every bias pin."""
    bcell = block_cell("psrr", "bias", cell)
    var = f"ac_psrr.{port}"
    try:
        f, g = read_curve(dataset, var, dict(cell))
    except NoData as exc:
        return missing_fit(port, "psrr", bcell, exc.reason, metric="|gdd| dB RMS")
    params = _fit_gdd(f, g)
    params = {"gdd": float(params["gdd"]),
              "psrr_pole_hz": (None if params["psrr_pole_hz"] is None
                               else float(params["psrr_pole_hz"])),
              "c_ft": (None if params.get("c_ft") is None else float(params["c_ft"]))}
    score = db_rms(predict_psrr(params, f), g)
    notes = ["the sign is KEPT: collapsing this to a magnitude loses the phase that matters "
             "when several references share one bias and their ripple currents superpose"]
    if params["psrr_pole_hz"]:
        notes.append(f"the transfer rolls in band: one pole at "
                     f"{params['psrr_pole_hz']:.4g} Hz")
    if params["c_ft"]:
        notes.append(f"supply feedthrough capacitance kept: {params['c_ft'] * 1e15:.3g} fF -- the "
                     "transfer RISES above the pole, which a falling-only form cannot follow")
    names = ["gdd"] + ([] if params["psrr_pole_hz"] is None else ["psrr_pole_hz"])         + ([] if not params["c_ft"] else ["c_ft"])
    vals = [params[n] for n in names]

    def gfun(p, f=f):
        q = dict(params)
        for n, v in zip(names, p):
            q[n] = v
        return predict_psrr(q, f)
    fe = ident.envelope_grid(f, ident.envelope_band(derived, "freq"))
    gate_res = ident.gate(gfun, names, vals, envelope=lambda p: gfun(p, fe))
    notes += ident.describe(gate_res)
    return BlockFit(port=port, block="psrr", cell=bcell, params=params, score=float(score),
                    metric="|gdd| dB RMS", n_points=int(f.size), identifiability=gate_res,
                    notes=notes)


_FITTERS = {"idc": fit_idc, "yout": fit_yout, "noise": fit_noise, "psrr": fit_psrr}


def fit(dataset, port: str, cell: dict, derived=None, *, block: str = "idc") -> BlockFit:
    """Uniform entry point: `block` selects which of the four bias blocks to fit."""
    if block not in _FITTERS:
        raise KeyError(f"bias.fit(): unknown block {block!r}; "
                       f"expected one of {', '.join(BLOCKS)}")
    return _FITTERS[block](dataset, port, cell, derived)