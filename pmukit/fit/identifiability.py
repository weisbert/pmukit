# from LDO_modeling/harness/crossval.py @ d2c5b80
"""The identifiability gate: can the data determine this number at all?

Ported from `crossval._jacobian` / `_classify`.  The Jacobian is RELATIVE (a log-sensitivity
Jacobian): every parameter is perturbed by `p -> p*(1+delta)` and the response is measured as
`d ln g / d ln|p|`, so gains (~1e-3), corner frequencies (~1e6) and the complex section's `s`
coefficient (~1e-9) all sit on one unit-free footing.

A parameter with ~zero sensitivity (`Rpl = 1e9` in the Zout ladder -- moving it barely moves Z)
or a parameter sitting at exactly 0 (an inert PSRR section coefficient, whose relative
perturbation stays 0 and therefore yields a zero column) produces `sigma_min -> 0` and
`cond -> inf`.  That is the documented, WANTED behaviour: it reproduces the audit's
base/v1/v2 `Zout cond = inf` and it correctly flags an off section as unidentifiable, where an
ABSOLUTE step on a 0-valued coefficient would instead inject a huge fake high-frequency column.

**A failing gate is REPORTED, not fatal.**  The documented real case is a rail whose ESR is so
large that the output capacitor is nearly invisible: the honest answer is "the data cannot
determine Cout here", not a confident wrong number.

**Pinned is one question; does it matter is another.**  The column-norm and sigma tests only
say the data leaves a number FREE.  Whether that freedom can mislead anybody depends on where
the model is USED: the envelope (the band the consumer declared -- `derived.freq` for the AC
blocks, `derived.noise` for noise).  So when a fitter passes `envelope=` the gate also measures,
for every flagged parameter, its INFLUENCE: the largest change of the predicted magnitude
anywhere in the envelope (dB, max over points) among all the moves the data cannot rule out --
moves along the parameter alone and along the data's own softest joint direction for it,
walked outward from the fit until some data point shifts by more than `DATA_TOL_DB`.  A damping
resistor at 1e4 or 1e9 (`Rpl` above a resonant peak), a feedthrough at -140 dB (`G0` on a rail
whose i_c rolls off), a Lorentzian buried under a flicker term (`amp_i[k]`): all free, none able
to move the prediction where the consumer looks.  A feedthrough the band stops short of, or a
capacitor whose resonance lies above the last measured point but inside the envelope: free AND
able to move it by many dB -- those stay flagged with a large influence.  `verify.grades` holds
a green at yellow only on the second kind.
"""
from __future__ import annotations

import numpy as np

__all__ = ["jacobian", "gate", "describe", "envelope_band", "envelope_grid",
           "UNIDENT_REL", "RANKDEF_REL", "POORLY_REL", "DATA_TOL_DB", "MOVE_MAX"]

#: column-norm / max column-norm below this => the data does not see this parameter
UNIDENT_REL = 1e-3
#: sigma_min / sigma_max below this => rank-deficient (cond ~ inf)
RANKDEF_REL = 1e-9
#: sigma this many times the block's typical sigma => the data sees the parameter but pins it
#: far more loosely than the rest -- a wide number reported as if it were tight
POORLY_REL = 20.0
#: a move that shifts NO data point by more than this (dB of magnitude) is one the data cannot
#: rule out.  Half the tightest green limit (1 dB RMS): a model moved this far is still green on
#: every point, so the data genuinely cannot tell it from the fitted one.
DATA_TOL_DB = 0.5
#: the widest move tried, as a factor either way (1e4 -> 1e10 covers "pushed to infinity")
MOVE_MAX = 1.0e6
#: the first log step of the outward walk, and its growth per step
_T0, _GROW = 0.02, 1.4


def envelope_band(derived, key: str = "freq"):
    """`(lo_hz, hi_hz)` of the band the consumer uses this block in, or None when unknown.

    `key` is `"freq"` (the AC / PSRR band, `care_up_to_hz`) or `"noise"` (the noise band).
    """
    band = getattr(derived, key, None) if derived is not None else None
    if not isinstance(band, dict):
        return None
    try:
        lo, hi = float(band.get("start_hz")), float(band.get("stop_hz"))
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(lo) and np.isfinite(hi)) or lo <= 0 or hi <= lo:
        return None
    return lo, hi


def envelope_grid(f, band, per_decade: int = 20) -> np.ndarray:
    """The data grid, extended to cover `band` wherever the data stops short of it.

    With no band (or a band the data already spans) this is the data grid itself -- the
    influence test then has no extrapolation to judge, only the data's own points.
    """
    f = np.unique(np.asarray(f, float))
    if band is None or f.size == 0:
        return f
    lo, hi = float(band[0]), float(band[1])
    parts = [f]
    if lo < f[0] * (1 - 1e-9):
        n = max(int(np.ceil(per_decade * np.log10(f[0] / lo))), 1)
        parts.append(np.logspace(np.log10(lo), np.log10(f[0]), n + 1)[:-1])
    if hi > f[-1] * (1 + 1e-9):
        n = max(int(np.ceil(per_decade * np.log10(hi / f[-1]))), 1)
        parts.append(np.logspace(np.log10(f[-1]), np.log10(hi), n + 1)[1:])
    return np.unique(np.concatenate(parts))


def _db(y) -> np.ndarray:
    return 20.0 * np.log10(np.abs(np.atleast_1d(np.asarray(y))) + 1e-300)


def _influence(g, envelope, names, values, todo, J, bounds, off, joint=()) -> dict:
    """For each name in `todo`: the largest envelope change (dB, max over points) among the
    moves the data cannot rule out.  See the module docstring.

    `bounds` maps a name to `(lo, hi)` (either may be None) -- the fitter's own box, so a move
    never leaves the space the fitter could have returned.  `off` names the parameters whose
    zero is a legal "switched off" value (a gain, an amplitude); for those, zero is one more
    move to try.  `joint` names the parameters that also walk the data's softest JOINT
    direction -- the poorly determined ones, whose looseness is a trade-off with neighbours
    that a move of the parameter alone would underestimate.  Nobody else walks it: for a
    parameter the data does not see at all, column k of the pseudo-inverse is round-off, and
    for a well-pinned one the joint walk mostly moves its LOOSE neighbours, which would pin
    their influence on the wrong name.
    """
    off = set(off or ())
    joint = set(joint or ())
    values = np.asarray(values, float)
    base_d = _db(g(values))
    base_e = _db(envelope(values))
    bounds = dict(bounds or {})
    # the data's softest joint direction per parameter: column k of (J^T J)^+, truncated like
    # sigma, in the same log-parameter coordinates as J
    try:
        _, s, Vt = np.linalg.svd(J, full_matrices=False)
        keep = s > s[0] * RANKDEF_REL if s.size else np.zeros(0, bool)
        cov = (Vt[keep].T / s[keep] ** 2) @ Vt[keep]
    except (np.linalg.LinAlgError, IndexError, ValueError):
        cov = None

    def dev(p):
        with np.errstate(all="ignore"):
            d = float(np.max(np.abs(_db(g(p)) - base_d)))
            e = float(np.max(np.abs(_db(envelope(p)) - base_e)))
        return (d if np.isfinite(d) else np.inf), (e if np.isfinite(e) else np.inf)

    boxed = [(i, lo, hi) for i, n in enumerate(names)
             for lo, hi in [bounds.get(n, (None, None))] if lo is not None or hi is not None]

    def clip(p):
        for i, lo, hi in boxed:
            if lo is not None:
                p[i] = max(p[i], float(lo))
            if hi is not None:
                p[i] = min(p[i], float(hi))
        return p

    tmax = float(np.log(MOVE_MAX))
    out: dict = {}
    for name in todo:
        k = names.index(name)
        if values[k] == 0.0 or not np.isfinite(values[k]):
            continue                      # an off coefficient: grades' `_inert` owns that case
        dirs = [np.eye(len(values))[k]]
        if cov is not None and len(values) > 1 and name in joint:
            d = cov[:, k]
            if np.isfinite(d).all() and d[k] > 0:
                d = d / d[k]
                if float(np.max(np.abs(np.delete(d, k)))) > 1e-6:
                    dirs.append(d)              # the move is shared with other parameters
        worst = 0.0
        for d in dirs:
            # `reach` scales the walk so that NO parameter's log-move exceeds ln(MOVE_MAX)
            reach = max(float(np.max(np.abs(d))), 1.0)
            for sign in (1.0, -1.0):
                t, last = _T0 / reach, None
                while t * reach <= tmax * 1.0001:
                    p = clip(values * np.exp(sign * t * d))
                    if last is not None and np.array_equal(p, last):
                        break                           # pinned against a bound
                    last = p
                    dd, de = dev(p)
                    if dd > DATA_TOL_DB:
                        break                           # the data rules this move out
                    worst = max(worst, de)
                    t *= _GROW
        # "off" is a move too: a gain or an amplitude the data would let go to zero
        if name in off:
            p = values.copy()
            p[k] = 0.0
            dd, de = dev(p)
            if dd <= DATA_TOL_DB:
                worst = max(worst, de)
        out[name] = float(worst)
    return out


def _jac(g, params, delta: float = 1e-4) -> np.ndarray:
    """The relative Jacobian matrix itself (rows: real then imaginary samples)."""
    params = np.asarray(params, float)
    g0 = np.atleast_1d(np.asarray(g(params)))
    cols = []
    for k in range(len(params)):
        pp = params.copy()
        pp[k] = params[k] * (1.0 + delta)
        with np.errstate(divide="ignore", invalid="ignore"):
            dl = np.log(np.atleast_1d(np.asarray(g(pp))) / g0) / delta
        dl = np.where(np.isfinite(dl), dl, 0.0)
        cols.append(np.concatenate([np.real(dl), np.imag(dl)]))
    if not cols:
        return np.zeros((0, 0))
    return np.column_stack(cols)


def jacobian(g, params, delta: float = 1e-4):
    """Relative (log-sensitivity) Jacobian of a complex transfer `g(params)`.

    Returns `(colnorm, singular_values, cond)`.
    """
    J = _jac(g, params, delta)
    if J.size == 0:
        return np.zeros(0), np.zeros(1), float("inf")
    colnorm = np.linalg.norm(J, axis=0)
    try:
        sv = np.linalg.svd(J, compute_uv=False)
    except np.linalg.LinAlgError:
        return colnorm, np.zeros(len(params)), float("inf")
    smin, smax = float(sv[-1]), float(sv[0])
    cond = (smax / smin) if smin > 0 else float("inf")
    return colnorm, sv, cond


def gate(g, names, values, delta: float = 1e-4, *, envelope=None, bounds=None,
         off=()) -> dict:
    """The block-level gate: `{"cond", "sigma": {name: float}, "unidentifiable": [...]}`.

    With `envelope` (the same model evaluated over the envelope grid, see `envelope_grid`) the
    result also carries `influence_db`: for every flagged parameter -- and, when the envelope
    reaches past the data, for EVERY parameter -- how far the prediction can move anywhere in
    the envelope under the moves the data cannot rule out (module docstring).  Without it,
    `influence_db` is absent and every flag counts -- the conservative reading.

    `sigma[name]` is the parameter-space uncertainty SCALE along that parameter, read off a
    TRUNCATED pseudo-inverse of the same log-sensitivity Jacobian:
    `sigma_k = sqrt(sum_{s_j > cutoff} (V[j,k]/s_j)^2)`.  It is unit-free (a relative parameter
    error per unit of relative residual) and it grows exactly where the data is uninformative.
    The truncation matters: with a rank-deficient Jacobian the untruncated sum is infinite for
    nearly EVERY parameter, because the null direction has a component on most of them -- which
    would report the whole block as undetermined because of one dead knob.

    `unidentifiable` uses the PORTED rule, and only that rule: a relative column norm below
    `UNIDENT_REL` means the data does not see that parameter at all.  That is what flags a
    damping resistor pushed to infinity, an inert section coefficient, and the documented case
    of an output capacitor made near-invisible by a large ESR.
    """
    names = list(names)
    values = [float(v) for v in values]
    if not names:
        return {"cond": float("nan"), "sigma": {}, "unidentifiable": []}
    J = _jac(g, values, delta=delta)
    if J.size == 0:
        return {"cond": float("inf"), "sigma": {n: float("inf") for n in names},
                "unidentifiable": list(names), "colnorm_rel": {n: 0.0 for n in names},
                "rank_deficient": True}
    colnorm = np.linalg.norm(J, axis=0)
    top = float(np.max(colnorm))
    rel = {n: (float(colnorm[i] / top) if top > 0 else 0.0) for i, n in enumerate(names)}
    try:
        _, s, Vt = np.linalg.svd(J, full_matrices=False)
    except np.linalg.LinAlgError:
        return {"cond": float("inf"), "sigma": {n: float("inf") for n in names},
                "unidentifiable": list(names), "colnorm_rel": rel, "rank_deficient": True}
    smin, smax = float(s[-1]), float(s[0])
    cond = (smax / smin) if smin > 0 else float("inf")
    cutoff = smax * RANKDEF_REL
    sigma: dict[str, float] = {}
    for i, n in enumerate(names):
        acc = 0.0
        for j in range(len(s)):
            if s[j] > cutoff:
                acc += (Vt[j, i] / s[j]) ** 2
        sigma[n] = float(np.sqrt(acc))
    unident = [n for n in names if rel[n] < UNIDENT_REL]
    # ... and the softer half of the same question: a parameter the data DOES see, but pins far
    # more loosely than everything else around it.  That is the documented near-invisible
    # output capacitor: it moves the curve a little, so the column norm does not vanish, yet a
    # fraction of a dB of mismatch moves it by tens of percent.  Naming it is the whole point;
    # a joint least-squares that pretends to pin it is REJECTED (it diverges).
    live = [sigma[n] for n in names if n not in unident and np.isfinite(sigma[n])]
    med = float(np.median(live)) if live else 0.0
    poorly = [n for n in names
              if n not in unident and med > 0 and sigma[n] > POORLY_REL * med]
    out = {"cond": float(cond), "sigma": sigma, "unidentifiable": unident,
           "poorly_determined": poorly, "colnorm_rel": rel,
           "rank_deficient": bool(smin < RANKDEF_REL * smax)}
    if envelope is not None:
        # Where the envelope reaches past the data (`envelope_grid` then has MORE points than
        # the data grid), even a parameter the column/sigma tests call pinned can steer the
        # prediction out there -- a flicker corner below the first measured point is seen at
        # the 1 % level in band and decides the answer a decade lower.  So every parameter is
        # measured then, not only the flagged ones.  Where it does not reach past the data the
        # unflagged ones are skipped: their influence is bounded by DATA_TOL_DB by construction.
        n_d = np.atleast_1d(np.asarray(g(values))).size
        n_e = np.atleast_1d(np.asarray(envelope(values))).size
        todo = list(unident + poorly)
        out["envelope_beyond_data"] = bool(n_e > n_d)
        if n_e > n_d:
            todo += [n for n in names if n not in todo]
        if todo:
            out["influence_db"] = _influence(g, envelope, names, values, todo, J,
                                             bounds, off, joint=poorly)
            out["data_tol_db"] = DATA_TOL_DB
    return out


def describe(gate_result: dict) -> list:
    """The gate's verdict as plain lines for `BlockFit.notes`.  Reported, never fatal."""
    out: list = []
    bad = list(gate_result.get("unidentifiable") or [])
    soft = list(gate_result.get("poorly_determined") or [])
    if bad:
        out.append("identifiability: the data does not determine " + ", ".join(bad)
                   + " -- reported, not fatal; a section that is switched off, a damping "
                     "resistor pushed to infinity and a near-invisible output capacitor all "
                     "read this way, and all three are the honest answer")
    if soft:
        out.append("identifiability: " + ", ".join(soft) + " is pinned far more loosely than "
                   "the rest of this block (sigma more than "
                   f"{POORLY_REL:g}x the typical) -- a wide number, not a tight one")
    infl = gate_result.get("influence_db") or {}
    if infl:
        tol = float(gate_result.get("data_tol_db", DATA_TOL_DB))
        out.append("influence over the envelope (the most any move the data cannot rule out "
                   f"-- no data point shifted by more than {tol:g} dB -- changes the "
                   "prediction anywhere the model is used): "
                   + ", ".join(f"{n} {float(v):.3g} dB" for n, v in infl.items()))
    return out
