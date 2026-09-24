"""The plan compiler: the smallest set of simulations, and why each one exists.

Everything here is synthetic. The properties that matter are: AC superposition really merges the
rail and bias PSRR into ONE supply injection; every run carries the parameters it feeds; the
run_id is a content hash so a re-plan of an unchanged run collides (that is resume); and
un-ticking a group names exactly what stops being measured.
"""
import math

import pytest

from pmukit import spec
from pmukit.config import DerivedConfig, ProjectConfig, derive
from pmukit.errors import PmuError
from pmukit.ledger import Ledger, Recipe, make_run_id
from pmukit.netlist import Netlist
from pmukit.plan import (Plan, compile_plan, default_cost, load_states, measured_cost,
                         nominal_state)
from pmukit.site import SiteConfig

DEMO = """\
simulator lang=spectre
parameters VSET=3
include "pdk/toplevel.scs" section=tt
include "pdk/rc.scs" section=typ

subckt pmu_demo (vdda a b ptat poly en tm vssa vssb agnd)
    ma1 (a na vdda vdda) pmos w=40u l=0.5u
    ra1 (a fb1) resistor r=100k
    rb1 (fb1 vssa) resistor r=100k
    mb1 (b nb vdda vdda) pmos w=20u l=0.5u
    rb2 (b fb2) resistor r=80k
    rb3 (fb2 vssb) resistor r=80k
    mp1 (ptat np agnd agnd) nmos w=10u l=1u
    mp2 (poly np agnd agnd) nmos w=10u l=1u
    men (na en vdda vdda) pmos w=2u l=1u
    mtm (tm agnd agnd agnd) nmos w=1u l=1u
ends pmu_demo

PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B IB_PTAT IB_POLY EN TESTMODE 0 0 0) pmu_demo
VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0
IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u
IL_VDD0P8_B (VDD0P8_B 0) isource dc=2m
VB_IB_PTAT (IB_PTAT 0) vsource dc=0.4
VB_IB_POLY (IB_POLY 0) vsource dc=0.4
VEN_EN (EN 0) vsource dc=1.0
Rtm (TESTMODE 0) resistor r=1meg

dcOp dc
"""

CFG = {
    "project": "demo_pmu",
    "netlist": "tb/input.scs",
    "pmu_inst": "PMU_TOP",
    "corners": ["tt", "ss"],
    "temps_c": [-40, 25, 125],
    "vset_codes": [3],
    "state_note": "synthetic demo",
    "ports": {"a": "model", "b": "model", "ptat": "model", "poly": "model",
              "vdda": "model", "en": "model", "tm": "ignore"},
    "my_load": {"a": {"on_a": 5e-4, "off_a": 2e-6, "switches": True},
                "b": {"on_a": 2e-3, "off_a": 5e-6, "switches": True}},
    "care_up_to_hz": 1e9,
}


@pytest.fixture
def parts():
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict(CFG)
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    der = derive(cfg, pins, SiteConfig(engine="spectre_ssh"))
    return cfg, der, nl, pins


@pytest.fixture
def plan(parts):
    cfg, der, nl, pins = parts
    return compile_plan(cfg, der, nl, pins, site=SiteConfig(engine="spectre_ssh"))


# --------------------------------------------------------------------------- load states
def test_load_states_walk_every_rail_together(parts):
    _cfg, der, _nl, _pins = parts
    states = load_states(der)
    assert [s.key for s in states] == ["L0", "L1", "L2", "L3"]
    # state k takes the k-th point of each rail's own grid: [off, 0.2*on, on, 2*on]
    assert states[0].of("a") == pytest.approx(2e-6)
    assert states[0].of("b") == pytest.approx(5e-6)
    assert states[2].of("a") == pytest.approx(5e-4)
    assert states[3].of("b") == pytest.approx(4e-3)
    assert "a=" in states[2].label and "b=" in states[2].label     # never a bare "L2"


def test_nominal_state_is_the_testbench_load(parts):
    _cfg, der, _nl, _pins = parts
    s = nominal_state(load_states(der), der)
    assert s.of("a") == pytest.approx(5e-4)        # IL_VDD0P8_A dc
    assert s.of("b") == pytest.approx(2e-3)


def test_a_rail_with_a_shorter_grid_holds_its_last_point():
    der = DerivedConfig(rails={"a": {}, "b": {}},
                        loads={"a": {"points_a": [1e-6, 1e-4]},
                               "b": {"points_a": [1e-5, 1e-3, 5e-3]}})
    st = load_states(der)
    assert len(st) == 3
    assert st[2].of("a") == pytest.approx(1e-4)    # held
    assert st[2].of("b") == pytest.approx(5e-3)


# ------------------------------------------------------------------- the superposition merge
def test_rail_and_bias_psrr_are_one_supply_injection(plan):
    """The whole point of AC superposition: one supply injection reads every port."""
    ac_supply = [g for g in plan.groups if g.id.startswith("ac:VS_")]
    assert len(ac_supply) == 1
    g = ac_supply[0]
    reads = set(g.runs[0].run.reads)
    # both the rails' PSRR and the biases' PSRR come out of this single run
    assert "ac_psrr.a" in reads and "ac_psrr.b" in reads
    assert "ac_psrr.ptat" in reads and "ac_psrr.poly" in reads
    # ...and it is genuinely one simulation, not four
    assert len({r.run_id for r in g.runs}) == g.n_runs


def test_zout_is_one_injection_per_rail(plan):
    ids = sorted(g.id for g in plan.groups if g.id.startswith("ac:IL_"))
    assert ids == ["ac:IL_VDD0P8_A", "ac:IL_VDD0P8_B"]
    g = plan.group("ac:IL_VDD0P8_A")
    assert all(r.run.reads == ["ac_zout.a"] for r in g.runs)


def test_dc_temp_is_one_sweep_reading_every_port(plan):
    g = plan.group("dc_temp")
    reads = set(g.runs[0].run.reads)
    assert {"dc_temp.a", "dc_temp.b", "dc_temp.ptat", "dc_temp.poly"} <= reads
    # temperature is swept INSIDE the run, so there is no run PER temperature...
    assert all(r.run.temp_c != r.run.temp_c for r in g.runs)          # NaN == "T swept"
    # ...but the rail's vout(T) does depend on load, so the family walks the load states
    assert g.n_runs == len(CFG["corners"]) * len(CFG["vset_codes"]) * 4
    assert {r.run.load_key for r in g.runs} == {"L0", "L1", "L2", "L3"}
    assert g.runs[0].run.cell_text().split(", ")[1] == "T swept"
    assert "param=temp" in g.runs[0].run.recipe


# ------------------------------------------------------------------------------- cell axes
def test_load_dependent_observables_run_at_every_load_state(plan):
    g = plan.group("ac:IL_VDD0P8_A")
    keys = {r.run.load_key for r in g.runs}
    assert keys == {"L0", "L1", "L2", "L3"}


def test_bias_admittance_does_not_sweep_the_load(plan):
    g = plan.group("ac:VB_IB_PTAT")
    assert {r.run.load_key for r in g.runs} == {""} or len({r.run.load_key for r in g.runs}) == 1
    assert g.n_runs == len(CFG["corners"]) * len(CFG["temps_c"])


def test_every_observable_in_the_spec_is_planned_or_explained(plan):
    planned = set()
    for r in plan.runs(enabled_only=False):
        for v in r.run.reads:
            planned.add(v.split(".", 1)[0])
    for pt in ("rail", "bias", "en"):
        for req in spec.requirements(pt):
            assert req.observable in planned, f"{req.observable} is in the spec but never planned"


def test_no_sink_never_produces_a_run(plan):
    # no_sink is an emitter constant -- it has no observable, so it must cost nothing
    assert all("no_sink" not in r.run.analysis for r in plan.runs(enabled_only=False))
    b = spec.block("no_sink", "rail")
    assert b.observables == ()


# ------------------------------------------------------------------------------ why / feeds
def test_every_run_knows_which_parameters_it_feeds(plan):
    for r in plan.runs(enabled_only=False):
        assert r.feeds, f"{r.run.analysis} {r.run.stimulus} feeds nothing"
        for port, block, param in r.feeds:
            assert spec.block(block, "rail" if port in ("a", "b")
                              else "en" if port == "en" else "bias")
            assert param


def test_why_names_the_port_block_and_param(plan):
    r = plan.group("ac:IL_VDD0P8_A").runs[0]
    why = r.why()
    assert "a needs" in why and "zout." in why and "ac_zout.a" in why


# ------------------------------------------------------------------------------- run ids
def test_run_id_is_a_content_hash_so_a_replan_collides(parts):
    cfg, der, nl, pins = parts
    p1 = compile_plan(cfg, der, nl, pins, site=SiteConfig())
    p2 = compile_plan(cfg, der, nl, pins, site=SiteConfig())
    assert [r.run_id for r in p1.runs()] == [r.run_id for r in p2.runs()]


def test_changing_a_corner_changes_the_run_ids(parts):
    cfg, der, nl, pins = parts
    p1 = compile_plan(cfg, der, nl, pins)
    cfg2 = ProjectConfig.from_dict({**CFG, "corners": ["tt", "ff"]})
    der2 = derive(cfg2, pins)
    p2 = compile_plan(cfg2, der2, nl, pins)
    assert set(r.run_id for r in p1.runs()) != set(r.run_id for r in p2.runs())


def test_run_ids_are_unique(plan):
    ids = [r.run_id for r in plan.runs(enabled_only=False)]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- netlist variants
def test_each_run_carries_its_own_netlist_with_the_right_corner(plan):
    for r in plan.runs(enabled_only=False):
        txt = r.netlist_text
        assert f"section={r.run.process}" in txt
        assert "parameters VSET=3" in txt
        assert "[pmukit stripped analysis] dcOp dc" in txt       # the TB analysis is gone
        assert "\r" not in txt


def test_the_code_variable_is_whatever_the_designer_named_it():
    """VSET is only the default: the variable the plan rewrites is config.vset_param."""
    text = DEMO.replace("parameters VSET=3", "parameters vout_sel=3 trim=0")
    nl = Netlist(text, "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [2, 5], "vset_param": "vout_sel"})
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    der = derive(cfg, pins)
    assert der.vset["param"] == "vout_sel"
    runs = compile_plan(cfg, der, nl, pins).runs(enabled_only=False)
    seen = set()
    for r in runs:
        assert "VSET" not in r.netlist_text
        assert f"parameters vout_sel={r.run.vset} trim=0" in r.netlist_text
        seen.add(r.run.vset)
    assert seen == {2, 5}


def test_per_ldo_codes_tied_to_one_variable_all_follow_it():
    """A real PMU has one code per LDO; the bench ties them to ONE variable and pmukit rewrites
    only that one -- the per-LDO variables are expressions of it and must be left alone, also
    when ADE continues the declaration over several lines."""
    text = DEMO.replace("parameters VSET=3",
                        "parameters vsel=3 ldo_a_sel=vsel \\\n    ldo_b_sel=vsel trim=0")
    nl = Netlist(text, "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [1, 3], "vset_param": "vsel"})
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    for r in compile_plan(cfg, derive(cfg, pins), nl, pins).runs(enabled_only=False):
        # the exported code (3) leaves the statement byte-identical, continuation and all
        assert (f"parameters vsel={r.run.vset} ldo_a_sel=vsel ldo_b_sel=vsel trim=0"
                in " ".join(r.netlist_text.replace("\\\n", " ").split()))
        if r.run.vset == 3:
            assert "parameters vsel=3 ldo_a_sel=vsel \\\n    ldo_b_sel=vsel trim=0" in r.netlist_text
            assert "parameters" not in r.run.recipe.split("[analyses]")[0]


def test_the_netlists_own_code_is_the_nominal_one():
    """Codes typed as 1,3 on a bench exported at VSET=3: 3 is the code every non-swept run
    and the model default use, not whichever was typed first."""
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [1, 3]})
    der = derive(cfg, nl.scan("PMU_TOP", ports=cfg.ports))
    assert der.vset["codes"] == [3, 1]
    # a netlist value that is not among the codes changes nothing
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [1, 2]})
    assert derive(cfg, nl.scan("PMU_TOP", ports=cfg.ports)).vset["codes"] == [1, 2]


def test_several_codes_on_an_undeclared_variable_are_refused():
    """Declaring it would run -- and every code would simulate the same circuit."""
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [2, 5], "vset_param": "vout_sel"})
    with pytest.raises(PmuError) as ei:
        derive(cfg, nl.scan("PMU_TOP", ports=cfg.ports))
    assert "vout_sel" in ei.value.what and "VSET" in " ".join(ei.value.do)
    # one code is harmless: nothing is being switched
    one = ProjectConfig.from_dict({**CFG, "vset_param": "vout_sel"})
    derive(one, nl.scan("PMU_TOP", ports=one.ports))


def test_ac_run_sets_exactly_one_hot_source(plan):
    r = plan.group("ac:VS_VDDA_1V0").runs[0]
    hot = [ln for ln in r.netlist_text.splitlines() if "mag=1" in ln]
    assert len(hot) == 1 and hot[0].startswith("VS_VDDA_1V0")


def test_noise_puts_oprobe_after_the_analysis_keyword(plan):
    """A scar from the cluster: `nz oprobe=<src> noise ...` is a PARSE ERROR."""
    g = [g for g in plan.groups if g.id.startswith("noise:noise_i.")][0]
    line = [ln for ln in g.runs[0].netlist_text.splitlines() if ln.startswith("nz ")][0]
    assert line.index(" noise ") < line.index("oprobe=")


def test_rail_noise_uses_its_own_ground(plan):
    g = plan.group("noise:noise_v.a")
    line = [ln for ln in g.runs[0].netlist_text.splitlines() if ln.startswith("nz ")][0]
    assert line.startswith("nz (VDD0P8_A ")


def test_load_transient_is_a_pwl_between_the_declared_currents(plan):
    g = plan.group("tran_load_on:IL_VDD0P8_A")
    txt = g.runs[0].netlist_text
    line = [ln for ln in txt.splitlines() if ln.startswith("IL_VDD0P8_A")][0]
    assert "type=pwl" in line and "2e-06" in line and "0.0005" in line
    assert "dc=" not in line


def test_recipe_carries_the_edits_and_ends_with_the_submit_command(plan):
    r = plan.group("ac:IL_VDD0P8_A").runs[0]
    rec = Recipe.parse(r.run.recipe)
    assert any(e.startswith("~ ") and "was:" in e for e in rec.edits)
    assert any(a.startswith("acz ac ") for a in rec.analyses)
    assert rec.submit.startswith("ssh ewave-vm 'tcsh -c \"source ~/.cshrc;")
    assert r.run.recipe.rstrip().endswith(rec.submit)


# --------------------------------------------------------------------------------- cost
def test_cost_summary_adds_up(plan):
    s = plan.cost_summary()
    assert s["runs"] == len(plan.runs())
    assert s["cpu_seconds"] > 0
    assert s["cpu_hours"] == pytest.approx(s["cpu_seconds"] / 3600.0, abs=5e-4)
    assert set(s["by_analysis"]) <= {"ac", "noise", "dc_load", "dc_temp", "dc_iv",
                                     "tran_load_on", "tran_load_off", "tran_en"}


def test_cost_model_is_pluggable(parts):
    cfg, der, nl, pins = parts
    p = compile_plan(cfg, der, nl, pins, cost=lambda run, d: 7.0)
    assert all(r.cost_s == 7.0 for r in p.runs())


def test_measured_cost_uses_the_ledger_then_falls_back(parts, tmp_path):
    cfg, der, nl, pins = parts
    led = Ledger(tmp_path / "runs.sqlite")
    p = compile_plan(cfg, der, nl, pins)
    ac = [r for r in p.runs() if r.run.analysis == "ac"][0]
    led.upsert(ac.run)
    led.set_status(ac.run_id, "done", cpu_seconds=42.0, finished=True)
    cost = measured_cost(led)
    p2 = compile_plan(cfg, der, nl, pins, cost=cost)
    assert all(r.cost_s == 42.0 for r in p2.runs() if r.run.analysis == "ac")
    # an analysis with no measurement yet still gets the default estimate
    noise = [r for r in p2.runs() if r.run.analysis == "noise"][0]
    assert noise.cost_s == default_cost(noise.run, der)
    led.close()


# ------------------------------------------------------------------ ticking groups off
def test_unticking_a_group_names_what_is_lost(plan):
    plan.set_enabled("noise:noise_v.a", False)
    lost = plan.consequences()
    hit = [e for e in lost if e["port"] == "a" and e["block"] == "noise"]
    assert hit, lost
    assert "noise_v" in hit[0]["observables"]
    assert "NOT RUN" in hit[0]["effect"]


def test_nothing_is_lost_while_everything_is_ticked(plan):
    assert plan.consequences() == []


def test_a_parameter_fed_by_another_enabled_run_is_not_reported_lost(plan):
    """dc_temp also feeds the rail dc block, so dropping dc_load must not claim dc.vout_tc."""
    plan.set_enabled("dc_load:IL_VDD0P8_A", False)
    lost = {(e["port"], e["block"], tuple(e["params"])) for e in plan.consequences()}
    params = {p for _port, _b, ps in lost for p in ps}
    assert "vout_tc" not in params          # still measured by the temperature sweep
    assert "vout" in params or "dropout" in params


# ------------------------------------------------------------------------------- commit
def test_commit_writes_runs_and_consumes(plan, tmp_path):
    led = Ledger(tmp_path / "runs.sqlite")
    counts = plan.commit(led)
    assert counts["new"] == len(plan.runs())
    rid = plan.group("ac:IL_VDD0P8_A").runs[0].run_id
    cons = led.consumers(rid)
    assert ("a", "zout", "Ra") in cons or any(c[1] == "zout" for c in cons)
    assert "zout" in led.why(rid)
    # re-planning the identical plan is all cache -- this is resume
    for r in plan.runs():
        led.set_status(r.run_id, "done", cpu_seconds=1.0, finished=True)
    again = plan.commit(led)
    assert again["cached"] == len(plan.runs()) and again["new"] == 0
    led.close()


def test_commit_only_writes_enabled_groups(plan, tmp_path):
    led = Ledger(tmp_path / "runs.sqlite")
    plan.set_enabled("tran_en", False)
    plan.commit(led)
    assert led.all(analysis="tran_en") == []
    led.close()


# --------------------------------------------------------------------------- refusals
def test_a_project_with_nothing_to_model_is_a_four_part_error(parts):
    cfg, _der, nl, pins = parts
    cfg2 = ProjectConfig.from_dict({**CFG, "ports": {k: "ignore" for k in CFG["ports"]}})
    der2 = derive(cfg2, pins)
    with pytest.raises(PmuError) as e:
        compile_plan(cfg2, der2, nl, pins)
    assert "model" in str(e.value) and "Do   :" in str(e.value)


def test_unknown_group_lists_the_real_ones(plan):
    with pytest.raises(PmuError) as e:
        plan.group("nope")
    assert "ac:" in str(e.value)


def test_to_rows_is_json_safe(plan):
    import json
    json.dumps(plan.to_rows())
    row = plan.to_rows()[0]
    assert set(row) >= {"id", "title", "why", "runs", "cpu_seconds", "ports", "enabled"}
