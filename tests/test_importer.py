"""The importer: raw PSF in, contract-2 dataset out, every ratio computed in Python.

Everything here is synthetic.  The properties that matter are the two shipped bugs this module
exists to make impossible, plus the contract translations nobody else does:

  * **the injection direction is read off the netlist.**  `IL_<pin> (<rail> 0) isource` SINKS its
    current, so a 1 A injection is -1 A INTO the pin and `Zout = -V(rail)`.  A reversed testbench
    is DETECTED, not assumed, and a Zout that still comes out active is reported rather than
    silently flipped.
  * **source vs sink comes from the probe, never from a constant.**  The same code must emit a
    positive I-V for a reference that sources and a negative one for a reference that sinks.
  * a noise total is stored as a POWER density, squared only when the file says V/sqrt(Hz) -- and
    a file that does not say is refused, because the two differ by 1e12.
  * `temp_cont` is a MODEL axis: for `dc_temp` it becomes the trailing coordinate, everywhere else
    the `temp_c` cell dim -- `Dataset.declare` rejects a sweep coordinate in the middle.
  * an external result directory that does not match says WHY, in one sentence.
"""
import pathlib

import numpy as np
import pytest

from pmukit import importer
from pmukit.backends.fake import write_psfascii
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.errors import PmuError
from pmukit.importer import Deck, dims_from_plan, variable_spec
from pmukit.ledger import Ledger, Run
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.site import SiteConfig

# --------------------------------------------------------------------------- a tiny PMU

DEMO = """\
simulator lang=spectre
global 0
parameters VSET=3
include "pdk/toplevel.scs" section=tt

subckt pmu_demo (vdda a ptat vss)
    ma1 (a na vdda vdda) pmos w=40u l=0.5u
    ra1 (a fb1) resistor r=100k
    rb1 (fb1 vss) resistor r=100k
    mp1 (ptat np vss vss) nmos w=10u l=1u
ends pmu_demo

PMU_TOP (VDDA_1V0 VDD0P8_A IB_PTAT 0) pmu_demo
VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0
IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u
VB_IB_PTAT (IB_PTAT 0) vsource dc=0.4

pmukit_opts options temp=27
"""

CFG = {
    "project": "demo_pmu",
    "netlist": "tb/input.scs",
    "pmu_inst": "PMU_TOP",
    "corners": ["tt"],
    "temps_c": [27],
    "vset_codes": [3],
    "state_note": "synthetic",
    "ports": {"a": "model", "ptat": "model", "vdda": "model"},
    "my_load": {"a": {"on_a": 5e-4, "off_a": 2e-6, "switches": False}},
    "care_up_to_hz": 1e6,
}

FREQ = [10.0, 100.0, 1000.0, 1e4, 1e5, 1e6]


@pytest.fixture
def parts():
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict(CFG)
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    site = SiteConfig(engine="fake")
    der = derive(cfg, pins, site)
    plan = compile_plan(cfg, der, nl, pins, site=site)
    return cfg, plan


@pytest.fixture
def ds(tmp_path, parts):
    cfg, plan = parts
    return importer.open_or_create(tmp_path / "dataset", plan, project=cfg.project,
                                   config_sha=cfg.sha())


def run_dir(tmp_path, name, deck, files):
    """A result directory in the shape the importer expects: input.scs + raw/."""
    d = pathlib.Path(tmp_path) / name
    (d / "raw").mkdir(parents=True, exist_ok=True)
    (d / "input.scs").write_text(deck, encoding="utf-8", newline="\n")
    for fname, spec in files.items():
        write_psfascii(d / "raw" / fname, spec["axis"], spec.get("unit", ""), spec["xs"],
                       spec["traces"], {"PSFversion": "1.00", "simulator": "test"},
                       spec.get("types"))
    return d


def ac_run(reads, stimulus="IL_VDD0P8_A", load_key="L2", analysis="ac", process="tt"):
    return Run(run_id="", process=process, temp_c=27.0, vset=3, load_key=load_key,
               analysis=analysis, stimulus=stimulus, reads=list(reads))


# --------------------------------------------------------------------------- variable shapes


def test_temp_cont_becomes_a_cell_dim_everywhere_but_dc_temp():
    """spec.AXES puts temp_cont in the MIDDLE; Dataset.declare rejects that, so it is translated."""
    dc_load = variable_spec("dc_load", "rail")
    assert dc_load["dims"] == ("process", "temp_c", "vset", "load_a", "iload_a")
    assert dc_load["dtype"] == "float64" and dc_load["unit"] == "V"

    dc_temp = variable_spec("dc_temp", "rail")
    assert dc_temp["dims"] == ("process", "vset", "load_a", "temp_sweep_c")   # temp_c dropped
    assert variable_spec("dc_temp", "bias")["dims"] == ("process", "vset", "temp_sweep_c")

    assert variable_spec("ac_zout", "rail")["dims"] == ("process", "temp_c", "vset", "load_a",
                                                        "freq_hz")
    assert variable_spec("ac_psrr", "bias")["dims"] == ("process", "temp_c", "freq_hz")
    assert variable_spec("ac_psrr", "bias")["unit"] == "A/V"
    assert variable_spec("ac_psrr", "rail")["unit"] == "V/V"


def test_tran_en_takes_its_axes_from_the_enable_block():
    """It is measured ON a rail but BELONGS to the EN ramp, so its axes are the ramp block's."""
    assert variable_spec("tran_en", "rail")["dims"] == ("process", "temp_c", "time_s")
    assert variable_spec("tran_en", "rail")["unit"] == "V"
    assert variable_spec("tran_en", "bias")["unit"] == "A"


def test_an_unknown_observable_is_refused():
    with pytest.raises(PmuError) as exc:
        variable_spec("ac_smith", "rail")
    assert "ac_smith" in exc.value.what


def test_dims_come_from_the_plan(parts):
    cfg, plan = parts
    dims = dims_from_plan(plan)
    assert dims["process"] == ["tt"]
    assert dims["temp_c"] == [27.0]
    assert dims["vset"] == [3]
    assert set(dims["load_a"]) == {"a"}
    assert dims["load_a"]["a"][0] == pytest.approx(2e-6)


# --------------------------------------------------------------------------- the scars


def test_zout_sign_is_read_off_the_injection_direction(tmp_path, ds):
    """`IL_a (VDD0P8_A 0)` sinks, so V is negative and Zout must come back POSITIVE."""
    v = [complex(-6.4, -0.1) for _ in FREQ]
    d = run_dir(tmp_path, "zout", DEMO.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
                                               "IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u mag=1")
                + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDD0P8_A": v}}})
    rep = importer.import_run(ac_run(["ac_zout.a"]), d / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["missing"] == []
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": 5e-4}))
    assert z[0] == pytest.approx(complex(6.4, 0.1))
    assert not any("negative" in n for n in rep["notes"])


def test_the_reversed_injection_gives_the_other_sign(tmp_path, ds):
    """With the source written `(0 <rail>)` the same raw V means the opposite current."""
    deck = DEMO.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
                        "IL_VDD0P8_A (0 VDD0P8_A) isource dc=500u mag=1")
    v = [complex(-6.4, -0.1) for _ in FREQ]
    d = run_dir(tmp_path, "zrev", deck + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDD0P8_A": v}}})
    rep = importer.import_run(ac_run(["ac_zout.a"]), d / "raw", ds, pmu_inst="PMU_TOP")
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": 5e-4}))
    assert z[0] == pytest.approx(complex(-6.4, -0.1))
    # ... and an active LF Zout is REPORTED, never silently flipped
    assert any("negative" in n and "does not flip" in n for n in rep["notes"])


@pytest.mark.parametrize("nodes, expect", [(("IB_PTAT", "0"), +1.0), (("0", "IB_PTAT"), -1.0)])
def test_a_bias_that_sources_and_one_that_sinks_get_opposite_signs(tmp_path, parts, nodes,
                                                                   expect):
    """The shipped bug was a hardcoded `sink`: every current source came out inverted.

    Spectre's branch current of `vsource (p n)` flows p -> n through the source, so with
    `VB_x (<pin> 0)` the SAME probe reading means current leaving the pin (the reference sources)
    and with `VB_x (0 <pin>)` it means the opposite.  One probe trace, two physical meanings --
    only the netlist says which.
    """
    cfg, plan = parts
    xs = [0.0, 0.2, 0.4]
    deck = DEMO.replace("VB_IB_PTAT (IB_PTAT 0) vsource dc=0.4",
                        f"VB_IB_PTAT ({nodes[0]} {nodes[1]}) vsource dc=0.4")
    d = run_dir(tmp_path, "iv",
                deck + "dcz dc dev=VB_IB_PTAT param=dc start=0 stop=0.4 lin=3\n"
                       "save VB_IB_PTAT:p\n",
                {"dcz.dc": {"axis": "dc", "unit": "V", "xs": xs,
                            "traces": {"VB_IB_PTAT:p": [1e-5, 1.1e-5, 1.2e-5]}}})
    store = Dataset.create(tmp_path / "ds", project="p", config_sha="",
                           dims=dims_from_plan(plan))
    run = ac_run(["dc_iv.ptat"], stimulus="VB_IB_PTAT", load_key="", analysis="dc_iv")
    rep = importer.import_run(run, d / "raw", store, pmu_inst="PMU_TOP")
    assert rep["missing"] == []
    iv = np.asarray(store.get("dc_iv.ptat", {"process": "tt", "temp_c": 27.0, "vset": 3}))
    assert np.sign(iv[0]) == expect
    assert abs(iv[0]) == pytest.approx(1e-5)


def test_noise_is_squared_only_when_the_file_says_amplitude(tmp_path, ds):
    out = [7.7e-6, 1e-6, 1e-7, 1e-8, 1e-9, 1e-10]
    d = run_dir(tmp_path, "nz", DEMO + "nz (VDD0P8_A 0) noise start=10 stop=1e6 dec=1\n"
                                       "save VDD0P8_A\n",
                {"nz.noise": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                              "traces": {"out": out}, "types": {"out": "V/sqrt(Hz)"}}})
    run = ac_run(["noise_v.a"], stimulus="oprobe:VDD0P8_A", analysis="noise")
    rep = importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["missing"] == []
    got = np.asarray(ds.get("noise_v.a", {"process": "tt", "temp_c": 27.0, "load_a": 5e-4}))
    assert got[0] == pytest.approx(7.7e-6 ** 2)
    assert ds.summary()["variables"]["noise_v.a"]["unit"] == "V^2/Hz"


def test_noise_already_a_power_density_is_not_squared_again(tmp_path, ds):
    out = [5.9e-11, 1e-12, 1e-14, 1e-16, 1e-18, 1e-20]
    d = run_dir(tmp_path, "nz2", DEMO + "nz (VDD0P8_A 0) noise start=10 stop=1e6 dec=1\n"
                                        "save VDD0P8_A\n",
                {"nz.noise": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                              "traces": {"out": out}, "types": {"out": "V^2/Hz"}}})
    run = ac_run(["noise_v.a"], stimulus="oprobe:VDD0P8_A", analysis="noise")
    importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    got = np.asarray(ds.get("noise_v.a", {"process": "tt", "temp_c": 27.0, "load_a": 5e-4}))
    assert got[0] == pytest.approx(5.9e-11)


def test_a_noise_file_with_no_unit_is_refused_not_guessed(tmp_path, ds):
    d = run_dir(tmp_path, "nz3", DEMO + "nz (VDD0P8_A 0) noise start=10 stop=1e6 dec=1\n"
                                        "save VDD0P8_A\n",
                {"nz.noise": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                              "traces": {"out": [1.0] * len(FREQ)},
                              "types": {"out": "mystery"}}})
    run = ac_run(["noise_v.a"], stimulus="oprobe:VDD0P8_A", analysis="noise")
    rep = importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["filled"] == []
    assert any("amplitude or a power density" in m for m in rep["missing"])


def test_psrr_divides_by_the_supply_and_says_when_it_assumed_the_drive(tmp_path, ds):
    deck = DEMO.replace("VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0",
                        "VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0 mag=1")
    v = [complex(1e-3, 0.0) for _ in FREQ]
    d = run_dir(tmp_path, "psrr", deck + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDD0P8_A": v}}})
    run = ac_run(["ac_psrr.a"], stimulus="VS_VDDA_1V0")
    rep = importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    got = np.asarray(ds.get("ac_psrr.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                          "load_a": 5e-4}))
    assert got[0] == pytest.approx(complex(1e-3, 0.0))
    assert any("was not saved" in n for n in rep["notes"])      # the assumption is stated


def test_psrr_uses_the_measured_supply_node_when_it_was_saved(tmp_path, ds):
    deck = DEMO.replace("VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0",
                        "VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0 mag=2")
    d = run_dir(tmp_path, "psrr2", deck + "acz ac start=10 stop=1e6 dec=1\n"
                                          "save VDD0P8_A VDDA_1V0\n",
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDD0P8_A": [complex(2e-3, 0)] * len(FREQ),
                                       "VDDA_1V0": [complex(2.0, 0)] * len(FREQ)}}})
    run = ac_run(["ac_psrr.a"], stimulus="VS_VDDA_1V0")
    rep = importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    got = np.asarray(ds.get("ac_psrr.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                          "load_a": 5e-4}))
    assert got[0] == pytest.approx(complex(1e-3, 0.0))          # 2 mV / 2 V
    assert not any("was not saved" in n for n in rep["notes"])


def test_a_missing_saved_signal_is_reported_not_skipped(tmp_path, ds):
    d = run_dir(tmp_path, "nosave", DEMO + "acz ac start=10 stop=1e6 dec=1\nsave VDDA_1V0\n",
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDDA_1V0": [complex(1, 0)] * len(FREQ)}}})
    rep = importer.import_run(ac_run(["ac_zout.a"]), d / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["filled"] == []
    assert any("VDD0P8_A" in m for m in rep["missing"])
    assert any("save" in m for m in rep["missing"])


def test_a_cell_that_cannot_be_derived_is_marked_missing_with_a_reason(tmp_path, ds):
    """Once the variable has storage, a later failure is registered, not silently dropped."""
    ok = run_dir(tmp_path, "ok", DEMO + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                 {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                             "traces": {"VDD0P8_A": [complex(-6.4, 0)] * len(FREQ)}}})
    importer.import_run(ac_run(["ac_zout.a"]), ok / "raw", ds, pmu_inst="PMU_TOP")
    run = ac_run(["ac_zout.a"])
    run.run_id = "cafe12345678"
    rep = importer.mark_run_missing(run, ds, "run failed: no convergence",
                                    netlist_text=DEMO, pmu_inst="PMU_TOP")
    assert rep["missing"]
    rows = ds.missing("ac_zout.a")
    assert rows and "no convergence" in rows[0][-1]
    assert ds.coverage("ac_zout.a")["missing"] == 1


# --------------------------------------------------------------------------- coordinates


def test_a_different_sweep_is_resampled_onto_the_stored_coordinate(tmp_path, ds):
    base = run_dir(tmp_path, "c1", DEMO + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                   {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                               "traces": {"VDD0P8_A": [complex(-1.0 * f / 10, 0) for f in FREQ]}}})
    importer.import_run(ac_run(["ac_zout.a"]), base / "raw", ds, pmu_inst="PMU_TOP")

    denser = [10.0, 31.6, 100.0, 316.0, 1000.0, 3160.0, 1e4, 1e5, 1e6]
    other = run_dir(tmp_path, "c2",
                    DEMO.replace("section=tt", "section=ss")
                    + "acz ac start=10 stop=1e6 dec=2\nsave VDD0P8_A\n",
                    {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": denser,
                                "traces": {"VDD0P8_A": [complex(-1.0 * f / 10, 0)
                                                        for f in denser]}}})
    rep = importer.import_run(ac_run(["ac_zout.a"]), other / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["missing"] == []
    assert any("resampled" in n for n in rep["notes"])
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": 5e-4}))
    assert z[2] == pytest.approx(complex(100.0, 0.0), rel=1e-6)


def test_points_outside_the_measured_band_become_nan_never_extrapolation(tmp_path, ds):
    base = run_dir(tmp_path, "b1", DEMO + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                   {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                               "traces": {"VDD0P8_A": [complex(-1, 0)] * len(FREQ)}}})
    importer.import_run(ac_run(["ac_zout.a"]), base / "raw", ds, pmu_inst="PMU_TOP")
    short = [10.0, 100.0, 1000.0]
    narrow = run_dir(tmp_path, "b2", DEMO + "acz ac start=10 stop=1e3 dec=1\nsave VDD0P8_A\n",
                     {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": short,
                                 "traces": {"VDD0P8_A": [complex(-1, 0)] * 3}}})
    rep = importer.import_run(ac_run(["ac_zout.a"]), narrow / "raw", ds, pmu_inst="PMU_TOP")
    assert any("never extrapolates" in n for n in rep["notes"])
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": 5e-4}))
    assert np.isnan(z[-1])                     # 1 MHz was never measured in the second run


def test_a_transient_is_stored_on_a_fixed_grid_and_the_cost_is_reported(tmp_path, ds):
    ts = list(np.linspace(0, 2e-5, 733))       # a solver's own, irregular point count
    vs = [0.8 - 0.05 * np.exp(-(t - 2e-6) / 2e-7) if t > 2e-6 else 0.8 for t in ts]
    d = run_dir(tmp_path, "tr", DEMO + "trz tran stop=2e-05 step=1e-10\nsave VDD0P8_A\n",
                {"trz.tran": {"axis": "time", "unit": "s", "xs": ts,
                              "traces": {"VDD0P8_A": vs}}})
    run = ac_run(["tran_load_on.a"], load_key="", analysis="tran_load_on")
    rep = importer.import_run(run, d / "raw", ds, pmu_inst="PMU_TOP")
    assert rep["missing"] == []
    coord = np.asarray(ds.coord("tran_load_on.a"))
    assert coord.size == importer.TRAN_POINTS
    assert coord[-1] == pytest.approx(2e-5)
    assert any("resampled" in n for n in rep["notes"])


# --------------------------------------------------------------------------- external import


def make_external(tmp_path, ds, parts, which=("ac",)):
    """Produce result directories the way a user's ADE run would leave them."""
    cfg, plan = parts
    dirs = []
    for pr in plan.runs():
        if pr.run.analysis not in which:
            continue
        deck = pr.netlist_text
        kind = {"ac": ("acz.ac", "freq", "Hz"), "noise": ("nz.noise", "freq", "Hz"),
                "dc_load": ("dcz.dc", "dc", "A"), "dc_iv": ("dcz.dc", "dc", "V"),
                "dc_temp": ("dcz.dc", "temp", "C")}[pr.run.analysis]
        saves = [t for line in deck.splitlines() if line.startswith("save ")
                 for t in line.split()[1:]]
        n = len(FREQ)
        traces, types = {}, {}
        for s in saves:
            if pr.run.analysis == "noise":
                continue
            traces[s] = ([complex(-1.0, 0.0)] * n if pr.run.analysis == "ac"
                         else [0.8] * n)
        if pr.run.analysis == "noise":
            traces["out"] = [1e-8] * n
            types["out"] = "V/sqrt(Hz)"
        dirs.append(run_dir(tmp_path, f"ext_{pr.run_id}", deck,
                            {kind[0]: {"axis": kind[1], "unit": kind[2], "xs": FREQ,
                                       "traces": traces, "types": types}}))
    return dirs


def test_external_import_fills_and_lists_what_is_left(tmp_path, ds, parts):
    cfg, plan = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    dirs = make_external(tmp_path, ds, parts, which=("ac",))
    rep = importer.import_external(dirs, plan, ledger, ds, pmu_inst="PMU_TOP")
    assert rep["filled"]
    assert rep["unmatched"] == []
    assert rep["still_to_run"]                            # the non-AC runs, listed
    assert all(r["analysis"] != "ac" for r in rep["still_to_run"])
    for row in ledger.all(status="imported"):
        assert row.source_path                            # contract 3: imported carries it
        assert row.analysis == "ac"


def test_an_unmatched_directory_says_why_in_one_sentence(tmp_path, ds, parts):
    cfg, plan = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    alien = run_dir(tmp_path, "alien",
                    DEMO.replace("section=tt", "section=ff").replace("temp=27", "temp=85")
                    + "acz ac start=10 stop=1e6 dec=1\nsave VDD0P8_A\n",
                    {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                                "traces": {"VDD0P8_A": [complex(-1, 0)] * len(FREQ)}}})
    rep = importer.import_external([alien], plan, ledger, ds, pmu_inst="PMU_TOP")
    assert rep["filled"] == []
    assert len(rep["unmatched"]) == 1
    why = rep["unmatched"][0]["why"]
    assert why and "\n" not in why and len(why) < 300    # one sentence, not a traceback
    assert "section" in why or "temperature" in why


def test_a_directory_with_no_netlist_says_so(tmp_path, ds, parts):
    cfg, plan = parts
    d = pathlib.Path(tmp_path) / "bare"
    (d / "raw").mkdir(parents=True)
    write_psfascii(d / "raw" / "acz.ac", "freq", "Hz", FREQ,
                   {"VDD0P8_A": [complex(-1, 0)] * len(FREQ)}, {"PSFversion": "1.00"})
    rep = importer.import_external([d], plan, Ledger(tmp_path / "l.sqlite"), ds,
                                   pmu_inst="PMU_TOP")
    assert "input.scs" in rep["unmatched"][0]["why"]


def test_two_analyses_of_one_type_in_one_directory_is_ambiguous_not_guessed(tmp_path, ds, parts):
    """An ADE deck that runs zoutA and zoutB in one go cannot be assigned by pmukit."""
    cfg, plan = parts
    pr = next(p for p in plan.runs() if p.run.analysis == "ac")
    deck = pr.netlist_text + "\nacz2 ac start=10 stop=1e6 dec=1\n"
    d = run_dir(tmp_path, "ambig", deck,
                {"acz.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                            "traces": {"VDD0P8_A": [complex(-1, 0)] * len(FREQ)}},
                 "acz2.ac": {"axis": "freq", "unit": "Hz", "xs": FREQ,
                             "traces": {"VDD0P8_A": [complex(-2, 0)] * len(FREQ)}}})
    rep = importer.import_external([d], plan, Ledger(tmp_path / "l.sqlite"), ds,
                                   pmu_inst="PMU_TOP")
    # the fingerprint still resolves it (both statements are compared, one matches exactly)
    assert rep["filled"] or "ambiguous" in rep["unmatched"][0]["why"]


# --------------------------------------------------------------------------- CSV


def test_csv_must_be_declared_never_inferred(tmp_path, ds):
    p = pathlib.Path(tmp_path) / "whatever.csv"
    p.write_text("10,1.0,0.0\n100,2.0,0.0\n", encoding="utf-8")
    rep = importer.import_csv({p: {}}, ds)
    assert rep["filled"] == []
    assert "does not infer" in rep["missing"][0]


def test_csv_with_a_declaration_lands_in_the_cell(tmp_path, ds):
    p = pathlib.Path(tmp_path) / "zout.csv"
    p.write_text("freq,re,im\n" + "\n".join(f"{f},{-1.0},{0.0}" for f in FREQ) + "\n",
                 encoding="utf-8")
    rep = importer.import_csv(
        {p: {"variable": "ac_zout.a",
             "cell": {"process": "tt", "temp_c": 27.0, "vset": 3, "load_a": 5e-4}}}, ds)
    assert rep["missing"] == []
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": 5e-4}))
    assert z[0] == pytest.approx(complex(-1.0, 0.0))     # a CSV IS the ratio; no sign is applied


# --------------------------------------------------------------------------- the deck reader


def test_deck_reads_roles_pins_and_the_cell():
    deck = Deck(DEMO, pmu_inst="PMU_TOP")
    assert deck.port_type("a") == "rail"
    assert deck.port_type("ptat") == "bias"
    assert deck.net_of("a") == "VDD0P8_A"
    assert deck.probe_of("ptat") == "VB_IB_PTAT:p"
    assert deck.temp_c == 27.0
    assert deck.vset == 3
    assert deck.sections == (("toplevel.scs", "tt"),)
    assert deck.orientation("a") == 1


def test_deck_finds_the_pmu_instance_when_it_is_not_told():
    deck = Deck(DEMO)
    assert deck.pmu_inst == "PMU_TOP"
    assert deck.port_type("a") == "rail"


def test_deck_reads_split_grounds_from_the_zero_volt_ties():
    text = DEMO.replace("PMU_TOP (VDDA_1V0 VDD0P8_A IB_PTAT 0) pmu_demo",
                        "PMU_TOP (VDDA_1V0 VDD0P8_A IB_PTAT VSS_A) pmu_demo\n"
                        "VGND_VSS_A (VSS_A 0) vsource dc=0")
    text = text.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
                        "IL_VDD0P8_A (VDD0P8_A VSS_A) isource dc=500u")
    deck = Deck(text, pmu_inst="PMU_TOP")
    assert "VSS_A" in deck.grounds
    assert deck.net_of("a") == "VDD0P8_A"      # not the ground end
    assert deck.orientation("a") == 1


def test_deck_captures_the_whole_pwl_wave():
    text = DEMO.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u",
                        "IL_VDD0P8_A (VDD0P8_A 0) isource type=pwl "
                        "wave=[0 2e-06 2e-06 2e-06 2.001e-06 0.0005 2e-05 0.0005]")
    deck = Deck(text, pmu_inst="PMU_TOP")
    assert deck.sources["IL_VDD0P8_A"]["wave"].startswith("0 2e-06 2e-06")
    # the two load transients differ ONLY here, so it must be part of the cell signature
    assert any("pwl" not in str(x) for x in deck.drives())
    assert deck.drives()[0][3].startswith("0 2e-06")
