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


def ensure_project(project: str) -> pathlib.Path:
    p = project_dir(project)
    for sub in ("", "dataset", "runs", "deliver", "digest", "netlists", "logs"):
        (p / sub if sub else p).mkdir(parents=True, exist_ok=True)
    return p
