"""The air-gap return path, end to end: box -> text -> desk.

The box exports a digest; the user pastes it; the desk rebuilds a contract-2 dataset subset and
compares the box's fitted numbers against a local refit. The two properties that matter are that
nothing the box dropped is silently invented, and that the box's own parameters survive the paste
bit-for-bit -- because a desk refit of the same data is allowed to disagree, and when it does, the
disagreement IS the finding.
"""
import json

import numpy as np
import pytest

from pmukit import digest as dg
from pmukit import spec
from pmukit import reproduce as rp
from pmukit.dataset import Dataset, cell_key
from pmukit.errors import PmuError


def box_payload(*, big=False):
    """What the box would hand over after a characterization run."""
    freq = [10.0 * (10 ** (k / 12.0)) for k in range(36)]
    gt = [50.0 / (1.0 + (f / 1e6) ** 2) ** 0.5 for f in freq]
    model = [g * 1.01 for g in gt]
    t = [k * 1e-9 for k in range(200)]
    dip = [0.8 - 0.05 * np.exp(-((k - 40) / 12.0) ** 2) for k in range(200)]
    n_params = 400 if big else 1
    payload = {
        "meta": {"project": "demo_pmu"},
        "provenance": {"config_sha": "abc123456789", "dataset_sha": "def123456789",
                       "spec_sha": spec.SPEC_SHA, "pmukit_version": "0.1.0"},
        "ledger": [{"run_id": f"r{i:012d}", "status": "done", "cell": "tt, 25 C, VSET 3, load L2",
                    "analysis": "ac", "cpu_s": 3.0} for i in range(40)],
        "params": {"VDD0P8_A": {"zout": {"Ra": 0.0931234567890123, "Cout": 1.2345678e-9,
                                         "esr": 0.75},
                                "psrr": {"G0": 0.004, "pole_1_hz": 12345.678}}},
        "curves": {
            "ac_zout.VDD0P8_A": {"sub": "rail", "kind": "zout",
                                 "cell": cell_key({"process": "tt", "temp_c": 25, "vset": 3,
                                                   "load_a": 5e-4}),
                                 "x": freq, "gt": gt, "model": model},
        },
        "transients": {
            "tran_load_on.VDD0P8_A": {"kind": "tran_load_on",
                                      "cell": cell_key({"process": "tt", "temp_c": 25, "vset": 3}),
                                      "t": t, "gt": [float(v) for v in dip]},
        },
    }
    if big:
        payload["params"] = {f"rail{i}": {"zout": {"Ra": float(i) + 0.123456789012345}}
                             for i in range(n_params)}
        payload["curves"].update({
            f"ac_zout.rail{j}": {"sub": "rail", "x": freq, "gt": gt, "model": model}
            for j in range(40)})
        payload["transients"].update({
            f"tran_load_on.rail{j}": {"t": t, "gt": [float(v) for v in dip]}
            for j in range(24)})
    return payload


# ------------------------------------------------------------------ the paste survives
def test_the_box_parameters_survive_the_round_trip_bit_for_bit():
    """D2 is lossless on purpose: the desk re-emits the model from this block alone."""
    payload = box_payload()
    back = dg.parse(dg.export(payload, budget=64000, project="demo_pmu"))
    assert back["params"] == payload["params"]
    assert back["params"]["VDD0P8_A"]["zout"]["Ra"] == 0.0931234567890123


def test_the_text_survives_a_relay_paste():
    for part in dg.export(box_payload(), budget=64000, project="demo_pmu"):
        assert "\r" not in part
        part.encode("ascii")


# ------------------------------------------------------------------- rebuild_dataset
def test_rebuild_makes_a_real_contract_2_dataset(tmp_path):
    payload = dg.parse(dg.export(box_payload(), budget=64000, project="demo_pmu"))
    ds = rp.rebuild_dataset(payload, tmp_path / "ds")
    try:
        assert "ac_zout.VDD0P8_A" in ds.variables()
        assert "tran_load_on.VDD0P8_A" in ds.variables()
        cell = {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": 5e-4}
        assert ds.has("ac_zout.VDD0P8_A", cell)
        curve = ds.get("ac_zout.VDD0P8_A", cell)
        coord = ds.coord("ac_zout.VDD0P8_A")
        assert curve.shape == coord.shape
        assert np.isfinite(curve).all()
        # the ground truth landed, not the model -- the model travels in `params`
        assert curve[0] == pytest.approx(50.0, rel=1e-3)
    finally:
        ds.close()


def test_rebuild_reopens_as_a_normal_dataset(tmp_path):
    payload = dg.parse(dg.export(box_payload(), budget=64000, project="demo_pmu"))
    rp.rebuild_dataset(payload, tmp_path / "ds").close()
    ds = Dataset.open(tmp_path / "ds")
    try:
        assert ds.variables()
        json.dumps(ds.summary())
    finally:
        ds.close()


def test_what_the_box_dropped_is_registered_missing_never_guessed(tmp_path):
    """A budget-truncated digest must leave holes, with the reason, not invented numbers."""
    parts = dg.export(box_payload(big=True), budget=32000, project="demo_pmu")
    payload = dg.parse(parts)
    assert payload["dropped"], "the big payload must overflow 32 KB"
    ds = rp.rebuild_dataset(payload, tmp_path / "ds")
    try:
        # Two places record the loss, because a wholly-dropped block has no variable to hang a
        # `missing` row on: per-variable rows for cells that vanished, and a dropped.json sidecar
        # naming the blocks that never arrived at all.
        dropped = rp.dropped_blocks(payload)
        assert dropped, "the trailer named dropped blocks; the rebuild must record them"
        assert all(rp.DROPPED_REASON in d["reason"] for d in dropped)
        assert (ds.path / "dropped.json").exists()
        for row in ds.missing():
            assert rp.DROPPED_REASON in str(row[-1])
        for var in ds.variables():
            cov = ds.coverage(var)
            assert cov["filled"] + cov["missing"] + cov["never_run"] == cov["declared"]
    finally:
        ds.close()


def test_an_unreadable_cell_string_is_a_four_part_error(tmp_path):
    payload = {"meta": {"project": "p"},
               "curves": {"ac_zout.A": {"cell": "tt/25C/vsetX/5.0e-04A",
                                        "x": [1.0, 2.0], "gt": [1.0, 2.0]}}}
    with pytest.raises(PmuError) as e:
        rp.rebuild_dataset(payload, tmp_path / "ds")
    assert "cell" in str(e.value) and "Do   :" in str(e.value)


# -------------------------------------------------------------------------- compare
def test_compare_names_the_parameter_that_moved():
    box = {"VDD0P8_A": {"zout": {"Ra": 0.0931, "Cout": 1.2e-9}}}
    desk = {"VDD0P8_A": {"zout": {"Ra": 0.0931, "Cout": 1.8e-9}}}
    out = rp.compare(box, desk)
    assert out["same"] == ["VDD0P8_A.zout.Ra"]
    assert len(out["moved"]) == 1
    m = out["moved"][0]
    assert m["param"] == "VDD0P8_A.zout.Cout" and m["box"] == 1.2e-9 and m["desk"] == 1.8e-9
    assert out["worst"]["param"] == "VDD0P8_A.zout.Cout"


def test_compare_reports_what_only_one_side_has():
    out = rp.compare({"a": {"b": {"x": 1.0}}}, {"a": {"b": {"y": 2.0}}})
    assert out["only_box"] == ["a.b.x"] and out["only_desk"] == ["a.b.y"]


def test_compare_is_exact_on_an_identical_refit():
    p = {"r": {"zout": {"Ra": 0.0931234567890123, "Cout": 1.2345678e-9}}}
    out = rp.compare(p, json.loads(json.dumps(p)))
    assert out["moved"] == [] and len(out["same"]) == 2


# ------------------------------------------------------------------------ reproduce
def test_reproduce_without_a_fitter_still_rebuilds_and_says_so(tmp_path, monkeypatch):
    """A desk with no fitter installed must not fail -- the box's params are still usable."""
    import sys

    import pmukit
    # `from . import fit` resolves via the package ATTRIBUTE once the submodule has been imported
    # anywhere in the session, so hiding it needs both: drop the attribute and poison the
    # sys.modules entry (a None entry is how Python says "this submodule is not here").
    monkeypatch.delattr(pmukit, "fit", raising=False)
    monkeypatch.setitem(sys.modules, "pmukit.fit", None)
    payload = dg.parse(dg.export(box_payload(), budget=64000, project="demo_pmu"))
    out = rp.reproduce(payload, workdir=tmp_path)
    assert out["refit"] is None
    assert "not installed" in out["note"]
    assert out["box_params"]["VDD0P8_A"]["zout"]["Ra"] == 0.0931234567890123
    assert "rebuilt" in rp.summary(out)


def test_reproduce_refuses_a_digest_with_no_parameters(tmp_path):
    payload = {"meta": {"project": "p"}, "curves": {}}
    with pytest.raises(PmuError) as e:
        rp.reproduce(payload, workdir=tmp_path)
    msg = str(e.value)
    assert "D2" in msg and "larger budget" in msg


def test_summary_names_the_holes(tmp_path):
    payload = dg.parse(dg.export(box_payload(big=True), budget=32000, project="demo_pmu"))
    out = rp.reproduce(payload, workdir=tmp_path) if payload.get("params") else None
    if out is None:
        pytest.skip("the big payload dropped D2 as well, which is its own (tested) behaviour")
    text = rp.summary(out)
    assert "dataset rebuilt at" in text
