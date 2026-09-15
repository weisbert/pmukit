"""The dry-run engine: write the deck, submit nothing, invent nothing.

Two jobs.

  1. **Plan without a simulator.**  Everything up to the simulation happens for real -- the
     netlist variants, the recipes, the ledger rows -- so a user can inspect exactly what would
     run before spending queue time.
  2. **The honest degradation target.**  When `spectre_ssh` cannot reach its host it falls back
     here rather than failing the whole sweep, and says so loudly on every run (`degraded_from`
     is carried in the reason, and the runner emits it as an event).

What it deliberately does NOT do: touch the ledger status `skipped_cached`.  That status means
"we already have results"; a dry run has none, and claiming otherwise would hide the run from the
report's NOT RUN list (CONTRACTS.md 0c).  `poll()` answers "skipped" and the runner leaves the
row `planned` with the reason in `error`.
"""
from __future__ import annotations

import pathlib

__all__ = ["DryRunBackend"]


class DryRunBackend:
    """Writes the run directory and stops there."""

    name = "dry_run"

    def __init__(self, site, *, degraded_from: str = "", reason: str = ""):
        self.site = site
        self.timeout_s: float | None = None
        self.degraded_from = str(degraded_from or "")
        self.reason = str(reason or "")

    # ------------------------------------------------------------------ interface
    def available(self) -> tuple[bool, str]:
        if self.degraded_from:
            return True, (f"DEGRADED from {self.degraded_from}: {self.reason} -- decks are "
                          "written, NOTHING is simulated")
        return True, "dry run: decks and recipes are written, no simulator is invoked"

    def submit(self, job) -> str:
        """No submission.  The runner has already written input.scs and recipe.txt; record where."""
        wd = pathlib.Path(job.workdir)
        note = f"dry run: nothing submitted; deck at {wd / 'input.scs'}"
        if self.degraded_from:
            note = (f"DEGRADED from {self.degraded_from} ({self.reason}): nothing submitted; "
                    f"deck at {wd / 'input.scs'}")
        job.detail = note
        job.state = "skipped"
        return ""

    def poll(self, job) -> str:
        return "skipped"

    def fetch(self, job) -> pathlib.Path:
        """There is nothing to fetch; the (empty, non-existent) psf dir is returned unchanged so
        the caller's path handling stays uniform."""
        return pathlib.Path(job.workdir) / "raw"

    def kill(self, job) -> None:
        return None
