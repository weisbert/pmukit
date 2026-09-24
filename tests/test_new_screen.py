"""The New screen, driven through `server.Api` the way the page drives it -- no browser.

What an RF-IC engineer must be able to do without a terminal:

* pick the PMU instance when the guess is wrong or fails -- the candidates come from the server,
  switching keeps every answer that is not about pins, and Ctrl-Z brings the old one back;
* re-read the netlist from where it was exported, in one click, and be told what changed:
  unchanged, pins gone (their answers dropped, by name), roles changed;
* type a path the way tcsh users do (`~`, `$WORK_ROOT`, `${VAR}`, relative), and when it is
  wrong, see the absolute path that was tried and the server's cwd;
* keep relative `include` lines meaning what they meant next to the ORIGINAL netlist, although
  the project works on a copy.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import time

import pytest

from pmukit import server
from pmukit.errors import PmuError
from pmukit.netlist import Netlist
from tests.test_server import PAGE, Client, four_part

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "pmu_demo"

#: A second, small subckt instance next to the PMU: a candidate the picker must offer.
REFGEN = """
subckt refgen (VREF VIN GND)
    Rtop (VIN VREF) resistor r=100k
    Rbot (VREF GND) resistor r=100k
ends refgen
XREF (vref VDDA_1V0 0) refgen
VB_VREF (vref 0) vsource dc=0.6
"""


# --------------------------------------------------------------------------- harness
@pytest.fixture
def bench(tmp_path):
    """The demo testbench exported to a directory of its own, the way ADE leaves it."""
    d = tmp_path / "bench"
    shutil.copytree(FIXTURE_DIR, d)
    return d


@pytest.fixture
def api(tmp_path):
    a = server.Api(root=tmp_path / "data")
    a.new_project({"name": "p"})
    return a


def wait(started: dict) -> server.Job:
    job = server.JOBS.get(started["job"])
    deadline = time.time() + 60
    while job.status in ("queued", "running"):
        assert time.time() < deadline, job.message
        time.sleep(0.02)
    return job


def load_ok(api, body: dict) -> dict:
    job = wait(api.load_netlist("p", body))
    assert job.status == "done", job.error
    return job.result


def edit(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def history(api) -> list:
    return server.Project("p", api.root).state().history().entries()


# --------------------------------------------------------------------------- PMU instance
def test_candidates_are_ranked_like_the_guess(bench):
    edit(bench / "input.scs", "simOpts options", REFGEN + "XUNDEF (a b c d e f g h i j k l) "
         "mystery\nsimOpts options")
    nl = Netlist.from_file(bench / "input.scs")
    cands = server.pmu_candidates(nl)
    assert [c["name"] for c in cands] == ["PMU_TOP", "XREF", "XUNDEF"]
    assert cands[0] == {"name": "PMU_TOP", "master": "pmu_demo", "pins": 11, "defined": True,
                        "guess": True}
    # an undefined master is offered even with the most pins, but never guessed
    assert cands[2]["pins"] == 12 and cands[2]["defined"] is False
    assert not any(c["name"].startswith(("IL_", "VB_", "VS_", "VEN_")) for c in cands)
    assert server.guess_pmu_inst(nl) == "PMU_TOP"


def test_switching_instance_reseeds_pins_and_keeps_the_other_answers(api, bench):
    edit(bench / "input.scs", "simOpts options", REFGEN + "simOpts options")
    first = load_ok(api, {"path": str(bench / "input.scs")})
    assert [c["name"] for c in first["candidates"]] == ["PMU_TOP", "XREF"]
    cfg = api.get_config("p")["config"]
    cfg.update(corners=["ss", "ff"], temps_c=[-40.0, 125.0], vset_codes=[2, 3],
               care_up_to_hz=3e9, state_note="RX mode")
    api.put_config("p", {"config": cfg, "note": "answers"})
    n_hist = len(history(api))

    out = api.set_instance("p", {"pmu_inst": "XREF"})
    assert out["pmu_inst"] == "XREF" and out["pins"]["pmu_inst"] == "XREF"
    assert out["changes"]["inst_switched"] is True
    after = api.get_config("p")
    new = after["config"]
    assert new["pmu_inst"] == "XREF"
    assert set(new["ports"]) == {"VREF", "VIN", "GND"}          # re-seeded for the new pins
    assert new["ports"]["VREF"] == "model" and new["my_load"] == {}
    for key in ("corners", "temps_c", "vset_codes", "care_up_to_hz", "state_note"):
        assert new[key] == cfg[key], key                        # the user's answers are kept
    assert len(history(api)) == n_hist + 1 and after["undoable"] == "config"
    info = api.netlist_info("p")
    assert info["pmu_inst"] == "XREF"
    assert "PMU_TOP -> XREF" in info["source"]["changes"]["text"]

    back = api.undo_config("p")
    assert back["config"]["pmu_inst"] == "PMU_TOP"
    assert api.pins("p")["pmu_inst"] == "PMU_TOP"


def test_a_failed_guess_offers_the_candidates_not_api_advice(api, bench, tmp_path):
    # the PMU subckt lives nowhere pmukit can read: nothing is defined, so nothing is guessed
    text = (bench / "input.scs").read_text(encoding="utf-8")
    text = re.sub(r"subckt pmu_demo .*?ends pmu_demo\n", "", text, flags=re.S)
    (bench / "input.scs").write_text(text, encoding="utf-8", newline="\n")
    job = wait(api.load_netlist("p", {"path": str(bench / "input.scs")}))
    assert job.status == "failed"
    err = job.error
    assert [c["name"] for c in err["candidates"]] == ["PMU_TOP"]
    assert err["candidates"][0]["defined"] is False
    assert any("Pick the PMU instance" in d for d in err["do"])
    assert not any("/api/" in d for d in err["do"])

    # the same picker data comes with the error the pins route answers over HTTP
    c = Client(root=api.root)
    try:
        status, payload = c.call("GET", "/api/p/p/pins")
    finally:
        c.close()
    assert status == 400
    assert four_part(payload)["candidates"][0]["name"] == "PMU_TOP"

    # picking one seeds the config -- pin names fall back to the nets, and the scan says so
    out = api.set_instance("p", {"pmu_inst": "PMU_TOP"})
    assert out["pmu_inst"] == "PMU_TOP"
    assert api.get_config("p")["config"]["pmu_inst"] == "PMU_TOP"
    assert any("not defined in this netlist" in n for n in out["pins"]["notes"])


def test_an_unknown_instance_is_refused_with_the_candidates(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    with pytest.raises(PmuError) as ei:
        api.set_instance("p", {"pmu_inst": "NOPE"})
    assert ei.value.extra["candidates"][0]["name"] == "PMU_TOP"
    assert api.get_config("p")["config"]["pmu_inst"] == "PMU_TOP"


# --------------------------------------------------------------------------- re-read
def test_reread_of_an_unchanged_file_says_unchanged_and_changes_nothing(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    n_hist = len(history(api))
    info = api.netlist_info("p")
    assert info["source"]["path"] == str(bench / "input.scs")
    assert info["source"]["on_disk"] == "same"
    out = load_ok(api, {"reread": True})
    assert out["changes"]["unchanged"] is True
    assert "unchanged" in out["changes"]["text"]
    assert len(history(api)) == n_hist


def test_reread_after_a_pin_vanished_drops_its_answers_and_says_so(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    cfg = api.get_config("p")["config"]
    cfg.update(corners=["ss"], temps_c=[25.0], stub_dc={"TESTMODE": 0.0})
    cfg["ports"]["VDD0P8_C"] = "stub"
    cfg["my_load"].pop("VDD0P8_C", None)
    api.put_config("p", {"config": cfg, "note": "answers"})
    n_hist = len(history(api))

    # the bench is fixed in Virtuoso and exported to the SAME path: TESTMODE is no longer a pin
    src = bench / "input.scs"
    edit(src, "IB_POLY EN TESTMODE VSS_A VSS_B AGND)", "IB_POLY EN VSS_A VSS_B AGND)")
    edit(src, "IB_POLY EN TESTMODE 0 0 0) pmu_demo", "IB_POLY EN 0 0 0) pmu_demo")
    assert api.netlist_info("p")["source"]["on_disk"] == "changed"

    out = load_ok(api, {"reread": True})
    ch = out["changes"]
    assert ch["unchanged"] is False and ch["pins_removed"] == ["TESTMODE"]
    assert ch["dropped"]["ports"] == ["TESTMODE"] and ch["dropped"]["stub_dc"] == ["TESTMODE"]
    assert "TESTMODE" in ch["text"] and "PMU_TOP still found" in ch["text"]
    new = api.get_config("p")["config"]
    assert "TESTMODE" not in new["ports"] and "stub_dc" not in new
    assert new["ports"]["VDD0P8_C"] == "stub"                   # the user's answer survives
    assert new["corners"] == ["ss"] and new["temps_c"] == [25.0]
    assert len(history(api)) == n_hist + 1                      # undoable like any change
    assert api.netlist_info("p")["source"]["changes"]["pins_removed"] == ["TESTMODE"]


def test_reread_when_a_role_appears_reseeds_that_pin(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    assert api.get_config("p")["config"]["ports"]["TESTMODE"] == "ignore"
    edit(bench / "input.scs", "VEN_EN      (EN 0)", "VEN_TESTMODE (TESTMODE 0) vsource dc=0\n"
                                                    "VEN_EN      (EN 0)")
    out = load_ok(api, {"reread": True})
    assert out["changes"]["roles_changed"] == [{"pin": "TESTMODE", "from": "none", "to": "en"}]
    assert api.get_config("p")["config"]["ports"]["TESTMODE"] == "model"


def test_reread_when_the_instance_was_renamed_offers_the_picker(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    edit(bench / "input.scs", "\nPMU_TOP (", "\nXPMU (")
    job = wait(api.load_netlist("p", {"reread": True}))
    assert job.status == "failed"
    assert [c["name"] for c in job.error["candidates"]] == ["XPMU"]
    # same cell, same pin names: picking the renamed instance carries the pin answers over
    out = api.set_instance("p", {"pmu_inst": "XPMU"})
    assert "carry over" in out["changes"]["text"]


def test_a_refused_deck_can_be_fixed_and_reread(api, bench):
    """scan()'s own refusals (here: a decap on a rail) reach the error block, and the file is
    remembered anyway -- so the fix in Virtuoso is followed by one click, not a new upload."""
    src = bench / "input.scs"
    edit(src, "simOpts options", "Cdecap (VDD0P8_A 0) capacitor c=1u\nsimOpts options")
    job = wait(api.load_netlist("p", {"path": str(src)}))
    assert job.status == "failed" and job.error["what"].startswith("decap on a rail")
    assert api.netlist_info("p")["source"]["path"] == str(src)
    edit(src, "Cdecap (VDD0P8_A 0) capacitor c=1u\n", "")
    out = load_ok(api, {"reread": True})
    assert out["pmu_inst"] == "PMU_TOP" and out["changes"]["first"] is True


def test_reread_of_a_dropped_file_explains_there_is_no_path(api, bench):
    load_ok(api, {"text": (bench / "input.scs").read_text(encoding="utf-8"),
                  "name": "input.scs"})
    assert api.netlist_info("p")["source"]["via"] == "upload"
    with pytest.raises(PmuError) as ei:
        api.load_netlist("p", {"reread": True})
    assert "dropped into the browser" in ei.value.why


# --------------------------------------------------------------------------- paths
def test_env_vars_and_tilde_expand_and_the_resolved_path_is_shown(api, bench, tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("PMK_BENCH", str(bench))
    for typed in ("$PMK_BENCH/input.scs", "${PMK_BENCH}/input.scs", "'$PMK_BENCH/input.scs'"):
        started = api.load_netlist("p", {"path": typed})
        assert started["source"]["path"] == str(bench / "input.scs")
        assert wait(started).status == "done"
    assert api.netlist_info("p")["source"]["typed"] == "'$PMK_BENCH/input.scs'"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    started = api.load_netlist("p", {"path": "~/bench/input.scs"})
    assert started["source"]["path"] == str(bench / "input.scs")
    wait(started)
    # ADE's netlist directory is accepted for the input.scs inside it
    started = api.load_netlist("p", {"path": "$PMK_BENCH"})
    assert started["source"]["path"] == str(bench / "input.scs")
    wait(started)


def test_a_relative_path_is_read_from_the_cwd_and_shown_absolute(api, bench, tmp_path,
                                                                  monkeypatch):
    monkeypatch.chdir(tmp_path)
    started = api.load_netlist("p", {"path": "bench/input.scs"})
    assert started["source"]["path"] == str(bench / "input.scs")
    assert started["source"]["typed"] == "bench/input.scs"
    wait(started)


def test_a_missing_path_names_the_absolute_path_tried_and_the_cwd(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PmuError) as ei:
        api.load_netlist("p", {"path": "nope/input.scs"})
    tried = str(tmp_path / "nope" / "input.scs")
    assert tried in ei.value.what and ei.value.where == tried
    assert str(tmp_path) in ei.value.why and "working directory" in ei.value.why
    monkeypatch.delenv("PMK_NOT_SET", raising=False)
    with pytest.raises(PmuError) as ei:
        api.load_netlist("p", {"path": "$PMK_NOT_SET/input.scs"})
    assert "$PMK_NOT_SET is not set" in ei.value.what


# --------------------------------------------------------------------------- includes
def test_includes_resolve_against_the_original_netlist_directory(api, bench):
    # the PMU subckt moves into its own file, included relatively -- as a DUT library would be
    src = bench / "input.scs"
    text = src.read_text(encoding="utf-8")
    body = re.search(r"subckt pmu_demo .*?ends pmu_demo\n", text, flags=re.S).group(0)
    (bench / "dut").mkdir()
    (bench / "dut" / "pmu.scs").write_text(body, encoding="utf-8", newline="\n")
    src.write_text(text.replace(body, 'include "dut/pmu.scs"\n'), encoding="utf-8",
                   newline="\n")

    out = load_ok(api, {"path": str(src)})
    # the copy lives in the data dir; the subckt was still found next to the ORIGINAL file
    assert out["pmu_inst"] == "PMU_TOP"
    assert out["candidates"][0]["defined"] is True
    assert any("read from the include" in n for n in out["notes"])
    names = [p["name"] for p in out["pins"]]
    assert "VSS_A" in names and "AGND" in names       # port names, not the nets they tie to (0)
    pr = server.Project("p", api.root)
    assert pr.netlist().section_names("pdk/toplevel.scs") == {"tt", "ss", "ff"}
    # without the origin, the copy alone resolves neither
    bare = Netlist.from_file(pr.netlist_path())
    assert bare.subckt_home("pmu_demo") is None
    assert bare.section_names("pdk/toplevel.scs") is None


# --------------------------------------------------------------------------- the page
NEW_ERR_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
function node(id){ return { id, innerHTML:'', style:{}, value:'', classList:{add(){},remove(){}},
  getAttribute(){return null;}, querySelectorAll(){return [];}, focus(){}, click(){} }; }
const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
  querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
  body:{appendChild(){},removeChild(){}} };
const sandbox = { document, navigator:{}, console, Math, JSON, Date, Number, String, Object,
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
  window:{ innerWidth:1440, innerHeight:900, location:{search:''}, addEventListener:()=>{} },
  fetch: () => new Promise(()=>{}), setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
sandbox.globalThis = sandbox;
vm.createContext(sandbox); vm.runInContext(src, sandbox);
const S = sandbox.S;
S.screen = 'new'; S.project = 'p';
S.data = { config: { exists:false, config:null }, netlistsrc: { source: { path:'/w/tb/input.scs',
  typed:'$WORK_ROOT/tb/input.scs', name:'input.scs', via:'path', on_disk:'changed' },
  copy:null, candidates:[], cwd:'/w' } };
S.errs.new = { status:501, error:{ what:'could not tell which instance is the PMU.', why:'y',
  do:['Pick the PMU instance from the list here'], where:'w',
  candidates:[{name:'PMU_TOP', master:'pmu_demo', pins:11, defined:false, guess:false}] } };
sandbox.render();
console.log(nodes.main.innerHTML);
"""


def test_new_screen_error_offers_the_picker_and_the_reread(tmp_path):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the render gate needs it")
    page = PAGE.read_text(encoding="utf-8")
    new_js = page[page.index("SCREENS.new = {"):page.index("SCREENS.plan = {")]
    assert "window.prompt" not in new_js, "the New screen asks in the page, not in a dialog"
    page_js = tmp_path / "page.js"
    page_js.write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", page)),
                       encoding="utf-8", newline="\n")
    check = tmp_path / "check.js"
    check.write_text(NEW_ERR_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(check), str(page_js)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    html = p.stdout
    assert 'id="instpick"' in html and "Use this instance" in html
    assert "Re-read input.scs" in html and "Load another" in html
    assert "changed on disk since the last read" in html
    assert 'id="nlpath"' in html and ">Load</button>" in html      # the path box, not a prompt
    assert "undefined" not in html
