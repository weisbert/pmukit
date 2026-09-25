"""What a REAL ADE testbench looks like to the scan, as opposed to the tidy demo fixture.

Every name here is invented; only the STRUCTURE is what a production PMU bench showed:

* the supply source returns to an analog-ground net that several PMU ground pins share
  (`VS_<sup> (<sup> <gnda>)`), so that net must read as a GROUND, never as the supply;
* the bias sources return to a second ground net that the bench also ties a handful of digital
  control inputs to (logic low) -- they are control pins passed through, not biases and not grounds;
* a third, per-LDO ground net carries the return of one rail and one PMU ground pin;
* three `include` lines of one PDK file: the process-corner row plus fixed model-library rows;
* the output code is a `*_VSET` parameter other parameters are expressions of;
* zero-impedance placeholders (`L=0`, `L=1f`, `R=0`) that are one node, not elements.
"""
from __future__ import annotations

import pathlib

import pytest

from pmukit import server
from pmukit.errors import PmuError
from pmukit.netlist import Netlist, code_variable, looks_like_corner, param_followers

INCLUDES_CORNER_FIRST = """\
include "models/pdk_top.scs" section=TOP_TT_X
include "models/pdk_top.scs" section=pre_Sim
include "models/pdk_top.scs" section=Noise_Worst
"""

BENCH = r"""// synthetic bench, structure of a production PMU testbench
simulator lang=spectre
global 0
parameters CORE_VSET=10 LVL_P=0.0125*CORE_VSET+0.7 LVL_Q=0.0125*CORE_VSET+0.7 CAL_ON=1 \
    AUX_LVL=0.5*CAL_ON-1 NEXT_LVL=2*LVL_P
{includes}
subckt ldo_blk vin vout vss en trim\<1\> trim\<0\> ib
    mp (vout g vin vin) pch_lvt w=40u l=0.5u
    mn (g en vss vss) nch_lvt w=2u l=1u
    mt1 (g t1 vss vss) nch_lvt w=1u l=1u
    rt1 (t1 trim\<1\>) resistor r=1k
    mt0 (g trim\<0\> vss vss) nch_lvt w=1u l=1u
    mi (g ib vss vss) nch_lvt w=1u l=1u
    rfb (vout vss) resistor r=200k
ends ldo_blk

subckt wx_pmu_core (AVDD_IN AGND_A PSUBX VSS_BLK1 VSS_BLK2 RETP VOUT_P VOUT_Q VOUT_R \
        IB_ALPHA IB_BETA ctl_en_a ctl_en_b ctl_trim\<1\> ctl_trim\<0\> ctl_sw_en ctl_spare)
    Xp (AVDD_IN VOUT_P RETP ctl_en_a ctl_trim\<1\> ctl_trim\<0\> nbp) ldo_blk
    Xq (AVDD_IN VOUT_Q VSS_BLK1 ctl_en_b ctl_trim\<1\> ctl_trim\<0\> nbq) ldo_blk
    Xr (AVDD_IN VOUT_R VSS_BLK2 swb ctl_trim\<1\> ctl_trim\<0\> nbr) ldo_blk
    Xinv (ctl_sw_en swb AVDD_IN VSS_BLK2) INVD1
    mb1 (IB_ALPHA nb AGND_A PSUBX) nch_lvt w=10u l=1u
    mb2 (IB_BETA nb AGND_A PSUBX) nch_lvt w=10u l=1u
    mb3 (nb nb AGND_A PSUBX) nch_lvt w=10u l=1u
ends wx_pmu_core

XPMU (AVDD_IN GNDA GNDA GNDA GNDA VSS_L3 VOUT_P VOUT_Q VOUT_R IB_ALPHA IB_BETA \
      VSS_CTL VSS_CTL VSS_CTL VSS_CTL VSS_CTL VSS_CTL) wx_pmu_core
VS_AVDD_IN (AVDD_IN GNDA) vsource dc=1.0
VB_IB_ALPHA (IB_ALPHA VSS_CTL) vsource dc=0.5
VB_IB_BETA (IB_BETA VSS_CTL) vsource dc=0.6
IL_VOUT_P (VOUT_P VSS_L3) isource dc=1m
IL_VOUT_Q (VOUT_Q GNDA) isource dc=500u
IL_VOUT_R (VOUT_R 0) isource dc=2m
RGA (GNDA 0) resistor r=1
RGC (VSS_CTL 0) resistor r=1
RGL (VSS_L3 0) resistor r=1
Lpk1 (VOUT_Q pad_q1) inductor l=1n
Lpk2 (VOUT_Q pad_q2) inductor l=2n
Rpad1 (pad_q1 0) resistor r=1meg
Rpad2 (pad_q2 0) resistor r=1meg
dcOp dc
"""

CONTROLS = ["ctl_en_a", "ctl_en_b", r"ctl_trim\<1\>", r"ctl_trim\<0\>", "ctl_sw_en",
            "ctl_spare"]
GROUNDS = ["AGND_A", "PSUBX", "VSS_BLK1", "VSS_BLK2", "RETP"]


def bench(includes: str = INCLUDES_CORNER_FIRST, extra: str = "") -> str:
    return BENCH.replace("{includes}", includes).replace("dcOp dc", extra + "dcOp dc")


def scan(text: str):
    return Netlist(text, "tb/input.scs").scan("XPMU")


# ------------------------------------------------------------------ pins, roles, grounds
def test_supplies_biases_and_rails_are_the_pins_their_sources_name():
    t = scan(bench())
    by = t.pins
    assert by["AVDD_IN"].role == "supply" and by["AVDD_IN"].src == "VS_AVDD_IN"
    assert not by["AVDD_IN"].src_reversed
    assert [p.name for p in t.of_role("bias")] == ["IB_ALPHA", "IB_BETA"]
    assert [p.name for p in t.of_role("rail")] == ["VOUT_P", "VOUT_Q", "VOUT_R"]
    assert all(not p.src_reversed for p in t.pins.values())
    assert not any("polarity is inverted" in n for n in t.notes)


def test_a_source_return_net_is_a_ground_and_its_pins_are_ground_pins():
    t = scan(bench())
    assert sorted(p.name for p in t.grounds()) == sorted(GROUNDS)
    for g in GROUNDS:
        assert t.pins[g].role == "none" and t.pins[g].fate == "ignore"
    assert not any("no ground pin" in n for n in t.notes)
    # a ground pin with no ground-like name is read from the devices it reaches
    assert "sources/bulks" in t.pins["RETP"].gnd_from
    assert any(n.startswith("ground nets read from the bench:") and "GNDA (the return of "
               "VS_AVDD_IN)" in n and "VSS_CTL" in n and "VSS_L3" in n for n in t.notes)


def test_control_inputs_tied_to_a_ground_net_are_passed_through_not_biases():
    t = scan(bench())
    for name in CONTROLS:
        p = t.pins[name]
        assert (p.role, p.fate, p.is_ground, p.tied) == ("none", "ignore", False, "VSS_CTL"), name
        assert p.reason == ("tied to VSS_CTL (a ground) in the bench: a control input, "
                            "passed through")
    assert t.unclassified() == []        # a tied control is accounted for, not "no role"
    assert "gates" in t.pins["ctl_en_a"].gnd_from                # reaches only MOS gates
    assert "gates" in t.pins["ctl_sw_en"].gnd_from               # a standard cell's input
    assert "bus" in t.pins[r"ctl_trim\<1\>"].gnd_from or "gates" in t.pins[r"ctl_trim\<1\>"].gnd_from
    tied = [n for n in t.notes if "read as control inputs" in n]
    assert len(tied) == 1 and tied[0].startswith("6 pin(s) tied to VSS_CTL")


def test_per_rail_ground_is_chosen_among_the_real_ground_pins_only():
    t = scan(bench())
    by = t.pins
    assert by["VOUT_P"].gnd == "RETP"                    # the only ground pin on its return net
    assert "where IL_VOUT_P returns" in by["VOUT_P"].gnd_from
    assert by["VOUT_Q"].gnd == "VSS_BLK1"                # nearest among the pins on GNDA
    assert "among the ground pins on GNDA" in by["VOUT_Q"].gnd_from
    assert by["VOUT_R"].gnd == "VSS_BLK2"                # returns to 0: nearest of them all
    assert by["IB_ALPHA"].gnd == "AGND_A" and by["IB_BETA"].gnd == "AGND_A"
    for p in t.pins.values():
        if p.gnd:
            assert p.gnd in GROUNDS, (p.name, p.gnd)


def test_scan_notes_are_one_line_per_thing():
    t = scan(bench())
    assert len(t.notes) == len(set(t.notes))
    # the package inductors on a rail are real elements and still worth a note each
    assert sum("also hangs on rail VOUT_Q" in n for n in t.notes) == 2


def test_a_reversed_source_on_many_pins_is_noted_once_with_the_count():
    text = bench().replace("XPMU (AVDD_IN GNDA", "XPMU (AVDD_IN GNDA").replace(
        "IL_VOUT_R (VOUT_R 0) isource dc=2m", "IL_VOUT_R (0 VOUT_R) isource dc=2m")
    text = text.replace("VOUT_P VOUT_Q VOUT_R IB_ALPHA", "VOUT_P VOUT_Q VOUT_R IB_ALPHA")
    # two PMU pins on the reversed rail's net
    text = text.replace("ctl_sw_en ctl_spare)", "ctl_sw_en ctl_spare VOUT_R2)")
    text = text.replace("VSS_CTL VSS_CTL) wx_pmu_core", "VSS_CTL VSS_CTL VOUT_R) wx_pmu_core")
    t = scan(text)
    assert t.pins["VOUT_R"].src_reversed and t.pins["VOUT_R2"].src_reversed
    rev = [n for n in t.notes if "polarity is inverted" in n]
    assert len(rev) == 1 and "wired (0 VOUT_R)" in rev[0] and "2 pins (VOUT_R, VOUT_R2)" in rev[0]
    assert any("IL_VOUT_R gives VOUT_R2" in n for n in t.notes) is False


def test_a_source_not_named_after_its_pin_is_used_and_noted_once():
    text = bench().replace("IL_VOUT_R (VOUT_R 0)", "IL_SOMETHING (VOUT_R 0)")
    t = scan(text)
    assert t.pins["VOUT_R"].src == "IL_SOMETHING"
    assert sum("IL_SOMETHING gives VOUT_R its role" in n for n in t.notes) == 1


def test_without_the_subckt_every_pin_on_a_ground_net_is_a_ground_and_that_is_said():
    text = bench()
    start = text.index("subckt wx_pmu_core")
    end = text.index("ends wx_pmu_core") + len("ends wx_pmu_core")
    t = scan(text[:start] + text[end:])
    assert all(p.is_ground for p in t.pins.values() if p.net in ("GNDA", "VSS_CTL", "VSS_L3"))
    assert any("is not readable, so every pin on a ground net is taken as a ground" in n
               for n in t.notes)


def test_the_demo_style_zero_ground_and_reversed_source_still_work():
    text = """\
simulator lang=spectre
subckt p (out ib vss)
    m1 (out ib vss vss) nch w=1u l=1u
ends p
X1 (VOUT IBIAS 0) p
IL_VOUT (VOUT 0) isource dc=1m
VB_IBIAS (0 IBIAS) vsource dc=0.4
"""
    t = Netlist(text).scan("X1")
    assert t.pins["ib"].role == "bias" and t.pins["ib"].src_reversed
    assert t.pins["vss"].is_ground and t.pins["out"].gnd == "vss"


# ------------------------------------------------------------------ zero-impedance shorts
SHORTS = r"""simulator lang=spectre
parameters LBW=0 LBIG=2n
subckt s (vo vi gnd)
    mp (vo g vi vi) pch w=1u l=1u
    mn (g g gnd gnd) nch w=1u l=1u
ends s
X1 (VO_PIN VI_PIN GX) s
Lbw (VO_PIN VO) inductor l=0
IL_VO (VO 0) isource dc=1m
Lsup (VI_PIN VI) inductor l=1f
VS_VI (VI 0) vsource dc=1.2
Rg (GX GX2) resistor r=0
Vprb (GX2 0) vsource dc=0
"""


def test_zero_impedance_elements_join_nets_into_one_node():
    t = Netlist(SHORTS).scan("X1")
    assert t.pins["vo"].role == "rail" and t.pins["vo"].src == "IL_VO"
    assert t.pins["vi"].role == "supply" and t.pins["vi"].src == "VS_VI"
    assert t.pins["gnd"].is_ground                    # GX -> R=0 -> 0 V probe -> 0
    assert not any("also hangs on rail" in n for n in t.notes)     # a short is not a load
    shorts = [n for n in t.notes if "treated as shorts" in n]
    assert len(shorts) == 1 and shorts[0].startswith("4 zero-impedance element(s)")
    assert "Lbw" in shorts[0]


@pytest.mark.parametrize("l,short", [("0", True), ("1f", True), ("10f", True), ("11f", False),
                                     ("1n", False), ("LBW", True), ("LBIG", False)])
def test_inductor_threshold_and_parameter_values(l, short):
    t = Netlist(SHORTS.replace("Lbw (VO_PIN VO) inductor l=0",
                               f"Lbw (VO_PIN VO) inductor l={l}")).scan("X1")
    assert (t.pins["vo"].role == "rail") is short
    if not short:
        assert t.pins["vo"].role == "none"


def test_resistor_threshold():
    ok = Netlist(SHORTS.replace("Rg (GX GX2) resistor r=0", "Rg (GX GX2) resistor r=1u"))
    assert ok.scan("X1").pins["gnd"].is_ground
    no = Netlist(SHORTS.replace("Rg (GX GX2) resistor r=0", "Rg (GX GX2) resistor r=1m"))
    assert not no.scan("X1").pins["gnd"].is_ground


def test_an_expression_that_does_not_evaluate_is_not_a_short_and_is_said():
    t = Netlist(SHORTS.replace("inductor l=0", "inductor l=LBW*2")).scan("X1")
    assert t.pins["vo"].role == "none"
    assert any(n.startswith("Lbw: the l= / r= value is an expression") for n in t.notes)


def test_a_decap_behind_a_zero_henry_placeholder_is_still_on_the_rail():
    text = SHORTS.replace("IL_VO (VO 0)", "Lfar (VO VO_FAR) inductor l=1f\n"
                                          "Cd (VO_FAR 0) capacitor c=1u\nIL_VO (VO 0)")
    with pytest.raises(PmuError) as e:
        Netlist(text).scan("X1")
    assert "Cd" in e.value.what and "decap" in e.value.what


def test_a_real_inductor_on_a_rail_is_still_reported():
    text = SHORTS.replace("IL_VO (VO 0)", "Lreal (VO VO_X) inductor l=1n\n"
                                          "Rx (VO_X 0) resistor r=1meg\nIL_VO (VO 0)")
    t = Netlist(text).scan("X1")
    assert any(n.startswith("Lreal (inductor) also hangs on rail vo") for n in t.notes)


# ------------------------------------------------------------------ includes and corners
def test_the_corner_is_the_corner_line_and_every_line_is_listed():
    t = scan(bench())
    assert t.sections == {"models/pdk_top.scs": "TOP_TT_X"}
    assert [(i["section"], i["corner"]) for i in t.includes] == [
        ("TOP_TT_X", True), ("pre_Sim", False), ("Noise_Worst", False)]


def test_the_corner_line_is_found_by_its_name_not_its_position():
    inc = ('include "models/pdk_top.scs" section=Noise_Worst\n'
           'include "models/pdk_top.scs" section=pre_Sim\n'
           'include "models/pdk_top.scs" section=TOP_SS_X\n')
    nl = Netlist(bench(inc), "tb/input.scs")
    t = nl.scan("XPMU")
    assert t.sections == {"models/pdk_top.scs": "TOP_SS_X"}
    assert [i["corner"] for i in t.includes] == [False, False, True]
    notes = nl.set_section_all("TOP_FF_X")
    lines = [ln for ln in nl.text.splitlines() if ln.startswith("include")]
    assert lines == ['include "models/pdk_top.scs" section=Noise_Worst',
                     'include "models/pdk_top.scs" section=pre_Sim',
                     'include "models/pdk_top.scs" section=TOP_FF_X']
    assert sum("fixed model-library section, kept as exported" in n for n in notes) == 2
    # a composite corner naming the file rewrites the same line
    nl2 = Netlist(bench(inc), "tb/input.scs")
    nl2.set_section("pdk_top.scs", "TOP_FF_X")
    assert "section=TOP_FF_X" in nl2.text.splitlines()[
        [i for i, ln in enumerate(nl2.text.splitlines()) if "pdk_top" in ln][2]]


def test_an_ambiguous_corner_line_falls_back_to_the_first_flagged_and_can_be_chosen():
    inc = ('include "models/pdk_top.scs" section=lib_a\n'
           'include "models/pdk_top.scs" section=lib_b\n'
           'include "models/pdk_top.scs" section=lib_c\n')
    nl = Netlist(bench(inc), "tb/input.scs")
    c = nl.corner_lines()["models/pdk_top.scs"]
    assert (c["index"], c["how"], c["sure"]) == (0, "first", False)
    assert [i["sure"] for i in nl.scan("XPMU").includes] == [False, False, False]
    notes = nl.copy().set_section_all("lib_z")
    assert any("is not clear from the names" in n for n in notes)
    nl.corner_choice = {"pdk_top.scs": 2}
    assert nl.corner_lines()["models/pdk_top.scs"]["how"] == "chosen"
    nl.set_section_all("lib_z")
    lines = [ln for ln in nl.text.splitlines() if ln.startswith("include")]
    assert lines[2].endswith("section=lib_z") and lines[0].endswith("section=lib_a")


def test_two_corner_looking_lines_are_ambiguous_too():
    inc = ('include "m.scs" section=TOP_TT_X\ninclude "m.scs" section=tt_noise\n')
    c = Netlist(bench(inc)).corner_lines()["m.scs"]
    assert (c["index"], c["sure"]) == (0, False)


@pytest.mark.parametrize("name,yes", [("tt", True), ("TOP_TT_X", True), ("ss_lib", True),
                                      ("mos_ff", True), ("typical", True), ("rcworst", True),
                                      ("Noise_Worst", False), ("pre_Sim", False), ("lib_a", False)])
def test_what_reads_as_a_process_corner(name, yes):
    assert looks_like_corner(name) is yes


# ------------------------------------------------------------------ the code variable
def test_a_single_vset_parameter_is_a_suggestion_and_its_followers_are_named():
    t = scan(bench())
    assert t.code_var == {"param": "CORE_VSET", "value": 10, "suggested": True,
                          "candidates": ["CORE_VSET"]}
    assert t.param_followers["CORE_VSET"] == ["LVL_P", "LVL_Q", "NEXT_LVL"]
    assert t.param_followers["CAL_ON"] == ["AUX_LVL"]


def test_vset_itself_or_no_single_candidate_is_never_a_suggestion():
    assert code_variable({"VSET": "3", "A_VSET": "1"})["suggested"] is False
    assert code_variable({"VSET": "3"})["param"] == "VSET"
    assert code_variable({"A_VSET": "1", "B_VSET": "2"})["param"] is None
    assert code_variable({"A_VSET": "0.5*X"})["param"] is None
    assert code_variable({"TRIM": "1"})["param"] is None
    assert param_followers({"A": "1", "B": "A+1", "C": "2*B", "D": "3"}) == {"A": ["B", "C"],
                                                                           "B": ["C"]}


# ------------------------------------------------------------------ the seed and the screen
def test_the_seed_takes_the_corner_line_and_the_suggested_code_variable():
    t = scan(bench('include "models/pdk_top.scs" section=Noise_Worst\n'
                   'include "models/pdk_top.scs" section=TOP_TT_X\n'))
    cfg = server._seed_config("p", pathlib.Path("tb/input.scs"), "XPMU", t)
    assert cfg.corners == ["TOP_TT_X"]
    assert cfg.vset_param == "CORE_VSET" and cfg.vset_codes == [10]
    assert all(cfg.ports[c] == "ignore" for c in CONTROLS)
    assert cfg.ports["IB_ALPHA"] == "model"


def test_the_seed_guesses_no_code_variable_when_there_is_none():
    t = scan(bench().replace("CORE_VSET", "CORE_CODE"))
    cfg = server._seed_config("p", pathlib.Path("tb/input.scs"), "XPMU", t)
    assert cfg.vset_param == "VSET" and cfg.vset_codes == [0]


def test_corner_chips_offer_only_the_declared_process_corners(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "pdk_top.scs").write_text(
        "".join(f"section {s}\nendsection {s}\n" for s in
                ("TOP_TT_X", "TOP_SS_X", "TOP_FF_X", "pre_Sim", "Noise_Worst", "stat_lib")),
        encoding="utf-8", newline="\n")
    nl = Netlist(bench(), tmp_path / "input.scs")
    out = server._include_payload(nl, nl.scan("XPMU"))
    assert out["section_choices"] == ["TOP_FF_X", "TOP_SS_X", "TOP_TT_X"]
    assert len(out["includes"]) == 3


def test_corner_chips_without_the_file_offer_the_defaults_and_the_current_corner(tmp_path):
    nl = Netlist(bench(), tmp_path / "input.scs")
    out = server._include_payload(nl, nl.scan("XPMU"))
    assert out["section_choices"] == ["TOP_TT_X", "tt", "ss", "ff"]


# ------------------------------------------------------------------ through the API
@pytest.fixture
def api(tmp_path):
    a = server.Api(root=tmp_path / "data")
    a.new_project({"name": "p"})
    return a


def _load(api, path):
    from tests.test_new_screen import load_ok
    return load_ok(api, {"path": str(path)})


def test_a_config_seeded_with_a_fixed_section_is_reseeded_from_the_corner_line(api, tmp_path):
    f = tmp_path / "input.scs"
    f.write_text(bench(), encoding="utf-8", newline="\n")
    _load(api, f)
    cfg = api.get_config("p")["config"]
    assert cfg["corners"] == ["TOP_TT_X"]
    cfg["corners"] = ["Noise_Worst"]                         # what the old seed wrote
    api.put_config("p", {"config": cfg})
    pins = api.pins("p")
    assert "Noise_Worst is a fixed model-library section" in pins["corner_fix"]
    assert api.get_config("p")["config"]["corners"] == ["TOP_TT_X"]
    assert "re-seeded to TOP_TT_X" in api.netlist_info("p")["source"]["changes"]["text"]
    assert "corner_fix" not in api.pins("p")                 # once: absent when nothing moved


def test_an_old_seed_with_the_default_vset_gets_the_suggestion(api, tmp_path):
    f = tmp_path / "input.scs"
    f.write_text(bench(), encoding="utf-8", newline="\n")
    _load(api, f)
    cfg = api.get_config("p")["config"]
    cfg.pop("vset_param")                                    # what the old seed wrote: VSET, [0]
    cfg["vset_codes"] = [0]
    cfg["corners"] = ["Noise_Worst"]
    api.put_config("p", {"config": cfg})
    fix = api.pins("p")["corner_fix"]
    assert "re-seeded to TOP_TT_X" in fix and "CORE_VSET=10 suggested" in fix
    got = api.get_config("p")
    assert got["config"]["vset_param"] == "CORE_VSET" and got["config"]["vset_codes"] == [10]
    assert got["answers"].get("vset_confirmed") != "CORE_VSET"   # still a suggestion


def test_the_suggested_code_variable_is_confirmed_once(api, tmp_path):
    f = tmp_path / "input.scs"
    f.write_text(bench(), encoding="utf-8", newline="\n")
    out = _load(api, f)
    assert out["code_var"]["suggested"] and out["code_var"]["param"] == "CORE_VSET"
    got = api.get_config("p")
    assert got["config"]["vset_param"] == "CORE_VSET"
    assert "vset_confirmed" not in got["answers"]
    n = len(api.get_config("p")["history"])
    done = api.put_config("p", {"config": got["config"], "confirm": "vset_param"})
    assert done["answers"]["vset_confirmed"] == "CORE_VSET"
    assert len(api.get_config("p")["history"]) == n           # confirming saves no new config


def test_choosing_the_corner_line_is_a_config_answer_the_plan_follows(api, tmp_path):
    inc = ('include "models/pdk_top.scs" section=lib_a\n'
           'include "models/pdk_top.scs" section=lib_b\n')
    f = tmp_path / "input.scs"
    f.write_text(bench(inc), encoding="utf-8", newline="\n")
    _load(api, f)
    cfg = api.get_config("p")["config"]
    cfg["corner_include"] = {"models/pdk_top.scs": 1}
    cfg["corners"] = ["lib_b"]
    api.put_config("p", {"config": cfg})
    pins = api.pins("p")
    assert [i["corner"] for i in pins["includes"]] == [False, True]
    assert pins["sections"] == {"models/pdk_top.scs": "lib_b"}
