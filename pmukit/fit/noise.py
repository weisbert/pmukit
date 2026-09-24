# from LDO_modeling/harness/fit_model.py @ d2c5b80
"""Rail output noise: a DECOUPLED Norton source at vout, with a gated hybrid alternative.

    norton:  In^2(f) = white^2 + flicker^2/f + sum_k amp_k^2 / (1 + (f/corner_k)^2)
             Sv(f)   = In(f) * |Zout(f)|

    hybrid:  Vn^2(f) = flicker^2/f + sum_k amp_k^2 / (1 + (f/corner_k)^2)      [series, in branch A]
             Sv^2(f) = Vn^2 * |Zout/ZA|^2 + white^2 * |Zout|^2

"DECOUPLED" decouples SYNTHESIS, not PHYSICS.  It is an algebraic round trip
(`In = Sv/|Zout|` at fit time, `Sv = In*|Zout|` at emit time), so a Zout error leaks IDENTICALLY
into the noise -- there is nothing to "fix" about the round trip, and the honest way to score the
block is end-to-end against the measured Sv, which is what `score` reports.

The fit is JOINT over the load points of one corner: the corner frequencies are SHARED (they are
load-independent poles) and only the amplitudes move, so a later interpolation moves amplitudes
across a fixed pole set instead of letting two sections collapse onto one pole.  A separation
penalty keeps adjacent shared corners at least 2x apart for the same reason.

It is fitted in the LOG AMPLITUDE domain.  A linear least-squares in POWER is dominated by the
large low-frequency flicker values and barely constrains the small high-frequency floor: it read
the white floor 5-30x high and scored 9-15 dB off on real parts.

`nmode` picks the realization, and it MUST be carried through to the emitter.  The hybrid form
once fitted correctly and was never emitted -- the emitter only knew the Norton form, so for a
hybrid rail it produced ZERO sections, shipped the bare white floor, and the ENTIRE 1/f tail
silently vanished from a deployed model (the low-frequency term is ~300x the high-frequency one
on a real rail, and it is the number the phase-noise user came for).
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from . import identifiability as ident
from . import zout as zout_block
from ._base import (TWO_PI, BlockFit, NoData, block_cell, missing_fit, read_curve)

__all__ = ["fit", "fit_bank", "predict", "sv_model", "PARAMS"]

PARAMS = ("nmode", "white", "flicker", "corner_i_hz", "amp_i")

#: Lorentzian sections the bank starts with
MNOISE = 6
#: dB worst-load Sv log-RMS that triggers growing the bank
NOISE_ADAPT_TRIG = 4.0
#: adaptive ceiling (sections are cheap: one R||C and one controlled source each)
NOISE_M_MAX = 10
#: the hybrid topology is only adopted for a clear win -- a marginal 0.05 dB "improvement"
#: must not flap the emitted structure
HYBRID_MARGIN_DB = 0.5
#: adjacent shared corners stay at least this far apart (anti-degeneracy)
MIN_LOG_GAP = np.log(2.0)


# --------------------------------------------------------------------------- the model


def _za(f, zparams):
    """Branch-A series impedance `ZA = Ra + jwLa||Rpl [+ ladder]`.  A series voltage source
    between the regulation node and Ra reaches vout through `T = Zout/ZA` (the noise bench load
    is a current source, i.e. an AC open), so T is computed from the SAME Zout the rest of the
    fit uses."""
    s = 1j * TWO_PI * np.asarray(f, float)
    ZA = float(zparams["Ra"]) + 0j * s
    for Li, Ri in zout_block.sections_of(zparams):
        ZA = ZA + (s * Li * Ri) / (s * Li + Ri)
    return ZA


def sv_model(params: dict, f, zparams: dict, c_ft: float = 0.0) -> np.ndarray:
    """THE shared output-noise reconstruction Sv [V/rtHz] -- the single source of truth used by
    the fit's own score and by `predict`, so they agree to machine precision."""
    f = np.asarray(f, float)
    Z = zout_block.predict(zparams, f=f, c_ft=c_ft)
    white = float(params.get("white", 0.0))
    flick = float(params.get("flicker", 0.0))
    corners = list(params.get("corner_i_hz") or [])
    amps = list(params.get("amp_i") or [])
    bank = flick ** 2 / np.maximum(f, 1e-300)
    for a, fk in zip(amps, corners):
        bank = bank + float(a) ** 2 / (1.0 + (f / float(fk)) ** 2)
    if str(params.get("nmode", "norton")) == "hybrid":
        T2 = np.abs(Z / _za(f, zparams)) ** 2
        return np.sqrt(bank * T2 + white ** 2 * np.abs(Z) ** 2)
    return np.sqrt(white ** 2 + bank) * np.abs(Z)


def predict(params: dict, *, f, zout, c_ft: float = 0.0, **_ignored) -> np.ndarray:
    """Analytic output-noise amplitude Sv(f) [V/rtHz].  `zout` is the fitted Zout parameter
    dict of the same cell -- the noise block is decoupled in SYNTHESIS only."""
    return sv_model(params, f, zout, c_ft)


# --------------------------------------------------------------------------- the fitter


def _equalize(f, y):
    """Resample onto a uniform log grid when the export is not log-uniform.

    The log residual weighs every SAMPLE equally, so a linear-frequency noise export (typical
    pnoise binning) puts almost no samples below 100 kHz and the flicker tail simply is not in
    the fit.  A log-uniform export skips this untouched.
    """
    f = np.asarray(f, float)
    y = np.asarray(y, float)
    dl = np.diff(np.log(f))
    pos = dl[dl > 0]
    if pos.size and np.max(dl) > 2.5 * np.min(pos):
        fu = np.logspace(np.log10(f[0]), np.log10(f[-1]),
                         max(int(24 * np.log10(f[-1] / f[0])), 24))
        return fu, np.exp(np.interp(np.log(fu), np.log(f), np.log(y + 1e-80))), True
    return f, y, False


#: the separation penalty's weight (a residual of 30 per unit of log-gap shortfall)
SEP_WEIGHT = 30.0
#: STALL GUARD: end a joint solve whose cost has improved by less than STALL_RTOL (relative)
#: over the last STALL_WINDOW iterations.  The optimum of this problem often sits exactly ON the
#: separation hinge (two corners one octave apart), where the Gauss-Newton model flips between
#: "penalty on" and "penalty off" from step to step; trf then crawls along the kink for
#: thousands of iterations, each improving the cost by ~1e-7 relative.  On the fake PMU at
#: 125 C that was 8400 iterations and 60 s for a bank indistinguishable -- to 1e-3 dB in Sv --
#: from the one it had after the first hundred; a normal solve converges in < 100 iterations
#: and never reaches the guard.  1e-4 of the cost is 5e-5 of the RMS dB score.
STALL_WINDOW = 50
STALL_RTOL = 1e-4


class _Stalled(Exception):
    """Raised from inside the residual to end a solve that has stopped making progress."""


class _Bank:
    """The joint noise-bank problem, vectorized over every (load, frequency) sample at once.

    The unknowns are the M SHARED log corner frequencies followed by, per load,
    `[log white^2, log flicker^2, log amp_1^2 .. log amp_M^2]`.  One residual evaluation is a
    handful of array operations instead of a Python loop over loads and sections -- it runs
    `1 + n_params` times per iteration under the finite-difference Jacobian.

    `resid` also carries the STALL GUARD: it keeps the best point seen and raises `_Stalled`
    when the solve stops making progress (see STALL_WINDOW).  Only ITERATIONS count toward the
    window, not the Jacobian's probe points: a probe differs from the last iterate in exactly
    one coordinate, by a relative step of ~1.5e-8.
    """

    def __init__(self, targets, keys, M, mode):
        self.M, self.mode, self.nL = M, mode, len(keys)
        fs, goals, t2s, z2s, idx = [], [], [], [], []
        self.slices = []
        n = 0
        for j, key in enumerate(keys):
            f, goal, T2, Z2 = targets[key]
            f = np.asarray(f, float)
            fs.append(f)
            goals.append(np.asarray(goal, float))
            t2s.append(np.broadcast_to(np.asarray(T2, float), f.shape))
            z2s.append(np.broadcast_to(np.asarray(Z2, float), f.shape))
            idx.append(np.full(f.size, j))
            self.slices.append(slice(n, n + f.size))
            n += f.size
        self.f = np.concatenate(fs)
        self.inv_f = 1.0 / np.maximum(self.f, 1e-300)
        self.T2 = np.concatenate(t2s)
        self.Z2 = np.concatenate(z2s)
        self.j = np.concatenate(idx)
        self.log_goal = np.log(np.concatenate(goals) + 1e-80)
        self.N = n
        self.best = (np.inf, None)         # (cost, point) -- the lowest cost evaluated
        self.iterations = 0                # evaluations that were not a Jacobian probe
        self._ref = np.inf                 # the cost the stall window measures progress from
        self._since = 0                    # iterations since the cost last beat _ref
        self._base = None                  # the last iterate (a probe perturbs this one)

    def model(self, p) -> np.ndarray:
        """The modeled goal (In^2 or Sv^2) at every sample, loads concatenated."""
        M = self.M
        p = np.asarray(p, float)
        fks = np.exp(p[:M])
        rest = np.exp(p[M:].reshape(self.nL, M + 2))[self.j]      # (N, M+2)
        lor = 1.0 / (1.0 + (self.f[:, None] / fks[None, :]) ** 2)  # (N, M)
        bank = rest[:, 1] * self.inv_f + np.sum(rest[:, 2:] * lor, axis=1)
        if self.mode == "hybrid":
            return bank * self.T2 + rest[:, 0] * self.Z2
        return rest[:, 0] + bank

    def per_load(self, p) -> list:
        m = self.model(p)
        return [m[s] for s in self.slices]

    def _is_probe(self, p) -> bool:
        if self._base is None:
            return False
        d = np.nonzero(p != self._base)[0]
        if d.size == 0:
            return True
        return d.size == 1 and abs(p[d[0]] - self._base[d[0]]) <= 1e-6 * max(
            1.0, abs(self._base[d[0]]))

    def resid(self, p):
        p = np.array(p, float)
        # SEPARATION penalty: two sections must not collapse onto one pole (which makes
        # anti-correlated giant amplitudes and a huge inter-corner interpolation overshoot).
        gaps = np.diff(np.sort(p[:self.M]))
        r = np.concatenate((np.log(self.model(p) + 1e-80) - self.log_goal,
                            SEP_WEIGHT * np.maximum(0.0, MIN_LOG_GAP - gaps)))
        cost = 0.5 * float(r @ r)
        if cost < self.best[0]:
            self.best = (cost, p)
        if not self._is_probe(p):
            self._base = p
            self.iterations += 1
            if cost < self._ref * (1.0 - STALL_RTOL):
                self._ref, self._since = cost, 0
            else:
                self._since += 1
                if self._since >= STALL_WINDOW:
                    raise _Stalled()
        return r


def _joint_fit(targets, keys, M, mode, fks_init=None):
    """One joint least_squares over the load points.

    `targets[key] = (f, goal, T2, Z2)` where `goal` is In^2 (norton) or Sv^2 (hybrid).
    The unknowns are the M SHARED log corner frequencies followed by, per load,
    `[log white^2, log flicker^2, log amp_1^2 .. log amp_M^2]`.
    """
    nL = len(keys)
    f0 = targets[keys[0]][0][0]
    f1 = targets[keys[0]][0][-1]
    prob = _Bank(targets, keys, M, mode)

    if fks_init is not None:
        fks0 = np.log(np.sort(np.asarray(fks_init, float)))
    else:
        fks0 = np.log(np.logspace(np.log10(f0 * 1.5), np.log10(f1 / 1.5), M))
    init = list(fks0)
    lob = list(np.log(np.full(M, f0 / 5)))
    hib = list(np.log(np.full(M, f1 * 5)))
    for key in keys:
        f, goal, T2, Z2 = targets[key]
        if mode == "hybrid":
            base = goal / (Z2 + 1e-80)
            vt = goal / (T2 + 1e-80)
            init += [float(np.log(np.mean(base[-3:]) + 1e-80)),
                     float(np.log(max(np.min(vt), 1e-80) * f[0] + 1e-80))]
            init += list(np.log(np.interp(np.exp(fks0), f, vt) + 1e-80))
        else:
            wht = float(np.mean(goal[-3:]))
            init += [float(np.log(wht + 1e-80)),
                     float(np.log(max(goal[0] - wht, wht * 1e-6) * f[0] + 1e-80))]
            init += list(np.log(np.interp(np.exp(fks0), f, goal) + 1e-80))
        lob += [-200.0] * (M + 2)
        hib += [60.0] * (M + 2)
    try:
        x = least_squares(prob.resid, init, bounds=(lob, hib), method="trf",
                          max_nfev=30000).x
    except _Stalled:
        x = prob.best[1]              # the lowest cost evaluated: an iterate or one probe off
    fks = np.exp(x[:M])
    rest = x[M:].reshape(nL, M + 2)
    worst = 0.0
    for key, m in zip(keys, prob.per_load(x)):
        goal = targets[key][1]
        # the goal is a SQUARED quantity, so its log ratio in 10*log10 IS the Sv dB error
        worst = max(worst, float(np.sqrt(np.mean(
            (10.0 * np.log10((m + 1e-80) / (goal + 1e-80))) ** 2))))
    order = np.argsort(fks)                 # 'section k' must be the same pole at every load
    fks = fks[order]
    out = {}
    for j, key in enumerate(keys):
        out[key] = {
            "white": float(np.sqrt(max(np.exp(rest[j, 0]), 0.0))),
            "flicker": float(np.sqrt(max(np.exp(rest[j, 1]), 0.0))),
            "amp_i": [float(np.sqrt(max(np.exp(rest[j, 2 + k]), 0.0))) for k in order],
        }
    return {"corner_i_hz": [float(x) for x in fks], "per_load": out, "worst": worst}


def _worst_point(bank, targets, keys, mode):
    """Frequency where the current bank misfits worst (max log error over the loads)."""
    fbest, ebest = None, -1.0
    for key in keys:
        f, goal, T2, Z2 = targets[key]
        row = bank["per_load"][key]
        p = dict(row, nmode=mode, corner_i_hz=bank["corner_i_hz"])
        m = _eval_goal(p, f, T2, Z2, mode)
        e = np.abs(10.0 * np.log10((m + 1e-80) / (goal + 1e-80)))
        j = int(np.argmax(e))
        if e[j] > ebest:
            ebest, fbest = float(e[j]), float(f[j])
    return fbest


def _eval_goal(p, f, T2, Z2, mode):
    bank = float(p["flicker"]) ** 2 / np.maximum(f, 1e-300)
    for a, fk in zip(p["amp_i"], p["corner_i_hz"]):
        bank = bank + float(a) ** 2 / (1.0 + (f / float(fk)) ** 2)
    if mode == "hybrid":
        return bank * T2 + float(p["white"]) ** 2 * Z2
    return float(p["white"]) ** 2 + bank


def _adaptive(targets, keys, mode, M0):
    """Start at the legacy bank size; while the worst load misfits by more than the trigger,
    GREEDILY INSERT one section at the worst-fit frequency and refit warm-started from the
    current corners (a fresh logspace init at larger M lands in worse local minima).  Stop when
    an insertion stops helping -- a residual no added low-pass section can remove is the known
    loop-noise-shape bound, not a fixable fit."""
    best = _joint_fit(targets, keys, M0, mode)
    f0 = targets[keys[0]][0][0]
    f1 = targets[keys[0]][0][-1]
    while best["worst"] > NOISE_ADAPT_TRIG and len(best["corner_i_hz"]) < NOISE_M_MAX:
        fstar = _worst_point(best, targets, keys, mode)
        if fstar is None:
            break
        fstar = float(np.clip(fstar, f0 / 5 * 1.01, f1 * 5 * 0.99))
        cand = _joint_fit(targets, keys, len(best["corner_i_hz"]) + 1, mode,
                          fks_init=list(best["corner_i_hz"]) + [fstar])
        if cand["worst"] < best["worst"] - 1e-9:
            best = cand
        else:
            break
    return best


def fit_bank(dataset, port: str, cell: dict, derived=None, *, zout_by_load=None,
             c_ft: float = 0.0) -> dict:
    """Fit the noise block JOINTLY across the load points of one corner.

    Returns `{load_a: BlockFit}`.  `zout_by_load` maps each load current to the Zout parameter
    dict fitted at that same load; when it is omitted the Zout blocks are fitted here.
    """
    var = f"noise_v.{port}"
    # The REPORTED cell is this block's own granularity (no vset: the noise parameters do not
    # vary with the trim code); the cell the dataset is READ with is the full one, because the
    # dataset stores every variable over its own declared axes.
    base = block_cell("noise", "rail", cell)

    def _report_cell(il):
        sub = dict(base)
        if "load_a" in base:
            sub["load_a"] = il
        return sub

    loads: list = []
    try:
        loads = [float(x) for x in dataset.axis("load_a", port)]
    except Exception:                                 # noqa: BLE001 -- a rail with no load axis
        loads = []
    if not loads:
        loads = [float(cell.get("load_a", 0.0))]

    targets: dict = {}
    zmap: dict = {}
    notes_common: list = []
    equalized = False
    for il in loads:
        sub = dict(cell, load_a=il)
        try:
            f, sv = read_curve(dataset, var, sub, psd=True)
        except NoData:
            continue
        zp = (zout_by_load or {}).get(il)
        if zp is None:
            zf = zout_block.fit(dataset, port, dict(cell, load_a=il), derived)
            if zf.missing:
                continue
            zp = zf.params
        f, sv, eq = _equalize(f, sv)
        equalized = equalized or eq
        Z = zout_block.predict(zp, f=f, c_ft=c_ft)
        T2 = np.abs(Z / _za(f, zp)) ** 2
        Z2 = np.abs(Z) ** 2
        targets[il] = (f, (sv / np.abs(Z)) ** 2, T2, Z2)       # norton goal: In^2
        zmap[il] = (zp, sv, f, T2, Z2)
    if not targets:
        return {il: missing_fit(port, "noise", _report_cell(il),
                                f"{var}: not measured at this corner",
                                metric="Sv dB RMS") for il in loads}
    if equalized:
        notes_common.append("the noise export was not log-uniform; it was resampled onto a "
                            "uniform log grid so the flicker tail is actually in the fit")

    keys = sorted(targets)
    nb = _adaptive(targets, keys, "norton", MNOISE)
    mode, best = "norton", nb
    if nb["worst"] > NOISE_ADAPT_TRIG:
        hyb_targets = {k: (zmap[k][2], zmap[k][1] ** 2, zmap[k][3], zmap[k][4]) for k in keys}
        nh = _adaptive(hyb_targets, keys, "hybrid", 4)
        if nh["worst"] < nb["worst"] - HYBRID_MARGIN_DB:
            mode, best = "hybrid", nh
            notes_common.append(
                f"HYBRID engaged: a series voltage bank in branch A with "
                f"{len(nh['corner_i_hz'])} sections beat the Norton bank "
                f"({nb['worst']:.2f} -> {nh['worst']:.2f} dB worst load). The emitter MUST "
                f"emit the series bank; emitting only the Norton form drops the whole 1/f tail.")
    if len(best["corner_i_hz"]) != (MNOISE if mode == "norton" else 4):
        notes_common.append(f"bank ADAPTED to {len(best['corner_i_hz'])} Lorentzians "
                            f"(worst-load Sv fit {best['worst']:.2f} dB)")

    out: dict = {}
    for il in keys:
        zp, sv, f, _, _ = zmap[il]
        row = best["per_load"][il]
        params = {"nmode": mode, "white": row["white"], "flicker": row["flicker"],
                  "corner_i_hz": list(best["corner_i_hz"]), "amp_i": list(row["amp_i"])}
        model = sv_model(params, f, zp, c_ft)
        score = float(np.sqrt(np.mean((20.0 * np.log10((model + 1e-30) / (sv + 1e-30))) ** 2)))
        names = ["white", "flicker"] + [f"amp_i[{k}]" for k in range(len(row["amp_i"]))]
        vals = [row["white"], row["flicker"]] + list(row["amp_i"])

        def g(p, f=f, zp=zp, params=params):
            q = dict(params, white=p[0], flicker=p[1], amp_i=list(p[2:]))
            return sv_model(q, f, zp, c_ft)
        gate = ident.gate(g, names, vals)
        notes = list(notes_common)
        notes.append("decoupled in SYNTHESIS, not in physics: a Zout error leaks identically "
                     "into this block, so the score above is end-to-end against the measured Sv")
        notes += ident.describe(gate)
        out[il] = BlockFit(port=port, block="noise", cell=_report_cell(il), params=params,
                           score=score, metric="Sv dB RMS", n_points=int(f.size),
                           identifiability=gate, notes=notes)
    for il in loads:
        if il not in out:
            out[il] = missing_fit(port, "noise", _report_cell(il),
                                  f"{var}: not measured at this load", metric="Sv dB RMS")
    return out


def fit(dataset, port: str, cell: dict, derived=None, *, zout_by_load=None,
        c_ft: float = 0.0) -> BlockFit:
    """Fit the noise block and return the result for THIS cell's load point.

    The corner frequencies are shared across loads, so the underlying fit is necessarily joint;
    `fit_bank` exposes the whole set for the driver.
    """
    bank = fit_bank(dataset, port, cell, derived, zout_by_load=zout_by_load, c_ft=c_ft)
    want = float(cell.get("load_a", next(iter(bank)) if bank else 0.0))
    if want in bank:
        return bank[want]
    for il, bf in bank.items():
        if np.isclose(il, want, rtol=1e-9):
            return bf
    return missing_fit(port, "noise", block_cell("noise", "rail", cell),
                       f"noise_v.{port}: no load point matches {want!r}", metric="Sv dB RMS")
