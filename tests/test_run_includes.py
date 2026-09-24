"""Relative includes in the run decks, and the corner rewrite that has to read them.

Every run's deck is written into its own run directory (`$WORK_ROOT/pmukit/<project>/runs/<id>/`),
far from where the user exported the netlist, and the project works on a COPY of that netlist.
So `include "pdk/rc.scs"` means the file next to the ORIGINAL netlist, and a run deck has to say
so with an absolute path -- QA found run decks still carrying `pdk/rc.scs` with no `pdk/` beside
them. A bare `include "toplevel.scs"` that is not next to the netlist is ADE's PDK include, found
at sim time through `-I $MODEL_ROOT/alps`, and must be left exactly as written.
"""
from __future__ import annotations

import pathlib
import shutil
import time

import pytest

from pmukit import jsonio, server
from pmukit.config import ProjectConfig, derive
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist, recorded_origin
from pmukit.plan import absolute_includes, compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "pmu_demo"

PORTS = {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model", "VDD0P8_C": "stub",
         "IB_PTAT": "model", "IB_POLY": "model", "EN": "model", "TESTMODE": "ignore"}


@pytest.fixture
def exported(tmp_path):
    """The demo bench exported to a directory of its own, and the project's COPY of it, the way
    the New screen leaves them (the copy in `<project>/netlists/`, source.json beside it)."""
    bench = tmp_path / "bench"
    shutil.copytree(FIXTURE_DIR, bench)
    copy = tmp_path / "data" / "p" / "netlists" / "input.scs"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(bench / "input.scs", copy)
    jsonio.write(copy.parent / "source.json", {"path": str(bench / "input.scs")})
    return bench, copy


def _copy_netlist(copy: pathlib.Path, text: str | None = None) -> Netlist:
    if text is not None:
        copy.write_text(text, encoding="utf-8", newline="\n")
    nl = Netlist.from_file(copy)
    nl.origin = recorded_origin(copy)
    return nl


def _plan(nl: Netlist, engine: str = "fake", corners=("tt", "ss")):
    cfg = ProjectConfig.from_dict({
        "project": "p", "netlist": nl.path, "pmu_inst": "PMU_TOP", "corners": list(corners),
        "temps_c": [25], "vset_codes": [3], "state_note": "", "ports": PORTS,
        "care_up_to_hz": 1e9})
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    site = SiteConfig(engine=engine)
    return compile_plan(cfg, derive(cfg, pins, site), nl, pins, site=site)


def _includes(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("include ",
                                                                           "ahdl_include "))]


# --------------------------------------------------------------------------- the rewrite
def test_the_copy_remembers_where_it_came_from(exported):
    bench, copy = exported
    assert recorded_origin(copy) == str(bench / "input.scs")
    assert recorded_origin(bench / "input.scs") == ""          # an original records nothing


def test_run_decks_carry_the_includes_absolute_against_the_original_directory(exported):
    bench, copy = exported
    plan = _plan(_copy_netlist(copy))
    top = (bench / "pdk" / "toplevel.scs").as_posix()
    rc = (bench / "pdk" / "rc.scs").as_posix()
    for pr in plan.runs():
        incs = _includes(pr.netlist_text)
        assert len(incs) == 2, incs
        assert incs[0].startswith(f'include "{top}" section=')
        assert incs[1].startswith(f'include "{rc}" section=')
        # the recipe shows the rewrite, with the line as the user wrote it
        assert f'include "{rc}"' in pr.run.recipe and 'was: include "pdk/rc.scs"' in pr.run.recipe
    # said once, on the Plan screen
    moved = [n for n in plan.notes if n.startswith("include pdk/rc.scs -> ")]
    assert len(moved) == 1 and rc in moved[0]
    assert len([n for n in plan.notes if n.startswith("include pdk/toplevel.scs -> ")]) == 1


def test_the_second_include_keeps_its_own_section_names(exported):
    """rc.scs declares typ/ss/ff: corner tt leaves it at typ, corner ss moves it to ss. QA saw it
    rewritten to `tt` because the COPY's directory has no pdk/ -- the corner rewrite has to read
    the file next to the ORIGINAL netlist."""
    bench, copy = exported
    plan = _plan(_copy_netlist(copy))
    rc = (bench / "pdk" / "rc.scs").as_posix()
    by_corner = {}
    for pr in plan.runs():
        by_corner.setdefault(pr.run.process, set()).add(_includes(pr.netlist_text)[1])
    assert by_corner["tt"] == {f'include "{rc}" section=typ'}
    assert by_corner["ss"] == {f'include "{rc}" section=ss'}
    assert any("pdk/rc.scs: left at section=typ" in n for n in plan.notes)
    assert not any("without being able to read the file" in n for n in plan.notes)


def test_without_the_origin_the_copy_alone_could_not_read_rc(exported):
    """The QA failure mode, pinned: the copy's directory has no pdk/, so the file cannot be
    read, the contract's blind rewrite happens and the note says so."""
    _bench, copy = exported
    bare = Netlist.from_file(copy)
    notes = bare.set_section_all("tt")
    assert 'include "pdk/rc.scs" section=tt' in bare.render()
    assert any("without being able to read the file" in n for n in notes)
    assert bare.absolutize_includes() == []                   # nothing found, nothing touched


def test_a_bare_pdk_include_and_an_absolute_one_are_left_alone(exported, tmp_path):
    bench, copy = exported
    va = bench / "models" / "foo.va"
    va.parent.mkdir()
    va.write_text("// verilog-a\n", encoding="utf-8", newline="\n")
    text = copy.read_text(encoding="utf-8").replace(
        'include "pdk/rc.scs" section=typ',
        'include "pdk/rc.scs" section=typ\n'
        'include "toplevel.scs" section=pre_Sim\n'          # ADE's bare PDK include (-I finds it)
        'include "/proj/pdk/models/extra.scs"\n'            # absolute: never touched
        'ahdl_include "models/foo.va"\n')
    plan = _plan(_copy_netlist(copy, text))
    incs = _includes(plan.runs()[0].netlist_text)
    assert 'include "toplevel.scs" section=pre_Sim' in incs
    assert 'include "/proj/pdk/models/extra.scs"' in incs
    assert f'ahdl_include "{va.as_posix()}"' in incs
    assert not any("toplevel.scs ->" in n and "pdk/" not in n for n in plan.notes)


def test_ades_three_includes_of_one_file_are_all_made_absolute(exported):
    """ADE writes one `include "toplevel.scs" section=<x>` per Model Library row. All of them
    must resolve in the run dir; only the FIRST is the process corner."""
    bench, copy = exported
    text = copy.read_text(encoding="utf-8").replace(
        'include "pdk/toplevel.scs" section=tt',
        'include "pdk/toplevel.scs" section=tt\n'
        'include "pdk/toplevel.scs" section=ff\n'
        'include "pdk/toplevel.scs" section=ss')
    plan = _plan(_copy_netlist(copy, text), corners=("ss",))
    top = (bench / "pdk" / "toplevel.scs").as_posix()
    incs = _includes(plan.runs()[0].netlist_text)
    assert incs[:3] == [f'include "{top}" section=ss', f'include "{top}" section=ff',
                        f'include "{top}" section=ss']
    assert not any('"pdk/toplevel.scs"' in i for i in incs)


def test_resume_still_collides_on_an_unchanged_plan(exported):
    """run_id is a content hash of the deck. Absolutizing changes the text -- deterministically,
    so a re-plan of the same install still finds every finished run."""
    _bench, copy = exported
    a = [r.run_id for r in _plan(_copy_netlist(copy)).runs()]
    b = [r.run_id for r in _plan(_copy_netlist(copy)).runs()]
    assert a == b and len(set(a)) == len(a)


def test_the_run_directory_deck_resolves_its_includes(exported, tmp_path):
    _bench, copy = exported
    plan = _plan(_copy_netlist(copy))
    led = Ledger(tmp_path / "runs.sqlite")
    plan.commit(led)
    Runner("p", plan, led, SiteConfig(engine="dry_run"), dataset=False,
           root=tmp_path / "runs").run_all()
    led.close()
    decks = sorted((tmp_path / "runs").glob("*/input.scs"))
    assert decks
    for deck in decks:
        for inc in _includes(deck.read_text(encoding="utf-8")):
            path = inc.split('"')[1]
            assert pathlib.Path(path).is_absolute() and pathlib.Path(path).is_file(), inc


# --------------------------------------------------------------------------- spectre_ssh
def test_a_run_shipped_to_another_host_keeps_relative_includes_and_ships_the_tree(exported):
    """spectre_ssh copies the run dir to the VM, which cannot see this machine's paths: the
    lines stay relative and the `pdk/` tree travels with the run (the runner's aux)."""
    bench, copy = exported
    assert absolute_includes(SiteConfig(engine="spectre_ssh")) is False
    assert absolute_includes(SiteConfig(engine="donau_alps")) is True
    assert absolute_includes(None) is True
    nl = _copy_netlist(copy)
    plan = _plan(nl, engine="spectre_ssh")
    assert _includes(plan.runs()[0].netlist_text)[1].startswith('include "pdk/rc.scs"')
    assert any(n.startswith("relative includes kept relative") for n in plan.notes)
    assert nl.include_trees() == [bench / "pdk"]


# --------------------------------------------------------------------------- through the server
def test_the_web_shell_plan_uses_the_original_directory(exported, tmp_path):
    bench, _copy = exported
    api = server.Api(root=tmp_path / "srv")
    api.new_project({"name": "p"})
    job = server.JOBS.get(api.load_netlist("p", {"path": str(bench / "input.scs")})["job"])
    deadline = time.time() + 60
    while job.status in ("queued", "running"):
        assert time.time() < deadline
        time.sleep(0.02)
    assert job.status == "done", job.error
    plan = server.Project("p", api.root).plan()
    rc = (bench / "pdk" / "rc.scs").as_posix()
    tt = [r for r in plan.runs() if r.run.process == "tt"]
    assert tt and all(f'include "{rc}" section=typ' in r.netlist_text for r in tt)
