"""The model emitter: fitted parameters -> one HB-safe Verilog-A module per process corner.

This is the most safety-critical module in the tool, because everything downstream of it runs
inside somebody else's harmonic-balance simulation.  Three rules shape it:

  1. `primitives.py` is a CLOSED whitelist, and every item outside it is on the list because it
     broke a real simulation.  `va.py` writes no text of its own -- it only calls whitelist
     builders, and `Netlist.text()` re-scans the result for banned constructs before returning it.
  2. `lint.py` measures the numeric conditioning of what was actually emitted (the recorded
     ELEMENTS, not a regex over the text) at the model's `f_max` and at a harmonic multiple of it.
     When it fires, `deliver()` refuses to call the model HB-ready.
  3. `deliverable.py` owns the container.  This package produces the `.va` BODY and hands it over.

Public API:

    deliver(project, *, root=None, fit=None, ...) -> pathlib.Path
    emit_va(port_fits, derived, corner, *, provenance, hb_robust=True) -> str
    build_va(...) -> {"text", "netlist", ...}          # emit_va plus what the lint measures
    lint.report(netlist, *, f_max_hz, harmonic=16) -> dict
"""
from __future__ import annotations

import importlib
import pathlib

from .. import jsonio, paths, spec
from ..config import DerivedConfig
from ..deliverable import DeliverableWriter, Envelope, Grade, Provenance
from ..errors import PmuError
from . import lint, primitives, scs
from .primitives import FORBIDDEN, WHITELIST, Netlist
from .va import build_va, emit_va, module_name

__all__ = ["deliver", "emit_va", "build_va", "module_name", "lint", "primitives", "scs",
           "Netlist", "WHITELIST", "FORBIDDEN", "HB_CHECK_NAME"]

#: Where the full conditioning report lands inside the deliverable (report.md links to it).
HB_CHECK_NAME = "hb_check.txt"


# --------------------------------------------------------------------------- inputs
def _load_derived(project: str, derived, root) -> DerivedConfig:
    if derived is not None:
        return derived if isinstance(derived, DerivedConfig) else DerivedConfig.from_dict(derived)
    base = pathlib.Path(root) if root is not None else paths.data_root()
    p = base / project / "derived.json"
    if not p.is_file():
        raise PmuError(
            what=f"no derived config for project {project!r}.",
            why="the emitter reads the ports, split grounds, stubs, load grid, VSET codes and "
                "frequency band out of contract 0b; without it the module has no ports.",
            do=[f"run the New screen (or `pmukit new`) so {p} is written",
                "or pass derived=<DerivedConfig> to deliver()"],
            where=str(p))
    return DerivedConfig.load(p)


def _resolve_fit(project: str, derived: DerivedConfig, fit, root):
    """The fitted blocks: whatever the caller passed, else fit the project's dataset now.

    `pmukit.fit` is imported LAZILY and its absence degrades with a four-part error rather than
    an ImportError traceback -- the emitter's own job does not depend on the fitter being present,
    only on being handed parameters.
    """
    if fit is not None:
        return fit
    try:
        fit_mod = importlib.import_module("pmukit.fit")
        ds_mod = importlib.import_module("pmukit.dataset")
    except ImportError as exc:
        raise PmuError(
            what="deliver() was called without fitted parameters and the fitter is not available.",
            why=f"the emitter turns fitted block parameters into a model; it cannot produce them, "
                f"and importing pmukit.fit failed: {exc}.",
            do=["pass fit=<FitResult, or the list of BlockFit records> to deliver()",
                "or run the Model screen / `pmukit fit <project>` first and hand the result over"],
            where="pmukit/emit/__init__.py:_resolve_fit") from None
    base = pathlib.Path(root) if root is not None else paths.data_root()
    ds_dir = base / project / "dataset"
    if not ds_dir.is_dir():
        raise PmuError(
            what=f"no characterization dataset for project {project!r}.",
            why="with no `fit=` argument the emitter fits the project's dataset itself, and "
                f"{ds_dir} does not exist -- nothing has been measured yet.",
            do=["run the Plan and Run screens (or `pmukit run <project>`) first",
                "or pass fit=<FitResult> to deliver() if the fit already happened elsewhere"],
            where=str(ds_dir))
    return fit_mod.fit_project(ds_mod.Dataset.open(ds_dir), derived)


def _envelope(d: DerivedConfig, rails, corners, ls_default_on, notes, ports=None) -> Envelope:
    """The validity envelope: what was actually characterized, not what was configured."""
    loads = {}
    for p in rails:
        pts = [float(x) for x in ((d.loads or {}).get(p, {}) or {}).get("points_a") or []]
        if pts:
            loads[p] = (min(pts), max(pts))
        else:
            i_typ = ((d.rails or {}).get(p, {}) or {}).get("i_typ_a")
            if i_typ is not None:
                loads[p] = (float(i_typ), float(i_typ))
    temps = [float(t) for t in ((d.temps_c or {}).get("points") or [25.0])]
    return Envelope(
        freq_max_hz=float((d.freq or {}).get("stop_hz", 0.0) or 0.0),
        load_a=loads,
        temp_c=(min(temps), max(temps)),
        corners=list(corners),
        vset_codes=[int(c) for c in ((d.vset or {}).get("codes") or []) if c is not None],
        ls_default_on=list(ls_default_on),
        notes=list(notes),
        # Every characterized port, rails AND biases AND the EN ramp's ports. Without this the
        # envelope's "was this port characterized?" question is answered from the load map, which
        # only rails appear in -- and every fully characterized bias row came out RED.
        ports=sorted(set(ports) if ports else
                     (set(rails) | set(d.biases or {}) | set(d.en or {}))),
    )


# --------------------------------------------------------------------------- the deliverable
def deliver(project: str, *, root=None, fit=None, derived=None, corners=None, grades=None,
            provenance=None, stamp=None, hb_robust: bool = True, harmonic: int = lint.HARMONIC,
            flicker_mode: str = "bank", ls_default_on=(), not_run=(), dataset_sha: str = "",
            tb_state_note: str = "") -> pathlib.Path:
    """Write the whole contract-4 deliverable and return its stamped directory.

    One `.va` per process corner, the Spectre section library, `envelope.json`, `report.md` (with
    its `grades.json` sidecar), `provenance.json` and `hb_check.txt` -- the full conditioning
    report.  `grades` comes from `pmukit.verify` (a later milestone); when it is not supplied the
    report says so honestly instead of inventing a verdict.

    The model is marked HB-ready only when the conditioning lint passes on EVERY corner and
    `hb_robust` is True.
    """
    d = _load_derived(project, derived, root)
    fit = _resolve_fit(project, d, fit, root)
    corner_list = [str(c) for c in (corners or (d.process or {}).get("corners") or [])]
    if not corner_list:
        raise PmuError(
            what=f"project {project!r} names no process corner.",
            why="the deliverable is one Verilog-A file and one Spectre section per corner; with "
                "no corner there is nothing to emit or to select.",
            do=['set "corners": ["tt", "ss", "ff"] in the project config',
                "or pass corners=[...] to deliver()"],
            where="derived.process.corners")

    from .. import sitenv
    who = sitenv.user().value           # $USER -- the employee id on the box
    prov = provenance or Provenance.now(
        config_sha=d.config_sha or "",
        dataset_sha=dataset_sha or str(getattr(fit, "dataset_sha", "") or ""),
        spec_sha=str(getattr(fit, "spec_sha", "") or spec.SPEC_SHA),
        tb_state_note=tb_state_note, host=sitenv.host().value,
        extra={"user": who} if who else {})

    writer = DeliverableWriter(project, root=root, stamp=stamp)
    modules, ports_by_corner, checks, notes, skipped = {}, {}, {}, [], {}
    rails_seen, ls_seen = [], []
    for corner in corner_list:
        built = build_va(fit, d, corner, provenance=prov, hb_robust=hb_robust, project=project,
                         flicker_mode=flicker_mode, ls_default_on=ls_default_on)
        writer.add_va(corner, built["text"], provenance=prov)
        modules[corner] = built["module"]
        ports_by_corner[corner] = built["ports"]
        checks[corner] = lint.report(built["netlist"], f_max_hz=float(
            (d.freq or {}).get("stop_hz", 0.0) or 0.0), harmonic=harmonic,
            grounds=built["grounds"], label=built["module"])
        notes += [f"{corner}: {n}" for n in built["notes"]]
        for p, why in built["skipped"]:
            skipped.setdefault((p, why), []).append(corner)
        for p in built["rails"]:
            if p not in rails_seen:
                rails_seen.append(p)
        for p in built.get("ls_ports") or []:
            if p not in ls_seen:
                ls_seen.append(p)

    env_notes = []
    if d.en:
        env_notes.append("EN power-up ramp: usable, not signed off -- it only guarantees that a "
                         "bench toggling EN does not blow up. Sign startup off on the real LDO.")
    off = [p for p in ls_seen if p not in set(ls_default_on)]
    if off:
        env_notes.append(
            "Large-signal load-event terms are OFF by default on " + ", ".join(off) +
            " (instance parameter load_en_<rail>); the `ls` tier may only default on after the "
            "HB first-step residual check, which lives in the verify milestone.")
    envelope = _envelope(d, rails_seen, corner_list, ls_default_on, env_notes,
                         ports=(set(rails_seen) | set(d.biases or {}) | set(d.en or {})))
    writer.write_scs(provenance=prov,
                     extra_lines=scs.extra_lines(modules, library=f"PMU_{project}",
                                                 ports_by_corner=ports_by_corner,
                                                 envelope=envelope))
    writer.write_envelope(envelope)

    hb_ok = all(c["ok"] for c in checks.values()) and hb_robust
    hb_check = {"status": "pass" if hb_ok else "fail"}
    if not hb_robust:
        hb_check["hb_robust"] = ("FALSE -- this is a diagnostic build carrying the refuted "
                                 "synthesized R-L-C PSRR section; it must never ship")
    for corner, c in checks.items():
        hb_check[f"conditioning {corner}"] = c["summary"]
    hb_check["detail"] = (f"full element-by-element conditioning report, the primitive whitelist "
                          f"and the emitter notes: {HB_CHECK_NAME}")

    writer.write_report(envelope=envelope,
                        grades=[g if isinstance(g, Grade) else Grade.from_json(g)
                                for g in (grades or [])],
                        hb_check=hb_check,
                        not_run=list(not_run) + _never_run(skipped, corner_list),
                        stubs=list(d.stubs or {}))
    writer.write_provenance(prov)
    (writer.path / HB_CHECK_NAME).write_text(
        _hb_check_text(project, checks, notes, hb_robust), encoding="utf-8", newline="\n")
    jsonio.write(writer.path / "hb_check.json",
                 {"project": project, "hb_ready": hb_ok, "hb_robust": hb_robust,
                  "corners": {k: v for k, v in checks.items()}, "notes": notes})
    return writer.finish()


def _never_run(skipped: dict, corners: list) -> list:
    """One line per (port, reason), naming the corners only when it is not every corner."""
    out = []
    for (port, why), where in skipped.items():
        tag = "" if len(where) == len(corners) else f" (corners: {', '.join(where)})"
        out.append(f"{port}: {why}{tag}")
    return out


def _hb_check_text(project: str, checks: dict, notes, hb_robust: bool) -> str:
    out = [f"# {project} -- emitter conditioning report", ""]
    if not hb_robust:
        out += ["*** hb_robust=False: this deliverable carries the REFUTED synthesized R-L-C",
                "*** complex PSRR section. It is a diagnostic build and must never ship.", ""]
    out += ["## Numeric conditioning gate", "",
            "Every element of every emitted corner, evaluated at the model's f_max and at a",
            "harmonic multiple of it. The primary metric is the dynamic range of the TOTAL",
            "branch admittance of each INTERNAL node -- the MNA diagonal -- because a single",
            "element's admittance floor is not a defect on its own (a fitted 20 uH branch really",
            "is 2 MOhm at 20 GHz), while a node whose whole row underflows is what makes a",
            "coupled harmonic-balance Jacobian singular.", ""]
    for corner, c in checks.items():
        out += [f"### corner {corner}", "", lint.render(c), ""]
    out += ["## The primitive whitelist", "",
            "The emitter may write only these; each is on the list because the alternative broke",
            "a real simulation.", ""]
    for name, (what, why) in WHITELIST.items():
        out += [f"- {name}: {what}", f"    {why}"]
    out += ["", "## Forbidden constructs", ""]
    for pat, why in FORBIDDEN.items():
        out += [f"- /{pat}/", f"    {why}"]
    if notes:
        out += ["", "## Emitter notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(out) + "\n"
