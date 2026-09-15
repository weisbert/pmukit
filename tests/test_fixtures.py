"""Simulator-free checks on the M3 ground-truth fixtures.

Everything here runs on a machine with no Spectre and no ssh: it reads the committed
netlists and re-runs the converter in memory.  The one test that needs the VM is
marked `vm` and skips unless PMUKIT_VM_TESTS=1 is set.

The real simulator evidence lives in the two acceptance harnesses, which are run by
hand against the VM:
    tests/fixtures/pmu_demo/acceptance.py
    tests/fixtures/ldo_gt/acceptance.py
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PMU = REPO / "tests" / "fixtures" / "pmu_demo"
GT = REPO / "tests" / "fixtures" / "ldo_gt"

PMU_INST = "PMU_TOP"
PMU_MASTER = "pmu_demo"
PDK_FILES = ["toplevel.scs", "rc.scs"]
CONVENTION = {"IL_": "isource", "VB_": "vsource", "VS_": "vsource", "VEN_": "vsource"}
ROLELESS_PIN = "TESTMODE"

LDO_NAMES = [
    "ldo_gt", "ldo_v1_nmos", "ldo_v2_capless", "ldo_v3_miller", "ldo_v4_ffpsrr",
    "ldo_v5_spur", "ldo_v6_spur2", "ldo_v7_esl", "ldo_v8_dlc", "ldo_v9_vldo",
    "ldo_v10_3lc", "ldo_classab", "ldo_pzmig", "ldo_qbow", "ldo_swbleed",
]
EXTRA_SCS = ["isrc_gt", "nmos_lv", "pmos_lv"]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def code_lines(text: str):
    """Yield (lineno, statement, lang) -- `//` comments and blank lines removed.

    `lang` tracks `simulator lang=spice|spectre` so a test can tell a SPICE dot-card
    that is legitimately inside a lang=spice block from one that leaked out of it.
    Checking only code lines also keeps the prose in the headers (which talks about
    `level=8` and `section=`) from tripping the assertions.
    """
    lang = "spectre"
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"simulator\s+lang=(\w+)", line)
        if m:
            lang = m.group(1)
            continue
        yield no, line, lang


def code_text(path: pathlib.Path) -> str:
    return "\n".join(line for _, line, _ in code_lines(path.read_text(encoding="utf-8")))


def assert_lf(path: pathlib.Path):
    data = path.read_bytes()
    assert b"\r" not in data, f"{path.relative_to(REPO)} contains CR -- must be LF only"


def pmu_text() -> str:
    return (PMU / "input.scs").read_text(encoding="utf-8")


def pmu_ports() -> list[str]:
    """Pin NAMES, from the subcircuit definition."""
    m = re.search(rf"^subckt\s+{PMU_MASTER}\s+\(([^)]*)\)", pmu_text(), re.M)
    assert m, f"no 'subckt {PMU_MASTER} (...)' definition in input.scs"
    return m.group(1).split()


def pmu_nets() -> list[str]:
    """Nets the testbench connects those pins to, in the same order."""
    m = re.search(rf"^{PMU_INST}\s+\(([^)]*)\)\s+{PMU_MASTER}\s*$", pmu_text(), re.M)
    assert m, f"no '{PMU_INST} (...) {PMU_MASTER}' instance line in input.scs"
    return m.group(1).split()


def pmu_pins() -> dict[str, str]:
    """{pin name: connected net}."""
    ports, nets = pmu_ports(), pmu_nets()
    assert len(ports) == len(nets), \
        f"{PMU_INST} connects {len(nets)} nets to a {len(ports)}-pin subcircuit"
    return dict(zip(ports, nets))


def convention_sources() -> dict[str, tuple[str, str, str, str]]:
    """{pin: (prefix, source master, first node, dc value)} from the `<PREFIX><pin>` lines."""
    out = {}
    pat = re.compile(r"^(IL_|VB_|VS_|VEN_)(\w+)\s+\(([^)]*)\)\s+(\w+source)\b(.*)$", re.M)
    for prefix, pin, nodes, master, rest in pat.findall(pmu_text()):
        dc = re.search(r"\bdc=(\S+)", rest)
        out[pin] = (prefix, master, nodes.split()[0], dc.group(1) if dc else "")
    return out


def ground_pins() -> set[str]:
    """Pins the testbench wires straight to global 0 -- that is what a ground IS here.

    The contract says grounds are read from the wiring, not from a source prefix, and
    pmukit/netlist.py implements that as `net in ("0", "gnd!", "gnd")`.
    """
    return {pin for pin, net in pmu_pins().items() if net in ("0", "gnd", "gnd!")}


# --------------------------------------------------------------------------- #
# pmu_demo fixture
# --------------------------------------------------------------------------- #
def test_pmu_files_exist_and_are_lf():
    for p in [PMU / "input.scs"] + [PMU / "pdk" / f for f in PDK_FILES] + \
             [PMU / "pdk" / "models_bsim3.scs"]:
        assert p.is_file(), f"missing {p.relative_to(REPO)}"
        assert_lf(p)


def test_pmu_has_one_cornered_include_per_pdk_file():
    lines = re.findall(r'^include\s+"([^"]+)"\s+section=(\w+)\s*$', pmu_text(), re.M)
    assert len(lines) == len(PDK_FILES), \
        f"expected {len(PDK_FILES)} cornered includes, found {lines}"
    for pdk in PDK_FILES:
        hits = [s for f, s in lines if f.endswith(pdk)]
        assert len(hits) == 1, f"{pdk}: expected exactly one section= include, got {hits}"
    # and no OTHER statement may carry a section=, or the corner rewriter has two masters
    assert len(re.findall(r"\bsection=", code_text(PMU / "input.scs"))) == len(PDK_FILES)


def test_pmu_has_a_single_vset_parameter_line():
    hits = re.findall(r"^parameters\s+VSET=(\S+)\s*$", pmu_text(), re.M)
    assert hits == ["3"], f"expected one 'parameters VSET=3' line, found {hits}"


def test_pmu_pdk_sections_are_defined():
    top = (PMU / "pdk" / "toplevel.scs").read_text(encoding="utf-8")
    rc = (PMU / "pdk" / "rc.scs").read_text(encoding="utf-8")
    for name in ("tt", "ss", "ff"):
        assert re.search(rf"^section {name}$", top, re.M), f"toplevel.scs has no section {name}"
        assert re.search(rf"^endsection {name}$", top, re.M)
    for name in ("typ", "ss", "ff"):
        assert re.search(rf"^section {name}$", rc, re.M), f"rc.scs has no section {name}"
        assert re.search(rf"^endsection {name}$", rc, re.M)


def test_pmu_pdk_uses_bsim3_level_49():
    models = code_text(PMU / "pdk" / "models_bsim3.scs")
    assert re.search(r"\blevel=49\b", models), "BSIM3 cards must be Spectre level=49"
    assert not re.search(r"\blevel\s*=\s*8\b", models), \
        "level=8 is ngspice BSIM3; Spectre level 8 is a generic mos8 that rejects the card"


def test_pmu_every_convention_source_matches_its_pin_and_type():
    pins = pmu_pins()
    for pin, (prefix, master, node, dc) in convention_sources().items():
        assert pin in pins, f"{prefix}{pin} names a net that is not a {PMU_INST} pin"
        assert node == pins[pin], \
            f"{prefix}{pin} must attach to the net on pin {pin} ({pins[pin]}), found {node}"
        assert master == CONVENTION[prefix], \
            f"{prefix}{pin} must be a {CONVENTION[prefix]}, found {master}"
        assert dc, f"{prefix}{pin} carries no dc= value, so its role value is unreadable"


def test_pmu_every_pin_is_accounted_for():
    pins = pmu_pins()
    sourced = set(convention_sources())
    grounds = ground_pins()
    unaccounted = [p for p in pins if p not in sourced and p not in grounds and p != ROLELESS_PIN]
    assert not unaccounted, f"pins with no role and no ground tie: {unaccounted}"
    # ...and the role-less pin really is role-less, which is the point of having it
    assert ROLELESS_PIN in pins
    assert ROLELESS_PIN not in sourced, f"{ROLELESS_PIN} must NOT have a convention source"
    assert ROLELESS_PIN not in grounds


def test_pmu_roles_are_the_ones_the_demo_config_expects():
    conv = convention_sources()
    by_prefix: dict[str, list[str]] = {}
    for pin, (prefix, *_rest) in conv.items():
        by_prefix.setdefault(prefix, []).append(pin)
    assert by_prefix["VS_"] == ["VDDA_1V0"]
    assert by_prefix["VEN_"] == ["EN"]
    assert sorted(by_prefix["IL_"]) == ["VDD0P8_A", "VDD0P8_B", "VDD0P8_C"]
    assert sorted(by_prefix["VB_"]) == ["IB_POLY", "IB_PTAT"]
    assert sorted(ground_pins()) == ["AGND", "VSS_A", "VSS_B"]


def test_pmu_grounds_are_separate_nets_inside_the_subcircuit():
    """Rail A must return to VSS_A and rail B to VSS_B -- not both to one net."""
    body = pmu_text().split(f"subckt {PMU_MASTER}", 1)[1].split("\nends ", 1)[0]
    for gnd in ("VSS_A", "VSS_B", "AGND"):
        assert re.search(rf"\b{gnd}\b", body), f"{gnd} is not used inside the subcircuit"
    assert re.search(r"^\s*R2a \(fba VSS_A\)", body, re.M), "rail A divider must return to VSS_A"
    assert re.search(r"^\s*R2b \(fbb VSS_B\)", body, re.M), "rail B divider must return to VSS_B"


def test_pmu_has_analyses_for_the_stripper_to_remove():
    text = pmu_text()
    assert re.search(r"^\w+\s+dc\b", text, re.M), "fixture should ship a dc analysis"
    assert re.search(r"^save\s+\S+", text, re.M), "fixture should ship at least one save"


# --------------------------------------------------------------------------- #
# converted ground-truth library
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", LDO_NAMES + EXTRA_SCS)
def test_converted_scs_exists_and_is_clean(name):
    p = GT / f"{name}.scs"
    assert p.is_file(), f"missing {p.relative_to(REPO)} -- run tools/gt_convert.py"
    assert_lf(p)
    for no, line, lang in code_lines(p.read_text(encoding="utf-8")):
        assert not re.search(r"\blevel\s*=\s*8\b", line, re.I), \
            f"{name}.scs:{no} still has ngspice level=8: {line}"
        assert "{" not in line and "}" not in line, \
            f"{name}.scs:{no} still has a {{param}} brace: {line}"
        if lang != "spice":
            assert not line.startswith("."), \
                f"{name}.scs:{no} is a SPICE dot-card outside a lang=spice block: {line}"


def test_all_fifteen_ldo_subcircuits_are_present():
    found = []
    for name in LDO_NAMES:
        text = (GT / f"{name}.scs").read_text(encoding="utf-8")
        found += re.findall(r"^subckt\s+(\S+)\s*\(", text, re.M)
    assert sorted(found) == sorted(LDO_NAMES)


def test_gt_convert_check_reports_nothing_untranslated():
    r = subprocess.run([sys.executable, str(REPO / "tools" / "gt_convert.py"), "--check"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, f"gt_convert --check failed:\n{r.stdout}\n{r.stderr}"
    assert "0 untranslated constructs" in r.stdout, r.stdout


def test_committed_scs_match_a_fresh_conversion():
    """The .scs files are generated; if they drift from the converter, regenerate them."""
    sys.path.insert(0, str(REPO / "tools"))
    import gt_convert                                            # noqa: PLC0415

    src = GT / "ngspice"
    for s in sorted(list(src.glob("*.lib")) + list(src.glob("*.mod"))):
        text, notes = gt_convert.convert_file(s)
        assert not [n for n in notes if n.kind == "UNTRANSLATED"], f"{s.name}: {notes}"
        dst = GT / gt_convert.out_name(s)
        assert dst.read_text(encoding="utf-8") == text, \
            f"{dst.name} is stale -- re-run `python tools/gt_convert.py`"


# --------------------------------------------------------------------------- #
# the one test that needs the simulator VM
# --------------------------------------------------------------------------- #
@pytest.mark.vm
@pytest.mark.skipif(os.environ.get("PMUKIT_VM_TESTS") != "1",
                    reason="needs the Spectre VM; set PMUKIT_VM_TESTS=1 to run")
def test_pmu_fixture_runs_on_the_vm(tmp_path):
    shutil = pytest.importorskip("shutil")
    assert shutil.which("ssh"), "no ssh on PATH"
    shutil.copytree(PMU / "pdk", tmp_path / "pdk")
    (tmp_path / "input.scs").write_text(pmu_text(), encoding="utf-8", newline="\n")
    r = subprocess.run(["bash", (REPO / "tools" / "vmrun.sh").as_posix(),
                        tmp_path.as_posix(), "pytest_pmu_demo"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "raw" / "dcOp.dc").is_file()
