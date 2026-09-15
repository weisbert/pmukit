"""The HB health check, without needing a simulator for most of it.

The two things that MUST be tested on a machine with no Spectre are the log reader and the deck
writer: if either drifts, the check on the VM reports a number that is not the number Spectre
printed.  The log fragments below are verbatim from a real run of `spectre -64` 18.1.0.077 on
the synthetic PMU (`annotate=detailed_hb`), trimmed only in length.
"""
import pytest

from pmukit.verify import hb as H

# --- verbatim from spectre.log, synthetic PMU, every ls term OFF -----------------------------
BASELINE_LOG = """\
==============================
     Harmonic balance
==============================
Important HB parameters:
    RelTol=1.00e-03
    residualtol=1.00e+00
    maxperiods=100

********** initial residual **********
Resd Norm=7.87e+02  at node X1.IB_PTAT_pdd  harm=(1)

********** iter = 1 **********
Delta Norm=9.79e+09  at node X1:VDD0P8_A_nC_VDD0P8_A_vrg_flow  harm=(1)
Resd Norm=2.18e-06  at node X1.VDDA_1V0_vrf  harm=(16)

********** iter = 2 **********
Delta Norm=2.01e-12  at node X1:VDD0P8_A_nbb_VDD0P8_A_vrg_flow  harm=(1)
Resd Norm=2.23e-06  at node X1.VDDA_1V0_vrf  harm=(16)

CPU time=0 s

Total time required for hb analysis `hb1': CPU = 27.281 ms, elapsed = 38.517 ms.
spectre completes with 0 errors, 0 warnings, and 2 notices.
"""

# --- the same bench with load_en_VDD0P8_A = 1 ------------------------------------------------
TERM_ON_LOG = """\
********** initial residual **********
Resd Norm=7.87e+02  at node X1.IB_PTAT_pdd  harm=(1)

********** iter = 1 **********
Delta Norm=9.50e+09  at node X1:VDD0P8_A_nC_VDD0P8_A_vrg_flow  harm=(1)
Resd Norm=1.55e+02  at node VDD0P8_A  harm=(1)

********** iter = 8 **********
Delta Norm=3.72e-04  at node X1:VDD0P8_A_nA_VDD0P8_A_vrg_flow  harm=(0)
Resd Norm=1.68e-06  at node X1.VDDA_1V0_vrf  harm=(16)

Total time required for hb analysis `hb1': CPU = 30.926 ms, elapsed = 38.8119 ms.
spectre completes with 0 errors, 0 warnings, and 2 notices.
"""


def test_the_residual_token_is_the_one_spectre_prints():
    assert H.RESID_TOKEN in BASELINE_LOG


def test_the_reader_separates_the_pre_newton_residual_from_the_first_step():
    got = H.parse_log(BASELINE_LOG)
    assert got["initial"] == pytest.approx(787.0)
    assert got["first_step"] == pytest.approx(2.18e-06)
    assert got["final"] == pytest.approx(2.23e-06)
    assert got["iterations"] == 2
    assert got["converged"] is True


def test_the_initial_residual_is_blind_to_an_op_inert_term_and_the_first_step_is_not():
    """The measurement that fixed the metric: the `ls` terms have zero value AND zero slope at
    the operating point, and the HB initial guess IS that operating point, so the pre-Newton
    residual is bit-identical with the term on and off.  Only the first Newton step sees it."""
    off, on = H.parse_log(BASELINE_LOG), H.parse_log(TERM_ON_LOG)
    assert off["initial"] == on["initial"], "an OP-inert term cannot move the initial residual"
    assert on["first_step"] / off["first_step"] > 1e6


def test_a_log_with_no_hb_trace_reads_as_no_numbers_rather_than_zero():
    got = H.parse_log("spectre completes with 0 errors, 0 warnings, and 0 notices.\n")
    assert got["first_step"] != got["first_step"]          # NaN, not 0.0
    assert got["iterations"] == 0 and got["converged"] is False


def test_a_run_that_never_finished_is_not_converged():
    assert H.parse_log(BASELINE_LOG.split("Total time")[0])["converged"] is False


# --------------------------------------------------------------------------- the bench
class _D:
    """A DerivedConfig-shaped stand-in: the deck writer reads it by key, never by class."""

    supply = {"pins": {"VDDA_1V0": {"nominal_v": 1.0}}}
    rails = {"VDD0P8_A": {"i_typ_a": 5e-4}, "VDD0P8_B": {"i_typ_a": 2e-3}}
    biases = {"IB_PTAT": {"vcomp_v": 0.4}}
    freq = {"start_hz": 10.0, "stop_hz": 2e9}


BUILT = {"module": "PMU_demo_tt", "grounds": ["VSS_A", "VSS_B"],
         "ports": ["VDDA_1V0", "VDD0P8_A", "VDD0P8_B", "IB_PTAT", "VDD0P8_C", "VSS_A", "VSS_B"],
         "supplies": ["VDDA_1V0"], "rails": ["VDD0P8_A", "VDD0P8_B"], "biases": ["IB_PTAT"],
         "stubs": ["VDD0P8_C"], "ls_ports": ["VDD0P8_A", "VDD0P8_B"]}
DRIVE = {"port": "VDD0P8_A", "f_hz": 1.34e6, "ampl_a": 3.4e-3, "z_peak_ohm": 86.0,
         "deadzone_v": 0.0967, "swing_v": 0.29, "engaged": True}


def test_every_ground_pin_is_tied_to_zero():
    """TOOL_FACTS: an emitted module whose VSS was left floating went to -100 MV."""
    deck = H.bench_deck(BUILT, _D(), va_name="m.va", drive=DRIVE, vset=3)
    inst = next(ln for ln in deck.splitlines() if ln.startswith("X1 "))
    nets = inst.split("(", 1)[1].split(")", 1)[0].split()
    assert nets[-2:] == ["0", "0"]
    assert "VSS_A" not in nets and "VSS_B" not in nets


def test_the_baseline_deck_has_every_ls_term_off():
    deck = H.bench_deck(BUILT, _D(), va_name="m.va", drive=DRIVE, on="", vset=3)
    assert "load_en_VDD0P8_A=0" in deck and "load_en_VDD0P8_B=0" in deck


def test_exactly_one_term_is_enabled_at_a_time():
    deck = H.bench_deck(BUILT, _D(), va_name="m.va", drive=DRIVE, on="load_en_VDD0P8_B", vset=3)
    assert "load_en_VDD0P8_B=1" in deck and "load_en_VDD0P8_A=0" in deck


def test_the_hot_rail_carries_the_tone_and_the_others_do_not():
    deck = H.bench_deck(BUILT, _D(), va_name="m.va", drive=DRIVE, vset=3)
    hot = next(ln for ln in deck.splitlines() if ln.startswith("IL_VDD0P8_A "))
    cold = next(ln for ln in deck.splitlines() if ln.startswith("IL_VDD0P8_B "))
    assert "type=sine" in hot and "freq=1.34e+06" in hot
    assert "type=sine" not in cold


def test_the_analysis_asks_for_the_annotation_that_prints_the_residual():
    deck = H.bench_deck(BUILT, _D(), va_name="m.va", drive=DRIVE, vset=3, harmonics=16)
    line = next(ln for ln in deck.splitlines() if ln.startswith("hb1 "))
    assert "annotate=detailed_hb" in line and "maxharms=[16]" in line
    assert "fundfreqs=[1.34e+06]" in line


def test_the_drive_is_sized_to_clear_the_terms_deadzone():
    """A term that never switches on inside the simulation makes every residual identical and
    the whole check vacuous, so the amplitude is solved from the fitted Zout, not guessed."""
    from pmukit.fit import FitResult
    from pmukit.fit._base import BlockFit

    fit = FitResult(ports={"VDD0P8_A": "rail"})
    fit.add(BlockFit(port="VDD0P8_A", block="zout", cell={"process": "tt", "temp_c": 25.0,
                                                          "load_a": 5e-4},
                     params={"Ra": 0.05, "La": 2e-6, "Rpl": 1e5, "Cout": 1e-9, "esr": 0.5},
                     score=0.1, metric="|Zout| dB RMS"))
    fit.add(BlockFit(port="VDD0P8_A", block="load_en", cell={"process": "tt", "temp_c": 25.0},
                     params={"ovVdz": 0.05, "iaG": 0.012, "iaV": 0.3},
                     score=10.0, metric="load-step droop % error"))
    d = _D()
    drive = H._drive(fit, "VDD0P8_A", "tt", d, 5e-4)
    assert drive["engaged"] is True
    # at least far enough past the deadzone to switch the term on, and never below a
    # fully-modulated load (the realistic worst case a consumer can present).
    assert drive["swing_v"] >= H.ENGAGE_X * 0.05
    assert drive["ampl_a"] >= 5e-4
    assert d.freq["start_hz"] <= drive["f_hz"] <= d.freq["stop_hz"]


def test_the_nominal_drive_is_the_documented_operational_ripple():
    nominal = H._nominal_drive(dict(DRIVE), i_typ_a := 5e-4)
    assert nominal["ampl_a"] == pytest.approx(H.NOMINAL_RIPPLE * i_typ_a)
    assert nominal["engaged"] is False, "the deadzone sits ABOVE the ordinary ripple by design"


# --------------------------------------------------------------------------- no simulator
def test_without_a_simulator_nothing_is_signed_off():
    """`hb=False` on the Model screen must not be the same as `hb` passing: an unrun check
    leaves every large-signal term OFF and says why."""
    class Dead:
        name = "dry_run"

        def available(self):
            return (False, "no ssh client on PATH")

    fit, derived = _fit_with_one_ls_term()
    report = H.hb_check(fit, derived, corner="tt", project="t", site=object(), backend=Dead())
    assert report["status"] == "not_run"
    assert report["ls_default_on"] == []
    assert "no simulator" in " ".join(report["notes"])
    assert all(t["status"] == "not_run" for t in report["terms"])


def test_a_model_with_no_large_signal_term_passes_trivially_and_says_so():
    fit, derived = _fit_with_one_ls_term(with_ls=False)
    report = H.hb_check(fit, derived, corner="tt", project="t", site=object(), backend=None)
    assert report["status"] == "pass" and report["terms"] == []
    assert "nothing the check could turn on" in " ".join(report["notes"])


def _fit_with_one_ls_term(with_ls=True):
    """The smallest fit the emitter will accept: one rail with a Zout and a DC level."""
    from pmukit.config import DerivedConfig
    from pmukit.fit import FitResult
    from pmukit.fit._base import BlockFit

    cell = {"process": "tt", "temp_c": 25.0, "vset": 0, "load_a": 5e-4}
    fit = FitResult(ports={"VDD0P8_A": "rail"})
    fit.add(BlockFit(port="VDD0P8_A", block="zout", cell=cell,
                     params={"Ra": 0.05, "La": 2e-6, "Rpl": 1e5, "Cout": 1e-9, "esr": 0.5},
                     score=0.1, metric="|Zout| dB RMS"))
    fit.add(BlockFit(port="VDD0P8_A", block="dc", cell={"process": "tt", "vset": 0,
                                                        "load_a": 5e-4},
                     params={"vout": 0.8, "vout_tc": -4e-5}, score=0.01, metric="vout % RMS"))
    if with_ls:
        fit.add(BlockFit(port="VDD0P8_A", block="load_en", cell={"process": "tt", "temp_c": 25.0},
                         params={"iaG": 0.012, "iaV": 0.3, "ovVdz": 0.05, "ovR": 4000.0,
                                 "ovVmax": 2.0, "ovVsc": 0.008, "ovIsc": 1e-3},
                         score=10.0, metric="load-step droop % error"))
    derived = DerivedConfig.from_dict({
        "project": "t",
        "process": {"corners": ["tt"]}, "temps_c": {"points": [25.0]}, "vset": {"codes": [0]},
        "freq": {"start_hz": 10.0, "stop_hz": 2e9},
        "supply": {"pins": {"VDDA_1V0": {"nominal_v": 1.0}}, "nominal_v": 1.0},
        "rails": {"VDD0P8_A": {"i_typ_a": 5e-4, "gnd": "VSS"}},
        "biases": {}, "grounds": {"by_pin": {"VDD0P8_A": "VSS"}, "nets": ["VSS"]},
        "loads": {"VDD0P8_A": {"points_a": [5e-4]}}, "stubs": {}, "en": {}})
    return fit, derived
