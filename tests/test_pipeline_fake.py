"""A simulator-free end-to-end gate: netlist -> plan -> run -> dataset -> fit.

The `fake` backend's DUT is deliberately kept INSIDE the model's own form -- Zout is a ladder,
PSRR is `i_c * Zout`, and the rail's output noise is a Norton current shaped by Zout. So the
small-signal blocks must round-trip to hundredths of a dB, and a residual here means something in
the PIPELINE moved (a sign, a unit, an axis, a cell lookup), not that a fit got harder.

That is what makes this a cheap regression gate: it runs on any machine, in seconds, and it has
caught three real inconsistencies already (see the comments in pmukit/backends/fake.py).
"""
import pathlib

import pytest

from pmukit import fit as fitmod
from pmukit.backends.fake import MODEL, FakeBackend, selftest
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"

# Thresholds are set an order of magnitude above what the pipeline currently achieves, so this
# gate fires on a regression rather than on ordinary numerical drift.
LIMITS = {"zout": 0.5, "psrr": 0.5, "noise": 0.2, "dc": 1e-6}


def test_the_fake_dut_is_physically_self_consistent():
    """Zout(DC) finite, peaked, ESR floor above -- checked by the backend's own selftest."""
    selftest()
    from pmukit.backends.fake import _psrr, _zout
    assert abs(_zout(1e-3, 1.0)) == pytest.approx(MODEL["zout"]["r_dc_ohm"], rel=1e-3)
    # PSRR is i_c * Zout, so i_c stays finite at DC -- no integrator is implied
    ic = _psrr(1e-3, 1.0) / _zout(1e-3, 1.0)
    assert abs(ic) == pytest.approx(MODEL["psrr"]["ic0_s"], rel=1e-3)


@pytest.fixture(scope="module")
def fitted(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": "pipeline", "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": [25], "vset_codes": [3],
        "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                  "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                  "EN": "model", "TESTMODE": "ignore"},
        "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
        "care_up_to_hz": 1e9})
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, SiteConfig(engine="fake"))
    plan = compile_plan(cfg, der, nl, pins, site=SiteConfig(engine="fake"))
    led = Ledger(root / "runs.sqlite")
    plan.commit(led)
    dims = {"process": der.process["corners"], "temp_c": der.temps_c["points"],
            "vset": der.vset["codes"],
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(root / "dataset", project=cfg.project, config_sha=cfg.sha(), dims=dims)
    Runner(cfg.project, plan, led, SiteConfig(engine="fake"), dataset=ds, root=root,
           backend=FakeBackend(SiteConfig(engine="fake"))).run_all()
    result = fitmod.fit_project(ds, der)
    ds.close()
    led.close()
    return result


def _scores(result, block):
    out = []
    for bf in result:
        if bf.block == block and not bf.missing and bf.score == bf.score:
            out.append(bf.score)
    return out


@pytest.mark.parametrize("block", sorted(LIMITS))
def test_small_signal_blocks_round_trip(fitted, block):
    scores = _scores(fitted, block)
    assert scores, f"no {block} fit at all -- the pipeline stopped producing it"
    worst = max(scores)
    assert worst <= LIMITS[block], (
        f"{block} worst residual {worst:.4g} exceeds {LIMITS[block]}; the fake DUT is inside the "
        f"model's own form, so this is a pipeline regression, not a harder fit")


def test_the_bias_blocks_are_essentially_exact(fitted):
    for block in ("idc", "yout", "psrr", "noise"):
        scores = [bf.score for bf in fitted
                  if bf.port.startswith("IB_") and bf.block == block
                  and not bf.missing and bf.score == bf.score]
        if scores:
            assert max(scores) < 0.1, (block, max(scores))


def test_nothing_silently_went_missing(fitted):
    missing = [(bf.port, bf.block) for bf in fitted if bf.missing]
    # A stub port must never be fitted at all, and nothing else may be quietly absent.
    assert not any(port == "VDD0P8_C" for port, _b in missing)
    for bf in fitted:
        if bf.missing:
            assert bf.notes, f"{bf.port}.{bf.block} is missing with no reason given"
