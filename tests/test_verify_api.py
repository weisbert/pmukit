"""`pmukit.verify.verify_project` -- the one entry point the CLI and the web shell both call.

Built on the `fake` backend, so it runs on any machine with no simulator at all: that is the
point of `hb=False`, and the Model screen has to stay useful on a laptop.
"""
import json
import pathlib

import pytest

import pmukit.verify as V
from pmukit import fit as fitmod
from pmukit import jsonio
from pmukit.backends.fake import FakeBackend
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.errors import PmuError
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"
PROJECT = "verify_demo"


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """A complete project directory: config, derived, dataset, fit -- nothing simulated."""
    data_root = tmp_path_factory.mktemp("data")
    d = data_root / PROJECT
    d.mkdir()
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": PROJECT, "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": [25], "vset_codes": [3],
        "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                  "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                  "EN": "model", "TESTMODE": "ignore"},
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
    Runner(cfg, plan, led, site, dataset=ds, root=d / "runs",
           backend=FakeBackend(site)).run_all()
    result = fitmod.fit_project(ds, der)
    jsonio.write(d / "fit.json", result.to_dict())
    ds.close()
    led.close()
    return {"root": data_root, "dir": d, "fit": result, "derived": der}


# --------------------------------------------------------------------------- the contract
def test_the_documented_signature_is_the_one_the_cli_calls(project):
    """`pmukit/cli.py:cmd_verify` calls `verify_project(project, root=d)` where `d` is the
    PROJECT directory."""
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    assert set(out) >= {"grades", "rollup", "hb_check", "envelope", "not_run", "notes"}


def test_the_data_root_spelling_works_too(project):
    """`emit.deliver()` and the web shell pass the DATA ROOT instead.  Guessing wrong would
    write verify.json into a directory that is not the project's, so both are accepted."""
    out = V.verify_project(PROJECT, root=project["root"], hb=False)
    assert out["project"] == PROJECT and out["grades"]


def test_the_web_shells_positional_call_works(project):
    """`pmukit/server.py` calls `fn(pr.name, fit, pr.dataset(), pr.derived())` -- four
    positional arguments.  It is not the documented spelling, but it is a real caller."""
    ds = Dataset.open(project["dir"] / "dataset")
    try:
        out = V.verify_project(PROJECT, project["fit"], ds, project["derived"], hb=False)
    finally:
        ds.close()
    assert out["grades"] and out["rollup"]


def test_a_project_with_no_fit_says_so_in_four_parts(tmp_path):
    with pytest.raises(PmuError) as exc:
        V.verify_project("nothing_here", root=tmp_path, hb=False)
    err = exc.value.to_dict()["error"]
    assert err["what"] and err["why"] and err["do"] and err["where"]
    assert "fit" in err["do"][0]


# --------------------------------------------------------------------------- the payload
def test_the_result_is_strict_json_with_no_nan(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    text = json.dumps(out, allow_nan=False)                # raises on NaN / Infinity
    json.loads(text, parse_constant=lambda c: pytest.fail(f"non-strict JSON constant {c}"))


def test_nan_becomes_null_and_infinity_keeps_its_name():
    assert V.json_safe({"a": float("nan")}) == {"a": None}
    assert V.json_safe([float("inf"), float("-inf")]) == ["inf", "-inf"]
    assert V.json_safe({"n": 1, "s": "x", "b": True, "z": None}) == \
        {"n": 1, "s": "x", "b": True, "z": None}


def test_every_graded_cell_appears_in_the_rollup(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    cells = {(g["port"], g["corner"]) for g in out["grades"]}
    rolled = {(p, c) for p, cs in out["rollup"].items() for c in cs}
    assert cells == rolled


def test_the_envelope_is_the_one_the_deliverable_will_carry(project):
    """Not a second copy of "what was characterized": `pmukit.emit` owns that definition, and
    the Model screen and the deliverable are not allowed to disagree about the valid range."""
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    env = out["envelope"]
    assert env["corners"] == ["tt"] and env["vset_codes"] == [3]
    assert set(env["load_a"]) == {"VDD0P8_A", "VDD0P8_B"}
    assert env["freq_max_hz"] == pytest.approx(1e9)
    assert env["ls_default_on"] == [], "nothing defaults on until the HB check has run"
    assert any("not signed off" in n for n in env["notes"])


def test_the_acceptance_limits_travel_with_the_result(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    joined = "\n".join(out["notes"])
    assert "|Zout| dB RMS" in joined and "ADDING COVERAGE" in joined


# --------------------------------------------------------------------------- no simulator
def test_hb_false_needs_no_simulator_and_signs_nothing_off(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    assert out["hb_check"]["status"] == "not_run"
    assert out["ls_default_on"] == []
    assert "stays OFF" in " ".join(out["hb_check"]["notes"])


def test_the_system_bench_is_off_by_default(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    assert "system_check" not in out


def test_render_produces_plain_text_for_a_machine_with_no_screenshots(project):
    out = V.verify_project(PROJECT, root=project["dir"], hb=False)
    text = V.render(out)
    assert "verification" in text and "worst overall:" in text
    assert "Acceptance limits" in text
    assert "\x1b[" not in text, "the red zone reads this in a terminal with no colour"


def test_the_heavy_submodules_are_imported_lazily():
    """A machine with no simulator must still be able to import `pmukit.verify` and grade a
    fit; only `grades` is eager."""
    import importlib
    import sys
    for name in ("pmukit.verify", "pmukit.verify.hb", "pmukit.verify.system",
                 "pmukit.verify.regression"):
        sys.modules.pop(name, None)
    mod = importlib.import_module("pmukit.verify")
    assert "pmukit.verify.hb" not in sys.modules
    assert mod.grades is not None
    assert mod.hb is not None                       # __getattr__ pulls it in on demand
    assert "pmukit.verify.hb" in sys.modules
