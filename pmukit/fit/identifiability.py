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
"""
from __future__ import annotations

import numpy as np

__all__ = ["jacobian", "gate", "describe", "UNIDENT_REL", "RANKDEF_REL", "POORLY_REL"]

#: column-norm / max column-norm below this => the data does not see this parameter
UNIDENT_REL = 1e-3
#: sigma_min / sigma_max below this => rank-deficient (cond ~ inf)
RANKDEF_REL = 1e-9
#: sigma this many times the block's typical sigma => the data sees the parameter but pins it
#: far more loosely than the rest -- a wide number reported as if it were tight
POORLY_REL = 20.0


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


def gate(g, names, values, delta: float = 1e-4) -> dict:
    """The block-level gate: `{"cond", "sigma": {name: float}, "unidentifiable": [...]}`.

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
    return {"cond": float(cond), "sigma": sigma, "unidentifiable": unident,
            "poorly_determined": poorly, "colnorm_rel": rel,
            "rank_deficient": bool(smin < RANKDEF_REL * smax)}


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
    return out
