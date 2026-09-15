"""M7 emitter: the module a consumer's harmonic-balance run has to survive.

The fixtures here are HAND-BUILT from `spec.block(...).params` rather than round-tripped through
the fitter, because for a hand-built model we know the right answer: a known RLC ladder, a known
PSRR bank with a deliberately planted near-cancelling doublet, a known noise bank in both
`norton` and `hybrid` modes, and a known bias I-V.
"""
from __future__ import annotations

import math

import pytest

from pmukit import spec
from pmukit.config import DerivedConfig
from pmukit.emit import build_va, emit_va, lint
from pmukit.emit.primitives import FORBIDDEN, WHITELIST, Netlist, balanced_gain, biquad_from_doublet
from pmukit.emit.va import _consolidate, _flicker_sections
from pmukit.errors import PmuError


def code_only(text: str) -> str:
    """The emitted module with its `//` comments stripped -- what Spectre actually elaborates."""
    out = []
    for line in text.splitlines():
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def statement(text: str, needle: str) -> str:
    """The one `;`-terminated statement containing `needle` (a contribution may span lines)."""
    for st in code_only(text).split(";"):
        if needle in st:
            return st
    raise AssertionError(f"no statement contains {needle!r}")


SUPPLY = "VSUP_A"
RAIL_A = "VDD0P8_A"
RAIL_B = "VDD0P8_B"
BIAS = "IB_PTAT"
STUB = "VDD0P8_C"
CORNERS = ["tt", "ss", "ff"]
F_MAX = 2.0e9

#: A low-frequency complex PSRR section. With the refuted 1 pF R-L-C realization this exact w0
#: forces Lpc = 8890 H -- the inductor the conditioning lint must fire on.
PC_W0 = 1.0 / math.sqrt(8890.0 * 1.0e-12)          # 1.0606e4 rad/s == 1688 Hz


# --------------------------------------------------------------------------- fixtures
def demo_derived(*, project="demo_pmu", stubs=True, f_max=F_MAX) -> DerivedConfig:
    """Contract 0b for a two-rail, one-bias, one-stub PMU with SPLIT grounds."""
    d = DerivedConfig(project=project, config_sha="cfg0123456789")
    d.process = {"corners": list(CORNERS), "composite": False}
    d.temps_c = {"points": [-40.0, 25.0, 125.0]}
    d.vset = {"codes": [2, 3]}
    d.freq = {"type": "log", "start_hz": 10.0, "stop_hz": f_max, "n_points": 161}
    d.noise = {"start_hz": 10.0, "stop_hz": 1.0e8}
    d.supply = {"pins": {SUPPLY: {"net": SUPPLY, "gnd": "VSS", "nominal_v": 1.0}},
                "nominal_v": 1.0}
    d.rails = {RAIL_A: {"net": RAIL_A, "gnd": "VSS_A", "i_typ_a": 5.0e-4},
               RAIL_B: {"net": RAIL_B, "gnd": "VSS_B", "i_typ_a": 1.0e-3}}
    d.biases = {BIAS: {"net": BIAS, "gnd": "AGND", "vcomp_v": 0.45}}
    d.grounds = {"by_pin": {SUPPLY: "VSS", RAIL_A: "VSS_A", RAIL_B: "VSS_B", BIAS: "AGND",
                            STUB: "VSS"},
                 "nets": ["VSS", "VSS_A", "VSS_B", "AGND"]}
    d.loads = {RAIL_A: {"points_a": [2.0e-6, 1.0e-4, 5.0e-4, 1.0e-3]},
               RAIL_B: {"points_a": [1.0e-5, 2.0e-4, 1.0e-3, 2.0e-3]}}
    if stubs:
        d.stubs = {STUB: {"role": "rail", "net": STUB, "gnd": "VSS", "note": "stub, not modeled"}}
    return d


def zout_params(**kw):
    """A known RLC ladder: Ra + (La||Rpl) + one extra stage, branch B off, Cout + esr."""
    p = {"Ra": 0.08, "La": 2.0e-5, "Rpl": 180.0,
         "La_i": [1.5e-6], "Rpl_i": [60.0],
         "Lb": 1.0e-12, "Rb": 1.0e9,              # the fitter's OFF sentinel
         "Cout": 1.0e-9, "esr": 0.4}
    p.update(kw)
    return p


def psrr_params(*, doublet=True, complex_section=True, c_ft=0.0, **kw):
    """A known PSRR bank. `doublet=True` plants the +17.23 / -17.23 near-cancelling pair whose
    poles differ by 0.004 % -- the emit-time coupled-HB hazard."""
    gains = [0.004, 17.23148, -17.23455] if doublet else [0.004, 0.0021, -0.0009]
    poles = [1.2e5, 442_360.0, 442_378.0] if doublet else [1.2e5, 4.4e5, 9.0e6]
    p = {"G0": 0.0194, "G_i": gains, "pole_i_hz": poles,
         "pc_gain": 3.1e-3 if complex_section else 0.0,
         "pc_zero": 4.0e-10 if complex_section else 0.0,
         "pc_w0": PC_W0, "pc_q": 0.8, "c_ft": c_ft}
    p.update(kw)
    return p


def noise_params(mode="norton", **kw):
    if mode == "hybrid":
        # `white` is the NORTON floor in BOTH modes (pmukit.fit.noise.sv_model); what moves is
        # the shaped part, which in hybrid is a SERIES voltage bank in V/rtHz.
        p = {"nmode": "hybrid", "white": 1.5e-11, "flicker": 2.8e-5,
             "corner_i_hz": [200.0, 1.2e4], "amp_i": [3.0e-6, 4.0e-7]}
    else:
        p = {"nmode": "norton", "white": 4.0e-11, "flicker": 0.0,
             "corner_i_hz": [300.0, 2.0e4, 1.0e6], "amp_i": [8.0e-10, 9.0e-11, 2.0e-11]}
    p.update(kw)
    return p


def dc_params(**kw):
    p = {"vout": {2: 0.75, 3: 0.80}, "vout_tc": -1.2e-4,
         "dropout": 0.12, "ilimit": 8.0e-3}
    p.update(kw)
    return p


def load_en_params(**kw):
    p = {"iaG": 4.6e-3, "iaV": 0.44,
         "ovVdz": 0.025, "ovR": 4.0e3, "ovVmax": 2.0, "ovVsc": 8.0e-3, "ovIsc": 1.0e-3}
    p.update(kw)
    return p


def bias_params(pol="source"):
    sign = 1.0 if pol == "source" else -1.0
    return {
        "idc": {"idc": sign * 5.05e-7, "pol": pol, "knee_side": "hi", "vhi": 0.9,
                "vknee": 0.12, "knee_p": 0.7, "ptat_slope": sign * 1.4e-9},
        "yout": {"g0": 2.0e-7, "Cp": 3.0e-14, "wz": 2.0e7, "wp": 9.0e7},
        "noise": {"white": 1.1e-13, "flicker": 4.0e-12},
        "psrr": {"gdd": -3.2e-8, "psrr_pole_hz": 2.0e6},
    }


def demo_fits(*, noise_mode="norton", doublet=True, corner_scale=None):
    """The nested `{port: {block: params}}` shape (a hand-built fixture is allowed to use it)."""
    s = corner_scale or 1.0
    return {
        RAIL_A: {"zout": zout_params(Ra=0.08 * s), "psrr": psrr_params(doublet=doublet),
                 "noise": noise_params(noise_mode), "dc": dc_params(),
                 "load_en": load_en_params()},
        RAIL_B: {"zout": zout_params(Ra=0.15 * s, La=8.0e-6, Rpl=90.0, La_i=[], Rpl_i=[],
                                     Cout=4.7e-10, esr=1.2),
                 "psrr": psrr_params(doublet=False, complex_section=False, c_ft=1.74e-13),
                 "noise": noise_params("norton"), "dc": dc_params(vout=0.8)},
        BIAS: bias_params(),
    }


def emit(corner="tt", **kw):
    kw.setdefault("project", "demo_pmu")
    return emit_va(kw.pop("fits", demo_fits()), kw.pop("derived", demo_derived()), corner, **kw)


# --------------------------------------------------------------------------- the whitelist
def test_every_whitelist_entry_states_why_it_is_allowed():
    assert WHITELIST, "the whitelist is the deliverable's hard constraint"
    for name, (what, why) in WHITELIST.items():
        assert what.strip() and len(why) > 40, f"{name} has no reason on the list"


def test_forbidden_constructs_never_appear():
    """Grep the emitted text: laplace, a henry above a sane bound, an unguarded ** with an
    exponent below 1, and a capacitive node with no Gleak."""
    import re
    text = code_only(emit())
    assert "laplace" not in text.lower()
    assert "$table_model" not in text
    # no synthesized henry: every emitted inductance must be a plausible branch inductance
    for value in re.findall(r"idt\(V\([^)]*\)\)/([0-9.eE+-]+)", text):
        assert float(value) < 1.0, f"emitted a {float(value):g} H inductor"
    # every power uses pow() on a sqrt-floored base, never a bare ** on a voltage
    assert "**" not in text
    for m in re.finditer(r"pow\(([^,]+),", text):
        assert "sqrt(" in m.group(1), f"pow() base is not sqrt-floored: {m.group(1)}"
    # no capacitive node without a DC path: that is exactly the lint's dc_path rule
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    assert [f for f in rep["findings"] if f["rule"] == "dc_path"] == []


def test_netlist_refuses_a_forbidden_construct_even_if_a_builder_writes_it():
    nl = Netlist()
    nl.body.append("    V(a, b) <+ laplace_nd(V(c,d), {1}, {1, 1});")
    with pytest.raises(PmuError) as e:
        nl.text()
    assert "laplace" in str(e.value).lower()
    assert FORBIDDEN, "the forbidden table carries the failure each construct caused"


def test_there_is_no_free_text_door():
    assert not hasattr(Netlist, "add_raw")
    with pytest.raises(PmuError):
        Netlist().behavioral_current("x", "a", "b", "1.0", "not_a_whitelist_rule")


# --------------------------------------------------------------------------- structure
def test_module_ports_grounds_and_instance_parameters():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    text = built["text"]
    assert text.startswith("// ====")
    assert f"module PMU_demo_pmu_tt(" in text
    # every ground is a REAL pin, never an implicit 0
    for g in ("VSS", "VSS_A", "VSS_B", "AGND"):
        assert g in built["ports"], f"{g} is not a module pin"
    assert built["grounds"] == ["VSS", "VSS_A", "VSS_B", "AGND"]  # supply ground first
    # split grounds: each rail returns to its own pin
    assert f"V({RAIL_A}_vrg, VSS_A) <+" in text
    assert f"V({RAIL_B}_vrg, VSS_B) <+" in text
    # instance parameters: vset and load_en_* only; everything fitted is a localparam
    assert "parameter real vset = 2" in text
    assert f"parameter real load_en_{RAIL_A} = 0" in text
    params = [ln for ln in text.splitlines() if ln.strip().startswith("parameter real")]
    assert {p.split()[2] for p in params} == {"vset", f"load_en_{RAIL_A}"}


def test_temperature_is_kelvin_and_converted_exactly_once():
    text = code_only(emit())
    assert text.count("$temperature") == 1, "$temperature must be read in ONE place"
    assert "localparam real KELVIN_TO_C = -2.731500e+02;" in text
    assert "tdegc = $temperature + KELVIN_TO_C;" in text
    # and the continuous terms use it
    assert "_vtc*(tdegc -" in text
    assert "_ptat*(tdegc -" in text


def test_stub_is_listed_and_carries_no_simulation():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    assert STUB in built["ports"]
    assert "stub, not modeled" in built["text"]
    # the fixture's stub has no characterized DC value, so it must NOT be driven at a guess
    assert any("no characterized DC value" in n for n in built["notes"])
    d = demo_derived()
    d.stubs[STUB]["dc_v"] = 0.8
    t2 = emit_va(demo_fits(), d, "tt", project="demo_pmu")
    assert f"V({STUB}, VSS) <+ 8.000000e-01;" in t2


def test_a_rail_without_its_zout_or_vout_is_not_emitted():
    fits = demo_fits()
    fits[RAIL_B].pop("zout")
    built = build_va(fits, demo_derived(), "tt", project="demo_pmu")
    assert RAIL_B not in built["rails"]
    assert RAIL_B not in built["ports"]
    assert any(p == RAIL_B for p, _ in built["skipped"])
    fits = demo_fits()
    fits[RAIL_B]["dc"] = {"vout_tc": -1e-4}
    built = build_va(fits, demo_derived(), "tt", project="demo_pmu")
    assert RAIL_B not in built["rails"]


def test_vset_selects_the_measured_output():
    text = emit()
    # one reference per characterized code, plus the ternary that selects on the instance param
    assert f"localparam real {RAIL_A}_vreg_v2 =" in text
    assert f"localparam real {RAIL_A}_vreg_v3 =" in text
    assert f"(vset <= 2.500000e+00 ? {RAIL_A}_vreg_v2 : {RAIL_A}_vreg_v3)" in text
    # vreg = measured vout + Ra*i_typ, so the PIN sits at the measured vout at the typical load
    ra, i_typ = zout_params()["Ra"], 5.0e-4
    assert f"{0.80 + ra * i_typ:.6e}" in text


# --------------------------------------------------------------------------- Zout
def test_zout_regulation_is_the_stiff_resistor_never_a_current_clamp():
    text = emit()
    assert f"V({RAIL_A}_nA2, {RAIL_A}_vrg) <+ 8.000000e-02*I(" in text
    assert "Icomp" not in code_only(text)
    # the ladder is in series inside branch A
    assert f"I({RAIL_A}, {RAIL_A}_nA) <+ idt(" in text
    assert f"I({RAIL_A}_nA, {RAIL_A}_nA2) <+ idt(" in text


def test_the_fitters_off_sentinel_branch_is_not_emitted():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    assert f"{RAIL_A}_nbb" not in built["text"], "a 1e9 ohm branch is a near-null column"
    assert any("OFF sentinel" in n for n in built["notes"])
    names = [e.name for e in built["netlist"].elements]
    assert f"{RAIL_A}.zout.Rb" not in names


# --------------------------------------------------------------------------- PSRR
def test_psrr_doublet_is_consolidated_into_one_gm_c_biquad():
    g = psrr_params()
    singles, merged, notes = _consolidate(g["G_i"], g["pole_i_hz"])
    assert len(merged) == 1 and len(singles) == 1
    b0, b1, w0, q = merged[0]
    assert b0 == pytest.approx(17.23148 - 17.23455, rel=1e-9)
    assert w0 == pytest.approx(2 * math.pi * math.sqrt(442_360.0 * 442_378.0), rel=1e-12)
    assert q == pytest.approx(0.5, abs=1e-4)
    assert notes and "near-null-space" in notes[0]

    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    text = built["text"]
    assert f"{RAIL_A}_psd1_bp" in text and f"{RAIL_A}_psd1_lp" in text
    # and NOT as the +-17.2 S pair
    for el in built["netlist"].elements:
        assert abs(el.value) < 1.0 or not el.controlled, \
            f"{el.name} is a {el.value:g} S controlled source"


def test_psrr_complex_section_is_a_gm_c_biquad_not_a_synthesized_rlc():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    kinds = {e.name: e.kind for e in built["netlist"].elements}
    assert f"{RAIL_A}.psrr.Lpc" not in kinds
    assert f"{RAIL_A}_pc.Cbp" in kinds and f"{RAIL_A}_pc.Clp" in kinds
    for e in built["netlist"].elements:
        if e.kind == "inductor":
            assert e.value < 1.0, f"{e.name} = {e.value:g} H is a synthesized inductor"


def test_g0_is_band_limited_from_the_project_band():
    from pmukit.emit.va import PSRR_BANDLIMIT_MARGIN
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    pole = [e for e in built["netlist"].elements if e.name == f"{RAIL_A}_psG0_bl.gm_p"][0]
    caps = [e for e in built["netlist"].elements if e.name == f"{RAIL_A}_psG0_bl.C"][0]
    w = pole.value / caps.value
    assert w / (2 * math.pi) == pytest.approx(PSRR_BANDLIMIT_MARGIN * F_MAX, rel=1e-9)
    # the corner comes from care_up_to_hz, so a different project moves it
    d2 = demo_derived(f_max=5.0e8)
    b2 = build_va(demo_fits(), d2, "tt", project="demo_pmu")
    p2 = [e for e in b2["netlist"].elements if e.name == f"{RAIL_A}_psG0_bl.gm_p"][0]
    c2 = [e for e in b2["netlist"].elements if e.name == f"{RAIL_A}_psG0_bl.C"][0]
    assert (p2.value / c2.value) / (2 * math.pi) == pytest.approx(
        PSRR_BANDLIMIT_MARGIN * 5.0e8, rel=1e-9)


def test_psrr_sign_is_an_injection_into_the_pin():
    """`I(pin,gnd) <+ X` REMOVES current from the pin, so every coupling tap is negated.

    A previous emitter shipped PSRR inverted by 180 degrees on exactly this point.
    """
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    g0 = psrr_params()["G0"]
    tap = [e for e in built["netlist"].elements if e.name == f"{RAIL_A}_psG0.tap"][0]
    assert tap.nodes[0] == RAIL_A and tap.nodes[1] == "VSS_A"
    assert tap.value == pytest.approx(-g0), "a positive G0 must ADD current at the pin"
    assert f"I({RAIL_A}, VSS_A) <+ -1.940000e-02*V({RAIL_A}_psG0_bl, VSS_A);" in built["text"]


def test_feedthrough_cap_is_a_real_capacitor_between_supply_and_pin():
    text = emit()
    assert f"I({SUPPLY}, {RAIL_B}) <+ 1.740000e-13*ddt(V({SUPPLY}, {RAIL_B}));" in text


# --------------------------------------------------------------------------- noise
def test_rail_noise_is_pinned_to_the_4kt_300k_fit_basis():
    import re
    text = code_only(emit())
    found = list(re.finditer(r"white_noise\(([^,]+),", text))
    assert found, "the rail noise bank must emit white_noise sources"
    for m in found:
        assert "$temperature" not in m.group(1)
    # nothing rescales a noise PSD by T/300
    assert "/300" not in text and "* 300" not in text
    # and the shaped nodes are on the 4kT*300 basis
    from pmukit.emit.primitives import KT4_FIT
    assert KT4_FIT == pytest.approx(4 * 1.380649e-23 * 300.0)


def test_hybrid_rail_emits_the_series_bank_and_splices_it():
    """The bug this guards: the shaped part silently dropped, and with it the whole 1/f tail."""
    fits = demo_fits(noise_mode="hybrid")
    built = build_va(fits, demo_derived(), "tt", project="demo_pmu")
    text = built["text"]
    assert f"{RAIL_A}_nvk1" in text, "the hybrid series-voltage sections must exist"
    # spliced into the branch-A regulation, NOT left as an orphan
    reg = statement(text, f"{RAIL_A}_vrg) <+")
    assert f"V({RAIL_A}_nvk1" in reg
    # the synthesized 1/f ladder is in the SAME series bank
    assert reg.count(f"V({RAIL_A}_nvk") >= 3
    # `white` stays the Norton floor at the pin in BOTH modes
    assert f'white_noise(2.250000e-22, "{RAIL_A}_nw")' in text
    # a Norton rail is untouched by the hybrid code path existing
    assert f"{RAIL_B}_nvk" not in text
    assert f'white_noise(1.600000e-21, "{RAIL_B}_nw")' in text


def test_orphaned_hybrid_bank_is_refused():
    nl = Netlist()
    nl.body.append('    I(x, VSS) <+ white_noise(1e-20, "VDD0P8_A_nvw");')
    from pmukit.emit.va import _check_hybrid_coupled
    with pytest.raises(PmuError) as e:
        _check_hybrid_coupled(nl, [RAIL_A])
    assert "1/f" in str(e.value)


def test_flicker_ladder_reproduces_one_over_f():
    secs = _flicker_sections(1.0, 1.0, 1.0e9, 2)
    for f in (10.0, 1.0e3, 1.0e5, 1.0e7):
        s = sum(a * a / (1.0 + (f / fc) ** 2) for fc, a in secs)
        assert 10 * math.log10(s * f) == pytest.approx(0.0, abs=0.15)


def test_native_flicker_is_opt_in_only():
    """The rail 1/f defaults to the validated Lorentzian bank; `native` is opt-in.

    The BIAS keeps `flicker_noise()` either way -- the ported emitter has always emitted it there
    and a bias has no network to shape a bank with.
    """
    default = emit(fits=demo_fits(noise_mode="hybrid"))
    assert f'"{RAIL_A}_nvf"' not in default
    assert f'"{BIAS}_flk"' in default, "the bias 1/f is native, as it has always been"
    native = emit(fits=demo_fits(noise_mode="hybrid"), flicker_mode="native")
    assert f'flicker_noise(' in native and f'"{RAIL_A}_nvf"' in native
    # one native line replaces the whole synthesized ladder
    assert native.count("noise_flicker") == 0
    assert default.count("_nvk") > native.count("_nvk")


# --------------------------------------------------------------------------- bias
def test_bias_direction_is_data_detected():
    d = demo_derived()
    src = build_va(demo_fits(), d, "tt", project="demo_pmu")["text"]
    assert f"I({SUPPLY}, {BIAS}) <+ (" in src, "a positive Idc must SOURCE supply->pin"
    fits = demo_fits()
    fits[BIAS] = bias_params("sink")
    snk = build_va(fits, d, "tt", project="demo_pmu")["text"]
    assert f"I({BIAS}, AGND) <+ (" in snk, "a negative Idc must SINK pin->ground"
    # and the supply-to-current sign is folded for the drive convention
    assert f"localparam real {BIAS}_gdd = -3.200000e-08" in src
    assert f"localparam real {BIAS}_gdd = 3.200000e-08" in snk


def test_compliance_knee_is_one_sided_and_sqrt_floored():
    text = emit()
    assert f"max({BIAS}_vhi - V({BIAS}, AGND), 0.0)" in text
    assert "sqrt(" in text and "+ 1e-12)" in text
    # the symmetric form is what reopened the sink above the ceiling
    assert f"abs({BIAS}_vhi" not in text
    assert "sqrt((max(" in text, "the sqrt floor must sit on the ONE-SIDED base"


def test_bias_flicker_stays_native():
    text = emit()
    assert f'flicker_noise(1.600000e-23, 1.0, "{BIAS}_flk")' in text


# --------------------------------------------------------------------------- large signal
def test_large_signal_terms_default_off_and_are_gated():
    text = emit()
    assert f"parameter real load_en_{RAIL_A} = 0.000000e+00" in text
    assert f"load_en_{RAIL_A}*(V({RAIL_A}, {RAIL_A}_vrg) > 2.500000e-02" in text
    assert "tanh(" in text
    on = emit(ls_default_on=[RAIL_A])
    assert f"parameter real load_en_{RAIL_A} = 1.000000e+00" in on


def test_unload_discharge_is_one_sided_source_gated_and_bounded():
    text = emit()
    stmt = statement(text, f"{RAIL_A}_vrg) <+")
    assert "> 2.500000e-02 ?" in stmt, "one-sided deadzone"
    assert "0.5*(1.0 - tanh(" in stmt, "source/sink discriminator"
    assert "2.000000e+00*tanh(" in stmt, "voltage-bounded reverse EMF"
    # and NOT an output-side clamp
    assert f"I({RAIL_A}, VSS_A) <+ (V({RAIL_A}" not in text


# --------------------------------------------------------------------------- lint
def test_lint_is_quiet_on_the_gm_c_realization():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu")
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    assert rep["ok"], lint.render(rep)
    assert rep["worst_node_range"] < lint.RANGE_MAX


@pytest.mark.parametrize("noise_mode", ["norton", "hybrid"])
@pytest.mark.parametrize("flicker_mode", ["bank", "native", "off"])
def test_lint_is_quiet_on_every_shipping_variant(noise_mode, flicker_mode):
    """Every combination the emitter can ship has to pass the gate -- the native 1/f carrier's
    bare resistor node was caught by exactly this."""
    built = build_va(demo_fits(noise_mode=noise_mode), demo_derived(), "tt",
                     project="demo_pmu", flicker_mode=flicker_mode)
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    assert rep["ok"], lint.render(rep)


def test_lint_fires_on_the_planted_8890_henry_inductor():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu", hb_robust=False)
    lpc = [e for e in built["netlist"].elements if e.name == f"{RAIL_A}.psrr.Lpc"]
    assert lpc and lpc[0].value == pytest.approx(8890.0, rel=1e-6)
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    assert not rep["ok"]
    names = {f.get("element") for f in rep["findings"]}
    assert f"{RAIL_A}.psrr.Lpc" in names
    txt = lint.render(rep)
    assert "8890" in txt and "inductor" in txt


def test_lint_names_the_rule_a_finding_strains():
    built = build_va(demo_fits(), demo_derived(), "tt", project="demo_pmu", hb_robust=False)
    rep = lint.report(built["netlist"], f_max_hz=F_MAX, grounds=built["grounds"])
    for f in rep["findings"]:
        assert f["strains"] in WHITELIST
        assert len(f["why"]) > 40


# --------------------------------------------------------------------------- primitives
def test_balanced_gain_minimizes_the_largest_coefficient():
    a, drive, taps = balanced_gain(2.78e-6, [17.23])
    assert a > 1.0
    assert drive == pytest.approx(abs(taps[0]), rel=1e-9)
    assert abs(taps[0]) < 1.0
    # small coefficients are left exactly alone
    assert balanced_gain(1e-6, [0.02]) == (1.0, 1e-6, [0.02])


def test_doublet_merge_is_exact():
    gi, wi, gj, wj = 17.23148, 2.779417e6, -17.23455, 2.779531e6
    b0, b1, w0, q = biquad_from_doublet(gi, wi, gj, wj)
    for w in (1e3, 1e5, 1e6, 1e7):
        s = 1j * w
        pair = gi / (1 + s / wi) + gj / (1 + s / wj)
        sec = (b0 + b1 * s) / (1 + s / (q * w0) + (s / w0) ** 2)
        assert abs(sec - pair) <= 1e-9 * max(abs(pair), 1e-12)


def test_spec_parameter_names_are_the_only_vocabulary():
    """Everything the fixtures feed the emitter is a name the spec declares."""
    for port_type, blocks in (("rail", ("dc", "zout", "psrr", "noise", "load_en")),
                              ("bias", ("idc", "yout", "noise", "psrr"))):
        for b in blocks:
            known = {p.name for p in spec.block(b, port_type).params}
            fixture = {"rail": {"dc": dc_params(), "zout": zout_params(),
                                "psrr": psrr_params(), "noise": noise_params(),
                                "load_en": load_en_params()},
                       "bias": bias_params()}[port_type][b]
            extra = set(fixture) - known
            assert not extra, f"{port_type}.{b} invents {extra}"
