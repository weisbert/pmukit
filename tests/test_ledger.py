"""Contract 3 (run ledger) -- schema, the resume hash, the lifecycle and the Why panel.

Synthetic names only: rails VDD0P8_A / VDD0P8_B, biases IB_PTAT / IB_POLY, project demo_pmu.
"""
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pmukit.errors import PmuError                                        # noqa: E402
from pmukit.ledger import (ANALYSES, STATUSES, Ledger, Recipe, Run,       # noqa: E402
                           canon_load_key, make_run_id)

NETLIST_SHA = "a1b2c3d4e5f6"


def mk(analysis="ac", *, process="tt", temp_c=25.0, vset=3, load_key="on_a",
       stimulus="IL_VDD0P8_A", reads=None, status="planned", **kw) -> Run:
    """A planned run in the demo_pmu corner cell, with its content-hash id."""
    reads = ["ac_zout.VDD0P8_A", "ac_psrr.VDD0P8_A"] if reads is None else reads
    run = Run(run_id="", process=process, temp_c=temp_c, vset=vset, load_key=load_key,
              analysis=analysis, stimulus=stimulus, reads=list(reads),
              netlist_sha=NETLIST_SHA, status=status, **kw)
    run.run_id = run.content_id()
    return run


@pytest.fixture
def led(tmp_path):
    with Ledger(tmp_path / "demo_pmu" / "runs.sqlite") as lg:
        yield lg


# --------------------------------------------------------------------------- schema
def test_runs_schema_matches_contract(led):
    info = led.connection.execute("PRAGMA table_info(runs)").fetchall()
    assert [r["name"] for r in info] == [
        "run_id", "process", "temp_c", "vset", "load_key", "analysis", "stimulus",
        "reads", "netlist_sha", "netlist_path", "psf_path", "engine", "job_id",
        "recipe", "status", "source_path", "submitted_at", "finished_at",
        "cpu_seconds", "peak_mem_mb", "error"]
    assert [r["type"] for r in info] == [
        "TEXT", "TEXT", "REAL", "INTEGER", "TEXT", "TEXT", "TEXT",
        "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT",
        "TEXT", "TEXT", "TEXT", "TEXT", "TEXT",
        "REAL", "REAL", "TEXT"]
    assert [r["name"] for r in info if r["pk"]] == ["run_id"]


def test_consumes_schema_matches_contract(led):
    info = led.connection.execute("PRAGMA table_info(consumes)").fetchall()
    assert [r["name"] for r in info] == ["run_id", "port", "block", "param"]
    assert [r["type"] for r in info] == ["TEXT", "TEXT", "TEXT", "TEXT"]


def test_pragmas(led):
    assert led.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert led.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_for_project_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    with Ledger.for_project("demo_pmu") as lg:
        assert lg.path == tmp_path / "demo_pmu" / "runs.sqlite"
        assert lg.path.exists()


# --------------------------------------------------------------------------- run_id
def _rid(**kw):
    base = dict(netlist_sha=NETLIST_SHA, process="tt", temp_c=25.0, vset=3,
                load_key="5e-4", analysis="ac", stimulus="IL_VDD0P8_A")
    base.update(kw)
    return make_run_id(**base)


def test_run_id_is_12_hex_and_stable():
    a, b = _rid(), _rid()
    assert a == b and len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_run_id_canonicalizes_numbers():
    # THIS IS THE RESUME MECHANISM: the same simulation, however the caller spelled the
    # corner cell, must land on the same row so it can be skipped_cached instead of re-run.
    assert _rid(load_key="5e-4") == _rid(load_key="0.0005") == _rid(load_key=0.0005)
    assert _rid(temp_c=25) == _rid(temp_c=25.0) == _rid(temp_c=25.0004)
    assert _rid(vset=3) == _rid(vset="3")


def test_run_id_ignores_reads_recipe_engine_and_timing():
    # A re-plan that harvests MORE observables out of the same AC sweep must collide with the
    # finished run -- otherwise contract 3's "one injection, read every port" would re-simulate.
    few = mk(reads=["ac_zout.VDD0P8_A"])
    many = mk(reads=["ac_zout.VDD0P8_A", "ac_psrr.VDD0P8_A", "ac_psrr.IB_PTAT"],
              recipe="totally different text", engine="spectre_ssh", job_id="J77",
              status="done", cpu_seconds=812.0)
    assert few.content_id() == many.content_id()


def test_run_id_separates_real_corner_differences():
    ids = {mk().content_id(), mk(process="ss").content_id(), mk(temp_c=125).content_id(),
           mk(vset=1).content_id(), mk(load_key="off_a").content_id(),
           mk(analysis="noise").content_id(), mk(stimulus="VS_VDDA_1V0").content_id()}
    assert len(ids) == 7


def test_canon_load_key_leaves_identifiers_alone():
    assert canon_load_key("VDD0P8_A=5e-4") == "VDD0P8_A=0.0005"
    assert canon_load_key("on_a") == "on_a"
    assert canon_load_key(None) == ""


# --------------------------------------------------------------------------- upsert
def test_upsert_planned_over_done_keeps_outcome_refreshes_plan(led):
    run = mk()
    led.upsert(run)
    led.set_status(run.run_id, "submitted", job_id="J1", submitted=True)
    led.set_status(run.run_id, "done", psf_path="/data/psf/ac", cpu_seconds=42.5,
                   peak_mem_mb=910.0, finished=True)
    done = led.get(run.run_id)

    replan = mk(reads=["ac_zout.VDD0P8_A", "ac_psrr.VDD0P8_A", "ac_psrr.IB_PTAT"],
                netlist_path="/data/netlists/tt_25c.scs")
    replan.recipe = "new recipe text"
    assert led.upsert(replan) == run.run_id

    after = led.get(run.run_id)
    assert after.status == "done"                       # outcome survives the re-plan
    assert after.psf_path == "/data/psf/ac"
    assert after.cpu_seconds == 42.5 and after.peak_mem_mb == 910.0
    assert after.submitted_at == done.submitted_at and after.finished_at == done.finished_at
    assert after.job_id == "J1"
    assert after.reads == replan.reads                  # plan's view is refreshed
    assert after.recipe == "new recipe text"
    assert after.netlist_path == "/data/netlists/tt_25c.scs"


def test_upsert_planned_over_failed_keeps_error(led):
    run = mk()
    led.upsert(run)
    led.set_status(run.run_id, "failed", error="spectre: singular matrix", finished=True)
    led.upsert(mk(recipe="re-planned"))
    after = led.get(run.run_id)
    assert after.status == "failed" and after.error == "spectre: singular matrix"


def test_upsert_runner_update_is_not_protected(led):
    run = mk()
    led.upsert(run)
    led.set_status(run.run_id, "done", cpu_seconds=10.0, finished=True)
    again = mk(status="running", engine="spectre_ssh")   # incoming is NOT 'planned'
    led.upsert(again)
    assert led.get(run.run_id).status == "running"


def test_upsert_rejects_unknown_status_and_analysis(led):
    with pytest.raises(PmuError):
        led.upsert(mk(status="halfway"))
    bad = mk()
    bad.analysis = "ac_zout"                            # an observable, not an analysis
    with pytest.raises(PmuError):
        led.upsert(bad)


def test_plan_many_counts(led):
    first = [mk("ac"), mk("noise", stimulus="IL_VDD0P8_A"), mk("dc_load", load_key="off_a")]
    assert led.plan_many(first) == {"new": 3, "cached": 0, "updated": 0}

    led.set_status(first[0].run_id, "done", cpu_seconds=100.0, finished=True)
    led.set_status(first[1].run_id, "failed", error="license", finished=True)

    replan = [mk("ac", reads=["ac_zout.VDD0P8_A"]),      # stored done       -> cached
              mk("noise", stimulus="IL_VDD0P8_A"),       # stored failed     -> updated
              mk("dc_load", load_key="off_a"),           # stored planned    -> updated
              mk("dc_temp", load_key="")]                # brand new         -> new
    assert led.plan_many(replan) == {"new": 1, "cached": 1, "updated": 2}


# --------------------------------------------------------------------------- lifecycle
def test_set_status_lifecycle_stamps_times(led):
    run = mk()
    led.upsert(run)
    assert led.get(run.run_id).status == "planned"

    led.set_status(run.run_id, "submitted", job_id="J42", submitted=True)
    sub = led.get(run.run_id)
    assert sub.status == "submitted" and sub.job_id == "J42"
    assert sub.submitted_at.endswith("Z") and sub.finished_at == ""

    led.set_status(run.run_id, "running")
    led.set_status(run.run_id, "failed", error="spectre: no license", finished=True)
    bad = led.get(run.run_id)
    assert bad.status == "failed" and bad.error == "spectre: no license"
    assert bad.finished_at.endswith("Z")

    led.set_status(run.run_id, "done", psf_path="/data/psf/ac", cpu_seconds=7.5,
                   peak_mem_mb=64.0, finished=True)
    ok = led.get(run.run_id)
    assert ok.status == "done" and ok.error == ""       # a retry clears the old error
    assert ok.psf_path == "/data/psf/ac" and ok.cpu_seconds == 7.5
    assert ok.submitted_at == sub.submitted_at          # untouched keywords stay put


def test_set_status_rejects_unknown_status(led):
    run = mk()
    led.upsert(run)
    with pytest.raises(PmuError) as e:
        led.set_status(run.run_id, "mostly_done")
    err = e.value.to_dict()["error"]
    assert err["what"] and err["why"] and err["do"] and err["where"]     # four-part
    assert "mostly_done" in err["what"]


def test_set_status_unknown_run(led):
    with pytest.raises(PmuError):
        led.set_status("deadbeef0000", "done")


def test_counts_by_status_zero_fills(led):
    led.plan_many([mk("ac"), mk("noise")])
    led.set_status(mk("ac").run_id, "done", finished=True)
    counts = led.counts_by_status()
    assert set(counts) == set(STATUSES)
    assert counts["done"] == 1 and counts["planned"] == 1 and counts["running"] == 0


# --------------------------------------------------------------------------- queries
def test_all_filters(led):
    led.plan_many([
        mk("ac", process="tt", reads=["ac_zout.VDD0P8_A"]),
        mk("ac", process="ss", reads=["ac_zout.VDD0P8_B"]),
        mk("noise", process="tt", reads=["noise_v.VDD0P8_A"]),
        mk("dc_iv", process="tt", stimulus="VB_IB_PTAT", load_key="",
           reads=["dc_iv.IB_PTAT"]),
    ])
    led.set_status(led.all(analysis="noise")[0].run_id, "done", finished=True)

    assert len(led.all()) == 4
    assert len(led.all(status="planned")) == 3
    assert [r.analysis for r in led.all(analysis=["ac", "noise"])] == ["ac", "ac", "noise"]
    assert len(led.all(process="tt")) == 3
    assert len(led.all(process=["tt", "ss"], analysis="ac")) == 2

    a = led.all(port="VDD0P8_A")
    assert {r.analysis for r in a} == {"ac", "noise"}
    assert [r.analysis for r in led.all(port="VDD0P8_B")] == ["ac"]
    assert [r.analysis for r in led.all(port="IB_PTAT")] == ["dc_iv"]
    assert led.all(port="VDD0P8_C") == []

    assert len(led.all(limit=2)) == 2
    assert led.all(offset=3)[0].analysis == "dc_iv"
    assert len(led.all(port="VDD0P8_A", limit=1)) == 1


def test_to_rows_is_json_safe(led):
    led.upsert(mk())
    rows = led.to_rows()
    assert json.loads(json.dumps(rows))[0]["reads"] == ["ac_zout.VDD0P8_A", "ac_psrr.VDD0P8_A"]


def test_not_run_lists_only_what_has_no_results(led):
    planned, failed, done, imported = mk("ac"), mk("noise"), mk("dc_load"), mk("dc_temp",
                                                                              load_key="")
    led.plan_many([planned, failed, done, imported])
    led.set_status(failed.run_id, "failed", error="timeout", finished=True)
    led.set_status(done.run_id, "done", finished=True)
    led.set_status(imported.run_id, "imported")
    assert {r.run_id for r in led.not_run()} == {planned.run_id, failed.run_id}


# --------------------------------------------------------------------------- consumes / why
def test_consumes_and_why(led):
    run = mk(reads=["ac_zout.VDD0P8_A", "ac_psrr.VDD0P8_A"])
    led.upsert(run)
    led.add_consumes(run.run_id, [("VDD0P8_A", "zout", "r_ladder"),
                                  ("VDD0P8_A", "zout", "l_ladder")])
    led.add_consumes(run.run_id, [("VDD0P8_A", "zout", "r_ladder")])      # idempotent
    assert led.consumers(run.run_id) == [("VDD0P8_A", "zout", "l_ladder"),
                                         ("VDD0P8_A", "zout", "r_ladder")]
    assert led.runs_for_param("VDD0P8_A", "zout") == [run.run_id]
    assert led.runs_for_param("VDD0P8_A", "zout", "r_ladder") == [run.run_id]
    assert led.runs_for_param("VDD0P8_B", "zout") == []

    why = led.why(run.run_id)
    assert "VDD0P8_A.zout.r_ladder" in why and "VDD0P8_A.zout.l_ladder" in why
    assert "ac_zout.VDD0P8_A" in why
    assert "tt" in why and "25 C" in why and "VSET 3" in why and "load on_a" in why


def test_why_without_consumers_says_so(led):
    run = mk()
    led.upsert(run)
    assert "no recorded consumer" in led.why(run.run_id)


def test_add_consumes_to_unknown_run_raises(led):
    with pytest.raises(PmuError):
        led.add_consumes("deadbeef0000", [("VDD0P8_A", "zout", "r_ladder")])


def test_cell_text_without_load_axis(led):
    assert "no load axis" in mk("dc_iv", load_key="").cell_text()


# --------------------------------------------------------------------------- cost
def test_cost_by_analysis_and_total_hours(led):
    ac1, ac2, ac3, ns = mk("ac"), mk("ac", process="ss"), mk("ac", process="ff"), mk("noise")
    led.plan_many([ac1, ac2, ac3, ns])
    led.set_status(ac1.run_id, "done", cpu_seconds=3600.0, finished=True)
    led.set_status(ac2.run_id, "imported", cpu_seconds=0.0)
    led.set_status(ns.run_id, "done", cpu_seconds=1800.0, finished=True)
    # ac3 never ran: it contributes a planned row and zero CPU

    cost = led.cost_by_analysis()
    assert cost["ac"] == {"runs": 3, "cpu_seconds": 3600.0, "done": 2}
    assert cost["noise"] == {"runs": 1, "cpu_seconds": 1800.0, "done": 1}
    assert set(cost) == {"ac", "noise"}
    assert led.total_cpu_hours() == pytest.approx(1.5)


def test_total_cpu_hours_empty(led):
    assert led.total_cpu_hours() == 0.0


# --------------------------------------------------------------------------- recipe
def _recipe() -> Recipe:
    return Recipe(
        edits=[Recipe.edit("include \"pdk.scs\" section=ss", "section=tt"),
               Recipe.edit("parameters VSET=3", "VSET=0"),
               Recipe.add("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u mag=1"),
               Recipe.strip("dcOp dc")],
        analyses=["ac_sweep ac start=10 stop=2e10 dec=20"],
        saves=["save VDD0P8_A VDD0P8_B"],
        submit="ssh box 'tcsh -c \"source ~/.cshrc; spectre -64 input.scs\"'")


def test_recipe_helper_formats():
    assert Recipe.edit("options temp=125", "temp=27") == "~ options temp=125        // was temp=27"
    assert Recipe.add("save VDD0P8_A") == "+ save VDD0P8_A"
    assert Recipe.strip("dcOp dc") == "- dcOp dc"


def test_recipe_roundtrip_and_submit_is_last_line():
    r = _recipe()
    text = r.text()
    assert text.splitlines()[-1] == r.submit
    back = Recipe.parse(text)
    assert back == r and back.text() == text


def test_recipe_empty_and_headerless_parse():
    assert Recipe().text() == ""
    assert Recipe.parse("") == Recipe()
    loose = Recipe.parse("~ options temp=125        // was temp=27\n")
    assert loose.edits == ["~ options temp=125        // was temp=27"] and loose.submit == ""


def test_recipe_survives_the_ledger(led):
    run = mk(recipe=_recipe().text())
    led.upsert(run)
    assert Recipe.parse(led.get(run.run_id).recipe) == _recipe()


# --------------------------------------------------------------------------- concurrency
def test_two_handles_share_the_file(tmp_path):
    path = tmp_path / "demo_pmu" / "runs.sqlite"
    with Ledger(path) as writer, Ledger(path) as reader:
        writer.upsert(mk("ac"))
        assert len(reader.all()) == 1                    # committed work is visible at once

        run = mk("noise")
        writer.connection.execute("BEGIN IMMEDIATE")
        writer._apply(run)                               # write held open, not committed
        assert len(reader.all()) == 1                    # WAL: the reader is not blocked
        writer.connection.commit()
        assert len(reader.all()) == 2


# --------------------------------------------------------------------------- constants
def test_contract_constants():
    assert STATUSES == ("planned", "submitted", "running", "done", "failed",
                        "skipped_cached", "imported")
    assert ANALYSES == ("dc_load", "dc_temp", "dc_iv", "ac", "noise",
                        "tran_load_on", "tran_load_off", "tran_en")
