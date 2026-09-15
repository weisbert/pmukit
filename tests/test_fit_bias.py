"""The current bias: the two shipped bugs, and the recovery of every fitted number.

Two of these tests exist because the bugs REACHED SILICON-FACING MODELS:

  * a hardcoded polarity made a reference that SOURCES emit as a sink, so the model drew the
    current the real reference injects;
  * a symmetric compliance gate let the sink REOPEN to full current above its ceiling.

Both are asserted directly, not through a proxy.
"""
import numpy as np
import pytest

from pmukit.config import DerivedConfig
from pmukit.fit import bias
from tests.test_fit_common import (BIAS, CELL_NOLOAD, FREQ, TEMPS, declare_ac_noload, make, rel)

VPIN = np.linspace(0.0, 1.0, 51)
VC = 0.4
IDC25, PTAT = 5.0e-7, 1.2e-9          # +500 nA at 25 C, +1.2 nA/degC: a SOURCE
G0, CP = 2.0e-9, 1.5e-14
VHI, VKNEE, KNEE_P = 0.85, 0.05, 2.0
GDD, POLE = -4.0e-9, 2.0e5
WHITE_I, FLICKER_I = 1.0e-12, 3.0e-12

DERIVED = DerivedConfig(biases={BIAS: {"vcomp_v": VC}})


def iv_curve(T, sign=1.0):
    idc = IDC25 + PTAT * (T - 25.0)
    return sign * (idc + G0 * (VPIN - VC)) * bias.gate(VPIN, VKNEE, KNEE_P, "hi", VHI)


def build(tmp_path, sign=1.0):
    ds = make(tmp_path)
    ds.declare(f"dc_iv.{BIAS}", dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", unit="A", coord=VPIN)
    declare_ac_noload(ds, f"ac_yout.{BIAS}", unit="S")
    declare_ac_noload(ds, f"noise_i.{BIAS}", unit="A^2/Hz", dtype="float64")
    declare_ac_noload(ds, f"ac_psrr.{BIAS}", unit="S")
    for T in TEMPS:
        cell = dict(CELL_NOLOAD, temp_c=T)
        ds.put(f"dc_iv.{BIAS}", cell, iv_curve(T, sign))
        ds.put(f"ac_yout.{BIAS}", cell, G0 + 1j * 2 * np.pi * FREQ * CP)
        ds.put(f"noise_i.{BIAS}", cell, WHITE_I ** 2 + FLICKER_I ** 2 / FREQ)
        ds.put(f"ac_psrr.{BIAS}", cell, GDD / (1.0 + 1j * FREQ / POLE))
    return ds


# --------------------------------------------------------------------------- the two bugs


def test_a_sourcing_reference_is_detected_as_a_source(tmp_path):
    """THE BUG THAT SHIPPED: a reference that SOURCES reads a positive probe current at its
    operating point.  The polarity comes from that sign and is never assumed."""
    ds = build(tmp_path, sign=+1.0)
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.params["pol"] == "source"
    assert any("direction DETECTED" in n for n in bf.notes)
    model = bias.predict_iv(bf.params, VPIN, vc=VC, g0=G0)
    assert float(np.interp(VC, VPIN, model)) > 0.0             # it INJECTS, as the data says


def test_a_sinking_reference_is_detected_as_a_sink(tmp_path):
    ds = build(tmp_path, sign=-1.0)
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.params["pol"] == "sink"
    model = bias.predict_iv(bf.params, VPIN, vc=VC, g0=G0)
    assert float(np.interp(VC, VPIN, model)) < 0.0


def test_the_compliance_knee_stays_collapsed_above_the_ceiling(tmp_path):
    """THE OTHER BUG THAT SHIPPED: a symmetric gate climbs back to 1 above the compliance
    ceiling, so the reference reopens to full current where the real device is starved."""
    ds = build(tmp_path)
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.params["knee_side"] == "hi"
    above = np.array([VHI + 0.02, VHI + 0.2, VHI + 1.0, 3.0])
    I_above = bias.predict_iv(bf.params, above, vc=VC, g0=G0)
    assert np.all(np.abs(I_above) < 1e-12), I_above
    # ... and it is numerically identical to the old form BELOW the ceiling, i.e. over the
    # whole characterized range
    inside = VPIN[VPIN < VHI - 2 * VKNEE]
    one_sided = bias.gate(inside, VKNEE, KNEE_P, "hi", VHI)
    symmetric = np.tanh(np.power(np.sqrt((VHI - inside) ** 2 + 1e-12) / VKNEE, KNEE_P))
    assert np.allclose(one_sided, symmetric, atol=1e-12)


def test_the_gate_is_one_sided_on_the_low_side_too():
    below = np.array([-1.0, -0.2, -0.01])
    assert np.all(bias.gate(below, 0.05, 2.0, "lo") == 0.0)


# --------------------------------------------------------------------------- recovery


def test_iv_and_ptat_law_are_recovered(tmp_path):
    ds = build(tmp_path)
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert not bf.missing
    assert bf.score < 2.0, bf.notes                    # % of the plateau current
    assert rel(bf.params["idc"], IDC25) < 0.01
    assert rel(bf.params["ptat_slope"], PTAT) < 0.02
    assert rel(bf.params["vhi"], VHI) < 0.02
    assert rel(bf.params["vknee"], VKNEE) < 0.10
    assert rel(bf.params["knee_p"], KNEE_P) < 0.25     # the closed-form knee exponent is coarse
    assert bf.cell == {"process": "tt", "vset": 3}     # temp_cont: no temperature in the cell


def test_ptat_slope_is_a_continuous_law_evaluated_by_predict(tmp_path):
    ds = build(tmp_path)
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    for T in (-40.0, 0.0, 25.0, 85.0, 125.0):
        want = IDC25 + PTAT * (T - 25.0)
        assert rel(bias.predict_idc_t(bf.params, T), want) < 0.02


def test_yout_recovers_g0_and_cp_and_rejects_a_degenerate_zero(tmp_path):
    ds = build(tmp_path)
    bf = bias.fit_yout(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.score < 0.05
    assert rel(bf.params["g0"], G0) < 0.01
    assert rel(bf.params["Cp"], CP) < 0.02
    assert bf.params["wz"] is None and bf.params["wp"] is None
    assert any("no second-order zero adopted" in n for n in bf.notes)


def test_yout_adopts_a_real_cascode_zero(tmp_path):
    wz, wp = 2 * np.pi * 1e5, 2 * np.pi * 3e6
    s = 1j * 2 * np.pi * FREQ
    Y = G0 * (1.0 + s / wz) / (1.0 + s / wp) + s * CP
    ds = make(tmp_path)
    declare_ac_noload(ds, f"ac_yout.{BIAS}", unit="S")
    ds.put(f"ac_yout.{BIAS}", CELL_NOLOAD, Y)
    bf = bias.fit_yout(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.params["wz"] is not None, bf.notes
    assert rel(bf.params["wz"], wz) < 0.10
    assert rel(bf.params["wp"], wp) < 0.15
    assert bf.score < 0.2


def test_bias_noise_is_recovered_from_the_log_domain(tmp_path):
    ds = build(tmp_path)
    bf = bias.fit_noise(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.score < 0.05
    assert rel(bf.params["white"], WHITE_I) < 0.02
    assert rel(bf.params["flicker"], FLICKER_I) < 0.02
    assert any("LOG AMPLITUDE" in n for n in bf.notes)


def test_bias_psrr_keeps_its_sign_and_finds_the_pole(tmp_path):
    ds = build(tmp_path)
    bf = bias.fit_psrr(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.params["gdd"] < 0.0                       # the SIGN is never collapsed
    assert rel(bf.params["gdd"], GDD) < 0.01
    assert rel(bf.params["psrr_pole_hz"], POLE) < 0.05
    assert bf.score < 0.2


def test_predict_reproduces_every_bias_score(tmp_path):
    ds = build(tmp_path)
    y = bias.fit_yout(ds, BIAS, CELL_NOLOAD, DERIVED)
    Y = G0 + 1j * 2 * np.pi * FREQ * CP
    assert float(np.sqrt(np.mean((20 * np.log10(
        np.abs(bias.predict(y.params, f=FREQ)) / np.abs(Y))) ** 2))) == pytest.approx(
            y.score, rel=1e-9, abs=1e-12)

    n = bias.fit_noise(ds, BIAS, CELL_NOLOAD, DERIVED)
    In = np.sqrt(WHITE_I ** 2 + FLICKER_I ** 2 / FREQ)
    assert float(np.sqrt(np.mean((20 * np.log10(
        np.abs(bias.predict(n.params, f=FREQ)) / In)) ** 2))) == pytest.approx(
            n.score, rel=1e-9, abs=1e-12)

    p = bias.fit_psrr(ds, BIAS, CELL_NOLOAD, DERIVED)
    g = GDD / (1.0 + 1j * FREQ / POLE)
    assert float(np.sqrt(np.mean((20 * np.log10(
        np.abs(bias.predict(p.params, f=FREQ)) / np.abs(g))) ** 2))) == pytest.approx(
            p.score, rel=1e-9, abs=1e-12)

    b = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    model = bias.predict(b.params, v=VPIN, vc=VC, g0=y.params["g0"])
    gt = iv_curve(25.0)
    plateau = float(np.median(np.sort(np.abs(gt))[-8:]))
    again = float(np.sqrt(np.mean(((model - gt) / plateau) ** 2)) * 100.0)
    assert again == pytest.approx(b.score, rel=1e-6, abs=1e-9)


def test_g0_comes_from_the_admittance_not_the_iv_chord(tmp_path):
    """The chord crosses the turn-off knee and is about 225x too steep; the I-V law and the
    `yout` block must share ONE conductance so the emitted model equals the graded one."""
    ds = build(tmp_path)
    y = bias.fit_yout(ds, BIAS, CELL_NOLOAD, DERIVED)
    b = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert any("anchored to ac_yout" in n for n in b.notes)
    assert rel(y.params["g0"], G0) < 0.01


def test_missing_bias_data_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    ds.declare(f"dc_iv.{BIAS}", dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", unit="A", coord=VPIN)
    ds.mark_missing(f"dc_iv.{BIAS}", dict(CELL_NOLOAD, temp_c=25.0), "engine exited 1")
    bf = bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)
    assert bf.missing is True and bf.params == {}
    assert "registered missing" in bf.notes[0]


def test_unknown_block_name_is_refused(tmp_path):
    ds = build(tmp_path)
    with pytest.raises(KeyError):
        bias.fit(ds, BIAS, CELL_NOLOAD, DERIVED, block="not_a_block")


# ------------------------------------------------- supply feedthrough (found on real Spectre)
def test_supply_feedthrough_is_kept_when_the_transfer_rises():
    """A real mirror's supply coupling RISES above the pole through overlap capacitance.

    Measured on the synthetic PMU's PTAT reference with real Spectre: flat at 357 nS to ~10 kHz,
    then 500x up to 173 uS at 1 GHz (28 fF). A falling-only form fitted to that costs ~22 dB, and
    it is exactly the band that makes VCO spurs -- so `c_ft` exists.
    """
    import numpy as np
    from pmukit.fit.bias import _fit_gdd, predict_psrr

    f = np.logspace(1, 9, 161)
    gdd, c_ft = 3.57e-7, 2.76e-14
    g = gdd + 1j * 2 * np.pi * f * c_ft
    p = _fit_gdd(f, g)
    assert p["c_ft"] is not None
    assert p["c_ft"] == pytest.approx(c_ft, rel=0.05)
    assert p["gdd"] == pytest.approx(gdd, rel=0.05)
    err = 20 * np.log10(np.abs(predict_psrr(p, f)) / np.abs(g))
    assert np.max(np.abs(err)) < 0.1


def test_a_flat_transfer_does_not_buy_a_feedthrough():
    """Keep-best: an extra knob must never be bought with noise."""
    import numpy as np
    from pmukit.fit.bias import _fit_gdd

    f = np.logspace(1, 9, 161)
    g = np.full(f.shape, 3.8e-5 + 0j)
    assert _fit_gdd(f, g)["c_ft"] is None


def test_a_rolling_transfer_still_finds_its_pole():
    import numpy as np
    from pmukit.fit.bias import _fit_gdd

    f = np.logspace(1, 9, 161)
    g = 1e-6 / (1 + 1j * f / 2e5)
    p = _fit_gdd(f, g)
    assert p["psrr_pole_hz"] == pytest.approx(2e5, rel=0.25)
    assert p["c_ft"] is None
