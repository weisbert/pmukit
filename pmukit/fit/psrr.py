# from LDO_modeling/harness/fit_model.py @ d2c5b80
"""Rail PSRR, identified as a supply COUPLING CURRENT that the same Zout carries to the pin.

    PSRR(s) = i_c(s) * Zout(s)
    i_c(s)  = G0 + sum_i G_i/(1 + s/w_i)
              + (pc_gain + pc_zero*s)/(1 + s/(pc_q*pc_w0) + (s/pc_w0)^2)
              + s*c_ft

The block identifies ONLY `i_c`, which is why a Zout error is common-mode across Zout, PSRR and
noise -- the same physical impedance appears in all three.  The earlier claim that "PSRR does not
factor as i_c*Zout" was REFUTED: it was a fit-method failure (an insufficient-order shelf; a naive
fit will not converge over 60 dB), not a structural one.

Method, in the order the selector tries it:
  * a minimum-phase SHELF (one signed real section).  When it is clean it is returned as-is and
    the complex section stays INERT -- minimum-phase rails must not regress.
  * a Sanathanan-Koerner rational fit of `i_c = H/Zout` (frequency-scaled, relative-weighted),
    realized as a bank of signed first-order REAL-POLE sections.
  * ONE signed complex-conjugate 2nd-order section, AAA-INITIALIZED and `least_squares`-polished
    on the realizable form.  N2 = 1 is the sweet spot; N2 >= 2 overfits.

Two rejections are load-bearing here:
  * raw AAA poles dumped into the model -- REJECTED (3-6 spurious pairs, Q ~ 1700).  AAA is an
    INITIALIZER only.
  * ranking a real notch fit by its ANALYTIC residual -- REJECTED: a pure-real fit of a notch can
    show the lower analytic residual and still realize with a huge phase error, because it leans
    on a fragile pole-zero cancellation.  Always realize and score; and when the complex section
    is adequate, PREFER it.

EMIT-FACING DETECTOR (a hard-won rule, see `notes`): a rational fit that emits a large
near-cancelling first-order residue pair breaks a COUPLED harmonic-balance run even though the AC
is perfect -- the pair is a near-null-space direction in the shared supply-node Jacobian, and the
singular columns surface at the package or at real transistors sharing the node, never at the
model.  Any `|G_i|` around 1 or above, and any near-cancelling pair, is flagged so the emitter
realizes it as ONE small-coefficient gm-C biquad instead.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from . import identifiability as ident
from . import zout as zout_block
from ._base import (TWO_PI, BlockFit, NoData, block_cell, db_rms, missing_fit, read_curve)

__all__ = ["fit", "predict", "ic_model", "psrr_model", "fit_psrr", "PARAMS"]

PARAMS = ("G0", "G_i", "pole_i_hz", "pc_gain", "pc_zero", "pc_w0", "pc_q", "c_ft")

#: real first-order sections in the coupling-current bank
NPS = 3
#: signed complex-conjugate 2nd-order sections.  One carries the non-minimum-phase / notch
#: phase that strictly-real sections cannot; two overfits and destabilizes.
NPC = 1
#: deg: try the complex bank if the shelf's PSRR phase RMS exceeds this even when its
#: magnitude residual is already small (keep-best => this can only improve the result)
SHELF_PH_TRIG = 2.5
#: the score band: below this the supply transfer is dominated by the DC operating point
SCORE_F_MIN = 1e3
#: |G_i| at or above this is an emit-time HB hazard (see the module docstring)
DOUBLET_G = 1.0
#: a signed pair cancelling to within this fraction, with poles this close, is a doublet
DOUBLET_CANCEL = 0.05
DOUBLET_POLE = 0.05


# --------------------------------------------------------------------------- the model


def ic_model(f, G, Q=None, c_ft=0.0):
    """The coupling current `i_c(s)`.  `G = [G0, G1, w1, G2, w2, ...]` (w in rad/s),
    `Q = (b0, b1, w0, Qf)` or None."""
    s = 1j * TWO_PI * np.asarray(f, float)
    n = (len(G) - 1) // 2
    i_c = G[0] + sum(G[1 + 2 * i] / (1 + s / G[2 + 2 * i]) for i in range(n))
    if Q is not None and (Q[0] != 0.0 or Q[1] != 0.0):
        b0, b1, w0, Qf = Q
        i_c = i_c + (b0 + b1 * s) / (1 + s / (Qf * w0) + (s / w0) ** 2)
    if c_ft > 0.0:
        # the feedthrough cap injects i = s*C_ft*(vin - vout); to first order (|H| << 1) that
        # is one more s*C_ft term in the coupling current
        i_c = i_c + s * c_ft
    return i_c


def psrr_model(f, zparams, G, Q=None, c_ft=0.0):
    """PSRR = i_c(s) * Zout(s) -- the Zout factor is the rail's OWN fitted ladder, so the two
    blocks auto-reconcile by construction."""
    Z = zout_block.predict(zparams, f=f, c_ft=c_ft)
    return ic_model(f, G, Q, c_ft) * Z


def _unpack(params: dict):
    G = [float(params["G0"])]
    for g, p in zip(params.get("G_i") or [], params.get("pole_i_hz") or []):
        G += [float(g), TWO_PI * float(p)]
    Q = (float(params.get("pc_gain", 0.0)), float(params.get("pc_zero", 0.0)),
         float(params.get("pc_w0", 1.0)), float(params.get("pc_q", 1.0)))
    return G, Q, float(params.get("c_ft", 0.0))


def predict(params: dict, *, f, zout, **_ignored) -> np.ndarray:
    """Analytic PSRR(f).  `zout` is the fitted Zout parameter dict of the SAME cell: this block
    only identifies the coupling current, so the impedance has to come from there."""
    G, Q, c_ft = _unpack(params)
    return psrr_model(np.asarray(f, float), zout, G, Q, c_ft)


# --------------------------------------------------------------------------- the fitters


def _sk_fit(s, H, n, n_iter=14):
    """Sanathanan-Koerner rational fit `H ~ N(sn)/D(sn)` (real coefficients, order n,
    frequency-scaled, relative-weighted).  Returns `(poles, residues, d)` in unscaled s via
    partial fractions, or None if any pole is complex (the caller falls back)."""
    w0 = 1.0 / (np.abs(H) + 1e-15)
    sc = float(np.exp(np.mean(np.log(np.abs(s) + 1e-30))))
    sn = s / sc
    D = np.ones(len(s), complex)
    nc = dc = None
    for _ in range(n_iter):
        w = w0 / np.abs(D)
        cols = [sn ** i for i in range(n + 1)] + [-H * sn ** j for j in range(1, n + 1)]
        A = (np.vstack(cols).T) * w[:, None]
        b = H * w
        x, *_ = np.linalg.lstsq(np.vstack([A.real, A.imag]),
                                np.concatenate([b.real, b.imag]), rcond=None)
        nc = x[:n + 1]
        dc = np.concatenate([[1.0], x[n + 1:]])
        D = sum(dc[j] * sn ** j for j in range(n + 1))
    psn = np.roots(dc[::-1])
    if np.any(np.abs(psn.imag) > 1e-6 * np.abs(psn.real) + 1e-12):
        return None                                   # complex poles -> need the RLC section
    psn = psn.real
    Dp = np.polyder(dc[::-1])
    res = np.array([np.polyval(nc[::-1], p) / np.polyval(Dp, p) for p in psn])
    d = nc[n] / dc[n]
    return psn * sc, res * sc, d


def _aaa_conj(f, y, max_terms=12):
    """AAA rational interpolant on the imaginary axis WITH conjugate samples, so the
    interpolant has real coefficients (conjugate-symmetric poles).  Used ONLY to initialize
    the bounded section fit -- raw AAA over-fits and must never reach the model."""
    from scipy.interpolate import AAA
    s = 1j * TWO_PI * np.asarray(f, float)
    r = AAA(np.concatenate([s, np.conj(s)]), np.concatenate([y, np.conj(y)]),
            max_terms=max_terms)
    poles = r.poles()
    try:
        res = r.residues()
    except Exception:                                 # noqa: BLE001 -- initializer only
        res = np.full(len(poles), np.nan, complex)
    return poles, res


def _pair_section(p, r):
    """Conjugate pole pair (p, residue r) -> realizable 2nd-order section `(b0, b1, w0, Qf)`
    for `(b0 + b1 s)/(1 + s/(Qf w0) + (s/w0)^2)`."""
    B1 = 2 * r.real
    B0 = -2 * (r * np.conj(p)).real
    A1 = -2 * p.real
    A0 = abs(p) ** 2
    w0 = np.sqrt(A0)
    Qf = w0 / A1 if A1 > 1e-30 else 40.0
    return B0 / A0, B1 / A0, w0, Qf


def _bank_fit(f, H, zparams, n1=NPS, c_ft=0.0):
    """Fit `i_c = H/Zout` as the (n1 real + 1 complex) bank: AAA-initialize the dominant
    complex pair and the real poles, then polish with `least_squares` on the EXACT realizable
    form (the residual is the complex log of model/measurement, i.e. magnitude-dB and phase
    jointly, matching the score).  Returns `(G, Q)` or None."""
    try:
        Zmod = zout_block.predict(zparams, f=f, c_ft=c_ft)
        ic = H / Zmod
        if c_ft > 0.0:
            ic = ic - 1j * TWO_PI * f * c_ft          # the model adds the tail itself
        w0lo, w0hi = TWO_PI * f[0] / 10.0, TWO_PI * f[-1] * 10.0
        pol, res = _aaa_conj(f, ic)
        band = (np.abs(pol) > w0lo * 3) & (np.abs(pol) < w0hi / 3) & (pol.real < 0)
        pol, res = pol[band], res[band]
        reals, pairs = [], []
        used = np.zeros(len(pol), bool)
        for i, p in enumerate(pol):
            if used[i]:
                continue
            if abs(p.imag) <= 1e-3 * abs(p.real) + 1.0:
                reals.append((p.real, res[i]))
                used[i] = True
            else:
                j = next((k for k in range(len(pol)) if not used[k] and k != i
                          and abs(pol[k] - np.conj(p)) < 1e-3 * abs(p) + 1.0), None)
                pairs.append((p, res[i]))
                used[i] = True
                if j is not None:
                    used[j] = True
        reals.sort(key=lambda pr: -abs(pr[1] / pr[0]))
        secs = sorted((_pair_section(p, r) for p, r in pairs), key=lambda d: -abs(d[0]))
        p0 = [float(ic[-1].real)]
        for i in range(n1):
            if i < len(reals):
                pr, rr = reals[i]
                p0 += [float((-rr / pr).real), float(np.log(min(max(-pr, w0lo), w0hi)))]
            else:
                p0 += [0.0, float(np.log(TWO_PI * f[len(f) // 2]))]
        if secs:
            b0, b1, w0, Qf = secs[0]
            p0 += [float(b0), float(b1), float(np.log(min(max(w0, w0lo), w0hi))),
                   float(np.log(min(max(Qf, 0.5), 40.0)))]
        else:
            p0 += [0.0, 0.0, float(np.log(TWO_PI * f[len(f) // 2])), float(np.log(2.0))]
        p0 = np.array(p0)
        lo = np.full_like(p0, -np.inf)
        hi = np.full_like(p0, np.inf)
        k = 1
        for _ in range(n1):
            lo[k + 1], hi[k + 1] = np.log(w0lo), np.log(w0hi)
            k += 2
        lo[k + 2], hi[k + 2] = np.log(w0lo), np.log(w0hi)
        lo[k + 3], hi[k + 3] = np.log(0.5), np.log(40.0)

        def unpack(p):
            G = [p[0]]
            for i in range(n1):
                G += [p[1 + 2 * i], np.exp(p[2 + 2 * i])]
            Q = (p[1 + 2 * n1], p[2 + 2 * n1], np.exp(p[3 + 2 * n1]), np.exp(p[4 + 2 * n1]))
            return G, Q

        def resid(p):
            G, Q = unpack(p)
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                r = np.log(psrr_model(f, zparams, G, Q, c_ft)) - np.log(H)
            return np.nan_to_num(np.concatenate([r.real, r.imag]),
                                 nan=1e6, posinf=1e6, neginf=-1e6)
        s = least_squares(resid, p0, bounds=(lo, hi), method="trf", max_nfev=20000)
        G, Q = unpack(s.x)
        if not np.all(np.isfinite(np.concatenate([G, Q]))):
            return None
        return G, Q
    except Exception:                                 # noqa: BLE001 -- one of several candidates
        return None


def _psrr_resid(f, H, zparams, G, Q=None, c_ft=0.0, sel_hz=SCORE_F_MIN):
    """Combined magnitude(dB) + phase(rad) RMS over the score band -- the ONE consistent metric
    the keep-best selector uses, so it can never regress."""
    m = psrr_model(f, zparams, G, Q, c_ft)
    sel = f >= sel_hz
    if not sel.any():
        sel = np.ones_like(f, bool)
    r = np.log(m[sel] / H[sel])
    return float(np.sqrt(np.mean(r.real ** 2 + r.imag ** 2)))


def _shelf(f, H, zparams, c_ft=0.0):
    """Minimum-phase one-section fit -> `G = [g_hf, g_lf - g_hf, wz, 0, big, 0, big]`.

    Floating-point warnings are silenced on purpose: a trial point that underflows or produces a
    zero-crossing model yields a non-finite residual, which simply makes that CANDIDATE lose the
    keep-best comparison.  Letting numpy shout about it would turn an ordinary step of the search
    into noise on the console.
    """
    def resid(p):
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            G = [1 / np.exp(p[0]), np.exp(p[1]) - 1 / np.exp(p[0]), np.exp(p[2]),
                 0, 1e9, 0, 1e9]
            r = np.log(psrr_model(f, zparams, G, None, c_ft)) - np.log(H)
        return np.nan_to_num(np.concatenate([r.real, r.imag]),
                             nan=1e6, posinf=1e6, neginf=-1e6)
    # the seed reference frequency: the legacy 8 MHz when the sweep covers it, otherwise the
    # nearest measured edge -- evaluating the model off-band would seed from an extrapolation
    fref = float(np.clip(8e6, f[0], f[-1]))
    Zr = np.abs(zout_block.predict(zparams, f=np.array([fref]), c_ft=c_ft)[0])
    Rpass0 = max(Zr / max(np.abs(H[int(np.argmin(np.abs(f - fref)))]), 1e-300), 1e-300)
    g_lf0 = max(np.abs(H[0]) / max(np.abs(zout_block.predict(
        zparams, f=np.array([f[0]]), c_ft=c_ft)[0]), 1e-300), 1e-300)
    fpk = f[int(np.argmax(np.abs(zout_block.predict(zparams, f=f, c_ft=c_ft))))]
    s1 = least_squares(resid, [np.log(Rpass0), np.log(g_lf0), np.log(TWO_PI * fpk)],
                       method="lm")
    Rpass, g_lf, wz = np.exp(s1.x)
    G = [1 / Rpass, g_lf - 1 / Rpass, wz, 0.0, 1e9, 0.0, 1e9]
    return G, float(np.sqrt(np.mean(resid(s1.x) ** 2)))


def fit_psrr(f, H, zparams, c_ft=0.0):
    """The coupling-current bank -> `(G, Q)`.  KEEP-BEST on one combined magnitude+phase
    residual, with the complex section PREFERRED whenever it is adequate."""
    f = np.asarray(f, float)
    H = np.asarray(H)
    Q0 = (0.0, 0.0, TWO_PI * float(np.sqrt(f[0] * f[-1])), 1.0)   # inert default section
    G_shelf, e_shelf = _shelf(f, H, zparams, c_ft)
    sel = f >= SCORE_F_MIN
    if not sel.any():
        sel = np.ones_like(f, bool)
    shelf_ph = np.degrees(np.sqrt(np.mean(
        np.angle(psrr_model(f, zparams, G_shelf, None, c_ft)[sel] / H[sel]) ** 2)))
    if e_shelf < 0.05 and shelf_ph < SHELF_PH_TRIG:
        return G_shelf, Q0, "shelf"                   # minimum-phase -> complex stays inert

    shelf_resid = _psrr_resid(f, H, zparams, G_shelf, Q0, c_ft)
    cands = [("shelf", G_shelf, Q0, shelf_resid)]

    ic_t = H / zout_block.predict(zparams, f=f, c_ft=c_ft)
    if c_ft > 0.0:
        ic_t = ic_t - 1j * TWO_PI * f * c_ft
    sk = _sk_fit(1j * TWO_PI * f, ic_t, NPS)
    if sk is not None:
        poles, res, d = sk
        secs = sorted([(-p, -r / p) for p, r in zip(poles, res)])
        G = [float(d)]
        for w_i, G_i in secs[:NPS]:
            G += [float(G_i), float(max(w_i, 1.0))]
        while len(G) < 1 + 2 * NPS:
            G += [0.0, 1e9]
        cands.append(("sk", G, Q0, _psrr_resid(f, H, zparams, G, Q0, c_ft)))

    bank = _bank_fit(f, H, zparams, c_ft=c_ft)
    if bank is not None:
        Gb, Qb = bank
        cands.append(("complex", Gb, Qb, _psrr_resid(f, H, zparams, Gb, Qb, c_ft)))

    # MULTI-START full-real-bank polish, always a candidate.  A loop-active rail has a PSRR
    # roll that the one-section shelf, the SK bank and the complex bank all miss; polishing all
    # NPS real sections from several decade-spread pole seeds is what catches it.  This can only
    # improve the result: it is one more keep-best candidate, and the complex-bank preference
    # below still wins whenever the complex section is adequate.
    w0lo, w0hi = TWO_PI * f[0] / 10.0, TWO_PI * f[-1] * 10.0

    def unpack3(p):
        return [p[0], p[1], np.exp(p[2]), p[3], np.exp(p[4]), p[5], np.exp(p[6])]

    def resid3(p):
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            r = np.log(psrr_model(f, zparams, unpack3(p), None, c_ft)) - np.log(H)
        return np.nan_to_num(np.concatenate([r.real, r.imag]),
                             nan=1e6, posinf=1e6, neginf=-1e6)
    lo3 = np.array([-np.inf, -np.inf, np.log(w0lo), -np.inf, np.log(w0lo), -np.inf,
                    np.log(w0lo)])
    hi3 = np.array([np.inf, np.inf, np.log(w0hi), np.inf, np.log(w0hi), np.inf, np.log(w0hi)])
    _gm = TWO_PI * float(np.sqrt(f[0] * f[-1]))
    _tri = np.geomspace(TWO_PI * f[0] * 3, TWO_PI * f[-1] / 3, 3)
    for w1, w2, w3 in [(G_shelf[2], TWO_PI * f[0] * 10, _gm),
                       tuple(_tri),
                       (TWO_PI * f[-1] / 3, _gm, TWO_PI * f[0] * 10),
                       (_gm, TWO_PI * f[0] * 10, TWO_PI * f[-1] / 3)]:
        p0 = np.array([G_shelf[0], G_shelf[1], np.log(np.clip(w1, w0lo, w0hi)),
                       0.0, np.log(np.clip(w2, w0lo, w0hi)),
                       0.0, np.log(np.clip(w3, w0lo, w0hi))])
        try:
            s3 = least_squares(resid3, p0, bounds=(lo3, hi3), method="trf", max_nfev=20000)
            G3 = unpack3(s3.x)
            if np.all(np.isfinite(G3)):
                cands.append(("bank3", G3, Q0, _psrr_resid(f, H, zparams, G3, Q0, c_ft)))
        except Exception:                             # noqa: BLE001
            pass

    best = min(cands, key=lambda c: c[3])
    comp = next((c for c in cands if c[0] == "complex"), None)
    if comp is not None and comp[3] <= max(2.0 * best[3], 0.15):
        return comp[1], comp[2], "complex"
    return best[1], best[2], best[0]


# --------------------------------------------------------------------------- HB hazard scan


def doublet_notes(G0: float, gains, poles_hz) -> list:
    """Scan the realized first-order residues for the emit-time harmonic-balance hazard."""
    out = []
    allg = [("G0", float(G0), float("inf"))] + [
        (f"G_i[{i}]", float(g), float(p)) for i, (g, p) in enumerate(zip(gains, poles_hz))]
    big = [(n, g) for n, g, _ in allg if abs(g) >= DOUBLET_G]
    if big:
        out.append("HB hazard: large first-order residue(s) "
                   + ", ".join(f"{n}={g:+.3g} S" for n, g in big)
                   + " -- the emitter must realize this as ONE small-coefficient gm-C biquad, "
                     "never as a synthesized R-L-C or as parallel large-residue sections "
                     "(a near-null-space direction in the shared supply-node Jacobian makes a "
                     "coupled oscillator HB singular even though the AC is perfect)")
    for i in range(len(gains)):
        for j in range(i + 1, len(gains)):
            gi, gj = float(gains[i]), float(gains[j])
            pi, pj = float(poles_hz[i]), float(poles_hz[j])
            top = max(abs(gi), abs(gj))
            if top <= 0 or gi * gj >= 0:
                continue
            if abs(gi + gj) > DOUBLET_CANCEL * top:
                continue
            if max(pi, pj) <= 0 or abs(pi - pj) > DOUBLET_POLE * max(pi, pj):
                continue
            out.append(f"HB hazard: near-cancelling first-order doublet G_i[{i}]={gi:+.4g} / "
                       f"G_i[{j}]={gj:+.4g} with poles {pi:.4g} / {pj:.4g} Hz -- realize the "
                       f"PAIR as one small-coefficient gm-C biquad")
    return out


# --------------------------------------------------------------------------- the block


def fit(dataset, port: str, cell: dict, derived=None, *, zout_params=None) -> BlockFit:
    """Fit the PSRR block of one rail at one cell.

    `zout_params` is the Zout block fitted at the SAME cell.  It is not optional physics: the
    identification de-embeds `i_c = H/Zout`, so passing a different impedance would move the
    error into the coupling current.  When it is omitted the Zout block is fitted here.
    """
    cell = block_cell("psrr", "rail", cell)
    var = f"ac_psrr.{port}"
    try:
        f, H = read_curve(dataset, var, cell)
    except NoData as exc:
        return missing_fit(port, "psrr", cell, exc.reason, metric="PSRR dB RMS")
    if f.size < 4:
        return missing_fit(port, "psrr", cell,
                           f"{var}: only {f.size} usable frequency point(s)",
                           metric="PSRR dB RMS")
    notes: list = []
    if zout_params is None:
        zf = zout_block.fit(dataset, port, cell, derived)
        if zf.missing:
            return missing_fit(port, "psrr", cell,
                               "PSRR is identified as i_c = H/Zout, and Zout is " + zf.notes[0],
                               metric="PSRR dB RMS")
        zout_params = zf.params
        notes.append("Zout was not supplied; it was fitted here from the same cell")

    c_ft = 0.0
    try:
        fz, Z = read_curve(dataset, f"ac_zout.{port}", cell)
        c_ft = zout_block.detect_cft(f, H, fz, Z)
    except NoData:
        c_ft = 0.0
    if c_ft > 0.0:
        notes.append(f"feedthrough capacitance gated ON: c_ft = {c_ft * 1e15:.1f} fF")

    G, Q, which = fit_psrr(f, H, zout_params, c_ft)
    gains = [G[1 + 2 * i] for i in range(NPS)]
    poles_hz = [G[2 + 2 * i] / TWO_PI for i in range(NPS)]
    params = {"G0": float(G[0]),
              "G_i": [float(g) for g in gains],
              "pole_i_hz": [float(p) for p in poles_hz],
              "pc_gain": float(Q[0]), "pc_zero": float(Q[1]),
              "pc_w0": float(Q[2]), "pc_q": float(Q[3]),
              "c_ft": float(c_ft)}
    notes.append(f"selector kept the {which} candidate"
                 + (" (the complex section is inert)" if Q[0] == 0.0 and Q[1] == 0.0 else
                    f" with one complex section at {Q[2] / TWO_PI / 1e6:.3f} MHz, Q={Q[3]:.2f}"))
    notes += doublet_notes(G[0], gains, poles_hz)

    Hm = psrr_model(f, zout_params, G, Q, c_ft)
    score = db_rms(Hm, H)
    sel = f >= SCORE_F_MIN
    if sel.any():
        ph = float(np.degrees(np.sqrt(np.mean(np.angle(Hm[sel] / H[sel]) ** 2))))
        notes.append(f"phase residual {ph:.1f} deg over the score band")

    flat_names = ["G0"] + [f"G_i[{i}]" for i in range(NPS)] + \
                 [f"pole_i_hz[{i}]" for i in range(NPS)] + \
                 ["pc_gain", "pc_zero", "pc_w0", "pc_q"]
    flat_vals = [G[0]] + gains + poles_hz + [Q[0], Q[1], Q[2], Q[3]]

    def g(p):
        Gv = [p[0]]
        for i in range(NPS):
            Gv += [p[1 + i], TWO_PI * p[1 + NPS + i]]
        Qv = (p[1 + 2 * NPS], p[2 + 2 * NPS], p[3 + 2 * NPS], p[4 + 2 * NPS])
        return psrr_model(f, zout_params, Gv, Qv, c_ft)
    gate = ident.gate(g, flat_names, flat_vals)
    notes += ident.describe(gate)
    return BlockFit(port=port, block="psrr", cell=cell, params=params, score=float(score),
                    metric="PSRR dB RMS", n_points=int(f.size), identifiability=gate,
                    notes=notes)
