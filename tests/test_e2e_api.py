"""M10's acceptance: the whole journey through the HTTP API, no browser anywhere.

One synthetic PMU (`tests/fixtures/pmu_demo/input.scs`), one project, one server, and the steps
in the order a user takes them::

    new -> netlist -> pins -> config -> plan -> run -> fit -> verify -> deliver -> digest

Each step asserts the PAYLOAD, not just the status code: that the parser really found the rails
by their IL_ prefixes, that the plan really merges one supply injection across every port, that
the ledger really drains, that the fitted model really produces a curve on the measurement's own
frequency points.

Two rules make this honest while the rest of the build is still landing:

* a step whose backing module is not in this build SKIPS with the four-part reason the server
  gave, so a missing module is visible in the report instead of quietly passing;
* a step that fails for any other reason FAILS, and every later step skips, because the pipeline
  is a chain -- there is nothing to learn from fitting a dataset that was never produced.

The simulator backend is `fake` (synthetic analytic results, nothing is simulated) unless
`PMUKIT_E2E_ENGINE` says otherwise -- set it to `spectre_ssh` on a desk that can reach the VM.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from pmukit import server

REPO = pathlib.Path(__file__).resolve().parents[1]
NETLIST = REPO / "tests" / "fixtures" / "pmu_demo" / "input.scs"
PROJECT = "demo_pmu"
ENGINE = os.environ.get("PMUKIT_E2E_ENGINE", "fake")

#: What the pipeline learned as it went. Later steps read it; a step that never ran leaves its
#: key absent, which is how the next one knows to skip.
STATE: dict = {}

JOB_TIMEOUT_S = float(os.environ.get("PMUKIT_E2E_TIMEOUT", "900"))


# --------------------------------------------------------------------------- harness
class Api:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.srv = server.make_server("127.0.0.1", 0, demo=False, root=None, tries=1)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read().decode("utf-8")
                return r.status, (json.loads(raw) if raw.lstrip()[:1] in "{[" else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw.lstrip()[:1] in "{[" else raw)

    # -- the two helpers every step uses
    def need(self, method: str, path: str, body=None):
        """Call a route and insist on a payload; a 501 skips, anything else fails loudly."""
        status, payload = self.call(method, path, body)
        if status == 501:
            pytest.skip(_why(payload, f"{method} {path}"))
        assert status == 200, f"{method} {path} -> {status}: {json.dumps(payload)[:400]}"
        return payload

    def job(self, method: str, path: str, body=None, *, allow_partial: bool = False) -> dict:
        """Start a background task and poll it to the end, the way the page does."""
        started = self.need(method, path, body)
        assert "job" in started, f"{path} must answer with a job id, got {started}"
        deadline = time.time() + JOB_TIMEOUT_S
        while True:
            status, j = self.call("GET", "/api/jobs/" + started["job"])
            assert status == 200, j
            if j["status"] not in ("queued", "running"):
                break
            if time.time() > deadline:
                raise AssertionError(f"{path} did not finish in {JOB_TIMEOUT_S:g} s "
                                     f"(last message: {j.get('message')})")
            time.sleep(0.2)
        if j.get("not_landed"):
            pytest.skip(f"{j['not_landed']} is not in this build: "
                        f"{(j.get('error') or {}).get('what', '')}")
        if j["status"] == "failed":
            raise AssertionError(f"{path} failed: {json.dumps(j.get('error'))}")
        if j["status"] == "partial" and not allow_partial:
            raise AssertionError(f"{path} finished with reservations: {json.dumps(j.get('error'))}")
        assert j["events"], "a background task must report what it is doing"
        return j


def _why(payload, where: str) -> str:
    err = (payload or {}).get("error") or {}
    return f"{where}: {err.get('what', payload)} -- {err.get('why', '')}"


def requires(*keys):
    """Skip when an earlier step of the chain never produced its result."""
    for k in keys:
        if k not in STATE:
            pytest.skip(f"the '{k}' step did not run, so there is nothing to check here")


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    root = tmp_path_factory.mktemp("pmukit_e2e")
    old = os.environ.get("PMUKIT_DATA")
    os.environ["PMUKIT_DATA"] = str(root)
    # A per-run engine, never the developer's site.json: this test must not submit to a queue.
    old_engine = os.environ.get("PMUKIT_ENGINE")
    os.environ["PMUKIT_ENGINE"] = ENGINE
    client = Api(root)
    yield client
    client.close()
    if old is None:
        os.environ.pop("PMUKIT_DATA", None)
    else:
        os.environ["PMUKIT_DATA"] = old
    if old_engine is None:
        os.environ.pop("PMUKIT_ENGINE", None)
    else:
        os.environ["PMUKIT_ENGINE"] = old_engine


# ========================================================================= 1  new
def test_01_new_project(api):
    d = api.need("POST", "/api/projects", {"name": PROJECT})
    assert d["project"] == PROJECT
    assert d["created"] is True
    assert pathlib.Path(d["path"]).is_dir()
    listing = api.need("GET", "/api/projects")
    assert PROJECT in [p["name"] for p in listing["projects"]]
    assert listing["demo"] is False
    STATE["new"] = d["path"]


# ========================================================================= 2  netlist
def test_02_netlist_is_parsed_and_the_roles_come_from_the_source_names(api):
    requires("new")
    job = api.job("POST", f"/api/p/{PROJECT}/netlist", {"path": str(NETLIST)})
    pins = job["result"]
    assert pins["pmu_inst"] == "PMU_TOP"
    assert pins["pmu_master"] == "pmu_demo"
    by_name = {p["name"]: p for p in pins["pins"]}

    # the convention, read out of the deck -- nothing was guessed
    assert by_name["VDD0P8_A"]["role"] == "rail"
    assert by_name["VDD0P8_A"]["src"] == "IL_VDD0P8_A"
    assert by_name["VDD0P8_A"]["dc"] == pytest.approx(5e-4)
    assert by_name["IB_PTAT"]["role"] == "bias"
    assert by_name["IB_PTAT"]["src"] == "VB_IB_PTAT"
    assert by_name["VDDA_1V0"]["role"] == "supply"
    assert by_name["EN"]["role"] == "en"

    # grounds come from the wiring, not from a prefix
    grounds = sorted(p["name"] for p in pins["pins"] if p["is_ground"])
    assert grounds == ["AGND", "VSS_A", "VSS_B"]
    assert by_name["VDD0P8_A"]["gnd"] in grounds

    # and the deliberately role-less pin is REPORTED, never invented
    assert by_name["TESTMODE"]["role"] == "none"
    assert by_name["TESTMODE"]["reason"]

    # the netlist's own analyses are seen (they get stripped) and the rewrite points are found
    assert pins["analyses"], "the shipped analyses must be recognised so they can be stripped"
    assert pins["params"].get("VSET") is not None
    assert pins["sections"], "the PDK include lines carry the section= that gets rewritten"
    STATE["netlist"] = pins


# ========================================================================= 3  pins
def test_03_the_model_column_decides_what_is_characterized(api):
    requires("netlist")
    d = api.need("PUT", f"/api/p/{PROJECT}/pins/VDD0P8_C", {"fate": "stub"})
    assert "fate stub" in " ".join(d["changed"])
    by_name = {p["name"]: p for p in d["pins"]["pins"]}
    assert by_name["VDD0P8_C"]["fate"] == "stub"
    cfg = api.need("GET", f"/api/p/{PROJECT}/config")
    assert cfg["config"]["ports"]["VDD0P8_C"] == "stub"
    assert "VDD0P8_C" not in cfg["config"]["my_load"], \
        "a stubbed rail carries no load: it is never characterized"
    STATE["pins"] = by_name


def test_04_measure_load_reports_what_the_netlist_says_and_marks_what_it_does_not(api):
    requires("pins")
    d = api.need("POST", f"/api/p/{PROJECT}/measure-load", {})
    assert d["rails"]["VDD0P8_A"]["on_a"] == pytest.approx(5e-4)
    assert "IL_VDD0P8_A" in d["rails"]["VDD0P8_A"]["on_from"]
    # off is NOT in the netlist, and the payload says so instead of pretending
    assert "off_a_suggested" in d["rails"]["VDD0P8_A"]
    assert "not in the netlist" in d["rails"]["VDD0P8_A"]["off_note"]
    assert "VDD0P8_C" not in d["rails"], "a stubbed rail is not measured"
    assert d["biases"]["IB_PTAT"]["compliance_v"] == pytest.approx(0.4)


# ========================================================================= 4  config
def test_05_the_three_answers_drive_the_derived_config(api):
    requires("pins")
    cfg = api.need("GET", f"/api/p/{PROJECT}/config")["config"]
    cfg["corners"] = ["tt"]
    cfg["temps_c"] = [25.0]
    cfg["care_up_to_hz"] = 1e9
    cfg["ports"]["IB_POLY"] = "ignore"          # keep the acceptance run small
    saved = api.need("PUT", f"/api/p/{PROJECT}/config", {"config": cfg, "note": "e2e"})
    assert saved["config"]["corners"] == ["tt"]
    assert saved["sha"]

    der = api.need("GET", f"/api/p/{PROJECT}/config/derived")["derived"]
    assert der["process"]["corners"] == ["tt"]
    assert der["temps_c"]["points"] == [25.0]
    assert der["freq"]["stop_hz"] == pytest.approx(1e9)
    # the load grid is derived, not asked for: off, 0.2x on, on, 2x on
    grid = der["loads"]["VDD0P8_A"]["points_a"]
    assert len(grid) == 4 and grid == sorted(grid)
    assert grid[-1] == pytest.approx(2 * 5e-4)
    # and every derived field says where it came from
    assert der["process"]["provenance"] and der["loads"]["VDD0P8_A"]["provenance"]
    STATE["config"] = saved["config"]


def test_06_a_configuration_change_is_undoable(api):
    requires("config")
    before = api.need("GET", f"/api/p/{PROJECT}/config")
    cfg = dict(before["config"], state_note="a note we will take back")
    api.need("PUT", f"/api/p/{PROJECT}/config", {"config": cfg})
    assert api.need("GET", f"/api/p/{PROJECT}/config")["config"]["state_note"] \
        == "a note we will take back"
    undone = api.need("POST", f"/api/p/{PROJECT}/config/undo", {})
    assert undone["kind"] == "config"
    assert undone["config"]["state_note"] == before["config"]["state_note"]


# ========================================================================= 5  plan
def test_07_the_plan_is_compiled_and_every_run_says_why_it_exists(api):
    requires("config")
    plan = api.need("GET", f"/api/p/{PROJECT}/plan")
    assert plan["groups"], "a configured project must produce a plan"
    assert plan["cost"]["runs"] > 0
    assert plan["cells"] == 1                      # 1 corner x 1 temp x 1 vset
    assert plan["states"], "the shared load states are part of the plan"

    ids = [g["id"] for g in plan["groups"]]
    assert any(g.startswith("dc_load") for g in ids)
    assert any(g.startswith("ac:") for g in ids)
    assert any(g.startswith("noise") for g in ids)

    # AC superposition: ONE supply injection is read at every port at once
    supply = [g for g in plan["groups"] if g["id"].startswith("ac:VS_")]
    assert supply, "the supply injection group is missing"
    assert len(supply[0]["ports"]) > 1, "one supply injection must feed more than one port"

    group = plan["groups"][0]["id"]
    runs = api.need("GET", f"/api/p/{PROJECT}/plan/runs?group={urllib.parse.quote(group)}")
    assert runs["runs"]
    first = runs["runs"][0]
    assert first["why"], "every run must say which parameters it feeds"
    assert first["process"] == "tt"
    STATE["plan"] = {"group": group, "run": first["run_id"], "runs": plan["cost"]["runs"]}


def test_08_the_recipe_shows_exactly_what_was_changed_in_the_netlist(api):
    requires("plan")
    rid = STATE["plan"]["run"]
    rec = api.need("GET", f"/api/p/{PROJECT}/runs/{rid}/recipe")
    marks = {line["mark"] for line in rec["lines"]}
    assert "~" in marks, "the recipe must mark the lines it edited in place"
    assert "+" in marks, "the recipe must mark the lines it added"
    assert rec["recipe"].strip()
    assert rec["cell"]


def test_09_unticking_a_group_names_what_stops_being_measured(api):
    requires("plan")
    clean = api.need("GET", f"/api/p/{PROJECT}/plan/consequences")
    assert clean["consequences"] == [], "with every group on, nothing may be NOT RUN"

    group = [g["id"] for g in api.need("GET", f"/api/p/{PROJECT}/plan")["groups"]
             if g["id"].startswith("noise")][0]
    off = api.need("PUT", f"/api/p/{PROJECT}/plan/groups", {"id": group, "on": False})
    assert off["consequences"], "unticking a group must name what is lost"
    lost = off["consequences"][0]
    assert lost["port"] and lost["block"] and "NOT RUN" in lost["effect"]
    assert off["undoable"] == "plan_ticks"

    # and Ctrl-Z puts it back -- a tick is a configuration change, not a submitted run
    undone = api.need("POST", f"/api/p/{PROJECT}/config/undo", {})
    assert undone["kind"] == "plan_ticks"
    assert api.need("GET", f"/api/p/{PROJECT}/plan/consequences")["consequences"] == []


# ========================================================================= 6  run
def test_10_submitting_writes_the_ledger_and_drains_the_queue(api):
    requires("plan")
    job = api.job("POST", f"/api/p/{PROJECT}/submit", {"engine": ENGINE}, allow_partial=True)
    committed = job["result"]["committed"]
    assert committed["new"] == STATE["plan"]["runs"], "every enabled run must reach the ledger"

    led = api.need("GET", f"/api/p/{PROJECT}/ledger")
    assert led["total"] == committed["new"]
    if job["status"] == "partial":
        pytest.skip(f"the plan was committed but not run: "
                    f"{(job.get('error') or {}).get('what', '')}")
    assert led["counts"]["done"] + led["counts"]["imported"] == led["total"], \
        f"the queue did not drain: {led['counts']}"
    assert led["not_run"] == 0
    STATE["run"] = led


def test_11_a_run_carries_its_recipe_its_log_and_what_it_feeds(api):
    requires("run")
    rid = api.need("GET", f"/api/p/{PROJECT}/ledger")["rows"][0]["run_id"]
    d = api.need("GET", f"/api/p/{PROJECT}/runs/{rid}")
    assert d["run"]["status"] in ("done", "imported", "skipped_cached")
    assert d["run"]["recipe"], "the ledger stores how the run was built"
    assert d["run"]["reads"], "a run that reads nothing should never have been planned"
    assert d["why"], "the ledger can say which parameters ate this run"
    log = api.need("GET", f"/api/p/{PROJECT}/runs/{rid}/log")
    assert isinstance(log["lines"], list) and log["lines"]


def test_12_re_submitting_is_free_because_the_ledger_is_the_cache(api):
    requires("run")
    job = api.job("POST", f"/api/p/{PROJECT}/submit", {"engine": ENGINE}, allow_partial=True)
    counts = job["result"]["committed"]
    assert counts["new"] == 0, "a re-plan of unchanged runs must not create new rows"
    assert counts["cached"] > 0, "finished runs must be recognised as already having results"


# ========================================================================= 7  fit
def test_13_the_fit_turns_the_dataset_into_model_parameters(api):
    requires("run")
    job = api.job("POST", f"/api/p/{PROJECT}/fit", {})
    summary = job["result"]["summary"]
    assert summary["ports"], "the fit must produce parameters for the modeled ports"
    assert "VDD0P8_A" in summary["ports"]
    assert "zout" in summary["blocks"] and "psrr" in summary["blocks"]
    assert summary["fitted"] > 0
    assert pathlib.Path(job["result"]["fit"]).is_file()
    STATE["fit"] = summary


def test_14_the_model_screen_answers_can_i_trust_this(api):
    requires("fit")
    s = api.need("GET", f"/api/p/{PROJECT}/model/summary")
    assert s["fitted"] is True
    # the validity envelope is a measured range, never a promise
    assert s["valid"]["corners"] == "tt"
    assert "freq" in s["valid"] and "temp" in s["valid"]
    assert any(k.startswith("load ") for k in s["valid"])
    # what is held back and what was never run are both named, not hidden
    assert isinstance(s["usable_not_signoff"], list)
    assert isinstance(s["not_run"], list)
    assert s["graded_by"] in ("fit", "verify")


def test_15_the_grade_grid_never_colours_an_ungraded_block_green(api):
    requires("fit")
    g = api.need("GET", f"/api/p/{PROJECT}/model/grades")
    assert g["cells"] and g["rows"]
    assert {c["corner"] for c in g["cells"]} == {"tt"}
    grades = {c["grade"] for row in g["rows"] for c in row["cells"]}
    if g["graded_by"] == "fit":
        assert "green" not in grades, \
            "without verify there are no pass/fail limits, so nothing may claim green"
        assert grades <= {"fitted", "not_run"}
        assert g["why"], "a provisional grid must say why it is provisional"
    STATE["grades"] = g


def test_16_a_cell_lists_its_blocks_with_the_metric_each_one_scores(api):
    requires("grades")
    g = STATE["grades"]
    port = g["rows"][0]["port"]
    cell = g["cells"][0]
    d = api.need("GET", f"/api/p/{PROJECT}/model/cell?port={port}"
                        f"&corner={cell['corner']}&temp={cell['temp_c']}")
    assert d["blocks"], f"{port} has no fitted block at {cell}"
    for b in d["blocks"]:
        assert b["name"] and b["cell_key"]
        assert b["grade"] in ("green", "yellow", "red", "fitted", "not_run")
        if not b["missing"]:
            assert b["metric"], "a score without its unit is not a number anyone can use"
    assert d["runs"], "a cell must be able to name the runs behind it"
    STATE["cell"] = {"port": port, "blocks": d["blocks"]}


def test_17_the_curve_is_ground_truth_and_model_on_the_same_points(api):
    """The load-bearing claim of the Model screen: the model side is the fitter's analytic
    predict(), evaluated at the measurement's own frequencies. No simulator is started."""
    requires("cell")
    port = STATE["cell"]["port"]
    drawn = 0
    for b in STATE["cell"]["blocks"]:
        if b["missing"]:
            continue
        q = urllib.parse.urlencode({"port": port, "cell": b["cell_key"], "block": b["name"]})
        status, c = api.call("GET", f"/api/p/{PROJECT}/model/curve?{q}")
        if status != 200:
            # the curve view covers the spectra and the temperature laws; a transient block
            # says so in four parts rather than drawing something it cannot.
            assert "error" in c and c["error"]["do"], c
            continue
        assert len(c["x"]) == len(c["gt"]["mag"]) == len(c["model"]["mag"]) > 0, \
            f"{b['name']}: the two sides are not on the same points"
        assert len(c["gt"]["phase_deg"]) == len(c["x"])
        assert c["source"].endswith("." + port), "the payload must name the measured variable"
        assert c["unit"]
        assert any(v is not None for v in c["model"]["mag"]), \
            f"{b['name']}: predict() produced nothing"
        drawn += 1
    assert drawn, "not one block of this cell could be drawn"
    STATE["curve"] = drawn


# ========================================================================= 8  verify
def test_18_verify_grades_the_model_and_runs_the_hb_health_check(api):
    requires("fit")
    job = api.job("POST", f"/api/p/{PROJECT}/verify", {})
    assert "verify" in job["result"]
    assert pathlib.Path(job["result"]["verify"]).is_file()
    g = api.need("GET", f"/api/p/{PROJECT}/model/grades")
    assert g["graded_by"] == "verify"
    assert g["grades"], "verify must produce per-block grades"
    STATE["verify"] = job["result"]


# ========================================================================= 9  deliver
def test_19_deliver_writes_one_stamped_folder(api):
    requires("fit")
    api.job("POST", f"/api/p/{PROJECT}/deliver", {})
    d = api.need("GET", f"/api/p/{PROJECT}/deliverables")
    assert d["deliverables"], "deliver produced no folder"
    dv = d["deliverables"][0]
    names = [f["name"] for f in dv["files"]]
    assert any(n.endswith(".scs") for n in names), "the corner library is missing"
    assert any(n.endswith(".va") for n in names), "no model was emitted"
    assert "envelope.json" in names and "report.md" in names
    assert dv["provenance"], "a deliverable that cannot be traced back is not a deliverable"
    STATE["deliver"] = dv


def test_20_a_delivered_file_can_be_previewed_but_not_escaped(api):
    requires("deliver")
    dv = STATE["deliver"]
    report = api.need("GET", f"/api/p/{PROJECT}/deliverables/{dv['stamp']}/files/report.md")
    assert report["text"].strip()
    assert report["bytes"] > 0
    status, payload = api.call("GET", f"/api/p/{PROJECT}/deliverables/{dv['stamp']}"
                                      f"/files/{urllib.parse.quote('../config.json', safe='')}")
    assert status != 200
    assert payload["error"]["what"]


# ========================================================================= 10 digest
def test_21_the_digest_packs_what_the_desk_needs_and_names_what_it_dropped(api):
    requires("run")
    blocks = api.need("GET", f"/api/p/{PROJECT}/digest/blocks")
    ids = [b["id"] for b in blocks["blocks"]]
    assert "D0" in ids and "D1" in ids, "provenance and the ledger are always in a digest"
    if "fit" in STATE:
        assert "D2" in ids, "the lossless parameter block must be offered once there is a fit"
    assert blocks["estimate"]["bytes"] > 0

    big = api.need("POST", f"/api/p/{PROJECT}/digest", {"budget": 128000})
    assert big["parts"] and big["text"].startswith("[pmukit-digest")
    assert "[D9 trailer]" in big["text"], "a digest must always carry its trailer"

    small = api.need("POST", f"/api/p/{PROJECT}/digest", {"budget": 32000})
    if small["dropped"]:
        for dropped in small["dropped"]:
            assert dropped in small["text"], \
                "a dropped block must be NAMED in the trailer, never silently cut"
    STATE["digest"] = big


def test_22_a_digest_round_trips_back_into_a_payload(api):
    requires("digest")
    d = api.need("POST", "/api/digest/import", {"text": STATE["digest"]["text"]})
    assert d["meta"]["project"] == PROJECT
    assert d["summary"]["ledger_rows"] == STATE["run"]["total"], \
        "the desk must see every run the box saw"
    assert d["provenance"]["config_sha"]
    if "fit" in STATE:
        assert d["summary"]["params"] is True, "the desk must be able to re-emit from the digest"


# ========================================================================= the strip
def test_23_every_screen_has_a_command_that_really_names_this_project(api):
    from pmukit import helptext
    for screen in helptext.SCREENS:
        d = api.need("GET", f"/api/cli?screen={screen}&project={PROJECT}")
        assert d["cli"].startswith("pmukit ")
        if screen not in ("home", "states"):
            assert PROJECT in d["cli"], f"the {screen} echo does not name the project"


def test_24_help_is_available_for_every_screen_of_the_journey(api):
    from pmukit import helptext
    for screen in helptext.SCREENS:
        d = api.need("GET", f"/api/help/{screen}")
        assert d == helptext.as_dict(screen)
