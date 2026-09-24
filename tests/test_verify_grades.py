"""The grading rules: the threshold table, the two caps on top of it, and the roll-up.

Every rule in `pmukit/verify/grades.py`'s docstring has a test here, because each of them is a
place where a plausible-looking shortcut would lie to the user:

  * calling missing coverage `red` (it is `not_run`);
  * calling a tight fit to an undetermined parameter `green` (it is capped at `yellow`);
  * letting a score number reach report.md's rail table (contract 0c forbids it);
  * reading a roll-up that got worse on richer coverage as a regression (it is a maximum).
"""
import re

import pytest

from pmukit.deliverable import Grade
from pmukit.fit import FitResult
from pmukit.fit._base import BlockFit
from pmukit.verify import grades as G

#: The same pattern `tests/test_deliverable.py` uses to police the rail table.
SCORE_RE = re.compile(r"\d\.\d{2,}|\d+\.?\d*e[+-]?\d+", re.I)


def bf(port="VDD0P8_A", block="zout", metric="|Zout| dB RMS", score=0.1, cell=None,
       params=None, ident=None, missing=False, notes=()):
    return BlockFit(port=port, block=block,
                    cell=dict({"process": "tt"} if cell is None else cell),
                    params=dict({"Ra": 0.05} if params is None else params),
                    score=score, metric=metric,
                    n_points=100, identifiability=dict(ident or {}), notes=list(notes),
                    missing=missing)


def result(*fits, ports=None):
    r = FitResult(project="t", ports=dict(ports or {"VDD0P8_A": "rail"}))
    for f in fits:
        r.add(f)
    return r


# --------------------------------------------------------------------------- the table
def test_every_metric_the_fitters_emit_has_a_row_of_its_own():
    """A metric with no row falls back on a unit guess, which is a silent judgement call.  If a
    fitter grows a new metric, this test is where it gets noticed."""
    from pmukit.fit import bias, dc, en, load_en, noise, psrr, zout

    seen = set()
    for mod in (zout, psrr, noise, dc, bias, load_en, en):
        for m in re.findall(r'metric\s*=\s*"([^"]+)"', open(mod.__file__, encoding="utf-8").read()):
            if m:
                seen.add(m)
    missing = sorted(m for m in seen if m not in G.LIMITS)
    assert not missing, f"no acceptance limit for {missing}; add a row to grades.LIMITS"


def test_every_limit_carries_its_reasoning_and_is_ordered():
    for metric, lim in G.LIMITS.items():
        assert lim.green < lim.yellow, metric
        assert lim.unit in ("dB", "%"), metric
        assert len(lim.why) > 120, f"{metric}: a threshold without its reasoning is a magic number"


def test_a_spectrum_is_not_judged_like_a_dc_table():
    """The instruction in one assertion: dB and % are different questions."""
    assert G.LIMITS["vout % RMS"].green < G.LIMITS["|Zout| dB RMS"].green
    assert G.LIMITS["load-step droop % error"].green > G.LIMITS["I-V % of plateau RMS"].green
    assert G.LIMITS["Sv dB RMS"].green > G.LIMITS["|Zout| dB RMS"].green


@pytest.mark.parametrize("score,want", [(0.5, "green"), (1.0, "green"), (1.01, "yellow"),
                                        (3.0, "yellow"), (3.01, "red")])
def test_the_bands_are_inclusive_upper_bounds(score, want):
    assert G.band_for("|Zout| dB RMS", score) == want


def test_an_unknown_metric_falls_back_on_its_unit_and_says_so():
    lim, exact = G.limit_for("wobble dB RMS")
    assert lim is G.DEFAULT_DB and exact is False
    grade, detail = G.grade_block(bf(metric="wobble dB RMS", score=0.5))
    assert grade == "green" and "generic limit" in detail


def test_a_metric_with_no_unit_cannot_be_signed_off_automatically():
    assert G.limit_for("wobbliness") == (None, False)
    grade, detail = G.grade_block(bf(metric="wobbliness", score=0.0))
    assert grade == "yellow" and "no acceptance limit" in detail


def test_explain_limits_prints_every_row_and_the_two_rules_on_top():
    text = G.explain_limits()
    for metric in G.LIMITS:
        assert metric in text
    assert "never `red`" in text
    assert "ADDING COVERAGE" in text and "CAN ONLY LOWER" in text


# --------------------------------------------------------------------------- not_run vs red
def test_missing_coverage_is_not_run_never_red():
    grade, detail = G.grade_block(bf(missing=True, score=float("nan"),
                                     notes=["ac_zout.VDD0P8_A at tt: never run"]))
    assert grade == "not_run"
    assert "never ran" in detail and "not a bad model" in detail


def test_a_run_that_broke_reads_differently_from_one_nobody_scheduled():
    broke = G.grade_block(bf(missing=True, notes=["ac_zout.X at tt: registered missing"]))[1]
    never = G.grade_block(bf(missing=True, notes=["ac_zout.X at tt: never run"]))[1]
    assert "ran and produced no usable data" in broke
    assert "never ran" in never


def test_an_emitter_constant_is_green_not_ungraded():
    """`no_sink` has no observable and no residual: there is nothing that can drift."""
    grade, detail = G.grade_block(bf(block="no_sink", metric="", score=float("nan"), params={}))
    assert grade == "green" and "no fitted number" in detail


def test_a_block_with_a_limit_but_no_residual_cannot_claim_green():
    assert G.grade_block(bf(score=float("nan")))[0] == "yellow"


# --------------------------------------------------------------------------- identifiability
def test_a_tight_fit_to_an_undetermined_parameter_is_capped_at_yellow():
    tight = bf(score=0.01, params={"Ra": 0.05, "Rpl": 3.2e4},
               ident={"unidentifiable": ["Rpl"]})
    grade, detail = G.grade_block(tight)
    assert grade == "yellow"
    assert "does not pin Rpl" in detail and "false green" in detail


def test_a_loosely_pinned_parameter_caps_it_too():
    grade, _ = G.grade_block(bf(score=0.01, params={"Ra": 0.05, "white": 9e-11},
                                ident={"poorly_determined": ["white"]}))
    assert grade == "yellow"


@pytest.mark.parametrize("name,params", [
    ("Rb", {"Rb": 1e9, "Lb": 1e-12}),                       # branch B at its OFF sentinel
    ("Lb", {"Rb": 1e9, "Lb": 1e-12}),
    ("pc_w0", {"pc_gain": 0.0, "pc_w0": 8.9e5, "pc_q": 1.0}),   # an off complex section
    ("pc_gain", {"pc_gain": 0.0}),
    ("G_i[1]", {"G_i": [-7e-3, 0.0, 0.0]}),                 # an unused bank section
    ("pole_i_hz[1]", {"G_i": [-7e-3, 0.0, 0.0], "pole_i_hz": [1.8e5, 1.6e8, 1.6e8]}),
    ("amp_i[1]", {"amp_i": [2.3e-6, 1e-13]}),               # 60 dB below the loudest section
])
def test_a_switched_off_parameter_does_not_cap_anything(name, params):
    """The documented exception.  These fire on EVERY fit; if they capped the grade, every
    block in the tool would be yellow and the colour would stop meaning anything."""
    assert G._inert(name, params) is True
    assert G.grade_block(bf(score=0.01, params=params,
                            ident={"unidentifiable": [name]}))[0] == "green"


def test_an_active_parameter_the_data_cannot_see_still_caps():
    params = {"G_i": [-7e-3, 4e-3, 0.0], "pole_i_hz": [1.8e5, 2.0e6, 1.6e8]}
    assert G._inert("G_i[1]", params) is False
    assert G.grade_block(bf(score=0.01, params=params, ident={"unidentifiable": ["G_i[1]"]},
                            metric="PSRR dB RMS", block="psrr"))[0] == "yellow"


# --------------------------------------------------------------------------- contract 0c
def test_no_grade_detail_can_carry_a_score_into_the_rail_table():
    """`deliverable.render_report` puts `detail` straight into the rail table, and contract 0c
    says no internal score may appear there.  A fitter note quoted back must be filtered."""
    rows = [
        bf(score=0.0123456),
        bf(score=float("nan")),
        bf(missing=True, notes=["ac_zout.X at tt/25C/vset3/5.0e-04A: never run"]),
        bf(score=12.5, ident={"unidentifiable": ["Rpl"], "poorly_determined": ["Ra"]},
           params={"Ra": 0.05, "Rpl": 3.2e4}),
        bf(block="load_en", metric="load-step droop % error", score=110.42),
    ]
    for row in rows:
        _grade, detail = G.grade_block(row)
        assert not SCORE_RE.search(detail), detail


def test_a_reason_that_carries_numbers_is_dropped_rather_than_truncated():
    detail = G.grade_block(bf(missing=True, notes=["Cout = 5.26e+03 pF so the replay failed"]))[1]
    assert "5.26" not in detail and not SCORE_RE.search(detail)


def test_the_tier_note_travels_with_the_large_signal_and_enable_blocks():
    assert "switched off" in G.grade_block(
        bf(block="load_en", metric="load-step droop % error", score=5.0))[1]


# --------------------------------------------------------------------------- the project
def test_one_row_per_port_corner_block_keeping_the_worst_cell():
    res = result(
        bf(cell={"process": "tt", "load_a": 1e-4}, score=0.2),
        bf(cell={"process": "tt", "load_a": 1e-3}, score=2.4),
        bf(cell={"process": "ss", "load_a": 1e-4}, score=0.1))
    rows = G.grade_project(res)
    by = {(g.corner, g.block): g for g in rows}
    assert by[("tt", "zout")].score == pytest.approx(2.4)
    assert by[("tt", "zout")].grade == "yellow"
    assert by[("ss", "zout")].grade == "green"


def test_a_block_with_no_process_axis_is_reported_on_every_corner():
    res = result(bf(cell={"process": "tt"}), bf(block="no_sink", metric="", cell={}, params={},
                                                score=float("nan")))
    rows = G.grade_project(res, corners=["tt", "ss"])
    assert {g.corner for g in rows if g.block == "no_sink"} == {"tt", "ss"}


def test_adding_coverage_can_only_lower_a_rollup():
    """The consequence the old repo recorded, as an executable statement: the roll-up is a max,
    so a rail that was green on one corner and turns yellow when a second corner is measured has
    not regressed -- the model did not change, the question got harder."""
    thin = result(bf(cell={"process": "tt", "load_a": 1e-4}, score=0.2))
    rich = result(bf(cell={"process": "tt", "load_a": 1e-4}, score=0.2),
                  bf(cell={"process": "tt", "load_a": 1e-6}, score=2.4))
    thin_roll = G.rollup(G.grade_project(thin))["VDD0P8_A"]["tt"]
    rich_roll = G.rollup(G.grade_project(rich))["VDD0P8_A"]["tt"]
    assert thin_roll["grade"] == "green" and rich_roll["grade"] == "yellow"
    assert G._rank(rich_roll["grade"]) >= G._rank(thin_roll["grade"])


def test_a_rollup_is_the_worst_block_and_red_outranks_not_run():
    res = result(bf(block="zout", score=0.1),
                 bf(block="psrr", metric="PSRR dB RMS", score=9.0),
                 bf(block="noise", metric="Sv dB RMS", missing=True))
    roll = G.rollup(G.grade_project(res))
    assert roll["VDD0P8_A"]["tt"]["grade"] == "red"
    assert roll["VDD0P8_A"]["tt"]["block"] == "psrr"


def test_worst_and_the_table_agree():
    rows = G.grade_project(result(bf(score=9.0), bf(block="dc", metric="vout % RMS", score=0.1)))
    assert G.worst(rows) == "red"
    text = G.rollup_table(rows)
    assert "worst overall: red" in text and "VDD0P8_A" in text
    assert not SCORE_RE.search(text.split("worst overall")[0]), "the roll-up table shows no scores"


def test_a_block_nobody_planned_a_run_for_is_not_run():
    class Plan:
        def consequences(self):
            return [{"port": "VDD0P8_B", "block": "noise", "params": ["white"],
                     "observables": ["noise_v"]}]

    rows = G.grade_project(result(bf()), plan=Plan(), corners=["tt"])
    row = next(g for g in rows if g.port == "VDD0P8_B")
    assert row.grade == "not_run" and "noise_v" in row.detail


def test_grades_round_trip_through_json():
    for g in G.grade_project(result(bf(), bf(block="dc", metric="vout % RMS", score=0.1))):
        assert Grade.from_json(g.to_json()).to_json() == g.to_json()


# --------------------------------------------------------------------------- as delivered
def _off_case():
    """VDD0P8_B: zout green, load_en red -- the fake engine's 208 % droop."""
    return G.grade_project(result(
        bf(port="VDD0P8_B", score=0.1),
        bf(port="VDD0P8_B", block="load_en", metric="load-step droop % error", score=208.0),
        ports={"VDD0P8_B": "rail"}))


def test_a_block_that_ships_off_does_not_colour_the_rollup_but_is_reported():
    """The headline is what the delivered model does BY DEFAULT: load_en ships switched off
    until the HB check clears it, so its red is listed beside the cell, not averaged into it."""
    rows = _off_case()
    cell = G.rollup(rows)["VDD0P8_B"]["tt"]
    assert cell["grade"] == "green" and cell["block"] == "zout"
    [off] = cell["off_by_default"]
    assert off["block"] == "load_en" and off["grade"] == "red"
    assert off["switch"] == "load_en_VDD0P8_B=1"
    assert off["note"].startswith("off by default: load_en FAIL")
    assert not SCORE_RE.search(off["note"]), "the note lands in the rail table: no scores"
    assert G.worst(rows) == "green"
    text = G.rollup_table(rows)
    assert "worst overall: green" in text
    assert "Off by default" in text and "load_en_VDD0P8_B=1" in text


def test_once_the_hb_check_clears_it_the_term_counts():
    rows = _off_case()
    for on in (["VDD0P8_B"], ["load_en_VDD0P8_B"]):
        cell = G.rollup(rows, ls_default_on=on)["VDD0P8_B"]["tt"]
        assert cell["grade"] == "red" and cell["block"] == "load_en"
        assert cell["off_by_default"] == []
        assert G.worst(rows, ls_default_on=on) == "red"


def test_only_the_ls_tier_and_the_unemitted_en_ramp_are_default_off():
    assert G.default_off("load_en", "VDD0P8_A", "rail")
    assert G.default_off("load_en", "VDD0P8_A")                 # port type looked up
    assert not G.default_off("load_en", "VDD0P8_A", "rail", ["VDD0P8_A"])
    # EN is a pass-through pin of the delivered model: its ramp is never emitted
    assert G.default_off("ramp", "EN", "en")
    assert "not active in the delivered model" in G.off_note("ramp", "EN", "red")
    for block, pt in (("zout", "rail"), ("psrr", "bias"), ("idc", "bias"), ("no_sink", "rail")):
        assert not G.default_off(block, "X", pt), block


def test_a_held_grade_is_marked_and_carries_a_short_reason():
    held = bf(score=0.039, ident={"unidentifiable": ["Rpl"]}, params={"Ra": 0.05, "Rpl": 3e4})
    v = G.block_verdict(held)
    assert v["grade"] == "yellow" and v["band"] == "green"
    assert v["held"] and v["held_by"] == ["Rpl"]
    assert v["reason"] == "held at yellow: the data does not pin Rpl"
    assert G.is_held(v["detail"])
    rows = G.grade_project(result(held))
    cell = G.rollup(rows)["VDD0P8_A"]["tt"]
    assert cell["held"] and cell["held_by"] == ["zout"]
    plain = G.block_verdict(bf(score=2.0))
    assert plain["grade"] == "yellow" and not plain["held"]
    assert plain["reason"] == "past the green limit, still usable"
    assert G.block_verdict(bf(score=0.1))["reason"] == ""
