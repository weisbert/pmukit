"""The Zout ladder must RECOVER a known RLC, not merely match its curve.

Every case below synthesizes a Zout with known element values on the contract's 20-points-per-
decade grid, writes it into a real Dataset, fits it, and checks the numbers that come back.
"""
import numpy as np
import pytest

from pmukit.fit import zout
from tests.test_fit_common import (CELL, FREQ, RAIL, declare_ac, make, rel, zparams)


def write_zout(ds, Z, cell=None):
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    ds.put(f"ac_zout.{RAIL}", cell or CELL, Z)


# --------------------------------------------------------------------------- one branch


def test_single_branch_recovers_its_elements(tmp_path):
    true = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
    ds = make(tmp_path)
    write_zout(ds, zout.predict(true, f=FREQ))
    bf = zout.fit(ds, RAIL, CELL)

    assert not bf.missing
    assert bf.metric == "|Zout| dB RMS"
    assert bf.score < 0.2, bf.notes
    assert rel(bf.params["Ra"], true["Ra"]) < 0.02
    assert rel(bf.params["La"], true["La"]) < 0.02
    assert rel(bf.params["Cout"], true["Cout"]) < 0.02
    assert rel(bf.params["esr"], true["esr"]) < 0.02
    # branch B must stay OFF: it is keep-best gated, and one branch already explains the data
    assert bf.params["Rb"] == zout.RB_OFF
    assert bf.params["La_i"] == [] and bf.params["Rpl_i"] == []
    # Rpl at 1e5 is a damping resistor that barely damps -- the data cannot see it, and the
    # gate has to say so rather than presenting 65 kOhm as a measurement
    assert "Rpl" in bf.identifiability["unidentifiable"]


def test_resistive_plateau_is_one_degree_of_freedom(tmp_path):
    """Finite Rpl (a damped plateau) and Rpl -> inf (a resonant peak) are the SAME topology one
    knob apart, so the fitter must handle the damped case with no change of form.

    The element SPLIT is not asserted here: `Ra + sLa||Rpl` and `(Ra + sLa) || (Rb + sLb)`
    describe nearly the same curve over this band, so the keep-best gate may legitimately adopt
    the second branch.  What must hold is the response and the extracted capacitor.
    """
    true = zparams(Ra=0.05, La=2e-6, Rpl=30.0, Cout=1e-9, esr=0.5)
    ds = make(tmp_path)
    Z = zout.predict(true, f=FREQ)
    write_zout(ds, Z)
    bf = zout.fit(ds, RAIL, CELL)
    assert bf.score < 0.2
    peak_true = float(np.max(np.abs(Z)))
    peak_fit = float(np.max(np.abs(zout.predict(bf.params, f=FREQ))))
    assert abs(20 * np.log10(peak_fit / peak_true)) < 0.5   # the damped peak height is kept
    assert rel(bf.params["Ra"], true["Ra"]) < 0.02
    assert rel(bf.params["Cout"], true["Cout"]) < 0.10


def test_predict_reproduces_the_reported_score(tmp_path):
    """`predict` on the fitting grid must reproduce the input to the fit's OWN score: the Model
    screen's curves and the number next to them have to be the same computation."""
    true = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
    ds = make(tmp_path)
    Z = zout.predict(true, f=FREQ)
    write_zout(ds, Z)
    bf = zout.fit(ds, RAIL, CELL)
    model = zout.predict(bf.params, f=FREQ)
    again = float(np.sqrt(np.mean((20 * np.log10(np.abs(model) / np.abs(Z))) ** 2)))
    assert again == pytest.approx(bf.score, rel=1e-9, abs=1e-12)


# --------------------------------------------------------------------------- two branches


def test_second_branch_engages_and_recovers(tmp_path):
    true = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.2, Rb=8.0, Lb=3e-8)
    ds = make(tmp_path)
    write_zout(ds, zout.predict(true, f=FREQ))
    bf = zout.fit(ds, RAIL, CELL)

    assert bf.score < 0.2, bf.notes
    assert bf.params["Rb"] < zout.RB_OFF, "the second R-L branch should have engaged"
    assert any("second R-L branch engaged" in n for n in bf.notes)
    assert rel(bf.params["Ra"], true["Ra"]) < 0.02
    assert rel(bf.params["La"], true["La"]) < 0.02
    assert rel(bf.params["esr"], true["esr"]) < 0.05
    # the split between the two branches is only loosely determined, so the tolerance on the
    # SECOND branch is wider than on the first -- that is the honest statement, not 1 %
    assert rel(bf.params["Rb"], true["Rb"]) < 0.25
    assert rel(bf.params["Lb"], true["Lb"]) < 0.30


def test_higher_order_ladder_recovers_every_stage(tmp_path):
    """A multi-decade inductive rise: one (L||R) stage mislocates the corner, so the ladder is
    the fix -- and it must come back with the stages that were planted."""
    stages = [(2.4e-5, 60.0), (2.0e-6, 120.0)]
    s = 1j * 2 * np.pi * FREQ
    ZA = 0.1 + 0j * s
    for L, R in stages:
        ZA = ZA + (s * L * R) / (s * L + R)
    ds = make(tmp_path)
    write_zout(ds, ZA)
    bf = zout.fit(ds, RAIL, CELL)

    assert any("shelf gate" in n for n in bf.notes)
    assert any("higher-order ladder engaged" in n for n in bf.notes)
    assert len(bf.params["La_i"]) == 1 and len(bf.params["Rpl_i"]) == 1
    assert rel(bf.params["Ra"], 0.1) < 0.01
    assert rel(bf.params["La"], stages[0][0]) < 0.01
    assert rel(bf.params["Rpl"], stages[0][1]) < 0.01
    assert rel(bf.params["La_i"][0], stages[1][0]) < 0.01
    assert rel(bf.params["Rpl_i"][0], stages[1][1]) < 0.01
    assert bf.score < 0.1


# --------------------------------------------------------------------------- the floors


def test_non_passive_ground_truth_is_reported_as_a_floor(tmp_path):
    """A genuinely non-passive Zout cannot be reproduced by a passive RLC.  The residual is a
    documented FLOOR, and the block has to SAY so instead of looking like a bad fit."""
    true = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
    Z = np.asarray(zout.predict(true, f=FREQ))
    Z = Z - 0.25                                          # push Re(Z) negative at low frequency
    assert np.min(Z.real) < 0
    ds = make(tmp_path)
    write_zout(ds, Z)
    bf = zout.fit(ds, RAIL, CELL)
    assert any("NON-PASSIVE" in n for n in bf.notes), bf.notes
    assert any("FLOOR, not a bug" in n for n in bf.notes)
    # the model itself stays passive by construction, which is the point
    assert np.min(zout.predict(bf.params, f=FREQ).real) >= 0.0


def test_invisible_output_cap_is_named_not_guessed(tmp_path):
    """The documented under-determined case: an ESR so large the output capacitor is nearly
    invisible.  The gate must NAME it instead of presenting a confident wrong number, and the
    joint least-squares that would 'fix' it stays rejected."""
    true = zparams(Ra=1.0, La=5e-6, Rpl=200.0, Cout=1e-9, esr=100.0)
    ds = make(tmp_path)
    write_zout(ds, zout.predict(true, f=FREQ))
    bf = zout.fit(ds, RAIL, CELL)

    flagged = set(bf.identifiability["unidentifiable"]) | set(
        bf.identifiability["poorly_determined"])
    assert "Cout" in flagged, bf.identifiability
    assert any("identifiability" in n for n in bf.notes)
    # and the number really is wrong, which is why naming it matters
    assert rel(bf.params["Cout"], true["Cout"]) > 0.2


def test_ac_zout_is_still_fitted_when_the_cap_is_ideal(tmp_path):
    """A low-ESR bulk cap: the capacitive-band extraction is the accurate path."""
    true = zparams(Ra=0.2, La=5e-7, Rpl=1e5, Cout=4.7e-9, esr=0.05)
    ds = make(tmp_path)
    write_zout(ds, zout.predict(true, f=FREQ))
    bf = zout.fit(ds, RAIL, CELL)
    assert rel(bf.params["Cout"], true["Cout"]) < 0.02
    assert rel(bf.params["esr"], true["esr"]) < 0.02


# --------------------------------------------------------------------------- missing data


def test_never_run_cell_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    bf = zout.fit(ds, RAIL, CELL)
    assert bf.missing is True
    assert bf.params == {}
    assert np.isnan(bf.score)
    assert "never run" in bf.notes[0]


def test_marked_missing_cell_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    write_zout(ds, zout.predict(zparams(), f=FREQ))
    ds.mark_missing(f"ac_zout.{RAIL}", CELL, "run failed: engine exited 1")
    bf = zout.fit(ds, RAIL, CELL)
    assert bf.missing is True
    assert bf.params == {}
    assert "registered missing" in bf.notes[0]


def test_undeclared_variable_is_missing(tmp_path):
    ds = make(tmp_path)
    bf = zout.fit(ds, RAIL, CELL)
    assert bf.missing is True
    assert "never declared" in bf.notes[0]
