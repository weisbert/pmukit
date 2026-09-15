"""Verification: the grades, the HB health check, the oscillator bench and the regression suite.

This is the last thing between a fitted model and somebody else's simulation, and it answers
three separate questions that are easy to confuse:

  * `grades`      -- *is the model RIGHT?*  Per corner, per port, per block, against one
                     documented table of limits, with "never run" kept distinct from "wrong".
  * `hb`          -- *does a large-signal term BREAK the solver?*  A real driven harmonic
                     balance on a real Spectre, one `ls` term at a time.  This is the only
                     thing allowed to turn an `ls` term on.
  * `system`      -- *does the model survive being INSIDE a limit cycle?*  A self-oscillating
                     bench powered through the model, because METHODOLOGY records that a
                     standalone driven HB is too easy: it converges where the coupled `oschb`
                     does not.

and a fourth that keeps the other three honest:

  * `regression`  -- fifteen synthetic LDOs through the whole flow, scored against a stored
                     baseline that records WHICH ENGINE produced it.

Public API::

    verify_project(project, *, root=None, hb=True, system=False, engine=None) -> dict

Returns a JSON-safe dict: `{"grades", "rollup", "hb_check", "envelope", "not_run", "notes"}`.
`hb=False` needs no simulator at all, so the Model screen is useful on a machine that has none.
"""
from __future__ import annotations

import pathlib

from .. import jsonio, paths
from ..errors import PmuError
from . import grades as _grades
from .grades import (LIMITS, Limit, explain_limits, grade_block, grade_project, limit_for,
                     rollup, rollup_table)

__all__ = ["verify_project", "grades", "hb", "system", "regression", "LIMITS", "Limit",
           "limit_for", "explain_limits", "grade_block", "grade_project", "rollup",
           "rollup_table", "render"]

grades = _grades


def __getattr__(name):
    """`hb`, `system` and `regression` are imported LAZILY.

    `grades` is pure python and always available; the other three reach for a simulator, a
    backend or the fixture tree, and a machine with none of those must still be able to import
    `pmukit.verify` and grade a fit.
    """
    if name in ("hb", "system", "regression"):
        import importlib
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(name)


# --------------------------------------------------------------------------- inputs
def _project_dir(project: str, root) -> pathlib.Path:
    """`root` is accepted in BOTH spellings, because the two callers disagree.

    `pmukit.cli.cmd_verify` passes the PROJECT directory (`$PMUKIT_DATA/<project>`); the web
    shell and `emit.deliver()` pass the DATA ROOT.  Guessing wrong would write `verify.json`
    into a directory that is not the project's, so the ambiguity is resolved by LOOKING: a
    directory that holds this project's `config.json` is the project directory.
    """
    if root is None:
        return paths.project_dir(project)
    p = pathlib.Path(root)
    if (p / "config.json").is_file() or p.name == str(project):
        return p
    return p / project


def _load(project: str, d: pathlib.Path):
    """(FitResult, Dataset|None, DerivedConfig|None) for a project directory."""
    from ..config import DerivedConfig
    from ..dataset import Dataset
    from ..fit import FitResult

    fit_path = d / "fit.json"
    if not fit_path.is_file():
        raise PmuError(
            what=f"{project} has no fitted model to verify.",
            why="verify grades a fit: it reads fit.json, the parameters the fitter wrote. "
                f"{fit_path} does not exist.",
            do=[f"Run the fit first: pmukit fit {project}",
                "Or run the Model screen, which does the same thing."],
            where=str(fit_path))
    fit = FitResult.from_dict(jsonio.read(fit_path))
    der = DerivedConfig.load(d / "derived.json") if (d / "derived.json").is_file() else None
    ds = None
    if (d / "dataset").is_dir():
        try:
            ds = Dataset.open(d / "dataset")
        except PmuError:
            ds = None
    return fit, ds, der


def _envelope(derived, rails, corners, ls_default_on, notes):
    """The validity envelope the deliverable will carry.

    Built by `pmukit.emit`, deliberately: "what was characterized" has exactly one definition
    in this tool and it lives next to the emitter that writes `envelope.json`.  Duplicating it
    here would let the Model screen and the deliverable disagree about the valid range, which
    is the one thing contract 4 says must never happen.
    """
    from ..emit import _envelope as build
    return build(derived, rails, corners, ls_default_on, notes)


# --------------------------------------------------------------------------- the entry point
def verify_project(project, fit=None, dataset=None, derived=None, *, root=None, hb=True,
                   system=False, engine=None) -> dict:
    """Grade a fitted project, optionally running the HB health check on a real simulator.

    Documented signature (the one the CLI and the web route are written against)::

        verify_project(project, *, root=None, hb=True, system=False, engine=None) -> dict

    `fit`, `dataset` and `derived` are accepted positionally as well, because
    `pmukit/server.py` calls `fn(pr.name, fit, pr.dataset(), pr.derived())` -- see the note in
    BUILD_REPORT about the two call sites.  Passing them skips the reload; leaving them out
    makes this function read the project directory itself, which is what the CLI does.

    `hb=False` runs with NO simulator: grading is a pure function of the fit, so the Model
    screen stays useful on a machine that has no Spectre.  `system=True` adds the autonomous
    oscillator bench, which is slower and is the one that catches what a driven HB cannot.
    """
    project = str(getattr(project, "project", None) or project)
    d = _project_dir(project, root)

    if fit is None or derived is None or dataset is None:
        loaded_fit, loaded_ds, loaded_der = _load(project, d)
        fit = fit if fit is not None else loaded_fit
        dataset = dataset if dataset is not None else loaded_ds
        derived = derived if derived is not None else loaded_der
    if hasattr(fit, "get") and not hasattr(fit, "fits"):       # a plain dict from JSON
        from ..fit import FitResult
        fit = FitResult.from_dict(fit)

    notes: list[str] = list(getattr(fit, "notes", None) or [])
    rows = grade_project(fit, derived=derived, dataset=dataset)
    roll = rollup(rows)
    not_run = [f"{g.port} {g.block} at corner {g.corner}: {g.detail}"
               for g in rows if g.grade == "not_run"]
    not_run += _grades.never_run_lines(dataset)

    corners = sorted({g.corner for g in rows}) or \
        [str(c) for c in ((getattr(derived, "process", None) or {}).get("corners") or [])]
    rails = [p for p, t in (getattr(fit, "ports", None) or {}).items() if t == "rail"]

    hb_report: dict = {"status": "not_run",
                       "notes": ["the HB health check was not run, so no large-signal term is "
                                 "signed off for HB use; every `ls` term stays OFF."]}
    if hb:
        from . import hb as hbmod
        site = _site(engine)
        try:
            hb_report = hbmod.hb_check(fit, derived, project=project, site=site,
                                       root=d, grades=rows)
        except PmuError as exc:
            hb_report = {"status": "not_run", "error": exc.to_dict(),
                         "notes": [f"the HB health check could not run: {exc.what}"]}
    ls_on = list(hb_report.get("ls_default_on") or [])

    sys_report: dict | None = None
    if system:
        from . import system as sysmod
        try:
            sys_report = sysmod.oscillator_check(fit, derived, project=project,
                                                 site=_site(engine), root=d)
        except PmuError as exc:
            sys_report = {"status": "not_run", "error": exc.to_dict()}

    env_notes = []
    if getattr(derived, "en", None):
        env_notes.append("EN power-up ramp: usable, not signed off -- it only guarantees that a "
                         "bench toggling EN does not blow up. Sign startup off on the real LDO.")
    envelope = _envelope(derived, rails, corners, ls_on, env_notes) if derived is not None \
        else None

    notes.append(f"acceptance limits used:\n{explain_limits()}")
    out = {
        "project": project,
        "grades": [g.to_json() for g in rows],
        "rollup": roll,
        "hb_check": hb_report,
        "envelope": envelope.to_json() if envelope is not None else {},
        "not_run": not_run,
        "notes": notes,
        "worst": _grades.worst(rows),
        "ls_default_on": ls_on,
    }
    if sys_report is not None:
        out["system_check"] = sys_report
    return json_safe(out)


def json_safe(obj):
    """STRICT-JSON only: NaN becomes null, infinity becomes the string "inf"/"-inf".

    `json.dumps` happily writes a bare `NaN`, which `JSON.parse` in the web shell refuses, and
    `verify.json` is read by both the CLI and the browser.  NaN here always means "this number
    was not produced" (a residual Spectre did not print, a block with no score), and `null` is
    the honest spelling of that; an infinite RATIO is a real measurement -- the baseline was
    zero -- so it keeps a name instead of being flattened to null.
    """
    import math as _math
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, (str, int)):
        return obj
    if isinstance(obj, float):
        if _math.isnan(obj):
            return None
        if _math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    try:                                    # numpy scalars and anything else number-like
        return json_safe(obj.item())
    except AttributeError:
        return obj


def _site(engine):
    from ..site import SiteConfig
    site = SiteConfig.load()
    if engine:
        site.engine = str(engine)
    return site


def render(result: dict) -> str:
    """The whole verification as plain text -- the red zone has no screenshots."""
    from ..deliverable import Grade

    rows = [Grade.from_json(g) for g in (result.get("grades") or [])]
    out = [f"# {result.get('project', '?')} -- verification", "",
           rollup_table(rows), ""]
    nr = result.get("not_run") or []
    out.append("Never run:" if nr else "Never run: nothing -- every planned item produced data.")
    for item in nr:
        out.append(f"  - {item}")
    out.append("")
    hbr = result.get("hb_check") or {}
    if hbr:
        from . import hb as hbmod
        out.append(hbmod.render(hbr))
    sysr = result.get("system_check")
    if sysr:
        from . import system as sysmod
        out.append(sysmod.render(sysr))
    out.append(explain_limits())
    return "\n".join(out)
