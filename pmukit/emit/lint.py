"""The numeric conditioning gate -- the automated form of the singular-Jacobian scar.

The failure this exists for: a behavioral model that is perfect in AC and converges standalone
can still make a COUPLED harmonic-balance run singular, and the singular column surfaces at the
package or at neighbouring transistors, never at the model.  Two mechanisms produced it:

  * an element whose branch admittance UNDERFLOWS against O(1) terms at a high harmonic -- the
    synthesized 8890 H PSRR inductor, ~1e-16 S at 77 GHz -> "matrix singular during
    decomposition".  Rescaling the L/C split only MOVES the extreme: 8.89 uF gives wC = 4.3e6 S
    and a NaN instead.
  * a controlled source with a LARGE coefficient in a SHARED node -- the +-17.2 S near-cancelling
    PSRR doublet, a near-null-space direction in the supply-node Jacobian.

So the gate measures, at the model's `f_max` and at a configurable harmonic multiple of it:

  1. `node_range` -- the dynamic range of the TOTAL branch admittance of every INTERNAL node.
     This is the MNA diagonal, and it is the metric that discriminates: the gm-C realization puts
     every synthesized node at ~w*C_NOM (uniform), while the R-L-C twin leaves one node at 1e-16 S
     next to another at 1e0 S.  A single element's admittance floor is NOT a defect on its own --
     a fitted 20 uH branch inductance really is 2 MOhm at 20 GHz -- which is why the primary gate
     is per node, not per element.  Port nodes are excluded: the consumer's circuit supplies their
     admittance, not this model.
  2. `element_extreme` -- any single branch admittance below `y_floor` or above `y_ceil`, which
     catches the planted inductor directly and names it.
  3. `controlled_gain` -- any |gm| at or above `gm_max`, the doublet rule.
  4. `dc_path` -- a node whose only branches are capacitors, which leaves the DC solve nothing to
     pin it with.

`report()` returns a structured dict; `deliver()` puts it in report.md and refuses to mark the
model HB-ready when `ok` is False.
"""
from __future__ import annotations

import math

from .primitives import GM_SOFT, WHITELIST

__all__ = ["report", "render", "RANGE_MAX", "Y_FLOOR", "Y_CEIL", "HARMONIC"]

#: Max allowed dynamic range of the per-node total admittance, at each evaluation frequency.
RANGE_MAX = 1.0e6
#: A branch admittance below this underflows against O(1) terms in the same matrix.
Y_FLOOR = 1.0e-12
#: ... and one above this is the other end of the same scar (wC = 4.3e6 S -> NaN).
Y_CEIL = 1.0e6
#: Default harmonic multiple: the real failure was pinned at harm 16 of a ~4.8 GHz oscillator.
HARMONIC = 16

#: Which whitelist entry an offending element is straining.
_STRAIN = {
    ("inductor", "low"): "inductor",
    ("inductor", "high"): "inductor",
    ("capacitor", "high"): "gm_c_biquad",
    ("capacitor", "low"): "capacitor",
    ("resistor", "low"): "resistor",
    ("resistor", "high"): "resistor",
    ("conductance", "low"): "gleak",
    ("conductance", "high"): "conductance",
    ("vccs", "low"): "vccs",
    ("vccs", "high"): "gm_c_biquad",
}


def _fmt(x: float) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x:.4g}"


def _node_totals(elements, skip: set[str]):
    """{node: (total |Y|, [contributing element names])} over PASSIVE branches only."""
    totals: dict[str, float] = {}
    who: dict[str, list[tuple[str, float]]] = {}
    for el, y in elements:
        if el.controlled or el.stiff or el.kind == "noise":
            continue
        if not math.isfinite(y):
            continue
        for n in el.nodes[:2]:
            if n in skip:
                continue
            totals[n] = totals.get(n, 0.0) + y
            who.setdefault(n, []).append((el.name, y))
    return totals, who


def report(netlist, *, f_max_hz: float, harmonic: int = HARMONIC, range_max: float = RANGE_MAX,
           y_floor: float = Y_FLOOR, y_ceil: float = Y_CEIL, gm_max: float = GM_SOFT,
           grounds=(), label: str = "") -> dict:
    """Measure one emitted module.  `netlist` is the `Netlist` the emitter built.

    Returns `{"ok", "status", "findings", "nodes", "frequencies", "summary", ...}`.  `ok` is False
    as soon as one rule fires; `deliver()` then refuses to call the model HB-ready.
    """
    f_max = float(f_max_hz)
    freqs = [f_max, f_max * float(harmonic)]
    skip = set(grounds) | set(netlist.ports) | set(netlist.pinned_nodes)
    findings: list[dict] = []
    per_f: list[dict] = []

    for f in freqs:
        pairs = [(el, el.admittance(f)) for el in netlist.elements]
        totals, who = _node_totals(pairs, skip)
        row = {"f_hz": f, "n_nodes": len(totals)}
        if totals:
            lo_node = min(totals, key=lambda n: totals[n])
            hi_node = max(totals, key=lambda n: totals[n])
            lo, hi = totals[lo_node], totals[hi_node]
            rng = (hi / lo) if lo > 0 else float("inf")
            row.update({"range": rng, "min_node": lo_node, "min_y": lo,
                        "max_node": hi_node, "max_y": hi})
            if rng > range_max:
                findings.append({
                    "rule": "node_range", "severity": "fail", "f_hz": f, "value": rng,
                    "node": lo_node,
                    "what": (f"internal node admittance spans {_fmt(rng)} at {_fmt(f)} Hz "
                             f"(limit {_fmt(range_max)}): {lo_node} = {_fmt(lo)} S against "
                             f"{hi_node} = {_fmt(hi)} S"),
                    "elements": [f"{n} ({_fmt(y)} S)"
                                 for n, y in sorted(who.get(lo_node, []), key=lambda t: t[1])[:4]],
                    "strains": "gm_c_biquad",
                    "why": WHITELIST["gm_c_biquad"][1],
                })
        per_f.append(row)

        for el, y in pairs:
            if el.controlled:
                if abs(el.value) >= gm_max:
                    findings.append({
                        "rule": "controlled_gain", "severity": "fail", "f_hz": f,
                        "value": abs(el.value), "node": el.nodes[0], "element": el.name,
                        "what": (f"controlled source {el.name} has |gm| = "
                                 f"{_fmt(abs(el.value))} S, at or above {_fmt(gm_max)} S"),
                        "elements": [f"{el.name} = {_fmt(el.value)} S"],
                        "strains": "gm_c_biquad",
                        "why": WHITELIST["gm_c_biquad"][1],
                    })
                continue
            if el.stiff or el.kind == "noise" or not math.isfinite(y):
                continue
            side = "low" if y < y_floor else ("high" if y > y_ceil else "")
            if not side:
                continue
            strains = _STRAIN.get((el.kind, side), "vccs")
            synth = "SYNTHESIZED" in (el.detail or "")
            findings.append({
                "rule": "element_extreme", "severity": "fail" if synth else "warn", "f_hz": f,
                "value": y, "element": el.name, "node": el.nodes[0],
                "what": (f"{el.kind} {el.name} = {_fmt(el.value)} has |Y| = {_fmt(y)} S at "
                         f"{_fmt(f)} Hz, {'below' if side == 'low' else 'above'} the "
                         f"{_fmt(y_floor if side == 'low' else y_ceil)} S limit"
                         + (" -- SYNTHESIZED" if synth else "")),
                "elements": [f"{el.name} = {_fmt(el.value)}"],
                "strains": strains,
                "why": WHITELIST[strains][1],
            })

    # -- DC path: a node whose only branches are capacitors ---------------------------
    cap_only: dict[str, bool] = {}
    for el in netlist.elements:
        if el.controlled or el.stiff or el.kind == "noise":
            continue
        for n in el.nodes[:2]:
            if n in skip:
                continue
            cap_only[n] = cap_only.get(n, True) and (el.kind == "capacitor")
    for n, only in sorted(cap_only.items()):
        if only:
            findings.append({
                "rule": "dc_path", "severity": "fail", "f_hz": 0.0, "value": 0.0, "node": n,
                "what": f"node {n} carries only capacitors, so the DC solve has nothing to pin it",
                "elements": [], "strains": "gleak", "why": WHITELIST["gleak"][1],
            })

    # de-duplicate: the same element fires at both frequencies
    seen, unique = set(), []
    for fdg in findings:
        key = (fdg["rule"], fdg.get("element"), fdg.get("node"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(fdg)

    fails = [f for f in unique if f["severity"] == "fail"]
    warns = [f for f in unique if f["severity"] == "warn"]
    ranges = [r.get("range") for r in per_f if r.get("range") is not None]
    return {
        "ok": not fails,
        "status": "pass" if not fails else "fail",
        "label": label,
        "f_max_hz": f_max, "harmonic": int(harmonic), "frequencies": freqs,
        "range_max": range_max, "y_floor": y_floor, "y_ceil": y_ceil, "gm_max": gm_max,
        "worst_node_range": max(ranges) if ranges else None,
        "per_frequency": per_f,
        "n_elements": len(netlist.elements), "n_internal_nodes": len(netlist.internal_nodes),
        "findings": unique, "n_fail": len(fails), "n_warn": len(warns),
        "summary": _summary(label, fails, warns, ranges, range_max, f_max, harmonic),
    }


def _summary(label, fails, warns, ranges, range_max, f_max, harmonic) -> str:
    where = f"{label}: " if label else ""
    rng = max(ranges) if ranges else float("nan")
    head = (f"{where}worst internal node admittance range {_fmt(rng)} "
            f"(limit {_fmt(range_max)}) at {_fmt(f_max)} Hz and harmonic {harmonic}")
    if not fails and not warns:
        return head + " -- PASS, the model is numerically well conditioned for HB."
    if not fails:
        return head + f" -- PASS with {len(warns)} warning(s)."
    return head + f" -- FAIL: {len(fails)} finding(s); {fails[0]['what']}."


def render(rep: dict) -> str:
    """The lint report as plain text, for report.md and for the CLI."""
    out = [rep["summary"], ""]
    out.append(f"  elements {rep['n_elements']}, internal nodes {rep['n_internal_nodes']}, "
               f"evaluated at {', '.join(_fmt(f) + ' Hz' for f in rep['frequencies'])}")
    for row in rep["per_frequency"]:
        if row.get("range") is None:
            continue
        out.append(f"  at {_fmt(row['f_hz'])} Hz: range {_fmt(row['range'])} "
                   f"({row['min_node']} {_fmt(row['min_y'])} S .. "
                   f"{row['max_node']} {_fmt(row['max_y'])} S)")
    if not rep["findings"]:
        out.append("  no finding.")
        return "\n".join(out)
    out.append("")
    for f in rep["findings"]:
        out.append(f"  [{f['severity'].upper()}] {f['rule']}: {f['what']}")
        for e in f.get("elements", []):
            out.append(f"      element: {e}")
        out.append(f"      strains whitelist rule '{f['strains']}' -- {f['why']}")
    return "\n".join(out)
