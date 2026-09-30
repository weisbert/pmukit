"""The New screen's corners row: the chosen corners only, one box that finds the rest.

A real PDK's corner include declares dozens of sections (TOP_TT_RFTYP, TOP_FF_RFTYP,
TOP_FFQBEST_RFTYP, TOP_FF_RFCB, ...) and a user runs two or three; a chip per section was a wall.
Pinned here:

* only the chosen corners are chips, each with an x; the last one's x is disabled (a project needs
  a corner) and its menu has no Remove;
* one combobox: typing narrows the declared sections (case-insensitive substring, the chosen
  ones left out), Up/Down lights a row, Enter adds it; Enter on a name the READ file does not
  declare is refused next to the box, and accepted as typed when the file could not be read;
* "each corner ~ +N runs" from the plan the server compiles -- nothing when it does not compile,
  and no banner for it;
* "my usual corners": saved per machine in site.json under the corner include file's name, so
  another PDK file never gets them; a saved name the read file lacks is skipped and said so; they
  are applied only by the button.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from pmukit import server
from pmukit.errors import PmuError
from pmukit.site import SiteConfig
from tests.test_new_screen import wait
from tests.test_new_screen_interaction import HARNESS_JS
from tests.test_real_bench_scan import SPICE_TOP, bench
from tests.test_server import PAGE

TWO_LINES = ('include "models/pdk_top.scs" section=TOP_TT_X\n'
             'include "models/pdk_top.scs" section=Noise_Worst\n')


# --------------------------------------------------------------------------- the server
@pytest.fixture
def api(tmp_path):
    a = server.Api(root=tmp_path / "data")
    for name in ("p", "q"):
        a.new_project({"name": name})
    return a


def _bench(d: pathlib.Path, includes: str = TWO_LINES, model: str | None = SPICE_TOP) -> pathlib.Path:
    d.mkdir(parents=True, exist_ok=True)
    if model is not None:
        (d / "models").mkdir(exist_ok=True)
        (d / "models" / "pdk_top.scs").write_text(model, encoding="utf-8", newline="\n")
    f = d / "input.scs"
    f.write_text(bench(includes), encoding="utf-8", newline="\n")
    return f


def _load(api, project: str, path: pathlib.Path):
    job = wait(api.load_netlist(project, {"path": str(path)}))
    assert job.status == "done", job.error
    return job.result


def _site(api) -> dict:
    return json.loads((api.root / "site.json").read_text(encoding="utf-8"))


def test_usual_corners_are_kept_in_site_json_under_the_include_file(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    assert api.usual_corners("p") == {"file": "models/pdk_top.scs", "read": True, "saved": [],
                                      "corners": [], "skipped": []}
    got = api.set_usual_corners("p", {"corners": ["TOP_TT_X", "TOP_FF_X", "TOP_TT_X"]})
    assert got["saved"] == got["corners"] == ["TOP_TT_X", "TOP_FF_X"] and got["skipped"] == []
    assert _site(api)["usual_corners"] == {"models/pdk_top.scs": ["TOP_TT_X", "TOP_FF_X"]}
    # the machine's, not the project's: the config's corners are untouched (never auto-applied)
    assert api.get_config("p")["config"]["corners"] == ["TOP_TT_X"]
    # the file loads back through SiteConfig, which is closed and validates it
    assert SiteConfig.load(api.root / "site.json", env=False).usual_corners == {
        "models/pdk_top.scs": ["TOP_TT_X", "TOP_FF_X"]}


def test_another_include_file_does_not_get_the_names(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    api.set_usual_corners("p", {"corners": ["TOP_TT_X", "TOP_FF_X"]})
    other = TWO_LINES.replace("models/pdk_top.scs", "models/pdk_other.scs")
    d = tmp_path / "b"
    _bench(d, other, model=None)
    (d / "models").mkdir()
    (d / "models" / "pdk_other.scs").write_text(SPICE_TOP, encoding="utf-8", newline="\n")
    _load(api, "q", d / "input.scs")
    got = api.usual_corners("q")
    assert got["file"] == "models/pdk_other.scs" and got["saved"] == [] and got["corners"] == []
    # saving there keeps both files' sets apart
    api.set_usual_corners("q", {"corners": ["TOP_FFQBEST_X"]})
    assert _site(api)["usual_corners"] == {"models/pdk_top.scs": ["TOP_TT_X", "TOP_FF_X"],
                                           "models/pdk_other.scs": ["TOP_FFQBEST_X"]}
    assert api.usual_corners("p")["corners"] == ["TOP_TT_X", "TOP_FF_X"]


def test_a_saved_name_the_read_file_lacks_is_skipped(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    SiteConfig(usual_corners={"models/pdk_top.scs": ["TOP_TT_X", "TOP_XX_Y", "TOP_FF_X"]}).save(
        api.root / "site.json")
    got = api.usual_corners("p")
    assert got["saved"] == ["TOP_TT_X", "TOP_XX_Y", "TOP_FF_X"]
    assert got["corners"] == ["TOP_TT_X", "TOP_FF_X"] and got["skipped"] == ["TOP_XX_Y"]
    # and a name the read file does not declare is not saved in the first place
    with pytest.raises(PmuError) as e:
        api.set_usual_corners("p", {"corners": ["TOP_TT_X", "TOP_XX_Y"]})
    assert "models/pdk_top.scs declares no section TOP_XX_Y" in e.value.what


def test_an_unread_file_skips_nothing(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a", model=None))
    assert api.usual_corners("p")["read"] is False
    got = api.set_usual_corners("p", {"corners": ["TOP_TT_X", "TOP_XX_Y"]})
    assert got["corners"] == ["TOP_TT_X", "TOP_XX_Y"] and got["skipped"] == []


def test_usual_corners_refuse_what_is_not_a_list_of_section_names(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    for body in ({}, {"corners": "TOP_TT_X"}, {"corners": ["TOP TT"]}, {"corners": [3]}):
        with pytest.raises(PmuError) as e:
            api.set_usual_corners("p", body)
        assert "not a list of section names" in e.value.what
    with pytest.raises(PmuError):
        SiteConfig.from_dict({"usual_corners": {"models/pdk_top.scs": "TOP_TT_X"}})
    # [] forgets the set
    api.set_usual_corners("p", {"corners": ["TOP_TT_X"]})
    assert api.set_usual_corners("p", {"corners": []})["saved"] == []
    assert _site(api)["usual_corners"] == {}


def test_the_plan_says_the_runs_per_corner(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    one = api.plan("p")
    per = one["by_corner"]["TOP_TT_X"]
    assert per > 0 and one["by_corner"] == {"TOP_TT_X": one["cost"]["runs"]}
    cfg = api.get_config("p")["config"]
    cfg["corners"] = ["TOP_TT_X", "TOP_FF_X"]
    api.put_config("p", {"config": cfg})
    assert api.plan("p")["by_corner"] == {"TOP_TT_X": per, "TOP_FF_X": per}


def test_the_routes_are_wired(api, tmp_path):
    _load(api, "p", _bench(tmp_path / "a"))
    assert any(m == "GET" and rx.match("/api/p/p/corners/usual") for m, rx, _ in server.ROUTES)
    assert any(m == "PUT" and rx.match("/api/p/p/corners/usual") for m, rx, _ in server.ROUTES)


# --------------------------------------------------------------------------- the page
PICKER_JS = r"""
const DECLARED = ['TOP_TT_RFTYP', 'TOP_FF_RFTYP', 'TOP_SS_RFTYP', 'TOP_FFQBEST_RFTYP', 'TOP_FF_RFCB'];
function pickerRoutes(o){
  o = o || {};
  const R = baseRoutes();
  R['GET /api/p/p/pins'] = [200, Object.assign(pinsPayload(), { section_choices: DECLARED,
    section_file: { file:'models/pdk_top.scs', read: o.read !== false, searched: o.read === false ? ['/a', '/b'] : [] } })];
  const cfg = configPayload(); cfg.config.corners = o.corners || ['TOP_TT_RFTYP'];
  R['GET /api/p/p/config'] = [200, cfg];
  R['PUT /api/p/p/config'] = (b) => [200, { config: b.config, sha:'c1', undoable:'config' }];
  R['GET /api/p/p/plan'] = o.plan || [200, { groups:[], cost:{ runs:160 }, by_corner:{ TOP_TT_RFTYP:160 } }];
  R['GET /api/p/p/corners/usual'] = o.usual || [200, { file:'models/pdk_top.scs', read:true,
    saved:['TOP_TT_RFTYP', 'TOP_XX_Y', 'TOP_SS_RFTYP'], corners:['TOP_TT_RFTYP', 'TOP_SS_RFTYP'], skipped:['TOP_XX_Y'] }];
  R['PUT /api/p/p/corners/usual'] = (b) => [200, { file:'models/pdk_top.scs', read:true, saved:b.corners,
    corners:b.corners, skipped:[] }];
  return R;
}
const main = (pg) => pg.nodes.main.innerHTML;
const crow = (pg) => { const m = main(pg); return m.slice(m.indexOf('>corners</span>'), m.indexOf('>temp C</span>')); };
const chipsOf = (pg) => [...crow(pg).matchAll(/<span class="chip on rc"[^>]*>([^<]+)<button/g)].map(m => m[1]);
const optsOf = (pg) => [...crow(pg).matchAll(/<button class="opt[^"]*"[^>]*>([^<]+)<\/button>/g)].map(m => m[1]);
const litOf = (pg) => (crow(pg).match(/<button class="opt on"[^>]*>([^<]+)</) || [])[1] || null;
const listOpen = (pg) => /id="cornerlist"[^>]*>/.exec(crow(pg))[0].indexOf('display:none') < 0;
const ev = (key) => ({ key, preventDefault(){}, stopPropagation(){} });
function combo(pg, kind, value, key){
  const id = act(main(pg), /data-combo="(a\d+)"/);
  pg.sandbox.ACTS[id](kind, ev(key), { value, setAttribute(){} });
}
const configPuts = (pg) => pg.calls.filter(c => c.method === 'PUT' && c.path === '/api/p/p/config');

(async () => {
  // ---- 1. only the chosen corners are chips; the list is there but closed
  {
    const pg = await openNew(pickerRoutes());
    const r = crow(pg);
    out.only = { chips: chipsOf(pg), toggles: (r.match(/<button class="chip/g) || []).length,
      open: listOpen(pg), note: r.includes('type to find one of the 5 sections models/pdk_top.scs declares'),
      cost: r.includes('each corner &asymp; +160 runs'), puts: configPuts(pg).length };
  }
  // ---- 2. the box: focus shows all, typing narrows, Up/Down lights, Enter adds the lit one
  {
    const pg = await openNew(pickerRoutes());
    const t = {};
    combo(pg, 'focus', '');                        // the list is toggled in the DOM; draw it
    pg.sandbox.render();
    t.all = optsOf(pg); t.all_lit = litOf(pg);
    combo(pg, 'input', 'ff');
    t.ff = optsOf(pg); t.ff_lit = litOf(pg); t.ff_open = listOpen(pg); t.kept = pg.S.sel['in:corner'];
    combo(pg, 'input', 'RfCb');
    t.rfcb = optsOf(pg);
    combo(pg, 'input', 'ff');
    combo(pg, 'key', 'ff', 'ArrowDown');
    t.down_lit = litOf(pg);
    combo(pg, 'key', 'ff', 'Escape');
    t.esc_closed = pg.S.sel.cornerOpen === false;
    pg.sandbox.render();
    t.esc_drawn_closed = !listOpen(pg) && pg.S.sel['in:corner'] === 'ff';
    combo(pg, 'key', 'ff', 'ArrowDown');
    t.reopened_lit = litOf(pg);
    combo(pg, 'key', 'ff', 'Enter');
    await settle();
    const put = configPuts(pg).pop();
    t.put = put && put.body.config.corners;
    t.chips_after = chipsOf(pg);
    t.typing_dropped = pg.S.sel['in:corner'] === undefined;
    t.opts_after = optsOf(pg);
    out.box = t;
  }
  // ---- 3. Enter on a name the read file does not declare is refused; unread: accepted
  {
    const pg = await openNew(pickerRoutes());
    combo(pg, 'input', 'TOP_XX');
    const t = { none: crow(pg).includes('models/pdk_top.scs declares no section matching TOP_XX') };
    combo(pg, 'key', 'TOP_XX', 'Enter');
    await settle();
    t.refused = crow(pg).includes('models/pdk_top.scs declares no section TOP_XX');
    t.puts = configPuts(pg).length;
    t.kept = pg.S.sel['in:corner'];
    const pu = await openNew(pickerRoutes({ read: false }));
    combo(pu, 'input', 'TOP_XX');
    t.unread_none = crow(pu).includes('Enter adds TOP_XX as typed');
    t.unread_note = crow(pu).includes('could not read models/pdk_top.scs') && crow(pu).includes('not verified');
    combo(pu, 'key', 'TOP_XX', 'Enter');
    await settle();
    const put = configPuts(pu).pop();
    t.unread_put = put && put.body.config.corners;
    out.undeclared = t;
  }
  // ---- 4. the x: disabled on the last corner (and no Remove in its menu); live on two
  {
    const pg = await openNew(pickerRoutes());
    const r = crow(pg);
    const ctx = pg.sandbox.CTXS[(r.match(/data-menuctx="(c\d+)"/) || [])[1]];
    const t = { last_disabled: /<button class="x" disabled title="the last corner cannot be removed/.test(r),
      last_menu: pg.sandbox.verbsFor('corner', ctx).filter(v => !v.sep).map(v => v.id) };
    const p2 = await openNew(pickerRoutes({ corners: ['TOP_TT_RFTYP', 'TOP_SS_RFTYP'] }));
    const r2 = crow(p2);
    t.two_disabled = (r2.match(/class="x" disabled/g) || []).length;
    t.two_live = (r2.match(/<button class="x" data-act=/g) || []).length;
    const ctx2 = p2.sandbox.CTXS[(r2.match(/data-menuctx="(c\d+)"/) || [])[1]];
    t.two_menu = p2.sandbox.verbsFor('corner', ctx2).filter(v => !v.sep).map(v => v.id);
    p2.sandbox.ACTS[act(r2, /<button class="x" data-act="(a\d+)" title="remove TOP_SS_RFTYP"/)]();
    await settle();
    const put = configPuts(p2).pop();
    t.removed = put && put.body.config.corners;
    out.x = t;
  }
  // ---- 5. no plan compiles: no cost, and no banner for it
  {
    const pg = await openNew(pickerRoutes({ plan: [400, { error:{ what:'no rail is modeled.', why:'w', do:['d'], where:'plan' } }] }));
    await settle(); pg.sandbox.render();
    out.noplan = { cost: crow(pg).includes('each corner'), banner: !!pg.S.errs.new,
      asked: count(pg, 'GET', '/api/p/p/plan') };
    const p0 = await openNew(pickerRoutes({ plan: [200, { groups:[], cost:{ runs:0 }, by_corner:{} }] }));
    out.noplan.zero = crow(p0).includes('each corner');
  }
  // ---- 6. my usual corners: shown, skipped named, applied only by the button, saved per file
  {
    const pg = await openNew(pickerRoutes());
    const r = crow(pg);
    const t = { use: r.includes('Use my usual corners (TOP_TT_RFTYP, TOP_SS_RFTYP)'),
      skipped: r.includes('TOP_XX_Y is not in models/pdk_top.scs, skipped'),
      save: r.includes('Save as my usual corners'), auto_applied: configPuts(pg).length };
    pg.sandbox.ACTS[act(r, /data-act="(a\d+)">Use my usual corners/)]();
    await settle();
    const put = configPuts(pg).pop();
    t.used = put && put.body.config.corners;
    pg.sandbox.ACTS[act(crow(pg), /data-act="(a\d+)" title="remember these corners[^"]*">Save as my usual corners/)]();
    await settle();
    const sv = pg.calls.filter(c => c.method === 'PUT' && c.path === '/api/p/p/corners/usual').pop();
    t.saved_body = sv && sv.body;
    t.after_save = crow(pg).includes('your usual corners') && !crow(pg).includes('Save as my usual corners');
    // the same set as the config's: no Use button
    const ps = await openNew(pickerRoutes({ corners: ['TOP_SS_RFTYP', 'TOP_TT_RFTYP'] }));
    t.same_no_use = !crow(ps).includes('Use my usual corners');
    out.usual = t;
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page gate needs it")
    d = tmp_path_factory.mktemp("cornerpicker")
    text = PAGE.read_text(encoding="utf-8")
    (d / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)),
                               encoding="utf-8", newline="\n")
    head = HARNESS_JS.split("(async () => {")[0]
    (d / "harness.js").write_text(head + PICKER_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(d / "harness.js"), str(d / "page.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_only_the_chosen_corners_are_chips(page):
    o = page["only"]
    assert o["chips"] == ["TOP_TT_RFTYP"] and o["toggles"] == 0
    assert not o["open"] and o["note"] and o["puts"] == 0


def test_typing_narrows_the_list_and_enter_adds_the_lit_row(page):
    b = page["box"]
    assert b["all"] == ["TOP_FF_RFTYP", "TOP_SS_RFTYP", "TOP_FFQBEST_RFTYP", "TOP_FF_RFCB"]
    assert b["all_lit"] is None                     # an empty box lights nothing
    assert b["ff"] == ["TOP_FF_RFTYP", "TOP_FFQBEST_RFTYP", "TOP_FF_RFCB"]
    assert b["ff_lit"] == "TOP_FF_RFTYP" and b["ff_open"] and b["kept"] == "ff"
    assert b["rfcb"] == ["TOP_FF_RFCB"]
    assert b["down_lit"] == "TOP_FFQBEST_RFTYP"
    assert b["esc_closed"] and b["esc_drawn_closed"]
    assert b["reopened_lit"] == "TOP_FF_RFTYP"
    assert b["put"] == ["TOP_TT_RFTYP", "TOP_FF_RFTYP"]
    assert b["chips_after"] == ["TOP_TT_RFTYP", "TOP_FF_RFTYP"] and b["typing_dropped"]
    assert "TOP_FF_RFTYP" not in b["opts_after"]


def test_enter_on_an_undeclared_name_is_refused_when_the_file_was_read(page):
    u = page["undeclared"]
    assert u["none"] and u["refused"] and u["puts"] == 0 and u["kept"] == "TOP_XX"
    assert u["unread_none"] and u["unread_note"]
    assert u["unread_put"] == ["TOP_TT_RFTYP", "TOP_XX"]


def test_the_last_corner_cannot_be_removed(page):
    x = page["x"]
    assert x["last_disabled"] and x["last_menu"] == ["only"]
    assert x["two_disabled"] == 0 and x["two_live"] == 2 and x["two_menu"] == ["only", "rm"]
    assert x["removed"] == ["TOP_TT_RFTYP"]


def test_the_cost_of_a_corner_comes_from_the_plan_or_is_not_shown(page):
    assert page["only"]["cost"]
    n = page["noplan"]
    assert not n["cost"] and not n["banner"] and n["asked"] == 1 and not n["zero"]


def test_usual_corners_are_offered_skipped_named_and_applied_only_by_the_button(page):
    u = page["usual"]
    assert u["use"] and u["skipped"] and u["save"] and u["auto_applied"] == 0
    assert u["used"] == ["TOP_TT_RFTYP", "TOP_SS_RFTYP"]
    assert u["saved_body"] == {"corners": ["TOP_TT_RFTYP", "TOP_SS_RFTYP"]}
    assert u["after_save"] and u["same_no_use"]
