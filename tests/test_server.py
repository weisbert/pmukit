"""The web shell's server: every route answers, nothing leaks, the page is self-contained.

What is asserted here is the CONTRACT, not the wording:

* every route of the interface table (docs/OVERNIGHT_BRIEF.md) answers -- 200 with a payload, or
  a four-part error with all four keys.  A route that 404s because it was renamed is the failure
  this file exists to catch;
* the server binds loopback and refuses a foreign Host, because it has no authentication;
* `--demo` serves a whole synthetic PMU without touching $PMUKIT_DATA;
* the deliverable file route refuses to walk out of its directory, encoded or not;
* the single page really is single: the JavaScript parses, and nothing is loaded from the net
  (the red-zone box has none);
* `/api/help/<screen>` is byte-identical to `pmukit.helptext` -- the CLI and the panel cannot
  drift apart because there is only one copy.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from pmukit import helptext, server
from pmukit.errors import PmuError

REPO = pathlib.Path(__file__).resolve().parents[1]
PAGE = REPO / "pmukit" / "web" / "index.html"
FIXTURE = REPO / "tests" / "fixtures" / "pmu_demo" / "input.scs"


# --------------------------------------------------------------------------- harness
class Client:
    """A live server on an ephemeral port, driven with urllib -- no browser anywhere."""

    def __init__(self, *, demo: bool = False, root=None) -> None:
        self.srv = server.make_server("127.0.0.1", 0, demo=demo, root=root, tries=1)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if data:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8")
                return r.status, (json.loads(raw) if raw.lstrip()[:1] in "{[" else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, (json.loads(raw) if raw.lstrip()[:1] in "{[" else raw)


@pytest.fixture
def demo():
    c = Client(demo=True)
    yield c
    c.close()


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    c = Client(demo=False)
    yield c
    c.close()


def four_part(payload) -> dict:
    """Assert the body is the four-part error and give it back."""
    assert isinstance(payload, dict), payload
    assert "error" in payload, payload
    err = payload["error"]
    for key in ("what", "why", "do", "where"):
        assert key in err, f"missing {key!r} in {err}"
    assert isinstance(err["what"], str) and err["what"].strip()
    assert isinstance(err["why"], str) and err["why"].strip()
    assert isinstance(err["do"], list) and err["do"], "Do must offer at least one action"
    assert isinstance(err["where"], str)
    return err


def ok_or_four_part(status, payload, where=""):
    if status == 200:
        return
    four_part(payload)
    assert 400 <= status <= 599, f"{where}: odd status {status}"


# --------------------------------------------------------------------------- the route table
#: Every row of the interface table in docs/OVERNIGHT_BRIEF.md, in the order it appears there.
ROUTE_TABLE = [
    # Home
    ("GET", "/api/projects", None),
    ("POST", "/api/projects", {"name": "route_smoke"}),
    ("GET", "/api/machine", None),
    ("GET", "/api/deliverables/diff?a=demo_pmu/a&b=demo_pmu/b", None),
    # New
    ("POST", "/api/p/demo_pmu/netlist", {}),
    ("GET", "/api/p/demo_pmu/netlist", None),
    ("PUT", "/api/p/demo_pmu/netlist/instance", {"pmu_inst": "PMU_TOP"}),
    ("POST", "/api/p/demo_pmu/import", {"dirs": []}),
    ("GET", "/api/p/demo_pmu/pins", None),
    ("PUT", "/api/p/demo_pmu/pins/VDD0P8_A", {"fate": "model"}),
    ("GET", "/api/p/demo_pmu/config", None),
    ("PUT", "/api/p/demo_pmu/config", {"config": {}}),
    ("GET", "/api/p/demo_pmu/config/derived", None),
    ("POST", "/api/p/demo_pmu/config/undo", {}),
    ("POST", "/api/p/demo_pmu/measure-load", {}),
    # Plan
    ("GET", "/api/p/demo_pmu/plan", None),
    ("PUT", "/api/p/demo_pmu/plan/groups", {"ticks": {}}),
    ("GET", "/api/p/demo_pmu/plan/consequences", None),
    ("GET", "/api/p/demo_pmu/plan/runs?group=dc_load:IL_VDD0P8_A", None),
    ("GET", "/api/p/demo_pmu/runs/7c3e91a04bd2/recipe", None),
    ("POST", "/api/p/demo_pmu/submit", {"commit_only": True}),
    # Run
    ("GET", "/api/p/demo_pmu/ledger?status=failed", None),
    ("GET", "/api/p/demo_pmu/runs/7c3e91a04bd2", None),
    ("GET", "/api/p/demo_pmu/runs/7c3e91a04bd2/log", None),
    ("POST", "/api/p/demo_pmu/runs/7c3e91a04bd2/retry", {}),
    ("POST", "/api/p/demo_pmu/runs/7c3e91a04bd2/skip", {}),
    ("POST", "/api/p/demo_pmu/runs/b19f0c72e4a8/kill", {}),
    ("POST", "/api/p/demo_pmu/fit", {}),
    # Model
    ("GET", "/api/p/demo_pmu/model/summary", None),
    ("GET", "/api/p/demo_pmu/model/grades", None),
    ("GET", "/api/p/demo_pmu/model/cell?port=VDD0P8_A&corner=tt&temp=25", None),
    ("GET", "/api/p/demo_pmu/model/curve?port=VDD0P8_A&cell=tt/25c&block=zout", None),
    ("POST", "/api/p/demo_pmu/verify", {}),
    # Deliver
    ("POST", "/api/p/demo_pmu/deliver", {}),
    ("GET", "/api/p/demo_pmu/deliverables", None),
    ("GET", "/api/p/demo_pmu/deliverables/20260915-140211/files/report.md", None),
    # Digest
    ("GET", "/api/p/demo_pmu/digest/blocks", None),
    ("POST", "/api/p/demo_pmu/digest", {"budget": 64000}),
    ("POST", "/api/digest/import", {"text": ""}),
    # global
    ("GET", "/api/help/plan", None),
    ("GET", "/api/cli?screen=plan&project=demo_pmu", None),
    ("GET", "/api/jobs/deadbeef0000", None),
]


@pytest.mark.parametrize("method,path,body", ROUTE_TABLE,
                         ids=[f"{m} {p.split('?')[0]}" for m, p, _b in ROUTE_TABLE])
def test_every_route_of_the_table_answers(demo, method, path, body):
    status, payload = demo.call(method, path, body)
    assert status != 404, f"{method} {path} is not routed at all"
    ok_or_four_part(status, payload, f"{method} {path}")


@pytest.mark.parametrize("method,path,body", ROUTE_TABLE,
                         ids=[f"{m} {p.split('?')[0]}" for m, p, _b in ROUTE_TABLE])
def test_every_route_answers_on_a_real_empty_project(live, method, path, body):
    """The same table against a real, empty $PMUKIT_DATA: nothing may 404 or crash, and every
    refusal must still be the four-part error -- that is what the empty screens render."""
    status, payload = live.call(method, path, body)
    assert status != 404, f"{method} {path} is not routed at all"
    assert status != 500, f"{method} {path} crashed: {payload}"
    ok_or_four_part(status, payload, f"{method} {path}")


def test_unknown_route_is_a_four_part_error(demo):
    status, payload = demo.call("GET", "/api/not-a-route")
    assert status == 404
    err = four_part(payload)
    assert "/api/not-a-route" in err["where"]


def test_bad_json_body_is_a_four_part_error(demo):
    req = urllib.request.Request(demo.base + "/api/p/demo_pmu/digest", data=b"{not json",
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        raise AssertionError("a broken body must be refused")
    except urllib.error.HTTPError as exc:
        four_part(json.loads(exc.read().decode()))


# --------------------------------------------------------------------------- binding
def test_binds_loopback_by_default():
    c = Client(demo=True)
    try:
        assert c.srv.server_address[0] == "127.0.0.1"
        # and it is really not on the machine's outside address
        outside = socket.gethostbyname(socket.gethostname())
        if outside not in ("127.0.0.1", ""):
            s = socket.socket()
            s.settimeout(1.0)
            with pytest.raises((ConnectionRefusedError, OSError)):
                s.connect((outside, c.port))
            s.close()
    finally:
        c.close()


def test_a_foreign_host_header_is_refused(demo):
    status, payload = demo.call("GET", "/api/projects", None, {"Host": "evil.example"})
    assert status == 403
    four_part(payload)


def test_port_auto_increments_when_busy():
    first = server.make_server("127.0.0.1", 0, demo=True, tries=1)
    port = first.server_address[1]
    try:
        second = server.make_server("127.0.0.1", port, demo=True, tries=4)
        try:
            assert second.server_address[1] != port
            assert port < second.server_address[1] <= port + 4
        finally:
            second.server_close()
    finally:
        first.server_close()


# --------------------------------------------------------------------------- demo mode
def test_demo_needs_no_data_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv("PMUKIT_DATA", str(missing))
    c = Client(demo=True)
    try:
        status, payload = c.call("GET", "/api/projects")
        assert status == 200
        assert payload["demo"] is True
        assert [p["name"] for p in payload["projects"]]
        for path in ("/api/p/demo_pmu/pins", "/api/p/demo_pmu/plan", "/api/p/demo_pmu/ledger",
                     "/api/p/demo_pmu/model/grades", "/api/p/demo_pmu/deliverables"):
            s, d = c.call("GET", path)
            assert s == 200, (path, d)
            assert d
    finally:
        c.close()
    assert not missing.exists(), "--demo must not create anything under $PMUKIT_DATA"


def test_demo_curve_gives_both_sides_on_the_same_points(demo):
    s, d = demo.call("GET", "/api/p/demo_pmu/model/curve?port=VDD0P8_A&cell=ss/25c&block=zout")
    assert s == 200
    assert len(d["x"]) == len(d["gt"]["mag"]) == len(d["model"]["mag"]) > 10
    assert len(d["gt"]["phase_deg"]) == len(d["x"])
    assert all(v > 0 for v in d["gt"]["mag"])


def test_demo_refuses_to_create_projects(demo):
    status, payload = demo.call("POST", "/api/projects", {"name": "nope"})
    assert status == 400
    four_part(payload)


# --------------------------------------------------------------------------- path traversal
@pytest.mark.parametrize("stamp,name", [
    ("..", "report.md"),
    ("20260915-140211", ".."),
    ("20260915-140211", "..%2F..%2Fsite.json"),
    ("%2e%2e", "report.md"),
    ("20260915-140211", "%2e%2e%5cconfig.json"),
    (".", "config.json"),
    ("20260915-140211", "~root"),
])
def test_deliverable_file_refuses_to_walk_out(demo, stamp, name):
    status, payload = demo.call("GET", f"/api/p/demo_pmu/deliverables/{stamp}/files/{name}")
    assert status != 200, f"{stamp}/{name} must not be served"
    # Either the route does not match at all (404) or the name is refused (400) -- both carry
    # the four-part body, and neither ever returns file content.
    four_part(payload)


def test_deliverable_file_refuses_an_absolute_path(demo):
    status, payload = demo.call(
        "GET", "/api/p/demo_pmu/deliverables/20260915-140211/files/"
               + urllib.parse.quote("C:\\Windows\\win.ini", safe=""))
    assert status != 200
    four_part(payload)


def test_safe_name_rejects_the_usual_suspects():
    for bad in ("..", ".", "a/b", "a\\b", "~/x", "", "a..b/c"):
        with pytest.raises(PmuError):
            server._safe_name(bad, server.FILENAME_RE, "file name", "test")
    assert server._safe_name("report.md", server.FILENAME_RE, "file name", "test") == "report.md"


# --------------------------------------------------------------------------- the page
def test_page_is_served_at_the_root(demo):
    status, body = demo.call("GET", "/")
    assert status == 200
    assert isinstance(body, str)
    assert body.lstrip().lower().startswith("<!doctype html")
    assert "pmukit" in body


def test_page_has_no_external_resource():
    """The box has no network. One inlined file, no CDN, no font service, no analytics."""
    text = PAGE.read_text(encoding="utf-8")
    for scheme in ("http" + "://", "https" + "://"):
        assert scheme not in text, f"index.html reaches out to {scheme}"
    assert "<script src" not in text.replace(" ", "")
    assert "<link" not in text or "stylesheet" not in text


def test_page_is_one_file_with_one_script_block():
    text = PAGE.read_text(encoding="utf-8")
    assert len(re.findall(r"<script[^>]*>", text)) == 1
    assert len(re.findall(r"<style[^>]*>", text)) == 1


def test_page_javascript_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH; the JS syntax gate needs it")
    text = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)
    assert blocks, "the page has no script block"
    js = tmp_path / "page.js"
    js.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    p = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


#: A fake DOM small enough to fit in this file and real enough to catch the bugs that matter:
#: a screen that throws, a template that renders the word "undefined", or a tag that carries two
#: class attributes (the browser keeps the first and silently drops the second, which is how a
#: row loses its click handling). Node runs the page's own script -- nothing is re-implemented.
RENDER_CHECK_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const FIX = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
  classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
  querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
  focus(){}, click(){}, appendChild(){}, removeChild(){} }; }
const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
  querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
  body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
const sandbox = { document, navigator:{}, console, Math, JSON, Date, Number, String, Object,
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
  window:{ innerWidth:1440, innerHeight:900, location:{search:'',reload(){}},
           prompt:()=>null, alert:()=>{}, addEventListener:()=>{} },
  fetch: () => new Promise(()=>{}), setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
sandbox.globalThis = sandbox; sandbox.window.document = document;
vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
const S = sandbox.S; let bad = 0;
if (!S || !sandbox.SCREENS) { console.log('FAIL the page exposes no state'); process.exit(1); }
for (const [screen, data] of Object.entries(FIX)) {
  S.screen = screen; S.project = 'demo_pmu'; S.data = Object.assign({}, data);
  S.loading = {}; S.errs = {}; S.sel = {};
  try {
    sandbox.render();
    const html = nodes.main.innerHTML;
    if (!html || html.length < 40) throw new Error('main rendered ' + html.length + ' bytes');
    if (/undefined/.test(html)) throw new Error('rendered the word "undefined"');
    for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
      if ((tag.match(/ class=/g) || []).length > 1)
        throw new Error('two class attributes: ' + tag.slice(0, 110));
    }
    if (!nodes.foot.innerHTML) throw new Error('no footer');
    console.log(screen.padEnd(9), String(html.length).padStart(7), 'OK');
  } catch (e) { bad++; console.log(screen.padEnd(9), 'FAIL', e.message); }
}
S.screen = 'plan';
S.errs.plan = { status:400, error:{what:'w', why:'y', do:['Go to New','Retry'], where:'f.scs'},
                retry(){} };
sandbox.render();
if (!/Do<\/span>/.test(nodes.main.innerHTML)) { bad++; console.log('FAIL the four-part error has no Do row'); }
for (const kind of Object.keys(sandbox.VERBS)) {
  const items = sandbox.verbsFor(kind, { row:{ name:'x', role:'rail', status:'failed',
                                               run_id:'abc', id:'g1', bytes:1, priority:0 } });
  if (!items.length) { bad++; console.log('FAIL menu', kind, 'is empty'); }
}
console.log('objects with a menu:', Object.keys(sandbox.VERBS).length);
process.exit(bad ? 1 : 0);
"""


def test_page_renders_every_screen_without_throwing(tmp_path, demo):
    """The page is executed for real, once per screen, on payloads the API actually returns."""
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the render gate needs it")

    def g(path):
        status, payload = demo.call("GET", path)
        return payload if status == 200 else None

    plan = g("/api/p/demo_pmu/plan") or {}
    group = (plan.get("groups") or [{}])[0].get("id", "")
    ledger = g("/api/p/demo_pmu/ledger") or {}
    run0 = (ledger.get("rows") or [{}])[0].get("run_id", "")
    fixtures = {
        "home": {"projects": g("/api/projects"), "machine": g("/api/machine"),
                 "state": g("/api/state/demo_pmu")},
        "new": {"pins": g("/api/p/demo_pmu/pins"), "config": g("/api/p/demo_pmu/config"),
                "netlistsrc": g("/api/p/demo_pmu/netlist")},
        "plan": {"plan": plan, "consequences": g("/api/p/demo_pmu/plan/consequences"),
                 "planruns:" + group: g("/api/p/demo_pmu/plan/runs?group="
                                        + urllib.parse.quote(group))},
        "run": {"ledger:all": ledger, "run:" + run0: g(f"/api/p/demo_pmu/runs/{run0}")},
        "model": {"model:summary": g("/api/p/demo_pmu/model/summary"),
                  "model:grades": g("/api/p/demo_pmu/model/grades")},
        "deliver": {"deliverables": g("/api/p/demo_pmu/deliverables")},
        "digest": {"digest": g("/api/p/demo_pmu/digest/blocks")},
        "states": {},
    }
    page_js = tmp_path / "page.js"
    blocks = re.findall(r"<script[^>]*>([\s\S]*?)</script>", PAGE.read_text(encoding="utf-8"))
    page_js.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    check_js = tmp_path / "render_check.js"
    check_js.write_text(RENDER_CHECK_JS, encoding="utf-8", newline="\n")
    fix_json = tmp_path / "fixtures.json"
    fix_json.write_text(json.dumps(fixtures), encoding="utf-8", newline="\n")

    p = subprocess.run([node_exe, str(check_js), str(page_js), str(fix_json)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    for screen in ("home", "new", "plan", "run", "model", "deliver", "digest", "states"):
        assert f"{screen:<9}" in p.stdout, f"{screen} was not rendered:\n{p.stdout}"


def test_page_declares_every_screen_and_every_object_menu():
    text = PAGE.read_text(encoding="utf-8")
    for screen in helptext.SCREENS:
        assert f"SCREENS.{screen} =" in text, f"the page has no {screen} screen"
    # one verb table, and every object the UX rules name has an entry in it
    for obj in ("project", "pin", "corner", "rail", "group", "planrun", "run", "cell", "block",
                "chart", "file", "digest"):
        assert re.search(rf"^\s+{obj}: \[", text, re.M), f"no verb list for {obj}"


def test_page_is_lf_only():
    assert b"\r\n" not in PAGE.read_bytes()


# --------------------------------------------------------------------------- help
def test_help_route_is_the_shared_dict(demo):
    for screen in helptext.SCREENS:
        status, payload = demo.call("GET", f"/api/help/{screen}")
        assert status == 200
        assert payload == helptext.as_dict(screen)
        assert payload["text"] == helptext.render(screen)
        assert len(payload["lines"]) == 3 or screen == "states"
        assert payload["global_keys"]


def test_help_for_an_unknown_screen_answers_instead_of_raising(demo):
    status, payload = demo.call("GET", "/api/help/not-a-screen")
    assert status == 200
    assert payload["known"] == list(helptext.SCREENS)
    assert "not-a-screen" in payload["text"]


# --------------------------------------------------------------------------- command echo
@pytest.mark.parametrize("screen", list(helptext.SCREENS))
def test_cli_echo_exists_for_every_screen(demo, screen):
    status, payload = demo.call("GET", f"/api/cli?screen={screen}&project=demo_pmu")
    assert status == 200
    assert payload["cli"].startswith("pmukit ")


def test_cli_echo_mirrors_what_the_user_did():
    cmd = server.cli_echo("plan", {"ticks": {"g1": False, "g2": True}}, "demo_pmu")
    assert "pmukit plan demo_pmu" in cmd
    assert "--skip g1" in cmd
    assert "g2" not in cmd.split("--skip")[1]
    cmd = server.cli_echo("new", {"netlist": "tb/input.scs", "pmu_inst": "PMU_TOP",
                                  "corners": ["tt", "ss"], "temps": [-40, 125], "vset": [3],
                                  "loads": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6,
                                                         "switches": True}},
                                  "stubs": ["VDD0P8_C"], "fmax": 2e10}, "demo_pmu")
    assert "--netlist tb/input.scs" in cmd
    assert "--corners tt,ss" in cmd
    assert "--load VDD0P8_A=500uA/2uA/switch" in cmd
    assert "--stub VDD0P8_C" in cmd
    assert server.cli_echo("run", {"run": "abc123", "action": "retry"}, "p") \
        == "pmukit run p --retry abc123"


# --------------------------------------------------------------------------- jobs
def test_a_job_runs_on_a_background_thread_and_is_polled(demo):
    done = threading.Event()

    def work(job):
        job.say("half way", 0.5)
        done.wait(5)
        return {"answer": 42}

    job = server.JOBS.submit("test", "demo_pmu", "a test job", work)
    status, payload = demo.call("GET", f"/api/jobs/{job.id}")
    assert status == 200
    assert payload["status"] in ("queued", "running")
    done.set()
    for _ in range(200):
        status, payload = demo.call("GET", f"/api/jobs/{job.id}")
        if payload["status"] not in ("queued", "running"):
            break
    assert payload["status"] == "done"
    assert payload["result"] == {"answer": 42}
    assert payload["progress"] == 1.0
    assert any(e["text"] == "half way" for e in payload["events"])


def test_a_job_that_raises_becomes_a_four_part_error(demo):
    def boom(job):
        raise PmuError(what="it broke.", why="on purpose.", do=["read the test"], where="here")

    job = server.JOBS.submit("test", "demo_pmu", "a failing job", boom)
    for _ in range(200):
        status, payload = demo.call("GET", f"/api/jobs/{job.id}")
        if payload["status"] not in ("queued", "running"):
            break
    assert payload["status"] == "failed"
    for key in ("what", "why", "do", "where"):
        assert key in payload["error"]


def test_an_unknown_job_is_a_four_part_error(demo):
    status, payload = demo.call("GET", "/api/jobs/0000deadbeef")
    assert status == 400
    four_part(payload)


# --------------------------------------------------------------------------- machine probes
def test_machine_never_hangs_and_each_probe_carries_a_reason(demo):
    status, payload = demo.call("GET", "/api/machine")
    assert status == 200
    names = [p["name"] for p in payload["probes"]]
    assert names == ["engine", "queue", "pdk", "license"]
    for probe in payload["probes"]:
        assert isinstance(probe["ok"], bool)
        if not probe["ok"]:
            for key in ("what", "why", "do", "where"):
                assert key in probe["reason"], f"{probe['name']} failed with a bare false"


def test_real_machine_probe_answers_within_its_own_deadline(live):
    """Every probe has its own timeout and the endpoint has an overall one; a dead VM must make
    Home slow, never stuck."""
    import time
    t0 = time.time()
    status, payload = live.call("GET", "/api/machine")
    elapsed = time.time() - t0
    assert status == 200
    assert elapsed < server.MACHINE_DEADLINE + 10
    for probe in payload["probes"]:
        if not probe["ok"]:
            assert probe["reason"]["do"], "a failing probe must say what to do"


# --------------------------------------------------------------------------- json hygiene
def test_nan_never_reaches_the_page(live, tmp_path):
    """The ledger really stores NaN (the temperature-sweep run has no single temperature) and
    JSON has no NaN: JSON.parse would throw and the screen would go blank."""
    assert server._clean(float("nan")) is None
    assert server._clean(float("inf")) is None
    assert server._clean({"a": [float("nan"), 1.0]}) == {"a": [None, 1.0]}
    body = json.dumps(server._clean({"t": float("nan")}), allow_nan=False)
    assert body == '{"t": null}'


def test_clean_handles_paths_sets_and_complex():
    out = server._clean({"p": pathlib.Path("a/b"), "s": {2, 1}, "z": complex(3, 4)})
    assert out["p"].replace("\\", "/") == "a/b"
    assert out["s"] == [1, 2]
    assert out["z"] == {"re": 3.0, "im": 4.0}


# --------------------------------------------------------------------------- module gating
def test_a_module_that_is_not_there_answers_501_not_a_crash(live, monkeypatch):
    """Other milestones land in parallel. A route whose module is missing must say so in four
    parts and leave every other screen usable."""
    with pytest.raises(server.NotLanded) as exc:
        server._lazy("pmukit.definitely_not_a_module", "A test")
    err = exc.value.error
    assert "pmukit.definitely_not_a_module" in err.what
    assert err.do and err.where


def test_state_route_round_trips(live):
    live.call("POST", "/api/projects", {"name": "state_demo"})
    status, payload = live.call("PUT", "/api/state/state_demo", {"screen": "plan",
                                                                 "note": "hello"})
    assert status == 200
    status, payload = live.call("GET", "/api/state/state_demo")
    assert status == 200
    assert payload["screen"] == "plan"
    assert payload["recent"][0]["text"] == "hello"


# --------------------------------------------------------------------------- UI state file
def test_ui_state_missing_file_is_a_new_project_not_an_error(tmp_path):
    from pmukit.state import UiState
    st = UiState.load("brand_new", tmp_path)
    assert st.exists is False
    assert st.screen == "new"
    assert st.recent == []


def test_ui_state_is_written_atomically_and_reloads(tmp_path):
    from pmukit.state import UiState
    st = UiState.load("p", tmp_path)
    st.go("model").note("fit finished")
    st.save()
    again = UiState.load("p", tmp_path)
    assert again.exists is True
    assert again.screen == "model"
    assert again.recent[0]["text"] == "fit finished"
    assert b"\r\n" not in (tmp_path / "p" / "state.json").read_bytes()


def test_ctrl_z_undoes_the_last_configuration_change_whichever_kind(tmp_path):
    """UX_RULES: configuration changes are undoable, submitted runs are not. One stack, so one
    Ctrl-Z always undoes the thing that actually happened last."""
    from pmukit.config import ProjectConfig
    from pmukit.state import UiState
    base = {"project": "p", "netlist": "n.scs", "pmu_inst": "X", "corners": ["tt"],
            "temps_c": [25.0], "vset_codes": [3], "ports": {"A": "model"},
            "my_load": {"A": {"on_a": 1e-3, "off_a": 1e-6}}, "care_up_to_hz": 1e9}
    st = UiState.load("p", tmp_path)
    (tmp_path / "p").mkdir(parents=True, exist_ok=True)
    hist = st.history()
    hist.push(ProjectConfig.from_dict(base), "first")
    st.record_config_change("first")
    two = dict(base, corners=["tt", "ss"])
    hist.push(ProjectConfig.from_dict(two), "second")
    st.record_config_change("second")
    st.set_ticks({"g1": False}, "untick g1")
    st.save()

    st = UiState.load("p", tmp_path)
    assert st.undoable() == "plan_ticks"
    kind, payload = st.undo()
    assert kind == "plan_ticks" and payload == {}
    assert st.undoable() == "config"
    kind, cfg = st.undo()
    assert kind == "config"
    assert cfg.corners == ["tt"]
    assert st.undoable() == ""
    with pytest.raises(PmuError):
        st.undo()


def test_undo_never_touches_a_submitted_run(tmp_path):
    from pmukit.state import UiState
    st = UiState.load("p", tmp_path)
    with pytest.raises(PmuError) as exc:
        st.undo()
    assert "skip" in " ".join(exc.value.do).lower() or "kill" in " ".join(exc.value.do).lower()


def test_screens_come_from_helptext_so_they_cannot_drift():
    from pmukit import state
    assert state.SCREENS is helptext.SCREENS
    assert set(state.SCREEN_INDEX) <= set(helptext.SCREENS)


# --------------------------------------------------------------------------- main()
def test_main_accepts_both_call_styles(monkeypatch):
    seen = {}

    def fake_serve(host, port, **kw):
        seen.update(dict(host=host, port=port, **kw))

    monkeypatch.setattr(server, "serve", fake_serve)
    server.main(["--host", "127.0.0.1", "--port", "9111", "--demo"])
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9111 and seen["demo"] is True
    seen.clear()
    # the shape pmukit.cli calls it with
    server.main(host="127.0.0.1", port=8765, demo=False, open_browser=False, project="demo_pmu")
    assert seen["project"] == "demo_pmu" and seen["demo"] is False
