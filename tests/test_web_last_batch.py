"""Last batch of a browser QA pass (main fd113c3), pinned.

1. A failed job's banner carries the job's REAL status: a refused netlist has no chip (it showed
   "501", not implemented), a missing module 501, a crash 500 -- carried by the server's job.
2. The refusal banner goes away after a later SUCCESS on the same screen (a saved config, a pin,
   a load that went through); while nothing succeeds it stays.
3. A refused netlist's full What / Why / Do / Where is shown once, as the banner; the "Refused"
   row is a one-line summary with its Re-read button; the job strip says only "refused".
4. After a reload the Refused row -- and the banner -- come back from netlists/attempt.json.
5. The Model grid cell (and the block row) survive a reload, through the URL.
6. The side-panel chart's last x label is inside the SVG ("10 G" was drawn as "10 (").
7. VSET codes are DISPLAYED ascending with the nominal named ("1, 3 (nominal 3)") in the valid
   range, report.md and the .scs "valid:" comment; the stored order (nominal first) is kept.
8. The HB tile's note joins its sentences on one period ("... and re-run.. Every" before).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.parse

import pytest

from pmukit import server
from pmukit.deliverable import Envelope, _fixed_paragraph, vset_text
from pmukit.emit import scs
from pmukit.errors import PmuError
from tests.test_server import PAGE

HARNESS_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const INPUT = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const flush = () => new Promise(r => setImmediate(() => setImmediate(r)));
async function settle(n){ for (let i = 0; i < (n || 8); i++) await flush(); }

/* One page: its own DOM stubs, scripted fetch, captured history.replaceState and timers. */
function makePage(search, routes){
  const calls = [], urls = [], timers = [];
  function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
    classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
    querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
    focus(){}, click(){}, appendChild(){}, removeChild(){} }; }
  const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
  const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
    querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
    body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
  const R = routes;
  function fetch(path, init){
    const method = (init && init.method) || 'GET';
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    let r = R[method + ' ' + path];
    if (r === undefined) r = R[method + ' ' + path.split('?')[0]];
    if (r === undefined) return new Promise(()=>{});
    if (typeof r === 'function') r = r(body, path);
    return Promise.resolve({ ok: r[0] < 400, status: r[0],
                             text: () => Promise.resolve(JSON.stringify(r[1])) });
  }
  const sandbox = { document, console, Math, JSON, Date, Number, String, Object,
    navigator:{ clipboard:{ writeText:()=>Promise.resolve() } },
    Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
    decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
    window:{ innerWidth:1440, innerHeight:900, location:{ search: search, reload(){} },
             addEventListener:()=>{}, history:{ replaceState:(s, t, u)=>{ urls.push(u); } } },
    fetch, setTimeout:(fn, ms)=>{ timers.push({fn, ms}); return timers.length; },
    clearTimeout:()=>{}, setInterval:()=>0 };
  sandbox.globalThis = sandbox; sandbox.window.document = document;
  vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
  return { sandbox, nodes, S: sandbox.S, calls, urls, timers, routes: R };
}
const act = (pg, html, re) => { const m = html.match(re); if (!m) throw new Error('no action ' + re);
                                return pg.sandbox.ACTS[m[1]]; };
const strip = (html) => (html.match(/<div class="prog"[\s\S]*?<\/div><\/div>[\s\S]*?<\/div>/) || [''])[0];
const banner = (html) => (html.match(/<div class="err">[\s\S]*?<span class="k">Where<\/span>[\s\S]*?<\/div><\/div>/) || [''])[0];
const refusedRow = (html) => (html.match(/<span class="k">Refused<\/span>[\s\S]*?<\/span><\/span><\/span>/) || [''])[0];

const CONFIG = { exists:true, sha:'c0', undoable:'', config:{ corners:['tt'], temps_c:[25],
  vset_codes:[3, 1], vset_param:'VSET', care_up_to_hz:1e9, ports:{ VDD0P8_A:'model' },
  my_load:{ VDD0P8_A:{ on_a:5e-4, off_a:2e-6, switches:true } } } };
const COPY = { path:'/d/p/netlists/input.scs', sha:'abc', bytes:10 };
const SOURCE = { path:'/w/tb/input.scs', name:'input.scs', via:'path', sha:'abc' };
const PINS = { pmu_inst:'PMU_TOP', pmu_master:'pmu_demo', candidates:[], sections:{}, params:{},
  analyses:[], notes:[], summary:{ rails:1, biases:0, stubs:0, unclassified:[] },
  netlist:{ path:'x' }, pins:[{ name:'VDD0P8_A', net:'VDD0P8_A', role:'rail', fate:'model',
  src:'IL_VDD0P8_A', dc:5e-4, is_ground:false }] };
const ERR = INPUT.job.error;                 /* the real refusal, from the real server */
const ATTEMPT = INPUT.attempt;
function baseRoutes(){
  return {
    'GET /api/projects': [200, { projects:[{ name:'p', screen:'new', step:1 }], initial_project:'p', demo:false }],
    'GET /api/state/p': [200, { project:'p', screen:'new', progress:{} }],
    'PUT /api/state/p': [200, {}],
    'GET /api/cli': [200, { cli:'x' }],
    'GET /api/p/p/config': [200, CONFIG],
    'GET /api/p/p/pins': [200, PINS],
    'GET /api/p/p/netlist': [200, { source:SOURCE, copy:COPY, candidates:[], cwd:'/w', attempt:null }],
    'PUT /api/p/p/config': (b) => [200, { config:b.config, sha:'c1', undoable:'config' }],
    'PUT /api/p/p/pins/VDD0P8_A': [200, { ok:true }],
  };
}
const out = {};

(async () => {
  // ---- 1. the chip of a failed job is the job's own status
  out.chips = {};
  for (const [name, extra] of [['refused', { error_kind:'refused', error_status:400 }],
                               ['not_landed', { error_kind:'not_landed', error_status:501, not_landed:'pmukit.fit' }],
                               ['crashed', { error_kind:'crashed', error_status:500 }],
                               ['old_server', {}]]){
    const R = baseRoutes();
    const pg = makePage('', R);
    await settle();
    Object.assign(pg.S, { screen:'new', project:'p', errs:{}, failed:{}, job:null });
    R['POST /api/p/p/netlist'] = [200, { job:'j', source:{} }];
    R['GET /api/jobs/j'] = [200, Object.assign({ job:'j', status:'failed', progress:1,
                                                 title:'read bad.scs', message:ERR.what, error:ERR }, extra)];
    pg.sandbox.loadNetlist({ path:'/w/tb/bad.scs' });
    await settle();
    pg.sandbox.render();
    const html = pg.nodes.main.innerHTML;
    out.chips[name] = { chip: pg.S.errs.new && pg.S.errs.new.status,
                        title: (banner(html).match(/<div class="t">[\s\S]*?<\/div>/) || [''])[0],
                        strip: strip(html) };
  }

  // ---- 2 + 3. one refusal: shown once; the banner outlives a render, not the next success
  {
    const R = baseRoutes();
    const pg = makePage('', R);
    await settle();
    Object.assign(pg.S, { screen:'new', project:'p', errs:{}, failed:{}, job:null, data:{} });
    R['POST /api/p/p/netlist'] = [200, { job:'j', source:{} }];
    R['GET /api/jobs/j'] = [200, INPUT.job];
    /* after the refusal, the server's Netlist row carries the staged attempt */
    R['GET /api/p/p/netlist'] = [200, { source:SOURCE, copy:COPY, candidates:[], cwd:'/w', attempt:ATTEMPT }];
    pg.sandbox.render(); await settle();
    pg.sandbox.loadNetlist({ path: ATTEMPT.path });
    await settle(); pg.sandbox.render(); await settle(); pg.sandbox.render();
    const html = pg.nodes.main.innerHTML;
    const s = { html, banner: banner(html), row: refusedRow(html), strip: strip(html) };
    for (let i = 0; i < 5; i++){ pg.sandbox.render(); await flush(); }
    s.kept_while_nothing_changed = !!pg.S.errs.new && banner(pg.nodes.main.innerHTML) !== '';
    /* a successful save of the codes on the same screen */
    const set = act(pg, pg.nodes.main.innerHTML, /id="vsetinp"[^>]*data-enter="(a\d+)"/);
    set('1, 3'); await settle(); pg.sandbox.render(); await settle(); pg.sandbox.render();
    s.after_save = { err: !!pg.S.errs.new, banner: banner(pg.nodes.main.innerHTML),
                     row: refusedRow(pg.nodes.main.innerHTML) !== '', job: pg.S.job };
    out.once = s;
  }
  // a failed save leaves the banner; a successful pin edit clears it
  {
    const R = baseRoutes();
    R['PUT /api/p/p/config'] = [400, { error:{ what:'bad config.', why:'w', do:['x'], where:'y' } }];
    const pg = makePage('', R);
    await settle();
    Object.assign(pg.S, { screen:'new', project:'p', errs:{}, failed:{}, job:null, data:{} });
    pg.S.errs.new = { status:0, kind:'refused', error:ERR, retry(){} };
    pg.sandbox.putConfig(JSON.parse(JSON.stringify(CONFIG.config)), 'x');
    await settle();
    const afterFail = pg.S.errs.new && pg.S.errs.new.error.what;
    pg.S.errs.new = { status:0, kind:'refused', error:ERR, retry(){} };
    pg.sandbox.setPin('VDD0P8_A', { fate:'stub' });
    await settle();
    out.pin = { after_failed_save: afterFail, after_pin: pg.S.errs.new ? pg.S.errs.new.error.what : null };
  }

  // ---- 4. a reload: the refused row and the banner come back from attempt.json
  {
    const R = baseRoutes();
    R['GET /api/p/p/netlist'] = [200, { source:SOURCE, copy:COPY, candidates:[], cwd:'/w', attempt:ATTEMPT }];
    const pg = makePage('?project=p&screen=new', R);
    await settle(12); pg.sandbox.render();
    const html = pg.nodes.main.innerHTML;
    const r = { banner: banner(html), row: refusedRow(html) };
    /* dismissed (Escape does the same): it does not come back on the next read of the row */
    delete pg.S.errs.new;
    pg.sandbox.forget('netlistsrc'); pg.sandbox.render(); await settle(); pg.sandbox.render();
    r.after_dismiss = { banner: banner(pg.nodes.main.innerHTML), row: refusedRow(pg.nodes.main.innerHTML) };
    /* nothing good was ever loaded: the row still comes back */
    const R2 = baseRoutes();
    R2['GET /api/p/p/netlist'] = [200, { source:null, copy:null, candidates:[], cwd:'/w', attempt:ATTEMPT }];
    R2['GET /api/p/p/config'] = [200, { exists:false, config:null }];
    const pg2 = makePage('?project=p&screen=new', R2);
    await settle(12); pg2.sandbox.render();
    r.first = { banner: banner(pg2.nodes.main.innerHTML), row: refusedRow(pg2.nodes.main.innerHTML) };
    out.reload = r;
  }

  // ---- 5. the Model selection through the URL
  {
    const P = 'VDD0P8_B', CK = 'tt/25C/vset3/1.0e-03A';
    const R = baseRoutes();
    const pg = makePage('', R);
    await settle();
    Object.assign(pg.S, { screen:'model', project:'p', errs:{}, failed:{}, job:null, sel:{} });
    pg.sandbox.pickCell(P, 'tt', 25);
    const afterCell = pg.urls[pg.urls.length - 1];
    pg.S.data['cell:' + P + 'tt25'] = { port:P, corner:'tt', temp_c:'25', grade:'green', blocks:[
      { name:'zout', cell_key:CK, grade:'green', row_grade:'green', metric:'m', value:'1', limit:'l' },
      { name:'psrr', cell_key:CK, grade:'green', row_grade:'green', metric:'m', value:'1', limit:'l' }] };
    pg.S.data['model:summary'] = { fitted:true, valid:{}, usable_not_signoff:[], not_run:[], hb:null };
    pg.S.data['model:grades'] = { rows:[] };
    pg.sandbox.render();
    act(pg, pg.nodes.main.innerHTML, /<tr[^>]*class="click rc[^"]*" data-act="(a\d+)"><td class="mono">psrr/)();
    const afterBlock = pg.urls[pg.urls.length - 1];
    const pg2 = makePage(afterBlock, baseRoutes());
    await settle();
    const s2 = pg2.S.sel;
    out.url = { after_cell: afterCell, after_block: afterBlock, screen: pg2.S.screen,
                sel: { port:s2.port, corner:s2.corner, temp:s2.temp, cell:s2.cell, block:s2.block,
                       blockCell:s2.blockCell } };
    /* the restored cell is the one marked selected in the grid */
    pg2.S.data['model:summary'] = { fitted:true, valid:{}, usable_not_signoff:[], not_run:[], hb:null };
    pg2.S.data['model:grades'] = { cells:[{ corner:'tt', temp_c:25, label:'tt 25' }],
      rows:[{ port:P, cells:[{ corner:'tt', temp_c:25, grade:'green' }] }] };
    pg2.sandbox.render();
    out.url.grid_selected = /class="cell rc c-[a-z]+ sel"/.test(pg2.nodes.main.innerHTML);
  }

  // ---- 6. the last x tick label stays inside the chart
  {
    const pg = makePage('', {});
    const xs = []; for (let i = 0; i <= 90; i++) xs.push(Math.pow(10, 1 + i / 10));
    const c = { x:xs, x_log:true, y_log:true, unit:'Ohm', complex:false,
                gt:{ mag:xs.map(f => 1 + f / 1e8) }, model:{ mag:xs.map(f => 1 + f / 1e8) } };
    const svg = pg.sandbox.chartSvg(c);
    const vb = svg.match(/viewBox="0 0 (\d+) (\d+)"/);
    const labels = [];
    for (const m of svg.matchAll(/<text x="([\d.]+)" y="([\d.]+)" text-anchor="(\w+)"[^>]*>([^<]*)<\/text>/g))
      labels.push({ x: +m[1], y: +m[2], anchor: m[3], text: m[4] });
    out.chart = { width: +vb[1], labels, ch: pg.sandbox.TICK_CH };
  }

  // ---- 8. the HB tile's note: one period, "stays off" once
  {
    const pg = makePage('', {});
    Object.assign(pg.S, { screen:'model', project:'p', errs:{}, failed:{}, job:null, sel:{} });
    pg.S.data['model:summary'] = { fitted:true, valid:{ VSET: INPUT.valid_vset }, usable_not_signoff:[],
      not_run:[], hb: INPUT.hb };
    pg.S.data['model:grades'] = { rows:[] };
    pg.sandbox.render();
    const html = pg.nodes.main.innerHTML;
    out.hb = { tile: (html.match(/HB health check<\/div>[\s\S]*?<\/button>/) || [''])[0],
               plain: pg.sandbox.hbOffNote('the check did not run.'),
               empty: pg.sandbox.hbOffNote(''),
               valid: (html.match(/<span class="k">VSET<\/span><span class="mono">([^<]*)</) || [])[1] };
  }

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _wait(started: dict) -> server.Job:
    job = server.JOBS.get(started["job"])
    deadline = time.time() + 60
    while job.status in ("queued", "running"):
        assert time.time() < deadline, job.message
        time.sleep(0.02)
    return job


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    """A real refusal (a decap on a rail), a real staged attempt and a real HB tile, from the
    server -- the page is driven with what the server actually sends."""
    from tests.test_new_screen import FIXTURE_DIR, edit
    d = tmp_path_factory.mktemp("lastbatch")
    bench = d / "bench"
    shutil.copytree(FIXTURE_DIR, bench)
    api = server.Api(root=d / "data")
    api.new_project({"name": "p"})
    assert _wait(api.load_netlist("p", {"path": str(bench / "input.scs")})).status == "done"
    edit(bench / "input.scs", "simOpts options", "Cdecap (VDD0P8_A 0) capacitor c=1u\nsimOpts options")
    job = _wait(api.load_netlist("p", {"reread": True}))
    assert job.status == "failed"
    info = api.netlist_info("p")
    assert info["attempt"] and info["copy"]
    derived = type("D", (), {"vset": {"codes": [3, 1]}, "loads": None, "temps_c": None,
                             "dc_temp_sweep": None, "freq": None, "process": None})()
    return {"job": job.to_dict(), "attempt": server._clean(info["attempt"]),
            "hb": _hb_not_run_tile(), "valid_vset": server._valid_from_derived(derived)["VSET"]}


def _hb_not_run_tile() -> dict:
    """The HB tile of a check that ran on no solver, with verify's own note text."""
    from pmukit.verify import hb
    ok, why = hb.usable(type("Fake", (), {"name": "fake"})())
    assert not ok and why.endswith("."), why          # the premise of the ".." it produced
    note = (f"no simulator: {why}. Every large-signal term therefore stays OFF -- the `ls` tier "
            f"may only default on after a real HB run, and an unrun check is not a pass.")
    return server._hb_summary({"status": "not_run", "engine": "fake", "notes": [note]})


@pytest.fixture(scope="module")
def page(real, tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page harness needs it")
    d = tmp_path_factory.mktemp("lastbatch_page")
    text = PAGE.read_text(encoding="utf-8")
    (d / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)),
                               encoding="utf-8", newline="\n")
    (d / "input.json").write_text(json.dumps(real), encoding="utf-8", newline="\n")
    (d / "harness.js").write_text(HARNESS_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(d / "harness.js"), str(d / "page.js"), str(d / "input.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- 1. the chip
@pytest.mark.parametrize("fn, kind, status", [
    (lambda job: (_ for _ in ()).throw(PmuError(what="refused.", why="w", do=["x"], where="y")),
     "refused", 400),
    (lambda job: (_ for _ in ()).throw(server.NotLanded("pmukit.nothing", "Something")),
     "not_landed", 501),
    (lambda job: 1 / 0, "crashed", 500),
])
def test_a_failed_job_says_what_kind_of_failure_it_is(fn, kind, status):
    job = _wait({"job": server.JOBS.submit("t", "p", "t", fn).id})
    d = job.to_dict()
    assert d["status"] == "failed"
    assert d["error_kind"] == kind and d["error_status"] == status
    assert d["error"]["what"]


def test_the_real_refusal_is_a_refusal(real):
    assert real["job"]["error_kind"] == "refused" and real["job"]["error_status"] == 400


def test_the_banner_chip_is_the_jobs_own_status(page):
    c = page["chips"]
    assert not c["refused"]["chip"] and "badge b-bad" not in c["refused"]["title"]
    assert c["not_landed"]["chip"] == 501 and ">501<" in c["not_landed"]["title"]
    assert c["crashed"]["chip"] == 500 and ">500<" in c["crashed"]["title"]
    assert not c["old_server"]["chip"]               # never a made-up 501
    for k in c:
        assert "501" not in c[k]["title"] or k == "not_landed", k


# --------------------------------------------------------------------------- 2. stale banner
def test_the_refusal_banner_stays_while_nothing_changed(page):
    assert page["once"]["kept_while_nothing_changed"]


def test_the_refusal_banner_clears_after_the_next_success(page):
    a = page["once"]["after_save"]
    assert not a["err"] and a["banner"] == ""
    assert a["job"] is None                          # the failed strip that pointed at it too
    assert a["row"]                                  # the staged attempt is still named


def test_a_failed_save_keeps_the_banner_and_a_pin_edit_clears_it(page, real):
    p = page["pin"]
    assert p["after_failed_save"] == "bad config."   # the newer error replaces it; not cleared
    assert p["after_pin"] is None


# --------------------------------------------------------------------------- 3. shown once
def test_a_refused_netlist_shows_its_full_error_once(page, real):
    o = page["once"]
    err = real["job"]["error"]
    html = o["html"]
    assert o["banner"] and o["row"] and o["strip"]
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
                     .replace('"', "&quot;").replace("'", "&#39;"))
    # the Where (the user's file and line) and the Why appear once on the whole screen: the banner
    assert html.count(esc(err["where"])) == 1 and esc(err["where"]) in o["banner"]
    assert html.count(esc(err["why"])) == 1 and esc(err["why"]) in o["banner"]
    # the row: a one-line summary and its Re-read button, never the full text
    assert esc(err["what"]) not in o["row"] and esc(err["where"]) not in o["row"]
    assert "decap on a rail" in o["row"] and "Re-read input.scs" in o["row"]
    assert "not loaded" in o["row"]
    # the strip: "refused", nothing else
    hint = re.search(r'<span class="hint">([^<]*)</span>', o["strip"]).group(1)
    assert hint == "refused"
    assert "decap" not in o["strip"] and "see the error" not in o["strip"]


# --------------------------------------------------------------------------- 4. reload
def test_the_refused_row_and_banner_come_back_after_a_reload(page, real):
    r = page["reload"]
    assert r["row"] and "decap on a rail" in r["row"]
    assert r["banner"] and real["attempt"]["error"]["why"][:40] in r["banner"]
    assert "badge b-bad\">4" not in r["banner"] and "badge b-bad\">5" not in r["banner"]
    # dismissed, it does not come back with the next read of the row; the row stays
    assert r["after_dismiss"]["banner"] == "" and r["after_dismiss"]["row"]
    # nothing good was ever loaded: row and banner both there
    assert r["first"]["row"] and r["first"]["banner"]


# --------------------------------------------------------------------------- 5. the cell URL
def test_the_model_cell_and_block_round_trip_through_the_url(page):
    u = page["url"]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(u["after_cell"]).query)
    assert q == {"project": ["p"], "screen": ["model"], "cell": ["VDD0P8_B|tt/25C"]}
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(u["after_block"]).query)
    assert q["block"] == ["psrr|tt/25C/vset3/1.0e-03A"]
    assert u["screen"] == "model"
    assert u["sel"] == {"port": "VDD0P8_B", "corner": "tt", "temp": "25", "cell": "tt/25C",
                        "block": "psrr", "blockCell": "tt/25C/vset3/1.0e-03A"}
    assert u["grid_selected"]


# --------------------------------------------------------------------------- 6. last tick
def test_every_x_tick_label_is_inside_the_chart(page):
    ch = page["chart"]
    w, cw = ch["width"], ch["ch"]
    axis = [lab for lab in ch["labels"] if lab["anchor"] in ("middle", "end", "start")
            and lab["text"] and re.match(r"^\d", lab["text"]) and lab["y"] > 150]
    assert any(lab["text"] == "10 G" for lab in axis), axis
    for lab in axis:
        width = len(lab["text"]) * cw
        left = {"middle": lab["x"] - width / 2, "end": lab["x"] - width, "start": lab["x"]}[lab["anchor"]]
        assert left >= 0 and left + width <= w, lab
    last = max(axis, key=lambda lab: lab["x"])
    assert last["text"] == "10 G" and last["anchor"] == "end"


# --------------------------------------------------------------------------- 7. VSET text
def test_vset_codes_display_sorted_with_the_nominal_named():
    assert vset_text([3, 1]) == "1, 3 (nominal 3)"
    assert vset_text([3]) == "3"
    assert vset_text([]) == "(none)"
    assert vset_text([2, 0, 3, 2]) == "0, 2, 3 (nominal 2)"
    env = {"vset_codes": [3, 1]}
    assert server._envelope_text(env)["VSET"] == "1, 3 (nominal 3)"


def test_vset_display_in_report_and_scs_keeps_the_stored_order():
    env = Envelope(freq_max_hz=1e9, load_a={"VDD0P8_A": (2e-6, 1e-3)}, temp_c=(-40, 125),
                   corners=["tt"], vset_codes=[3, 1], ls_default_on=[])
    assert env.vset_codes == [3, 1]                       # nominal first, as stored
    head = "\n".join(_fixed_paragraph("p", env, [], []))
    assert "VSET codes 1, 3 (nominal 3)." in head and "3, 1" not in head
    lines = scs.extra_lines({"tt": "pmu_p"}, envelope=env)["*"]
    valid = [ln for ln in lines if ln.startswith("// valid:")]
    assert valid and "VSET 1, 3 (nominal 3) " in valid[0] and "3, 1" not in valid[0]
    assert env.to_json()["vset_codes"] == [3, 1]


def test_the_model_valid_range_shows_sorted_vset(page, real):
    assert real["valid_vset"] == "1, 3 (nominal 3)"
    assert page["hb"]["valid"] == "1, 3 (nominal 3)"


# --------------------------------------------------------------------------- 8. HB note
def test_the_hb_note_joins_on_one_period(real, page):
    note = real["hb"]["note"]
    assert ".." not in note and "re-run. Every" in note
    tile = page["hb"]["tile"]
    assert ".." not in tile
    assert len(re.findall(r"stays off", tile, re.I)) == 1
    assert page["hb"]["plain"] == "the check did not run. Every large-signal term stays off."
    assert page["hb"]["empty"] == "Every large-signal term stays off."
    assert server._one_period("wait... then.. next") == "wait... then. next"
