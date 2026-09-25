"""The New screen's pin table under the hand: nothing blanks out, a tick flips at once.

On a real 99-pin bench a Model tick blanked the whole screen for 1-3 s: the page forgot the pins,
drew a skeleton and waited for a re-read. Pinned here, with the page's own script in node
against a scripted fetch whose answers are held until the test releases them:

* a tick flips on the same frame (before the PUT answers), never draws a skeleton, and the
  PUT's answer is the new table -- the pins are not asked again;
* a refused tick flips back and says why next to the table;
* "Model: all rails / all biases / none" is one request, optimistic like a tick;
* the role-less, ignored pins are folded away by default behind "Show N unclassified pins",
  and the header counts pins per role;
* data being asked again (back from Virtuoso, after Ctrl Z, after a role write) is drawn as it
  is with an "updating" mark -- on New and on Plan -- never replaced by a skeleton;
* the command echo follows an optimistic tick and a bulk change at once.
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
const flush = () => new Promise(r => setImmediate(() => setImmediate(r)));
async function settle(n){ for (let i = 0; i < (n || 8); i++) await flush(); }

function makePage(routes){
  const calls = [], held = {};
  function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
    classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
    querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
    focus(){}, click(){}, appendChild(){}, removeChild(){} }; }
  const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
  const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
    querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
    body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
  const R = routes;
  const answer = (r) => ({ ok: r[0] < 400, status: r[0], text: () => Promise.resolve(JSON.stringify(r[1])) });
  function fetch(path, init){
    const method = (init && init.method) || 'GET';
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    const key = method + ' ' + path.split('?')[0];
    let r = R[method + ' ' + path];
    if (r === undefined) r = R[key];
    if (r === undefined) return new Promise(()=>{});
    if (r === 'hold') return new Promise(res => { (held[key] = held[key] || []).push((st, b) => res(answer([st, b]))); });
    if (typeof r === 'function') r = r(body, path);
    return Promise.resolve(answer(r));
  }
  const sandbox = { document, console, Math, JSON, Date, Number, String, Object,
    navigator:{ clipboard:{ writeText:()=>Promise.resolve() } },
    Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
    decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
    window:{ innerWidth:1440, innerHeight:900, location:{ search:'', reload(){} },
             addEventListener:()=>{}, history:{ replaceState(){} } },
    fetch, setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
  sandbox.globalThis = sandbox; sandbox.window.document = document;
  vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
  return { sandbox, nodes, S: sandbox.S, calls, held, routes: R };
}
const release = (pg, key, st, body) => { const f = (pg.held[key] || []).shift(); if (!f) throw new Error('nothing held for ' + key); f(st, body); };
const act = (html, re) => { const m = html.match(re); if (!m) throw new Error('no action ' + re); return m[1]; };
const count = (pg, method, path) => pg.calls.filter(c => c.method === method && c.path.split('?')[0] === path).length;
const skel = (html) => html.includes('class="skel"');
const checked = (html, pin) => new RegExp('aria-label="Model ' + pin + '" checked').test(html);
const hasBox = (html, pin) => html.includes('aria-label="Model ' + pin + '"');
const cliState = (pg) => { const c = pg.calls.filter(c => c.path.indexOf('/api/cli') === 0).pop();
  return c ? JSON.parse(decodeURIComponent(c.path.match(/state=([^&]*)/)[1])) : null; };

const row = (name, role, fate, extra) => Object.assign({ name, net: name, role, fate, src: role === 'none' ? null : 'X_' + name,
  dc: role === 'rail' ? 5e-4 : role === 'bias' ? 0.4 : null, is_ground: false, index: 0 }, extra || {});
function pinsPayload(fates){
  const rows = [row('VDDA', 'supply', 'model'), row('VRAIL_A', 'rail', 'model'), row('VRAIL_B', 'rail', 'model'),
    row('IBIAS', 'bias', 'model'), row('CTRL0', 'none', 'ignore'), row('CTRL1', 'none', 'ignore'),
    row('CTRL2', 'none', 'ignore'), row('GND', 'none', 'ignore', { is_ground: true, net: '0' })];
  rows.forEach((r, i) => { r.index = i; if (fates && fates[r.name]) r.fate = fates[r.name]; });
  return { pmu_inst:'I_PMU', pmu_master:'pmu_synth', candidates:[], sections:{}, params:{},
           analyses:[], notes:[], summary:{ rails:2, biases:1, stubs:0, unclassified:[] },
           netlist:{ path:'/d/p/netlists/input.scs', sha:'abc', bytes:10 }, pins: rows };
}
function configPayload(ports){
  const base = { VDDA:'model', VRAIL_A:'model', VRAIL_B:'model', IBIAS:'model', CTRL0:'ignore',
                 CTRL1:'ignore', CTRL2:'ignore', GND:'ignore' };
  return { exists:true, sha:'c0', undoable:'', answers:{}, history:[], config:{ corners:['tt'], temps_c:[25],
    vset_codes:[3], vset_param:'VSET', care_up_to_hz:1e9, ports:Object.assign(base, ports || {}),
    my_load:{ VRAIL_A:{ on_a:5e-4, off_a:2e-6, switches:true }, VRAIL_B:{ on_a:5e-4, off_a:2e-6, switches:true } } } };
}
function baseRoutes(){
  return {
    'GET /api/projects': [200, { projects:[{ name:'p', screen:'new', step:1 }], initial_project:'p', demo:false }],
    'GET /api/state/p': [200, { project:'p', screen:'new', progress:{} }],
    'PUT /api/state/p': [200, {}],
    'GET /api/cli': [200, { cli:'x' }],
    'GET /api/p/p/config': [200, configPayload()],
    'GET /api/p/p/pins': [200, pinsPayload()],
    'GET /api/p/p/netlist': [200, { source:{ path:'/w/tb/input.scs', name:'input.scs', via:'path', sha:'abc' },
                                    copy:{ path:'/d/p/netlists/input.scs', sha:'abc', bytes:10 },
                                    candidates:[], cwd:'/w', attempt:null }],
  };
}
async function openNew(R){
  const pg = makePage(R || baseRoutes());
  await settle();
  pg.S.screen = 'new';
  pg.sandbox.render(); await settle(); pg.sandbox.render();
  return pg;
}
function putAnswer(fates){
  const pins = pinsPayload(fates), cfg = configPayload(fates);
  return { pins, config: Object.assign(cfg, { undoable:'config' }), undoable:'config', changed:Object.keys(fates) };
}
const out = {};

(async () => {
  // ---- 1. a tick: flips on this frame, no skeleton, the answer is the table
  {
    const R = baseRoutes();
    R['PUT /api/p/p/pins/VRAIL_A'] = 'hold';
    const pg = await openNew(R);
    const before = pg.nodes.main.innerHTML;
    const gets = count(pg, 'GET', '/api/p/p/pins');
    const cli0 = pg.calls.filter(c => c.path.indexOf('/api/cli') === 0).length;
    pg.sandbox.ACTS[act(before, /data-act="(a\d+)" aria-label="Model VRAIL_A"/)]();
    const during = pg.nodes.main.innerHTML;
    const t = {
      was_checked: checked(before, 'VRAIL_A'),
      flipped_now: !checked(during, 'VRAIL_A') && hasBox(during, 'VRAIL_A'),
      skeleton_now: skel(during),
      saving_mark: /saving&hellip;/.test(during),
      foot_now: pg.nodes.foot.innerHTML,
      put_sent: count(pg, 'PUT', '/api/p/p/pins/VRAIL_A'),
      cli_asked: pg.calls.filter(c => c.path.indexOf('/api/cli') === 0).length > cli0,
      cli_stubs: (cliState(pg) || {}).stubs,
    };
    await settle();
    t.skeleton_waiting = skel(pg.nodes.main.innerHTML);
    release(pg, 'PUT /api/p/p/pins/VRAIL_A', 200, putAnswer({ VRAIL_A:'stub' }));
    await settle();
    const after = pg.nodes.main.innerHTML;
    t.after_unchecked = !checked(after, 'VRAIL_A') && hasBox(after, 'VRAIL_A');
    t.skeleton_after = skel(after);
    t.saving_after = /saving&hellip;/.test(after);
    t.pins_refetched = count(pg, 'GET', '/api/p/p/pins') - gets;
    t.undoable = pg.S.undoable;
    t.config_port = pg.S.data.config.config.ports.VRAIL_A;
    out.tick = t;
  }

  // ---- 2. a refused tick flips back and says why, next to the table
  {
    const R = baseRoutes();
    R['PUT /api/p/p/pins/VRAIL_B'] = 'hold';
    const pg = await openNew(R);
    pg.sandbox.ACTS[act(pg.nodes.main.innerHTML, /data-act="(a\d+)" aria-label="Model VRAIL_B"/)]();
    const t = { flipped: !checked(pg.nodes.main.innerHTML, 'VRAIL_B') };
    release(pg, 'PUT /api/p/p/pins/VRAIL_B', 400, { error:{ what:'the config was refused.',
      why:'A reason.', do:['Do this'], where:'config.json' } });
    await settle();
    const html = pg.nodes.main.innerHTML;
    t.back = checked(html, 'VRAIL_B');
    t.inline = /Not saved: VRAIL_B/.test(html) && html.includes('the config was refused.');
    t.config_back = pg.S.data.config.config.ports.VRAIL_B;
    t.load_back = !!pg.S.data.config.config.my_load.VRAIL_B;
    t.skeleton = skel(html);
    out.refused = t;
  }

  // ---- 3. bulk: one request, optimistic, one answer
  {
    const R = baseRoutes();
    R['PUT /api/p/p/pins'] = 'hold';
    const pg = await openNew(R);
    const html = pg.nodes.main.innerHTML;
    const none = act(html, /data-act="(a\d+)" title="untick every rail and bias[^"]*"/);
    pg.sandbox.ACTS[none]();
    const during = pg.nodes.main.innerHTML;
    const puts = pg.calls.filter(c => c.method === 'PUT' && c.path.indexOf('/api/p/p/pins') === 0);
    const t = {
      requests: puts.length, path: puts[0] && puts[0].path, body: puts[0] && puts[0].body,
      all_unticked: ['VRAIL_A', 'VRAIL_B', 'IBIAS'].every(p => hasBox(during, p) && !checked(during, p)),
      skeleton: skel(during),
      foot: pg.nodes.foot.innerHTML,
      cli_stubs: ((cliState(pg) || {}).stubs || []).slice().sort(),
    };
    release(pg, 'PUT /api/p/p/pins', 200, putAnswer({ VRAIL_A:'stub', VRAIL_B:'stub', IBIAS:'stub' }));
    await settle();
    t.after = ['VRAIL_A', 'VRAIL_B', 'IBIAS'].every(p => !checked(pg.nodes.main.innerHTML, p));
    // all rails back: only the rails move
    R['PUT /api/p/p/pins'] = (b) => [200, putAnswer(Object.assign({ IBIAS:'stub' },
                                         Object.keys(b.fates).reduce((o, k) => (o[k] = b.fates[k], o), {})))];
    pg.sandbox.render();
    pg.sandbox.ACTS[act(pg.nodes.main.innerHTML, /data-act="(a\d+)" title="tick Model on every rail pin[^"]*"/)]();
    t.rails_now = checked(pg.nodes.main.innerHTML, 'VRAIL_A');
    await settle();                        /* sent after the previous one: requests are chained */
    const pinPuts = pg.calls.filter(c => c.method === 'PUT' && c.path.indexOf('/api/p/p/pins') === 0);
    t.rails_body = pinPuts[pinPuts.length - 1].body;
    t.bulk_requests = pinPuts.length;
    t.rails_after = checked(pg.nodes.main.innerHTML, 'VRAIL_A') && checked(pg.nodes.main.innerHTML, 'VRAIL_B')
                    && !checked(pg.nodes.main.innerHTML, 'IBIAS');
    out.bulk = t;
  }

  // ---- 4. the role-less, ignored pins are folded; the header counts roles
  {
    const pg = await openNew();
    const html = pg.nodes.main.innerHTML;
    const t = {
      hidden: !/<td class="mono">CTRL0<\/td>/.test(html) && !/<td class="mono">CTRL2<\/td>/.test(html),
      ground_shown: /<td class="mono">GND<\/td>/.test(html),
      rail_shown: /<td class="mono">VRAIL_A<\/td>/.test(html),
      button: (html.match(/aria-expanded="false">([^<]*)</) || [])[1],
      counts: (html.match(/aria-label="pins per role">([\s\S]*?)<\/span><span class="rowflex">/) || [])[1] || '',
    };
    pg.sandbox.ACTS[act(html, /data-act="(a\d+)" aria-expanded="false"/)]();
    const open = pg.nodes.main.innerHTML;
    t.shown = /<td class="mono">CTRL0<\/td>/.test(open) && /<td class="mono">CTRL2<\/td>/.test(open);
    t.button_open = (open.match(/aria-expanded="true">([^<]*)</) || [])[1];
    t.menu_on_rows = /data-menutitle="pin CTRL1"/.test(open);
    out.fold = t;
  }

  // ---- 5. back from Virtuoso / a role write: drawn as it is, marked, never a skeleton
  {
    const R = baseRoutes();
    const pg = await openNew(R);
    R['GET /api/p/p/netlist'] = 'hold'; R['GET /api/p/p/pins'] = 'hold';
    pg.sandbox.refresh('netlistsrc'); pg.sandbox.refresh('pins');
    pg.sandbox.render();
    const html = pg.nodes.main.innerHTML;
    const t = { skeleton: skel(html), table: hasBox(html, 'VRAIL_A'), mark: /class="hint upd"/.test(html) };
    release(pg, 'GET /api/p/p/netlist', 200, baseRoutes()['GET /api/p/p/netlist'][1]);
    release(pg, 'GET /api/p/p/pins', 200, pinsPayload({ VRAIL_A:'stub' }));
    await settle();
    t.after = !checked(pg.nodes.main.innerHTML, 'VRAIL_A') && !/class="hint upd"/.test(pg.nodes.main.innerHTML);
    // a role written into the deck: the old table stays up, marked, until the answer
    R['PUT /api/p/p/pins/CTRL0'] = 'hold';
    pg.sandbox.setPin('CTRL0', { role:'bias', dc:0.4 });
    const during = pg.nodes.main.innerHTML;
    t.role_skeleton = skel(during);
    t.role_mark = /writing the role and re-reading the pins/.test(during);
    release(pg, 'PUT /api/p/p/pins/CTRL0', 200, putAnswer({}));
    await settle();
    t.role_after_mark = /writing the role/.test(pg.nodes.main.innerHTML);
    // an older server's answer ({ok:true}) is followed by one re-ask, still no skeleton
    R['GET /api/p/p/pins'] = [200, pinsPayload()];
    R['PUT /api/p/p/pins/VRAIL_B'] = [200, { ok:true }];
    const g0 = count(pg, 'GET', '/api/p/p/pins');
    pg.sandbox.setPin('VRAIL_B', { fate:'stub' });
    await settle();
    t.old_server_regets = count(pg, 'GET', '/api/p/p/pins') - g0;
    t.old_server_skeleton = skel(pg.nodes.main.innerHTML);
    out.refresh = t;
  }

  // ---- 6. Ctrl Z on Plan: the groups stay on screen while the plan is recomputed
  {
    const R = baseRoutes();
    const plan = { cells:4, cached:0, cost:{ runs:12, cpu_hours:1 }, ticks:{}, groups:[
      { id:'g1', title:'AC', enabled:true, ports:['VRAIL_A'], runs:12, observables:['zout'], cpu_hours:1 }] };
    R['GET /api/p/p/plan'] = [200, plan];
    R['GET /api/p/p/plan/consequences'] = [200, { consequences:[] }];
    R['GET /api/site'] = [200, { engine:'fake' }];
    R['POST /api/p/p/config/undo'] = [200, { kind:'plan_ticks', plan_ticks:{}, undoable:'' }];
    const pg = makePage(R);
    await settle();
    pg.S.screen = 'plan'; pg.sandbox.render(); await settle(); pg.sandbox.render();
    const t = { before: /<td class="mono"[^>]*>g1<\/td>/.test(pg.nodes.main.innerHTML) };
    R['GET /api/p/p/plan'] = 'hold';
    pg.sandbox.doUndo();
    await settle();
    const html = pg.nodes.main.innerHTML;
    t.skeleton = skel(html);
    t.kept = /<td class="mono"[^>]*>g1<\/td>/.test(html);
    t.mark = /recomputing&hellip;/.test(html);
    t.submit_blocked = /the plan is being recomputed/.test(pg.nodes.foot.innerHTML);
    release(pg, 'GET /api/p/p/plan', 200, plan);
    await settle();
    t.after_mark = /recomputing&hellip;/.test(pg.nodes.main.innerHTML);
    t.after_blocked = /the plan is being recomputed/.test(pg.nodes.foot.innerHTML);
    out.plan = t;
  }

  // ---- 7. an answer to a request a refresh superseded is dropped, never drawn over a newer one
  {
    const R = baseRoutes();
    const pg = await openNew(R);
    R['GET /api/p/p/pins'] = 'hold';
    pg.sandbox.refresh('pins'); pg.sandbox.render();          // request 1 (old)
    pg.sandbox.refresh('pins'); pg.sandbox.render();          // request 2 (newer)
    release(pg, 'GET /api/p/p/pins', 200, pinsPayload({ VRAIL_A:'ignore' }));     // 1 lands late
    await settle();
    const t = { dropped: pg.S.data.pins.pins[1].fate === 'model' };
    release(pg, 'GET /api/p/p/pins', 200, pinsPayload({ VRAIL_A:'stub' }));
    await settle();
    t.newest = pg.S.data.pins.pins[1].fate === 'stub';
    out.supersede = t;
  }

  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page gate needs it")
    d = tmp_path_factory.mktemp("newscreen")
    text = PAGE.read_text(encoding="utf-8")
    (d / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)),
                               encoding="utf-8", newline="\n")
    (d / "harness.js").write_text(HARNESS_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(d / "harness.js"), str(d / "page.js")],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_a_tick_flips_before_the_answer_and_never_blanks_the_screen(page):
    t = page["tick"]
    assert t["was_checked"] and t["flipped_now"], "the tick did not flip on the click's frame"
    assert not t["skeleton_now"] and not t["skeleton_waiting"] and not t["skeleton_after"]
    assert t["saving_mark"] and not t["saving_after"]
    assert t["put_sent"] == 1
    assert "1 rails" in t["foot_now"], "the footer counts follow the tick at once"
    assert t["after_unchecked"]
    assert t["pins_refetched"] == 0, "the PUT answer is the table: nothing is asked again"
    assert t["undoable"] == "config" and t["config_port"] == "stub"


def test_the_command_echo_follows_a_tick_at_once(page):
    assert page["tick"]["cli_asked"]
    assert page["tick"]["cli_stubs"] == ["VRAIL_A"]


def test_a_refused_tick_flips_back_and_says_why_inline(page):
    t = page["refused"]
    assert t["flipped"] and t["back"]
    assert t["inline"]
    assert t["config_back"] == "model" and t["load_back"]
    assert not t["skeleton"]


def test_model_none_is_one_optimistic_request(page):
    t = page["bulk"]
    assert t["requests"] == 1 and t["path"] == "/api/p/p/pins"
    assert t["body"]["fates"] == {"VRAIL_A": "stub", "VRAIL_B": "stub", "IBIAS": "stub"}
    assert t["body"]["note"]
    assert t["all_unticked"] and not t["skeleton"] and t["after"]
    assert "0 rails, 0 biases" in t["foot"]
    assert t["cli_stubs"] == ["IBIAS", "VRAIL_A", "VRAIL_B"]
    assert t["rails_now"], "all rails ticks on the click's frame too"
    assert t["rails_body"]["fates"] == {"VRAIL_A": "model", "VRAIL_B": "model"}
    assert t["bulk_requests"] == 2 and t["rails_after"]


def test_unclassified_pins_are_folded_by_default_with_their_count(page):
    t = page["fold"]
    assert t["hidden"] and t["ground_shown"] and t["rail_shown"]
    assert t["button"] == "Show 3 unclassified pins"
    for c in ("2 rail", "1 bias", "1 supply", "1 ground", "3 unclassified"):
        assert c in t["counts"], c
    assert t["shown"] and t["button_open"] == "Hide the 3 unclassified pins"
    assert t["menu_on_rows"], "a shown row keeps its right-click menu (set role)"


def test_data_asked_again_is_drawn_as_it_is_with_a_mark(page):
    t = page["refresh"]
    assert not t["skeleton"] and t["table"] and t["mark"]
    assert t["after"]
    assert not t["role_skeleton"] and t["role_mark"] and not t["role_after_mark"]
    assert t["old_server_regets"] == 1 and not t["old_server_skeleton"]


def test_ctrl_z_on_plan_keeps_the_groups_and_holds_submit(page):
    t = page["plan"]
    assert t["before"] and t["kept"] and not t["skeleton"] and t["mark"]
    assert t["submit_blocked"] and not t["after_mark"] and not t["after_blocked"]


def test_a_superseded_answer_is_dropped(page):
    t = page["supersede"]
    assert t["dropped"] and t["newest"]
