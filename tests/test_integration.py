"""Cross-module integration on the real synthetic PMU fixture.

The unit tests prove each contract on its own; this one proves the seams between them, on the
same deck the simulation backend runs: netlist -> pins -> config 0a/0b -> plan -> ledger -> dataset
-> deliverable -> digest -> back.

Everything the simulator would produce is stood in for analytically here, and clearly labelled as
such. The point is the interfaces, not the numbers -- the numbers are the runner's and the fitter's
acceptance, not this file's.
"""
import json
import math
import pathlib

import numpy as np
import pytest

from pmukit import deliverable as dlv
from pmukit import digest as dg
from pmukit import spec
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.ledger import Ledger, Recipe
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan, load_states
from pmukit.site import SiteConfig

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"

CONFIG = {
    "project": "demo_pmu",
    "netlist": str(FIXTURE),
    "pmu_inst": "PMU_TOP",
    "corners": ["tt", "ss"],
    "temps_c": [-40, 25, 125],
    "vset_codes": [3],
    "state_note": "synthetic fixture, nominal state",
    # VDD0P8_C is a stub on purpose: it must cost zero simulations and still reach the deliverable.
    "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
              "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
              "EN": "model", "TESTMODE": "ignore"},
    "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True},
                "VDD0P8_B": {"on_a": 2e-3, "off_a": 5e-6, "switches": True}},
    "care_up_to_hz": 1e9,
}


@pytest.fixture(scope="module")
def parsed():
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict(CONFIG)
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, SiteConfig(engine="spectre_ssh"))
    return cfg, der, nl, pins


@pytest.fixture(scope="module")
def plan(parsed):
    cfg, der, nl, pins = parsed
    return compile_plan(cfg, der, nl, pins, site=SiteConfig(engine="spectre_ssh"))


# -------------------------------------------------------------- netlist -> pins -> config
def test_the_fixture_satisfies_its_own_convention(parsed):
    _cfg, _der, _nl, pins = parsed
    roles = {p.name: p.role for p in pins.pins.values()}
    assert roles["VDDA_1V0"] == "supply"
    assert roles["VDD0P8_A"] == roles["VDD0P8_B"] == roles["VDD0P8_C"] == "rail"
    assert roles["IB_PTAT"] == roles["IB_POLY"] == "bias"
    assert roles["EN"] == "en"
    assert roles["TESTMODE"] == "none"                 # deliberately role-less
    assert [p.name for p in pins.unclassified()] == ["TESTMODE"]


def test_split_grounds_come_out_of_the_subcircuit_wiring(parsed):
    """The fixture wires all three grounds to 0 in the bench; the association is INSIDE the PMU."""
    _cfg, _der, _nl, pins = parsed
    by = {p.name: p for p in pins.pins.values()}
    assert by["VDD0P8_A"].gnd == "VSS_A"
    assert by["VDD0P8_B"].gnd == "VSS_B"
    assert by["IB_PTAT"].gnd == by["IB_POLY"].gnd == "AGND"
    assert sorted(p.name for p in pins.grounds()) == ["AGND", "VSS_A", "VSS_B"]


def test_derived_config_explains_every_field(parsed):
    _cfg, der, _nl, _pins = parsed
    d = der.to_dict()
    for key in ("process", "temps_c", "dc_temp_sweep", "vset", "freq", "noise", "grouping"):
        assert "provenance" in d[key], key
    assert der.freq["points_per_decade"] == 20
    assert (der.noise["start_hz"], der.noise["stop_hz"]) == (10, 1e8)
    # the documented footgun: the AC band must NOT leak into the transient edge
    for rail, tw in der.transient.items():
        assert tw["edge_s"] <= 1e-6, rail
        assert tw["tstop_s"] > tw["edge_s"] * 10


def test_a_stub_port_is_carried_but_never_simulated(parsed, plan):
    _cfg, der, _nl, _pins = parsed
    assert "VDD0P8_C" in der.stubs
    assert der.stubs["VDD0P8_C"]["emit"] == "vsource"      # a rail stub is an ideal voltage source
    assert "VDD0P8_C" not in der.rails
    for r in plan.runs(enabled_only=False):
        assert not any(v.endswith(".VDD0P8_C") for v in r.run.reads)


# --------------------------------------------------------------------------- plan -> ledger
def test_the_plan_covers_every_modelled_port_and_nothing_else(plan, parsed):
    _cfg, der, _nl, _pins = parsed
    read_ports = {v.split(".", 1)[1] for r in plan.runs(enabled_only=False) for v in r.run.reads}
    assert read_ports == set(der.rails) | set(der.biases) | set(der.en)


def test_one_supply_injection_reads_every_port(plan):
    g = [g for g in plan.groups if g.id.startswith("ac:VS_")][0]
    reads = {v for r in g.runs for v in r.run.reads}
    assert reads == {"ac_psrr.VDD0P8_A", "ac_psrr.VDD0P8_B",
                     "ac_psrr.IB_PTAT", "ac_psrr.IB_POLY"}


def test_every_netlist_variant_is_self_consistent(plan, parsed):
    """Each run's netlist must carry its own corner, code, temperature and load state."""
    _cfg, der, _nl, _pins = parsed
    for r in plan.runs(enabled_only=False)[:60]:
        txt = r.netlist_text
        assert f"section={r.run.process}" in txt
        assert f"parameters VSET={r.run.vset}" in txt
        if not math.isnan(r.run.temp_c):
            assert f"temp={r.run.temp_c:g}" in txt
        if r.run.load_key:
            state = next(s for s in plan.states if s.key == r.run.load_key)
            for rail, amps in state.currents.items():
                src = der.rails[rail]["src"]
                assert f"{src} (" in txt
                line = next(ln for ln in txt.splitlines() if ln.startswith(src + " "))
                if "type=pwl" not in line:
                    assert f"dc={amps:g}" in line


def test_commit_then_resume(plan, tmp_path):
    led = Ledger(tmp_path / "runs.sqlite")
    first = plan.commit(led)
    assert first["new"] == len(plan.runs()) and first["cached"] == 0
    for r in plan.runs():
        led.set_status(r.run_id, "done", cpu_seconds=2.0, finished=True)
    second = plan.commit(led)
    assert second["cached"] == len(plan.runs()) and second["new"] == 0
    # and the ledger can still say why each of them existed
    rid = plan.runs()[0].run_id
    assert led.consumers(rid)
    assert "because" in led.why(rid).lower()
    led.close()


def test_recipe_round_trips_and_names_the_remote_command(plan):
    r = plan.group("ac:VS_VDDA_1V0").runs[0]
    rec = Recipe.parse(r.run.recipe)
    assert rec.analyses and rec.edits
    assert "source ~/.cshrc" in rec.submit          # the Cadence env lives only in ~/.cshrc
    assert "spectre -64" in rec.submit              # -64 or ahdlcmi builds -m32 and dies


# -------------------------------------------------------------------- plan -> dataset shape
def test_the_plan_and_the_dataset_agree_on_the_axes(plan, parsed, tmp_path):
    cfg, der, _nl, _pins = parsed
    dims = {"process": der.process["corners"],
            "temp_c": der.temps_c["points"],
            "vset": der.vset["codes"],
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(tmp_path / "ds", project=cfg.project, config_sha=cfg.sha(), dims=dims)

    freq = np.array(der.freq_points())
    ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", coord=freq, coord_name="freq_ac")
    ds.declare("ac_psrr.IB_PTAT", dims=("process", "temp_c", "vset", "freq_hz"),
               dtype="complex128", coord=freq, coord_name="freq_ac")

    # every cell the plan would produce must be addressable in the dataset
    states = {s.key: s for s in load_states(der)}
    written = 0
    for r in plan.runs(enabled_only=False):
        for var in r.run.reads:
            if var not in ("ac_zout.VDD0P8_A", "ac_psrr.IB_PTAT"):
                continue
            cell = {"process": r.run.process, "vset": r.run.vset}
            if not math.isnan(r.run.temp_c):
                cell["temp_c"] = r.run.temp_c
            else:
                continue
            if var == "ac_zout.VDD0P8_A":
                cell["load_a"] = states[r.run.load_key].of("VDD0P8_A")
            # a stand-in for the simulator: a known one-pole impedance, NOT a measurement
            z = 1.0 / (1.0 / 50.0 + 2j * np.pi * freq * 1e-9)
            ds.put(var, cell, z.astype("complex128"))
            written += 1
    assert written > 0
    for var in ("ac_zout.VDD0P8_A", "ac_psrr.IB_PTAT"):
        cov = ds.coverage(var)
        assert cov["filled"] > 0
        assert cov["filled"] + cov["missing"] + cov["never_run"] == cov["declared"]
    ds.close()

    reopened = Dataset.open(tmp_path / "ds")
    assert reopened.sha() == Dataset.open(tmp_path / "ds").sha()
    reopened.close()


def test_a_cell_that_never_ran_is_distinguishable_from_one_that_broke(parsed, tmp_path):
    cfg, der, _nl, _pins = parsed
    ds = Dataset.create(tmp_path / "ds2", project=cfg.project, config_sha=cfg.sha(),
                        dims={"process": ["tt", "ss"], "temp_c": [25.0], "vset": [3],
                              "load_a": {"VDD0P8_A": [5e-4]}})
    ds.declare("noise_v.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="float64", coord=np.array([10.0, 100.0]), coord_name="freq_noise")
    cell = {"process": "ss", "temp_c": 25.0, "vset": 3, "load_a": 5e-4}
    ds.mark_missing("noise_v.VDD0P8_A", cell, "run failed: convergence at 125 C")
    cov = ds.coverage("noise_v.VDD0P8_A")
    assert cov["missing"] == 1 and cov["never_run"] == 1 and cov["filled"] == 0
    rows = ds.missing("noise_v.VDD0P8_A")
    assert rows and "convergence" in rows[0][-1]
    ds.close()


# ------------------------------------------------------------------ deliverable -> digest
def test_deliverable_and_digest_round_trip(parsed, plan, tmp_path):
    cfg, der, _nl, _pins = parsed
    prov = dlv.Provenance(config_sha=cfg.sha(), dataset_sha="deadbeef", spec_sha=spec.SPEC_SHA,
                          pmukit_version="0.1.0", created="2026-09-16T00:00:00Z",
                          tb_state_note=cfg.state_note)
    env = dlv.Envelope(freq_max_hz=cfg.care_up_to_hz,
                       load_a={r: (min(der.loads[r]["points_a"]), max(der.loads[r]["points_a"]))
                               for r in der.rails},
                       temp_c=(min(cfg.temps_c), max(cfg.temps_c)),
                       corners=cfg.corner_names(), vset_codes=cfg.vset_codes,
                       ls_default_on=[])
    w = dlv.DeliverableWriter(cfg.project, root=tmp_path, stamp="20260916-000000")
    for corner in cfg.corner_names():
        w.add_va(corner, f"// stand-in body for {corner}\n", provenance=prov)
    w.write_scs(provenance=prov)
    w.write_envelope(env)
    w.write_provenance(prov)
    grades = [dlv.Grade(port=r, corner=c, block="zout", grade="green", detail="within 0.4 dB",
                        score=0.4)
              for r in der.rails for c in cfg.corner_names()]
    w.write_report(envelope=env, grades=grades, hb_check=None,
                   not_run=["tran_en (EN ramp): not requested"],
                   stubs=list(der.stubs))
    path = w.finish()

    d = dlv.Deliverable.open(path)
    assert d.provenance.spec_sha == spec.SPEC_SHA
    report = d.read_file("report.md")
    assert "VDD0P8_C" in report                       # the stub is named as not modelled
    for corner in cfg.corner_names():
        assert f"section {corner}" in d.read_file(f"PMU_{cfg.project}.scs") or \
               f"section {corner} " in d.read_file(f"PMU_{cfg.project}.scs")
    # an axis outside the envelope is named, not silently extrapolated
    ok, why = env.contains(freq_hz=1e11)
    assert not ok and why

    payload = {
        "meta": {"project": cfg.project},
        "provenance": {"config_sha": cfg.sha(), "spec_sha": spec.SPEC_SHA},
        "ledger": [{"run_id": r.run_id, "status": "done", "cell": r.run.cell_text(),
                    "analysis": r.run.analysis, "cpu_s": 2.0}
                   for r in plan.runs()[:25]],
        "params": {"VDD0P8_A": {"zout": {"Ra": 0.0931234567890123, "Cout": 1.234e-9}}},
        "grades": [{"port": g.port, "corner": g.corner, "block": g.block, "grade": g.grade}
                   for g in grades],
    }
    parts = dg.export(payload, budget=64000, project=cfg.project)
    back = dg.parse(parts)
    assert back["params"] == payload["params"]        # D2 is lossless -- the desk re-emits from it
    assert back["meta"]["project"] == cfg.project
    for part in parts:
        assert "\r" not in part
        part.encode("ascii")                         # must survive a relay paste


def test_a_digest_over_budget_names_what_it_dropped(plan):
    payload = {
        "meta": {"project": "demo_pmu"},
        "provenance": {"config_sha": "abc"},
        "ledger": [{"run_id": r.run_id, "status": "done", "cell": r.run.cell_text(),
                    "analysis": r.run.analysis, "cpu_s": 1.0}
                   for r in plan.runs(enabled_only=False)],
        "params": {f"p{i}": {"block": {"Ra": float(i) + 0.123456789012345}}
                   for i in range(900)},
        "curves": {f"ac_zout.rail{j}": {"sub": "rail", "x": [10.0 * k for k in range(400)],
                                        "gt": [float(k) for k in range(400)],
                                        "model": [float(k) + 0.01 for k in range(400)]}
                   for j in range(40)},
        "transients": {f"tran_load_on.rail{j}": {"t": [1e-9 * k for k in range(800)],
                                                 "gt": [float(k) for k in range(800)]}
                       for j in range(24)},
    }
    # `estimate` reports the size AFTER truncation, so the proof that the payload overflows is
    # that it names blocks it had to drop.
    est = dg.estimate(payload, dg.BLOCKS, 32000)
    assert est["dropped"], "the payload must be big enough to overflow a 32 KB budget"
    parts = dg.export(payload, budget=32000, project="demo_pmu")
    trailer = parts[-1]
    back = dg.parse(parts)
    assert back["dropped"], "something must have been dropped at 32 KB"
    for name in back["dropped"]:
        assert name in trailer                        # never a silent truncation
    assert "provenance" in back                       # D0 is never the thing that gets dropped


def test_spec_sha_pins_the_version_a_deliverable_came_from():
    a = spec.SPEC_SHA
    assert isinstance(a, str) and len(a) == 12
    assert spec.SPEC_SHA == a


def test_json_safe_everywhere(plan):
    json.dumps(plan.to_rows())
    json.dumps(plan.cost_summary())
    json.dumps(plan.consequences())
