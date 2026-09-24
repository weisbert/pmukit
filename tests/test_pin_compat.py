"""Contract 4: the delivered model is PIN-COMPATIBLE with the PMU.

The Deliver screen tells the user the model has "the same pins as your PMU". QA found the
emitted module was `(VDDA_1V0, VDD0P8_A, VDD0P8_B, VDD0P8_C, IB_POLY, IB_PTAT, VSS_A, VSS_B,
AGND)` against a bench instance of `PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B VDD0P8_C IB_PTAT IB_POLY
EN TESTMODE 0 0 0)` -- EN and TESTMODE missing, the biases swapped -- so a positional instance was
mis-wired SILENTLY. The rule now: every pin of the PMU subcircuit, in its order; what the model
has nothing for is a declared PASS-THROUGH pin, tied to ground inside so it never floats.

The fixture pipeline runs on the `fake` backend (no simulator), end to end: netlist -> plan ->
run -> dataset -> fit -> deliver.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from pmukit import emit, fit as fitmod, jsonio
from pmukit.config import DerivedConfig, ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.deliverable import Deliverable
from pmukit.backends.fake import FakeBackend
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig
from pmukit.verify import hb as H, system as S

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"
PORTS = {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model", "VDD0P8_C": "stub",
         "IB_PTAT": "model", "IB_POLY": "model", "EN": "model", "TESTMODE": "ignore"}


@pytest.fixture(scope="module")
def delivered(tmp_path_factory):
    root = tmp_path_factory.mktemp("pincompat")
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": "pins", "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": [25], "vset_codes": [3], "ports": PORTS,
        "stub_dc": {"VDD0P8_C": 0.74},
        "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
        "care_up_to_hz": 1e9})
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    site = SiteConfig(engine="fake")
    der = derive(cfg, pins, site)
    plan = compile_plan(cfg, der, nl, pins, site=site)
    led = Ledger(root / "runs.sqlite")
    plan.commit(led)
    dims = {"process": der.process["corners"], "temp_c": der.temps_c["points"],
            "vset": der.vset["codes"],
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(root / "dataset", project=cfg.project, config_sha=cfg.sha(), dims=dims)
    Runner(cfg.project, plan, led, site, dataset=ds, root=root / "runs",
           backend=FakeBackend(site)).run_all()
    result = fitmod.fit_project(ds, der)
    ds.close()
    led.close()
    out = emit.deliver("pins", root=root, fit=result, derived=der, stamp="t0")
    built = emit.build_va(result, der, "tt", project="pins")
    return {"nl": nl, "pins": pins, "der": der, "out": out, "built": built, "fit": result}


def _module_ports(va_text: str) -> list[str]:
    m = re.search(r"^module \w+\(([^)]*)\);", va_text, re.MULTILINE)
    assert m, "no module line"
    return [p.strip() for p in m.group(1).split(",")]


def _bench_instance(nl: Netlist) -> tuple[str, list[str]]:
    name, nodes, master, _rest = nl.find_instance("PMU_TOP")
    return master, nodes


# --------------------------------------------------------------------------- the rule
def test_the_module_has_every_pmu_pin_in_the_pmu_order(delivered):
    subckt = delivered["nl"]._subckt_ports("pmu_demo")
    assert subckt == ["VDDA_1V0", "VDD0P8_A", "VDD0P8_B", "VDD0P8_C", "IB_PTAT", "IB_POLY",
                      "EN", "TESTMODE", "VSS_A", "VSS_B", "AGND"]
    va = (delivered["out"] / "PMU_pins_tt.va").read_text(encoding="utf-8")
    assert _module_ports(va) == subckt
    assert delivered["built"]["ports"] == subckt


def test_a_positional_instance_from_the_bench_binds_every_net_to_its_own_pin(delivered):
    """Take the bench's own `PMU_TOP (...)` line, swap the master for the model, and bind
    positionally: every net must land on the pin of the same name it was on in the PMU."""
    _master, nets = _bench_instance(delivered["nl"])
    ports = delivered["built"]["ports"]
    assert len(nets) == len(ports)
    table = delivered["pins"].pins
    for net, port in zip(nets, ports):
        assert table[port].net == net, (port, net)
    # the instance line the deliverable hands the user IS that line, master swapped
    use = jsonio.read(delivered["out"] / "interface.json")
    assert use["instance"]["tt"] == f"PMU_TOP ({' '.join(nets)}) PMU_pins_tt vset=3"


def test_pins_the_model_has_nothing_for_are_declared_pass_through(delivered):
    built = delivered["built"]
    assert built["pass_through"] == ["EN", "TESTMODE"]
    va = (delivered["out"] / "PMU_pins_tt.va").read_text(encoding="utf-8")
    # declared, and tied through 1 GOhm so a bench that leaves them open has no floating node
    for p in ("EN", "TESTMODE"):
        assert re.search(rf"I\({p}, \w+\) <\+ 1(?:\.0+)?e-0?9\*V\({p}, \w+\);", va), p
        assert f"{p} is a pass-through pin (declared, not modeled)" in va
    assert "pass-through (declared, NOT modeled" in va
    # the three grounds keep the PMU's names and positions; every one is a real return
    assert built["ground_pins"] == ["VSS_A", "VSS_B", "AGND"]
    by = {e["pin"]: e for e in built["interface"]}
    assert all(by[g]["modeled"] for g in ("VSS_A", "VSS_B", "AGND"))
    assert "always on" in by["EN"]["what"]


def test_report_and_scs_say_which_pins_are_pass_through(delivered):
    out = delivered["out"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "## Pins" in report
    assert "| 7 | EN | **pass-through:**" in report
    assert "| 8 | TESTMODE | **pass-through:**" in report
    assert "EN has no effect" in report and "the model is always on" in report
    # numbers and pin names only: the bench's instance name and nets stay out of report.md
    assert "PMU_TOP" not in report
    scs = (out / "PMU_pins.scs").read_text(encoding="utf-8")
    assert "// pass-through (declared, not modeled): EN TESTMODE" in scs
    assert "// PMU_TOP (VDDA_1V0 VDD0P8_A VDD0P8_B VDD0P8_C IB_PTAT IB_POLY EN TESTMODE 0 0 0) " \
           "PMU_pins_tt vset=3" in scs
    grades = jsonio.read(out / "grades.json")
    assert grades["pass_through"] == ["EN", "TESTMODE"]


def test_the_deliverables_api_serves_the_instance_line(delivered):
    d = Deliverable.open(delivered["out"])
    use = d.interface()
    assert use["pmu_order"] is True and use["pass_through"] == ["EN", "TESTMODE"]
    assert use["instance"]["tt"].startswith("PMU_TOP (VDDA_1V0 ")
    assert [p["pin"] for p in use["pins"]][:8] == ["VDDA_1V0", "VDD0P8_A", "VDD0P8_B",
                                                    "VDD0P8_C", "IB_PTAT", "IB_POLY", "EN",
                                                    "TESTMODE"]


# --------------------------------------------------------------------------- the verify benches
def test_the_verify_decks_bind_positionally_with_every_ground_on_zero(delivered):
    built, der = delivered["built"], delivered["der"]
    t = S.tank(1.2e9, 0.8, 5e-4)
    deck = S.model_deck(built, der, va_name="m.va", rail="VDD0P8_A", t=t, vset=3)
    inst = next(ln for ln in deck.splitlines() if ln.startswith("X1 "))
    nets = inst.split("(", 1)[1].split(")", 1)[0].split()
    assert nets == ["VDDA_1V0", "VDD0P8_A", "VDD0P8_B", "VDD0P8_C", "IB_PTAT", "IB_POLY",
                    "EN", "TESTMODE", "0", "0", "0"]
    drive = {"port": "VDD0P8_A", "f_hz": 1e6, "ampl_a": 1e-4, "z_peak_ohm": 10.0}
    hb = H.bench_deck(built, der, va_name="m.va", drive=drive, vset=3)
    inst = next(ln for ln in hb.splitlines() if ln.startswith("X1 "))
    assert inst.split("(", 1)[1].split(")", 1)[0].split()[-3:] == ["0", "0", "0"]


def test_the_oscillator_check_still_builds_and_degrades_offline(delivered):
    """No solver on this machine: the check builds the model and its benches, then says why it
    cannot answer -- it must not trip over the new pin list on the way."""
    report = S.oscillator_check(delivered["fit"], delivered["der"], corner="tt", project="pins",
                                site=SiteConfig(engine="fake"),
                                backend=FakeBackend(SiteConfig(engine="fake")))
    assert report["order"] == ["VDD0P8_A", "VDD0P8_B"]
    assert any("no simulator" in n for n in report["notes"])


# --------------------------------------------------------------------------- edges
def test_an_older_derived_config_falls_back_and_says_so(delivered):
    d = DerivedConfig.from_dict({k: v for k, v in delivered["der"].to_dict().items()
                                 if k != "interface"})
    built = emit.build_va(delivered["fit"], d, "tt", project="pins")
    assert built["ports"][:1] == ["VDDA_1V0"] and "EN" not in built["ports"]
    assert any("pin order is not in the derived config" in n for n in built["notes"])


def test_pins_named_after_nets_are_made_legal_but_keep_their_position(delivered):
    """A PMU subckt pmukit could not read leaves the pins named after their nets -- the grounds
    come out as `0`, `0#9`, `0#10`. Positions are what bind; the names only have to compile."""
    d = DerivedConfig.from_dict(delivered["der"].to_dict())
    rename = {"VSS_A": "0", "VSS_B": "0#9", "AGND": "0#10"}
    for e in d.interface["pins"]:
        e["pin"] = rename.get(e["pin"], e["pin"])
    d.grounds = {"by_pin": {}, "nets": []}
    built = emit.build_va(delivered["fit"], d, "tt", project="pins")
    assert len(built["ports"]) == 11
    assert built["ports"][8:] == ["pin8_0", "pin9_0_9", "pin10_0_10"]
    assert built["grounds"] == ["pin8_0"]
    assert built["ground_pins"] == ["pin8_0", "pin9_0_9", "pin10_0_10"]
