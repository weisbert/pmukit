"""Site configuration: engine, queue, CPUs -- configured once at install, never per project.

Contract 0b ends with this row on purpose: which simulator runs the jobs is a property of the
machine (desk with a Linux VM over ssh, or the red-zone box with Donau/ALPS), not of the PMU being
characterized. Keeping it out of the project config is what lets the same `config.json` /
`derived.json` move between the desk and the box unchanged.

Lives in `$PMUKIT_DATA/site.json`. Environment overrides, applied by `load()` after the file:

    PMUKIT_ENGINE     engine name   (donau_alps | spectre_ssh | dry_run | fake)
    PMUKIT_SSH_HOST   ssh host alias for the spectre_ssh engine
    PMUKIT_CPUS       cpus per job  (positive integer)

The defaults are the BOX's: submit to Donau's short queue and run ALPS (the site's Spectre
licenses are scarce; `simulator` switches a Donau job to Spectre).  The desk, which runs Spectre
on a VM over ssh, says so once in its own site.json.  Facts the box's environment already knows --
the ALPS install, the user id, the license -- are read by `pmukit.sitenv`, not stored here.
"""
from __future__ import annotations

import os
import pathlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields

from . import jsonio
from .errors import PmuError
from .paths import data_root

ENGINES = ("donau_alps", "spectre_ssh", "dry_run", "fake")
"""donau_alps: submit to the Donau queue (the box; runs `simulator`, ALPS by default).
spectre_ssh: run Spectre on a Linux host over ssh (the desk). dry_run: write the netlists, submit
nothing. fake: synthesize results for tests."""

SIMULATORS = ("alps", "spectre")

SITE_FILE = "site.json"


def _err(what: str, why: str, do, where: str) -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=where)


@dataclass
class SiteConfig:
    """How this machine runs simulations."""

    engine: str = "donau_alps"
    queue: str = "short"
    cpus: int = 8
    simulator: str = "alps"
    ssh_host: str = "ewave-vm"
    remote_workdir: str = "~/pmukit_work"
    spectre_cmd: str = "spectre"
    project_account: str = ""

    # ---------------------------------------------------------------- serialization
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d, *, where: str = "") -> "SiteConfig":
        if not isinstance(d, Mapping):
            raise _err("The site config is not a JSON object.",
                       "site.json holds one flat object: engine, queue, cpus and the ssh details.",
                       ["Delete the file to fall back to the defaults, or fix the JSON"],
                       where or SITE_FILE)
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise _err(f"The site config has unknown key(s): {sorted(unknown)}.",
                       "site.json is closed: an unknown key is a typo that would be silently ignored.",
                       [f"Remove {sorted(unknown)}; the accepted keys are {sorted(known)}"],
                       where or SITE_FILE)
        cfg = cls(**{k: d[k] for k in known if k in d})
        cfg.validate(where or SITE_FILE)
        return cfg

    # ---------------------------------------------------------------- validation
    def validate(self, where: str = SITE_FILE) -> None:
        if self.engine not in ENGINES:
            raise _err(f"site engine {self.engine!r} is not one of {list(ENGINES)}.",
                       "The runner dispatches on this name; an unknown engine has no backend.",
                       [f"Set engine to one of {list(ENGINES)}",
                        "PMUKIT_ENGINE=dry_run runs the whole flow without a simulator"], where)
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, int) or self.cpus < 1:
            raise _err(f"site cpus is not a positive integer ({self.cpus!r}).",
                       "cpus becomes the -mt / queue slot count on every submitted job.",
                       ["Set cpus to a positive integer, e.g. 8"], where)
        if self.simulator not in SIMULATORS:
            raise _err(f"site simulator {self.simulator!r} is not one of {list(SIMULATORS)}.",
                       "It picks the solver a Donau job runs; ALPS is the default because "
                       "Spectre licenses are scarce.",
                       [f"Set simulator to one of {list(SIMULATORS)}"], where)
        for name in ("queue", "ssh_host", "remote_workdir", "spectre_cmd", "project_account"):
            val = getattr(self, name)
            if not isinstance(val, str):
                raise _err(f"site {name} is not a string ({val!r}).",
                           f"{name} is pasted verbatim into the submit command.",
                           [f"Set {name} to a string (empty string when it does not apply)"], where)
        if self.engine == "spectre_ssh" and not self.ssh_host.strip():
            raise _err("site engine is spectre_ssh but ssh_host is empty.",
                       "The spectre_ssh engine copies the run directory to that host and runs there.",
                       ["Set ssh_host to the alias in your ~/.ssh/config",
                        "Or set engine to dry_run to plan without a simulator"], where)
        if self.engine == "donau_alps" and not self.queue.strip():
            raise _err("site engine is donau_alps but queue is empty.",
                       "Donau needs a queue name to submit into; there is no default queue.",
                       ["Set queue to the queue name your site uses",
                        "Or set engine to dry_run to plan without submitting"], where)

    # ---------------------------------------------------------------- storage
    @staticmethod
    def default_path() -> pathlib.Path:
        return data_root() / SITE_FILE

    @classmethod
    def load(cls, path=None) -> "SiteConfig":
        """Read `$PMUKIT_DATA/site.json` (or `path`); a missing file means the defaults.
        `PMUKIT_ENGINE` / `PMUKIT_SSH_HOST` / `PMUKIT_CPUS` override whatever was read."""
        p = pathlib.Path(path) if path is not None else cls.default_path()
        if p.is_file():
            try:
                d = jsonio.read(p)
            except (OSError, ValueError) as exc:
                raise _err(f"Could not read the site config at {p}.",
                           f"The file is not valid UTF-8 JSON: {exc}.",
                           ["Fix the JSON, or delete the file to fall back to the defaults"],
                           str(p)) from None
            cfg = cls.from_dict(d, where=str(p))
        else:
            cfg = cls()
        cfg._apply_env(str(p))
        cfg.validate(str(p))
        return cfg

    def save(self, path=None) -> pathlib.Path:
        p = pathlib.Path(path) if path is not None else self.default_path()
        self.validate(str(p))
        return jsonio.write(p, self.to_dict())

    # ---------------------------------------------------------------- env
    def _apply_env(self, where: str) -> None:
        engine = os.environ.get("PMUKIT_ENGINE")
        if engine:
            self.engine = engine.strip()
        host = os.environ.get("PMUKIT_SSH_HOST")
        if host:
            self.ssh_host = host.strip()
        cpus = os.environ.get("PMUKIT_CPUS")
        if cpus:
            try:
                self.cpus = int(cpus)
            except ValueError:
                raise _err(f"PMUKIT_CPUS is not an integer ({cpus!r}).",
                           "The environment override is parsed as the per-job CPU count.",
                           ["Set PMUKIT_CPUS to a positive integer, e.g. 8",
                            "Or unset it to use the value in site.json"], where) from None
