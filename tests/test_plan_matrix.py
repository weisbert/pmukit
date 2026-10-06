"""The Plan screen's matrix and its batch: what to run THIS time, never what the model covers.

* every group sits on the matrix: one port's cell (rail: DC load / Zout / noise / load on / load
  off; bias: I-V / Yout / noise), or shared when one simulation reads every port -- the supply
  injection, the temperature sweep, the enable ramp, the output-code check;
* the batch narrows a submission to some of the temperatures / corners chosen on New: the
  submitted runs, the cost and Submit's count follow it, the model's range (New) does not; a run
  that sweeps temperature inside itself is in every batch; a list naming all of them is "all";
* the batch is undone with the ticks (one Plan selection, one Ctrl-Z), and a state.json written
  before the batch existed still loads;
* a click on a block says what it simulates: the analysis and save lines its runs add, the
  source it drives, what it reads, the cells -- and per run, whether this batch sends it;
* `pmukit plan|run --only/--off/--temps/--corners` is the same selection, and the command the
  Plan screen echoes is one `pmukit run` really takes.
"""
from __future__ import annotations

import json
import math
import types

import pytest

from pmukit import cli, server
from pmukit.errors import PmuError
from pmukit.plan import CELL_COLUMN, compile_plan
from pmukit.state import UiState
from tests.test_new_screen import FIXTURE_DIR, wait
from tests.test_plan_scale import build


# ------------------------------------------------------------------------------ the compiler
def test_every_group_is_a_port_cell_or_shared():
    cfg, der, nl, pins, site = build("demo")
    plan = compile_plan(cfg, der, nl, pins, site=site)
    by = {g.id: g for g in plan.groups}
    assert (by["dc_load:IL_VDD0P8_A"].kind, by["dc_load:IL_VDD0P8_A"].port,
            by["dc_load:IL_VDD0P8_A"].column) == ("rail", "VDD0P8_A", "dc")
    assert (by["ac:IL_VDD0P8_B"].kind, by["ac:IL_VDD0P8_B"].column) == ("rail", "ac")
    assert (by["tran_load_off:IL_VDD0P8_C"].kind,
            by["tran_load_off:IL_VDD0P8_C"].column) == ("rail", "load_off")
    assert (by["dc_iv:VB_IB_PTAT"].kind, by["dc_iv:VB_IB_PTAT"].port,
            by["dc_iv:VB_IB_PTAT"].column) == ("bias", "IB_PTAT", "dc")
    assert (by["noise:noise_i.IB_POLY"].kind, by["noise:noise_i.IB_POLY"].column) == \
        ("bias", "noise")
    for gid in ("dc_temp", "ac:VS_VDDA_1V0", "tran_en", "code_check"):
        assert by[gid].kind == "shared" and by[gid].port == "" and by[gid].column == gid
    for g in plan.groups:
        if g.kind != "shared":
            assert g.ports() == [g.port]
            assert {CELL_COLUMN[o] for o in g.observables()} == {g.column}
    # at most one group per (port, column): a cell is one checkbox
    cells = [(g.port, g.column) for g in plan.groups if g.kind != "shared"]
    assert len(cells) == len(set(cells))


def test_the_batch_narrows_what_is_sent_and_nothing_else():
    cfg, der, nl, pins, site = build("demo")       # corners tt, ss; temps -40, 25, 125
    plan = compile_plan(cfg, der, nl, pins, site=site)
    every = plan.runs()
    assert plan.cost_summary()["runs"] == len(every)
    plan.batch = {"temps": [25.0]}
    sent = plan.runs()
    assert 0 < len(sent) < len(every)
    assert all(r.run.temp_c == 25.0 or math.isnan(r.run.temp_c) for r in sent)
    # the temperature sweep runs the whole range in one run: it is in every batch
    swept = [r for r in every if math.isnan(r.run.temp_c)]
    assert swept and all(r in sent for r in swept)
    assert plan.cost_summary()["runs"] == len(sent)
    rows = {row["id"]: row for row in plan.to_rows()}
    assert sum(row["in_batch"] for row in rows.values()) == len(sent)
    assert all(row["runs"] >= row["in_batch"] for row in rows.values())
    plan.batch = {"temps": [25.0], "corners": ["ss"]}
    assert all(r.run.process == "ss" for r in plan.runs())
    # the plan itself is untouched: every run still there, the same ids
    assert [r.run_id for r in plan.runs(enabled_only=False)] == \
        [r.run_id for r in compile_plan(cfg, der, nl, pins, site=site).runs(enabled_only=False)]


# ------------------------------------------------------------------------------- the state
def test_the_batch_is_saved_and_undone_with_the_ticks(tmp_path):
    st = UiState(project="p")
    st._root = tmp_path
    st.set_ticks({"g1": False}, "untick")
    st.set_ticks(st.plan_ticks, "batch", batch={"temps": [25], "corners": ["tt"], "junk": 1})
    assert st.plan_batch == {"temps": [25.0], "corners": ["tt"]}
    st.save()
    raw = json.loads(st.path.read_text(encoding="utf-8"))
    assert raw["plan_batch"] == {"temps": [25.0], "corners": ["tt"]}
    assert UiState.load("p", root=tmp_path).plan_batch == st.plan_batch
    kind, _ = st.undo()
    assert kind == "plan_ticks" and st.plan_batch == {} and st.plan_ticks == {"g1": False}
    st.undo()
    assert st.plan_ticks == {}


def test_a_state_written_before_the_batch_still_loads():
    st = UiState.from_dict({"project": "p", "plan_ticks": {"g1": False},
                            "tick_history": [{}, {"g2": False}],
                            "undo_log": [{"kind": "plan_ticks"}, {"kind": "plan_ticks"}]})
    assert st.plan_batch == {}
    assert st.tick_history == [{"ticks": {}, "batch": {}},
                               {"ticks": {"g2": False}, "batch": {}}]
    st.undo()
    assert st.plan_ticks == {"g2": False} and st.plan_batch == {}


# ------------------------------------------------------------------------------ the server
@pytest.fixture
def api(tmp_path):
    a = server.Api(root=tmp_path / "data")
    a.new_project({"name": "p"})
    job = wait(a.load_netlist("p", {"path": str(FIXTURE_DIR / "input.scs")}))
    assert job.status == "done", job.error
    cfg = a.get_config("p")["config"]
    cfg["temps_c"] = [-40, 25, 125]
    a.put_config("p", {"config": cfg})
    return a


def test_the_plan_payload_places_every_group_and_says_the_batch(api):
    d = api.plan("p")
    assert d["batch"] == {"temps": [-40.0, 25.0, 125.0], "corners": d["batch"]["corners"],
                          "chosen": {}, "out": 0}
    kinds = {g["kind"] for g in d["groups"]}
    assert {"rail", "shared"} <= kinds
    for g in d["groups"]:
        assert g["in_batch"] == g["runs"] and g["batch_cached"] == 0
        assert (g["port"] != "") == (g["kind"] != "shared")


def test_the_batch_route_narrows_submit_and_keeps_new(api):
    full = api.plan("p")["cost"]["runs"]
    d = api.set_plan_batch("p", {"temps": [25]})
    assert d["batch"]["chosen"] == {"temps": [25.0]}
    assert 0 < d["cost"]["runs"] < full
    assert d["batch"]["out"] == full - d["cost"]["runs"]
    assert d["undoable"] == "plan_ticks"
    assert api.get_config("p")["config"]["temps_c"] == [-40, 25, 125]     # the model's range
    # every one of them is "all": a temperature added on New later is in the next batch
    assert api.set_plan_batch("p", {"temps": [125, -40, 25]})["batch"]["chosen"] == {}
    assert api.set_plan_batch("p", {"temps": [25]})["batch"]["chosen"] == {"temps": [25.0]}
    assert api.set_plan_batch("p", {"temps": None})["batch"]["chosen"] == {}
    # Ctrl-Z
    api.set_plan_batch("p", {"temps": [125]})
    assert api.undo_config("p")["kind"] == "plan_ticks"
    assert api.plan("p")["batch"]["chosen"] == {}


def test_the_batch_refuses_what_new_did_not_choose(api):
    with pytest.raises(PmuError) as e:
        api.set_plan_batch("p", {"temps": [30]})
    assert "30.0 is not one of this project's temps" in e.value.what
    assert "changed there, not here" in e.value.why
    with pytest.raises(PmuError):
        api.set_plan_batch("p", {"corners": ["nope"]})
    with pytest.raises(PmuError):
        api.set_plan_batch("p", {"temps": "25"})


def test_a_block_says_what_it_simulates_and_which_runs_this_batch_sends(api):
    api.set_plan_batch("p", {"temps": [25]})
    gid = next(g["id"] for g in api.plan("p")["groups"] if g["id"].startswith("dc_load:"))
    d = api.plan_runs("p", gid)
    w = d["what"]
    assert w["analyses"] and w["analyses"][0].lstrip("+ ").startswith("dcz dc dev=IL_")
    assert w["saves"] and w["stimulus"].startswith("IL_")
    assert w["temps"] == [-40.0, 25.0, 125.0] and w["corners"] and w["codes"]
    assert w["loads"]
    sent = [r for r in d["runs"] if r["in_batch"]]
    assert sent and all(r["temp_c"] == 25.0 for r in sent)
    assert all(r["cached"] is False for r in d["runs"])
    swept = api.plan_runs("p", "dc_temp")
    assert swept["what"]["temps"] == ["swept"] and all(r["in_batch"] for r in swept["runs"])


def test_submit_sends_only_the_batch(api):
    api.set_plan_batch("p", {"temps": [25]})
    want = api.plan("p")["cost"]["runs"]
    job = wait(api.submit("p", {"commit_only": True}))
    assert job.status == "done", job.error
    assert job.result["committed"]["new"] == want


# --------------------------------------------------------------------------------- the CLI
def test_the_cli_selection_is_the_screens(tmp_path):
    cfg, der, nl, pins, site = build("demo")
    plan = compile_plan(cfg, der, nl, pins, site=site)
    a = types.SimpleNamespace(project="demo", only=["dc_load:IL_VDD0P8_A", "dc_temp"], off=None,
                              temps="25", corners="tt")
    cli._select(plan, cfg, a)
    assert [g.id for g in plan.groups if g.enabled] == ["dc_load:IL_VDD0P8_A", "dc_temp"]
    assert plan.batch == {"temps": [25.0], "corners": ["tt"]}
    assert {r.run.process for r in plan.runs()} == {"tt"}
    with pytest.raises(PmuError) as e:
        cli._select(plan, cfg, types.SimpleNamespace(project="demo", only=None, off=None,
                                                     temps="30", corners=None))
    assert e.value.what == "--temps 30 is not one of this project's temps."
    with pytest.raises(PmuError):
        cli._select(plan, cfg, types.SimpleNamespace(project="demo", only=["nope"], off=None,
                                                     temps=None, corners=None))


def test_the_screens_command_is_one_run_takes():
    st = {"on": ["dc_load:IL_VDD0P8_A"], "off": ["a", "b"], "temps": [25], "corners": None}
    cmd = server.cli_echo("plan", st, "demo")
    assert cmd == "pmukit run demo --only dc_load:IL_VDD0P8_A --temps 25"
    st = {"on": ["a", "b", "c"], "off": ["noise:noise_v.VDD0P8_B"], "temps": None,
          "corners": ["tt", "ss"]}
    assert server.cli_echo("plan", st, "demo") == \
        "pmukit run demo --off noise:noise_v.VDD0P8_B --corners tt,ss"
    # and argparse takes every flag it writes, on `run` and on `plan`
    for sub in ("run", "plan"):
        args = cli.build_parser().parse_args(
            [sub, "demo", "--only", "g1", "--off", "g2", "--temps", "25", "--corners", "tt,ss"])
        assert args.only == ["g1"] and args.off == ["g2"]
        assert args.temps == "25" and args.corners == "tt,ss"


# ------------------------------------------------------------------------- the ledger's queue
def test_a_narrower_commit_drops_what_an_earlier_one_only_planned(api):
    """Dry run with every group on, then with one: the ledger holds that one group, not all of
    them still `planned` -- the Run screen showed everything queued after one was ticked."""
    groups = [g["id"] for g in api.plan("p")["groups"]]
    one = next(g for g in groups if g.startswith("dc_load:"))

    def dry():
        job = wait(api.submit("p", {"commit_only": True}))
        assert job.status == "done", job.error
        return job.result["committed"]
    every = dry()
    assert api.ledger("p")["total"] == every["new"]
    api.set_plan_groups("p", {"ticks": {g: g == one for g in groups}})
    want = api.plan("p")["cost"]["runs"]
    got = dry()
    assert got["dropped"] == every["new"] - want
    assert api.ledger("p")["total"] == want


def test_history_is_never_dropped(tmp_path):
    from pmukit.ledger import Ledger, Run
    led = Ledger(tmp_path / "runs.sqlite")

    def run(rid, **kw):
        return Run(run_id=rid, process="tt", temp_c=25.0, vset=3, load_key="",
                   analysis="dc_load", stimulus="IL_A", reads=["dc_load.A"], **kw)
    led.plan_many([run("aaaaaaaaaaa1"), run("aaaaaaaaaaa2"), run("aaaaaaaaaaa3"),
                   run("aaaaaaaaaaa4"), run("aaaaaaaaaaa5")])
    led.add_consumes("aaaaaaaaaaa1", [("A", "dc", "v0")])
    led.upsert(run("aaaaaaaaaaa2", status="done"))
    led.upsert(run("aaaaaaaaaaa3", status="planned", error="skipped: not now"))
    led.upsert(run("aaaaaaaaaaa4", status="submitted", job_id="123"))
    assert led.drop_unsubmitted(["aaaaaaaaaaa5"]) == 1          # only the bare planned one
    assert led.get("aaaaaaaaaaa1") is None and not led.consumers("aaaaaaaaaaa1")
    for rid in ("aaaaaaaaaaa2", "aaaaaaaaaaa3", "aaaaaaaaaaa4", "aaaaaaaaaaa5"):
        assert led.get(rid) is not None
