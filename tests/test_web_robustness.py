"""The page under a browser QA pass: what broke, pinned so it stays fixed.

* a GET that fails is asked ONCE, not on every render (it was ~700 requests a second, and the
  page rebuilt itself under the cursor);
* a new project with no netlist opens on the netlist box, not on a red error;
* what was typed and not saved survives a re-render, every Set box submits on Enter;
* a bad number is named next to its box, never read as 0, never dropped from a list;
* "Measure on / off from the netlist" touches its own row's rail and says what it set;
* no alert(), confirm() or prompt(); Export plan downloads a file; Fit waits while a job runs.

The page's own script runs in node against a scripted fetch -- nothing is re-implemented.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from pmukit import server
from pmukit.errors import PmuError
from tests.test_new_screen import api, bench, load_ok  # noqa: F401  (pytest fixtures)
from tests.test_server import PAGE

SCENARIOS_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const clicks = [], blobs = [], calls = [];
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
  if (r === undefined) return new Promise(()=>{});           // never answers
  if (typeof r === 'function') r = r(body);
  return Promise.resolve({ ok: r[0] < 400, status: r[0],
                           text: () => Promise.resolve(JSON.stringify(r[1])) });
}
class Blob { constructor(parts, opts){ this.text = parts.join(''); this.type = (opts||{}).type; } }
const URL = { createObjectURL:(b)=>{ blobs.push(b); return 'blob:1'; }, revokeObjectURL(){} };
const sandbox = { document, navigator:{}, console, Math, JSON, Date, Number, String, Object,
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams, Blob, URL,
  window:{ innerWidth:1440, innerHeight:900, location:{search:'',reload(){}},
           addEventListener:()=>{} },
  fetch, setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
sandbox.globalThis = sandbox; sandbox.window.document = document;
vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
const S = sandbox.S;
const flush = () => new Promise(r => setImmediate(() => setImmediate(r)));
const count = (m, p) => calls.filter(c => c.method === m && c.path === p).length;
const puts = () => calls.filter(c => c.method === 'PUT' && c.path === '/api/p/p/config');
const act = (html, re) => { const m = html.match(re); if (!m) throw new Error('no action ' + re); return sandbox.ACTS[m[1]]; };
function fresh(screen, data){
  S.screen = screen; S.project = 'p'; S.data = Object.assign({}, data || {});
  S.loading = {}; S.failed = {}; S.errs = {}; S.sel = {}; S.job = null; S.note = null;
  calls.length = 0; ROUTES = {};
}
const COPY = { path:'/d/p/netlist/input.scs', sha:'abc', bytes:10 };
const CONFIG = { exists:true, sha:'c0', undoable:'', config:{ corners:['tt'], temps_c:[25,-40],
  vset_codes:[3], vset_param:'VSET', care_up_to_hz:2e10, ports:{},
  my_load:{ VDD0P8_A:{on_a:5e-4, off_a:2e-6, switches:true},
            VDD0P8_B:{on_a:2e-3, off_a:8e-6, switches:false} } } };
const PINS = { pmu_inst:'PMU_TOP', pmu_master:'pmu_demo', pins:[], candidates:[],
  sections:{ tt:'tt', ss:'ss' }, params:{ VSET:'3' }, analyses:[], notes:[],
  summary:{ rails:2, biases:0, stubs:0, unclassified:[] }, netlist:{ path:'x' } };
const echoConfig = (b) => [200, { config: b.config, sha:'c1', undoable:'config' }];
const out = {};

(async () => {
  // ---- 1. a failing GET is asked once, not per render
  fresh('new');
  ROUTES['GET /api/p/p/netlist'] = [200, { source:null, copy:COPY, candidates:[], cwd:'/w' }];
  ROUTES['GET /api/p/p/config'] = [200, { exists:false, config:null }];
  ROUTES['GET /api/p/p/pins'] = [400, { error:{ what:'the deck is broken.', why:'w',
                                                 do:['Reload the page'], where:'x' } }];
  sandbox.render(); await flush();
  for (let i = 0; i < 40; i++) { sandbox.render(); await flush(); }
  out.storm = { pins: count('GET', '/api/p/p/pins'), netlist: count('GET', '/api/p/p/netlist'),
                shown: nodes.main.innerHTML.includes('the deck is broken.') };
  S.errs.new.retry(); await flush();
  for (let i = 0; i < 10; i++) { sandbox.render(); await flush(); }
  out.storm.after_retry = count('GET', '/api/p/p/pins');
  sandbox.go('new'); await flush();
  for (let i = 0; i < 10; i++) { sandbox.render(); await flush(); }
  out.storm.after_nav = count('GET', '/api/p/p/pins');
  sandbox.forget('pins'); sandbox.render(); await flush();
  out.storm.after_forget = count('GET', '/api/p/p/pins');

  // ---- 2. no netlist yet: the netlist box, no red error, no pins request
  fresh('new');
  ROUTES['GET /api/p/p/netlist'] = [200, { source:null, copy:null, candidates:[], cwd:'/w' }];
  ROUTES['GET /api/p/p/config'] = [200, { exists:false, config:null }];
  for (let i = 0; i < 5; i++) { sandbox.render(); await flush(); }
  out.empty = { pins: count('GET', '/api/p/p/pins'), html: nodes.main.innerHTML };
  fresh('plan', { site:{ engine:'fake', engines:[] } });
  ROUTES['GET /api/p/p/plan'] = [400, { error:{ what:'p has no netlist yet.', why:'w',
      do:['Drop an input.scs on the New screen'], where:'x', empty:'no_netlist' } }];
  sandbox.render(); await flush(); sandbox.render();
  out.empty.plan = nodes.main.innerHTML;

  // ---- 3/4. typed values survive, Enter submits, bad tokens are named
  fresh('new', { pins:PINS, netlistsrc:{ source:null, copy:COPY }, config:JSON.parse(JSON.stringify(CONFIG)) });
  ROUTES['PUT /api/p/p/config'] = echoConfig;
  S.sel['in:load:VDD0P8_A:on_a'] = '600u';            // typed, not saved (what oninput keeps)
  sandbox.render();
  const t = { kept_render: nodes.main.innerHTML.includes('value="600u"') };
  const fmax = act(nodes.main.innerHTML, /id="fmaxinp"[^>]*data-enter="(a\d+)"/);
  fmax('1G'); await flush(); sandbox.render();
  t.fmax_put = puts().map(c => c.body.config.care_up_to_hz);
  t.kept_after_other_set = nodes.main.innerHTML.includes('value="600u"');
  act(nodes.main.innerHTML, /id="fmaxinp"[^>]*data-enter="(a\d+)"/)('fast');
  t.fast = nodes.main.innerHTML.match(/fielderr[^>]*>[^<]*(<svg[\s\S]*?<\/svg>)?([^<]*)/)[2];
  t.fast_kept = nodes.main.innerHTML.includes('value="fast"');
  const codes = () => act(nodes.main.innerHTML, /id="vsetinp"[^>]*data-enter="(a\d+)"/);
  codes()('abc'); t.abc = nodes.main.innerHTML.includes('&quot;abc&quot; is not an integer');
  codes()('1,x,5'); t.x = nodes.main.innerHTML.includes('&quot;x&quot; is not an integer');
  t.puts_before_codes = puts().length;
  codes()('1, 3 5'); await flush(); sandbox.render();
  t.codes_put = puts().slice(-1)[0].body.config.vset_codes;
  S.sel['in:load:VDD0P8_B:off_a'] = '0';
  sandbox.saveLoadsFromInputs(); await flush(); sandbox.render();
  const ld = puts().slice(-1)[0].body.config.my_load;
  t.loads = { a_on: ld.VDD0P8_A.on_a, b_off: ld.VDD0P8_B.off_a, b_on: ld.VDD0P8_B.on_a };
  t.typed_dropped_after_save = S.sel['in:load:VDD0P8_A:on_a'] === undefined;
  S.sel['in:load:VDD0P8_A:on_a'] = '5 parsecs';
  const n = puts().length; sandbox.saveLoadsFromInputs();
  t.bad_load_not_sent = puts().length === n && nodes.main.innerHTML.includes('5 parsecs');
  sandbox.patchConfig('temps_c', ['125', '-40', '25', '85']); await flush();
  t.temps = puts().slice(-1)[0].body.config.temps_c;
  t.pdk = (nodes.main.innerHTML.match(/PDK includes<\/span><span class="mono">([^<]*)</) || [])[1];
  t.nav = nodes.nav.innerHTML;
  const r = {};
  for (const [s, u] of [['600u','A'], ['0','A'], ['0 A','A'], ['20 GHz','Hz'], ['2e10','Hz'],
                        ['1meg','Hz'], ['1M','Hz'], ['5mA','A'], ['400m','V'], ['fast','Hz'],
                        ['5 V','A'], ['', 'A'], ['1.2.3','A']])
    r[s + '|' + u] = sandbox.readEng(s, u);
  t.readEng = r;
  out.typed = t;

  // ---- 5. measure one rail: the others (saved or typed) are left alone
  fresh('new', { pins:PINS, netlistsrc:{ source:null, copy:COPY }, config:JSON.parse(JSON.stringify(CONFIG)) });
  ROUTES['PUT /api/p/p/config'] = echoConfig;
  ROUTES['POST /api/p/p/measure-load'] = (b) => [200, { biases:{}, rails:{ [b.rail]:{ on_a:5.12e-4,
      on_from:'IL_' + b.rail + ' dc=512u', off_a_suggested:2e-6, off_note:'not in the netlist' } } }];
  S.sel['in:load:VDD0P8_B:on_a'] = '3m';
  sandbox.render();
  const mv = sandbox.verbsFor('rail', { row:{ name:'VDD0P8_A' } }).filter(v => v.id === 'measure')[0];
  mv.run({ row:{ name:'VDD0P8_A' } }); await flush(); sandbox.render();
  const mb = calls.filter(c => c.path === '/api/p/p/measure-load')[0].body;
  const put = puts().slice(-1)[0].body.config.my_load;
  out.measure = { body: mb, a_on: put.VDD0P8_A.on_a, a_off: put.VDD0P8_A.off_a,
                  b_on: put.VDD0P8_B.on_a, html: nodes.main.innerHTML };

  // ---- 6. explanations are in the page
  fresh('plan');
  sandbox.VERBS.planrun.filter(v => v.id === 'why')[0].run({ row:{ run_id:'abcdef1234567',
      why:'the AC sweep behind zout', process:'ss', temp_c:125, vset:3 } });
  out.note = nodes.overlays.innerHTML;

  // ---- 7. Export plan downloads <project>_plan.csv
  fresh('plan', { plan:{ cells:1, cached:0, cost:{ runs:3, cpu_hours:1.5 }, groups:[{ id:'g,1',
      title:'t', enabled:true, analysis:'ac', runs:3, cached:0, cpu_hours:1.5, ports:['A'],
      observables:['zout'], why:'w' }] }, consequences:{ consequences:[] },
      site:{ engine:'fake', engines:[] } });
  sandbox.render();
  act(nodes.foot.innerHTML, /data-act="(a\d+)">Export plan/)();
  const a = clicks.filter(c => c.download)[0] || {};
  out.csv = { name: a.download, text: (blobs[0] || {}).text, type: (blobs[0] || {}).type };

  // ---- 8. Fit waits for a running job
  fresh('run', { 'ledger:all':{ total:1, counts:{ done:1 }, rows:[] } });
  S.job = { status:'running', title:'submitting the plan', progress:0.2 };
  sandbox.render();
  const busy = nodes.foot.innerHTML.match(/<button class="btn pri"[^>]*>Fit model/)[0];
  S.job = null; sandbox.render();
  const idle = nodes.foot.innerHTML.match(/<button class="btn pri"[^>]*>Fit model/)[0];
  out.fit = { busy, idle };

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page gate needs it")
    d = tmp_path_factory.mktemp("page")
    text = PAGE.read_text(encoding="utf-8")
    (d / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)),
                               encoding="utf-8", newline="\n")
    (d / "check.js").write_text(SCENARIOS_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(d / "check.js"), str(d / "page.js")],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- 1 request storm
def test_a_failing_get_is_requested_once_until_the_user_acts(page):
    s = page["storm"]
    assert s["netlist"] == 1
    assert s["pins"] == 1, "a refused read was asked again on a re-render"
    assert s["shown"], "the failure is on screen"
    assert s["after_retry"] == 2, "Try again asks once more"
    assert s["after_nav"] == 3, "going to the screen asks once more"
    assert s["after_forget"] == 4, "forget() (a load, a save) asks once more"


# --------------------------------------------------------------------------- 5 empty state
def test_a_project_without_a_netlist_opens_on_the_netlist_box_not_an_error(page):
    e = page["empty"]
    assert e["pins"] == 0, "no netlist: the pins are not even asked for"
    assert 'id="nlpath"' in e["html"] and "has no netlist yet" in e["html"]
    assert 'class="err"' not in e["html"]
    # another screen answered "no netlist yet": a callout with the way forward, not a red block
    assert 'class="err"' not in e["plan"] and "Drop an input.scs on the New screen" in e["plan"]
    assert "400" not in e["plan"]


def test_no_netlist_is_marked_empty_by_the_server(tmp_path):
    a = server.Api(root=tmp_path / "data")
    a.new_project({"name": "p"})
    with pytest.raises(PmuError) as ei:
        a.pins("p")
    body = ei.value.to_dict()["error"]
    assert body["empty"] == "no_netlist"
    assert not any("/api/" in d for d in body["do"])


# --------------------------------------------------------------------------- 2 + 4 typing
def test_typed_values_survive_another_boxs_set(page):
    t = page["typed"]
    assert t["kept_render"]
    assert t["fmax_put"] == [1e9], "Enter in the frequency box saves it"
    assert t["kept_after_other_set"], "saving the frequency discarded the load being typed"


def test_bad_input_is_named_next_to_the_field_and_nothing_is_sent(page):
    t = page["typed"]
    assert '"fast" is not a frequency' in t["fast"].replace("&quot;", '"')
    assert t["fast_kept"], "the bad text stays in the box to be fixed"
    assert t["abc"] and t["x"], "a bad code token is named, never dropped"
    assert t["puts_before_codes"] == 1
    assert t["codes_put"] == [1, 3, 5]
    assert t["bad_load_not_sent"]


def test_off_current_zero_is_saved_and_only_typed_boxes_are_read(page):
    ld = page["typed"]["loads"]
    assert ld["b_off"] == 0
    assert ld["a_on"] == pytest.approx(6e-4)
    assert ld["b_on"] == 2e-3, "an untouched box is not re-read from its rounded display"
    assert page["typed"]["typed_dropped_after_save"]


def test_engineering_suffixes_are_read_consistently(page):
    r = page["typed"]["readEng"]
    ok = {"600u|A": 6e-4, "0|A": 0, "0 A|A": 0, "20 GHz|Hz": 2e10, "2e10|Hz": 2e10,
          "1meg|Hz": 1e6, "1M|Hz": 1e6, "5mA|A": 5e-3, "400m|V": 0.4}
    for k, v in ok.items():
        assert "bad" not in r[k], (k, r[k])
        assert r[k]["v"] == pytest.approx(v), k
    for k in ("fast|Hz", "5 V|A", "|A", "1.2.3|A"):
        assert r[k].get("bad"), k


def test_temperatures_are_sorted_when_saved(page):
    assert page["typed"]["temps"] == [-40, 25, 85, 125]


# --------------------------------------------------------------------------- 3 measure
def test_measure_acts_on_its_own_rail_and_says_what_it_set(page):
    m = page["measure"]
    assert m["body"] == {"rail": "VDD0P8_A"}
    assert m["a_on"] == pytest.approx(5.12e-4) and m["a_off"] == pytest.approx(2e-6)
    assert m["b_on"] == 2e-3, "another rail's saved value was overwritten"
    assert 'value="3m"' in m["html"], "another rail's typing was discarded"
    assert "VDD0P8_A on 500 uA → measured 512 uA" in m["html"]


def test_measure_load_narrows_to_one_rail(api, bench):  # noqa: F811
    load_ok(api, {"path": str(bench / "input.scs")})
    one = api.measure_load("p", {"rail": "VDD0P8_A"})
    assert list(one["rails"]) == ["VDD0P8_A"] and one["biases"] == {}
    assert set(api.measure_load("p")["rails"]) > {"VDD0P8_A"}      # the old answer, unchanged
    with pytest.raises(PmuError) as ei:
        api.measure_load("p", {"rail": "NOPE"})
    assert "NOPE" in ei.value.what and "VDD0P8_A" in ei.value.why


# --------------------------------------------------------------------------- 6-9
def test_the_page_never_opens_a_browser_dialog(page):
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", text)
    assert "Why run abcdef1234 exists" in page["note"]
    assert "the AC sweep behind zout" in page["note"]


def test_export_plan_downloads_a_csv_file(page):
    c = page["csv"]
    assert c["name"] == "p_plan.csv" and c["type"] == "text/csv"
    assert c["text"].splitlines()[0].startswith("group,enabled,analysis,runs")
    assert '"g,1",on,ac,3' in c["text"]


def test_fit_is_disabled_while_a_job_runs(page):
    assert "disabled" in page["fit"]["busy"]
    assert "disabled" not in page["fit"]["idle"]


def test_cosmetics_middot_and_nav_names(page):
    t = page["typed"]
    assert t["pdk"] == "tt section=tt · ss section=ss"
    for name in ("Home", "New", "Plan", "Run", "Model", "Deliver"):
        assert f'aria-label="{name}"' in t["nav"]
    assert 'title="key ' not in t["nav"]


def test_the_footer_sticks_to_the_bottom_of_the_viewport():
    css = PAGE.read_text(encoding="utf-8")
    foot = re.search(r"^\.foot\{[^}]*\}", css, re.M).group(0)
    assert "position:sticky" in foot and "bottom:0" in foot
