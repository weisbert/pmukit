"""PSRR: identify the coupling current, realize it, and flag what would break a coupled HB run.

The ground truth is synthesized from the block's OWN realizable form -- a signed real-pole bank
plus one signed complex-conjugate section -- so "did the port survive" is a question about
numbers, not about a stored curve.
"""
import numpy as np
import pytest

from pmukit.fit import psrr, zout
from tests.test_fit_common import (CELL, FREQ, RAIL, declare_ac, make, rel, zparams)

TWO_PI = 2 * np.pi
ZTRUE = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)


def write(ds, H, Z=None):
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    declare_ac(ds, f"ac_psrr.{RAIL}", unit="V/V")
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(ZTRUE, f=FREQ) if Z is None else Z)
    ds.put(f"ac_psrr.{RAIL}", CELL, H)


# --------------------------------------------------------------------------- recovery


def test_minimum_phase_shelf_is_recovered_and_the_complex_section_stays_inert(tmp_path):
    G = [1e-3, 5e-3, TWO_PI * 1e5, 0.0, 1e9, 0.0, 1e9]
    ds = make(tmp_path)
    write(ds, psrr.psrr_model(FREQ, ZTRUE, G, None, 0.0))
    bf = psrr.fit(ds, RAIL, CELL, zout_params=ZTRUE)

    assert not bf.missing
    assert bf.score < 0.02, bf.notes
    assert rel(bf.params["G0"], 1e-3) < 0.01
    assert rel(bf.params["G_i"][0], 5e-3) < 0.01
    assert rel(bf.params["pole_i_hz"][0], 1e5) < 0.01
    # a minimum-phase rail must come back on the shelf with the complex section switched off
    assert bf.params["pc_gain"] == 0.0 and bf.params["pc_zero"] == 0.0
    assert any("kept the shelf candidate" in n for n in bf.notes)


def test_complex_conjugate_section_is_recovered(tmp_path):
    G = [2e-3, -6e-3, TWO_PI * 2e4, 3e-3, TWO_PI * 3e6, 0.0, 1e9]
    Q = (4e-3, 2e-10, TWO_PI * 1.2e6, 3.0)
    ds = make(tmp_path)
    write(ds, psrr.psrr_model(FREQ, ZTRUE, G, Q, 0.0))
    bf = psrr.fit(ds, RAIL, CELL, zout_params=ZTRUE)

    assert bf.score < 0.05, bf.notes
    assert any("kept the complex candidate" in n for n in bf.notes)
    assert rel(bf.params["pc_w0"], Q[2]) < 0.02
    assert rel(bf.params["pc_q"], Q[3]) < 0.05
    assert rel(bf.params["pc_gain"], Q[0]) < 0.05
    assert rel(bf.params["G0"], G[0]) < 0.05
    # the real sections come back signed, which is the whole point of a SIGNED bank
    assert bf.params["G_i"][0] < 0.0 and bf.params["G_i"][1] > 0.0
    assert rel(bf.params["pole_i_hz"][0], G[2] / TWO_PI) < 0.05
    assert rel(bf.params["pole_i_hz"][1], G[4] / TWO_PI) < 0.05


def test_predict_reproduces_the_reported_score(tmp_path):
    G = [2e-3, -6e-3, TWO_PI * 2e4, 3e-3, TWO_PI * 3e6, 0.0, 1e9]
    Q = (4e-3, 2e-10, TWO_PI * 1.2e6, 3.0)
    H = psrr.psrr_model(FREQ, ZTRUE, G, Q, 0.0)
    ds = make(tmp_path)
    write(ds, H)
    bf = psrr.fit(ds, RAIL, CELL, zout_params=ZTRUE)
    model = psrr.predict(bf.params, f=FREQ, zout=ZTRUE)
    again = float(np.sqrt(np.mean((20 * np.log10(np.abs(model) / np.abs(H))) ** 2)))
    assert again == pytest.approx(bf.score, rel=1e-9, abs=1e-12)


def test_the_block_is_scored_on_the_realized_form_not_an_analytic_residual(tmp_path):
    """Ranking a notch fit by its ANALYTIC residual is rejected: a pure-real fit can look better
    on paper and realize with a huge phase error.  The reported score therefore has to be the
    REALIZED model against the measurement, which is exactly what `predict` recomputes above."""
    G = [1e-3, 5e-3, TWO_PI * 1e5, 0.0, 1e9, 0.0, 1e9]
    H = psrr.psrr_model(FREQ, ZTRUE, G, None, 0.0)
    ds = make(tmp_path)
    write(ds, H)
    bf = psrr.fit(ds, RAIL, CELL, zout_params=ZTRUE)
    realized = psrr.predict(bf.params, f=FREQ, zout=ZTRUE)
    assert bf.score == pytest.approx(
        float(np.sqrt(np.mean((20 * np.log10(np.abs(realized) / np.abs(H))) ** 2))),
        rel=1e-9, abs=1e-12)


# --------------------------------------------------------------------------- the HB detector


def test_doublet_detector_fires_on_a_planted_near_cancelling_pair():
    """The emit-facing rule: a large near-cancelling first-order pair is perfect in AC and makes
    a COUPLED harmonic-balance Jacobian singular.  It has to be flagged for the emitter."""
    notes = psrr.doublet_notes(1e-3, [17.23, -17.23, 0.0],
                               [1.0e5, 1.00004e5, 1e9])
    assert any("near-cancelling first-order doublet" in n for n in notes), notes
    assert any("gm-C biquad" in n for n in notes)
    assert any("large first-order residue" in n for n in notes)


def test_doublet_detector_is_quiet_on_an_ordinary_bank():
    assert psrr.doublet_notes(1e-3, [5e-3, -2e-3, 0.0], [1e4, 1e6, 1e9]) == []


def test_a_large_single_residue_is_flagged_even_without_a_partner():
    notes = psrr.doublet_notes(1e-3, [3.4, 0.0, 0.0], [1e5, 1e9, 1e9])
    assert any("large first-order residue" in n for n in notes)
    assert not any("doublet" in n for n in notes)


# --------------------------------------------------------------------------- plumbing


def test_missing_psrr_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    declare_ac(ds, f"ac_psrr.{RAIL}", unit="V/V")
    ds.put(f"ac_zout.{RAIL}", CELL, zout.predict(ZTRUE, f=FREQ))
    bf = psrr.fit(ds, RAIL, CELL, zout_params=ZTRUE)
    assert bf.missing is True and bf.params == {}


def test_psrr_without_zout_says_so_when_zout_is_missing(tmp_path):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    declare_ac(ds, f"ac_psrr.{RAIL}", unit="V/V")
    G = [1e-3, 5e-3, TWO_PI * 1e5, 0.0, 1e9, 0.0, 1e9]
    ds.put(f"ac_psrr.{RAIL}", CELL, psrr.psrr_model(FREQ, ZTRUE, G, None, 0.0))
    bf = psrr.fit(ds, RAIL, CELL)                      # no zout handed in, and none measured
    assert bf.missing is True
    assert "i_c = H/Zout" in bf.notes[0]
