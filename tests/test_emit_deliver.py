"""M7: `deliver()` -- the whole contract-4 deliverable, and the pieces around the .va body.

The container itself belongs to `pmukit.deliverable` (tested there).  What is tested here is what
the emitter puts INTO it: one .va per corner, the section library, the validity envelope built
from what was actually characterized, the conditioning gate's verdict, and the honest degradation
when the fitter has not landed yet.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from pmukit import jsonio
from pmukit.deliverable import Deliverable, Grade, Provenance
from pmukit.emit import HB_CHECK_NAME, deliver, lint, scs
from pmukit.emit.primitives import FORBIDDEN, WHITELIST
from pmukit.errors import PmuError
from tests.test_emit_va import (BIAS, CORNERS, F_MAX, RAIL_A, RAIL_B, STUB, code_only,
                                demo_derived, demo_fits)


def _deliver(tmp_path, **kw):
    kw.setdefault("fit", demo_fits())
    kw.setdefault("derived", demo_derived())
    kw.setdefault("stamp", "20260916-000000")
    return deliver("demo_pmu", root=tmp_path, **kw)


# --------------------------------------------------------------------------- the directory
def test_deliver_writes_every_contract_4_file(tmp_path):
    out = _deliver(tmp_path)
    names = sorted(p.name for p in out.iterdir())
    assert "PMU_demo_pmu.scs" in names
    for corner in CORNERS:
        assert f"PMU_demo_pmu_{corner}.va" in names
    for n in ("envelope.json", "report.md", "grades.json", "provenance.json", HB_CHECK_NAME):
        assert n in names


def test_every_va_repeats_the_provenance_header(tmp_path):
    prov = Provenance.now(config_sha="cfg123", dataset_sha="ds456", spec_sha="spec789",
                          tb_state_note="RX mode, register 0x12 = 0x03")
    out = _deliver(tmp_path, provenance=prov)
    for corner in CORNERS:
        text = (out / f"PMU_demo_pmu_{corner}.va").read_text(encoding="utf-8")
        assert "cfg123" in text and "ds456" in text and "spec789" in text
        assert "RX mode" in text
        assert f"module PMU_demo_pmu_{corner}(" in text


def test_files_are_lf_only(tmp_path):
    """The .va and the .scs are consumed on Linux."""
    out = _deliver(tmp_path)
    for p in out.iterdir():
        if p.suffix in (".va", ".scs", ".md", ".txt", ".json"):
            assert b"\r" not in p.read_bytes(), p.name


def test_section_library_selects_the_corner(tmp_path):
    out = _deliver(tmp_path)
    text = (out / "PMU_demo_pmu.scs").read_text(encoding="utf-8")
    assert "library PMU_demo_pmu" in text and "endlibrary PMU_demo_pmu" in text
    for corner in CORNERS:
        assert f"section {corner}" in text
        assert f'ahdl_include "PMU_demo_pmu_{corner}.va"' in text
        assert f"module PMU_demo_pmu_{corner} (process corner {corner})" in text
    # the consumer's one include line, and the valid range, are spelled out
    assert "include" in text and "section=" in text
    assert "valid:" in text


def test_scs_extra_lines_are_comments_only(tmp_path):
    lines = scs.extra_lines({"tt": "PMU_x_tt"}, ports_by_corner={"tt": ["A", "B"]})
    for key, block in lines.items():
        for ln in block:
            assert ln.lstrip().startswith("//"), f"{key}: {ln!r} would enter the consumer netlist"


# --------------------------------------------------------------------------- the envelope
def test_envelope_is_what_was_characterized(tmp_path):
    out = _deliver(tmp_path)
    env = jsonio.read(out / "envelope.json")
    assert env["freq_max_hz"] == F_MAX
    assert env["corners"] == list(CORNERS)
    assert env["vset_codes"] == [2, 3]
    assert env["temp_c"] == [-40.0, 125.0]
    assert env["load_a"][RAIL_A] == [2.0e-6, 1.0e-3]
    assert env["load_a"][RAIL_B] == [1.0e-5, 2.0e-3]
    # the large-signal tier is OFF by default until the HB check signs it off
    assert env["ls_default_on"] == []
    assert any("OFF by default" in n for n in env["notes"])


def test_ls_default_on_flows_into_the_envelope_and_the_module(tmp_path):
    out = _deliver(tmp_path, ls_default_on=[RAIL_A], stamp="20260916-000001")
    env = jsonio.read(out / "envelope.json")
    assert env["ls_default_on"] == [RAIL_A]
    va = (out / "PMU_demo_pmu_tt.va").read_text(encoding="utf-8")
    assert f"parameter real load_en_{RAIL_A} = 1.000000e+00" in va


def test_stub_and_skipped_ports_are_red_in_the_report(tmp_path):
    fits = demo_fits()
    fits[RAIL_B].pop("zout")
    out = _deliver(tmp_path, fit=fits)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert f"**RED:** {STUB} -- stub, not modeled" in report
    assert RAIL_B in report and "Zout block is missing" in report


# --------------------------------------------------------------------------- the HB verdict
def test_hb_ready_when_the_lint_passes(tmp_path):
    out = _deliver(tmp_path)
    grades = jsonio.read(out / "grades.json")
    assert grades["hb_check"]["status"] == "pass"
    hb = jsonio.read(out / "hb_check.json")
    assert hb["hb_ready"] is True
    txt = (out / HB_CHECK_NAME).read_text(encoding="utf-8")
    for corner in CORNERS:
        assert f"### corner {corner}" in txt
    # the whitelist and its reasons travel with the deliverable
    for name in WHITELIST:
        assert f"- {name}:" in txt
    for pat in FORBIDDEN:
        assert pat in txt


def test_deliver_refuses_to_call_a_diagnostic_build_hb_ready(tmp_path):
    out = _deliver(tmp_path, hb_robust=False)
    hb = jsonio.read(out / "hb_check.json")
    assert hb["hb_ready"] is False
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "**RED:** status `fail`" in report
    assert "must never ship" in (out / HB_CHECK_NAME).read_text(encoding="utf-8")
    # and the refuted realization really is in there, which is what the lint fired on
    va = (out / "PMU_demo_pmu_tt.va").read_text(encoding="utf-8")
    assert "SYNTHESIZED" in va


def test_grades_are_accepted_not_invented(tmp_path):
    """`pmukit.verify` is a later milestone: with no grades the report says so, it does not
    manufacture a verdict."""
    out = _deliver(tmp_path)
    assert jsonio.read(out / "grades.json")["grades"] == []
    assert "no block was graded in this deliverable" in \
        (out / "report.md").read_text(encoding="utf-8")
    out2 = _deliver(tmp_path, stamp="20260916-000002",
                    grades=[Grade(RAIL_A, "tt", "zout", "green", "matches across the band", 0.4)])
    assert jsonio.read(out2 / "grades.json")["grades"][0]["grade"] == "green"


def test_deliverable_reads_back(tmp_path):
    out = _deliver(tmp_path)
    d = Deliverable.open(out)
    assert d.envelope.freq_max_hz == F_MAX
    assert d.provenance.spec_sha
    inside, why = d.envelope.contains(port=RAIL_A, load_a=5.0e-4, corner="tt", vset=3,
                                      temp_c=25.0, freq_hz=1e6)
    assert inside, why
    inside, why = d.envelope.contains(port=RAIL_A, freq_hz=1e11)
    assert not inside and "above the characterized ceiling" in why[0]


# --------------------------------------------------------------------------- degradation
def test_no_fitter_gives_a_four_part_error(tmp_path):
    with pytest.raises(PmuError) as e:
        deliver("demo_pmu", root=tmp_path, derived=demo_derived(), fit=None)
    err = e.value.to_dict()["error"]
    assert err["what"] and err["why"] and err["do"] and err["where"]
    assert "fit=" in " ".join(err["do"])


def test_missing_derived_config_gives_a_four_part_error(tmp_path):
    with pytest.raises(PmuError) as e:
        deliver("nosuch", root=tmp_path, fit=demo_fits())
    assert "derived" in e.value.where


def test_no_corner_is_refused(tmp_path):
    d = demo_derived()
    d.process = {"corners": []}
    with pytest.raises(PmuError) as e:
        deliver("demo_pmu", root=tmp_path, derived=d, fit=demo_fits())
    assert "corner" in e.value.what


# --------------------------------------------------------------------------- the lint report
def test_lint_report_is_structured_and_renders(tmp_path):
    from pmukit.emit import build_va
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    assert set(rep) >= {"ok", "status", "findings", "per_frequency", "summary",
                        "worst_node_range", "n_elements"}
    json.dumps(rep)                     # it has to survive the JSON sidecar
    assert rep["frequencies"] == [F_MAX, F_MAX * lint.HARMONIC]
    assert "PASS" in lint.render(rep)


def test_the_real_fitter_output_goes_straight_into_the_emitter(tmp_path):
    """Integration: `fit_project` -> `build_va`, with no adapter in between.

    The point is the DATA CONTRACT, not the numbers: a `FitResult` is what the pipeline actually
    hands over, and every parameter name the emitter reads has to be one the fitter writes.
    """
    import numpy as np

    from pmukit.config import DerivedConfig
    from pmukit.emit import build_va
    from pmukit.fit import fit_project
    from tests.test_fit_common import BIAS as FBIAS, LOADS, RAIL as FRAIL
    from tests.test_fit_driver import build as build_ds

    ds = build_ds(tmp_path)
    # the AC-only fixture has no DC block; a rail with no measured vout is refused on purpose,
    # so give it the load regulation the real flow measures
    ds.declare(f"dc_load.{FRAIL}", dims=("process", "temp_c", "vset", "load_a"), dtype="float64",
               unit="V")
    for il in LOADS[1:3]:
        ds.put(f"dc_load.{FRAIL}", {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": il},
               np.asarray(0.80 - 0.05 * il))
    derived = DerivedConfig(
        project="demo_pmu", config_sha="c0ffee123456",
        process={"corners": ["tt"]}, temps_c={"points": [25.0]}, vset={"codes": [3]},
        freq={"start_hz": 10.0, "stop_hz": 1.0e9}, noise={"start_hz": 10.0, "stop_hz": 1.0e8},
        supply={"pins": {"VSUP_A": {"nominal_v": 1.05}}, "nominal_v": 1.05},
        rails={FRAIL: {"gnd": "VSS_A", "i_typ_a": 5.0e-4}},
        biases={FBIAS: {"gnd": "AGND", "vcomp_v": 0.4}},
        grounds={"by_pin": {"VSUP_A": "VSS", FRAIL: "VSS_A", FBIAS: "AGND"}},
        loads={FRAIL: {"points_a": [1.0e-4, 5.0e-4]}})
    res = fit_project(ds, derived)
    assert res.fits, "the fitter produced nothing to emit"

    built = build_va(res, derived, "tt", project="demo_pmu")
    assert FRAIL in built["rails"], built["skipped"]
    assert FBIAS in built["biases"], built["skipped"]

    # the numbers that reached the module are the numbers the fitter produced
    z = res.get(FRAIL, "zout", {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": 5e-4})
    assert f"{z.params['Ra']:.6e}" in built["text"]
    dc = res.get(FRAIL, "dc", {"process": "tt", "vset": 3, "load_a": 5e-4})
    assert f"{dc.params['vout'] + z.params['Ra'] * 5.0e-4:.6e}" in built["text"]
    idc = res.get(FBIAS, "idc", {"process": "tt", "vset": 3})
    assert f"{abs(idc.params['idc']):.6e}" in built["text"]

    rep = lint.report(built["netlist"], f_max_hz=1.0e9, grounds=built["grounds"])
    assert rep["ok"], lint.render(rep)


def test_a_missing_fit_never_becomes_a_fabricated_parameter(tmp_path):
    """A `missing` BlockFit carries no params, and the emitter must not invent any."""
    from pmukit.emit.va import normalize_fits
    from pmukit.fit import BlockFit
    fits = normalize_fits([
        BlockFit(port=RAIL_A, block="zout", cell={"process": "tt"}, params={"Ra": 1.0}),
        BlockFit(port=RAIL_A, block="psrr", cell={"process": "tt"}, params={}, missing=True),
    ])
    assert (RAIL_A, "zout") in fits
    assert (RAIL_A, "psrr") not in fits


def test_lint_harmonic_multiple_is_configurable(tmp_path):
    from pmukit.emit import build_va
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu", hb_robust=False)
    r16 = lint.report(built["netlist"], f_max_hz=F_MAX, harmonic=16, grounds=built["grounds"])
    r64 = lint.report(built["netlist"], f_max_hz=F_MAX, harmonic=64, grounds=built["grounds"])
    assert r64["worst_node_range"] >= r16["worst_node_range"]
    assert not r16["ok"] and not r64["ok"]
