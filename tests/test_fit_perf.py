"""Fit performance on the fixture PMU, in the shape the browser QA ran it.

Three temperatures at one corner on the `fake` engine used to take 70 s to fit on a desk that now
does it in ~5 s, and ONE step (VDD0P8_A at 125 C) sat for 60 s with no progress: the joint noise
bank's optimum sat on the corner-separation hinge and trf crawled along the kink for ~10 000
iterations, improving the cost by 1e-7 a step. The stall guard in `fit/noise.py` ends that crawl.

Two kinds of guard live here:

* DETERMINISTIC ones -- the number of joint-solve iterations, that every long block announces
  itself -- which fail on any machine the moment the crawl comes back;
* WALL-CLOCK ones (`@pytest.mark.timing`), with bounds several times the current time and well
  under the old one. Set `PMUKIT_SKIP_TIMING=1` on a slow or shared CI box.
"""
import os
import pathlib
import time

import pytest

from pmukit import fit as fitmod
from pmukit.backends.fake import FakeBackend
from pmukit.config import ProjectConfig, derive
from pmukit.dataset import Dataset
from pmukit.fit import noise
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Runner
from pmukit.site import SiteConfig

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "input.scs"

#: the old fit of this project took 71 s, its worst step 62 s (this desk, before the guard)
FIT_BOUND_S = 30.0
STEP_BOUND_S = 15.0
#: the crawl was 8 000 - 11 000 iterations; a converging bank here takes 90 - 130
ITER_BOUND = 1000

timing = pytest.mark.skipif(os.environ.get("PMUKIT_SKIP_TIMING") == "1",
                            reason="PMUKIT_SKIP_TIMING=1: wall-clock guards are off here")


@pytest.fixture(scope="module")
def qa(tmp_path_factory):
    root = tmp_path_factory.mktemp("perf")
    nl = Netlist.from_file(FIXTURE)
    cfg = ProjectConfig.from_dict({
        "project": "perf", "netlist": str(FIXTURE), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": [-40, 25, 125], "vset_codes": [3],
        "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                  "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                  "EN": "model", "TESTMODE": "ignore"},
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
    runner = Runner(cfg.project, plan, led, site, dataset=ds, root=root,
                    backend=FakeBackend(site))
    runner.run_all()
    notes = [n for rep in runner.reports.values() for n in rep["notes"]]

    banks = []
    real_bank = noise._Bank

    class Counting(real_bank):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            banks.append(self)

    steps, blocks = [], []
    last = [time.perf_counter(), None]

    def on_progress(done, total, port, cell):
        now = time.perf_counter()
        if last[1] is not None:
            steps.append((now - last[0], last[1]))
        last[0], last[1] = now, (port, dict(cell))

    noise._Bank = Counting
    try:
        t0 = time.perf_counter()
        result = fitmod.fit_project(ds, der, on_progress=on_progress,
                                    on_block=lambda p, b, c: blocks.append((p, b, dict(c))))
        elapsed = time.perf_counter() - t0
    finally:
        noise._Bank = real_bank
    steps.append((time.perf_counter() - last[0], last[1]))
    ds.close()
    led.close()
    return {"result": result, "elapsed": elapsed, "steps": steps, "banks": banks,
            "blocks": blocks, "notes": notes}


def test_no_joint_noise_solve_crawls_along_the_separation_hinge(qa):
    iters = [b.iterations for b in qa["banks"]]
    assert iters, "the noise bank was never fitted"
    assert max(iters) < ITER_BOUND, iters


def test_the_guarded_banks_still_fit_the_fake_dut(qa):
    """The fake rail noise is inside the model's form: the guard must not cost accuracy."""
    scores = [bf.score for bf in qa["result"] if bf.block == "noise" and bf.port.startswith("VDD")
              and not bf.missing]
    assert scores and max(scores) < 0.05, scores


def test_every_long_block_announces_itself_before_it_runs(qa):
    """A step that can take seconds says what it is busy with (the Run screen's sub-step)."""
    names = {b for _p, b, _c in qa["blocks"]}
    assert set(fitmod.LONG_BLOCKS) <= names
    temps = {c.get("temp_c") for p, b, c in qa["blocks"] if b == "noise" and p == "VDD0P8_A"}
    assert temps == {-40.0, 25.0, 125.0}


def test_a_plain_fake_run_fills_every_cell_once(qa):
    twice = [n for n in qa["notes"] if "already filled" in n]
    assert not twice, twice


@pytest.mark.timing
@timing
def test_the_fit_is_fast(qa):
    assert qa["elapsed"] < FIT_BOUND_S, f"fit took {qa['elapsed']:.1f} s"


@pytest.mark.timing
@timing
def test_no_single_step_stalls(qa):
    worst = max(qa["steps"], key=lambda s: s[0])
    assert worst[0] < STEP_BOUND_S, f"step {worst[1]} took {worst[0]:.1f} s"
