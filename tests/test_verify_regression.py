"""The regression suite: the stored baseline, and today's run against it.

The expensive half -- fifteen transistor-level LDOs through the whole flow -- needs a simulator
and is OPT-IN: it runs when `PMUKIT_REGRESSION=1` and a simulator is reachable, and skips with a
reason otherwise.  Everything about the baseline FILE is checked unconditionally, because a
baseline that has lost its engine, its bench or its known-hard annotations is worse than no
baseline: it looks authoritative and is not.
"""
import os

import pytest

from pmukit.verify import regression as R

BASELINE = R.load_baseline()


# --------------------------------------------------------------------------- the file
def test_the_baseline_says_which_engine_produced_it():
    """A baseline must never be ambiguous about that: `fake` scores and Spectre scores are
    different measurements, and comparing them produces a confident wrong answer."""
    assert BASELINE["engine"] in ("spectre_ssh", "fake", "donau_alps")
    if BASELINE["engine"] == "fake":
        assert BASELINE.get("notes"), "a synthesized baseline must carry its own warning"
        assert any("NOT MEASURED" in n for n in BASELINE["notes"])
    else:
        assert BASELINE["engine_note"], "a measured baseline records the simulator it used"


def test_the_baseline_records_the_bench_it_was_measured_at():
    bench = BASELINE["bench"]
    for key in ("vin_v", "iload_a", "temps_c", "care_up_to_hz", "corners", "vset_codes"):
        assert key in bench, key
    assert bench["care_up_to_hz"] >= 1e9, (
        "two variants hide their defect above 100 MHz; a suite that stops there cannot see it")


def test_the_baseline_carries_every_variant_in_the_fixture_tree():
    cells = {c for c, _lib in R.variants()}
    assert set(BASELINE["variants"]) == cells
    assert len(cells) == 15


def test_a_variant_that_is_supposed_to_be_hard_says_so_in_the_file():
    """The old repo's headline was that the in-band composite is BLIND to some real defects.  A
    suite that cannot express 'this one is meant to score badly' teaches the wrong lesson."""
    hard = {c: v for c, v in BASELINE["variants"].items() if v.get("known_hard")}
    assert len(hard) >= 14
    for cell, row in hard.items():
        assert len(row["known_hard"]) > 60, f"{cell}: a one-word excuse is not an explanation"
    assert BASELINE["variants"]["ldo_gt"].get("known_hard") is None, (
        "the unmodified base LDO is the control: it is not allowed an excuse")


def test_the_variants_that_are_supposed_to_score_worst_actually_do():
    """The evidence that the suite measures what the fixtures were built to break, rather than
    just measuring noise."""
    def worst(cell, block):
        return (BASELINE["variants"][cell]["blocks"].get(block) or {}).get("worst")

    base_z = worst("ldo_gt", "vout/zout")
    assert worst("ldo_v10_3lc", "vout/zout") > 10 * base_z, "the un-modeled third resonance"
    assert worst("ldo_v7_esl", "vout/zout") > 4 * base_z, "the inductive HF tail"
    base_p = worst("ldo_gt", "vout/psrr")
    assert worst("ldo_v4_ffpsrr", "vout/psrr") > 10 * base_p, "the non-minimum-phase PSRR"
    assert worst("ldo_v8_dlc", "vout/psrr") > 10 * base_p, "the PI-network notch"


def test_every_block_row_carries_its_metric_and_its_band():
    for cell, row in BASELINE["variants"].items():
        assert row["blocks"], cell
        for key, blk in row["blocks"].items():
            assert blk["band"] in ("green", "yellow", "red", "not_run"), (cell, key)
            assert blk["cells"] >= 1
            if blk["worst"] is not None:
                assert blk["metric"], f"{cell} {key}: a score with no unit is not a number"


def test_the_stored_limits_match_the_ones_the_tool_uses_today():
    """If someone moves a threshold, the baseline's bands were computed against the old one."""
    from pmukit.verify import grades as G
    for metric, row in BASELINE["limits"].items():
        assert metric in G.LIMITS, f"{metric} has no limit any more"
        assert row["green"] == G.LIMITS[metric].green, metric
        assert row["yellow"] == G.LIMITS[metric].yellow, metric


# --------------------------------------------------------------------------- the comparison
def _payload(engine="spectre_ssh", **variants):
    return {"kind": R.KIND, "engine": engine, "created": "now", "variants": variants}


def _one(worst, band="green", metric="|Zout| dB RMS", known_hard=None):
    return {"blocks": {"vout/zout": {"worst": worst, "band": band, "metric": metric,
                                     "cells": 1, "missing_cells": 0, "flagged": []}},
            "known_hard": known_hard}


def test_a_cross_engine_comparison_is_refused_not_fudged():
    out = R.compare(_payload("fake", a=_one(0.2)), _payload("spectre_ssh", a=_one(0.2)))
    assert out["engine_match"] is False
    assert out["regressions"] == []
    assert "refusing to compare" in out["summary"]


def test_drift_inside_the_tolerance_is_not_a_regression():
    out = R.compare(_payload(a=_one(0.22)), _payload(a=_one(0.20)))
    assert out["regressions"] == []


def test_a_score_past_the_tolerance_is_a_regression():
    out = R.compare(_payload(a=_one(0.40)), _payload(a=_one(0.20)))
    assert len(out["regressions"]) == 1
    assert out["regressions"][0]["what"] == "the score grew past the tolerance"


def test_the_absolute_floor_stops_a_near_zero_score_tripping_on_its_last_digit():
    """0.001 -> 0.0015 is +50 %, and it is noise: the floor is 2 % of the metric's own green
    limit, which for a spectrum is 0.02 dB."""
    assert R.compare(_payload(a=_one(0.0015)), _payload(a=_one(0.001)))["regressions"] == []
    assert R.tolerance_for("|Zout| dB RMS", 0.0) == pytest.approx(0.02)
    assert R.tolerance_for("load-step droop % error", 0.0) == pytest.approx(0.2)


def test_a_band_that_falls_is_a_regression_even_inside_the_numeric_tolerance():
    """The gate is what the USER sees.  0.95 -> 1.05 dB is +11 %, inside the tolerance, and it
    crosses green into yellow -- so it fails."""
    out = R.compare(_payload(a=_one(1.05, band="yellow")), _payload(a=_one(0.95, band="green")))
    assert len(out["regressions"]) == 1
    assert "green to yellow" in out["regressions"][0]["what"]


def test_a_block_that_stops_being_fitted_is_a_regression():
    out = R.compare(_payload(a={"blocks": {}, "known_hard": None}), _payload(a=_one(0.2)))
    assert out["regressions"] and "disappeared" in out["regressions"][0]["what"]


def test_a_known_hard_variant_is_still_held_to_its_own_baseline():
    """Known-hard means 'expected to score badly', NOT 'exempt'.  It may not get worse."""
    out = R.compare(_payload(a=_one(30.0, band="red", known_hard="by design")),
                    _payload(a=_one(11.0, band="red", known_hard="by design")))
    assert len(out["regressions"]) == 1 and out["regressions"][0]["known_hard"] is True


def test_a_real_improvement_is_reported_but_never_fails():
    out = R.compare(_payload(a=_one(0.05)), _payload(a=_one(0.20)))
    assert out["regressions"] == [] and len(out["improvements"]) == 1


def test_a_new_or_removed_variant_is_named():
    out = R.compare(_payload(b=_one(0.2)), _payload(a=_one(0.2)))
    assert out["new"] == ["b"] and out["gone"] == ["a"]


# --------------------------------------------------------------------------- the real run
def _simulator():
    from pmukit.backends import make_backend
    from pmukit.site import SiteConfig
    try:
        site = SiteConfig.load()
    except Exception:                                   # noqa: BLE001 -- no site config at all
        return (False, "no site configuration on this machine")
    engine = BASELINE["engine"]
    try:
        return make_backend(engine, SiteConfig(**{**site.to_dict(), "engine": engine})).available()
    except Exception as exc:                            # noqa: BLE001 -- a probe never fails hard
        return (False, f"{exc.__class__.__name__}: {exc}")


@pytest.mark.vm
def test_the_fifteen_synthetic_ldos_still_score_what_the_baseline_says(tmp_path):
    """The suite proper.  Opt-in (`PMUKIT_REGRESSION=1`) because it runs ~600 simulations; it
    skips cleanly with a reason when the engine that made the baseline is not reachable, and it
    fails loudly on a real regression."""
    if not os.environ.get("PMUKIT_REGRESSION"):
        pytest.skip("set PMUKIT_REGRESSION=1 to run the 15-LDO suite (it needs a simulator)")
    ok, why = _simulator()
    if not ok:
        pytest.skip(f"the baseline was made with {BASELINE['engine']!r}, which is not available "
                    f"here: {why}. Comparing across engines would be meaningless.")

    current = R.run_all(engine=BASELINE["engine"], root=tmp_path, jobs=4)
    current.pop("_temp_root", None)
    out = R.compare(current, BASELINE)
    assert out["engine_match"], out["summary"]
    assert not out["gone"], f"variants vanished: {out['gone']}"
    assert not out["regressions"], R.render_compare(out)
