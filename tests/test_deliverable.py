"""Contract 4: the deliverable container -- layout, section library, envelope, report, reader."""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from pmukit.deliverable import (Deliverable, DeliverableWriter, Envelope, Grade, Provenance)
from pmukit.errors import PmuError

CORNERS = ["tt", "ss", "ff"]


def make_envelope(**kw) -> Envelope:
    base = dict(
        freq_max_hz=2e10,
        load_a={"VDD0P8_A": (2e-6, 1e-3), "VDD0P8_B": (2e-5, 4e-3)},
        temp_c=(-40.0, 125.0),
        corners=list(CORNERS),
        vset_codes=[3],
        ls_default_on=["load_en_A"],
        notes=["EN power-up ramp is usable, not signed off."],
    )
    base.update(kw)
    return Envelope(**base)


def make_provenance(**kw) -> Provenance:
    base = dict(config_sha="5d8ca1", dataset_sha="a91fc3", spec_sha="77c003",
                pmukit_version="0.1.0", created="2026-09-15T14:02:11Z",
                tb_state_note="RX mode, register 0x12 = 0x03")
    base.update(kw)
    return Provenance(**base)


def make_grades() -> list[Grade]:
    return [
        Grade("VDD0P8_A", "tt", "zout", "green", "matches the reference across the band", 0.42),
        Grade("VDD0P8_A", "tt", "psrr", "green", "matches the reference across the band", 0.31),
        Grade("VDD0P8_A", "ss", "zout", "yellow", "high-ESR cap barely visible in the sweep", 3.81),
        Grade("VDD0P8_B", "ss", "noise", "red", "the fit misses the flicker corner", 12.5),
        Grade("VDD0P8_B", "ff", "zout", "not_run", "the sweep behind it never ran", None),
    ]


_DEFAULT = object()


def build(tmp_path, *, stamp="20260915-140211", corners=CORNERS, with_envelope=True,
          grades=None, not_run=("tran_load_off.VDD0P8_B @ ss / 125 C",), stubs=("VDD0P8_C",),
          envelope=None, provenance=None, hb=_DEFAULT):
    env = envelope or make_envelope()
    prov = provenance or make_provenance()
    if hb is _DEFAULT:
        hb = {"status": "pass", "first_step_residual": "7.7e-3"}
    w = DeliverableWriter("demo_pmu", root=tmp_path, stamp=stamp)
    for corner in corners:
        w.add_va(corner, f"module PMU_demo_pmu_{corner}(); endmodule\n", provenance=prov)
    w.write_scs(provenance=prov)
    if with_envelope:
        w.write_envelope(env)
    w.write_provenance(prov)
    w.write_report(envelope=env, grades=make_grades() if grades is None else grades,
                   hb_check=hb, not_run=list(not_run), stubs=list(stubs))
    return w, env, prov


# --------------------------------------------------------------------------- layout
def test_write_three_corners_and_read_back(tmp_path):
    w, env, prov = build(tmp_path)
    assert w.finish() == w.path

    d = Deliverable.open(w.path)
    assert d.stamp == "20260915-140211"
    assert d.files() == sorted([
        "PMU_demo_pmu.scs", "PMU_demo_pmu_tt.va", "PMU_demo_pmu_ss.va", "PMU_demo_pmu_ff.va",
        "envelope.json", "grades.json", "provenance.json", "report.md"])
    assert d.envelope.to_json() == env.to_json()
    assert d.provenance.to_json() == prov.to_json()
    assert Deliverable.latest("demo_pmu", tmp_path).path == w.path
    assert [x.stamp for x in Deliverable.list("demo_pmu", tmp_path)] == ["20260915-140211"]


def test_list_is_newest_first(tmp_path):
    build(tmp_path, stamp="20260101-000000")
    build(tmp_path, stamp="20260915-140211")
    assert [d.stamp for d in Deliverable.list("demo_pmu", tmp_path)] == \
        ["20260915-140211", "20260101-000000"]


def test_every_text_file_is_lf(tmp_path):
    w, _, _ = build(tmp_path)
    for name in w.path.iterdir():
        assert b"\r" not in name.read_bytes(), name


def test_finish_catches_a_missing_envelope(tmp_path):
    w, _, _ = build(tmp_path, with_envelope=False)
    with pytest.raises(PmuError) as e:
        w.finish()
    assert "envelope.json" in str(e.value)
    assert e.value.do and e.value.where == str(w.path)


def test_finish_catches_a_missing_va(tmp_path):
    prov = make_provenance()
    w = DeliverableWriter("demo_pmu", root=tmp_path, stamp="20260915-140211")
    w.add_va("tt", "module m(); endmodule\n", provenance=prov)
    w.write_scs(provenance=prov)
    w.write_envelope(make_envelope())
    w.write_provenance(prov)
    w.write_report(envelope=make_envelope(), grades=[], not_run=[])
    (w.path / "PMU_demo_pmu_tt.va").unlink()
    with pytest.raises(PmuError) as e:
        w.finish()
    assert ".va" in str(e.value)


def test_corner_name_must_be_an_identifier(tmp_path):
    w = DeliverableWriter("demo_pmu", root=tmp_path, stamp="s1")
    with pytest.raises(PmuError) as e:
        w.add_va("../evil", "body", provenance=make_provenance())
    assert "is not a plain identifier" in e.value.what
    assert "[A-Za-z_][A-Za-z0-9_]*" in e.value.why


# --------------------------------------------------------------------------- .scs + headers
def test_scs_has_one_section_per_corner_including_its_own_va(tmp_path):
    w, _, _ = build(tmp_path)
    scs = (w.path / "PMU_demo_pmu.scs").read_text(encoding="utf-8")
    assert "library PMU_demo_pmu" in scs and "endlibrary PMU_demo_pmu" in scs
    for corner in CORNERS:
        assert f"section {corner}\n" in scs
        assert f'    ahdl_include "PMU_demo_pmu_{corner}.va"' in scs
        assert f"endsection {corner}\n" in scs
    assert scs.count("section ") == 2 * len(CORNERS)          # section + endsection per corner


def test_every_va_starts_with_the_provenance_header(tmp_path):
    w, _, prov = build(tmp_path)
    header = prov.header_text("//")
    for corner in CORNERS:
        text = (w.path / f"PMU_demo_pmu_{corner}.va").read_text(encoding="utf-8")
        assert text.startswith(header)
        assert "config sha  : 5d8ca1" in text
        assert "RX mode, register 0x12 = 0x03" in text
        assert len(header.splitlines()) <= 12


def test_scs_extra_lines_must_name_a_real_corner(tmp_path):
    prov = make_provenance()
    w = DeliverableWriter("demo_pmu", root=tmp_path, stamp="s1")
    w.add_va("tt", "body", provenance=prov)
    w.write_scs(provenance=prov, extra_lines={"tt": ["parameters vset=3"], "*": ["// shared"]})
    scs = (w.path / "PMU_demo_pmu.scs").read_text(encoding="utf-8")
    assert "    parameters vset=3" in scs and "    // shared" in scs
    with pytest.raises(PmuError) as e:
        w.write_scs(provenance=prov, extra_lines={"ss": ["oops"]})
    assert "ss" in e.value.what


def test_write_scs_without_a_va_refuses(tmp_path):
    w = DeliverableWriter("demo_pmu", root=tmp_path, stamp="s1")
    with pytest.raises(PmuError):
        w.write_scs(provenance=make_provenance())


# --------------------------------------------------------------------------- envelope
def test_envelope_contains_finds_every_axis():
    env = make_envelope()
    inside, why = env.contains(freq_hz=1e9, temp_c=25, port="VDD0P8_A", load_a=5e-4,
                               corner="tt", vset=3)
    assert inside and why == []

    inside, why = env.contains(freq_hz=3e10, temp_c=150, port="VDD0P8_X", load_a=9.0,
                               corner="xx", vset=7)
    assert not inside
    joined = " | ".join(why)
    assert "frequency" in joined and "temperature" in joined
    assert "VDD0P8_X" in joined and "corner" in joined and "VSET" in joined
    assert len(why) == 5          # the unknown port swallows its own load check

    # one axis at a time, each naming itself
    assert env.contains(freq_hz=2e10)[0]                      # exactly at the ceiling: inside
    assert not env.contains(freq_hz=2.1e10)[0]
    assert "temperature" in env.contains(temp_c=-41)[1][0]
    assert "load" in env.contains(port="VDD0P8_A", load_a=2e-3)[1][0]
    assert "without a port" in env.contains(load_a=1e-3)[1][0]
    assert "corner" in env.contains(corner="nn")[1][0]
    assert "VSET" in env.contains(vset=4)[1][0]


def test_envelope_round_trips_through_json():
    env = make_envelope()
    back = Envelope.from_json(json.loads(json.dumps(env.to_json())))
    assert back.to_json() == env.to_json()
    assert back.load_a["VDD0P8_A"] == (2e-6, 1e-3)
    assert back.temp_c == (-40.0, 125.0)


def test_envelope_missing_key_is_a_four_part_error():
    with pytest.raises(PmuError) as e:
        Envelope.from_json({"freq_max_hz": 1e9})
    assert e.value.what and e.value.why and e.value.do and e.value.where == "envelope.json"


def test_unknown_grade_refused():
    with pytest.raises(PmuError):
        Grade("VDD0P8_A", "tt", "zout", "purple")


# --------------------------------------------------------------------------- report.md
FIXED_ITEMS = ("**Valid range:**", "**Usable, not signed off:**", "**Never run:**",
               "**Trust per corner and rail:**")
SCORE_RE = re.compile(r"\d\.\d{2,}|\d+\.?\d*e[+-]?\d+", re.I)


def test_report_first_paragraph_has_the_four_fixed_items(tmp_path):
    w, _, _ = build(tmp_path)
    text = (w.path / "report.md").read_text(encoding="utf-8")
    head = text.split("\n## ", 1)[0]
    for item in FIXED_ITEMS:
        assert item in head, item
    assert "load per rail VDD0P8_A 2 uA to 1 mA" in head
    assert "frequency up to 20 GHz" in head
    assert "**Usable, not signed off:** EN power-up ramp is usable, not signed off." in head
    assert "temperature -40 to 125 C" in head
    assert "corners tt, ss, ff" in head
    assert "VSET codes 3" in head


def test_rail_table_carries_no_score_numbers_but_grades_json_does(tmp_path):
    w, _, _ = build(tmp_path)
    text = (w.path / "report.md").read_text(encoding="utf-8")
    table = text.split("## Trust per corner and rail", 1)[1].split("\n## ", 1)[0]
    rows = [r for r in table.splitlines() if r.startswith("|") and not r.startswith("|---")]
    assert len(rows) == 1 + 4          # header + one line per (rail, corner)
    for row in rows:
        assert not SCORE_RE.search(row), row
    assert "| green |" in table and "| yellow |" in table
    assert "| red | noise | **RED:** the fit misses the flicker corner |" in table
    assert "| not_run | zout | **RED:** the sweep behind it never ran |" in table

    sidecar = json.loads((w.path / "grades.json").read_text(encoding="utf-8"))
    assert [g["score"] for g in sidecar["grades"]] == [0.42, 0.31, 3.81, 12.5, None]
    assert sidecar["not_run"] == ["tran_load_off.VDD0P8_B @ ss / 125 C"]
    assert sidecar["stubs"] == ["VDD0P8_C"]


def test_a_default_off_block_does_not_colour_the_rail_row():
    """The report's rail row follows verify.grades.headline, like the Model screen: a load_en
    that ships switched off is named under the table but does not turn the row red."""
    from pmukit.deliverable import render_report
    grades = [Grade("VDD0P8_B", "tt", "zout", "green", "", 0.4),
              Grade("VDD0P8_B", "tt", "load_en", "red", "droop 208 %", 208.0)]
    text = render_report(project="p", stamp="s", envelope=make_envelope(ls_default_on=[]),
                         grades=grades, hb_check={"status": "pass"}, not_run=[], stubs=[])
    table = text.split("## Trust per corner and rail", 1)[1].split("\n## ", 1)[0]
    assert "| VDD0P8_B | tt | green | zout |" in table
    assert "off by default: load_en FAIL" in table and "load_en_VDD0P8_B=1" in table
    # switched on by the HB check, it counts again
    text = render_report(project="p", stamp="s",
                         envelope=make_envelope(ls_default_on=["VDD0P8_B"]),
                         grades=grades, hb_check={"status": "pass"}, not_run=[], stubs=[])
    assert "| VDD0P8_B | tt | red | load_en |" in text


def test_report_marks_never_run_and_stubs_red(tmp_path):
    w, _, _ = build(tmp_path)
    text = (w.path / "report.md").read_text(encoding="utf-8")
    assert "**RED:** tran_load_off.VDD0P8_B @ ss / 125 C -- never run" in text
    assert "**RED:** VDD0P8_C -- stub, not modeled" in text
    assert "## HB health check" in text and "status `pass`" in text
    tier = text.split("## Usable but not signed off", 1)[1].split("\n## ", 1)[0]
    assert "- EN power-up ramp is usable, not signed off." in tier
    assert "Large-signal terms on by default (each passed the HB health check): load_en_A." in tier


def test_report_marks_a_cell_outside_the_envelope_red(tmp_path):
    grades = [Grade("VDD0P8_A", "hot", "zout", "green", "fits well")]
    w, _, _ = build(tmp_path, grades=grades, not_run=(), stubs=())
    text = (w.path / "report.md").read_text(encoding="utf-8")
    assert "## Outside the validity envelope" in text
    assert "corner 'hot' was not characterized" in text
    assert text.count("**RED:**") >= 2


def test_report_without_an_hb_check_is_red(tmp_path):
    w, _, _ = build(tmp_path, hb=None)
    text = (w.path / "report.md").read_text(encoding="utf-8")
    assert "**RED:** the harmonic-balance health check was never run" in text


# --------------------------------------------------------------------------- reader
def test_read_file_refuses_traversal_and_absolute_paths(tmp_path):
    w, _, _ = build(tmp_path)
    d = Deliverable.open(w.path)
    assert d.read_file("report.md").startswith("# demo_pmu")
    for bad in ("../report.md", "..", "sub/report.md", r"..\report.md",
                str(tmp_path / "report.md"), "/etc/passwd", "~/report.md"):
        with pytest.raises(PmuError) as e:
            d.read_file(bad)
        assert e.value.do and e.value.why
    with pytest.raises(PmuError):
        d.read_file("no_such_file.txt")


def test_open_refuses_an_incomplete_directory(tmp_path):
    (tmp_path / "demo_pmu" / "deliver" / "empty").mkdir(parents=True)
    with pytest.raises(PmuError) as e:
        Deliverable.open(tmp_path / "demo_pmu" / "deliver" / "empty")
    assert "envelope.json" in e.value.what
    with pytest.raises(PmuError):
        Deliverable.open(tmp_path / "nope")
    assert Deliverable.latest("nobody", tmp_path) is None
    assert Deliverable.list("nobody", tmp_path) == []


def test_diff_reports_provenance_envelope_files_and_grades(tmp_path):
    a_writer, _, _ = build(tmp_path, stamp="20260101-000000")

    env_b = make_envelope(freq_max_hz=1e10, corners=["tt", "ss"])
    prov_b = make_provenance(dataset_sha="beef42", created="2026-09-16T09:00:00Z")
    grades_b = [
        Grade("VDD0P8_A", "tt", "zout", "green", "matches the reference across the band", 0.40),
        Grade("VDD0P8_A", "tt", "psrr", "yellow", "phase drifts above the band edge", 4.2),
        Grade("VDD0P8_A", "ss", "zout", "yellow", "high-ESR cap barely visible", 3.9),
        Grade("VDD0P8_B", "ss", "noise", "red", "the fit misses the flicker corner", 12.5),
    ]
    b_writer, _, _ = build(tmp_path, stamp="20260915-140211", corners=["tt", "ss"],
                           envelope=env_b, provenance=prov_b, grades=grades_b)

    a, b = Deliverable.open(a_writer.path), Deliverable.open(b_writer.path)
    d = a.diff(b)

    assert d["provenance"]["dataset_sha"] == ("a91fc3", "beef42")
    assert "created" in d["provenance"] and "config_sha" not in d["provenance"]
    assert d["envelope"]["freq_max_hz"] == (2e10, 1e10)
    assert d["envelope"]["corners"] == (["tt", "ss", "ff"], ["tt", "ss"])
    assert "load_a" not in d["envelope"]

    assert d["files"]["removed"] == ["PMU_demo_pmu_ff.va"]
    assert d["files"]["added"] == []
    assert "report.md" in d["files"]["changed"] and "grades.json" in d["files"]["changed"]
    assert "PMU_demo_pmu_tt.va" in d["files"]["changed"]          # provenance header changed

    assert d["grades"][("VDD0P8_A", "tt", "psrr")] == ("green", "yellow")
    assert d["grades"][("VDD0P8_B", "ff", "zout")] == ("not_run", None)
    assert ("VDD0P8_A", "tt", "zout") not in d["grades"]          # same grade, different score


def test_diff_of_identical_deliverables_is_empty(tmp_path):
    # same stamp, two data roots: the stamp itself is printed in report.md and grades.json,
    # so two stamps of the same content would legitimately differ.
    a, _, _ = build(tmp_path / "a", stamp="20260101-000000")
    b, _, _ = build(tmp_path / "b", stamp="20260101-000000")
    d = Deliverable.open(a.path).diff(Deliverable.open(b.path))
    assert d["provenance"] == {} and d["envelope"] == {} and d["grades"] == {}
    assert d["files"] == {"added": [], "removed": [], "changed": []}


def test_writer_defaults_to_pmukit_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "data"))
    w = DeliverableWriter("demo_pmu")
    assert w.path.parent.parent == pathlib.Path(tmp_path / "data" / "demo_pmu")
    assert re.match(r"^\d{8}-\d{6}$", w.stamp)
