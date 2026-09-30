"""The output code is fixed on the chip: small-signal at the nominal code, the others DC only.

A designer moves the code in simulation only for the V of PVT.  Across the code range the DC
output is what changes; Zout / PSRR move ~2 dB -- except a headroom collapse at the top codes.
So, pinned here:

* the plan runs Zout / PSRR only at the nominal code (DC per code), one writer per cell;
* with more than one code, the output-code check re-runs Zout / PSRR at the lowest and the
  highest code -- per corner, at the coldest and the hottest temperature, at the nominal load --
  and those runs feed nothing, land in their own dataset and are never fitted;
* `verify.codecheck` compares measured against measured and grades ok / marginal / bad;
* the delivered instance line writes the designer's own code variable when the bench declares it;
* a whole fake project with codes [3, 1, 5] still plans, runs, fits, verifies and delivers.
"""
from __future__ import annotations

import pathlib

import numpy as np
import pytest

from pmukit import jsonio, spec
from pmukit.backends.fake import FakeBackend
from pmukit.config import DerivedConfig, ProjectConfig, derive
from pmukit.dataset import Dataset, cell_key
from pmukit.deliverable import Envelope, render_report
from pmukit.emit import instance_params
from pmukit.importer import CODE_CHECK_DIR, variable_spec
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import (CODE_CHECK, check_codes, compile_plan, load_states,
                         nominal_state)
from pmukit.site import SiteConfig
from pmukit.verify import codecheck
from pmukit.verify.grades import CODE_CHECK_MARGINAL_DB, CODE_CHECK_OK_DB
from tests.test_plan import CFG, DEMO

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"


def _plan(codes, **over):
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": codes, **over})
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    der = derive(cfg, pins, SiteConfig(engine="spectre_ssh"))
    return cfg, der, compile_plan(cfg, der, nl, pins, site=SiteConfig(engine="spectre_ssh"))


# --------------------------------------------------------------------------- the spec
def test_small_signal_has_no_code_axis_and_dc_keeps_it():
    for name in ("zout", "psrr"):
        for p in spec.block(name, "rail").params:
            assert "vset" not in p.axes, (name, p.name)
    dc = {p.name: p.axes for p in spec.block("dc", "rail").params}
    assert all("vset" in dc[k] for k in ("vout", "vout_tc", "dropout", "ilimit"))
    assert all("vset" in p.axes for p in spec.block("idc", "bias").params
               if p.observable == "dc_iv" and p.name not in ("pol", "knee_side"))


# --------------------------------------------------------------------------- the plan
def test_ac_runs_only_at_the_nominal_code_and_dc_runs_per_code():
    _cfg, der, plan = _plan([3, 1, 5])
    assert der.vset["codes"][0] == 3
    for pr in plan.runs(enabled_only=False):
        if pr.check:
            continue
        if pr.run.analysis in ("ac", "noise") or pr.run.analysis.startswith("tran"):
            assert pr.run.vset == 3, (pr.run.analysis, pr.run.vset)
    for gid in ("dc_load:IL_VDD0P8_A", "dc_iv:VB_IB_PTAT"):
        assert {r.run.vset for r in plan.group(gid).runs} == {1, 3, 5}


def test_extra_codes_add_dc_runs_not_ac_runs():
    """Before, every extra code multiplied every Zout/PSRR run; now it adds DC runs + the check."""
    _c, _d, one = _plan([3])
    _c, _d, three = _plan([3, 1, 5])
    ac = lambda p: [r for r in p.runs(enabled_only=False)                     # noqa: E731
                    if r.run.analysis == "ac" and not r.check]
    assert len(ac(three)) == len(ac(one))
    assert not any(g.id == CODE_CHECK for g in one.groups)


def test_one_writer_per_cell_and_nothing_written_twice():
    """Every (variable, dataset cell) of the fitted dataset has exactly ONE designated run."""
    _cfg, der, plan = _plan([3, 1, 5])
    port_type = {**{p: "rail" for p in der.rails}, **{p: "bias" for p in der.biases},
                 **{p: "en" for p in der.en}}
    states = {s.key: s for s in plan.states}
    seen: dict = {}
    for pr in plan.runs(enabled_only=False):
        if pr.check:
            continue
        run = pr.run
        for var in run.reads:
            obs, port = var.split(".", 1)
            dims = variable_spec(obs, port_type[port])["dims"]
            cell = {"process": run.process}
            if "temp_c" in dims:
                cell["temp_c"] = run.temp_c
            if "vset" in dims:
                cell["vset"] = run.vset
            if "load_a" in dims:
                cell["load_a"] = states[run.load_key].of(port) if run.load_key else -1.0
            key = (var, cell_key(cell))
            seen.setdefault(key, []).append(run.run_id)
    twice = {k: v for k, v in seen.items() if len(v) > 1}
    assert not twice, list(twice.items())[:3]


def test_the_check_runs_the_extreme_codes_at_the_extreme_temperatures_nominal_load():
    _cfg, der, plan = _plan([3, 1, 5])
    g = plan.group(CODE_CHECK)
    nominal = nominal_state(load_states(der), der)
    runs = [r.run for r in g.runs]
    # 2 corners x {-40, 125} x {1, 5} x (Zout per rail (2) + the supply injection (1))
    assert len(runs) == 2 * 2 * 2 * 3
    assert {r.vset for r in runs} == {1, 5}
    assert {r.temp_c for r in runs} == {-40.0, 125.0}
    assert {r.process for r in runs} == {"tt", "ss"}
    assert {r.load_key for r in runs} == {nominal.key}
    assert {r.stimulus for r in runs} == {"IL_VDD0P8_A", "IL_VDD0P8_B", "VS_VDDA_1V0"}
    # rails only: the bias PSRR of the same injection is not part of the check
    assert set().union(*(r.reads for r in runs)) == {"ac_zout.a", "ac_zout.b", "ac_psrr.a",
                                                     "ac_psrr.b"}
    # never fitted: no run feeds a parameter, and un-ticking the group loses nothing
    assert all(r.feeds == () and r.check == CODE_CHECK for r in g.runs)
    plan.set_enabled(CODE_CHECK, False)
    assert plan.consequences() == []
    assert "feeds no parameter" in g.runs[0].why()
    ids = [r.run_id for r in plan.runs(enabled_only=False)]
    assert len(ids) == len(set(ids))


def test_the_check_skips_a_code_equal_to_the_nominal_and_is_absent_with_one_code():
    # bench exported at VSET=3: 3 is nominal and the highest, so only the lowest is checked
    _cfg, der, plan = _plan([1, 3])
    assert der.vset["codes"][0] == 3 and check_codes(der) == [1]
    assert {r.run.vset for r in plan.group(CODE_CHECK).runs} == {1}
    _cfg, der, plan = _plan([3])
    assert check_codes(der) == []


# --------------------------------------------------------------------------- the comparison
FREQ = np.logspace(1, 9, 81)
RAIL = "R"


def _derived(codes=(3, 1, 5)) -> DerivedConfig:
    d = DerivedConfig(project="cc", config_sha="c0")
    d.process = {"corners": ["ss"]}
    d.temps_c = {"points": [-40.0, 25.0, 125.0]}
    d.vset = {"codes": list(codes), "param": "LDO_VSET"}
    d.freq = {"start_hz": 10.0, "stop_hz": 1.0e9}
    d.rails = {RAIL: {"i_typ_a": 5e-4}}
    d.loads = {RAIL: {"points_a": [1e-4, 5e-4]}}
    return d


def _zout(f):
    return 0.05 + 1j * 2 * np.pi * f * 1e-8


def _psrr(f):
    return 1e-3 * (1 + 1j * f / 1e5)


def _datasets(tmp_path, bump_psrr=None):
    """Main dataset (nominal code, no vset axis) + the check dataset (per code)."""
    der = _derived()
    dims = {"process": ["ss"], "temp_c": [-40.0, 25.0, 125.0], "vset": [1, 3, 5],
            "load_a": {RAIL: [1e-4, 5e-4]}}
    main = Dataset.create(tmp_path / "dataset", project="cc", config_sha="c0", dims=dims)
    chk = Dataset.create(tmp_path / CODE_CHECK_DIR, project="cc", config_sha="c0", dims=dims)
    for obs, fn in (("ac_zout", _zout), ("ac_psrr", _psrr)):
        var = f"{obs}.{RAIL}"
        main.declare(var, dims=variable_spec(obs, "rail")["dims"], dtype="complex128", coord=FREQ)
        chk.declare(var, dims=variable_spec(obs, "rail", per_code=True)["dims"],
                    dtype="complex128", coord=FREQ)
        for t in (-40.0, 125.0):
            main.put(var, {"process": "ss", "temp_c": t, "load_a": 5e-4}, fn(FREQ))
            for code in (1, 5):
                y = fn(FREQ) * 10 ** (0.5 / 20)                    # +0.5 dB: the beta shift
                if bump_psrr and obs == "ac_psrr" and code == 5 and t == -40.0:
                    y = y * bump_psrr(FREQ)
                chk.put(var, {"process": "ss", "temp_c": t, "vset": code, "load_a": 5e-4}, y)
    return main, chk, der


def test_a_flat_code_dependence_grades_ok(tmp_path):
    main, chk, der = _datasets(tmp_path)
    cc = codecheck.compare(main, chk, der)
    assert cc["nominal"] == 3 and cc["codes"] == [1, 5] and cc["temps_c"] == [-40.0, 125.0]
    assert len(cc["rows"]) == 1 * 2 * 2                    # rail x temps x codes
    assert cc["grade"] == "ok"
    for r in cc["rows"]:
        assert r["grade"] == "ok"
        assert r["zout_db"] == pytest.approx(0.5, abs=1e-6)
        assert r["psrr_db"] == pytest.approx(0.5, abs=1e-6)
    assert "ok" in codecheck.summary_line(cc) and "small-signal from code 3" in \
        codecheck.summary_line(cc)


def test_a_collapsed_psrr_at_the_top_code_grades_bad_with_its_frequency(tmp_path):
    # a 9 dB bump centred at 2 MHz at code 5, -40 C: the loop runs out of headroom
    bump = lambda f: 10 ** ((9.0 * np.exp(-np.log(f / 2e6) ** 2 / 0.2)) / 20)   # noqa: E731
    main, chk, der = _datasets(tmp_path, bump_psrr=bump)
    cc = codecheck.compare(main, chk, der)
    assert cc["grade"] == "bad"
    bad = [r for r in cc["rows"] if r["grade"] == "bad"]
    assert len(bad) == 1
    r = bad[0]
    assert (r["code"], r["temp_c"], r["corner"]) == (5, -40.0, "ss")
    assert r["psrr_db"] > CODE_CHECK_MARGINAL_DB and r["psrr_sign"] == 1
    assert r["psrr_hz"] == pytest.approx(2e6, rel=0.15)
    assert "R at code 5, ss -40C: PSRR 9.5 dB worse at 2" in r["text"]
    assert "headroom?" in r["text"] and "characterize that code as nominal" in r["text"]
    # the other rows are untouched
    assert all(x["grade"] == "ok" for x in cc["rows"] if x is not r)
    assert codecheck.grade_db(CODE_CHECK_OK_DB) == "ok"
    assert codecheck.grade_db(CODE_CHECK_OK_DB + 0.1) == "marginal"
    assert codecheck.grade_db(CODE_CHECK_MARGINAL_DB + 0.1) == "bad"


def test_the_check_only_looks_inside_the_care_band(tmp_path):
    # the same bump, but above the care band: not the consumer's problem, not reported
    bump = lambda f: 10 ** ((9.0 * np.exp(-np.log(f / 5e8) ** 2 / 0.05)) / 20)  # noqa: E731
    main, chk, der = _datasets(tmp_path, bump_psrr=bump)
    der.freq["stop_hz"] = 1e8
    assert codecheck.compare(main, chk, der)["grade"] == "ok"


def test_no_check_data_is_not_checked_never_bad(tmp_path):
    main, _chk, der = _datasets(tmp_path)
    cc = codecheck.compare(main, None, der)
    assert cc["grade"] == "not_run"
    assert all(r["grade"] == "not_run" and "not checked" in r["text"] for r in cc["rows"])
    assert "has not run yet" in codecheck.summary_line(cc)
    # one code: nothing to check, nothing said
    assert codecheck.compare(main, None, _derived((3,)))["codes"] == []
    assert codecheck.summary_line(codecheck.compare(main, None, _derived((3,)))) == ""


def test_the_report_has_an_output_code_section_with_the_red_row(tmp_path):
    bump = lambda f: 10 ** ((9.0 * np.exp(-np.log(f / 2e6) ** 2 / 0.2)) / 20)   # noqa: E731
    main, chk, der = _datasets(tmp_path, bump_psrr=bump)
    cc = codecheck.compare(main, chk, der)
    env = Envelope(freq_max_hz=1e9, load_a={RAIL: (1e-4, 5e-4)}, temp_c=(-40.0, 125.0),
                   corners=["ss"], vset_codes=[3, 1, 5], ls_default_on=[], ports=[RAIL])
    text = render_report(project="cc", stamp="s", envelope=env, grades=[], hb_check=None,
                         not_run=[], stubs=[], pins=[{"pin": RAIL, "modeled": True,
                                                      "what": "rail", "role": "rail"}],
                         code_check=cc, vset="LDO_VSET")
    sec = text.split("## Output code: other codes", 1)[1].split("\n## ", 1)[0]
    assert "Small-signal (Zout, PSRR, noise) is from code 3" in sec
    assert "(1, 5) change the DC output only" in sec
    assert "ok <= 2 dB, marginal <= 6 dB" in sec
    assert "| R | 5 | ss | -40 C |" in sec
    assert "**RED:** R at code 5, ss -40C: PSRR 9.5 dB worse at 2" in sec
    # the Pins / usage text: the designer's variable, the snap, the nominal small-signal
    assert "`vset=LDO_VSET`" in text
    assert "snaps to the nearest characterized code" in text
    assert "are from code 3 at every code" in text


# --------------------------------------------------------------------------- instance line
def test_the_instance_line_follows_the_designers_variable_when_the_bench_declares_it():
    d = _derived()
    d.vset["declared"] = True
    assert instance_params(d) == [("vset", "LDO_VSET")]
    d.vset["declared"] = False
    assert instance_params(d) == [("vset", 3)]
    d.vset.pop("declared")                       # an older derived.json: the number, as before
    assert instance_params(d) == [("vset", 3)]
    d.vset = {}
    assert instance_params(d) == []


def test_derive_records_whether_the_bench_declares_the_code_variable():
    nl = Netlist(DEMO.replace("parameters VSET=3", "parameters LDO_VSET=3"), "tb/input.scs")
    cfg = ProjectConfig.from_dict({**CFG, "vset_codes": [3, 1], "vset_param": "LDO_VSET"})
    der = derive(cfg, nl.scan("PMU_TOP", ports=cfg.ports))
    assert der.vset["declared"] is True and der.vset["param"] == "LDO_VSET"
    assert instance_params(der) == [("vset", "LDO_VSET")]
    one = ProjectConfig.from_dict({**CFG, "vset_param": "vout_sel"})
    der = derive(one, Netlist(DEMO, "tb/input.scs").scan("PMU_TOP", ports=one.ports))
    assert der.vset["declared"] is False and instance_params(der) == [("vset", 3)]


# --------------------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def qa(tmp_path_factory):
    """The QA-shaped fake project with three codes: plan, run, fit, verify, deliver."""
    from pmukit import fit as fitmod
    from pmukit.emit import deliver, verify_inputs
    from pmukit.runner import Runner
    from pmukit.verify import verify_project

    root = tmp_path_factory.mktemp("codes")
    pdir = root / "qa"
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": "qa", "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": [-40, 25, 125], "vset_codes": [3, 1, 5],
        "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                  "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                  "EN": "model", "TESTMODE": "ignore"},
        "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
        "care_up_to_hz": 1e9})
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    site = SiteConfig(engine="fake")
    der = derive(cfg, pins, site)
    pdir.mkdir(parents=True)
    cfg.save(pdir / "config.json")
    der.save(pdir / "derived.json")
    plan = compile_plan(cfg, der, nl, pins, site=site)
    led = Ledger(pdir / "runs.sqlite")
    plan.commit(led)
    dims = {"process": der.process["corners"], "temp_c": der.temps_c["points"],
            "vset": sorted(der.vset["codes"]),
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(pdir / "dataset", project=cfg.project, config_sha=cfg.sha(), dims=dims)
    runner = Runner(cfg.project, plan, led, site, dataset=ds, root=pdir / "runs",
                    backend=FakeBackend(site))
    summary = runner.run_all()
    notes = [n for rep in runner.reports.values() for n in rep["notes"]]
    result = fitmod.fit_project(ds, der)
    jsonio.write(pdir / "fit.json", result.to_dict())
    ver = verify_project("qa", result, ds, der, root=pdir, hb=False)
    out = deliver("qa", root=root, fit=result, derived=der, stamp="t0", **verify_inputs(ver))
    ds.close()
    led.close()
    return {"cfg": cfg, "der": der, "plan": plan, "summary": summary, "notes": notes,
            "fit": result, "ver": ver, "out": out, "pdir": pdir}


def test_the_whole_fake_project_runs_with_three_codes(qa):
    s = qa["summary"]
    assert s["failed"] == 0 and s["done"] == s["planned"] == len(qa["plan"].runs())
    assert not [n for n in qa["notes"] if "already filled" in n], "a cell was written twice"
    # the check runs went to their own dataset, per code; the fitted one has no AC code axis
    main = Dataset.open(qa["pdir"] / "dataset")
    chk = Dataset.open(qa["pdir"] / CODE_CHECK_DIR)
    assert "vset" not in main.var_dims("ac_zout.VDD0P8_A")
    assert main.var_dims("dc_load.VDD0P8_A")[:3] == ("process", "temp_c", "vset")
    assert "vset" in chk.var_dims("ac_zout.VDD0P8_A")
    assert set(chk.variables()) <= {f"{o}.{r}" for o in ("ac_zout", "ac_psrr")
                                    for r in qa["der"].rails}
    assert chk.coverage("ac_psrr.VDD0P8_A")["filled"] == 2 * 2      # {1, 5} x {-40, 125}


def test_the_fit_has_small_signal_once_and_dc_per_code(qa):
    fits = list(qa["fit"])
    z = [bf for bf in fits if bf.port == "VDD0P8_A" and bf.block == "zout"]
    assert z and all("vset" not in bf.cell and not bf.missing for bf in z)
    dc = {bf.cell.get("vset") for bf in fits if bf.port == "VDD0P8_A" and bf.block == "dc"
          and not bf.missing}
    assert dc == {1, 3, 5}


def test_the_deliverable_reports_the_check_and_follows_the_code_variable(qa):
    out = qa["out"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Output code: other codes" in report
    assert "Small-signal (Zout, PSRR, noise) is from code 3" in report
    grades = jsonio.read(out / "grades.json")
    cc = grades["code_check"]
    assert cc["codes"] == [1, 5] and cc["grade"] == "ok"      # the fake DUT barely moves
    assert all(r["grade"] == "ok" for r in cc["rows"])
    use = jsonio.read(out / "interface.json")
    assert use["instance_line"].endswith(" vset=VSET")        # the fixture declares VSET
    scs = next(out.glob("*.scs")).read_text(encoding="utf-8")
    assert "vset=VSET" in scs
    va = next(out.glob("*.va")).read_text(encoding="utf-8")
    assert "snaps to the nearest" in va and "from code 3 at every code" in va


def test_the_model_screen_says_it_in_one_line(qa):
    """No new panel: one line in the Valid-range box the Model screen already has."""
    from pmukit import server
    s = server.Api(root=qa["pdir"].parent).model_summary("qa")
    line = s["valid"]["code check"]
    assert line.startswith("small-signal from code 3; codes 1, 5 change the DC output only")
    assert "check at codes 1, 5: ok" in line
