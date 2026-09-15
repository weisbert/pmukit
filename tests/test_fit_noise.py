"""Rail noise: recover a known white + 1/f + Lorentzian spectrum from the LOG domain.

The measurement written into the dataset is a PSD (`V^2/Hz`, contract 2's unit) and the fit
works in AMPLITUDE, so this also pins the unit handling: getting that wrong would square the
whole spectrum silently.
"""
import numpy as np
import pytest

from pmukit.fit import noise, zout
from tests.test_fit_common import (CELL, NOISE_FREQ, RAIL, declare_ac, make, rel, zparams)

ZTRUE = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
CORNERS = [1e3, 1e5]
TRUE = {1e-4: {"white": 1e-9, "flicker": 3e-8, "amps": [2e-8, 5e-9]},
        5e-4: {"white": 2e-9, "flicker": 5e-8, "amps": [4e-8, 8e-9]}}


def in_amplitude(f, t):
    """The planted Norton current amplitude In(f) [A/rtHz]."""
    In2 = t["white"] ** 2 + t["flicker"] ** 2 / f
    for a, fk in zip(t["amps"], CORNERS):
        In2 = In2 + a ** 2 / (1.0 + (f / fk) ** 2)
    return np.sqrt(In2)


def build(tmp_path):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm", coord=NOISE_FREQ)
    declare_ac(ds, f"noise_v.{RAIL}", unit="V^2/Hz", dtype="float64", coord=NOISE_FREQ)
    Z = zout.predict(ZTRUE, f=NOISE_FREQ)
    for il, t in TRUE.items():
        cell = dict(CELL, load_a=il)
        ds.put(f"ac_zout.{RAIL}", cell, Z)
        sv = in_amplitude(NOISE_FREQ, t) * np.abs(Z)
        ds.put(f"noise_v.{RAIL}", cell, sv ** 2)        # contract 2 stores a PSD
    return ds


def test_bank_recovers_white_flicker_and_the_shared_corners(tmp_path):
    ds = build(tmp_path)
    bank = noise.fit_bank(ds, RAIL, CELL, zout_by_load={il: ZTRUE for il in TRUE})

    corners = None
    for il, t in TRUE.items():
        bf = bank[il]
        assert not bf.missing
        assert bf.params["nmode"] == "norton"
        assert bf.score < 0.2, bf.notes
        assert rel(bf.params["white"], t["white"]) < 0.05
        assert rel(bf.params["flicker"], t["flicker"]) < 0.10
        # the planted Lorentzians must be in the bank, at their amplitudes
        for fk, amp in zip(CORNERS, t["amps"]):
            k = int(np.argmin(np.abs(np.asarray(bf.params["corner_i_hz"]) - fk)))
            assert rel(bf.params["corner_i_hz"][k], fk) < 0.05
            assert rel(bf.params["amp_i"][k], amp) < 0.10
        if corners is None:
            corners = list(bf.params["corner_i_hz"])
        else:
            # SHARED corner frequencies across the loads: only the amplitudes may move
            assert bf.params["corner_i_hz"] == corners


def test_predict_reproduces_the_reported_score(tmp_path):
    ds = build(tmp_path)
    bank = noise.fit_bank(ds, RAIL, CELL, zout_by_load={il: ZTRUE for il in TRUE})
    bf = bank[5e-4]
    sv = in_amplitude(NOISE_FREQ, TRUE[5e-4]) * np.abs(zout.predict(ZTRUE, f=NOISE_FREQ))
    model = noise.predict(bf.params, f=NOISE_FREQ, zout=ZTRUE)
    again = float(np.sqrt(np.mean((20 * np.log10((model + 1e-30) / (sv + 1e-30))) ** 2)))
    assert again == pytest.approx(bf.score, rel=1e-9, abs=1e-12)


def test_the_block_says_it_is_decoupled_in_synthesis_only(tmp_path):
    """'Decoupled' is an algebraic round trip, not a physical separation: a Zout error leaks
    identically into the noise.  The block has to say so where the number is read."""
    ds = build(tmp_path)
    bf = noise.fit(ds, RAIL, CELL, zout_by_load={il: ZTRUE for il in TRUE})
    assert any("decoupled in SYNTHESIS, not in physics" in n for n in bf.notes)


def test_a_zout_error_moves_the_noise_by_the_same_amount(tmp_path):
    """The round trip, demonstrated: scaling |Zout| by 1 dB moves the reconstructed Sv by 1 dB.
    This is the documented physics, not a defect to be 'fixed'."""
    ds = build(tmp_path)
    bank = noise.fit_bank(ds, RAIL, CELL, zout_by_load={il: ZTRUE for il in TRUE})
    bf = bank[5e-4]
    good = noise.predict(bf.params, f=NOISE_FREQ, zout=ZTRUE)
    wrong = noise.predict(bf.params, f=NOISE_FREQ,
                          zout=dict(ZTRUE, Ra=ZTRUE["Ra"] * 1.1, Rpl=ZTRUE["Rpl"] * 1.1))
    lf = NOISE_FREQ < 1e2
    assert np.allclose(20 * np.log10(wrong[lf] / good[lf]),
                       20 * np.log10(1.1), atol=0.05)


def test_hybrid_realization_is_carried_and_reconstructs(tmp_path):
    """The hybrid form once fitted correctly and was never EMITTED, so the whole 1/f tail
    vanished from a deployed model.  `nmode` must therefore round-trip through the parameters
    and drive the reconstruction, not just sit in a log line."""
    p = {"nmode": "hybrid", "white": 1e-9, "flicker": 1e-5,
         "corner_i_hz": [1e3, 1e5], "amp_i": [2e-7, 5e-8]}
    sv_hy = noise.sv_model(p, NOISE_FREQ, ZTRUE)
    sv_no = noise.sv_model(dict(p, nmode="norton"), NOISE_FREQ, ZTRUE)
    assert not np.allclose(sv_hy, sv_no)

    # the hybrid really is the documented two-term form: a SERIES voltage bank riding the
    # branch-A divider Zout/ZA, plus the kept Norton white floor through |Zout|
    Z = zout.predict(ZTRUE, f=NOISE_FREQ)
    ZA = ZTRUE["Ra"] + (1j * 2 * np.pi * NOISE_FREQ * ZTRUE["La"] * ZTRUE["Rpl"]) / \
        (1j * 2 * np.pi * NOISE_FREQ * ZTRUE["La"] + ZTRUE["Rpl"])
    vn2 = p["flicker"] ** 2 / NOISE_FREQ
    for a, fk in zip(p["amp_i"], p["corner_i_hz"]):
        vn2 = vn2 + a ** 2 / (1.0 + (NOISE_FREQ / fk) ** 2)
    want = np.sqrt(vn2 * np.abs(Z / ZA) ** 2 + p["white"] ** 2 * np.abs(Z) ** 2)
    assert np.allclose(sv_hy, want, rtol=1e-12)

    # and it still carries a 1/f tail: dropping the flicker term must change the answer
    flat = noise.sv_model(dict(p, flicker=0.0), NOISE_FREQ, ZTRUE)
    assert sv_hy[0] / flat[0] > 10.0


def test_missing_noise_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm", coord=NOISE_FREQ)
    declare_ac(ds, f"noise_v.{RAIL}", unit="V^2/Hz", dtype="float64", coord=NOISE_FREQ)
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(ZTRUE, f=NOISE_FREQ))
    bf = noise.fit(ds, RAIL, CELL, zout_by_load={5e-4: ZTRUE})
    assert bf.missing is True and bf.params == {}
