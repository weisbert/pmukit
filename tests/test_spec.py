"""Contract 1: the physics inventory must stay internally consistent and stably hashed."""
import copy

import pytest

from pmukit import jsonio, spec
from pmukit.errors import PmuError


# --- the table is well formed ------------------------------------------------------------


def test_every_block_uses_the_fixed_vocabularies():
    assert spec.SPEC, "the spec table is empty"
    for b in spec.SPEC:
        assert b.port_type in spec.PORT_TYPES, b
        assert b.tier in spec.TIERS, b
        assert b.priority >= 1, b
        assert b.why, b
        for p in b.params:
            assert p.observable in spec.all_observables(), (b.name, p.name, p.observable)
            assert set(p.axes) <= set(spec.AXES), (b.name, p.name, p.axes)


def test_block_observables_are_derived_and_deduplicated():
    """`observables` must be exactly the params' observables, first-seen order, no repeats."""
    for b in spec.SPEC:
        seen = []
        for p in b.params:
            if p.observable not in seen:
                seen.append(p.observable)
        assert b.observables == tuple(seen), b.name
        assert len(set(b.observables)) == len(b.observables), b.name


def test_block_keys_are_unique():
    keys = [(b.name, b.port_type) for b in spec.SPEC]
    assert len(set(keys)) == len(keys), keys


def test_contract_rows_are_all_present():
    """The rows of CONTRACTS.md section 1, one for one."""
    assert [b.name for b in spec.blocks_for("rail")] == \
        ["dc", "zout", "psrr", "noise", "load_en", "no_sink"]
    assert [b.name for b in spec.blocks_for("bias")] == ["idc", "yout", "noise", "psrr"]
    assert [b.name for b in spec.blocks_for("en")] == ["ramp"]


def test_tiers_match_the_contract():
    assert spec.tier_of("load_en", "rail") == "ls"     # per-item switch + HB residual check
    assert spec.tier_of("ramp", "en") == "en"          # usable, not signed off
    for name in ("dc", "zout", "psrr", "noise", "no_sink"):
        assert spec.tier_of(name, "rail") == "hb"
    for name in ("idc", "yout", "noise", "psrr"):
        assert spec.tier_of(name, "bias") == "hb"


def test_named_parameters_from_the_modeling_method():
    """The fitter will produce these names; the UI rows and the plan both key off them."""
    zout = {p.name for p in spec.block("zout", "rail").params}
    assert {"Ra", "La", "Rpl", "La_i", "Rpl_i", "Cout", "esr"} <= zout
    psrr = {p.name for p in spec.block("psrr", "rail").params}
    assert {"G_i", "pole_i_hz", "pc_gain", "pc_w0", "pc_q"} <= psrr
    noise = {p.name for p in spec.block("noise", "rail").params}
    assert {"white", "flicker", "corner_i_hz", "amp_i", "nmode"} <= noise
    load_en = {p.name for p in spec.block("load_en", "rail").params}
    assert {"iaG", "iaV", "ovVdz", "ovR", "ovVmax", "ovVsc", "ovIsc"} <= load_en
    yout = {p.name for p in spec.block("yout", "bias").params}
    assert {"g0", "Cp", "wz", "wp"} <= yout


def test_dc_blocks_use_the_continuous_temperature_axis():
    """DC quantities get a temperature SWEEP (temp_cont); AC/noise/tran get discrete points."""
    assert "temp_cont" in spec.axes_for("dc_load")
    assert "temp_cont" in spec.axes_for("dc_iv")
    assert "temp_cont" not in spec.axes_for("ac_zout")
    assert "temp_c" in spec.axes_for("ac_zout")


# --- lookups ------------------------------------------------------------------------------


def test_blocks_for_covers_every_port_type():
    for pt in spec.PORT_TYPES:
        assert spec.blocks_for(pt), pt
        assert all(b.port_type == pt for b in spec.blocks_for(pt))


def test_blocks_for_rejects_an_unknown_port_type():
    with pytest.raises(PmuError) as e:
        spec.blocks_for("supply")
    err = e.value
    assert err.what and err.why and err.do and err.where      # all four parts present
    assert "rail" in str(err) and "bias" in str(err) and "en" in str(err)


def test_block_rejects_an_unknown_name_and_names_the_valid_ones():
    with pytest.raises(PmuError) as e:
        spec.block("bandgap", "rail")
    assert "zout" in str(e.value)


def test_noise_and_psrr_exist_for_two_port_types():
    assert spec.block("noise", "rail") is not spec.block("noise", "bias")
    assert spec.block("psrr", "rail").observables == ("ac_psrr",)


def test_observables_for_is_deduplicated_and_in_declaration_order():
    assert spec.observables_for("rail") == (
        "dc_load", "dc_temp", "ac_zout", "ac_psrr", "noise_v", "tran_load_on", "tran_load_off")
    assert spec.observables_for("bias") == ("dc_iv", "dc_temp", "ac_yout", "noise_i", "ac_psrr")
    assert spec.observables_for("en") == ("tran_en",)


def test_all_observables_is_the_union_over_port_types():
    union = set()
    for pt in spec.PORT_TYPES:
        union |= set(spec.observables_for(pt))
    assert union == set(spec.all_observables())


def test_axes_for_unions_every_consumer():
    # noise_v is fitted per load point but the Lorentzian corners are shared -> the union
    # still has to carry load_a, or the plan would only run one load.
    assert spec.axes_for("noise_v") == ("process", "temp_c", "load_a")
    with pytest.raises(PmuError):
        spec.axes_for("ac_gain")


# --- the no_sink constant -----------------------------------------------------------------


def test_no_sink_has_no_observable_and_no_run():
    b = spec.block("no_sink", "rail")
    assert b.params == ()
    assert b.observables == ()                                  # emitter constant
    reqs = spec.requirements("rail")
    consumers = [c for r in reqs for c in r.consumers]
    assert all(blk != "no_sink" for blk, _ in consumers)        # never schedules a run
    assert b.why                                                # but it still explains itself


# --- the supply-injection merge -----------------------------------------------------------


def test_both_psrr_blocks_declare_ac_psrr():
    """The rail PSRR and the bias PSRR come from the SAME supply injection.

    This is the property the plan compiler needs: one AC run injects at the supply and reads
    every rail and every bias pin, so the two blocks must name the identical observable or the
    compiler would schedule two runs for one measurement.
    """
    assert "ac_psrr" in spec.block("psrr", "rail").observables
    assert "ac_psrr" in spec.block("psrr", "bias").observables
    assert "ac_psrr" in spec.observables_for("rail")
    assert "ac_psrr" in spec.observables_for("bias")


# --- contract-2 variable names -------------------------------------------------------------


def test_variable_name_round_trip():
    for obs in spec.all_observables():
        for port in ("VDD0P8_A", "VDD0P8_B", "IB_PTAT", "IB_POLY", "_x9"):
            name = spec.variable_name(obs, port)
            assert name == f"{obs}.{port}"
            assert spec.split_variable(name) == (obs, port)


def test_variable_name_rejects_an_unknown_observable():
    with pytest.raises(PmuError) as e:
        spec.variable_name("ac_gain", "VDD0P8_A")
    assert "ac_gain" in str(e.value)


def test_variable_name_rejects_a_dotted_port():
    with pytest.raises(PmuError):
        spec.variable_name("ac_zout", "top.VDD0P8_A")
    with pytest.raises(PmuError):
        spec.variable_name("ac_zout", "9volts")
    with pytest.raises(PmuError):
        spec.variable_name("ac_zout", "")


def test_split_variable_rejects_junk():
    with pytest.raises(PmuError):
        spec.split_variable("ac_zout")                    # no separator
    with pytest.raises(PmuError):
        spec.split_variable("ac_gain.VDD0P8_A")           # unknown observable
    with pytest.raises(PmuError):
        spec.split_variable("ac_zout.top.VDD0P8_A")       # port carries a dot


# --- requirements --------------------------------------------------------------------------


def test_requirements_cover_every_observable_with_axes_and_consumers():
    for pt in spec.PORT_TYPES:
        reqs = spec.requirements(pt)
        assert [r.observable for r in reqs] == list(spec.observables_for(pt))
        for r in reqs:
            assert r.consumers, r.observable
            assert set(r.axes) <= set(spec.AXES)
            assert set(r.axes) <= set(spec.axes_for(r.observable))
            for blk, param in r.consumers:
                b = spec.block(blk, pt)
                assert any(p.name == param and p.observable == r.observable for p in b.params)


def test_requirement_axes_are_the_union_over_its_own_consumers():
    (req,) = [r for r in spec.requirements("bias") if r.observable == "ac_psrr"]
    # a bias-only project must not inherit the rail's load sweep
    assert req.axes == ("process", "temp_c")


def test_tier_filter_drops_the_large_signal_and_en_blocks():
    rail_hb = spec.requirements("rail", ("hb",))
    obs = [r.observable for r in rail_hb]
    assert "tran_load_on" not in obs and "tran_load_off" not in obs
    assert "ac_zout" in obs
    assert spec.requirements("en", ("hb",)) == ()
    assert [r.observable for r in spec.requirements("en", ("en",))] == ["tran_en"]
    # ls alone leaves exactly the load_en block
    ls = spec.requirements("rail", ("ls",))
    assert [r.observable for r in ls] == ["tran_load_on", "tran_load_off"]


def test_requirements_reject_an_unknown_tier():
    with pytest.raises(PmuError):
        spec.requirements("rail", ("hb", "rf"))


# --- provenance -------------------------------------------------------------------------------


def test_spec_sha_is_stable_across_runs():
    assert spec.SPEC_SHA == jsonio.sha(spec.to_dict(), 12)
    assert spec.SPEC_SHA == jsonio.sha(spec.to_dict(), 12)
    assert len(spec.SPEC_SHA) == 12
    int(spec.SPEC_SHA, 16)                                 # it is hex


def test_spec_sha_changes_when_the_table_is_perturbed():
    for mutate in (
        lambda d: d["blocks"][1]["params"][0].__setitem__("note", "perturbed"),
        lambda d: d["blocks"][0].__setitem__("tier", "ls"),
        lambda d: d["blocks"][0]["params"].pop(),
        lambda d: d["observables"].__setitem__("dc_load", "perturbed"),
    ):
        d = copy.deepcopy(spec.to_dict())
        mutate(d)
        assert jsonio.sha(d, 12) != spec.SPEC_SHA


def test_to_dict_is_json_able_and_complete():
    d = spec.to_dict()
    jsonio.canon(d)                                        # must not raise
    assert len(d["blocks"]) == len(spec.SPEC)
    assert d["not_modeled"] and all(len(x) == 2 for x in d["not_modeled"])
    names = {(b["port_type"], b["name"]) for b in d["blocks"]}
    assert names == {(b.port_type, b.name) for b in spec.SPEC}


# --- help --------------------------------------------------------------------------------------


def test_explain_carries_the_why_and_the_tier_meaning():
    for b in spec.SPEC:
        text = spec.explain(b.name, b.port_type)
        assert b.why in text
        assert spec.TIER_MEANING[b.tier] in text
        assert spec.WHAT[(b.port_type, b.name)] in text
        for p in b.params:
            assert p.name in text


def test_explain_of_no_sink_says_it_needs_no_run():
    text = spec.explain("no_sink", "rail")
    assert "emitter constant" in text


def test_explain_of_an_unknown_block_points_at_not_modeled():
    text = spec.explain("uvlo", "rail")
    assert "UVLO" in text
    assert "Deliberately NOT modeled" in text
    assert "rail-to-rail coupling" in text
    assert "zout" in text                                  # and lists what does exist


def test_explain_rejects_an_unknown_port_type():
    with pytest.raises(PmuError):
        spec.explain("dc", "supply")


def test_not_modeled_covers_the_refactor_plan_list():
    items = " | ".join(i for i, _ in spec.NOT_MODELED).lower()
    for token in ("rail-to-rail", "vset", "startup", "quiescent", "thermal",
                  "uvlo", "esd", "bandgap", "digital"):
        assert token in items, token
    assert all(reason for _, reason in spec.NOT_MODELED)
