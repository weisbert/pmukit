"""The CLI must be able to do what the screens do -- that is what makes the command echo honest.

UX_RULES: "GUI 能做的，CLI 都能做" and "`pmukit help <screen>` prints the same text as the help
panel". These tests drive the real entry point, in a temporary $PMUKIT_DATA, on a synthetic
netlist.
"""
import json
import pathlib
import subprocess
import sys

import pytest

from pmukit import cli, helptext
from pmukit.errors import PmuError

from .test_plan import DEMO

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "data"))
    nl = tmp_path / "tb" / "input.scs"
    nl.parent.mkdir(parents=True)
    nl.write_text(DEMO, encoding="utf-8", newline="\n")
    return tmp_path, nl


def run(argv, expect=0):
    rc = cli.main(argv)
    assert rc == expect, f"{argv} -> {rc}"
    return rc


def new_project(nl, name="demo_pmu"):
    run([name and "new", name, "--netlist", str(nl), "--pmu-inst", "PMU_TOP",
         "--corners", "tt,ss", "--temps=-40,25,125", "--vset", "3",
         "--care-up-to", "1e9", "--load", "a=5e-4,2e-6", "--load", "b=2e-3,5e-6"])


# --------------------------------------------------------------------------------- help
def test_help_prints_the_same_text_as_the_panel(capsys):
    run(["help", "plan"])
    printed = capsys.readouterr().out
    assert printed == helptext.render("plan")
    assert helptext.as_dict("plan")["text"] == printed


def test_every_screen_has_three_sentences_and_the_global_keys():
    for screen in helptext.SCREENS:
        h = helptext.HELP[screen]
        assert len(h["lines"]) == 3, screen
        text = helptext.render(screen)
        for key, _what in helptext.GLOBAL_KEYS:
            assert key in text, (screen, key)


def test_help_with_no_screen_lists_them(capsys):
    run(["help"])
    out = capsys.readouterr().out
    for s in helptext.SCREENS:
        assert s in out


def test_unknown_screen_does_not_raise():
    assert "Screens:" in helptext.render("nope")


# ------------------------------------------------------------------------------ new/pins
def test_new_reads_the_pin_roles_out_of_the_netlist(workspace, capsys):
    tmp, nl = workspace
    new_project(nl)
    out = capsys.readouterr().out
    assert "rail" in out and "bias" in out and "supply" in out
    assert "tm" in out and "have no role" in out        # reported, never guessed
    cfg = json.loads((tmp / "data" / "demo_pmu" / "config.json").read_text(encoding="utf-8"))
    assert cfg["ports"]["a"] == "model" and cfg["ports"]["tm"] == "ignore"
    assert cfg["my_load"]["a"] == {"on_a": 5e-4, "off_a": 2e-6, "switches": True}
    assert (tmp / "data" / "demo_pmu" / "derived.json").exists()


def test_negative_temperatures_work_without_an_equals_sign(workspace):
    """`--temps -40,25,125` must work, not just `--temps=-40,25,125`."""
    tmp, nl = workspace
    run(["new", "p2", "--netlist", str(nl), "--pmu-inst", "PMU_TOP",
         "--temps", "-40,25,125", "--corners", "tt"])
    cfg = json.loads((tmp / "data" / "p2" / "config.json").read_text(encoding="utf-8"))
    assert cfg["temps_c"] == [-40.0, 25.0, 125.0]


def test_load_without_both_currents_is_a_four_part_error(workspace, capsys):
    _tmp, nl = workspace
    # the CLI never tracebacks: a PmuError is rendered in four parts and exits 2
    rc = cli.main(["new", "p3", "--netlist", str(nl), "--pmu-inst", "PMU_TOP", "--load", "a=5e-4"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "on_a" in err
    for part in ("What :", "Why  :", "Do   :", "Where:"):
        assert part in err


def test_pins_round_trips_as_json(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["--json", "pins", "demo_pmu"])
    data = json.loads(capsys.readouterr().out)
    assert data["a"]["role"] == "rail" and data["a"]["gnd"] == "vssa"


# ----------------------------------------------------------------------------- list
def test_list_shows_the_step_the_project_stopped_at(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["--json", "list"])
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["project"] == "demo_pmu" and rows[0]["step"] == "planned"


def test_list_on_an_empty_root_says_how_to_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "nothing"))
    run(["list"])
    assert "pmukit new" in capsys.readouterr().out


# ----------------------------------------------------------------------------- config
def test_config_set_then_undo(workspace, capsys):
    tmp, nl = workspace
    new_project(nl)
    before = json.loads((tmp / "data" / "demo_pmu" / "config.json").read_text(encoding="utf-8"))
    run(["config", "demo_pmu", "--set", 'corners=["tt","ss","ff"]'])
    mid = json.loads((tmp / "data" / "demo_pmu" / "config.json").read_text(encoding="utf-8"))
    assert mid["corners"] == ["tt", "ss", "ff"]
    run(["config", "demo_pmu", "--undo"])
    after = json.loads((tmp / "data" / "demo_pmu" / "config.json").read_text(encoding="utf-8"))
    assert after["corners"] == before["corners"]


def test_config_derived_is_the_0b_document(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["config", "demo_pmu", "--derived"])
    d = json.loads(capsys.readouterr().out)
    assert d["freq"]["points_per_decade"] == 20
    assert d["loads"]["a"]["points_a"][0] == pytest.approx(2e-6)
    assert "provenance" in d["freq"]            # every derived field explains itself


# ------------------------------------------------------------------------------- plan
def test_plan_lists_groups_costs_and_consequences(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["plan", "demo_pmu"])
    out = capsys.readouterr().out
    assert "ac:VS_VDDA_1V0" in out and "CPU-hours estimated" in out
    assert "NOT RUN" not in out                  # nothing is unticked yet

    run(["plan", "demo_pmu", "--off", "noise:noise_v.a"])
    out = capsys.readouterr().out
    assert "OFF" in out and "NOT RUN: a noise" in out


def test_plan_submit_then_status(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["plan", "demo_pmu", "--submit"])
    assert "submitted to the ledger" in capsys.readouterr().out
    run(["--json", "status", "demo_pmu"])
    st = json.loads(capsys.readouterr().out)
    assert st["counts"]["planned"] > 0
    assert st["runs"][0]["analysis"] in ("ac", "dc_load", "dc_temp", "dc_iv", "noise",
                                         "tran_load_on", "tran_load_off", "tran_en")


def test_status_why_explains_one_run(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["plan", "demo_pmu", "--submit"])
    capsys.readouterr()
    run(["--json", "status", "demo_pmu"])
    rid = json.loads(capsys.readouterr().out)["runs"][0]["run_id"]
    run(["status", "demo_pmu", "--why", rid])
    assert "because" in capsys.readouterr().out.lower()


def test_plan_recipe_ends_with_the_submit_command(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    run(["--json", "plan", "demo_pmu"])
    capsys.readouterr()
    run(["plan", "demo_pmu", "--runs", "ac:VS_VDDA_1V0"])
    rid = capsys.readouterr().out.splitlines()[2].split()[0]
    run(["plan", "demo_pmu", "--recipe", rid])
    recipe = capsys.readouterr().out
    assert "[edits]" in recipe or "~ " in recipe
    assert "acz ac " in recipe
    assert recipe.strip().splitlines()[-1].startswith(("ssh ", "dsub ", "[dry_run]", "[fake]"))


# -------------------------------------------------------------------------- refusals
def test_unknown_project_lists_the_real_ones(workspace, capsys):
    _tmp, nl = workspace
    new_project(nl)
    assert cli.main(["pins", "nope"]) == 2
    assert "demo_pmu" in capsys.readouterr().err


def test_pmuerror_exits_2_and_prints_four_parts(workspace, capsys):
    _tmp, nl = workspace
    rc = cli.main(["--json", "pins", "nope"])
    assert rc == 2
    body = json.loads(capsys.readouterr().err)
    assert set(body["error"]) == {"what", "why", "do", "where"}


# ----------------------------------------------------------------- the installed entry point
def test_python_dash_m_pmukit_works(tmp_path):
    """`python -m pmukit` is what the installed launcher execs -- the box depends on it."""
    r = subprocess.run([sys.executable, "-m", "pmukit", "help", "run"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Run -- the ledger" in r.stdout


def test_every_screen_has_a_command(capsys):
    """The command echo is only honest if each screen's command actually parses."""
    parser = cli.build_parser()
    verbs = set(parser._subparsers._group_actions[0].choices)      # noqa: SLF001
    for screen in helptext.SCREENS:
        cmd = helptext.HELP[screen]["cli"]
        if not cmd:
            continue
        assert cmd.split()[1] in verbs, (screen, cmd)
