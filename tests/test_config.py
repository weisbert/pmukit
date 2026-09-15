"""Contract 0a/0b: intake round-trip, every validation refusal, the derived characterization config.

Synthetic names only (the commit gate forbids customer identifiers): rails VDD0P8_A/B/C, biases
IB_PTAT/IB_POLY, supply VDDA_1V0, grounds VSS_A/VSS_B/AGND, enable EN, project demo_pmu.
"""
import copy
import math

import pytest

from pmukit import jsonio
from pmukit.config import (
    EDGE_DEFAULT_S,
    SETTLE_DEFAULT_S,
    SETTLE_WINDOWS,
    ConfigHistory,
    DerivedConfig,
    MyLoad,
    ProjectConfig,
    derive,
    n_log_points,
    refine_from_zout,
)
from pmukit.errors import PmuError
from pmukit.site import SiteConfig

# --- exactly the JSON of docs/CONTRACTS.md section 0a -------------------------------------------
CONTRACT_0A = {
    "project": "demo_pmu",
    "netlist": "tb/input.scs",
    "pmu_inst": "PMU_TOP",
    "corners": ["tt", "ss", "ff"],
    "temps_c": [-40, 25, 125],
    "vset_codes": [3],
    "state_note": "RX mode, register 0x12=0x03",
    "ports": {"VDD0P8_A": "model", "VDD0P8_B": "model", "VDD0P8_C": "stub",
              "IB_PTAT": "model", "IB_POLY": "model", "TESTMODE": "ignore"},
    "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
    "care_up_to_hz": 2e10,
}

# the same project once the netlist has been parsed (what netlist.py will hand over)
PINS = {
    "VDD0P8_A": {"role": "rail", "net": "vdd0p8_a", "gnd": "VSS_A",
                 "src": "IL_VDD0P8_A", "dc": 3.0e-4, "fate": "model"},
    "VDD0P8_B": {"role": "rail", "net": "vdd0p8_b", "gnd": "VSS_B",
                 "src": "IL_VDD0P8_B", "dc": 1.5e-4, "fate": "model"},
    "VDD0P8_C": {"role": "rail", "net": "vdd0p8_c", "gnd": "VSS_B",
                 "src": "IL_VDD0P8_C", "dc": 1.0e-4, "fate": "model"},
    "IB_PTAT": {"role": "bias", "net": "ib_ptat", "gnd": "AGND",
                "src": "VB_IB_PTAT", "dc": 0.45, "fate": "model"},
    "IB_POLY": {"role": "bias", "net": "ib_poly", "gnd": "AGND",
                "src": "VB_IB_POLY", "dc": 0.40, "fate": "model"},
    "VDDA_1V0": {"role": "supply", "net": "vdda_1v0", "gnd": "AGND",
                 "src": "VS_VDDA_1V0", "dc": 1.0, "fate": "model"},
    "EN": {"role": "en", "net": "en", "gnd": "AGND", "src": "VEN_EN", "dc": 1.0, "fate": "model"},
    "TESTMODE": {"role": "none", "net": "testmode", "gnd": None, "src": None,
                 "dc": None, "fate": "ignore"},
}


def cfg_dict(**over):
    d = copy.deepcopy(CONTRACT_0A)
    d.update(over)
    return d


def wired_cfg():
    """The contract config extended with the supply and EN pins the parser reports."""
    d = cfg_dict()
    d["ports"] = dict(d["ports"])
    d["ports"]["VDDA_1V0"] = "model"
    d["ports"]["EN"] = "model"
    return ProjectConfig.from_dict(d)


# ================================================================= 0a round-trip
def test_roundtrip_is_exact():
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    assert cfg.to_dict() == CONTRACT_0A


def test_myload_nests_as_plain_dict_and_omits_none_edge():
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    assert isinstance(cfg.my_load["VDD0P8_A"], MyLoad)
    assert cfg.to_dict()["my_load"]["VDD0P8_A"] == {"on_a": 5e-4, "off_a": 2e-6, "switches": True}


def test_edge_s_survives_the_round_trip():
    d = cfg_dict(my_load={"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True,
                                       "edge_s": 5e-9}})
    assert ProjectConfig.from_dict(d).to_dict()["my_load"]["VDD0P8_A"]["edge_s"] == 5e-9


def test_save_load_round_trip(tmp_path):
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    p = cfg.save(tmp_path / "config.json")
    assert p.read_bytes().count(b"\r\n") == 0
    assert ProjectConfig.load(p).to_dict() == cfg.to_dict()


def test_load_missing_file_is_a_four_part_error(tmp_path):
    with pytest.raises(PmuError) as e:
        ProjectConfig.load(tmp_path / "nope.json")
    assert e.value.do and e.value.where


# ================================================================= 0a validation
@pytest.mark.parametrize("key", ["project", "netlist", "pmu_inst"])
def test_empty_identity_fields(key):
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(**{key: ""}))
    assert key in str(e.value)


@pytest.mark.parametrize("key", ["project", "netlist", "pmu_inst"])
def test_missing_identity_fields(key):
    d = cfg_dict()
    d.pop(key)
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(d)
    assert key in str(e.value)


def test_empty_corners():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(corners=[]))
    assert "corners" in str(e.value)


def test_composite_corner_value_must_be_file_to_section():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(corners={"MOSff_RCss": "ff"}))
    msg = str(e.value)
    assert "MOSff_RCss" in msg and "section" in msg


def test_composite_corner_section_must_be_a_string():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(corners={"MOSff_RCss": {"toplevel.scs": 3}}))
    assert "MOSff_RCss" in str(e.value)


def test_empty_temps():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(temps_c=[]))
    assert "temps_c" in str(e.value)


def test_non_numeric_temp():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(temps_c=["25"]))
    assert "temps_c" in str(e.value)


def test_empty_vset_codes():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(vset_codes=[]))
    assert "vset_codes" in str(e.value)


@pytest.mark.parametrize("bad", [3.0, "3", True])
def test_non_integer_vset_code(bad):
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(vset_codes=[bad]))
    assert "vset_codes" in str(e.value)


def test_unknown_port_fate():
    ports = dict(CONTRACT_0A["ports"])
    ports["VDD0P8_B"] = "maybe"
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(ports=ports))
    msg = str(e.value)
    assert "VDD0P8_B" in msg and "model" in msg and "stub" in msg and "ignore" in msg


@pytest.mark.parametrize("bad", [0, -1, "2e10"])
def test_bad_care_up_to_hz(bad):
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(care_up_to_hz=bad))
    assert "care_up_to_hz" in str(e.value)


def test_my_load_key_must_be_a_port():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(my_load={"VDD0P8_X": {"on_a": 1e-3, "off_a": 1e-6}}))
    msg = str(e.value)
    assert "VDD0P8_X" in msg and "ports" in msg


def test_on_must_exceed_off():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(my_load={"VDD0P8_A": {"on_a": 1e-6, "off_a": 1e-6}}))
    msg = str(e.value)
    assert "VDD0P8_A" in msg and "on_a" in msg


@pytest.mark.parametrize("key", ["on_a", "off_a"])
def test_negative_load_current(key):
    load = {"on_a": 5e-4, "off_a": 2e-6}
    load[key] = -1e-6
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(my_load={"VDD0P8_A": load}))
    msg = str(e.value)
    assert "VDD0P8_A" in msg and key in msg


def test_unknown_top_level_key_is_refused():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(care_up_to_HZ=1e9))
    assert "care_up_to_HZ" in str(e.value)


def test_every_error_is_four_part():
    with pytest.raises(PmuError) as e:
        ProjectConfig.from_dict(cfg_dict(temps_c=[]))
    err = e.value.to_dict()["error"]
    assert err["what"] and err["why"] and err["do"] and err["where"]


# ================================================================= corners + ports
def test_simple_corners():
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    assert cfg.corner_names() == ["tt", "ss", "ff"]
    assert cfg.corner_sections("ss") == "ss"


def test_composite_corners_keep_declaration_order():
    corners = {"MOSff_RCss": {"toplevel.scs": "ff", "rc.scs": "ss"},
               "MOSss_RCff": {"toplevel.scs": "ss", "rc.scs": "ff"}}
    cfg = ProjectConfig.from_dict(cfg_dict(corners=corners))
    assert cfg.corner_names() == ["MOSff_RCss", "MOSss_RCff"]
    assert cfg.corner_sections("MOSff_RCss") == {"toplevel.scs": "ff", "rc.scs": "ss"}


def test_unknown_corner_name_raises():
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    with pytest.raises(PmuError):
        cfg.corner_sections("nope")


def test_port_fate_lists_are_in_declaration_order():
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    assert cfg.modeled_ports() == ["VDD0P8_A", "VDD0P8_B", "IB_PTAT", "IB_POLY"]
    assert cfg.stub_ports() == ["VDD0P8_C"]
    assert cfg.ignored_ports() == ["TESTMODE"]


# ================================================================= config_sha
def test_sha_is_key_order_free():
    a = ProjectConfig.from_dict(CONTRACT_0A)
    shuffled = {k: CONTRACT_0A[k] for k in sorted(CONTRACT_0A, reverse=True)}
    assert ProjectConfig.from_dict(shuffled).sha() == a.sha()
    assert len(a.sha()) == 12


def test_sha_is_int_float_spelling_free():
    a = ProjectConfig.from_dict(CONTRACT_0A)
    b = ProjectConfig.from_dict(cfg_dict(temps_c=[-40.0, 25.0, 125.0]))
    assert a.sha() == b.sha()


def test_sha_tracks_care_up_to_hz():
    a = ProjectConfig.from_dict(CONTRACT_0A)
    b = ProjectConfig.from_dict(cfg_dict(care_up_to_hz=1e10))
    assert a.sha() != b.sha()


# ================================================================= ConfigHistory (Ctrl-Z)
def test_history_push_current_undo(tmp_path):
    h = ConfigHistory(tmp_path / "config_history.json")
    assert h.current() is None
    first = ProjectConfig.from_dict(CONTRACT_0A)
    second = ProjectConfig.from_dict(cfg_dict(care_up_to_hz=1e9))
    h.push(first, "initial")
    h.push(second, "narrow the band")
    assert h.current().care_up_to_hz == 1e9
    back = h.undo()
    assert back.care_up_to_hz == 2e10
    assert h.current().sha() == first.sha()


def test_history_entries_are_metadata_only_newest_last(tmp_path):
    h = ConfigHistory(tmp_path / "h.json")
    h.push(ProjectConfig.from_dict(CONTRACT_0A), "one")
    h.push(ProjectConfig.from_dict(cfg_dict(care_up_to_hz=1e9)), "two")
    ents = h.entries()
    assert [e["note"] for e in ents] == ["one", "two"]
    assert set(ents[0]) == {"sha", "note", "saved_at"}
    assert ents[0]["saved_at"].endswith("Z")


def test_history_undo_needs_a_previous_snapshot(tmp_path):
    h = ConfigHistory(tmp_path / "h.json")
    with pytest.raises(PmuError) as e:
        h.undo()
    assert "undo" in str(e.value).lower()
    h.push(ProjectConfig.from_dict(CONTRACT_0A))
    with pytest.raises(PmuError):
        h.undo()


def test_history_drops_a_repeat_push(tmp_path):
    h = ConfigHistory(tmp_path / "h.json")
    cfg = ProjectConfig.from_dict(CONTRACT_0A)
    h.push(cfg, "one")
    h.push(ProjectConfig.from_dict(CONTRACT_0A), "same again")
    assert len(h.entries()) == 1


def test_history_caps_at_fifty(tmp_path):
    h = ConfigHistory(tmp_path / "h.json")
    for i in range(60):
        h.push(ProjectConfig.from_dict(cfg_dict(care_up_to_hz=1e9 + i)), f"n{i}")
    ents = h.entries()
    assert len(ents) == 50
    assert [e["note"] for e in ents][0] == "n10"
    assert h.current().care_up_to_hz == 1e9 + 59


# ================================================================= 0b derive
def test_derive_without_pins_still_gives_the_config_only_axes():
    d = derive(ProjectConfig.from_dict(CONTRACT_0A))
    assert d.process["corners"] == ["tt", "ss", "ff"]
    assert d.temps_c["points"] == [-40.0, 25.0, 125.0]
    assert d.vset["codes"] == [3]
    assert d.freq["stop_hz"] == 2e10
    assert d.noise["start_hz"] == 10.0
    assert d.rails == {} and d.biases == {} and d.loads == {} and d.transient == {}


def test_derive_process_rewrite_spec():
    corners = {"MOSff_RCss": {"toplevel.scs": "ff", "rc.scs": "ss"}}
    d = derive(ProjectConfig.from_dict(cfg_dict(corners=corners)))
    assert d.process["composite"] is True
    assert d.process["sections"]["MOSff_RCss"] == {"toplevel.scs": "ff", "rc.scs": "ss"}
    simple = derive(ProjectConfig.from_dict(CONTRACT_0A))
    assert simple.process["composite"] is False
    assert simple.process["sections"] == {"tt": "tt", "ss": "ss", "ff": "ff"}


def test_dc_temp_sweep_step_rule():
    d = derive(ProjectConfig.from_dict(CONTRACT_0A))
    s = d.dc_temp_sweep
    assert (s["start_c"], s["stop_c"]) == (-40.0, 125.0)
    assert s["step_c"] == pytest.approx(165.0 / 8)      # span/8, inside [5, 25]
    assert "span/8" in s["rule"]
    wide = derive(ProjectConfig.from_dict(cfg_dict(temps_c=[-55, 175]))).dc_temp_sweep
    assert wide["step_c"] == 25.0                        # clamped at the top
    narrow = derive(ProjectConfig.from_dict(cfg_dict(temps_c=[20, 30]))).dc_temp_sweep
    assert narrow["step_c"] == 5.0                       # clamped at the bottom


def test_freq_grid_is_twenty_per_decade_from_ten_hz():
    d = derive(ProjectConfig.from_dict(CONTRACT_0A))
    assert d.freq["type"] == "log"
    assert d.freq["start_hz"] == 10.0 and d.freq["stop_hz"] == 2e10
    assert d.freq["points_per_decade"] == 20
    assert d.freq["n_points"] == n_log_points(10.0, 2e10, 20) == 187
    pts = d.freq_points()
    assert len(pts) == 187
    assert pts[0] == pytest.approx(10.0) and pts[-1] == pytest.approx(2e10)
    decades = math.log10(2e10 / 10.0)
    assert (len(pts) - 1) / decades == pytest.approx(20.0, rel=0.01)


def test_noise_band_is_ten_hz_to_hundred_mhz():
    d = derive(ProjectConfig.from_dict(CONTRACT_0A))
    assert (d.noise["start_hz"], d.noise["stop_hz"]) == (10.0, 1e8)


def test_grouping_is_ac_superposition():
    assert derive(ProjectConfig.from_dict(CONTRACT_0A)).grouping["mode"] == "ac_superposition"


def test_derive_reads_the_pin_roles():
    d = derive(wired_cfg(), PINS)
    assert sorted(d.rails) == ["VDD0P8_A", "VDD0P8_B"]
    assert sorted(d.biases) == ["IB_POLY", "IB_PTAT"]
    assert list(d.en) == ["EN"]
    assert d.ignored == ["TESTMODE"]
    assert d.stubs["VDD0P8_C"]["emit"] == "vsource"
    assert "stub" in d.stubs["VDD0P8_C"]["note"]
    assert d.grounds["nets"] == ["AGND", "VSS_A", "VSS_B"]
    assert d.grounds["by_pin"]["VDD0P8_A"] == "VSS_A"


def test_supply_is_nominal_only_by_default():
    d = derive(wired_cfg(), PINS)
    assert d.supply["sweep"] is False and d.supply["advanced"] is True
    assert d.supply["pins"]["VDDA_1V0"]["nominal_v"] == 1.0
    assert d.supply["nominal_v"] == 1.0


def test_bias_iv_sweep_spans_zero_to_nominal_supply():
    d = derive(wired_cfg(), PINS)
    assert d.biases["IB_PTAT"]["vcomp_v"] == 0.45          # the VB_ compliance dc
    iv = d.biases["IB_PTAT"]["iv_sweep"]
    assert (iv["start_v"], iv["stop_v"]) == (0.0, 1.0)     # 0 .. nominal supply
    assert iv["provenance"]


def test_load_grid_is_off_02on_on_2on():
    d = derive(wired_cfg(), PINS)
    on, off = 5e-4, 2e-6
    assert d.loads["VDD0P8_A"]["points_a"] == sorted({off, 0.2 * on, on, 2 * on})
    assert d.loads["VDD0P8_A"]["load_en"] is True


def test_load_grid_is_clipped_to_the_current_limit():
    pins = copy.deepcopy(PINS)
    pins["VDD0P8_A"]["ilimit"] = 8e-4
    d = derive(wired_cfg(), pins)
    pts = d.loads["VDD0P8_A"]["points_a"]
    assert max(pts) == 8e-4
    assert pts == sorted({2e-6, 0.2 * 5e-4, 5e-4, 8e-4})
    assert "current limit" in d.loads["VDD0P8_A"]["provenance"]


def test_rail_without_my_load_gets_one_point_and_a_reason():
    d = derive(wired_cfg(), PINS)
    b = d.loads["VDD0P8_B"]
    assert b["points_a"] == [1.5e-4]              # the IL_ source dc
    assert b["load_en"] is False
    assert b["reason"] == "my_load not declared"
    assert b["events"] == []


def test_load_en_events_only_when_the_load_switches():
    d = derive(wired_cfg(), PINS)
    ev = d.loads["VDD0P8_A"]["events"]
    assert [e["event"] for e in ev] == ["tran_load_on", "tran_load_off"]
    assert (ev[0]["from_a"], ev[0]["to_a"]) == (2e-6, 5e-4)
    assert (ev[1]["from_a"], ev[1]["to_a"]) == (5e-4, 2e-6)

    cfg = ProjectConfig.from_dict(cfg_dict(
        my_load={"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": False}},
        ports={**CONTRACT_0A["ports"], "VDDA_1V0": "model", "EN": "model"}))
    d2 = derive(cfg, PINS)
    assert d2.loads["VDD0P8_A"]["events"] == []
    assert d2.loads["VDD0P8_A"]["load_en"] is False
    assert "switches" in d2.loads["VDD0P8_A"]["reason"]


# ----------------------------------------------------------------- the transient footgun
def test_edge_defaults_to_one_nanosecond():
    d = derive(wired_cfg(), PINS)
    assert d.transient["VDD0P8_A"]["edge_s"] == EDGE_DEFAULT_S == 1e-9
    assert d.loads["VDD0P8_A"]["events"][0]["edge_s"] == 1e-9


def test_measured_edge_wins():
    cfg = ProjectConfig.from_dict(cfg_dict(
        my_load={"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True, "edge_s": 5e-9}},
        ports={**CONTRACT_0A["ports"], "VDDA_1V0": "model", "EN": "model"}))
    d = derive(cfg, PINS)
    assert d.transient["VDD0P8_A"]["edge_s"] == 5e-9
    assert d.loads["VDD0P8_A"]["events"][0]["edge_s"] == 5e-9


def test_tstop_is_eight_settling_times_not_one_over_f_min():
    d = derive(wired_cfg(), PINS)
    tr = d.transient["VDD0P8_A"]
    assert tr["t_settle_s"] == SETTLE_DEFAULT_S == 2e-6
    assert tr["tstop_s"] == pytest.approx(SETTLE_WINDOWS * SETTLE_DEFAULT_S) == 1.6e-5
    # the carrier-sized AC band must NOT leak into the timescale
    assert tr["edge_s"] == 1e-9 and d.freq["stop_hz"] == 2e10
    assert tr["tstop_s"] / tr["edge_s"] < 1e5
    assert "default" in tr["provenance"] and "Zout" in tr["provenance"]


def test_refine_from_zout_replaces_the_default_settling():
    d = derive(wired_cfg(), PINS)
    before = d.transient["VDD0P8_A"]["provenance"]
    refine_from_zout(d, "VDD0P8_A", 1.2e6)
    tr = d.transient["VDD0P8_A"]
    assert tr["t_settle_s"] == pytest.approx(8.0 / (2 * math.pi * 1.2e6))
    assert tr["tstop_s"] == pytest.approx(8.0 * tr["t_settle_s"])
    assert tr["f_peak_hz"] == 1.2e6
    assert tr["provenance"] != before and "f_peak" in tr["provenance"]


def test_refine_from_zout_refuses_a_bad_rail_or_frequency():
    d = derive(wired_cfg(), PINS)
    with pytest.raises(PmuError) as e:
        refine_from_zout(d, "VDD0P8_X", 1e6)
    assert "VDD0P8_X" in str(e.value)
    with pytest.raises(PmuError):
        refine_from_zout(d, "VDD0P8_A", 0.0)


# ----------------------------------------------------------------- derived plumbing
def test_every_derived_field_carries_provenance():
    d = derive(wired_cfg(), PINS)
    for name in ("process", "temps_c", "dc_temp_sweep", "vset", "supply", "grounds",
                 "freq", "noise", "grouping"):
        assert getattr(d, name)["provenance"]
    for group in (d.rails, d.biases, d.loads, d.transient, d.stubs, d.en):
        for entry in group.values():
            assert entry["provenance"]


def test_derived_round_trips_through_json(tmp_path):
    d = derive(wired_cfg(), PINS, SiteConfig(engine="dry_run", cpus=4))
    p = d.save(tmp_path / "derived.json")
    again = DerivedConfig.load(p)
    assert again.to_dict() == d.to_dict()
    assert again.config_sha == wired_cfg().sha()
    assert again.site["engine"] == "dry_run"


def test_derived_sha_ignores_the_site():
    a = derive(wired_cfg(), PINS, SiteConfig(engine="dry_run", cpus=4))
    b = derive(wired_cfg(), PINS, SiteConfig(engine="fake", cpus=64))
    assert a.sha() == b.sha()
    assert a.to_dict()["site"] != b.to_dict()["site"]


def test_derive_accepts_a_pin_table_object():
    class FakeTable:
        def to_dict(self):
            return {"pins": copy.deepcopy(PINS)}

    d = derive(wired_cfg(), FakeTable())
    assert sorted(d.rails) == ["VDD0P8_A", "VDD0P8_B"]


def test_derive_refuses_a_nonsense_pin_table():
    with pytest.raises(PmuError):
        derive(wired_cfg(), 42)


def test_ports_fate_overrides_the_parser_guess():
    pins = copy.deepcopy(PINS)
    pins["VDD0P8_C"]["fate"] = "model"      # the parser guessed model ...
    d = derive(wired_cfg(), pins)           # ... but ports says stub
    assert "VDD0P8_C" not in d.rails and "VDD0P8_C" in d.stubs


def test_derived_json_is_canonical(tmp_path):
    d = derive(wired_cfg(), PINS)
    p = d.save(tmp_path / "derived.json")
    assert p.read_bytes().count(b"\r\n") == 0
    assert jsonio.sha(jsonio.read(p), 12) == jsonio.sha(d.to_dict(), 12)


# ================================================================= site config
def test_site_defaults():
    s = SiteConfig()
    assert s.engine == "spectre_ssh" and s.cpus == 8 and s.queue == ""
    assert s.remote_workdir and s.spectre_cmd == "spectre"


def test_site_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("PMUKIT_ENGINE", raising=False)
    monkeypatch.delenv("PMUKIT_SSH_HOST", raising=False)
    monkeypatch.delenv("PMUKIT_CPUS", raising=False)
    s = SiteConfig(engine="donau_alps", queue="rf_long", cpus=16, project_account="demo_acct")
    p = s.save(tmp_path / "site.json")
    assert p.read_bytes().count(b"\r\n") == 0
    assert SiteConfig.load(p).to_dict() == s.to_dict()


def test_site_missing_file_is_the_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("PMUKIT_ENGINE", raising=False)
    monkeypatch.delenv("PMUKIT_CPUS", raising=False)
    assert SiteConfig.load(tmp_path / "absent.json").to_dict() == SiteConfig().to_dict()


def test_site_default_path_follows_pmukit_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    assert SiteConfig.default_path() == tmp_path / "site.json"


def test_site_env_overrides(tmp_path, monkeypatch):
    SiteConfig(engine="spectre_ssh", cpus=8, ssh_host="build-vm").save(tmp_path / "site.json")
    monkeypatch.setenv("PMUKIT_ENGINE", "dry_run")
    monkeypatch.setenv("PMUKIT_SSH_HOST", "other-vm")
    monkeypatch.setenv("PMUKIT_CPUS", "2")
    s = SiteConfig.load(tmp_path / "site.json")
    assert (s.engine, s.ssh_host, s.cpus) == ("dry_run", "other-vm", 2)


def test_site_bad_env_cpus(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_CPUS", "many")
    with pytest.raises(PmuError) as e:
        SiteConfig.load(tmp_path / "absent.json")
    assert "PMUKIT_CPUS" in str(e.value)


def test_site_unknown_engine():
    with pytest.raises(PmuError) as e:
        SiteConfig(engine="ngspice").validate()
    assert "ngspice" in str(e.value) and "dry_run" in str(e.value)


def test_site_bad_cpus():
    with pytest.raises(PmuError) as e:
        SiteConfig(cpus=0).validate()
    assert "cpus" in str(e.value)


def test_site_donau_needs_a_queue():
    with pytest.raises(PmuError) as e:
        SiteConfig(engine="donau_alps", queue="").validate()
    assert "queue" in str(e.value)


def test_site_unknown_key_is_refused():
    with pytest.raises(PmuError) as e:
        SiteConfig.from_dict({"engine": "dry_run", "nodes": 4})
    assert "nodes" in str(e.value)


# ---------------------------------------------------- stub_dc: the one number the netlist lacks
def test_stub_dc_reaches_derive_as_volts_or_amps():
    """A rail stub needs VOLTS and a bias stub needs AMPS -- the dual of what its source carries."""
    from pmukit.config import ProjectConfig, derive

    pins = {
        "VDD0P8_A": {"role": "rail", "net": "VDD0P8_A", "src": "IL_VDD0P8_A", "dc": 5e-4},
        "VDD0P8_C": {"role": "rail", "net": "VDD0P8_C", "src": "IL_VDD0P8_C", "dc": 1e-4},
        "IB_POLY": {"role": "bias", "net": "IB_POLY", "src": "VB_IB_POLY", "dc": 0.4},
        "VDDA_1V0": {"role": "supply", "net": "VDDA_1V0", "src": "VS_VDDA_1V0", "dc": 1.0},
    }
    cfg = ProjectConfig.from_dict({
        "project": "p", "netlist": "tb.scs", "pmu_inst": "X",
        "corners": ["tt"], "temps_c": [25], "vset_codes": [3],
        "ports": {"VDD0P8_A": "model", "VDD0P8_C": "stub", "IB_POLY": "stub",
                  "VDDA_1V0": "model"},
        "my_load": {}, "care_up_to_hz": 1e9,
        "stub_dc": {"VDD0P8_C": 0.8, "IB_POLY": 5e-6}})
    d = derive(cfg, pins)
    rail, bias = d.stubs["VDD0P8_C"], d.stubs["IB_POLY"]
    assert rail["dc_v"] == 0.8 and rail["dc_a"] is None
    assert bias["dc_a"] == 5e-6 and bias["dc_v"] is None
    assert rail["dc_source"] == "config.stub_dc"


def test_a_stub_without_a_declared_level_says_so():
    from pmukit.config import ProjectConfig, derive

    pins = {"VDD0P8_C": {"role": "rail", "net": "VDD0P8_C", "src": "IL_VDD0P8_C", "dc": 1e-4},
            "VDD0P8_A": {"role": "rail", "net": "VDD0P8_A", "src": "IL_VDD0P8_A", "dc": 5e-4}}
    cfg = ProjectConfig.from_dict({
        "project": "p", "netlist": "tb.scs", "pmu_inst": "X",
        "corners": ["tt"], "temps_c": [25], "vset_codes": [3],
        "ports": {"VDD0P8_C": "stub", "VDD0P8_A": "model"},
        "my_load": {}, "care_up_to_hz": 1e9})
    s = derive(cfg, pins).stubs["VDD0P8_C"]
    assert s["dc_v"] is None and s["dc_a"] is None
    assert "weakly tied" in s["dc_source"]


def test_stub_dc_round_trips_through_the_config_json():
    from pmukit.config import ProjectConfig

    base = {"project": "p", "netlist": "tb.scs", "pmu_inst": "X", "corners": ["tt"],
            "temps_c": [25], "vset_codes": [3], "ports": {"A": "stub"}, "my_load": {},
            "care_up_to_hz": 1e9, "stub_dc": {"A": 0.8}}
    cfg = ProjectConfig.from_dict(base)
    assert ProjectConfig.from_dict(cfg.to_dict()).stub_dc == {"A": 0.8}
    # and it is part of the identity of the configuration
    other = ProjectConfig.from_dict({**base, "stub_dc": {"A": 0.9}})
    assert cfg.sha() != other.sha()
