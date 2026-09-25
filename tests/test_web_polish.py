"""Second browser QA pass: the shell's smaller correctness items, pinned.

* Settings shows only the fields the chosen engine reads (fake / dry_run read none);
* a failed job's error is shown ONCE -- the four-part banner; the job strip only points at it;
* a job strip belongs to the screen that started it and clears itself after a success;
* a reload rebuilds the nav progress and the New screen's command strip from the server;
* "Copy failure bundle" builds one bounded, secrets-free text blob, copies it and offers it as
  `<run_id>_bundle.txt`.

The page's own script runs in node against a scripted fetch, as in test_web_robustness.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse

import pytest

from pmukit import server
from pmukit.ledger import Recipe, Run
from tests.test_server import PAGE

HARNESS_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const clicks = [], blobs = [], calls = [], clip = [], timers = [];
function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
  classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
  querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
  focus(){}, click(){ clicks.push(this); }, appendChild(){}, removeChild(){} }; }
const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
  querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
  body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
let ROUTES = {};
function fetch(path, init){
  const method = (init && init.method) || 'GET';
  const body = init && init.body ? JSON.parse(init.body) : undefined;
  calls.push({ method, path, body });
  let r = ROUTES[method + ' ' + path];
  if (r === undefined) r = ROUTES[method + ' ' + path.split('?')[0]];
  if (r === undefined) return new Promise(()=>{});
  if (typeof r === 'function') r = r(body, path);
  return Promise.resolve({ ok: r[0] < 400, status: r[0],
                           text: () => Promise.resolve(JSON.stringify(r[1])) });
}
class Blob { constructor(parts, opts){ this.text = parts.join(''); this.type = (opts||{}).type; } }
const URL = { createObjectURL:(b)=>{ blobs.push(b); return 'blob:1'; }, revokeObjectURL(){} };
let T = 0;
const sandbox = { document, console, Math, JSON, Date, Number, String, Object,
  navigator:{ clipboard:{ writeText:(t)=>{ clip.push(t); return Promise.resolve(); } } },
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams, Blob, URL,
  window:{ innerWidth:1440, innerHeight:900, location:{search:'',reload(){}},
           addEventListener:()=>{} },
  fetch, setTimeout:(fn, ms)=>{ timers.push({fn, ms}); return ++T; }, clearTimeout:()=>{},
  setInterval:()=>0 };
sandbox.globalThis = sandbox; sandbox.window.document = document;
const flush = () => new Promise(r => setImmediate(() => setImmediate(r)));
async function runTimers(maxMs){
  for (let n = 0; n < 50; n++){
    const due = timers.filter(t => t.ms <= maxMs); if (!due.length) return;
    for (const t of due){ timers.splice(timers.indexOf(t), 1); t.fn(); }
    await flush();
  }
}
const act = (html, re) => { const m = html.match(re); if (!m) throw new Error('no action ' + re); return sandbox.ACTS[m[1]]; };
const out = {};
const CONFIG = { exists:true, sha:'c0', undoable:'', config:{ corners:['ss','ff'], temps_c:[-40,125],
  vset_codes:[2,3], vset_param:'VSET', care_up_to_hz:3e9,
  ports:{ VDD0P8_A:'model', VDD0P8_C:'stub', TESTMODE:'ignore' },
  my_load:{ VDD0P8_A:{on_a:5e-4, off_a:2e-6, switches:true} } } };
const PINS = { pmu_inst:'PMU_TOP', pmu_master:'pmu_demo', pins:[], candidates:[],
  sections:{}, params:{ VSET:'3' }, analyses:[], notes:[],
  summary:{ rails:1, biases:0, stubs:1, unclassified:[] }, netlist:{ path:'x' } };

// ---- reload: the page boots against a project it was left on
ROUTES['GET /api/projects'] = [200, { projects:[{ name:'p', screen:'new', step:1 }],
                                      initial_project:'p', demo:false }];
ROUTES['GET /api/state/p'] = [200, { project:'p', screen:'new',
                                     progress:{ new:true, plan:true, run:false, model:false, deliver:false } }];
ROUTES['PUT /api/state/p'] = [200, { project:'p', screen:'new' }];
ROUTES['GET /api/p/p/netlist'] = [200, { source:{ path:'/w/tb/input.scs', name:'input.scs', via:'path' },
                                         copy:{ path:'/d/p/netlists/input.scs', sha:'abc', bytes:10 },
                                         candidates:[], cwd:'/w', attempt:null }];
ROUTES['GET /api/p/p/config'] = [200, CONFIG];
ROUTES['GET /api/p/p/pins'] = [200, PINS];
ROUTES['GET /api/cli'] = (b, path) => [200, { cli: 'cli#' + calls.length }];
vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
const S = sandbox.S;

(async () => {
  for (let i = 0; i < 6; i++) { await flush(); }
  await runTimers(100);
  for (let i = 0; i < 4; i++) { await flush(); }
  sandbox.render();
  const cli = calls.filter(c => c.path.indexOf('/api/cli?') === 0);
  out.reload = { screen: S.screen, nav: nodes.nav.innerHTML,
                 cli_last: cli.length ? cli[cli.length - 1].path : '' };

  // ---- Settings: per engine
  out.settings = {};
  for (const eng of ['fake', 'dry_run', 'donau_alps', 'spectre_ssh']){
    S.screen = 'settings'; S.errs = {}; S.failed = {};
    S.data.site = { engine: eng, simulator:'alps', queue:'short', cpus:8, ssh_host:'ewave-vm',
      remote_workdir:'~/pmukit_work', spectre_cmd:'spectre', accounts:[{name:'ug_a', note:''}],
      stored:{ engine: eng, simulator:'alps', queue:'short', cpus:8, ssh_host:'ewave-vm',
               remote_workdir:'~/pmukit_work', spectre_cmd:'spectre', project_account:'ug_a' },
      overrides:{}, engines:[{name:'donau_alps',note:''},{name:'spectre_ssh',note:''},
                             {name:'dry_run',note:''},{name:'fake',note:''}],
      simulators:['alps','spectre'], environment:[], path:'/d/site.json' };
    sandbox.render();
    out.settings[eng] = { html: nodes.main.innerHTML, cli: sandbox.SCREENS.settings.cli() };
  }

  // ---- a failed job: one error, the strip points at it
  S.screen = 'new'; S.errs = {}; S.failed = {}; S.job = null; calls.length = 0;
  ROUTES['POST /api/p/p/netlist'] = [200, { job:'j1', source:{} }];
  ROUTES['GET /api/jobs/j1'] = [200, { job:'j1', status:'failed', progress:0.2, title:'read bad.scs',
    message:'decap on a rail: Cdecap (capacitor) on rail VDD0P8_A (net VDD0P8_A, line 41).',
    error:{ what:'decap on a rail: Cdecap (capacitor) on rail VDD0P8_A (net VDD0P8_A, line 41).',
            why:'w', do:['Remove Cdecap'], where:'/w/tb/bad.scs:41: instance Cdecap' } }];
  sandbox.loadNetlist({ path:'/w/tb/bad.scs' });
  for (let i = 0; i < 6; i++) { await flush(); }
  sandbox.render();
  const html = nodes.main.innerHTML;
  const strip = (html.match(/<div class="prog"[\s\S]*?<\/div><\/div>[\s\S]*?<\/div>/) || [''])[0];
  out.failed = { html, strip, job: S.job && S.job.screen };

  // ---- a finished job: only on its own screen, and it clears itself
  S.screen = 'new'; S.errs = {}; S.job = null;
  ROUTES['POST /api/p/p/netlist'] = [200, { job:'j2', source:{} }];
  ROUTES['GET /api/jobs/j2'] = [200, { job:'j2', status:'done', progress:1, title:'read input.scs',
                                       message:'done', result:{} }];
  timers.length = 0;
  sandbox.loadNetlist({ path:'/w/tb/input.scs' });
  for (let i = 0; i < 6; i++) { await flush(); }
  out.done = { on_new: sandbox.jobBlock() };
  S.screen = 'plan';
  out.done.on_plan = sandbox.jobBlock();
  S.screen = 'new';
  await runTimers(10000);
  out.done.after = S.job === null;
  S.job = { job:'j3', status:'running', progress:0.5, title:'fitting', message:'x', screen:'run' };
  S.screen = 'model';
  out.done.running_elsewhere = sandbox.jobBlock();
  S.job = null;

  // ---- failure bundle
  S.screen = 'run'; S.errs = {}; S.failed = {}; S.sel = { run:'r1' }; S.toast = '';
  S.data['ledger:all'] = { total:1, counts:{ failed:1 }, rows:[{ run_id:'r1', status:'failed',
    cell_text:'tt 25C', analysis:'ac', stimulus:'IL_X', cpu_seconds:0 }] };
  S.data['run:r1'] = { run:{ run_id:'r1', status:'failed', cell_text:'tt 25C', analysis:'ac',
    stimulus:'IL_X', reads:[], recipe:'[edits]\n' }, log:'', why:'' };
  ROUTES['GET /api/p/p/runs/r1/bundle'] = [200, { run_id:'r1', name:'r1_bundle.txt',
                                                  text:'BUNDLE TEXT', bytes:11 }];
  sandbox.render();
  out.bundle = { before: nodes.main.innerHTML, foot: nodes.foot.innerHTML };
  act(nodes.main.innerHTML, /data-act="(a\d+)">Copy failure bundle for the desk/)();
  for (let i = 0; i < 6; i++) { await flush(); }
  sandbox.render();
  out.bundle.clip = clip.slice();
  out.bundle.toast = S.toast;
  out.bundle.after = nodes.main.innerHTML;
  act(nodes.main.innerHTML, /data-act="(a\d+)">Download r1_bundle\.txt/)();
  out.bundle.blob = blobs.length ? blobs[blobs.length - 1].text : null;
  out.bundle.download = clicks.length ? clicks[clicks.length - 1].download : null;
  out.bundle.gets = calls.filter(c => c.path === '/api/p/p/runs/r1/bundle').length;

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def page_run(tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page harness needs it")
    d = tmp_path_factory.mktemp("polish")
    page = PAGE.read_text(encoding="utf-8")
    page_js = d / "page.js"
    page_js.write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", page)),
                       encoding="utf-8", newline="\n")
    check = d / "harness.js"
    check.write_text(HARNESS_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(check), str(page_js)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- reload
def test_a_reload_rebuilds_the_nav_progress_from_the_server(page_run):
    r = page_run["reload"]
    assert r["screen"] == "new"
    nav = r["nav"]
    assert 'class="step cur" data-go="new"' in nav
    assert 'class="step done" data-go="plan"' in nav          # done on disk, not "before here"
    assert 'class="step" data-go="run"' in nav
    assert 'class="step" data-go="model"' in nav


def test_a_reload_rebuilds_the_new_screen_command_strip(page_run):
    q = urllib.parse.urlsplit(page_run["reload"]["cli_last"]).query
    params = urllib.parse.parse_qs(q)
    assert params["screen"] == ["new"]
    st = json.loads(params["state"][0])
    assert st["corners"] == ["ss", "ff"] and st["temps"] == [-40, 125] and st["vset"] == [2, 3]
    assert st["loads"]["VDD0P8_A"]["on_a"] == 5e-4
    cli = server.cli_echo("new", st, "p")
    for flag in ("--netlist /w/tb/input.scs", "--pmu-inst PMU_TOP", "--corners ss,ff",
                 "--temps -40,125", "--vset 2,3", "--load VDD0P8_A=0.0005,",
                 "--port VDD0P8_C=stub", "--care-up-to 3e+09"):
        assert flag in cli, (flag, cli)


def test_the_state_route_reports_progress_from_disk(tmp_path):
    api = server.Api(root=tmp_path / "data")
    api.new_project({"name": "p"})
    pr = server.Project("p", api.root)
    assert pr.progress() == {"new": False, "plan": False, "run": False, "model": False,
                             "deliver": False}
    pr.config_path.write_text("{}", encoding="utf-8")
    pr.fit_path.write_text("{}", encoding="utf-8")
    (pr.dir / "deliver" / "20260101-000000").mkdir(parents=True)
    with pr.ledger() as led:
        led.upsert(Run(run_id="aaaaaaaaaaaa", process="tt", temp_c=25.0, vset=3, load_key="",
                       analysis="ac", stimulus="IL_X", reads=[]))
    p = pr.progress()
    assert p == {"new": True, "plan": True, "run": False, "model": True, "deliver": True}
    with pr.ledger() as led:
        led.set_status("aaaaaaaaaaaa", "done", finished=True)
    assert pr.progress()["run"] is True


# --------------------------------------------------------------------------- settings
@pytest.mark.parametrize("engine", ["fake", "dry_run"])
def test_settings_shows_no_queue_fields_for_an_engine_that_reads_none(page_run, engine):
    html = page_run["settings"][engine]["html"]
    for label in ("Simulator", "Queue", "CPUs per job", "SSH host", "Remote work dir",
                  "Spectre command", "Donau accounts"):
        assert f">{label}<" not in html, (engine, label)
    assert "needs nothing else from this machine" in html
    assert page_run["settings"][engine]["cli"] == {"engine": engine}


def test_settings_for_donau_shows_simulator_queue_cpus_and_accounts(page_run):
    html = page_run["settings"]["donau_alps"]["html"]
    for label in ("Simulator", "Queue", "CPUs per job"):
        assert f">{label}<" in html
    assert "Donau accounts" in html and "ug_a" in html
    for label in ("SSH host", "Remote work dir", "Spectre command"):
        assert f">{label}<" not in html
    cli = page_run["settings"]["donau_alps"]["cli"]
    assert cli == {"engine": "donau_alps", "simulator": "alps", "queue": "short", "cpus": 8,
                   "account": "ug_a"}


def test_settings_for_spectre_ssh_shows_host_workdir_and_command(page_run):
    html = page_run["settings"]["spectre_ssh"]["html"]
    for label in ("SSH host", "Remote work dir", "Spectre command"):
        assert f">{label}<" in html
    for label in ("Simulator", "Queue", "CPUs per job"):
        assert f">{label}<" not in html
    assert "Donau accounts" not in html
    cli = server.cli_echo("settings", page_run["settings"]["spectre_ssh"]["cli"])
    assert cli == ("pmukit site --engine spectre_ssh --ssh-host ewave-vm "
                   "--remote-workdir ~/pmukit_work --spectre-cmd spectre")


def test_the_site_route_carries_the_ssh_fields(tmp_path):
    api = server.Api(root=tmp_path / "data")
    s = api.site_put({"engine": "spectre_ssh", "remote_workdir": "/scratch/me",
                      "spectre_cmd": "spectre231"})
    assert s["remote_workdir"] == "/scratch/me" and s["spectre_cmd"] == "spectre231"
    assert s["stored"]["remote_workdir"] == "/scratch/me"


# --------------------------------------------------------------------------- jobs
def test_a_failed_job_shows_its_error_once(page_run):
    f = page_run["failed"]
    what = "decap on a rail: Cdecap"
    assert what in f["html"] and "/w/tb/bad.scs:41" in f["html"]      # the banner, with Where
    assert f["strip"], "the strip is still there, pointing at the banner"
    assert what not in f["strip"]
    assert "see the error above" in f["strip"]
    assert f["job"] == "new"


def test_a_finished_job_strip_stays_on_its_screen_and_clears_itself(page_run):
    d = page_run["done"]
    assert "read input.scs" in d["on_new"]
    assert d["on_plan"] == ""                # it does not follow you to Plan
    assert d["after"] is True                # and clears itself a few seconds after success
    assert "fitting" in d["running_elsewhere"]   # a job still working is visible everywhere


# --------------------------------------------------------------------------- failure bundle
def test_copy_failure_bundle_copies_and_offers_the_file(page_run):
    b = page_run["bundle"]
    assert "Copy failure bundle for the desk" in b["before"]
    assert "Copy failure bundle of r1" in b["foot"]
    assert b["gets"] == 1
    assert b["clip"] == ["BUNDLE TEXT"]
    assert b["toast"] == "failure bundle copied"
    assert "Failure bundle copied to the clipboard" in b["after"]
    assert b["blob"] == "BUNDLE TEXT" and b["download"] == "r1_bundle.txt"


def test_the_failure_bundle_route_is_bounded_and_secrets_free(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_SIM_ROOT", str(tmp_path / "sim"))
    monkeypatch.setenv("PMK_TEST_SECRET", "hunter2-do-not-leak")
    api = server.Api(root=tmp_path / "data")
    api.new_project({"name": "p"})
    pr = server.Project("p", api.root)
    rid = "0123456789ab"
    submit = "dsub -A ug_a -q short -R 'cpu=8;mem=8000' -- alps input.scs"
    recipe = Recipe(edits=["~ parameters VSET=2        // was: parameters VSET=3"],
                    analyses=["acZ ac start=10 stop=1G"], saves=[], submit=submit).text()
    with pr.ledger() as led:
        led.upsert(Run(run_id=rid, process="ss", temp_c=125.0, vset=2, load_key="",
                       analysis="ac", stimulus="IL_VDD0P8_A", reads=["ac_zout.VDD0P8_A"],
                       engine="donau_alps", recipe=recipe))
        led.set_status(rid, "failed", error="rc=1", job_id="4242", finished=True)
    wd = server.paths.runs_dir("p") / rid
    wd.mkdir(parents=True)
    deck = "simulator lang=spectre\n" + "".join(f"R{i} (a b) resistor r=1k\n" for i in range(5000))
    (wd / "input.scs").write_text(deck, encoding="utf-8", newline="\n")
    log = "".join(f"line {i}\n" for i in range(5000)) + "ERROR (SPECTRE-16): timestep too small\n"
    (wd / "spectre.log").write_text(log, encoding="utf-8", newline="\n")
    (wd / "job.err").write_text("dsub: job 4242 exited 1\n", encoding="utf-8", newline="\n")

    b = api.run_bundle("p", rid)
    t = b["text"]
    assert b["name"] == f"{rid}_bundle.txt" and b["bytes"] == len(t.encode("utf-8"))
    assert f"run {rid}" in t and "pmukit " in t
    assert submit in t                                           # the submit command
    assert "timestep too small" in t and "line 4999" in t and "line 10\n" not in t
    assert "dsub: job 4242 exited 1" in t                        # the scheduler's output
    assert '"job_id": "4242"' in t and '"status": "failed"' in t   # the ledger row
    assert "simulator lang=spectre" in t and "R4999 (a b)" in t   # the deck, head and tail
    assert "cut from the middle" in t
    assert len(t) < 120_000
    assert "hunter2-do-not-leak" not in t and "PMK_TEST_SECRET" not in t

    # over HTTP too, and a run that is not in the ledger is a four-part error
    with pytest.raises(server.PmuError):
        api.run_bundle("p", "ffffffffffff")


def test_the_failure_bundle_route_answers_in_demo():
    api = server.Api(demo=True)
    rid = server._demo_ledger_rows()[0]["run_id"]
    b = api.run_bundle("demo_pmu", rid)
    assert b["name"] == f"{rid}_bundle.txt" and rid in b["text"]
