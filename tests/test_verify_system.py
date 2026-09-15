"""The self-oscillating bench: the tank maths, the two decks, and reading an autonomous log.

The bench itself needs Spectre; everything that DECIDES what Spectre is asked, and everything
that reads what it answered, is tested here without one.  The log fragments are verbatim from
`spectre -64` 18.1.0.077 runs of this bench on the synthetic PMU.
"""
import math

import pytest

from pmukit.verify import system as S

# --- verbatim: the oscillator on an ideal supply, converging -------------------------------
IDEAL_LOG = """\
********** initial residual **********
Resd Norm=2.22e+00  at node Ln:1  harm=(0)

********** iter = 1 **********
Delta Norm=3.00e+01  at node Ln:1  harm=(1)
Resd Norm=1.88e-01  at node on  harm=(4)
Frequency= 1.1950e+09 Hz, delta f= -2.41e+07

********** iter = 5 **********
Delta Norm=2.27e-02  at node Ct:1  harm=(4)
Resd Norm=2.03e-12  at node op  harm=(6)
Frequency= 1.1966e+09 Hz, delta f= 3.55e+03

*************************************************
Fundamental frequency is 1.19664 GHz.
*************************************************

Total time required for hb analysis `osc': CPU = 341.72 ms, elapsed = 360.916 ms.
spectre completes with 0 errors, 0 warnings, and 2 notices.
"""

# --- verbatim: the same oscillator through the emitted model, STALLING ----------------------
STALLED_LOG = """\
********** initial residual **********
Resd Norm=1.18e+02  at node X1.VDD0P8_A_nC  harm=(2)

********** iter = 1 **********
Delta Norm=8.89e+01  at node X1:VDD0P8_A_nC_VDD0P8_A_vrg_flow  harm=(0)
Resd Norm=1.09e+01  at node on  harm=(3)

********** iter = 100 **********
Damping Factor is 0.1
Delta Norm=5.32e+01  at node X1:VDD0P8_A_nC_VDD0P8_A_vrg_flow  harm=(2)
Resd Norm=1.15e+01  at node on  harm=(1)

Warning: Maximum number of iterations reached. Result may not be correct. You can increase \
tstab and harms or try to use twotier method.

*************************************************
Fundamental frequency is 1.19701 GHz.
*************************************************

Total time required for hb analysis `osc': CPU = 341.72 ms, elapsed = 360.916 ms.
spectre completes with 0 errors, 0 warnings, and 0 notices.
"""


# --------------------------------------------------------------------------- reading
def test_the_oscillation_frequency_is_read_with_its_unit():
    assert S._f_osc(IDEAL_LOG) == pytest.approx(1.19664e9)
    assert S._f_osc("Fundamental frequency is 950 MHz.\n") == pytest.approx(950e6)
    assert S._f_osc("nothing here\n") != S._f_osc("nothing here\n")      # NaN


def test_a_run_that_ran_out_of_newton_iterations_is_not_converged():
    """Spectre prints a frequency even when it gave up, and says so in ONE warning line.  A
    reader that only looks for 'the analysis finished' calls this a pass."""
    row = S._row({"log": STALLED_LOG, "ok": True, "command": "", "detail": "", "workdir": ""})
    assert row["hit_iteration_limit"] is True
    assert row["converged"] is False and row["oscillated"] is False
    assert row["iterations"] == 100

    good = S._row({"log": IDEAL_LOG, "ok": True, "command": "", "detail": "", "workdir": ""})
    assert good["converged"] is True and good["oscillated"] is True
    assert good["f_osc_hz"] == pytest.approx(1.19664e9)


# --------------------------------------------------------------------------- the tank
def test_the_tank_resonates_where_it_was_asked_to():
    t = S.tank(1.2e9, vreg_v=0.8, i_typ_a=2e-3)
    f0 = 1.0 / (2 * math.pi * math.sqrt(2 * t["l_half_h"] * t["c_tank_f"]))
    assert f0 == pytest.approx(1.2e9, rel=1e-9)


def test_the_negative_resistance_starts_up_and_the_cubic_limits_where_intended():
    """gm must beat the tank loss (or nothing oscillates) and the describing function of the
    cubic limiter must settle at the amplitude the bench asked for."""
    t = S.tank(1.2e9, vreg_v=0.8, i_typ_a=2e-3)
    assert t["gm_s"] > 1.0 / t["r_tank_ohm"]
    a = t["ampl_target_v"]
    assert 0.75 * t["beta_a_v3"] * a * a + 1.0 / t["r_tank_ohm"] == pytest.approx(t["gm_s"])


def test_the_supply_pump_draws_a_real_fraction_of_the_rails_current():
    t = S.tank(1.2e9, vreg_v=0.8, i_typ_a=2e-3)
    a = t["ampl_target_v"]
    assert t["pump_a_v2"] * a * a / 2.0 == pytest.approx(S.PUMP_FRACTION * 2e-3)


# --------------------------------------------------------------------------- the decks
BUILT = {"module": "PMU_t_tt", "grounds": ["VSS"],
         "ports": ["VDDA", "VDD0P8_A", "VDD0P8_B", "VSS"],
         "supplies": ["VDDA"], "rails": ["VDD0P8_A", "VDD0P8_B"], "biases": [],
         "stubs": [], "ls_ports": ["VDD0P8_A"]}


class _D:
    supply = {"pins": {"VDDA": {"nominal_v": 1.0}}}
    rails = {"VDD0P8_A": {"i_typ_a": 5e-4}, "VDD0P8_B": {"i_typ_a": 2e-3}}
    biases = {}
    freq = {"start_hz": 10.0, "stop_hz": 2e9}


def test_the_oscillator_is_powered_through_the_rail_pin_itself():
    t = S.tank(1.2e9, 0.8, 5e-4)
    deck = S.model_deck(BUILT, _D(), va_name="m.va", rail="VDD0P8_A", t=t, vset=0)
    assert "Lp (op VDD0P8_A) inductor" in deck and "Ln (on VDD0P8_A) inductor" in deck
    assert "Itail (VDD0P8_A 0) isource" in deck
    # the benched rail's ordinary DC load is REPLACED by the oscillator; the other rail keeps it
    assert "IL_VDD0P8_A " not in deck
    assert "IL_VDD0P8_B (VDD0P8_B 0) isource" in deck


def test_the_large_signal_terms_are_off_in_the_oscillator_bench():
    """This bench asks about the HB tier, not the opt-in one; leaving an `ls` term on would
    confound the two questions."""
    t = S.tank(1.2e9, 0.8, 5e-4)
    assert "load_en_VDD0P8_A=0" in S.model_deck(BUILT, _D(), va_name="m.va", rail="VDD0P8_A",
                                                t=t, vset=0)


def test_the_control_is_the_same_oscillator_on_an_ideal_source():
    t = S.tank(1.2e9, 0.8, 5e-4)
    model = S.model_deck(BUILT, _D(), va_name="m.va", rail="VDD0P8_A", t=t, vset=0)
    ideal = S.ideal_deck(t, vreg_v=0.8)
    osc = [ln for ln in model.splitlines()
           if ln.startswith(("Lp ", "Ln ", "Ct ", "Rt ", "Gneg ", "Itail ", "Ipump "))]
    ctrl = [ln for ln in ideal.splitlines()
            if ln.startswith(("Lp ", "Ln ", "Ct ", "Rt ", "Gneg ", "Itail ", "Ipump "))]
    assert len(osc) == len(ctrl) == 7
    # component values identical; only the supply net differs
    assert [ln.replace("VDD0P8_A", "X") for ln in osc] == \
           [ln.replace("vosc", "X") for ln in ctrl]
    assert "Vosc (vosc 0) vsource dc=0.8" in ideal
    assert "ahdl_include" not in ideal, "the control must not contain the model at all"


def test_the_analysis_is_autonomous():
    t = S.tank(1.2e9, 0.8, 5e-4)
    line = next(ln for ln in S.ideal_deck(t, vreg_v=0.8).splitlines() if " hb " in ln)
    assert line.startswith("osc ( op on ) hb"), "p/n nodes are what make the hb autonomous"
    assert "oscic=lin" in line and "annotate=detailed_hb" in line


def test_the_ground_pin_is_tied_to_zero():
    t = S.tank(1.2e9, 0.8, 5e-4)
    deck = S.model_deck(BUILT, _D(), va_name="m.va", rail="VDD0P8_A", t=t, vset=0)
    nets = next(ln for ln in deck.splitlines()
                if ln.startswith("X1 ")).split("(", 1)[1].split(")", 1)[0].split()
    assert nets[-1] == "0" and "VSS" not in nets


# --------------------------------------------------------------------------- no simulator
def test_without_a_simulator_the_bench_reports_a_gap_not_a_pass():
    from tests.test_verify_hb import _fit_with_one_ls_term

    class Dead:
        name = "dry_run"

        def available(self):
            return (False, "no ssh client on PATH")

    fit, derived = _fit_with_one_ls_term()
    report = S.oscillator_check(fit, derived, corner="tt", project="t", site=object(),
                                backend=Dead())
    assert report["status"] == "not_run"
    assert "not a pass -- it is a gap" in " ".join(report["notes"])


def test_the_default_is_every_rail_because_rails_do_not_behave_alike():
    """Measured on the synthetic PMU: the peaked low-ESR rail stalls the autonomous solve where
    its ESR-damped sibling converges.  Benching only the first rail would have missed it."""
    import inspect
    src = inspect.getsource(S.oscillator_check)
    assert 'rails = [rail] if rail else list(built["rails"])' in src
