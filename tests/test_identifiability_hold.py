"""When may an unpinned parameter hold a green at yellow?  Only when it can MOVE THE PREDICTION
where the model is used (grades rule 2; METHODOLOGY, "Identifiability holds a grade only when the
freedom matters").

Three families, each synthesized from the blocks' OWN predict functions so the truth is known:

  * PINNED -- data that genuinely determines the parameter (a damped Zout peak, a PSRR with a
    real feedthrough floor, a noise spectrum with real Lorentzian bumps, a bias with a real
    compliance knee).  The gate does not flag it and the block is green.
  * UNPINNED AND INFLUENTIAL -- the envelope (the band the consumer declared) reaches past the
    data, and the parameter the data cannot see decides the prediction out there.  Held at
    yellow, and the hold names the parameter.
  * THE FOUR THAT HELD EVERY CELL OF THE FAKE DEMO -- Rpl, G0, amp_i[k], and the bias knee.
    Each is the fake DUT's own shape, and each one's decision is asserted with its reason.
"""
import numpy as np
import pytest

from pmukit.backends.fake import MODEL
from pmukit.config import DerivedConfig
from pmukit.fit import bias, noise, psrr, zout
from pmukit.fit import identifiability as ident
from pmukit.verify import grades as G
from tests.test_fit_common import (BIAS, CELL, CELL_NOLOAD, FREQ, NOISE_FREQ, RAIL, TEMPS,
                                   declare_ac, declare_ac_noload, make, zparams)

TWO_PI = 2 * np.pi
#: the contract's bands: AC 10 Hz .. care_up_to_hz, noise 10 Hz .. 100 MHz
DERIVED = DerivedConfig(freq={"start_hz": float(FREQ[0]), "stop_hz": float(FREQ[-1])},
                        noise={"start_hz": float(NOISE_FREQ[0]),
                               "stop_hz": float(NOISE_FREQ[-1])},
                        biases={BIAS: {"vcomp_v": 0.4}})

#: the fake engine's Zout: branch A (R_dc + sL) || (esr + 1/sC) -- NO damping resistor
_FZ = MODEL["zout"]
FAKE_Z = zparams(Ra=_FZ["r_dc_ohm"], La=_FZ["l_h"], Rpl=1e12, Cout=_FZ["c_f"], esr=_FZ["esr_ohm"])
#: the fake engine's coupling current: one real pole, no flat feedthrough (G0 = 0)
FAKE_IC = [0.0, MODEL["psrr"]["ic0_s"], TWO_PI * MODEL["psrr"]["fp_hz"], 0.0, 1e9, 0.0, 1e9]


def _verdict(bf):
    return G.block_verdict(bf)


# --------------------------------------------------------------------------- builders


def fit_zout(tmp_path, true, freq=FREQ):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm", coord=freq)
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(true, f=freq))
    return zout.fit(ds, RAIL, CELL, DERIVED)


def fit_psrr(tmp_path, G_ic, Z=FAKE_Z, freq=FREQ):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm", coord=freq)
    declare_ac(ds, f"ac_psrr.{RAIL}", unit="V/V", coord=freq)
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(Z, f=freq))
    ds.put(f"ac_psrr.{RAIL}", CELL, psrr.psrr_model(freq, Z, G_ic, None, 0.0))
    return psrr.fit(ds, RAIL, CELL, DERIVED, zout_params=Z)


def fit_noise(tmp_path, white, flicker, amps=(), corners=(), freq=NOISE_FREQ):
    Z = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm", coord=freq)
    declare_ac(ds, f"noise_v.{RAIL}", unit="V^2/Hz", dtype="float64", coord=freq)
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(Z, f=freq))
    In2 = white ** 2 + flicker ** 2 / freq
    for a, fk in zip(amps, corners):
        In2 = In2 + a ** 2 / (1.0 + (freq / fk) ** 2)
    ds.put(f"noise_v.{RAIL}", CELL, In2 * np.abs(zout.predict(Z, f=freq)) ** 2)
    return noise.fit_bank(ds, RAIL, CELL, DERIVED, zout_by_load={CELL["load_a"]: Z})[CELL["load_a"]]


def fit_idc(tmp_path, knee: bool):
    vpin = np.linspace(0.0, 1.0, 51)
    ds = make(tmp_path)
    ds.declare(f"dc_iv.{BIAS}", dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", unit="A", coord=vpin)
    declare_ac_noload(ds, f"ac_yout.{BIAS}", unit="S")
    for T in TEMPS:
        cell = dict(CELL_NOLOAD, temp_c=T)
        I = 5e-7 + 1.2e-9 * (T - 25.0) + 2e-9 * (vpin - 0.4)
        if knee:
            I = I * bias.gate(vpin, 0.05, 2.0, "hi", 0.85)
        ds.put(f"dc_iv.{BIAS}", cell, I)
        ds.put(f"ac_yout.{BIAS}", cell, 2e-9 + 1j * TWO_PI * FREQ * 1.5e-14)
    return bias.fit_idc(ds, BIAS, CELL_NOLOAD, DERIVED)


def flagged_by_gate(bf):
    idn = bf.identifiability
    return set(idn.get("unidentifiable") or []) | set(idn.get("poorly_determined") or [])


# --------------------------------------------------------------------------- the gate itself


def test_the_envelope_grid_is_the_data_grid_unless_the_band_reaches_past_it():
    f = np.logspace(1, 6, 101)
    assert np.array_equal(ident.envelope_grid(f, None), f)
    assert np.array_equal(ident.envelope_grid(f, (10.0, 1e6)), f)
    wide = ident.envelope_grid(f, (1.0, 1e9))
    assert wide[0] == pytest.approx(1.0) and wide[-1] == pytest.approx(1e9)
    assert set(f) <= set(wide)                        # the data's own points are all in it
    assert ident.envelope_band(DERIVED, "freq") == (FREQ[0], FREQ[-1])
    assert ident.envelope_band(DERIVED, "noise") == (NOISE_FREQ[0], NOISE_FREQ[-1])
    assert ident.envelope_band(None) is None
    assert ident.envelope_band(DerivedConfig()) is None


def test_a_free_parameter_the_band_covers_is_measured_harmless():
    """`faint` is the gate's own poorly-determined example.  Over the band the data covers, no
    move the data allows can shift the prediction by more than the data tolerance."""
    f = np.logspace(1, 8, 141)

    def g(p, f=f):
        return p[0] + 1j * TWO_PI * f * p[1] + 5e-12 * p[2] * np.ones_like(f)

    res = ident.gate(g, ["g0", "Cp", "faint"], [2e-9, 1.5e-14, 1.0], envelope=g)
    assert res["poorly_determined"] == ["faint"]
    assert res["envelope_beyond_data"] is False
    assert 0.0 <= res["influence_db"]["faint"] <= ident.DATA_TOL_DB


def test_a_feedthrough_the_band_stops_short_of_is_measured_influential():
    """A flat coupling term 60 dB under a pole that has rolled off by the band's top edge: the
    data barely sees it, and a decade and more above the band it IS the answer."""
    f = np.logspace(1, 6, 101)
    fe = ident.envelope_grid(f, (10.0, 1e9))

    def g(p, f=f):
        s = 1j * TWO_PI * f
        return p[0] + p[1] / (1 + s / (TWO_PI * p[2]))

    res = ident.gate(g, ["G0", "G1", "fp"], [1e-9, 2e-4, 1e4], envelope=lambda p: g(p, fe),
                     off=["G0", "G1"])
    assert res["envelope_beyond_data"] is True
    assert res["influence_db"]["G0"] > 10.0, res["influence_db"]
    # ... and with no envelope there is no measurement at all: the conservative reading
    assert "influence_db" not in ident.gate(g, ["G0", "G1", "fp"], [1e-9, 2e-4, 1e4])


# --------------------------------------------------------------------------- the grade rule


def _bf(**kw):
    from tests.test_verify_grades import bf
    return bf(**kw)


def test_a_flag_measured_harmless_does_not_hold_but_an_unmeasured_one_still_does():
    free = {"unidentifiable": ["Rpl"], "influence_db": {"Rpl": 0.3}}
    assert G.grade_block(_bf(score=0.04, params={"Ra": 5.0, "Rpl": 1e4}, ident=free))[0] == \
        "green"
    # the SAME flag with no measurement behind it keeps the strict rule
    assert G.grade_block(_bf(score=0.04, params={"Ra": 5.0, "Rpl": 1e4},
                             ident={"unidentifiable": ["Rpl"]}))[0] == "yellow"


def test_a_flag_whose_influence_passes_the_green_limit_holds():
    v = _verdict(_bf(score=0.04, params={"Ra": 5.0, "Rpl": 1e4},
                     ident={"poorly_determined": ["Rpl"], "influence_db": {"Rpl": 2.2}}))
    assert v["grade"] == "yellow" and v["held"] and v["held_by"] == ["Rpl"]


def test_the_limit_is_the_metrics_own_green_row():
    """Noise is judged against its own (looser, 2 dB) green row, not the spectral 1 dB one."""
    ident_ = {"unidentifiable": ["amp_i[0]"], "influence_db": {"amp_i[0]": 1.5}}
    params = {"amp_i": [1e-9]}
    assert G.grade_block(_bf(block="noise", metric="Sv dB RMS", score=0.01, params=params,
                             ident=ident_))[0] == "green"
    assert G.grade_block(_bf(block="zout", metric="|Zout| dB RMS", score=0.01, params=params,
                             ident=ident_))[0] == "yellow"


def test_an_unflagged_parameter_that_decides_the_envelope_holds_too():
    """The column/sigma tests can call a parameter pinned when the band sees it at the 1 %
    level; if it moves the envelope past the green limit, it is not pinned where it matters."""
    v = _verdict(_bf(block="noise", metric="Sv dB RMS", score=0.01,
                     params={"white": 2e-9, "flicker": 2e-8},
                     ident={"unidentifiable": [], "influence_db": {"flicker": 12.0,
                                                                   "white": 0.4}}))
    assert v["held"] and v["held_by"] == ["flicker"]


def test_an_undetected_knee_is_switched_off_not_unpinned():
    params = {"idc": 1e-5, "knee_side": "none", "vknee": 1.0, "knee_p": 1.0, "vhi": 1.0}
    for name in ("vknee", "knee_p", "vhi"):
        assert G._inert(name, params)
        assert not G._inert(name, dict(params, knee_side="hi"))
    assert G.grade_block(_bf(block="idc", metric="I-V % of plateau RMS", score=0.09,
                             params=params,
                             ident={"unidentifiable": ["vknee", "knee_p", "vhi"]}))[0] == "green"


# --------------------------------------------------------------------------- pinned -> green


def test_pinned_zout_damping_resistor_is_green(tmp_path):
    """A damped peak (Rpl = 300 ohm across 2 uH): the peak height pins Rpl."""
    bf = fit_zout(tmp_path, zparams(Ra=0.05, La=2e-6, Rpl=300.0, Cout=1e-9, esr=0.5))
    assert "Rpl" not in flagged_by_gate(bf), bf.identifiability
    assert _verdict(bf)["grade"] == "green"


def test_pinned_psrr_feedthrough_is_green(tmp_path):
    """A real flat coupling floor under the pole: the HF plateau of i_c pins G0."""
    bf = fit_psrr(tmp_path, [2e-6] + FAKE_IC[1:])
    assert "G0" not in flagged_by_gate(bf), bf.identifiability
    assert _verdict(bf)["grade"] == "green"


def test_pinned_noise_lorentzians_are_green(tmp_path):
    """Two real Lorentzian bumps on white + 1/f: the sections that carry them are pinned."""
    bf = fit_noise(tmp_path, 1e-9, 3e-8, amps=(2e-8, 5e-9), corners=(1e3, 1e5))
    corners = np.asarray(bf.params["corner_i_hz"])
    for fk in (1e3, 1e5):
        k = int(np.argmin(np.abs(np.log(corners / fk))))
        assert f"amp_i[{k}]" not in flagged_by_gate(bf), (k, bf.identifiability)
    assert _verdict(bf)["grade"] == "green"


def test_pinned_bias_knee_is_green(tmp_path):
    bf = fit_idc(tmp_path, knee=True)
    assert bf.params["knee_side"] == "hi"
    assert _verdict(bf)["grade"] == "green", bf.identifiability


# --------------------------------------------------------------------------- unpinned + influential


def test_a_feedthrough_above_a_short_psrr_band_holds_the_grade(tmp_path):
    """PSRR measured to 1 MHz, the consumer's band to 1 GHz, and a -160 dB flat feedthrough
    the band cannot see that IS the coupling above ~100 MHz.  A perfect in-band fit."""
    low = FREQ[FREQ <= 1e6]
    bf = fit_psrr(tmp_path, [1e-8] + FAKE_IC[1:], freq=low)
    v = _verdict(bf)
    assert v["band"] == "green"
    assert v["grade"] == "yellow" and v["held"] and "G0" in v["held_by"], bf.identifiability
    assert bf.identifiability["influence_db"]["G0"] > G.LIMITS["PSRR dB RMS"].green


def test_a_flicker_corner_below_a_short_noise_band_holds_the_grade(tmp_path):
    """Noise measured from 10 kHz, used from 10 Hz, flicker corner at 100 Hz: the band sees the
    1/f term at the 1 % level and the envelope's low end is 10 dB of it."""
    high = NOISE_FREQ[NOISE_FREQ >= 1e4]
    bf = fit_noise(tmp_path, 2e-9, 2e-9 * np.sqrt(1e2), freq=high)
    v = _verdict(bf)
    assert v["band"] == "green"
    assert v["grade"] == "yellow" and v["held_by"] == ["flicker"], bf.identifiability


def test_an_esr_above_a_short_zout_band_holds_the_grade(tmp_path):
    """The fake DUT's Zout measured to 100 MHz, used to 1 GHz: the ESR sets the floor only
    above ~500 MHz, so the band leaves it loose and the envelope's top decade is decided by it."""
    bf = fit_zout(tmp_path, FAKE_Z, freq=FREQ[FREQ <= 1e8])
    v = _verdict(bf)
    assert v["band"] == "green"
    assert v["grade"] == "yellow" and "esr" in v["held_by"], bf.identifiability


def test_the_near_invisible_cap_is_still_named_but_cannot_move_a_covered_band(tmp_path):
    """The documented under-determined rail (ESR so large the cap is nearly invisible).  The
    gate still NAMES Cout and the number is still wrong -- but with the sweep covering the whole
    envelope, every Cout the data allows gives the same |Z| to under the data tolerance, so the
    grade no longer holds on it.  Stretch the envelope past the data and the hold would return
    (the three tests above)."""
    bf = fit_zout(tmp_path, zparams(Ra=1.0, La=5e-6, Rpl=200.0, Cout=1e-9, esr=100.0))
    assert "Cout" in flagged_by_gate(bf)
    assert bf.identifiability["envelope_beyond_data"] is False
    assert bf.identifiability["influence_db"]["Cout"] <= ident.DATA_TOL_DB
    assert _verdict(bf)["grade"] == "green"


# --------------------------------------------------------------------------- the demo's four


def test_rpl_on_the_fake_zout_is_free_and_powerless(tmp_path):
    """DECISION: release.  The fake DUT has no damping resistor (Rpl -> inf); the fit parks it
    somewhere in 1e4..1e9, every value of which gives the same Zout to a fraction of a dB over
    the whole band.  The gate still NAMES it; the grade no longer holds on it."""
    bf = fit_zout(tmp_path, FAKE_Z)
    assert "Rpl" in flagged_by_gate(bf)
    assert bf.identifiability["influence_db"]["Rpl"] <= ident.DATA_TOL_DB
    assert _verdict(bf)["grade"] == "green"
    assert any("influence over the envelope" in n for n in bf.notes)


def test_g0_on_the_fake_psrr_is_free_and_powerless(tmp_path):
    """DECISION: release.  The fake coupling current has no flat term; the fit's G0 is ~0, and
    the relative Jacobian calls a ~0 number unpinned even though the data bounds it in
    ABSOLUTE terms far below anything the band can see."""
    bf = fit_psrr(tmp_path, FAKE_IC)
    assert _verdict(bf)["grade"] == "green", bf.identifiability
    infl = bf.identifiability.get("influence_db") or {}
    assert infl.get("G0", 0.0) <= ident.DATA_TOL_DB


def test_amp_i_on_the_fake_noise_is_free_and_powerless(tmp_path):
    """DECISION: release.  The fake spectrum is white + 1/f exactly; the model's explicit
    flicker term carries all of it, so the Lorentzian bank is redundant and every section is
    buried under the total -- quiet relative to the NOISE, not merely to the loudest section."""
    bf = fit_noise(tmp_path, MODEL["noise_v"]["white_i_rthz"],
                   MODEL["noise_v"]["white_i_rthz"] * np.sqrt(MODEL["noise_v"]["corner_hz"]))
    assert any(n.startswith("amp_i[") for n in flagged_by_gate(bf))
    assert all(v <= ident.DATA_TOL_DB for v in bf.identifiability["influence_db"].values())
    assert _verdict(bf)["grade"] == "green"


def test_the_knee_of_a_knee_less_bias_is_not_asked_about(tmp_path):
    """DECISION: a fitter defect, fixed at the fitter.  With no knee detected the gate is
    identically 1 and the emitter does not write vknee / knee_p / vhi at all -- so they are not
    gated, and `idc` is the only number the I-V law carries."""
    bf = fit_idc(tmp_path, knee=False)
    assert bf.params["knee_side"] == "none"
    assert not ({"vknee", "knee_p", "vhi"} & flagged_by_gate(bf)), bf.identifiability
    assert _verdict(bf)["grade"] == "green"
