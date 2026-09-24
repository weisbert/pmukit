"""The Model screen shows model QUALITY: a wrong number there is worse than no number.

These tests pin the joins between what `verify` wrote (verify.json) and what the page draws --
the grade grid, the cell table, the HB tile and the valid-range box -- on hand-written fit and
verify files, so each one isolates exactly one rule. The whole flow on the fake engine is in
tests/test_e2e_api.py.
"""
import os
import time

import pytest

from pmukit import jsonio, server

RAIL = "VDD0P8_A"


def _bf(block, cell, score, metric="|Zout| dB RMS", missing=False):
    return {"port": RAIL, "block": block, "cell": cell, "params": {}, "score": score,
            "metric": metric, "n_points": 10, "identifiability": {}, "notes": [],
            "missing": missing}


FIT = {"project": "p1", "ports": {RAIL: "rail"}, "dataset_sha": "abc", "fits": {
    f"{RAIL}/zout/tt/-40C/vset3/1.0e-03A": _bf("zout", {"process": "tt", "temp_c": -40.0,
                                                        "vset": 3, "load_a": 1e-3}, 0.04),
    f"{RAIL}/zout/tt/125C/vset3/1.0e-03A": _bf("zout", {"process": "tt", "temp_c": 125.0,
                                                        "vset": 3, "load_a": 1e-3}, 0.04),
    f"{RAIL}/dc/tt/vset3/1.0e-03A": _bf("dc", {"process": "tt", "vset": 3, "load_a": 1e-3},
                                        0.0, "vout % RMS"),
    f"{RAIL}/dc/tt/vset4/1.0e-03A": _bf("dc", {"process": "tt", "vset": 4, "load_a": 1e-3},
                                        0.0, "vout % RMS"),
    f"{RAIL}/load_en/tt/125C": _bf("load_en", {"process": "tt", "temp_c": 125.0}, 196.0,
                                   "load-step droop % error"),
}}

#: What verify_project writes: one row per (port, corner, block), NO temperature.
GRADES = [{"port": RAIL, "corner": "tt", "block": "zout", "grade": "yellow", "detail": "z",
           "score": 0.04},
          {"port": RAIL, "corner": "tt", "block": "dc", "grade": "green", "detail": "d",
           "score": 0.0},
          {"port": RAIL, "corner": "tt", "block": "load_en", "grade": "red", "detail": "l",
           "score": 196.0}]

ENVELOPE = {"freq_max_hz": 1e9, "load_a": {RAIL: [2e-6, 1e-3]}, "temp_c": [-40.0, 125.0],
            "corners": ["tt"], "vset_codes": [3, 4], "ls_default_on": [], "ports": [RAIL],
            "notes": []}


@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "p1"
    d.mkdir()
    jsonio.write(d / "fit.json", FIT)
    return server.Api(root=tmp_path), d


def _verify(d, grades=GRADES, hb=None, envelope=ENVELOPE, ls_on=()):
    """`ls_on`: the rails whose load_en passed the HB check and so ships ON by default."""
    jsonio.write(d / "verify.json", {"project": "p1", "grades": grades, "envelope": envelope,
                                     "hb_check": hb or {"status": "not_run", "notes": ["x"]},
                                     "ls_default_on": list(ls_on)})
    # verify runs after the fit: make that ordering visible to the mtime check
    later = time.time() + 5
    os.utime(d / "verify.json", (later, later))


def _grid(api):
    g = api.model_grades("p1")
    return g, {(c["corner"], c["temp_c"]): c["grade"] for c in g["rows"][0]["cells"]}


def test_without_verify_nothing_claims_a_colour(proj):
    api, _d = proj
    g, cells = _grid(api)
    assert g["graded_by"] == "fit" and g["why"]
    assert set(cells.values()) == {"fitted"}


def test_a_per_corner_verify_grade_reaches_every_temperature_of_that_corner(proj):
    """verify.json carries no temperature, so keying the lookup by the cell's temperature
    never found it: after verify the grid still said FIT everywhere."""
    api, d = proj
    _verify(d, ls_on=[RAIL])              # load_en passed HB: it ships on, so it counts
    g, cells = _grid(api)
    assert g["graded_by"] == "verify" and not g["why"]
    # zout (yellow) is fitted at both temperatures, load_en (red) only at 125 C
    assert cells == {("tt", -40.0): "yellow", ("tt", 125.0): "red"}
    s = api.model_summary("p1")
    assert s["graded_by"] == "verify"


def test_a_temperature_specific_grade_wins_over_the_corner_one(proj):
    api, d = proj
    grades = [dict(g) for g in GRADES if g["block"] != "load_en"] + [
        {"port": RAIL, "corner": "tt", "temp_c": 125.0, "block": "load_en", "grade": "red"},
        {"port": RAIL, "corner": "tt", "temp_c": -40.0, "block": "load_en", "grade": "green"}]
    _verify(d, grades, ls_on=[RAIL])
    _g, cells = _grid(api)
    assert cells == {("tt", -40.0): "yellow", ("tt", 125.0): "red"}


def test_the_banner_stays_while_a_block_is_only_fit_graded(proj):
    api, d = proj
    _verify(d, [g for g in GRADES if g["block"] != "dc"])
    g, cells = _grid(api)
    assert g["graded_by"] == "partial"
    assert "provisional" in g["why"] and f"{RAIL}.dc" in g["why"]
    assert g["ungraded"] == [f"{RAIL}.dc"]


def test_a_verify_older_than_the_fit_is_not_shown_as_the_verdict(proj):
    api, d = proj
    _verify(d)
    earlier = time.time() - 60
    os.utime(d / "verify.json", (earlier, earlier))
    g, cells = _grid(api)
    assert g["graded_by"] == "fit" and g["verify_stale"]
    assert "re-run verify" in g["why"]
    assert set(cells.values()) == {"fitted"}


def test_the_cell_table_uses_the_same_join_and_names_vset_and_load(proj):
    api, d = proj
    _verify(d)
    c = api.model_cell("p1", RAIL, "tt", "-40")
    assert c["graded_by"] == "verify"
    rows = {(b["name"], b["vset"]): b for b in c["blocks"]}
    assert set(rows) == {("zout", 3), ("dc", 3), ("dc", 4)}
    assert rows[("zout", 3)]["grade"] == "yellow"
    assert rows[("dc", 4)]["load"] == "1 mA" and rows[("dc", 4)]["temp"] == "sweep"
    assert rows[("zout", 3)]["limit"].startswith("<=")
    # two dc rows that differ only by VSET carry different cell keys: the chart can tell them apart
    assert rows[("dc", 3)]["cell_key"] != rows[("dc", 4)]["cell_key"]


def test_hb_health_is_read_from_hb_check(proj):
    api, d = proj
    assert api.model_summary("p1")["hb"] is None                  # verify never ran
    _verify(d, hb={"status": "not_run", "engine": "fake",
                   "notes": ["no simulator: the 'fake' engine runs no solver"]})
    hb = api.model_summary("p1")["hb"]
    assert hb["ran"] is False and hb["ok"] is False and "fake" in hb["note"]
    _verify(d, hb={"status": "pass", "engine": "spectre", "ls_default_on": [RAIL],
                   "baseline": {"first_step": 7.7e-3},
                   "terms": [{"term": f"load_en_{RAIL}", "pass": True}]})
    hb = api.model_summary("p1")["hb"]
    assert hb["ran"] and hb["ok"] and "all 1" in hb["detail"] and RAIL in hb["note"]


def test_the_valid_range_keeps_freq_and_vset_after_verify(proj):
    """envelope.json's keys are freq_max_hz / vset_codes; reading freq_hz_max / vset dropped
    both lines the moment verify ran."""
    api, d = proj
    _verify(d)
    v = api.model_summary("p1")["valid"]
    assert v["freq"] == "<= 1 GHz"
    assert v["VSET"] == "3, 4 (nominal 3)"
    assert v["temp"] == "-40 - 125 C (continuous)"
    assert v[f"load {RAIL}"] == "2 uA - 1 mA"


def test_envelope_text_reads_every_key_the_envelope_writes():
    from pmukit.deliverable import Envelope
    env = Envelope(freq_max_hz=2e10, load_a={"A": (1e-3, 1e-3)}, temp_c=(25, 25),
                   corners=["tt"], vset_codes=[3], ls_default_on=[]).to_json()
    text = server._envelope_text(env)
    assert text == {"load A": "1 mA only", "temp": "25 C only", "freq": "<= 20 GHz",
                    "corners": "tt", "VSET": "3"}


def test_a_temperature_law_is_drawn_linear_in_its_own_unit():
    assert server._curve_units("dc_temp", "rail")[0] == "V"
    assert server._curve_units("dc_temp", "bias")[0] == "A"
    assert server._curve_units("ac_zout", "rail")[0] == "ohm"


# --------------------------------------------------------------------------- default-off blocks
def test_a_block_that_ships_off_is_graded_and_named_but_does_not_colour_the_cell(proj):
    """load_en is OFF in the delivered model until the HB check clears it. Its red is real and
    stays on screen -- beside the cell, with the switch that turns it on -- but a consumer who
    instantiates the model as delivered never meets it, so it must not paint the cell FAIL."""
    api, d = proj
    _verify(d)                                           # HB not run: every ls term is off
    g, cells = _grid(api)
    assert cells == {("tt", -40.0): "yellow", ("tt", 125.0): "yellow"}
    hot = [c for c in g["rows"][0]["cells"] if c["temp_c"] == 125.0][0]
    assert [o["block"] for o in hot["off"]] == ["load_en"]
    off = hot["off"][0]
    assert off["grade"] == "red" and off["switch"] == f"load_en_{RAIL}=1"
    assert "off by default" in off["note"] and "FAIL" in off["note"]

    c = api.model_cell("p1", RAIL, "tt", "125")
    assert c["grade"] == "yellow"
    assert [o["block"] for o in c["off_by_default"]] == ["load_en"]
    row = [b for b in c["blocks"] if b["name"] == "load_en"][0]
    assert row["default_off"] is True and row["grade"] == "red"
    assert row["switch"] == f"load_en_{RAIL}=1" and "off by default" in row["off_note"]
    assert all(not b["default_off"] for b in c["blocks"] if b["name"] != "load_en")

    usable = {u["item"]: u["note"] for u in api.model_summary("p1")["usable_not_signoff"]}
    assert "off by default" in usable[f"{RAIL}.load_en"]


def test_once_hb_clears_the_term_it_counts_again(proj):
    api, d = proj
    _verify(d, hb={"status": "pass", "ls_default_on": [RAIL], "terms": []})
    _g, cells = _grid(api)
    assert cells[("tt", 125.0)] == "red"
    assert api.model_cell("p1", RAIL, "tt", "125")["off_by_default"] == []


# --------------------------------------------------------------------------- held reasons
HELD_FIT = {"project": "p1", "ports": {RAIL: "rail"}, "dataset_sha": "abc", "fits": {
    f"{RAIL}/zout/tt/25C/vset3/1.0e-03A": dict(
        _bf("zout", {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": 1e-3}, 0.039),
        params={"Ra": 0.05, "Rpl": 3.2e4}, identifiability={"unidentifiable": ["Rpl"]}),
}}


def test_a_held_grade_says_why_on_the_row_and_marks_the_grid(tmp_path):
    """zout at 0.039 dB against a 1 dB limit shows MARG; the reason -- the data does not pin
    Rpl -- used to live only in a hover tooltip."""
    from pmukit.fit._base import BlockFit
    from pmukit.verify import grades as G

    d = tmp_path / "p1"
    d.mkdir()
    jsonio.write(d / "fit.json", HELD_FIT)
    bf = BlockFit.from_dict(next(iter(HELD_FIT["fits"].values())))
    grade, detail = G.grade_block(bf)
    assert grade == "yellow" and G.is_held(detail)
    _verify(d, [{"port": RAIL, "corner": "tt", "block": "zout", "grade": grade,
                 "detail": detail, "score": 0.039}])
    api = server.Api(root=tmp_path)

    g = api.model_grades("p1")
    cell = g["rows"][0]["cells"][0]
    assert cell["grade"] == "yellow" and cell["held"] is True and cell["held_by"] == ["zout"]

    c = api.model_cell("p1", RAIL, "tt", "25")
    assert c["held"] is True
    row = c["blocks"][0]
    assert row["held"] is True and row["held_by"] == ["Rpl"]
    assert row["reason"].startswith("held at yellow") and "Rpl" in row["reason"]
    assert "held at yellow" in row["reason_full"]


def test_a_non_green_row_carries_a_short_reason(proj):
    api, d = proj
    _verify(d, ls_on=[RAIL])
    rows = api.model_cell("p1", RAIL, "tt", "125")["blocks"]
    by = {b["name"]: b for b in rows}
    assert by["load_en"]["reason"] == "outside the acceptance limit"
    assert by["load_en"]["reason_full"]


# --------------------------------------------------------------------------- chart axes
def test_every_curve_has_a_concrete_unit_and_the_right_axis():
    """A dB quantity drawn on a log axis is the log of a log. dB -> linear y; a magnitude
    spanning decades -> log y; a DC law -> linear. No unit is 'A or V'."""
    for (ptype, obs), (unit, label, scale) in server.CURVE_AXES.items():
        assert unit and " or " not in unit and label, (ptype, obs)
        assert scale in ("log", "db", "linear"), (ptype, obs)
        assert (unit == "dB") == (scale == "db"), (ptype, obs)
    assert server._curve_axis("ac_psrr", "rail")[2] == "db"
    assert server._curve_axis("ac_zout", "rail")[2] == "log"
    assert server._curve_axis("noise_v", "rail")[2] == "log"
    assert server._curve_axis("noise_i", "bias")[2] == "log"
    assert server._curve_axis("dc_temp", "rail")[2] == "linear"
    # every observable the curve view draws has a row, for its own port type
    for (ptype, block), obs in server.CURVE_OBSERVABLE.items():
        assert (ptype, obs) in server.CURVE_AXES, (ptype, block)
    assert server._to_db([1.0, 0.001, 0.0, None]) == [0.0, -60.0, None, None]


# --------------------------------------------------------------------------- machine probes
@pytest.fixture
def no_ssh(monkeypatch):
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        return 255, "", "ssh: connect to host ewave-vm port 22: Connection timed out"
    monkeypatch.setattr(server, "_run_cmd", run)
    return calls


@pytest.mark.parametrize("engine", ["fake", "dry_run"])
def test_a_simulator_free_engine_probes_nothing_and_flags_nothing(tmp_path, monkeypatch,
                                                                  no_ssh, engine):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    monkeypatch.setenv("PMUKIT_ENGINE", engine)
    m = server.machine(deadline=10)
    assert no_ssh == [], "a fake engine must never ssh anywhere"
    assert m["ready"] and m["engine"] == engine
    for p in m["probes"]:
        assert p["ok"] and p["needed"] is False, p
        assert "not needed" in p["detail"] or engine in p["detail"]


def test_spectre_ssh_probes_the_sites_host_and_not_the_local_licence(tmp_path, monkeypatch,
                                                                     no_ssh):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    monkeypatch.setenv("PMUKIT_ENGINE", "spectre_ssh")
    monkeypatch.setenv("PMUKIT_SSH_HOST", "my-vm")
    for v in ("CDS_LIC_FILE", "LM_LICENSE_FILE", "EMPYREAN_LICENSE_FILE"):
        monkeypatch.delenv(v, raising=False)
    m = server.machine(deadline=10)
    probes = {p["name"]: p for p in m["probes"]}
    assert no_ssh and "my-vm" in no_ssh[0]
    assert not probes["engine"]["ok"] and "my-vm" in probes["engine"]["reason"]["what"]
    assert probes["queue"]["needed"] is False
    assert probes["license"]["needed"] is False and "my-vm" in probes["license"]["detail"]


def test_donau_alps_reads_the_local_facts_and_never_sshs(tmp_path, monkeypatch, no_ssh):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    monkeypatch.setenv("PMUKIT_ENGINE", "donau_alps")
    m = server.machine(deadline=10)
    assert no_ssh == [] or all("ssh" not in c[0] for c in no_ssh)
    assert {p["name"] for p in m["probes"]} == {"engine", "queue", "pdk", "license"}
    assert all(p.get("needed", True) for p in m["probes"])
