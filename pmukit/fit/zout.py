# from LDO_modeling/harness/fit_model.py @ d2c5b80
"""Rail output impedance: a PASSIVE-BY-CONSTRUCTION RLC ladder.

    Zout(s) = [ Ra + (sLa || Rpl) + sum_i (sLa_i || Rpl_i) ]  ||  (Rb + sLb)  ||  (esr + 1/sCout)

* `Ra` is a stiff resistor, never a current clamp: `1/Ra` holds the DC pin everywhere.  A
  saturating "DC current compliance" was REJECTED -- its knee is a VOLTAGE `Icomp*Ra`, a few mV
  with the fitted `Ra`, so a few mV off regulation the branch becomes a zero-conductance current
  source and the DC pin is lost (FF-corner runaway).
* `Rpl` is the damping resistor across `La`: `Rpl -> inf` is the resonant peak, finite `Rpl` is
  a resistive plateau.  ONE degree of freedom covers both.
* `(La_i, Rpl_i)` is the optional higher-order ladder: a single section cannot reproduce a
  multi-decade inductive rise whose effective L varies with frequency, and fitting one anyway
  mislocates the rise corner.  Adopted only when it cuts the |Z| dB-RMS by the keep-best margin.
* `(Rb, Lb)` is the optional second parallel R-L branch, engaged only when it beats the
  single-branch fit by more than 40 %.
* `Cout`/`esr` are read off the CAPACITIVE band (phase of Z below -45 deg, `C = -1/(w*Im Z)`).
  A joint least-squares on Cout/ESR is REJECTED: on a high-ESR or capless rail it is
  underdetermined and diverges (it once sent an invisible cap to 1e269 F).

Passive by construction is the single biggest HB-convergence lever, and it carries a documented
FLOOR: a genuinely NON-PASSIVE ground truth (`Re Z < 0`, an actively regulated rail) cannot be
reproduced by a passive RLC.  That residual is reported in `notes`, not treated as a bug.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from . import identifiability as ident
from ._base import (TWO_PI, BlockFit, NoData, block_cell, db_rms, have, missing_fit,
                    read_curve)

__all__ = ["fit", "predict", "zmodel", "extract_cout_esr", "fit_zout", "fit_zout_ladder",
           "detect_cft", "is_shelf", "PARAMS"]

PARAMS = ("Ra", "La", "Rpl", "La_i", "Rpl_i", "Lb", "Rb", "Cout", "esr")

#: branch B off (the single-branch fit recovered exactly)
RB_OFF, LB_OFF = 1e9, 1e-12
#: the second R-L branch is adopted only when it cuts the squared residual by >40 %
BRANCH_B_KEEP = 0.6
#: a ladder stage is adopted only while it cuts the |Z| dB-RMS by at least this much
LADDER_KEEP_DB = 0.3


# --------------------------------------------------------------------------- the model


def zmodel(f, Ra, La, Rpl=1e12, Rb=RB_OFF, Lb=LB_OFF, extra=None,
           cout=1e-9, esr=0.5, c_ft=0.0):
    """Zout = (Ra + sLa||Rpl [+ extra sL_i||R_i]) || (Rb + sLb) || (esr + 1/sCout).

    `extra` is a list of additional SERIES (L_i||R_i) ladder sections inside branch A, each a
    rising-shelf section (short at DC, -> R_i at HF, corner R_i/L_i).  `extra=None`/`[]`
    reproduces the single-section shelf exactly.  `c_ft` is the gated vin->vout feedthrough
    capacitance: the Zout bench AC-grounds vin, so an enabled C_ft shunts the output.
    """
    f = np.asarray(f, float)
    s = 1j * TWO_PI * f
    ZA = Ra + (s * La * Rpl) / (s * La + Rpl)
    if extra:
        for Li, Ri in extra:
            ZA = ZA + (s * Li * Ri) / (s * Li + Ri)
    ZB = Rb + s * Lb
    ZC = esr + 1.0 / (s * cout)
    Y = 1.0 / ZA + 1.0 / ZB + 1.0 / ZC
    if c_ft > 0.0:
        Y = Y + s * c_ft
    return 1.0 / Y


def sections_of(params: dict):
    """[(L1,R1), (L2,R2), ...] -- the branch-A ladder of a fitted parameter dict, section 1
    being the (La, Rpl) shelf.  The large-signal ODE needs exactly this list."""
    secs = [(float(params["La"]), float(params["Rpl"]))]
    for Li, Ri in zip(params.get("La_i") or [], params.get("Rpl_i") or []):
        secs.append((float(Li), float(Ri)))
    return secs


def predict(params: dict, *, f, c_ft: float = 0.0, **_ignored) -> np.ndarray:
    """Analytic Zout(f) from the fitted parameters.  Pure numpy -- no simulator.

    `c_ft` is context, not a Zout parameter: it belongs to the PSRR block (the same physical
    feedthrough capacitance appears in both), so the caller threads it in when it was fitted.
    """
    return zmodel(np.asarray(f, float), float(params["Ra"]), float(params["La"]),
                  float(params["Rpl"]), float(params.get("Rb", RB_OFF)),
                  float(params.get("Lb", LB_OFF)),
                  extra=list(zip(params.get("La_i") or [], params.get("Rpl_i") or [])),
                  cout=float(params["Cout"]), esr=float(params["esr"]), c_ft=float(c_ft))


# --------------------------------------------------------------------------- shape gates


def is_shelf(f, Z) -> bool:
    """A loop-active output-impedance SHELF, not a passive LC resonance: |Z| climbs from a
    small LF floor to a high HF PLATEAU and STAYS there (no post-peak rolloff).

    SIGN-AGNOSTIC on purpose: the discriminator is the SHAPE, not the phase.  An earlier
    version gated on `Re(Z) < 0` and MISSED a positive-real silicon shelf, falling back to a
    mislocated resonant fit; the rolloff test below is the real discriminator, and a genuine
    HF LC peak does not pass it.
    """
    f = np.asarray(f, float)
    Z = np.asarray(Z)
    if f.size < 5:
        return False
    mag = np.abs(Z)
    R0 = float(mag[0])
    if not np.isfinite(R0) or R0 <= 0:
        return False
    ipk = int(np.argmax(mag))
    peak = float(mag[ipk])
    plateau = float(np.median(mag[f >= 0.5 * f[-1]]))
    Q = peak / R0
    return Q > 3.0 and ipk >= 0.6 * f.size and plateau > 0.6 * peak


def detect_cft(f_p, H, f_z, Z):
    """Extract + GATE the vin->vout feedthrough capacitance C_ft from the PSRR transfer's
    equivalent injection `i_c = H/Zout`.

    A physical feedthrough cap (pass-device Cgd / package coupling) makes `i_c` an exact `jwC`
    tail at the top of the band.  Fit `ic ~ G_hf + jwC` (weighted complex LS) on the TOP 1.5
    DECADES and enable ONLY when the tail is unambiguously a feedthrough cap:

      (a) C_LS > 0;
      (b) per-point relative std of `Im(ic)/(2 pi f)` over the top decade < 10 %;
      (c) dominance `w*C_LS/|ic(f_top)| > 0.5` at the band top;
      (d) the `(G_hf + jwC)` tail-fit relative RMS < 5 %.

    Returns 0.0 when any gate fails -- and it fails by orders of magnitude on a rail that has no
    feedthrough, so the legacy (no-C_ft) path is untouched.  The cross-CORNER spread gate of the
    original lives in the driver (`fit_project`), which is the only place that sees every load.
    """
    f_p = np.asarray(f_p, float)
    H = np.asarray(H)
    zr = np.interp(np.log(f_p), np.log(np.asarray(f_z, float)), np.asarray(Z).real)
    zi = np.interp(np.log(f_p), np.log(np.asarray(f_z, float)), np.asarray(Z).imag)
    Zi = zr + 1j * zi
    with np.errstate(divide="ignore", invalid="ignore"):
        ic = H / Zi
    good = (np.isfinite(ic) & (np.abs(ic) > 0) & np.isfinite(H) & (np.abs(H) > 0)
            & np.isfinite(Zi) & (np.abs(Zi) > 0))
    fp, ic = f_p[good], ic[good]
    if fp.size == 0:
        return 0.0
    top = fp >= fp[-1] / 10.0 ** 1.5
    if top.sum() < 8:
        return 0.0
    ft, yt = fp[top], ic[top]
    w = 1.0 / np.abs(yt)
    A = np.vstack([np.ones(len(ft)), 1j * TWO_PI * ft]).T * w[:, None]
    b = yt * w
    try:
        x, *_ = np.linalg.lstsq(np.vstack([A.real, A.imag]),
                                np.concatenate([b.real, b.imag]), rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    g_hf, c_ls = float(x[0]), float(x[1])
    if not np.isfinite(c_ls) or c_ls <= 0 or not np.isfinite(g_hf):          # (a)
        return 0.0
    td = fp >= fp[-1] / 10.0
    cpt = ic[td].imag / (TWO_PI * fp[td])
    mu = float(np.mean(cpt))
    if not np.isfinite(mu) or mu <= 0 or not float(np.std(cpt)) / abs(mu) <= 0.10:   # (b)
        return 0.0
    if TWO_PI * fp[-1] * c_ls / abs(ic[-1]) < 0.5:                            # (c)
        return 0.0
    rel = np.abs((g_hf + 1j * TWO_PI * ft * c_ls) - yt) / np.abs(yt)
    if float(np.sqrt(np.mean(rel ** 2))) > 0.05:                              # (d)
        return 0.0
    return float(c_ls)


# --------------------------------------------------------------------------- Cout / ESR


def extract_cout_esr(f, Z, c_ft: float = 0.0):
    """Auto-extract the physical Cout/ESR from the CAPACITIVE band of the measured Zout.

    Above all resonances the L branch is high-Z and `Z -> esr + 1/(jwC)`, so `esr = Re Z` and
    `C = -1/(w Im Z)`.  Reading the band directly (rather than fitting the whole curve) is
    robust to multi-pole mid-band shapes, which pull a full fit off.  Selecting by PHASE
    (`< -45 deg`) rather than by "the last decade" keeps C right even when a large ESR floors
    the HF tail.

    KNOWN LIMITATION, kept deliberately: when ESR >> the output resistance the capacitor is
    electrically near-invisible and Cout/esr are weakly identifiable.  The joint least-squares
    that would "fix" it is REJECTED (underdetermined -> diverges); the identifiability gate
    reports it instead.

    Two shape gates ride along:
      * SHELF -- |Z| rises to an HF plateau with no rolloff, so there is NO physical shunt cap
        and the envelope fallback would clamp the plateau: hold the cap branch open.
      * GHOST-CAP -- when the claimed shunt branch sits orders below the measured |Z| in the
        very band the estimate came from, the part is capless (or the HF export is bad); fall
        back to the LARGEST cap whose impedance clears |Z| everywhere, then ADJUDICATE the two
        candidates by fitting the full Zout with each and keeping whichever explains the
        measurement (a real bulk cap behind a multi-stage PDN looks exactly like a ghost).
    Returns `(Cout, esr, notes)`.
    """
    f = np.asarray(f, float)
    Z = np.asarray(Z)
    notes: list[str] = []
    if c_ft > 0.0:
        Z = 1.0 / (1.0 / Z - 1j * TWO_PI * f * c_ft)
    cap = np.angle(Z) < -np.pi / 4
    sel = cap if cap.sum() >= 3 else (f > 0.3 * f[-1])
    with np.errstate(divide="ignore", invalid="ignore"):
        Cc = float(np.median(-1.0 / (TWO_PI * f[sel] * Z[sel].imag)))
    tail = f > 0.3 * f[-1]
    Rc = float(max(np.median(Z[tail].real), 1e-3))

    if is_shelf(f, Z):
        notes.append("shelf gate: Zout is a loop-active rising shelf (no output cap) -> the "
                     "cap branch is held open")
        return 1e-13, Rc, notes

    band = sel & (Z.imag < 0)
    ghost = not np.isfinite(Cc) or Cc <= 0
    if not ghost and band.sum() >= 3:
        zb = np.abs(Rc + 1.0 / (1j * TWO_PI * f[band] * Cc))
        ghost = bool(np.median(np.abs(Z[band]) / zb) > 4.0)
    if not ghost:
        zb = np.abs(Rc + 1.0 / (1j * TWO_PI * f * Cc))
        ghost = bool(np.max(np.abs(Z) / zb) > 20.0)
    if ghost:
        Cc_med = Cc
        Cc = float(1.0 / (TWO_PI * np.max(f * np.abs(Z))))
        if np.isfinite(Cc_med) and Cc_med > 0:
            def _zrms_with(cand):
                Zm = zmodel(f, *fit_zout(f, Z, cand, Rc), cout=cand, esr=Rc)
                return db_rms(Zm, Z)
            r_med, r_env = _zrms_with(Cc_med), _zrms_with(Cc)
            if r_med < r_env - 1.0:
                notes.append(f"ghost-cap gate OVERTURNED by evidence: the median read "
                             f"{Cc_med * 1e12:.2f} pF fits Zout to {r_med:.2f} dB vs "
                             f"{r_env:.2f} dB for the envelope -> a real shunt capacitor "
                             f"(a high-Q tank peak, or a bulk cap behind a multi-stage "
                             f"network, sits above both branches exactly like a ghost does); "
                             f"keeping the median")
                return Cc_med, Rc, notes
        notes.append(f"ghost-cap gate: the HF band is not a shunt cap (capless rail or a bad "
                     f"HF export) -> Cout = {Cc * 1e12:.2f} pF (envelope fallback)")
    return Cc, Rc, notes


# --------------------------------------------------------------------------- the fitters


def fit_zout(f, Z, cout, esr, c_ft=0.0):
    """Fit `(Ra, La, Rpl, Rb, Lb)` to the measured Zout.

    Bounded TRF (it cannot run away -- an unbounded Levenberg-Marquardt with an argmax-of-flat
    init is REJECTED: it produced a 32 MOhm Ra, a 92 dB spurious peak and a 1e269 F cap),
    peak-adaptive weighting, multi-start over the peak and plateau regimes, and a
    peak-significance gate so a flat response is fitted as ~R (the L pole pushed to the band
    top) instead of a spurious huge resonance.
    """
    f = np.asarray(f, float)
    Z = np.asarray(Z)
    R0 = float(np.abs(Z[0]))
    mag = np.abs(Z)
    fpk = f[int(np.argmax(mag))]
    Q = mag.max() / R0
    w = (1 + 4 * ((f > 0.5 * fpk) & (f < 2 * fpk))) if Q > 1.3 else np.ones_like(f)
    Lmax = 1.0 / ((TWO_PI * (f[0] / 3)) ** 2 * cout)
    Lmin = 1.0 / ((TWO_PI * (f[-1] * 3)) ** 2 * cout)
    Lpk = float(np.clip(1.0 / ((TWO_PI * fpk) ** 2 * cout), Lmin, Lmax))
    Lflat = float(np.clip(1.0 / ((TWO_PI * f[-1]) ** 2 * cout), Lmin, Lmax))

    if is_shelf(f, Z):
        # The positive-real model cannot match a loop-active negative-real phase, so fit |Z|
        # MAGNITUDE-ONLY (the score is magnitude-only) with the cap branch held open.
        plateau = float(np.median(mag[f >= 0.5 * f[-1]]))
        R_pl0 = max(plateau - R0, R0)
        zc = np.sqrt(R0 * max(plateau, R0 * 1.01))
        wz = TWO_PI * f[int(np.argmin(np.abs(mag - zc)))]
        La0 = float(np.clip(R_pl0 / max(wz, TWO_PI * f[0]), Lmin, Lmax))

        def resid_shelf(p):
            Zm = zmodel(f, np.exp(p[0]), np.exp(p[1]), np.exp(p[2]), cout=cout, esr=esr,
                        c_ft=c_ft)
            return np.log(np.abs(Zm)) - np.log(mag)
        bnds_s = ([np.log(R0 / 5), np.log(Lmin), np.log(R0 / 3)],
                  [np.log(R0 * 5), np.log(Lmax), np.log(1e9)])
        ss = least_squares(resid_shelf, [np.log(R0), np.log(La0), np.log(R_pl0)],
                           method="trf", bounds=bnds_s, max_nfev=4000)
        Ra, La, Rpl = np.exp(ss.x)
        return (Ra, La, Rpl, RB_OFF, LB_OFF)

    def err_x(resid, inits, bnds):
        best = None
        for p0 in inits:
            s = least_squares(resid, p0, method="trf", bounds=bnds, max_nfev=4000)
            e = float(np.sum(s.fun ** 2))
            if best is None or e < best[0]:
                best = (e, s.x)
        return best

    def resid1(p):
        Zm = zmodel(f, np.exp(p[0]), np.exp(p[1]), np.exp(p[2]), cout=cout, esr=esr, c_ft=c_ft)
        r = np.log(Zm) - np.log(Z)                       # ln|.| + j*angle => magnitude+phase
        return np.concatenate([r.real * w, r.imag * w])
    bnds1 = ([np.log(R0 / 5), np.log(Lmin), np.log(R0 / 3)],
             [np.log(R0 * 5), np.log(Lmax), np.log(1e9)])
    inits1 = [[np.log(R0), np.log(Lpk), np.log(1e9)],
              [np.log(R0), np.log(Lpk), np.log(R0 * 30)],
              [np.log(R0), np.log(Lflat), np.log(max(R0 * 8, R0 / 2))]]
    e1, x1 = err_x(resid1, inits1, bnds1)
    Ra, La, Rpl = np.exp(x1)

    def resid2(p):
        Zm = zmodel(f, np.exp(p[0]), np.exp(p[1]), np.exp(p[2]), np.exp(p[3]), np.exp(p[4]),
                    cout=cout, esr=esr, c_ft=c_ft)
        r = np.log(Zm) - np.log(Z)
        return np.concatenate([r.real * w, r.imag * w])
    bnds2 = ([np.log(R0 / 5), np.log(Lmin), np.log(R0 / 3), np.log(R0 / 3), np.log(Lmin)],
             [np.log(R0 * 5), np.log(Lmax), np.log(1e9), np.log(1e9), np.log(Lmax)])
    inits2 = [[x1[0], x1[1], x1[2], np.log(R0 * 3), np.log(lb)]
              for lb in (Lpk, max(Lpk / 20, Lmin), Lflat)]
    e2, x2 = err_x(resid2, inits2, bnds2)
    if e2 < BRANCH_B_KEEP * e1:
        return tuple(np.exp(x2))                         # the 2nd branch earned its place
    return (Ra, La, Rpl, RB_OFF, LB_OFF)


def fit_zout_ladder(f, Z, n_max: int = 3, gain_db: float = LADDER_KEEP_DB):
    """Fit branch A as a HIGHER-ORDER (L||R) ladder, magnitude-fitted to |Zout|.

    A single (La||Rpl) section cannot reproduce a measured multi-decade inductive rise whose
    effective L VARIES with frequency, so the one-section fit mislocates the rise corner.  Each
    extra section adds a rise corner.  KEEP-BEST + GATED: N grows only while it cuts the |Z|
    dB-RMS by at least `gain_db`, and the smallest N that plateaus wins.  N == 1 returns an
    empty `extra` -> identical to the single-section shelf.

    Returns `(Ra, La, Rpl, extra, rms_db)`; the cap/branch-B are omitted here on purpose (the
    ladder targets the low-frequency rise where the small cap is negligible).
    """
    f = np.asarray(f, float)
    mag = np.abs(np.asarray(Z))
    R0 = float(mag[0])
    plateau = float(np.median(mag[f >= 0.5 * f[-1]]))
    lo, hi = f[0], f[-1]

    def zladder(p):
        s = 1j * TWO_PI * f
        ZA = np.exp(p[0]) + 0j * s
        for i in range(1, len(p), 2):
            Li, Ri = np.exp(p[i]), np.exp(p[i + 1])
            ZA = ZA + (s * Li * Ri) / (s * Li + Ri)
        return ZA

    def resid(p):
        return np.log(np.abs(zladder(p))) - np.log(mag)

    def fit_n(n):
        corners = np.logspace(np.log10(max(lo * 3, 1e2)), np.log10(hi / 3), n)
        Rsec = max((plateau - R0) / n, R0)
        p0 = [np.log(max(R0, 1e-3))]
        for wc in corners:
            p0 += [np.log(Rsec / (TWO_PI * wc)), np.log(Rsec)]
        lob = [np.log(R0 / 10)] + [np.log(1e-12), np.log(R0 / 50)] * n
        hib = [np.log(max(R0 * 10, 1e-2))] + [np.log(1e-1), np.log(plateau * 5 + 1)] * n
        best = None
        for jit in (1.0, 0.3, 3.0):
            pp = list(p0)
            for k in range(1, len(pp), 2):
                pp[k] += np.log(jit)
            try:
                ss = least_squares(resid, pp, method="trf", bounds=(lob, hib), max_nfev=6000)
            except Exception:                            # noqa: BLE001 -- ladder is opportunistic
                continue
            e = float(np.sqrt(np.mean(ss.fun ** 2))) * (20.0 / np.log(10))
            if best is None or e < best[0]:
                best = (e, ss.x)
        return best

    res = {}
    for n in range(1, n_max + 1):
        b = fit_n(n)
        if b is not None:
            res[n] = b
    if not res:
        return (R0, 1e-6, max(plateau, R0), [], float("inf"))
    nbest = 1
    for n in range(2, n_max + 1):
        if n in res and (n - 1) in res and res[n][0] <= res[n - 1][0] - gain_db:
            nbest = n
    x = res[nbest][1]
    Ra = float(np.exp(x[0]))
    secs = [(float(np.exp(x[i])), float(np.exp(x[i + 1]))) for i in range(1, len(x), 2)]
    secs.sort(key=lambda LR: LR[1] / LR[0])              # section 1 = the lowest corner
    La, Rpl = secs[0]
    return (Ra, La, Rpl, secs[1:], float(res[nbest][0]))


# --------------------------------------------------------------------------- the block


def fit(dataset, port: str, cell: dict, derived=None) -> BlockFit:
    """Fit the Zout block of one rail at one cell of contract 2."""
    cell = block_cell("zout", "rail", cell)
    var = f"ac_zout.{port}"
    try:
        f, Z = read_curve(dataset, var, cell)
    except NoData as exc:
        return missing_fit(port, "zout", cell, exc.reason, metric="|Zout| dB RMS")
    if f.size < 4:
        return missing_fit(port, "zout", cell,
                           f"{var}: only {f.size} usable frequency point(s)",
                           metric="|Zout| dB RMS")

    notes: list[str] = []
    c_ft = 0.0
    pvar = f"ac_psrr.{port}"
    if have(dataset, pvar):
        try:
            fp, H = read_curve(dataset, pvar, cell)
            c_ft = detect_cft(fp, H, f, Z)
        except NoData:
            c_ft = 0.0
    if c_ft > 0.0:
        notes.append(f"feedthrough gate: i_c has a jwC tail -> C_ft = {c_ft * 1e15:.1f} fF, "
                     f"de-embedded from Zout before the cap extraction")

    cout, esr, cnotes = extract_cout_esr(f, Z, c_ft)
    notes += cnotes
    Ra, La, Rpl, Rb, Lb = fit_zout(f, Z, cout, esr, c_ft)
    extra: list = []
    rms = db_rms(zmodel(f, Ra, La, Rpl, Rb, Lb, cout=cout, esr=esr, c_ft=c_ft), Z)

    # The (L||R) ladder ONLY refines a rising-shelf Zout (that is its purpose); running its
    # multi-start on a resonant corner is wasted compute it can never win.
    if is_shelf(f, Z):
        try:
            Ra2, La2, Rpl2, ex2, _ = fit_zout_ladder(f, Z)
        except Exception:                                # noqa: BLE001 -- opportunistic
            ex2 = []
        if ex2:
            rms2 = db_rms(zmodel(f, Ra2, La2, Rpl2, RB_OFF, LB_OFF, extra=ex2,
                                 cout=cout, esr=esr, c_ft=c_ft), Z)
            if rms2 <= rms - LADDER_KEEP_DB:
                notes.append(f"higher-order ladder engaged: {len(ex2) + 1} (L||R) stages beat "
                             f"one stage by {rms - rms2:.2f} dB")
                Ra, La, Rpl, Rb, Lb, extra, rms = Ra2, La2, Rpl2, RB_OFF, LB_OFF, ex2, rms2
    if Rb < RB_OFF:
        notes.append("second R-L branch engaged (it beat the single-branch fit by more "
                     "than 40 %)")

    params = {"Ra": float(Ra), "La": float(La), "Rpl": float(Rpl),
              "La_i": [float(L) for L, _ in extra], "Rpl_i": [float(R) for _, R in extra],
              "Lb": float(Lb), "Rb": float(Rb),
              "Cout": float(cout), "esr": float(esr)}

    min_re = float(np.min(np.asarray(Z).real))
    if min_re < 0.0:
        notes.append(f"FLOOR, not a bug: the ground truth is NON-PASSIVE (min Re Z = "
                     f"{min_re:.3g} ohm, an actively regulated rail). A passive RLC cannot "
                     f"reproduce it, so part of the {rms:.2f} dB residual is representational.")

    keys = ["Ra", "La", "Rpl", "Rb", "Lb", "Cout", "esr"]

    def g(p, f=f):
        return zmodel(f, p[0], p[1], p[2], p[3], p[4], extra=extra, cout=p[5], esr=p[6],
                      c_ft=c_ft)
    fe = ident.envelope_grid(f, ident.envelope_band(derived, "freq"))
    gate = ident.gate(g, keys, [params[k] for k in keys], envelope=lambda p: g(p, fe),
                      bounds={"Rpl": (None, 1e9), "Rb": (None, RB_OFF)})
    notes += ident.describe(gate)
    return BlockFit(port=port, block="zout", cell=cell, params=params, score=float(rms),
                    metric="|Zout| dB RMS", n_points=int(f.size), identifiability=gate,
                    notes=notes)

