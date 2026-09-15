"""Shared fixtures for the fitter tests.

The fitters are validated against ANALYTICALLY GENERATED ground truth, which for a port is
stronger than a regression baseline: a stored baseline only proves the code still does what it
did, while a synthesized model with KNOWN parameters proves the ported numerics still recover
the numbers they were written to recover.  There is no simulator in this milestone, and no
module under `pmukit/fit/` may launch one.

Synthetic names only: rails VDD0P8_A / VDD0P8_B, biases IB_PTAT / IB_POLY, project demo_pmu.
"""
import numpy as np

from pmukit import config
from pmukit.dataset import Dataset

#: the contract's AC grid: 10 Hz .. care_up_to_hz at 20 points per decade, endpoints exact
CARE_UP_TO_HZ = 1.0e9
FREQ = np.asarray(config.log_points(config.F_START_HZ, CARE_UP_TO_HZ,
                                    config.n_log_points(config.F_START_HZ, CARE_UP_TO_HZ)),
                  dtype=float)
#: the noise band of contract 0b
NOISE_FREQ = np.asarray(config.log_points(
    config.NOISE_BAND_HZ[0], config.NOISE_BAND_HZ[1],
    config.n_log_points(*config.NOISE_BAND_HZ)), dtype=float)

RAIL = "VDD0P8_A"
BIAS = "IB_PTAT"
TEMPS = [-40.0, 25.0, 125.0]
LOADS = [2e-6, 1e-4, 5e-4, 1e-3]

DIMS = {"process": ["tt"], "temp_c": TEMPS, "vset": [3],
        "load_a": {RAIL: LOADS, "VDD0P8_B": [1e-4]}}

CELL = {"process": "tt", "temp_c": 25.0, "vset": 3, "load_a": 5e-4}
CELL_NOLOAD = {"process": "tt", "temp_c": 25.0, "vset": 3}


def make(tmp_path, dims=None, project="demo_pmu"):
    return Dataset.create(tmp_path / "dataset", project=project, config_sha="c0ffee123456",
                          dims=dims or DIMS)


def declare_ac(ds, var, unit="", dtype="complex128", coord=None):
    ds.declare(var, dims=("process", "temp_c", "vset", "load_a", "freq_hz"), dtype=dtype,
               unit=unit, coord=FREQ if coord is None else coord, coord_name="freq_ac")


def declare_ac_noload(ds, var, unit="", dtype="complex128", coord=None):
    ds.declare(var, dims=("process", "temp_c", "vset", "freq_hz"), dtype=dtype, unit=unit,
               coord=FREQ if coord is None else coord, coord_name="freq_ac_noload")


def zparams(Ra=0.05, La=2e-6, Rpl=1e5, Cout=1e-9, esr=0.5, Rb=1e9, Lb=1e-12,
            La_i=(), Rpl_i=()):
    """A complete Zout parameter dict, exactly as `fit/zout.py` produces one."""
    return {"Ra": Ra, "La": La, "Rpl": Rpl, "La_i": list(La_i), "Rpl_i": list(Rpl_i),
            "Lb": Lb, "Rb": Rb, "Cout": Cout, "esr": esr}


def rel(fit_value, true_value):
    """Signed relative error, as a fraction."""
    return abs(float(fit_value) / float(true_value) - 1.0)
