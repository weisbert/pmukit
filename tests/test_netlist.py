"""The netlist convention (CONTRACTS.md 0a): roles come from source-name prefixes, nothing else.

Every fixture here is synthetic. The parser must recognise the convention, refuse to guess a role
it cannot read, and rewrite the four things pmukit varies: section=, VSET, temp and source values.
"""
import pytest

from pmukit.errors import PmuError
from pmukit.netlist import Netlist, parse_number

# A miniature PMU testbench: two rails on separate grounds, two biases on a third, an enable,
# and one deliberately role-less pin (TESTMODE) that must NOT be guessed.
DEMO = """\
simulator lang=spectre
parameters VSET=3 TRIM=1
include "pdk/toplevel.scs" section=tt
include "pdk/rc.scs" section=typ

subckt pmu_demo (vdda a b ptat poly en tm vssa vssb agnd)
    ma1 (a na vdda vdda) pmos w=40u l=0.5u
    ra1 (a fb1 ) resistor r=100k
    rb1 (fb1 vssa) resistor r=100k
    ca1 (a vssa) capacitor c=200p
    mb1 (b nb vdda vdda) pmos w=20u l=0.5u
    rb2 (b fb2) resistor r=80k
    rb3 (fb2 vssb) resistor r=80k
    mp1 (ptat np agnd agnd) nmos w=10u l=1u
    mp2 (poly np agnd agnd) nmos w=10u l=1u
    men (na en vdda vdda) pmos w=2u l=1u
    mtm (tm agnd agnd agnd) nmos w=1u l=1u
ends pmu_demo

PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B IB_PTAT IB_POLY EN TESTMODE 0 0 0) pmu_demo
VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0
IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u
IL_VDD0P8_B (VDD0P8_B 0) isource dc=2m
VB_IB_PTAT (IB_PTAT 0) vsource dc=0.4
VB_IB_POLY (IB_POLY 0) vsource dc=0.4
VEN_EN (EN 0) vsource dc=1.0
Rtm (TESTMODE 0) resistor r=1meg

dcOp dc
myac ac start=10 stop=1G dec=20
save VDD0P8_A VDD0P8_B
"""

# The same testbench with the PMU's grounds brought out to distinct top-level nets, so the
# split-ground path has something to resolve.
SPLITGND = DEMO.replace(
    "PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B IB_PTAT IB_POLY EN TESTMODE 0 0 0) pmu_demo",
    "PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B IB_PTAT IB_POLY EN TESTMODE 0 0 0) pmu_demo")


def nl(text=DEMO):
    return Netlist(text, "tb/input.scs")


# ----------------------------------------------------------------------------- numbers
@pytest.mark.parametrize("tok,want", [
    ("500u", 500e-6), ("2m", 2e-3), ("1.0", 1.0), ("1e-9", 1e-9),
    ("1MEG", 1e6), ("1M", 1e6), ("-3.5n", -3.5e-9), ("1k", 1e3),
])
def test_parse_number(tok, want):
    assert parse_number(tok) == pytest.approx(want)


def test_parse_number_gives_up_instead_of_guessing():
    # An expression or a parameter reference is "unknown", never silently 0.
    assert parse_number("VSET*0.1") is None
    assert parse_number("") is None


# ------------------------------------------------------------------------------ scanning
def test_roles_come_from_prefixes():
    t = nl().scan("PMU_TOP")
    assert t.pmu_master == "pmu_demo"
    roles = {p.name: p.role for p in t.pins.values()}
    assert roles["a"] == "rail" and roles["b"] == "rail"
    assert roles["ptat"] == "bias" and roles["poly"] == "bias"
    assert roles["vdda"] == "supply"
    assert roles["en"] == "en"
    assert roles["tm"] == "none"          # TESTMODE has a resistor, not a convention source


def test_source_dc_values_are_read():
    t = nl().scan("PMU_TOP")
    by = {p.name: p for p in t.pins.values()}
    assert by["a"].dc == pytest.approx(500e-6)     # the testbench's typical load
    assert by["b"].dc == pytest.approx(2e-3)
    assert by["vdda"].dc == pytest.approx(1.0)     # nominal supply
    assert by["ptat"].dc == pytest.approx(0.4)     # bias compliance voltage
    assert by["a"].src == "IL_VDD0P8_A" and by["a"].src_master == "isource"


def test_unclassified_pin_is_reported_never_guessed():
    t = nl().scan("PMU_TOP")
    bad = t.unclassified()
    assert [p.name for p in bad] == ["tm"]
    with pytest.raises(PmuError) as e:
        t.require_classified()
    msg = str(e.value)
    assert "tm" in msg
    assert "does not guess" in msg          # the Why says the mechanism
    assert "IL_tm" in msg                   # the Do gives a concrete next action
    assert "Where:" in msg


def test_ground_pins_are_the_ones_tied_to_zero():
    t = nl().scan("PMU_TOP")
    assert sorted(p.name for p in t.grounds()) == ["agnd", "vssa", "vssb"]


def test_split_grounds_are_read_from_the_wiring():
    """Each rail/bias attaches to the ground pin nearest to it inside the subcircuit."""
    t = nl(SPLITGND).scan("PMU_TOP")
    by = {p.name: p for p in t.pins.values()}
    assert by["a"].gnd == "vssa"            # ra1/rb1/ca1 tie rail a to vssa
    assert by["b"].gnd == "vssb"
    assert by["ptat"].gnd == "agnd"
    assert by["poly"].gnd == "agnd"
    assert "hops" in by["a"].gnd_from


def test_single_ground_needs_no_graph():
    text = DEMO.replace("(vdda a b ptat poly en tm vssa vssb agnd)",
                        "(vdda a b ptat poly en tm gnda)")
    text = text.replace("vssa", "gnda").replace("vssb", "gnda").replace("agnd", "gnda")
    text = text.replace("EN TESTMODE 0 0 0)", "EN TESTMODE 0)")
    t = Netlist(text).scan("PMU_TOP")
    assert all(p.gnd == "gnda" for p in t.pins.values() if p.role in ("rail", "bias"))
    assert all(p.gnd_from == "the only ground pin"
               for p in t.pins.values() if p.role in ("rail", "bias"))


def test_wrong_master_for_a_prefix_is_a_hard_error():
    bad = DEMO.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
                       "IL_VDD0P8_A (VDD0P8_A 0) vsource dc=0.8")
    with pytest.raises(PmuError) as e:
        Netlist(bad).scan("PMU_TOP")
    assert "isource" in str(e.value) and "IL_" in str(e.value)


@pytest.mark.parametrize("cap", ["Cdec (VDD0P8_A 0) capacitor c=1u",
                                 "Cdec (0 VDD0P8_A) capacitor c=1u",
                                 "Cdec (VDD0P8_A 0) mimcap_hd w=20u l=20u"])
def test_a_decap_on_a_rail_is_refused(cap):
    """The rail is characterized intrinsic; a bench decap would be fitted into Zout and then
    counted again in the system bench."""
    with pytest.raises(PmuError) as e:
        Netlist(DEMO.replace("dcOp dc", cap + "\ndcOp dc")).scan("PMU_TOP")
    assert "Cdec" in e.value.what and "VDD0P8_A" in e.value.what


def test_only_top_level_rail_loads_count():
    """The PMU's own output cap (ca1, inside the subckt) is the circuit, not a bench decap; a
    cap on a bias or supply net is not a rail load; anything else on a rail is only noted."""
    text = DEMO.replace("dcOp dc", "Cb (IB_PTAT 0) capacitor c=1p\n"
                                   "Rld (VDD0P8_B 0) resistor r=10k\ndcOp dc")
    t = Netlist(text).scan("PMU_TOP")
    assert any("Rld" in n and "VDD0P8_B" in n for n in t.notes)
    assert not any("ca1" in n or "Cb" in n for n in t.notes)


def test_missing_pmu_instance_lists_candidates():
    with pytest.raises(PmuError) as e:
        nl().scan("NOT_THERE")
    assert "PMU_TOP" in str(e.value)


def test_instance_and_subckt_port_count_must_agree():
    bad = DEMO.replace("EN TESTMODE 0 0 0) pmu_demo", "EN TESTMODE 0 0) pmu_demo")
    with pytest.raises(PmuError) as e:
        Netlist(bad).scan("PMU_TOP")
    assert "declares" in str(e.value)


def test_subckt_internal_name_cannot_shadow_a_top_level_source():
    """A device inside the DUT named like a convention source must not be matched."""
    text = DEMO.replace("    ma1 (a na vdda vdda) pmos w=40u l=0.5u",
                        "    IL_VDD0P8_A (a na vdda vdda) pmos w=40u l=0.5u")
    n = Netlist(text)
    n.set_dc("IL_VDD0P8_A", 1e-3)
    # the TOP-LEVEL isource is the one that moved; the subckt device is untouched
    assert "IL_VDD0P8_A (VDD0P8_A 0) isource dc=0.001" in n.render()
    assert "IL_VDD0P8_A (a na vdda vdda) pmos w=40u l=0.5u" in n.render()


def test_includes_and_parameters_are_reported():
    t = nl().scan("PMU_TOP")
    assert t.sections == {"pdk/toplevel.scs": "tt", "pdk/rc.scs": "typ"}
    assert t.params["VSET"] == "3" and t.params["TRIM"] == "1"
    assert len(t.analyses) == 2


def test_fates_from_the_config_are_stamped_on():
    t = nl().scan("PMU_TOP", ports={"a": "model", "b": "stub", "tm": "ignore"})
    by = {p.name: p for p in t.pins.values()}
    assert (by["a"].fate, by["b"].fate, by["tm"].fate) == ("model", "stub", "ignore")


# ----------------------------------------------------------------------------- rewriting
def test_set_section_produces_a_corner():
    n = nl()
    n.set_section("toplevel.scs", "ss")
    assert 'include "pdk/toplevel.scs" section=ss' in n.render()
    assert 'include "pdk/rc.scs" section=typ' in n.render()   # the other include is untouched
    assert n.recipe_edits()[0].startswith("~ ")
    assert "was:" in n.recipe_edits()[0] and "section=tt" in n.recipe_edits()[0]


def test_set_section_matches_a_long_pdk_path():
    text = DEMO.replace('include "pdk/toplevel.scs" section=tt',
                        'include "/very/long/pdk/path/toplevel.scs" section=tt')
    n = Netlist(text)
    n.set_section("toplevel.scs", "ff")
    assert 'section=ff' in n.render()


def test_set_section_without_a_section_line_says_what_is_available():
    text = DEMO.replace('include "pdk/toplevel.scs" section=tt', 'include "pdk/toplevel.scs"')
    with pytest.raises(PmuError) as e:
        Netlist(text).set_section("toplevel.scs", "ss")
    assert "rc.scs section=typ" in str(e.value)


def test_set_param_rewrites_vset_in_place():
    n = nl()
    n.set_param("VSET", 1)
    assert "parameters VSET=1 TRIM=1" in n.render()


def test_set_param_declares_a_missing_variable():
    n = Netlist("simulator lang=spectre\nR1 (a 0) resistor r=1\n")
    n.set_param("VSET", 2)
    # below `simulator lang=spectre`, never above it (ALPS / SPICE would read line 1 as a title)
    assert n.render().startswith("simulator lang=spectre\nparameters VSET=2\n")
    assert n.recipe_edits() == ["+ parameters VSET=2"]


def test_inserted_lines_stay_below_an_ade_header():
    ade = ("// Generated for: spectre\n// Design cell name: TB\n"
           "simulator lang=spectre\nglobal 0\nR1 (a 0) resistor r=1\n")
    n = Netlist(ade)
    n.set_param("VSET", 2)
    n.set_temperature(85)
    lines = n.render().splitlines()
    assert lines[:3] == ["// Generated for: spectre", "// Design cell name: TB",
                         "simulator lang=spectre"]
    assert {"parameters VSET=2", "pmukit_opts options temp=85"} <= set(lines[3:5])
    headerless = Netlist("// just a comment\nR1 (a 0) resistor r=1\n")
    headerless.set_param("VSET", 1)
    assert headerless.render().splitlines()[:2] == ["// just a comment", "parameters VSET=1"]


def test_appended_block_switches_back_to_spectre():
    n = Netlist("simulator lang=spectre\nR1 (a 0) resistor r=1\n"
                "simulator lang=spice\n.param x=1\n")
    n.append("acz ac start=1 stop=1G dec=10")
    tail = n.render().rstrip().splitlines()[-2:]
    assert tail == ["simulator lang=spectre", "acz ac start=1 stop=1G dec=10"]
    plain = Netlist("simulator lang=spectre\nR1 (a 0) resistor r=1\n")
    plain.append("acz ac start=1 stop=1G dec=10")
    assert plain.render().count("simulator lang=spectre") == 1


def test_set_temperature_replaces_and_adds():
    n = Netlist("simulator lang=spectre\nopt1 options temp=27 gmin=1e-13\n")
    n.set_temperature(125)
    assert "options temp=125 gmin=1e-13" in n.render()
    n2 = nl()
    n2.set_temperature(-40)
    assert n2.render().startswith("simulator lang=spectre\npmukit_opts options temp=-40\n")


def test_set_mag_and_dc_preserve_comments_and_indent():
    text = "simulator lang=spectre\n  IL_X (x 0) isource dc=1m  // typical load\n"
    n = Netlist(text)
    n.set_mag("IL_X", 1).set_dc("IL_X", 2e-3)
    out = n.render()
    assert "  IL_X (x 0) isource dc=0.002 mag=1  // typical load" in out


def test_set_pwl_drops_dc_and_mag():
    n = nl()
    n.set_pwl("IL_VDD0P8_A", "0 2e-6 1n 2e-6 1.001n 5e-4")
    line = [x for x in n.render().splitlines() if x.startswith("IL_VDD0P8_A")][0]
    assert "dc=" not in line and "mag=" not in line
    assert "type=pwl wave=[0 2e-6 1n 2e-6 1.001n 5e-4]" in line


def test_rewriting_an_absent_source_is_a_four_part_error():
    with pytest.raises(PmuError) as e:
        nl().set_mag("IL_NOPE", 1)
    assert "IL_NOPE" in str(e.value) and "Do   :" in str(e.value)


def test_continued_statements_survive_rewriting():
    text = ("simulator lang=spectre\n"
            "IL_X (x \\\n"
            "      0) isource dc=1m\n"
            "R1 (x 0) resistor r=1\n")
    n = Netlist(text)
    n.set_mag("IL_X", 1)
    out = n.render()
    assert "\\" not in out                       # collapsed to one clean line
    assert "IL_X (x 0) isource dc=1m mag=1" in out
    assert "R1 (x 0) resistor r=1" in out


def test_strip_analyses_comments_every_analysis():
    n = nl()
    n.strip_analyses()
    out = n.render()
    assert out.count("[pmukit stripped analysis]") == 2
    assert "\ndcOp dc" not in out and "\nmyac ac " not in out
    assert "PMU_TOP (" in out                    # instances are never touched
    assert sorted(e[:1] for e in n.recipe_edits()) == ["-", "-"]


def test_strip_does_not_touch_an_instance_whose_master_looks_like_an_analysis():
    n = Netlist("simulator lang=spectre\nXdc (a b) dc_block\n")
    n.strip_analyses()
    assert "Xdc (a b) dc_block" in n.render()


def test_multiline_analysis_is_fully_neutralised():
    text = ("simulator lang=spectre\n"
            "myac ac start=10 \\\n"
            "     stop=1G dec=20\n"
            "R1 (a 0) resistor r=1\n")
    n = Netlist(text)
    n.strip_analyses()
    for line in n.render().splitlines():
        if "stop=1G" in line or "myac ac" in line:
            assert line.startswith("// [pmukit stripped analysis]")
    assert "\\" not in n.render()                # no orphan continuation left live


# ------------------------------------------------------------------- inserting a role source
def test_insert_role_source_gives_a_roleless_pin_its_convention_source():
    n = nl()
    t = n.scan("PMU_TOP")
    tm = t.pins["tm"]
    name = n.insert_role_source(tm, "rail", dc=1e-4)
    assert name == "IL_tm"
    assert "IL_tm (TESTMODE 0) isource dc=0.0001" in n.render()
    assert tm.role == "rail" and tm.fate == "model"
    # and a re-scan now classifies it
    assert n.scan("PMU_TOP").pins["tm"].role == "rail"


def test_insert_role_source_rejects_an_unknown_role():
    n = nl()
    t = n.scan("PMU_TOP")
    with pytest.raises(PmuError):
        n.insert_role_source(t.pins["tm"], "clock", dc=1.0)


# ------------------------------------------------------------------------------ plumbing
def test_untouched_netlist_round_trips_and_is_lf():
    n = nl()
    assert n.render() == DEMO
    assert "\r" not in n.render()


def test_crlf_input_is_normalised():
    n = Netlist(DEMO.replace("\n", "\r\n"))
    assert "\r" not in n.render()


def test_sha_moves_only_when_the_text_moves():
    a, b = nl(), nl()
    assert a.sha() == b.sha()
    b.set_param("VSET", 0)
    assert a.sha() != b.sha()


def test_pin_table_dict_is_what_derive_consumes():
    d = nl().scan("PMU_TOP").to_dict()
    assert set(d["a"]) >= {"role", "net", "gnd", "src", "dc", "fate"}
    assert d["a"]["role"] == "rail" and d["a"]["net"] == "VDD0P8_A"


def test_write_is_lf_on_disk(tmp_path):
    p = nl().write(tmp_path / "out.scs")
    assert b"\r" not in p.read_bytes()


# ------------------------------------------- a convention source wired the other way round
REVERSED = """\
simulator lang=spectre
subckt p (out ib)
    r1 (out ib) resistor r=1
ends p
X1 (VOUT IBIAS) p
IL_VOUT (VOUT 0) isource dc=1m
VB_IBIAS (0 IBIAS) vsource dc=0.4
"""


def test_a_reversed_convention_source_still_classifies_the_pin():
    """`VB_x (0 <pin>)` is an easy thing to draw; failing to classify it would be worse."""
    t = Netlist(REVERSED).scan("X1")
    by = {p.name: p for p in t.pins.values()}
    assert by["ib"].role == "bias" and by["ib"].src == "VB_IBIAS"
    assert by["ib"].src_reversed is True
    assert by["out"].src_reversed is False
    assert any("wired (0 IBIAS)" in n for n in t.notes)
    assert any("polarity is inverted" in n for n in t.notes)


def test_a_source_wired_the_normal_way_round_wins():
    text = REVERSED.replace("VB_IBIAS (0 IBIAS) vsource dc=0.4",
                            "VB_IBIAS (IBIAS 0) vsource dc=0.4")
    t = Netlist(text).scan("X1")
    assert t.pins["ib"].src_reversed is False


def test_two_convention_sources_on_one_net_is_ambiguous():
    text = REVERSED + "VB_OTHER (IBIAS 0) vsource dc=0.5\n"
    text = text.replace("VB_IBIAS (0 IBIAS)", "VB_IBIAS (IBIAS 0)")
    with pytest.raises(PmuError) as e:
        Netlist(text).scan("X1")
    assert "more than one convention source" in str(e.value)
    assert "VB_IBIAS" in str(e.value) and "VB_OTHER" in str(e.value)


# --------------------------------------------- the simple corner must not over-reach
def _pdk(tmp_path, name, sections):
    d = tmp_path / "pdk"
    d.mkdir(exist_ok=True)
    body = "\n".join(f"section {s}\n  // {s}\nendsection {s}" for s in sections)
    (d / name).write_text(body + "\n", encoding="utf-8", newline="\n")
    return d / name


def test_section_names_are_read_from_the_included_file(tmp_path):
    _pdk(tmp_path, "toplevel.scs", ["tt", "ss", "ff"])
    nl = Netlist('simulator lang=spectre\ninclude "pdk/toplevel.scs" section=tt\n',
                 tmp_path / "input.scs")
    assert nl.section_names("pdk/toplevel.scs") == {"tt", "ss", "ff"}


def test_an_unreadable_include_reports_none_not_a_guess(tmp_path):
    nl = Netlist('simulator lang=spectre\ninclude "/nowhere/toplevel.scs" section=tt\n',
                 tmp_path / "input.scs")
    assert nl.section_names("/nowhere/toplevel.scs") is None


def test_a_simple_corner_skips_an_include_that_has_no_such_section(tmp_path):
    """The bug this catches: rewriting rc.scs to 'tt' made Spectre say 'No section found'."""
    _pdk(tmp_path, "toplevel.scs", ["tt", "ss", "ff"])
    _pdk(tmp_path, "rc.scs", ["typ", "ss", "ff"])
    text = ('simulator lang=spectre\n'
            'include "pdk/toplevel.scs" section=tt\n'
            'include "pdk/rc.scs" section=typ\n')
    nl = Netlist(text, tmp_path / "input.scs")
    notes = nl.set_section_all("tt")
    out = nl.render()
    assert 'include "pdk/toplevel.scs" section=tt' in out
    assert 'include "pdk/rc.scs" section=typ' in out          # untouched -- it has no 'tt'
    assert any("has no 'tt'" in n and "composite corner form" in n for n in notes)

    nl2 = Netlist(text, tmp_path / "input.scs")
    nl2.set_section_all("ss")                                  # both DO have 'ss'
    assert nl2.render().count("section=ss") == 2


def test_an_unverifiable_include_is_rewritten_but_says_so(tmp_path):
    nl = Netlist('simulator lang=spectre\ninclude "/nowhere/toplevel.scs" section=tt\n',
                 tmp_path / "input.scs")
    notes = nl.set_section_all("ss")
    assert "section=ss" in nl.render()                         # the contract's behaviour...
    assert any("not verified to exist" in n for n in notes)    # ...but never silently
