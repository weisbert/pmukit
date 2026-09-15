"""The driver: block ordering, per-block cell granularity, and the lossless round trip.

The digest re-emits the model from a `FitResult` and NOTHING else, so `to_dict()` /
`from_dict()` losing a number is the same thing as the model losing it.
"""
import numpy as np
import pytest

from pmukit import jsonio, spec
from pmukit.config import DerivedConfig
from pmukit.dataset import cell_key
from pmukit.errors import PmuError
from pmukit.fit import BlockFit, FitResult, fit_port, fit_project
from pmukit.fit import bias as bias_mod
from pmukit.fit import psrr as psrr_mod
from pmukit.fit import zout as zout_mod
from tests.test_fit_common import (BIAS, CELL, CELL_NOLOAD, FREQ, LOADS, RAIL,
                                   declare_ac, declare_ac_noload, make, zparams)

TWO_PI = 2 * np.pi
ZTRUE = zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5)
G = [1e-3, 5e-3, TWO_PI * 1e5, 0.0, 1e9, 0.0, 1e9]
VPIN = np.linspace(0.0, 1.0, 41)
DERIVED = DerivedConfig(supply={"nominal_v": 1.05}, rails={RAIL: {}},
                        biases={BIAS: {"vcomp_v": 0.4}})


def build(tmp_path, with_noise=True):
    dims = {"process": ["tt"], "temp_c": [25.0], "vset": [3], "load_a": {RAIL: LOADS[1:3]}}
    ds = make(tmp_path, dims)
    declare_ac(ds, f"ac_zout.{RAIL}", unit="ohm")
    declare_ac(ds, f"ac_psrr.{RAIL}", unit="V/V")
    if with_noise:
        declare_ac(ds, f"noise_v.{RAIL}", unit="V^2/Hz", dtype="float64")
    Z = zout_mod.predict(ZTRUE, f=FREQ)
    H = psrr_mod.psrr_model(FREQ, ZTRUE, G, None, 0.0)
    for il in LOADS[1:3]:
        cell = dict(CELL, load_a=il)
        ds.put(f"ac_zout.{RAIL}", cell, Z)
        ds.put(f"ac_psrr.{RAIL}", cell, H)
        if with_noise:
            In = np.sqrt(1e-18 + (3e-8) ** 2 / FREQ)
            ds.put(f"noise_v.{RAIL}", cell, (In * np.abs(Z)) ** 2)
    ds.declare(f"dc_iv.{BIAS}", dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", unit="A", coord=VPIN)
    declare_ac_noload(ds, f"ac_yout.{BIAS}", unit="S")
    ds.put(f"dc_iv.{BIAS}", CELL_NOLOAD,
           (5e-7 + 2e-9 * (VPIN - 0.4)) * bias_mod.gate(VPIN, 0.05, 2.0, "hi", 0.85))
    ds.put(f"ac_yout.{BIAS}", CELL_NOLOAD, 2e-9 + 1j * TWO_PI * FREQ * 1.5e-14)
    return ds


# --------------------------------------------------------------------------- fit_port


def test_fit_port_fits_every_rail_block_and_threads_one_zout(tmp_path):
    """ORDER IS PHYSICS: PSRR, noise and the large-signal replay all get the SAME fitted Zout,
    because they are the same impedance driven by different sources."""
    ds = build(tmp_path)
    fits = fit_port(ds, RAIL, "rail", CELL, DERIVED)
    assert set(fits) == {b.name for b in spec.blocks_for("rail")}
    assert not fits["zout"].missing and not fits["psrr"].missing
    zp = fits["zout"].params
    # the PSRR block must reproduce its score against THAT Zout, which is only true if it was
    # identified against it
    H = psrr_mod.psrr_model(FREQ, ZTRUE, G, None, 0.0)
    model = psrr_mod.predict(fits["psrr"].params, f=FREQ, zout=zp)
    again = float(np.sqrt(np.mean((20 * np.log10(np.abs(model) / np.abs(H))) ** 2)))
    assert again == pytest.approx(fits["psrr"].score, rel=1e-9, abs=1e-12)


def test_no_sink_is_an_emitter_constant_with_no_run(tmp_path):
    ds = build(tmp_path)
    fits = fit_port(ds, RAIL, "rail", CELL, DERIVED)
    ns = fits["no_sink"]
    assert ns.params == {} and ns.missing is False
    assert "emitter constant" in ns.notes[0]
    assert spec.block("no_sink", "rail").observables == ()


def test_tier_filter_drops_the_large_signal_blocks(tmp_path):
    ds = build(tmp_path)
    fits = fit_port(ds, RAIL, "rail", CELL, DERIVED, tiers=("hb",))
    assert "load_en" not in fits
    assert "zout" in fits and "dc" in fits


def test_fit_port_fits_every_bias_block(tmp_path):
    ds = build(tmp_path)
    fits = fit_port(ds, BIAS, "bias", CELL_NOLOAD, DERIVED)
    assert set(fits) == {"idc", "yout", "noise", "psrr"}
    assert not fits["idc"].missing and not fits["yout"].missing
    assert fits["noise"].missing and fits["psrr"].missing      # never run, not a bad fit
    assert fits["noise"].params == {}


# --------------------------------------------------------------------------- cells


def test_each_block_is_keyed_at_its_own_granularity(tmp_path):
    ds = build(tmp_path)
    res = fit_project(ds, DERIVED)
    idx = res.index()
    zout_cells = [k for k in idx if k[0] == RAIL and k[1] == "zout"]
    dc_cells = [k for k in idx if k[0] == RAIL and k[1] == "dc"]
    noise_cells = [k for k in idx if k[0] == RAIL and k[1] == "noise"]
    # Zout carries a load axis: one fit per load
    assert len({k[5] for k in zout_cells}) == 2
    # the DC block is continuous in temperature: its cell carries no temperature at all
    assert all(k[3] is None for k in dc_cells)
    # the noise block has no vset axis in the spec, so its cell does not carry one
    assert all(k[4] is None for k in noise_cells)
    assert all(bf.cell == {} or set(bf.cell) <= {"process", "temp_c", "vset", "load_a"}
               for bf in res.fits.values())


def test_a_block_is_not_refitted_once_per_load_when_it_has_no_load_axis(tmp_path):
    ds = build(tmp_path)
    seen = []
    fit_project(ds, DERIVED, on_event=lambda e: seen.append((e["port"], e["block"])))
    n_psrr_bias = sum(1 for p, b in seen if p == BIAS and b == "psrr")
    assert n_psrr_bias == 1                                    # one corner, one fit


def test_events_are_reported_for_progress(tmp_path):
    ds = build(tmp_path)
    seen = []
    res = fit_project(ds, DERIVED, on_event=seen.append)
    assert len(seen) == len(res.fits)
    assert set(seen[0]) == {"port", "block", "cell", "score", "metric", "missing"}


# --------------------------------------------------------------------------- the result


def test_fit_result_round_trips_through_json_losslessly(tmp_path):
    ds = build(tmp_path)
    res = fit_project(ds, DERIVED)
    text = jsonio.canon(res.to_dict())
    back = FitResult.from_dict(jsonio.read(jsonio.write(tmp_path / "fit.json",
                                                        res.to_dict())))
    assert jsonio.canon(back.to_dict()) == text
    assert back.sha() == res.sha()
    assert set(back.fits) == set(res.fits)
    for key, bf in res.fits.items():
        other = back.fits[key]
        assert other.port == bf.port and other.block == bf.block
        assert other.missing == bf.missing
        assert other.metric == bf.metric
        assert other.params == bf.to_dict()["params"]


def test_nan_and_infinity_survive_the_round_trip(tmp_path):
    """A `nan` score means 'not measured' and an infinite sigma means 'not determinable'.
    Strict JSON has neither, so both have to survive as something that reads back."""
    bf = BlockFit(port=RAIL, block="dc", cell={"process": "tt"},
                  params={"vout": 0.8, "dropout": None},
                  score=float("nan"), metric="vout % RMS",
                  identifiability={"cond": float("inf"), "sigma": {"vout_tc": float("inf")},
                                   "unidentifiable": ["vout_tc"]})
    res = FitResult(project="demo_pmu")
    res.add(bf)
    text = jsonio.canon(res.to_dict())
    assert "NaN" not in text and "Infinity" not in text
    back = FitResult.from_dict(res.to_dict())
    got = back.fits[bf.key]
    assert got.params["dropout"] is None
    assert got.identifiability["sigma"]["vout_tc"] == "inf"
    assert got.identifiability["unidentifiable"] == ["vout_tc"]


def test_from_dict_refuses_something_that_is_not_a_fit_result():
    with pytest.raises(PmuError):
        FitResult.from_dict({"nope": 1})


def test_missing_inventory_is_reported_not_hidden(tmp_path):
    ds = build(tmp_path, with_noise=False)
    res = fit_project(ds, DERIVED)
    missing = res.missing()
    assert any(bf.block == "noise" and bf.port == RAIL for bf in missing)
    from pmukit.fit import explain_missing
    text = explain_missing(res)
    assert f"{RAIL}/noise" in text
    assert all(bf.params == {} for bf in missing)


def test_worst_is_a_max_over_corners(tmp_path):
    ds = build(tmp_path)
    res = fit_project(ds, DERIVED)
    worst = res.worst("zout")
    assert worst is not None
    others = [bf.score for bf in res.fits.values()
              if bf.block == "zout" and not bf.missing]
    assert worst.score == max(others)


def test_parameter_names_match_the_spec_table_exactly(tmp_path):
    """The emitter and the Model screen read the SAME table, so a key that is not in
    `spec.block(...).params` would be a number nothing downstream knows how to place."""
    ds = build(tmp_path)
    res = fit_project(ds, DERIVED)
    for bf in res.fits.values():
        if bf.missing or not bf.params:
            continue
        ptype = res.ports.get(bf.port, "rail")
        allowed = {p.name for p in spec.block(bf.block, ptype).params}
        assert set(bf.params) <= allowed, (bf.port, bf.block, set(bf.params) - allowed)


def test_block_fit_key_is_stable_and_readable(tmp_path):
    bf = BlockFit(port=RAIL, block="zout", cell=dict(CELL))
    assert bf.key == f"{RAIL}/zout/{cell_key(CELL)}"
    assert "tt/25C/vset3" in bf.key


def test_no_module_under_fit_launches_a_process():
    """The Model screen's curves and scores come from `predict`, never from a simulator."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "pmukit" / "fit"
    banned = ("subprocess", "os.system", "os.popen", "os.spawn", "multiprocessing",
              "popen", "shutil.which")
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} mentions {token}"
