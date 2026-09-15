"""The rail DC block: the vout table, the continuous temperature law, dropout and ilimit.

The honesty case matters as much as the recovery case: a load sweep that never leaves regulation
must report `ilimit = None` with the reason, not a number.  A synthetic flat DC stand-in that
invented a dropout and a load-regulation slope with no flag at the model boundary is a REJECTED
practice, and this is where that rejection is enforced.
"""
import numpy as np
import pytest

from pmukit.config import DerivedConfig
from pmukit.fit import dc
from tests.test_fit_common import CELL, LOADS, RAIL, TEMPS, make, rel

TSWEEP = np.linspace(-40.0, 125.0, 12)
VREG, RLOAD, TC = 0.8, 0.2, -1.5e-5       # 0.8 V, 0.2 ohm of load regulation, -15 uV/degC
SUPPLY = 1.05
DERIVED = DerivedConfig(supply={"nominal_v": SUPPLY}, rails={RAIL: {}})


def vout(il, T, ilimit=None):
    v = VREG - RLOAD * il + TC * (T - 25.0)
    if ilimit is not None and il > ilimit:
        v = v * (1.0 - 4.0 * (il - ilimit) / ilimit)          # the regulation collapse
    return v


def build(tmp_path, loads=None, ilimit=None):
    loads = loads or LOADS
    dims = {"process": ["tt"], "temp_c": TEMPS, "vset": [3], "load_a": {RAIL: loads}}
    ds = make(tmp_path, dims)
    ds.declare(f"dc_load.{RAIL}", dims=("process", "temp_c", "vset", "load_a"),
               dtype="float64", unit="V")
    ds.declare(f"dc_temp.{RAIL}", dims=("process", "vset", "load_a", "temp_sweep_c"),
               dtype="float64", unit="V", coord=TSWEEP)
    for T in TEMPS:
        for il in loads:
            ds.put(f"dc_load.{RAIL}", {"process": "tt", "temp_c": T, "vset": 3, "load_a": il},
                   vout(il, T, ilimit))
    for il in loads:
        ds.put(f"dc_temp.{RAIL}", {"process": "tt", "vset": 3, "load_a": il},
               np.array([vout(il, t, ilimit) for t in TSWEEP]))
    return ds


def test_vout_and_its_temperature_coefficient_are_recovered(tmp_path):
    ds = build(tmp_path)
    for il in LOADS:
        bf = dc.fit(ds, RAIL, dict(CELL, load_a=il), DERIVED)
        assert not bf.missing
        assert bf.score < 0.01, bf.notes                      # % of vout
        assert rel(bf.params["vout"], vout(il, dc.T_REF_C)) < 1e-6
        assert rel(bf.params["vout_tc"], TC) < 1e-6
    assert any("dc_temp sweep" in n for n in bf.notes)


def test_temperature_is_continuous_inside_the_corner(tmp_path):
    """The cell carries no temperature: the DC law is fitted against the SWEEP, which is what
    lets one corner section cover the whole range without interpolating between corners."""
    ds = build(tmp_path)
    bf = dc.fit(ds, RAIL, CELL, DERIVED)
    assert "temp_c" not in bf.cell
    assert bf.cell == {"process": "tt", "vset": 3, "load_a": CELL["load_a"]}
    for T in (-40.0, 0.0, 25.0, 85.0, 125.0):
        assert rel(dc.predict(bf.params, T=T), vout(CELL["load_a"], T)) < 1e-6


def test_predict_reproduces_the_reported_score(tmp_path):
    ds = build(tmp_path)
    bf = dc.fit(ds, RAIL, CELL, DERIVED)
    gt = np.array([vout(CELL["load_a"], t) for t in TSWEEP])
    model = dc.predict(bf.params, T=TSWEEP)
    again = float(np.sqrt(np.mean(((model - gt) / bf.params["vout"]) ** 2)) * 100.0)
    assert again == pytest.approx(bf.score, rel=1e-6, abs=1e-12)


def test_load_regulation_shows_up_as_a_vout_table(tmp_path):
    """`vout` has a load axis, so the table IS the load regulation: it must move with load."""
    ds = build(tmp_path)
    vals = [dc.fit(ds, RAIL, dict(CELL, load_a=il), DERIVED).params["vout"] for il in LOADS]
    slope = np.polyfit(np.asarray(LOADS, float), np.asarray(vals, float), 1)[0]
    assert rel(-slope, RLOAD) < 1e-6


def test_current_limit_not_reached_is_reported_as_none(tmp_path):
    ds = build(tmp_path)                                       # no collapse in the swept range
    bf = dc.fit(ds, RAIL, CELL, DERIVED)
    assert bf.params["ilimit"] is None
    assert bf.params["dropout"] is None
    assert any("NOT reached in the swept range" in n for n in bf.notes), bf.notes
    assert any("must not invent them" in n for n in bf.notes)


def test_current_limit_and_dropout_are_measured_when_the_rail_collapses(tmp_path):
    loads = [2e-6, 1e-4, 5e-4, 1e-3, 2e-3, 4e-3]
    ilimit = 1.5e-3
    ds = build(tmp_path, loads=loads, ilimit=ilimit)
    bf = dc.fit(ds, RAIL, dict(CELL, load_a=5e-4), DERIVED)
    assert bf.params["ilimit"] is not None
    # the knee can only be located between the two swept points that bracket it: a coarse load
    # grid bounds this number, and the fitter must not pretend otherwise
    assert 1e-3 <= bf.params["ilimit"] <= 2e-3
    assert ilimit == 1.5e-3                                    # the planted value is in there
    assert bf.params["dropout"] is not None
    assert 0.0 < bf.params["dropout"] < SUPPLY


def test_dropout_needs_a_supply_and_says_so_when_there_is_none(tmp_path):
    loads = [2e-6, 1e-4, 5e-4, 1e-3, 2e-3, 4e-3]
    ds = build(tmp_path, loads=loads, ilimit=1.5e-3)
    bf = dc.fit(ds, RAIL, dict(CELL, load_a=5e-4), DerivedConfig())
    assert bf.params["ilimit"] is not None
    assert bf.params["dropout"] is None
    assert any("supply voltage is unknown" in n for n in bf.notes)


def test_a_single_temperature_reports_a_zero_slope_out_loud(tmp_path):
    dims = {"process": ["tt"], "temp_c": [25.0], "vset": [3], "load_a": {RAIL: LOADS}}
    ds = make(tmp_path, dims)
    ds.declare(f"dc_load.{RAIL}", dims=("process", "temp_c", "vset", "load_a"),
               dtype="float64", unit="V")
    for il in LOADS:
        ds.put(f"dc_load.{RAIL}", {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": il},
               vout(il, 25.0))
    bf = dc.fit(ds, RAIL, CELL, DERIVED)
    assert bf.params["vout_tc"] == 0.0
    assert any("only one characterized temperature" in n for n in bf.notes)
    assert "vout_tc" in bf.identifiability["unidentifiable"]


def test_missing_dc_is_missing_not_a_bad_fit(tmp_path):
    ds = make(tmp_path)
    ds.declare(f"dc_load.{RAIL}", dims=("process", "temp_c", "vset", "load_a"),
               dtype="float64", unit="V")
    bf = dc.fit(ds, RAIL, CELL, DERIVED)
    assert bf.missing is True and bf.params == {}
