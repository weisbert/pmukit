"""The four simulation engines, behind one interface (`pmukit.runner.Backend`).

    spectre_ssh   the desk engine: rsync-free tar-over-ssh to a Linux host, `spectre -64` there.
    donau_alps    the red-zone engine: `dsub` into the Donau queue, ALPS on the compute node.
    fake          ANALYTIC results, no simulator at all -- the whole pipeline testable anywhere.
    dry_run       write the netlist and the recipe, submit nothing, invent nothing.

`make_backend(name, site)` is the only lookup; nothing else in pmukit dispatches on an engine
name.  Every backend answers `available()` without raising, so the New screen and the CLI can
offer what this machine can actually do.
"""
from __future__ import annotations

from ..errors import PmuError

__all__ = ["BACKENDS", "make_backend", "probe_all"]

#: engine name -> (module, class) inside this package.
BACKENDS = {
    "spectre_ssh": ("spectre_ssh", "SpectreSSHBackend"),
    "donau_alps": ("donau_alps", "DonauAlpsBackend"),
    "fake": ("fake", "FakeBackend"),
    "dry_run": ("dry_run", "DryRunBackend"),
}


def make_backend(name: str, site, **kwargs):
    """Build the backend called `name` for this site.  Import is lazy, per engine, so a machine
    with no cluster tooling never imports the cluster module."""
    entry = BACKENDS.get(str(name))
    if entry is None:
        raise PmuError(
            what=f"There is no simulation backend named {name!r}.",
            why="The runner dispatches on `site.engine`; every engine is one module in "
                "pmukit/backends, and this name matches none of them.",
            do=[f"Use one of: {', '.join(sorted(BACKENDS))}.",
                "PMUKIT_ENGINE=dry_run runs the whole flow without a simulator.",
                "PMUKIT_ENGINE=fake runs it with analytic (clearly synthetic) results."],
            where="pmukit/backends/__init__.py:BACKENDS")
    module_name, class_name = entry
    module = __import__(f"{__name__}.{module_name}", fromlist=[class_name])
    return getattr(module, class_name)(site, **kwargs)


def probe_all(site) -> dict:
    """``{engine: (usable?, reason)}`` for every backend -- what `pmukit doctor` prints.

    `available()` never raises by contract; a backend that breaks that contract is reported as
    unusable with the exception text, so the probe itself can never take the CLI down.
    """
    out = {}
    for name in sorted(BACKENDS):
        try:
            out[name] = make_backend(name, site).available()
        except Exception as exc:                       # noqa: BLE001 -- a probe never raises
            out[name] = (False, f"{exc.__class__.__name__}: {exc}")
    return out
