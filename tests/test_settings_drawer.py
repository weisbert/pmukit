"""Settings is a drawer over the screen it was opened on, not a screen of its own.

* it opens from the nav button, the "Runs go" line, Ctrl K and an old `?screen=settings` link;
* the screen under it and the step bar stay exactly as they were, and Esc puts you back;
* while it is open the screen under it takes no keys (no Ctrl Enter submit, no digit jump);
* the Parallel jobs field saves through PUT /api/site and shows the CPU footprint;
* "Runs go" on Plan and Run says engine, queue, account, CPUs x jobs -- and opens the drawer.

The page's own script runs in node against a scripted fetch, as in test_web_polish.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.test_server import PAGE

HARNESS_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const search = process.argv[3] || '';
const calls = [], listeners = {};
function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
  classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
  querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
  focus(){}, click(){}, appendChild(){}, removeChild(){} }; }
const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
  querySelectorAll:()=>[], createElement:(t)=>node(t),
  addEventListener:(type, fn)=>{ (listeners[type] = listeners[type] || []).push(fn); },
  body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
const SITE = (jobs) => ({ engine:'donau_alps', simulator:'alps', simulator_source:'site config',
  queue:'short', cpus:8, jobs: jobs, jobs_default:4, max_jobs:64, ssh_host:'ewave-vm',
  remote_workdir:'~/w', spectre_cmd:'spectre', accounts:[{name:'ug_a', note:'small'}],
  account:'ug_a', account_source:'site config',
  stored:{ engine:'donau_alps', simulator:'alps', queue:'short', cpus:8, jobs: jobs,
           ssh_host:'ewave-vm', remote_workdir:'~/w', spectre_cmd:'spectre', project_account:'ug_a' },
  overrides:{}, engines:[{name:'donau_alps', note:'the queue', default_jobs:4},
                         {name:'spectre_ssh', note:'ssh', default_jobs:1},
                         {name:'dry_run', note:'nothing', default_jobs:1},
                         {name:'fake', note:'synthetic', default_jobs:1}],
  simulators:['alps','spectre'], environment:[], path:'/d/site.json' });
let ROUTES = {};
ROUTES['GET /api/projects'] = [200, { projects:[{ name:'p', screen:'plan', step:2 }],
                                      initial_project:'p', demo:false }];
ROUTES['GET /api/state/p'] = [200, { project:'p', screen:'plan',
                                     progress:{ new:true, plan:false, run:false, model:false, deliver:false } }];
ROUTES['PUT /api/state/p'] = [200, { project:'p' }];
ROUTES['GET /api/cli'] = (b, path) => [200, { cli: path.indexOf('screen=settings') >= 0
                                                    ? 'pmukit site --engine donau_alps' : 'pmukit plan p' }];
ROUTES['GET /api/site'] = [200, SITE(null)];
ROUTES['PUT /api/site'] = (b) => [200, SITE(b.jobs === null ? null : Number(b.jobs))];
ROUTES['GET /api/help/settings'] = [200, { title:'Settings', lines:['x'], keys:[], global_keys:[], screen:'settings' }];
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
const sandbox = { document, console, Math, JSON, Date, Number, String, Object,
  navigator:{ clipboard:{ writeText:()=>Promise.resolve() } },
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
  window:{ innerWidth:1440, innerHeight:900, location:{ search, reload(){} },
           history:{ replaceState(){} }, addEventListener:()=>{} },
  fetch, setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
sandbox.globalThis = sandbox; sandbox.window.document = document;
const flush = () => new Promise(r => setImmediate(() => setImmediate(r)));
const settle = async () => { for (let i = 0; i < 8; i++) await flush(); };
const key = (k, opts) => {
  const e = Object.assign({ key:k, target:{ tagName:'BODY' }, ctrlKey:false, metaKey:false,
                            altKey:false, preventDefault(){}, stopPropagation(){} }, opts || {});
  for (const fn of listeners.keydown || []) fn(e);
};
/* the html of a screen without the per-render action ids, which renumber on every render */
const bare = (html) => html.replace(/data-(act|change|enter)="a\d+"/g, '');
const act = (html, re) => { const m = html.match(re); if (!m) throw new Error('no action ' + re); return sandbox.ACTS[m[1]]; };
vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
const S = sandbox.S;
const out = {};

(async () => {
  await settle();
  sandbox.render();
  out.boot = { screen: S.screen, settings: S.settings, overlays: nodes.overlays.innerHTML };
  if (search){ console.log(JSON.stringify(out)); return; }

  // ---- the screen under the drawer: Plan, with its step bar and its foot
  sandbox.render();
  const before = { main: bare(nodes.main.innerHTML), nav: bare(nodes.nav.innerHTML),
                   foot: bare(nodes.foot.innerHTML) };
  out.plan_foot = nodes.foot.innerHTML;

  // ---- open it from the "Runs go" line (data-go="settings" is go("settings"))
  sandbox.go('settings');
  const first = nodes.overlays.innerHTML;          // the render that opened it: slides in
  await settle();
  out.open = { first, screen: S.screen, settings: S.settings, overlays: nodes.overlays.innerHTML,
               nav: nodes.nav.innerHTML, cli: S.cli, settingsCli: S.settingsCli,
               main_same: bare(nodes.main.innerHTML) === before.main,
               foot_same: bare(nodes.foot.innerHTML) === before.foot,
               state_puts: calls.filter(c => c.method === 'PUT' && c.path === '/api/state/p'
                                            && c.body && c.body.screen === 'settings').length };

  // ---- keys: the screen under it takes none; typing in the drawer is typing
  const posts = () => calls.filter(c => c.method === 'POST').length;
  const p0 = posts();
  key('3'); key('2', { target:{ tagName:'INPUT' } });
  key('Enter', { ctrlKey:true });
  key('z', { ctrlKey:true });
  await settle();
  out.keys = { screen: S.screen, settings: S.settings, posts: posts() - p0 };

  // ---- Parallel jobs: saved, and the footprint follows
  const html = nodes.overlays.innerHTML;
  act(html, /id="setjobs"[^>]*data-change="(a\d+)"/)('6');
  await settle();
  const put = calls.filter(c => c.method === 'PUT' && c.path === '/api/site');
  out.jobs = { body: put[put.length - 1].body, overlays: nodes.overlays.innerHTML,
               settingsCli: S.settingsCli };
  act(nodes.overlays.innerHTML, /id="setjobs"[^>]*data-change="(a\d+)"/)('  ');
  await settle();
  const put2 = calls.filter(c => c.method === 'PUT' && c.path === '/api/site');
  out.jobs.cleared = put2[put2.length - 1].body;
  out.jobs.cleared_overlays = nodes.overlays.innerHTML;

  // ---- Esc: back where it was, even from a field inside it
  S.toast = '';                                     // the "saved" badge times out (no timers here)
  key('Escape', { target:{ tagName:'INPUT' } });
  await settle();
  out.closed = { screen: S.screen, settings: S.settings, overlays: nodes.overlays.innerHTML,
                 main_same: bare(nodes.main.innerHTML) === before.main,
                 nav_same: bare(nodes.nav.innerHTML) === before.nav };

  // ---- a digit works again once it is closed
  key('3');
  await settle();
  out.after_digit = S.screen;
  sandbox.render();
  out.run_foot = nodes.foot.innerHTML;

  // ---- going somewhere else from inside it (Ctrl K "Go to ...") closes it
  sandbox.go('settings');
  sandbox.go('plan');
  out.go_closes = { screen: S.screen, settings: S.settings };

  // ---- the nav button toggles it; the palette names it
  sandbox.render();
  act(nodes.nav.innerHTML, /data-act="(a\d+)"[^>]*>Settings</)();
  out.nav_toggle = [S.settings];
  sandbox.render();
  act(nodes.nav.innerHTML, /data-act="(a\d+)"[^>]*>Settings</)();
  out.nav_toggle.push(S.settings);
  out.palette = sandbox.paletteCommands().map(c => c.label).filter(l => /Settings/.test(l));
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run(tmp_path_factory, search=""):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page harness needs it")
    d = tmp_path_factory.mktemp("drawer")
    page_js = d / "page.js"
    page_js.write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>",
                                            PAGE.read_text(encoding="utf-8"))),
                       encoding="utf-8", newline="\n")
    check = d / "harness.js"
    check.write_text(HARNESS_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(check), str(page_js), search], capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def drawer(tmp_path_factory):
    return _run(tmp_path_factory)


def test_the_page_boots_on_the_project_screen_with_no_drawer(drawer):
    assert drawer["boot"]["screen"] == "plan" and drawer["boot"]["settings"] is False
    assert 'class="drawer' not in drawer["boot"]["overlays"]


def test_settings_opens_as_a_drawer_over_the_current_screen(drawer):
    o = drawer["open"]
    assert o["screen"] == "plan" and o["settings"] is True
    assert 'class="drawer enter"' in o["first"]
    assert 'class="drawer"' in o["overlays"], "a re-render must not slide it in again"
    assert 'role="dialog"' in o["overlays"]
    assert 'class="veil dim" data-close="settings"' in o["overlays"]       # a click beside it
    assert 'data-close="settings"' in o["overlays"].split('class="dh"')[1]  # and a Close button
    # the screen under it is untouched, and the step bar still says Plan
    assert o["main_same"] and o["foot_same"]
    assert 'class="step cur" data-go="plan"' in o["nav"] and 'aria-current="page"' in o["nav"]
    assert 'aria-expanded="true"' in o["nav"]
    assert o["state_puts"] == 0, "the drawer is never written into the project's state.json"
    # the strip under the screen keeps the screen's command; the drawer has its own
    assert o["cli"] == "pmukit plan p" and o["settingsCli"] == "pmukit site --engine donau_alps"
    assert "pmukit site --engine donau_alps" in o["overlays"]


def test_the_drawer_holds_the_same_content_in_one_column(drawer):
    html = drawer["open"]["overlays"]
    for text in ("Where runs go", "donau_alps", ">Simulator<", ">Queue<", ">CPUs per job<",
                 ">Parallel jobs<", "Donau accounts", "ug_a", "Read from this machine"):
        assert text in html, text
    assert "flex:0 0 480px" not in html                      # no second column any more
    # an unset jobs shows the engine's default as the placeholder and the footprint
    assert re.search(r'id="setjobs"[^>]*placeholder="4"[^>]*value=""', html)
    assert "8 CPUs &times; 4 jobs = 32 CPUs at once" in html


def test_the_screen_under_the_drawer_takes_no_keys(drawer):
    k = drawer["keys"]
    assert k["screen"] == "plan" and k["settings"] is True
    assert k["posts"] == 0, "Ctrl Enter submitted the plan from behind the drawer"


def test_parallel_jobs_saves_and_shows_the_cpu_footprint(drawer):
    j = drawer["jobs"]
    assert j["body"] == {"jobs": "6"}
    assert "8 CPUs &times; 6 jobs = 48 CPUs at once" in j["overlays"]
    assert re.search(r'id="setjobs"[^>]*value="6"', j["overlays"])
    assert j["cleared"] == {"jobs": None}                     # empty = the engine's default
    assert "8 CPUs &times; 4 jobs = 32 CPUs at once" in j["cleared_overlays"]


def test_esc_closes_the_drawer_and_you_are_where_you_were(drawer):
    c = drawer["closed"]
    assert c["screen"] == "plan" and c["settings"] is False
    assert 'class="drawer' not in c["overlays"]
    assert c["main_same"] and c["nav_same"]
    assert drawer["after_digit"] == "run"                     # the keys are the screen's again


def test_going_elsewhere_closes_the_drawer(drawer):
    assert drawer["go_closes"] == {"screen": "plan", "settings": False}
    assert drawer["nav_toggle"] == [True, False]
    assert any("parallel jobs" in label for label in drawer["palette"])


def test_runs_go_line_on_plan_and_run_opens_settings(drawer):
    plan = drawer["plan_foot"]
    m = re.search(r'<button class="btn sm dest" data-go="settings"[^>]*>(.*?)</button>', plan)
    assert m, plan
    assert m.group(1) == "&rarr; Donau · ALPS · short · 8 CPU × 4 jobs"
    assert "32 CPUs at once" in plan                          # the footprint, in the title
    assert "choose a Donau account" in plan or 'value="ug_a" selected' in plan  # the picker
    run = drawer["run_foot"]
    m = re.search(r'<button class="btn sm dest" data-go="settings"[^>]*>(.*?)</button>', run)
    assert m, run
    assert m.group(1) == ("&rarr; Donau · ALPS · short · acct ug_a · "
                          "8 CPU × 4 jobs")


def test_an_old_settings_link_opens_the_drawer_over_the_project_screen(tmp_path_factory):
    out = _run(tmp_path_factory, "?project=p&screen=settings")
    assert out["boot"]["screen"] == "plan"                    # where the project was left
    assert out["boot"]["settings"] is True
    assert 'class="drawer' in out["boot"]["overlays"]
