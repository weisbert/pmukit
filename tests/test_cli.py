"""The CLI must be able to do what the screens do -- that is what makes the command echo honest.

UX_RULES: "GUI 能做的，CLI 都能做" and "`pmukit help <screen>` prints the same text as the help
panel". These tests drive the real entry point, in a temporary $PMUKIT_DATA, on a synthetic
netlist.
"""
import json
import pathlib
import re
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


# ------------------------------------------------------- `pmukit check`, the pre-flight
TEMPLATE = REPO / "tools" / "templates" / "tb_convention.scs"


def test_check_accepts_the_shipped_template(capsys):
    """The template we hand the user must itself pass the checker."""
    assert cli.main(["check", str(TEMPLATE), "--pmu-inst", "PMU_TOP"]) == 0
    out = capsys.readouterr().out
    assert "Convention OK" in out
    assert "rail" in out and "bias" in out and "supply" in out


def test_check_names_the_unclassifiable_pin_and_how_to_fix_it(capsys):
    cli.main(["check", str(TEMPLATE), "--pmu-inst", "PMU_TOP"])
    out = capsys.readouterr().out
    assert "no role: TESTMODE" in out
    assert "IL_<pin>  isource" in out and "VB_<pin>  vsource" in out


def test_check_flags_a_missing_section(tmp_path, capsys):
    bad = tmp_path / "nosection.scs"
    # strip section= from EVERY include -- one surviving section is enough to make corners
    text = re.sub(r"\s+section=\S+", "", TEMPLATE.read_text(encoding="utf-8"))
    bad.write_text(text, encoding="utf-8", newline="\n")
    assert cli.main(["check", str(bad), "--pmu-inst", "PMU_TOP"]) == 1
    assert "cannot generate process corners" in capsys.readouterr().out


def test_check_refuses_a_rail_driven_by_a_voltage_source(tmp_path, capsys):
    bad = tmp_path / "wrongmaster.scs"
    bad.write_text(TEMPLATE.read_text(encoding="utf-8").replace(
        "IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
        "IL_VDD0P8_A (VDD0P8_A 0) vsource dc=0.8"), encoding="utf-8", newline="\n")
    assert cli.main(["check", str(bad), "--pmu-inst", "PMU_TOP"]) == 2
    err = capsys.readouterr().err
    assert "must be an isource" in err and "Do   :" in err


def test_check_json_is_machine_readable(capsys):
    cli.main(["--json", "check", str(TEMPLATE), "--pmu-inst", "PMU_TOP"])
    d = json.loads(capsys.readouterr().out)
    assert d["problems"] == [] and d["unclassified"] == ["TESTMODE"]
    assert d["sections"]["pdk/toplevel.scs"] == "tt"


def test_check_looks_for_the_named_code_variable(capsys):
    """The template declares VSET; asking for another name lists what IS declared."""
    assert cli.main(["check", str(TEMPLATE), "--pmu-inst", "PMU_TOP",
                     "--vset-param", "vout_sel"]) == 1
    out = capsys.readouterr().out
    assert "parameters vout_sel=<n>" in out and "declares: VSET" in out


def test_duplicate_ground_pins_do_not_collapse(capsys):
    """Three grounds all tied to 0 must stay three pins, not one."""
    cli.main(["--json", "check", str(TEMPLATE), "--pmu-inst", "PMU_TOP"])
    pins = json.loads(capsys.readouterr().out)["pins"]
    grounds = [k for k, v in pins.items() if v["is_ground"]]
    assert len(grounds) == 3, grounds


# ------------------------------------------------- `pmukit site`: which simulator this machine has
def test_site_shows_the_engine_and_how_to_change_it(workspace, capsys):
    run(["site"])
    out = capsys.readouterr().out
    assert "engine" in out and "spectre_ssh" in out
    assert "donau_alps" in out                       # the box's engine is named
    assert "PMUKIT_ENGINE" in out                    # and the env override


def test_site_persists_a_change(workspace, capsys):
    tmp, _nl = workspace
    run(["site", "--engine", "donau_alps", "--queue", "short", "--cpus", "8"])
    capsys.readouterr()
    run(["--json", "site"])
    d = json.loads(capsys.readouterr().out)
    assert d["engine"] == "donau_alps" and d["queue"] == "short" and d["cpus"] == 8
    assert (tmp / "data" / "site.json").exists()


def test_site_is_not_part_of_a_project(workspace, capsys):
    """Which simulator this machine can reach is a property of the MACHINE, not the project."""
    tmp, nl = workspace
    new_project(nl)
    capsys.readouterr()
    cfg = json.loads((tmp / "data" / "demo_pmu" / "config.json").read_text(encoding="utf-8"))
    assert "engine" not in cfg and "queue" not in cfg
    der = json.loads((tmp / "data" / "demo_pmu" / "derived.json").read_text(encoding="utf-8"))
    assert "site" in der                             # recorded for provenance...
    # ...but deliberately excluded from the hash, so the same characterization hashes the same
    # on the desk and in the red zone
    from pmukit.config import DerivedConfig
    a = DerivedConfig.from_dict({**der, "site": {"engine": "spectre_ssh"}})
    b = DerivedConfig.from_dict({**der, "site": {"engine": "donau_alps"}})
    assert a.sha() == b.sha()


# ------------------------------------------------- `pmukit open`: the page opens on the project
def test_open_hands_the_project_to_the_server(workspace, monkeypatch):
    """`pmukit open p` starts the web shell with p as the page's initial project (the server
    prints the URL with ?project=p and serves it as `initial_project` for a bare URL)."""
    from pmukit import server
    tmp, _nl = workspace
    (tmp / "data" / "fresh_pmu").mkdir(parents=True)      # made from Home: no config.json yet
    seen = {}
    monkeypatch.setattr(server, "main", lambda **kw: seen.update(kw) or 0)
    run(["open", "fresh_pmu", "--port", "9123"])
    assert seen["project"] == "fresh_pmu"
    assert seen["open_browser"] is True and seen["demo"] is False and seen["port"] == 9123


def test_open_refuses_a_project_that_is_not_there(workspace, monkeypatch, capsys):
    from pmukit import server
    tmp, _nl = workspace
    (tmp / "data" / "other_pmu").mkdir(parents=True)
    monkeypatch.setattr(server, "main", lambda **kw: pytest.fail("the server must not start"))
    assert cli.main(["--json", "open", "nope"]) == 2
    err = json.loads(capsys.readouterr().err)["error"]
    assert set(err) == {"what", "why", "do", "where"}
    assert "nope" in err["what"] and "other_pmu" in " ".join(err["do"])
