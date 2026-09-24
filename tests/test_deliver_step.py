"""The Deliver step: one module name, the saved fit, honest grades, a usable include line, a diff.

QA on the Deliver screen found six things, each pinned here:

* the module was `PMU_<proj>_<corner>`, so switching `section=` also meant changing the master --
  against contract 4's "one include line, switch corners by section name". Every corner now
  defines `PMU_<proj>`; each section includes only its own corner's file, and a netlist that
  includes one section has exactly one definition live;
* Deliver re-fitted the whole dataset (four minutes, the progress bar parked at 30 %). It now
  emits from the saved fit.json and re-fits only when that is missing or older than the dataset;
* a verify.json older than the fit still graded the deliverable. Those grades are now the fit's
  own and report.md / grades.json say "verify is older than the fit -- grades are from the fit";
* the conditioning lines in report.md printed raw floats ("limit 1e+06 at 1e+09 Hz");
* the include line glued a '/' onto a Windows path;
* "Diff against the previous deliverable" only jumped to Home.

The project is built on the `fake` backend, so nothing is simulated.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

from pmukit import emit, fit as fitmod, jsonio, server
from pmukit.backends.fake import FakeBackend
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.deliverable import Deliverable, eng, ratio
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig
from pmukit.verify import hb as H, system as S
from tests.test_emit_va import CORNERS, demo_derived, demo_fits

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "pmu_demo" / "input.scs"
PAGE = REPO / "pmukit" / "web" / "index.html"
PROJECT = "dstep"


# --------------------------------------------------------------------------- a real project
@pytest.fixture(scope="module")
def built_project(tmp_path_factory):
    """config, derived, dataset and fit.json of one project -- two corners, nothing simulated."""
    root = tmp_path_factory.mktemp("dstep")
    d = root / PROJECT
    d.mkdir()
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": PROJECT, "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt", "ss"], "temps_c": [25], "vset_codes": [3],
        "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                  "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                  "EN": "model", "TESTMODE": "ignore"},
        "stub_dc": {"VDD0P8_C": 0.74},
        "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
        "care_up_to_hz": 1e9})
    site = SiteConfig(engine="fake")
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, site)
    cfg.save(d / "config.json")
    der.save(d / "derived.json")
    plan = compile_plan(cfg, der, nl, pins, site=site)
    led = Ledger(d / "runs.sqlite")
    plan.commit(led)
    dims = {"process": der.process["corners"], "temp_c": der.temps_c["points"],
            "vset": der.vset["codes"],
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(d / "dataset", project=cfg.project, config_sha=cfg.sha(), dims=dims)
    Runner(cfg, plan, led, site, dataset=ds, root=d / "runs", backend=FakeBackend(site)).run_all()
    result = fitmod.fit_project(ds, der)
    ds.close()
    led.close()
    jsonio.write(d / "fit.json", result.to_dict())
    return root


@pytest.fixture
def proj(built_project, tmp_path):
    """A private copy of the built project, so each test can age, delete or rewrite files."""
    root = tmp_path / "data"
    shutil.copytree(built_project, root)
    return root


def _age(path: pathlib.Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _no_fit(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("Deliver re-fitted the dataset although fit.json is current")
    monkeypatch.setattr(fitmod, "fit_project", boom)
    return calls


def _count_fits(monkeypatch):
    calls = []
    real = fitmod.fit_project

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(fitmod, "fit_project", counted)
    return calls


def _sections(scs_text: str) -> dict:
    return {m.group(1): m.group(2) for m in
            re.finditer(r"^section (\w+)\n(.*?)^endsection \1", scs_text, re.M | re.S)}


# --------------------------------------------------------------------------- 1. one module name
def test_every_corner_defines_the_same_module_and_each_section_includes_only_its_own(tmp_path):
    out = emit.deliver("demo_pmu", root=tmp_path, fit=demo_fits(), derived=demo_derived(),
                       stamp="s1")
    names = set()
    for corner in CORNERS:
        va = (out / f"PMU_demo_pmu_{corner}.va").read_text(encoding="utf-8")
        names |= set(re.findall(r"^module (\w+)\(", va, re.M))
        assert f"process corner {corner}" in va
    assert names == {"PMU_demo_pmu"}, "the master must not change with the corner"
    secs = _sections((out / "PMU_demo_pmu.scs").read_text(encoding="utf-8"))
    assert list(secs) == list(CORNERS)
    for corner, body in secs.items():
        incs = re.findall(r'^\s*ahdl_include "([^"]+)"', body, re.M)
        assert incs == [f"PMU_demo_pmu_{corner}.va"], (corner, incs)
    # the instance line is ONE line, the same master on every corner
    use = jsonio.read(out / "interface.json")
    assert use["module"] == "PMU_demo_pmu"
    assert use["instance_line"].split(")")[1].split()[0] == "PMU_demo_pmu"
    assert set(use["modules"].values()) == {"PMU_demo_pmu"}
    assert set(use["instance"].values()) == {use["instance_line"]}
    # the library says it: one section only
    assert "include ONE section only" in (out / "PMU_demo_pmu.scs").read_text(encoding="utf-8")


def test_the_verify_decks_include_exactly_one_corner(tmp_path):
    der = demo_derived()
    built = emit.build_va(demo_fits(), der, "ss", project="demo_pmu")
    drive = {"port": built["rails"][0], "f_hz": 1e6, "ampl_a": 1e-4, "z_peak_ohm": 10.0}
    for deck in (H.bench_deck(built, der, va_name="PMU_demo_pmu_ss.va", drive=drive),
                 S.model_deck(built, der, va_name="PMU_demo_pmu_ss.va", rail=built["rails"][0],
                              t=S.tank(1.2e9, 0.8, 5e-4))):
        assert re.findall(r"^ahdl_include .*$", deck, re.M) == ['ahdl_include "PMU_demo_pmu_ss.va"']
        assert re.search(r"^X1 \(.*\) PMU_demo_pmu\b", deck, re.M)
    # the HB check writes the corner's file, named like the deliverable's, in its own directory
    rep = H.hb_check(demo_fits(), der, corner="ss", project="demo_pmu", root=tmp_path,
                     site=SiteConfig(engine="fake"), backend=FakeBackend(SiteConfig(engine="fake")))
    va = pathlib.Path(rep["va"])
    assert va.name == "PMU_demo_pmu_ss.va" and va.parent.name == "ss"
    assert [p.name for p in va.parent.glob("*.va")] == ["PMU_demo_pmu_ss.va"]


def test_an_older_deliverable_with_per_corner_modules_still_lists_and_reads(tmp_path):
    """A stamp written before this change: modules per corner, no module / instance_line."""
    out = emit.deliver("demo_pmu", root=tmp_path, fit=demo_fits(), derived=demo_derived(),
                       stamp="old")
    use = jsonio.read(out / "interface.json")
    for k in ("module", "instance_line", "sections"):
        use.pop(k)
    use["modules"] = {c: f"PMU_demo_pmu_{c}" for c in CORNERS}
    jsonio.write(out / "interface.json", use)
    g = jsonio.read(out / "grades.json")
    g.pop("graded_by")
    g.pop("provisional")
    jsonio.write(out / "grades.json", g)
    api = server.Api(root=tmp_path)
    lst = api.deliverables("demo_pmu")["deliverables"]
    assert [x["stamp"] for x in lst] == ["old"]
    assert lst[0]["use"]["modules"]["ss"] == "PMU_demo_pmu_ss"
    assert lst[0]["provisional"] == "" and lst[0]["include"]


# --------------------------------------------------------------------------- 2. no re-fit
def test_deliver_emits_from_the_saved_fit_without_refitting(proj, monkeypatch):
    calls = _no_fit(monkeypatch)
    seen = []
    res = emit.deliver_project(PROJECT, root=proj, on_progress=lambda m, f: seen.append((m, f)))
    assert not calls and res["refit"] == ""
    fit = jsonio.read(proj / PROJECT / "fit.json")
    assert Deliverable.open(res["path"]).provenance.dataset_sha == fit["dataset_sha"]
    # real progress: one line per corner, and the fraction only moves forward
    msgs = [m for m, _f in seen]
    for corner in ("tt", "ss"):
        assert any(f"emitting corner {corner}" in m for m in msgs), msgs
    fr = [f for _m, f in seen]
    assert fr == sorted(fr) and fr[-1] == pytest.approx(0.99, abs=0.02)


def test_deliver_refits_when_fit_json_is_missing_and_says_so(proj, monkeypatch):
    (proj / PROJECT / "fit.json").unlink()
    calls = _count_fits(monkeypatch)
    res = emit.deliver_project(PROJECT, root=proj)
    assert len(calls) == 1
    assert res["refit"].startswith("re-fitted the dataset") and "no fit.json" in res["refit"]
    assert (proj / PROJECT / "fit.json").is_file(), "the new fit is the one the Model screen shows"
    # and the next delivery reuses it
    again = emit.deliver_project(PROJECT, root=proj, stamp="again")
    assert len(calls) == 1 and again["refit"] == ""


def test_deliver_refits_when_the_dataset_changed_after_the_fit(proj, monkeypatch):
    p = proj / PROJECT / "fit.json"
    fit = jsonio.read(p)
    fit["dataset_sha"] = "000000000000"
    jsonio.write(p, fit)
    calls = _count_fits(monkeypatch)
    res = emit.deliver_project(PROJECT, root=proj)
    assert len(calls) == 1 and "dataset changed" in res["refit"]


def test_the_cli_takes_the_same_path(proj, monkeypatch, capsys):
    from pmukit import cli
    calls = _no_fit(monkeypatch)
    monkeypatch.setenv("PMUKIT_DATA", str(proj))
    assert cli.main(["deliver", PROJECT]) == 0
    text = capsys.readouterr().out
    assert not calls
    assert "emitting corner tt" in text and "delivered ->" in text
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("include "))
    path = line.split('"')[1]
    assert ("/" in path) != ("\\" in path), f"mixed separators: {path}"
    assert "The instance's master is PMU_dstep on every corner." in text


def test_the_web_deliver_job_reuses_the_fit_and_refuses_while_a_fit_runs(proj, monkeypatch):
    calls = _no_fit(monkeypatch)
    api = server.Api(root=proj)
    fake = server.Job("fit", PROJECT, "fit the model")
    fake.status = "running"
    with server.JOBS._lock:
        server.JOBS._jobs[fake.id] = fake
        server.JOBS._order.append(fake.id)
    try:
        with pytest.raises(server.PmuError) as e:
            api.deliver(PROJECT, {})
        assert "still fitting" in e.value.what
    finally:
        fake.status = "done"
    jid = api.deliver(PROJECT, {})["job"]
    job = server.JOBS.get(jid)
    for _ in range(600):
        if job.status not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job.status == "done", job.error
    assert not calls and job.result["refit"] == ""
    assert any("emitting corner ss" in e["text"] for e in job.events)


# --------------------------------------------------------------------------- 3. stale verify
def _verify(proj, grade="green"):
    fit = fitmod.FitResult.from_dict(jsonio.read(proj / PROJECT / "fit.json"))
    bf = next(iter(fit))
    return {"grades": [{"port": bf.port, "corner": "tt", "block": bf.block, "grade": grade,
                        "detail": "from verify", "score": 0.1}],
            "ls_default_on": ["VDD0P8_A"], "hb_check": {"status": "pass"}}


def test_a_verify_older_than_the_fit_makes_the_grades_provisional(proj, monkeypatch):
    _no_fit(monkeypatch)
    d = proj / PROJECT
    jsonio.write(d / "verify.json", _verify(proj))
    _age(d / "verify.json", 120)                   # the fit was re-run after verify
    _age(d / "fit.json", 0)
    res = emit.deliver_project(PROJECT, root=proj)
    assert res["verify"] == "stale" and res["graded_by"] == "fit"
    out = pathlib.Path(res["path"])
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "verify is older than the fit -- grades are from the fit" in report
    g = jsonio.read(out / "grades.json")
    assert g["provisional"] == "verify is older than the fit -- grades are from the fit"
    assert g["graded_by"] == "fit"
    assert g["grades"], "the fit's own grades, not an empty table"
    assert not any(x["detail"] == "from verify" for x in g["grades"])
    # an HB check that judged another model switches nothing on
    assert jsonio.read(out / "envelope.json")["ls_default_on"] == []
    # the Deliver screen's API says so, for this stamp and for the next delivery
    lst = server.Api(root=proj).deliverables(PROJECT)
    assert lst["deliverables"][0]["provisional"].startswith("verify is older than the fit")
    assert lst["verify"]["state"] == "stale"


def test_a_current_verify_grades_the_deliverable(proj, monkeypatch):
    _no_fit(monkeypatch)
    d = proj / PROJECT
    _age(d / "fit.json", 120)
    jsonio.write(d / "verify.json", _verify(proj, "yellow"))
    res = emit.deliver_project(PROJECT, root=proj)
    assert res["verify"] == "current" and res["provisional"] == ""
    g = jsonio.read(pathlib.Path(res["path"]) / "grades.json")
    assert g["graded_by"] == "verify" and g["provisional"] == ""
    assert [x["detail"] for x in g["grades"]] == ["from verify"]
    assert "Provisional grades" not in (pathlib.Path(res["path"]) / "report.md").read_text("utf-8")
    assert server.Api(root=proj).deliverables(PROJECT)["verify"]["state"] == "current"


def test_no_verify_at_all_is_provisional_too(proj, monkeypatch):
    _no_fit(monkeypatch)
    res = emit.deliver_project(PROJECT, root=proj)
    assert res["verify"] == "missing"
    assert "verify has not run on this fit" in \
        (pathlib.Path(res["path"]) / "report.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 4. numbers
def test_the_conditioning_lines_are_in_engineering_notation(tmp_path):
    out = emit.deliver("demo_pmu", root=tmp_path, fit=demo_fits(), derived=demo_derived(),
                       stamp="n1", hb_robust=False)
    report = (out / "report.md").read_text(encoding="utf-8")
    lines = [ln for ln in report.splitlines() if ln.startswith("- conditioning ")]
    assert lines, "the HB section lists one conditioning line per corner"
    for ln in lines:
        assert not re.search(r"\de[+-]\d", ln), f"raw float in report.md: {ln}"
        assert "(limit 1e6)" in ln and " GHz " in ln
    assert "8.89 kH" in (out / "hb_check.txt").read_text(encoding="utf-8")


def test_the_number_helpers():
    assert eng(1e9, "Hz") == "1 GHz" and eng(2e-6, "A") == "2 uA" and eng(8890, "H") == "8.89 kH"
    assert ratio(1e6) == "1e6" and ratio(3.2e7) == "3.2e7" and ratio(250) == "250"
    assert ratio(2.399e10) == "2.399e10" and ratio(float("inf")) == "inf"


# --------------------------------------------------------------------------- 5. include line
def test_the_include_line_uses_one_separator(proj, monkeypatch):
    _no_fit(monkeypatch)
    res = emit.deliver_project(PROJECT, root=proj)
    d = Deliverable.open(res["path"])
    line = d.include_line()
    path = line.split('"')[1]
    assert line.endswith(" section=tt")
    assert path == str(pathlib.Path(res["path"]) / "PMU_dstep.scs")
    assert not ("/" in path and "\\" in path), f"mixed separators: {path}"
    row = server.Api(root=proj).deliverables(PROJECT)["deliverables"][0]
    assert row["include"] == line and row["sep"] == os.sep


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_joins_a_path_with_the_separator_it_came_with():
    src = PAGE.read_text(encoding="utf-8")
    fns = []
    for name in ("joinPath", "includeLine"):
        m = re.search(r"^function " + name + r"\(.*?^}\n", src, re.M | re.S)
        assert m, name
        fns.append(m.group(0))
    js = "\n".join(fns) + r"""
const dvw = { path: "C:\\data\\p\\deliver\\20260924-053504", sep: "\\", scs: "PMU_qa_pmu.scs",
              include: "", envelope: { corners: ["tt"] } };
const dvp = { path: "/work/pmukit/data/p/deliver/20260924-053504/", scs: "PMU_qa_pmu.scs",
              envelope: { corners: ["tt"] } };
console.log(JSON.stringify([includeLine(dvw, "", "ss"), includeLine(dvp, "", "tt"),
  joinPath("C:\\x\\y", "f.scs"), includeLine({ include: 'include "S" section=tt', path: "",
  envelope: { corners: ["tt"] } }, "", "tt")]));
"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    import json
    got = json.loads(r.stdout)
    assert got[0] == 'include "C:\\data\\p\\deliver\\20260924-053504\\PMU_qa_pmu.scs" section=ss'
    assert got[1] == 'include "/work/pmukit/data/p/deliver/20260924-053504/PMU_qa_pmu.scs" section=tt'
    assert got[2] == "C:\\x\\y\\f.scs"
    assert got[3] == 'include "S" section=tt', "the server's line is used as is"


# --------------------------------------------------------------------------- 6. the diff
def test_diff_against_the_previous_deliverable(proj, monkeypatch):
    _no_fit(monkeypatch)
    d = proj / PROJECT
    first = emit.deliver_project(PROJECT, root=proj, stamp="20260101-000000")
    # something real changes: verify now grades one block, and it switched a term on
    _age(d / "fit.json", 120)
    jsonio.write(d / "verify.json", _verify(proj, "red"))
    second = emit.deliver_project(PROJECT, root=proj, stamp="20260102-000000")
    assert first["graded_by"] == "fit" and second["graded_by"] == "verify"

    api = server.Api(root=proj)
    r = api.deliverable_diff(PROJECT, "20260102-000000")
    assert r["previous"] == r["a"] == "20260101-000000" and r["b"] == "20260102-000000"
    x = r["diff"]
    assert set(x) >= {"files", "valid", "grades", "pins", "provenance", "text", "envelope"}
    assert "grades.json" in x["files"]["changed"] and "report.md" in x["files"]["changed"]
    assert any(v["what"] == "large-signal on" and v["b"] == "VDD0P8_A" for v in x["valid"])
    red = [g for g in x["grades"] if g["b"] == "red"]
    assert red and red[0]["port"] and red[0]["block"]
    assert x["pins"] == {}, "same pins, same order"
    # the model text: provenance header left out, the load_en default that changed shown
    va = [t for t in x["text"] if t["file"] == "PMU_dstep_tt.va"]
    assert va and "load_en_VDD0P8_A" in va[0]["diff"]
    assert "created" not in va[0]["diff"]
    assert all(len(t["diff"].splitlines()) <= 161 for t in x["text"])
    # the first stamp has nothing before it; an unknown stamp is named
    with pytest.raises(server.PmuError) as e:
        api.deliverable_diff(PROJECT, "20260101-000000")
    assert "first deliverable" in e.value.what
    with pytest.raises(server.PmuError):
        api.deliverable_diff(PROJECT, "nope")
    # an explicit `against` works both ways
    back = api.deliverable_diff(PROJECT, "20260101-000000", "20260102-000000")
    assert back["a"] == "20260102-000000"


DELIVER_JS = r"""
(async () => {
  const DV = (stamp, prov) => ({ stamp, path: 'C:\\d\\p\\deliver\\' + stamp, sep: '\\',
    scs: 'PMU_p.scs', include: 'include "C:\\d\\p\\deliver\\' + stamp + '\\PMU_p.scs" section=tt',
    files: [{ name: 'PMU_p.scs', kind: 'Spectre library', bytes: 900, sha: 'aa', desc: '' },
            { name: 'report.md', kind: 'report', bytes: 900, sha: 'bb', desc: '' }],
    use: { module: 'PMU_p', instance_line: 'PMU_TOP (A B 0) PMU_p vset=3',
           modules: { tt: 'PMU_p', ss: 'PMU_p' }, instance: { tt: 'PMU_TOP (A B 0) PMU_p vset=3' },
           pmu_order: true, pass_through: [], params: { load_en: [] } },
    graded_by: prov ? 'fit' : 'verify', provisional: prov || '',
    envelope: { corners: ['tt', 'ss'] }, provenance: { created: 'x' } });
  fresh('deliver');
  ROUTES['GET /api/p/p/deliverables'] = [200, { deliverables: [
      DV('20260102-000000', 'verify is older than the fit -- grades are from the fit'),
      DV('20260101-000000', '')],
    verify: { state: 'stale', why: 'verify is older than the fit -- the next one too' } }];
  ROUTES['GET /api/p/p/deliverables/20260102-000000/files/PMU_p.scs'] = [200, { text: 'library PMU_p', bytes: 13 }];
  ROUTES['GET /api/p/p/deliverables/20260102-000000/diff'] = [200, { a: '20260101-000000',
    b: '20260102-000000', previous: '20260101-000000', diff: {
      files: { added: [], removed: [], changed: ['report.md'] },
      valid: [{ what: 'large-signal on', a: '(none)', b: 'VDD0P8_A' }],
      grades: [{ port: 'VDD0P8_A', corner: 'tt', block: 'zout', a: 'green', b: 'red' }],
      pins: {}, provenance: {}, envelope: {},
      text: [{ file: 'PMU_p_tt.va', diff: '--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y', added: 1,
               removed: 1, truncated: false }] } }];
  for (let i = 0; i < 3; i++) { sandbox.render(); await flush(); }
  const m = nodes.main.innerHTML;
  const out = { include: (m.match(/include (?:&quot;|")([^&"]*)/) || [])[1],
    verify_first: m.includes('Verify first'), provisional: m.includes('grades are provisional'),
    one_master: m.includes('the master stays'), diff_btn: m.includes('Diff against the previous deliverable') };
  act(m, /data-act="(a\d+)">Diff against the previous deliverable/)();
  for (let i = 0; i < 3; i++) { sandbox.render(); await flush(); }
  const m2 = nodes.main.innerHTML;
  out.diff = { asked: count('GET', '/api/p/p/deliverables/20260102-000000/diff'),
    grades: m2.includes('Grades per port and block'), red: m2.includes('b-bad'),
    added_line: m2.includes('<span class="a">+y</span>'), valid: m2.includes('VDD0P8_A') };
  S.job = { status: 'running', title: 'fitting the model' }; sandbox.render();
  out.busy = /<button class="btn pri"[^>]*disabled[^>]*>Deliver again/.test(nodes.foot.innerHTML);
  const before = calls.length; sandbox.doDeliver(); await flush();
  out.busy_no_post = calls.length === before;
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_deliver_screen_warns_diffs_and_waits(tmp_path):
    """The page's own script, in node against a scripted fetch (the harness of
    test_web_robustness): the provisional warning with "Verify first", the include line as the
    server wrote it, the diff panel, and Deliver disabled while a job runs."""
    import json
    from tests.test_web_robustness import SCENARIOS_JS
    head = SCENARIOS_JS.split("(async () => {")[0]
    text = PAGE.read_text(encoding="utf-8")
    (tmp_path / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>",
                                                           text)), encoding="utf-8", newline="\n")
    (tmp_path / "check.js").write_text(head + DELIVER_JS, encoding="utf-8", newline="\n")
    p = subprocess.run(["node", str(tmp_path / "check.js"), str(tmp_path / "page.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["include"] == "C:\\d\\p\\deliver\\20260102-000000\\PMU_p.scs"
    assert out["verify_first"] and out["provisional"] and out["one_master"] and out["diff_btn"]
    d = out["diff"]
    assert d["asked"] == 1 and d["grades"] and d["red"] and d["added_line"] and d["valid"]
    assert out["busy"] and out["busy_no_post"]


def test_the_diff_trims_a_long_text_diff(tmp_path):
    from pmukit import deliverable as D
    a = emit.deliver("demo_pmu", root=tmp_path, fit=demo_fits(), derived=demo_derived(),
                     stamp="a")
    b = emit.deliver("demo_pmu", root=tmp_path, fit=demo_fits(), derived=demo_derived(),
                     stamp="b", hb_robust=False)
    va = b / "PMU_demo_pmu_tt.va"
    va.write_text(va.read_text(encoding="utf-8") + "".join(f"// pad {i}\n" for i in range(500)),
                  encoding="utf-8", newline="\n")
    x = Deliverable.open(a).diff(Deliverable.open(b))
    t = next(t for t in x["text"] if t["file"] == "PMU_demo_pmu_tt.va")
    assert t["truncated"] and len(t["diff"].splitlines()) == D.DIFF_LINES_PER_FILE + 1
    assert t["added"] >= 500
    total = sum(len(t["diff"].splitlines()) for t in x["text"])
    assert total <= D.DIFF_LINES_TOTAL + len(x["text"])
