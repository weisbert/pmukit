"""Where pmukit keeps real data.

Rule (README "Data rule"): customer netlists, PSF, datasets and delivered models NEVER
enter the repo. They live under $PMUKIT_DATA (default ~/pmukit_data).
"""
from __future__ import annotations

import os
import pathlib


def data_root() -> pathlib.Path:
    """Root of the out-of-tree data area. Honours $PMUKIT_DATA."""
    env = os.environ.get("PMUKIT_DATA")
    root = pathlib.Path(env).expanduser() if env else pathlib.Path.home() / "pmukit_data"
    return root


def project_dir(project: str) -> pathlib.Path:
    return data_root() / project


def sim_dir(project: str, fallback: pathlib.Path | None = None) -> pathlib.Path:
    """Where a project's SIMULATIONS run (decks, logs, PSF) -- kept apart from the tool and data.

    `$PMUKIT_SIM_ROOT/<project>`, else `$WORK_ROOT/pmukit/<project>` (the box's simulation
    area, as LDO_modeling used `$WORK_ROOT/ldo_modeling`), else `fallback` or the project's
    data dir (the desk, which has no simulation area).  See `pmukit.sitenv.sim_root`.
    """
    from . import sitenv
    root = sitenv.sim_root().value
    if root:
        return pathlib.Path(root).expanduser() / project
    return fallback if fallback is not None else project_dir(project)


def runs_dir(project: str) -> pathlib.Path:
    """`<sim_dir>/runs` -- one `<run_id>/` per planned run.  The ONE place both the CLI and the
    web shell take it from (they used to disagree: `<project>/<id>` vs `<project>/runs/<id>`)."""
    return sim_dir(project) / "runs"


def ensure_project(project: str) -> pathlib.Path:
    p = project_dir(project)
    for sub in ("", "dataset", "deliver", "digest", "netlists", "logs"):   # runs: runs_dir()
        (p / sub if sub else p).mkdir(parents=True, exist_ok=True)
    return p
