"""Per corner, per port, per block: green / yellow / red / not_run.

This module answers the only question contract 0c says the user actually asks -- *can I trust
this model in my simulation?* -- and it answers it per rail and per corner, in words, with the
numbers kept out of the rail table and parked in `grades.json`.

Four rules shape it, and each one is a scar:

1. **A block whose data was NEVER RUN is `not_run`, never `red`.**  The dataset can tell the
   two apart (`Dataset.coverage()` separates *filled* / *missing, with a reason* / *never_run*)
   and the fitter carries it through as `BlockFit.missing`.  "I measured this and it is wrong"
   and "nobody measured this" are different sentences to the user: one says fix the model, the
   other says run the sweep.

2. **A block flagged by `identifiability` is capped at `yellow` however good its residual is.**
   A tight fit to a parameter the data cannot determine is the classic false green: the curve
   goes through the points and the number underneath it is arbitrary.  The cap has one
   documented exception, `_inert()` below -- a parameter that is switched OFF carries no
   transfer, so it cannot make anything falsely green, and flagging it would paint every block
   yellow and teach the user to ignore the colour.

3. **A roll-up is `max()` over blocks, and over the cells inside a block.**  Which means
   ADDING COVERAGE CAN ONLY LOWER A ROLL-UP.  A grade that got worse after more corners, more
   loads or more temperatures were characterized is NOT a regression -- it is the same model
   being asked a harder question.  (METHODOLOGY, "Load dependence": the rail grade is max over
   corners; the real part's light-load edge is exactly this effect.)

4. **Thresholds live in ONE table (`LIMITS`), per metric, with the reasoning attached.**  A
   spectrum in dB is not judged like a DC table in %, and a large-signal droop is not judged
   like either.  `explain_limits()` prints the table, and it is what goes into the report.

Nothing here runs a simulator; grading is a pure function of `FitResult` (+ optionally the
dataset's coverage and the plan's consequences, which is where *never run* comes from).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .. import spec
from ..deliverable import GRADES, Grade

__all__ = ["Limit", "LIMITS", "DEFAULT_DB", "DEFAULT_PCT", "limit_for", "explain_limits",
           "band_for", "grade_block", "grade_project", "rollup", "rollup_table",
           "worst", "GRADES", "flagged_parameters", "partial_variables", "never_run_lines",
           "headline", "default_off", "switch_for", "off_note", "block_verdict",
           "short_reason", "is_held", "HELD_MARK"]


# --------------------------------------------------------------------------- the table
@dataclass(frozen=True)
class Limit:
    """One row of the threshold table: `green` and `yellow` are UPPER bounds on the score."""

    green: float
    yellow: float
    unit: str
    why: str

    def band(self, score: float) -> str:
        if score != score:                       # NaN
            return "yellow"
        if score <= self.green:
            return "green"
        if score <= self.yellow:
            return "yellow"
        return "red"


#: THE threshold table.  Keyed by `BlockFit.metric`, which is the fitter's own name for what
#: its score measures -- so this really is one row PER METRIC, not per block, and two blocks
#: that measure the same thing the same way share a row.
#:
#: Every number below is an upper bound on the block's residual, and every one of them carries
#: the reason it sits where it sits.  Where the private LDO_modeling repo has a precedent, the
#: precedent is cited: it is the only out-of-sample evidence this tool has.
LIMITS: dict[str, Limit] = {
    # ---- the spectral kernel: dB RMS of 20*log10(|model|/|gt|) --------------------------
    "|Zout| dB RMS": Limit(
        green=1.0, yellow=3.0, unit="dB",
        why="Zout is the shared kernel: PSRR, the output noise, the spurs and the load-step "
            "replay are all THIS impedance driven by different sources, so a Zout error is "
            "common-mode across all four observables and gets the tightest spectral limit. "
            "1 dB is 12 % in impedance -- below the corner-to-corner spread of the rail "
            "itself, so a green model is inside the part's own variability. 3 dB is a factor "
            "of 1.4, where a predicted droop or spur amplitude stops being the same number. "
            "Precedent: the real part shipped at composite 1.81 dB and its desk replica at "
            "2.30 dB, both accepted; the synthetic variants deliberately built to break a "
            "gate sat at 4.2 / 5.6 / 57 dB."),
    "PSRR dB RMS": Limit(
        green=1.0, yellow=3.0, unit="dB",
        why="Same scale as Zout, and for the same reason: PSRR is identified as a coupling "
            "current carried to the pin BY Zout, so the two share a unit and an error budget. "
            "A supply-rejection number a consumer puts in a spur budget is useful to about a "
            "dB and useless past three."),
    "|gdd| dB RMS": Limit(
        green=1.0, yellow=3.0, unit="dB",
        why="The supply-to-bias-current transfer: the same physical quantity as a rail PSRR, "
            "read at a current output instead of a voltage one, so it shares the rail's row."),
    "|Y| dB RMS": Limit(
        green=1.0, yellow=3.0, unit="dB",
        why="The bias pin's output admittance is the current-output twin of Zout and loads "
            "the consumer's node the same way; same unit, same budget."),

    # ---- noise: deliberately looser, and the reason is written down --------------------
    "Sv dB RMS": Limit(
        green=2.0, yellow=6.0, unit="dB",
        why="Looser ON PURPOSE. spec.py records a SUSPENDED, unmeasured error source of "
            "roughly 3 dB (bandgap noise reaches the rails and the biases together; this "
            "model treats them as independent). A gate tighter than a known systematic nobody "
            "has measured yet would be theatre. 2 dB is within the spread of a noise density "
            "between corners; 6 dB is a factor of 2 in density -- 4x in power -- past which a "
            "phase-noise budget built on it is wrong by a design margin. Precedent: the "
            "decoupled-Norton noise block was accepted with every rail at or under 3.6 dB."),
    "In dB RMS": Limit(
        green=2.0, yellow=6.0, unit="dB",
        why="The bias reference's output current noise: same estimator, same correlation "
            "caveat, same row as the rail's voltage noise."),

    # ---- DC: a table, not a spectrum ----------------------------------------------------
    "vout % RMS": Limit(
        green=0.5, yellow=2.0, unit="%",
        why="A DC table is not a spectrum -- the consumer reads the rail voltage as a NUMBER "
            "and builds headroom on it. 0.5 % of a sub-volt rail is a few millivolts, which "
            "is the load and line regulation of a real LDO, so a green model is inside the "
            "part's own spec. 2 % is tens of millivolts: the point where a Vth or headroom "
            "budget taken from the model is wrong."),
    "I-V % of plateau RMS": Limit(
        green=1.0, yellow=3.0, unit="%",
        why="A current reference is specified in percent, and 1 % is a good reference. Wider "
            "than the rail row because the score is taken across the whole compliance sweep, "
            "including the knee, not just on the plateau. 3 % is where a PTAT-derived tuning "
            "current moves a VCO band enough to matter."),

    # ---- large signal: the loosest row, and it says why --------------------------------
    "load-step droop % error": Limit(
        green=10.0, yellow=25.0, unit="%",
        why="Large-signal, and deliberately the loosest row: METHODOLOGY frames the non-linear "
            "layer as a SAFETY NET that has to BOUND the excursion, not reproduce it. Its own "
            "accepted result was a held-out droop error near 4 % with full-waveform RMS of "
            "10-16 mV on a ~200 mV dip -- about 6 %. 10 % keeps that green. Past 25 % the "
            "model is not describing the same event, and a consumer sizing a decap from it "
            "would size it wrong. This block is tier `ls`: it is emitted OFF by default and "
            "only defaults on after the HB health check, so a red here does not ship."),
    "EN ramp % RMS": Limit(
        green=15.0, yellow=40.0, unit="%",
        why="The `en` tier is 'usable, not signed off' BY CONTRACT -- it only promises that a "
            "consumer's bench toggling EN does not blow up; startup is signed off on the real "
            "LDO, never here. Grading it tightly would imply a sign-off the contract "
            "explicitly withholds. Within 15 % it is a faithful-looking ramp; past 40 % it is "
            "not the same ramp and the note has to say so."),
}

#: Fallback when a fitter grows a metric this table has not met yet.  It is keyed on the UNIT
#: in the metric text, because that is the one thing a new metric cannot lie about -- and the
#: fallback is announced in the grade's detail, never applied silently.
DEFAULT_DB = Limit(green=1.0, yellow=3.0, unit="dB",
                   why="fallback for an unlisted dB metric: the spectral row.")
DEFAULT_PCT = Limit(green=1.0, yellow=3.0, unit="%",
                    why="fallback for an unlisted % metric: the percent row.")


def limit_for(metric: str) -> tuple[Limit | None, bool]:
    """`(limit, exact?)` for one metric string.  `exact=False` means a unit fallback was used
    and the grade must say so; `(None, False)` means the metric carries no unit at all."""
    m = str(metric or "")
    if m in LIMITS:
        return LIMITS[m], True
    low = m.lower()
    if "db" in low:
        return DEFAULT_DB, False
    if "%" in low or "percent" in low:
        return DEFAULT_PCT, False
    return None, False


def band_for(metric: str, score: float) -> str:
    """The colour one score earns from the table alone, before any cap."""
    lim, _exact = limit_for(metric)
    if lim is None:
        return "yellow"
    return lim.band(float(score))


def explain_limits() -> str:
    """The threshold table as plain text -- the same words in the CLI, the report and the web
    shell, so there is exactly one place the limits are written down."""
    out = ["Acceptance limits, one row per metric.",
           "A score at or below `green` is green; at or below `yellow` is yellow; above it is "
           "red.", ""]
    for metric in sorted(LIMITS):
        lim = LIMITS[metric]
        out.append(f"{metric}   green <= {lim.green:g} {lim.unit}   "
                   f"yellow <= {lim.yellow:g} {lim.unit}")
        out.append(f"    {lim.why}")
        out.append("")
    out += ["Two rules sit on top of the table:",
            "  * a block whose measurement never ran is `not_run`, never `red`;",
            "  * a block the identifiability gate flags is capped at `yellow` however good its "
            "residual is, because a tight fit to an undetermined parameter is a false green.",
            "",
            "And one consequence, recorded so nobody reads it as a regression:",
            "  * a roll-up is max() over the blocks and cells it covers, so ADDING COVERAGE "
            "CAN ONLY LOWER IT.",
            "    A worse grade on richer coverage is the same model answering a harder "
            "question, not a step backwards."]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- identifiability
#: A gain and the numbers that only exist to shape it.  When the gain is off, its companions
#: are inert too: a pole frequency of a section with zero gain is not in the emitted transfer.
_COMPANIONS = {
    "G_i": ("pole_i_hz",),              # PSRR real-pole bank: gain[k] shapes pole[k]
    "amp_i": ("corner_i_hz",),          # noise Lorentzian bank
    "pc_gain": ("pc_w0", "pc_q", "pc_zero"),   # the signed complex PSRR section
}
#: A series branch the fitter switches off by pushing its resistance to a sentinel.
_OFF_OHM = 1.0e8
#: ... and the inductance it leaves behind when it does.
_OFF_HENRY = 1.0e-12
#: A bank section this far below the loudest section of the same bank contributes under
#: 0.01 dB to the total, so the data not seeing it is arithmetic, not ignorance.
_QUIET_REL = 1.0e-3


def _value(params: dict, name: str):
    """Resolve `G_i[2]` against `params['G_i'][2]`; plain names resolve directly."""
    if name.endswith("]") and "[" in name:
        base, _, idx = name[:-1].partition("[")
        seq = params.get(base)
        try:
            return list(seq)[int(idx)]
        except (TypeError, ValueError, IndexError):
            return None
    return params.get(name)


def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _inert(name: str, params: dict) -> bool:
    """Is this flagged parameter switched OFF in the emitted model?

    An off parameter carries no transfer, so it cannot make a fit falsely green -- and the
    fitter's own gate fires on every one of them (an unused PSRR section, a branch-B resistor
    at its infinity sentinel, a Lorentzian the bank did not need).  Counting those as evidence
    of a false green would paint every block yellow and teach the user to ignore the colour.

    Four ways to be off, and nothing else counts:
      1. the value is exactly zero;
      2. it is a series-branch resistance at or above the fitter's OFF sentinel, or the
         inductance that branch leaves behind;
      3. it is a bank section whose own gain is zero, or more than 60 dB below the loudest
         section of the same bank;
      4. it is a companion (a pole frequency, a Q) of a gain that is itself off.
    """
    base = name.partition("[")[0]
    val = _value(params, name)
    v = _num(val)

    if v == 0.0:
        return True
    if base in ("Rb", "Rpl_b") and v >= _OFF_OHM:
        return True
    if base in ("Lb",) and 0.0 < v <= _OFF_HENRY:
        return True

    # a bank section, judged against the loudest section of its own bank
    for gain_name, companions in _COMPANIONS.items():
        if base == gain_name or base in companions:
            idx = name.partition("[")[2].rstrip("]") if "[" in name else ""
            gain_key = f"{gain_name}[{idx}]" if idx else gain_name
            g = _num(_value(params, gain_key))
            if g == 0.0:
                return True
            seq = params.get(gain_name)
            if isinstance(seq, (list, tuple)) and seq:
                loudest = max((abs(_num(x)) for x in seq), default=0.0)
                if loudest > 0.0 and abs(g) <= _QUIET_REL * loudest:
                    return True
            return False
    return False


def flagged_parameters(bf) -> list[str]:
    """The identifiability flags that actually matter for THIS block, inert ones removed."""
    ident = dict(getattr(bf, "identifiability", None) or {})
    params = dict(getattr(bf, "params", None) or {})
    names: list[str] = []
    for key in ("unidentifiable", "poorly_determined"):
        for n in ident.get(key) or []:
            n = str(n)
            if n not in names and not _inert(n, params):
                names.append(n)
    return names


# --------------------------------------------------------------------------- one block
_TIER_NOTE = {
    "ls": " This is a large-signal term: it ships switched off unless the HB health check "
          "clears it.",
    "en": " The enable ramp is usable but not signed off -- sign startup off on the real part.",
}


def grade_block(bf, *, port_type: str = "rail") -> tuple[str, str]:
    """`(grade, detail)` for one `BlockFit`.  `detail` is PLAIN LANGUAGE AND CARRIES NO NUMBER:
    contract 0c forbids internal scores in report.md's rail table, and this string is what
    lands in that cell."""
    try:
        tier = spec.tier_of(bf.block, port_type or "rail")
    except Exception:                                  # noqa: BLE001 -- the tier is decoration
        tier = ""
    extra = _TIER_NOTE.get(tier, "")

    if getattr(bf, "missing", False):
        reason = str((getattr(bf, "notes", None) or [""])[0])
        low = reason.lower()
        if "never run" in low:
            head = "the sweep behind it never ran"
        elif "registered missing" in low or "no usable" in low:
            head = "the run behind it ran and produced no usable data"
        else:
            head = "nothing was fitted here"
        why = f" ({_table_safe(reason)})" if _table_safe(reason) else ""
        return "not_run", _join(
            f"{head}{why} -- this is missing coverage, not a bad model", extra)

    params = dict(getattr(bf, "params", None) or {})
    score = _num(getattr(bf, "score", float("nan")))

    if not params and not str(getattr(bf, "metric", "") or ""):
        # An emitter constant (the rail's one-way conduction): the spec gives it no parameter,
        # the plan schedules no run for it, and there is nothing that can drift.
        return "green", _join("a fixed constraint the emitter always writes; there is no "
                              "fitted number here to be wrong", extra)

    lim, exact = limit_for(getattr(bf, "metric", ""))
    if lim is None:
        return "yellow", _join("this block reports a residual in a unit with no acceptance "
                               "limit, so it cannot be signed off automatically -- read the "
                               "number in grades.json", extra)

    if math.isnan(score):
        return "yellow", _join("no residual could be computed for this block, so it cannot be "
                               "called good -- read its note in grades.json", extra)

    grade = lim.band(score)
    if grade == "green":
        detail = "the fit is inside the acceptance limit for this quantity"
    elif grade == "yellow":
        detail = "the fit is past the green limit but still usable -- read the note"
    else:
        detail = "the fit misses this quantity by more than the acceptance limit"
    if not exact:
        detail += " (judged against the generic limit for its unit, not a dedicated one)"

    flags = flagged_parameters(bf)
    if flags and grade == "green":
        grade = "yellow"
        detail = ("the residual is inside the green limit, but the data does not pin "
                  + _and_list(flags) + " -- a tight fit to an undetermined parameter is the "
                  "classic false green, so " + HELD_MARK)
    elif flags:
        detail += "; the data also does not pin " + _and_list(flags)
    return grade, _join(detail, extra)


#: The phrase a held grade's detail always carries.  `is_held()` reads it back, so a grade that
#: travelled through verify.json (which stores only grade + detail) still says it was held.
HELD_MARK = "this is held at yellow"


def is_held(detail) -> bool:
    """Was this grade capped at yellow by the identifiability rule rather than earned by its
    residual?  True only for the detail `grade_block` writes when it holds a green."""
    return HELD_MARK in str(detail or "")


def block_verdict(bf, *, port_type: str = "rail") -> dict:
    """`grade_block` plus what the screen needs to say WHY, in one dict.

    `band` is the colour the residual alone earns; `held` is True when the identifiability rule
    pulled a green residual down to yellow, and `held_by` names the parameters it held on.
    `reason` is a short line (no number) for an inline cell; `detail` is the full sentence.
    """
    grade, detail = grade_block(bf, port_type=port_type)
    flags = flagged_parameters(bf) if not getattr(bf, "missing", False) else []
    held = is_held(detail)
    lim, _exact = limit_for(getattr(bf, "metric", ""))
    score = _num(getattr(bf, "score", float("nan")))
    band = lim.band(score) if (lim is not None and not math.isnan(score)) else grade
    return {"grade": grade, "detail": detail, "band": band, "held": held,
            "held_by": flags if held else [], "flags": flags,
            "reason": short_reason(grade, detail, held_by=flags if held else [])}


def short_reason(grade: str, detail: str, *, held_by=()) -> str:
    """A few words for the inline cell -- the full `detail` sits behind it."""
    if held_by:
        return "held at yellow: the data does not pin " + _and_list(held_by)
    if is_held(detail):
        return "held at yellow: a parameter is not pinned by the data"
    if grade == "green":
        return ""
    if grade == "not_run":
        return "never measured -- missing coverage, not a bad fit"
    d = str(detail or "")
    if "no acceptance limit" in d or "no residual" in d:
        return "cannot be signed off automatically"
    if grade == "yellow":
        return "past the green limit, still usable"
    if grade == "red":
        return "outside the acceptance limit"
    return ""


# --------------------------------------------------------------------------- default-on
def _tier(block: str, port_type=None) -> str:
    """The block's tier; with no port type, any port type that has a block of this name."""
    types = [port_type] if port_type else list(spec.PORT_TYPES)
    for pt in types:
        try:
            return spec.tier_of(str(block), str(pt))
        except Exception:                              # noqa: BLE001 -- not a block of pt
            continue
    return ""


def default_off(block: str, port: str, port_type=None, ls_default_on=()) -> bool:
    """Is this block switched OFF in the model as delivered, unless the consumer turns it on?

    Only the `ls` tier has a switch (`load_en_<rail>`), and it defaults on only for the rails
    that passed the HB health check (`ls_default_on`, which lists rails; a `load_en_<rail>`
    spelling is accepted too).  The `en` tier (the EN ramp) is always off: it is not emitted.
    Every other tier is what the delivered model does by default.
    """
    tier = _tier(block, port_type)
    if tier == "en":
        # The EN ramp is fitted but never emitted: EN is a pass-through pin of the delivered
        # model (contract 4), so no consumer ever meets this block.
        return True
    if tier != "ls":
        return False
    on = {str(x) for x in (ls_default_on or ())}
    return str(port) not in on and f"load_en_{port}" not in on


def switch_for(block: str, port: str) -> str:
    """The instance parameter that turns a default-off block on, e.g. `load_en_VDD0P8_B=1`."""
    return f"load_en_{port}=1" if str(block) == "load_en" else ""


def off_note(block: str, port: str, grade: str) -> str:
    """The one line that names a default-off block without letting it colour the cell."""
    sw = switch_for(block, port)
    how = (f"turn on with {sw} only if you need the load event and accept this grade"
           if sw else "it is not active in the delivered model")
    return (f"off by default: {block} {_WORD.get(grade, grade)} -- not part of this grade; "
            f"{how}")


_WORD = {"green": "OK", "yellow": "MARG", "red": "FAIL", "not_run": "not run",
         "fitted": "fitted"}

#: The rank the headline uses; "fitted" (the web shell's "fitted, not yet judged") sits
#: between green and yellow, exactly as in `pmukit.server`.
_HEAD_RANK = {"green": 0, "fitted": 1, "yellow": 2, "not_run": 3, "red": 4}


def _get(item, key, default=""):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def headline(items, *, port: str, port_type=None, ls_default_on=()) -> dict:
    """THE aggregation of one (port, cell): what the delivered model does BY DEFAULT.

    `items` are grades for the blocks of one port in one cell (a `Grade`, or any dict with
    `block` / `grade` / `detail`).  The headline is the worst of the blocks that are ON in the
    delivered model; a block that ships switched off (an `ls` term that did not pass the HB
    check) is still graded and still reported -- in `off_by_default`, with the switch that
    turns it on -- but it never drags the cell's colour, because a consumer who instantiates
    the model as delivered never meets it.

    Returns `{grade, block, detail, held, held_by, off_by_default: [{block, grade, detail,
    switch, note}]}`.  `held` is True when the headline block's grade was held at yellow by the
    identifiability rule.  The web shell's grid, the cell view and the rail roll-up all call
    this, and `deliverable.render_report` should too, so they cannot disagree.
    """
    on, off = [], []
    for it in items:
        blk = str(_get(it, "block"))
        if default_off(blk, port, port_type, ls_default_on):
            off.append(it)
        else:
            on.append(it)
    worst = None
    for it in on:
        if worst is None or _HEAD_RANK.get(str(_get(it, "grade")), 0) > \
                _HEAD_RANK.get(str(_get(worst, "grade")), 0):
            worst = it
    if worst is None:
        head = {"grade": "not_run", "block": "",
                "detail": "only blocks that are off by default were graded here"}
    else:
        head = {"grade": str(_get(worst, "grade")), "block": str(_get(worst, "block")),
                "detail": str(_get(worst, "detail") or "")}
    held = [str(_get(it, "block")) for it in on
            if str(_get(it, "grade")) == head["grade"] and is_held(_get(it, "detail"))]
    head["held"] = bool(held) and head["grade"] == "yellow"
    head["held_by"] = held if head["held"] else []
    seen, off_rows = set(), []
    for it in sorted(off, key=lambda x: -_HEAD_RANK.get(str(_get(x, "grade")), 0)):
        blk = str(_get(it, "block"))
        if blk in seen:
            continue
        seen.add(blk)
        g = str(_get(it, "grade"))
        off_rows.append({"block": blk, "grade": g, "detail": str(_get(it, "detail") or ""),
                         "switch": switch_for(blk, port), "note": off_note(blk, port, g)})
    head["off_by_default"] = off_rows
    return head


#: Contract 0c: report.md's rail table may carry NO internal score.  `deliverable.py`'s own
#: test enforces it with this pattern, so the same pattern guards every string this module
#: puts in a `detail` -- including a reason quoted back from a fitter's note.
_LOOKS_LIKE_A_SCORE = re.compile(r"\d\.\d{2,}|\d+\.?\d*e[+-]?\d+", re.I)


def _table_safe(text: str) -> str:
    """The text if it can go in the rail table, else "" -- never a truncated half sentence."""
    t = " ".join(str(text or "").split())
    return "" if (not t or _LOOKS_LIKE_A_SCORE.search(t)) else t


def _join(detail: str, extra: str) -> str:
    """One sentence, then the tier note, with the punctuation between them."""
    d = detail.rstrip()
    if extra:
        if not d.endswith("."):
            d += "."
        return d + extra
    return d


def _and_list(names) -> str:
    names = [str(n) for n in names]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# --------------------------------------------------------------------------- coverage
def partial_variables(dataset) -> dict:
    """`{variable: coverage dict}` for every variable with a hole in it.

    Contract 2 separates *filled* from *missing* (ran and broke, with a reason) from
    *never_run*; both holes matter here, and they matter differently -- which is exactly why
    the dataset keeps them apart and why this tool refuses to call either one a bad fit.
    """
    out: dict[str, dict] = {}
    if dataset is None or not hasattr(dataset, "variables"):
        return out
    try:
        names = list(dataset.variables())
    except Exception:                                  # noqa: BLE001 -- a closed dataset
        return out
    for var in names:
        try:
            cov = dataset.coverage(var)
        except Exception:                              # noqa: BLE001
            continue
        if int(cov.get("missing", 0)) or int(cov.get("never_run", 0)):
            out[var] = cov
    return out


def never_run_lines(dataset) -> list[str]:
    """One plain line per variable with a hole -- what `deliver(not_run=...)` prints."""
    lines = []
    for var, cov in sorted(partial_variables(dataset).items()):
        parts = []
        if int(cov.get("never_run", 0)):
            parts.append(f"{int(cov['never_run'])} cell(s) never run")
        if int(cov.get("missing", 0)):
            parts.append(f"{int(cov['missing'])} cell(s) ran and produced no usable data")
        lines.append(f"{var}: {', '.join(parts)} of {int(cov.get('declared', 0))}")
    return lines


def _has_hole(bf, port_type: str, holes: dict) -> bool:
    if not holes:
        return False
    try:
        obs = spec.block(bf.block, port_type or "rail").observables
    except Exception:                                  # noqa: BLE001 -- unknown block
        return False
    return any(f"{o}.{bf.port}" in holes for o in obs)


# --------------------------------------------------------------------------- the project
def _rank(grade: str) -> int:
    # `deliverable._GRADE_RANK` is the same order; re-stated here so this module can be read on
    # its own: red outranks not_run, and neither may be masked by a green sibling.
    return {"green": 0, "yellow": 1, "not_run": 2, "red": 3}[grade]


def _corners_of(fit, derived, corners) -> list[str]:
    if corners:
        return [str(c) for c in corners]
    seen: list[str] = []
    for bf in fit:
        c = bf.cell.get("process")
        if c is not None and str(c) not in seen:
            seen.append(str(c))
    if seen:
        return sorted(seen)
    got = list(((getattr(derived, "process", None) or {}).get("corners") or []))
    return [str(c) for c in got] or ["(single corner)"]


def grade_project(fit, *, derived=None, dataset=None, plan=None, corners=None,
                  ports=None) -> list[Grade]:
    """Every `(port, corner, block)` verdict of one fitted project.

    One row per port per corner per block: the WORST cell of that block inside that corner,
    because a roll-up is a max and the user is entitled to the worst case they might hit.  A
    block whose cells carry no process axis (an emitter constant) is reported on every corner.

    `plan` is optional; when given, its `consequences()` add a `not_run` row for a block that
    has no fit at all because the group that feeds it was ticked off.
    """
    table = {str(k): str(v) for k, v in
             dict(ports or getattr(fit, "ports", None) or {}).items()}
    corner_list = _corners_of(fit, derived, corners)

    # (port, corner, block) -> the worst row so far
    best: dict[tuple[str, str, str], Grade] = {}
    order: list[tuple[str, str, str]] = []

    holes = partial_variables(dataset)
    for bf in fit:
        ptype = table.get(bf.port, "rail")
        grade, detail = grade_block(bf, port_type=ptype)
        if not getattr(bf, "missing", False) and _has_hole(bf, ptype, holes):
            detail += ("; part of the sweep behind it never ran, so this grade covers less "
                       "than the block claims")
        cell_corner = bf.cell.get("process")
        where = [str(cell_corner)] if cell_corner is not None else list(corner_list)
        for corner in where:
            key = (bf.port, corner, bf.block)
            row = Grade(port=bf.port, corner=corner, block=bf.block, grade=grade,
                        detail=detail, score=(None if math.isnan(_num(bf.score))
                                              else float(bf.score)))
            cur = best.get(key)
            if cur is None:
                order.append(key)
                best[key] = row
            elif _worse(row, cur):
                best[key] = row

    rows = [best[k] for k in order]
    rows += _never_planned(plan, corner_list, {(p, b) for (p, _c, b) in best})
    rows.sort(key=lambda g: (g.port, g.corner, g.block))
    return rows


def _worse(a: Grade, b: Grade) -> bool:
    """Worse = a higher grade rank; ties broken by the larger score, so the row that survives
    is the one whose number the user would actually hit."""
    ra, rb = _rank(a.grade), _rank(b.grade)
    if ra != rb:
        return ra > rb
    sa = a.score if a.score is not None else float("-inf")
    sb = b.score if b.score is not None else float("-inf")
    return sa > sb


def _never_planned(plan, corner_list, have) -> list[Grade]:
    """A block nobody scheduled a run for is `not_run` -- it never reaches the fitter at all,
    so without this it would be silently absent from the report instead of named in it."""
    if plan is None or not hasattr(plan, "consequences"):
        return []
    out: list[Grade] = []
    for entry in plan.consequences():
        port, block = str(entry.get("port", "")), str(entry.get("block", ""))
        if not port or not block or (port, block) in have:
            continue
        for corner in corner_list:
            obs = ", ".join(entry.get("observables") or []) or "its measurement"
            out.append(Grade(
                port=port, corner=corner, block=block, grade="not_run",
                detail=(f"no run was scheduled for {obs}, so this block was never "
                        "characterized -- anything the model says about it is outside the "
                        "validity envelope"),
                score=None))
    return out


# --------------------------------------------------------------------------- roll-up
def rollup(grades, *, ls_default_on=(), port_types=None) -> dict:
    """`{port: {corner: {grade, block, detail, held, held_by, off_by_default}}}`.

    The headline of each cell is `headline()`: max() over the blocks that are ON in the
    delivered model.  A block that ships switched off (`ls_default_on` names the rails whose
    load-event term passed the HB check; everything else in the `ls` tier is off) is listed in
    `off_by_default` with its own grade, and does not colour the cell.

    ADDING COVERAGE CAN ONLY LOWER THIS.  The roll-up is a maximum over everything that was
    characterized, so a rail that was green on one corner and turns yellow once a second
    corner, a colder temperature or a lighter load is measured has not regressed: the model
    did not change, the question got harder.  METHODOLOGY records the same effect on the real
    part, where a single-operating-point fit is silently optimistic at the light-load edge.
    """
    types = dict(port_types or {})
    cells: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    for g in grades:
        key = (g.port, g.corner)
        if key not in cells:
            cells[key] = []
            order.append(key)
        cells[key].append(g)
    out: dict[str, dict[str, dict]] = {}
    for (port, corner) in order:
        out.setdefault(port, {})[corner] = headline(
            cells[(port, corner)], port=port, port_type=types.get(port),
            ls_default_on=ls_default_on)
    return out


def worst(grades, *, ls_default_on=(), port_types=None) -> str:
    """The one colour for the whole deliverable, as delivered (default-off blocks excluded)."""
    if not grades:
        return "not_run"
    roll = rollup(grades, ls_default_on=ls_default_on, port_types=port_types)
    heads = [c["grade"] for cells in roll.values() for c in cells.values()]
    return max(heads, key=_rank) if heads else "not_run"


def rollup_table(grades, *, ls_default_on=(), port_types=None) -> str:
    """The roll-up as fixed-width text -- what `pmukit verify` prints and what goes in the
    report.  No scores: contract 0c."""
    roll = rollup(grades, ls_default_on=ls_default_on, port_types=port_types)
    ports = sorted(roll)
    corners = sorted({c for cells in roll.values() for c in cells})
    if not ports:
        return "no block was graded.\n"
    wide = max([len(p) for p in ports] + [4])
    head = "rail".ljust(wide) + "".join(f"  {c:>10}" for c in corners) + "   worst block"
    lines = [head, "-" * len(head)]
    off_lines: list[str] = []
    for p in ports:
        row = p.ljust(wide)
        bad = None
        for c in corners:
            cell = roll[p].get(c)
            row += f"  {(cell['grade'] if cell else '--'):>10}"
            if cell and (bad is None or _rank(cell["grade"]) > _rank(bad["grade"])):
                bad = cell
            for o in (cell or {}).get("off_by_default") or []:
                if o["grade"] != "green":
                    off_lines.append(f"  {p} at {c}: {o['note']}")
        lines.append(row + f"   {(bad['block'] or '--') if bad else '--'}")
    overall = worst(grades, ls_default_on=ls_default_on, port_types=port_types)
    lines += ["", f"worst overall: {overall}"]
    if off_lines:
        lines += ["", "Off by default (graded, not part of the grades above):"] + off_lines
    return "\n".join(lines) + "\n"
