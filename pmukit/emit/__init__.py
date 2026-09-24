"""The model emitter: fitted parameters -> one HB-safe Verilog-A file per process corner, every
one defining the same module `PMU_<project>` (the library's `section` picks the corner).

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

    deliver_project(project, *, root=None, on_progress=None) -> dict   # the whole Deliver step
    deliver(project, *, root=None, fit=None, ...) -> pathlib.Path
    saved_fit(project, *, root=None) -> (FitResult, note)   # fit.json; re-fits only if stale
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

__all__ = ["deliver", "deliver_project", "saved_fit", "delivery_grades", "verify_state",
           "verify_inputs", "STALE_VERIFY", "NO_VERIFY", "emit_va", "build_va", "module_name", "lint", "primitives", "scs",
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


def _netlist_sha(project: str, root) -> str:
    """12 hex of the netlist the runs were planned from -- the same number the New screen shows
    for the project's copy -- or "" when the project has none on disk.

    Looked up in the order the web shell's `Project.netlist_path()` uses: the copy the New screen
    last read (state.json), config.json's `netlist` (relative to the project, then to the current
    directory), then `netlists/input.scs`."""
    base = (pathlib.Path(root) if root is not None else paths.data_root()) / project
    cands: list[pathlib.Path] = []
    for name, key in (("state.json", "netlist"), ("config.json", "netlist")):
        try:
            val = str((jsonio.read(base / name) or {}).get(key) or "")
        except (OSError, ValueError, AttributeError):
            continue
        if val:
            p = pathlib.Path(val)
            cands += [p] if p.is_absolute() else [base / p, p]
    cands.append(base / "netlists" / "input.scs")
    for p in cands:
        try:
            if p.is_file():
                return jsonio.sha_file(p, 12)
        except OSError:
            continue
    return ""


def verify_inputs(verify) -> dict:
    """What `pmukit verify` decided (verify.json), as keyword arguments for `deliver()`.

    Without this the report says "nothing was graded" even when verify.json is sitting right
    next to it, and every large-signal term stays off because nothing told deliver() which ones
    cleared the HB check.  `pmukit deliver` and the Deliver screen both go through here.
    """
    kw: dict = {}
    if not isinstance(verify, dict):
        return kw
    grades = [Grade(port=g["port"], corner=g["corner"], block=g["block"], grade=g["grade"],
                    detail=g.get("detail", ""), score=g.get("score"))
              for g in (verify.get("grades") or []) if g.get("port") and g.get("block")]
    if grades:
        kw["grades"] = grades
    if verify.get("ls_default_on"):
        kw["ls_default_on"] = list(verify["ls_default_on"])
    if verify.get("not_run"):
        kw["not_run"] = [str(x) for x in verify["not_run"]]
    return kw


def _resolve_fit(project: str, derived: DerivedConfig, fit, root):
    """The fitted blocks: whatever the caller passed, else the project's saved fit (fit.json),
    re-fitting only when that is missing or older than the dataset."""
    if fit is not None:
        return fit
    return saved_fit(project, root=root, derived=derived)[0]


#: What report.md / grades.json / the Deliver screen say when the grades are the fit's own.
STALE_VERIFY = "verify is older than the fit -- grades are from the fit"
NO_VERIFY = "verify has not run on this fit -- grades are from the fit"


def _project_dir(project: str, root) -> pathlib.Path:
    return (pathlib.Path(root) if root is not None else paths.data_root()) / project


def _fit_is_current(fit_path: pathlib.Path, fit, ds_dir: pathlib.Path) -> tuple[bool, str]:
    """(current?, why not). The fit records the sha of the dataset it was fitted on; when it did
    not (an older fit.json), the file times decide: index.json is rewritten on every write."""
    index = ds_dir / "index.json"
    if not index.is_file():
        return True, ""                      # nothing to be older than: use what was fitted
    want = str(getattr(fit, "dataset_sha", "") or "")
    if want:
        try:
            ds_mod = importlib.import_module("pmukit.dataset")
            ds = ds_mod.Dataset.open(ds_dir)
            try:
                have = ds.sha()
            finally:
                close = getattr(ds, "close", None)
                if close:
                    close()
        except Exception:                                   # noqa: BLE001 -- fall back to times
            have = ""
        if have:
            return (have == want, "" if have == want else
                    f"the dataset changed since the fit (fit.json was fitted on dataset {want}, "
                    f"the dataset is now {have})")
    try:
        newer = index.stat().st_mtime > fit_path.stat().st_mtime
    except OSError:
        newer = False
    return (not newer, "the dataset was written after fit.json" if newer else "")


def saved_fit(project: str, *, root=None, derived=None, on_progress=None,
              save: bool = True) -> tuple:
    """(FitResult, note) -- the fit the Model screen shows, i.e. fit.json, WITHOUT re-fitting.

    Deliver used to re-fit the whole dataset (as long as the fit itself -- minutes) and could
    hand over a model that is not the one the Model screen graded. Now the saved fit is the
    model; only when fit.json is missing, unreadable or older than the dataset is the dataset
    fitted again, and `note` then says so in one sentence (the web job and the CLI print it).
    The fresh fit is written back to fit.json (`save`), so the Model screen, verify and the
    deliverable all look at the same one.

    `on_progress(message, fraction)` follows a re-fit (fraction 0..1 of the fit alone).
    `pmukit.fit` is imported LAZILY and its absence degrades with a four-part error.
    """
    d = _project_dir(project, root)
    fit_path, ds_dir = d / "fit.json", d / "dataset"
    try:
        fit_mod = importlib.import_module("pmukit.fit")
    except ImportError as exc:
        raise PmuError(
            what="deliver() was called without fitted parameters and the fitter is not available.",
            why=f"the emitter turns fitted block parameters into a model; it cannot produce them, "
                f"and importing pmukit.fit failed: {exc}.",
            do=["pass fit=<FitResult, or the list of BlockFit records> to deliver()",
                "or run the Model screen / `pmukit fit <project>` first and hand the result over"],
            where="pmukit/emit/__init__.py:saved_fit") from None
    why = ""
    if fit_path.is_file():
        try:
            fit = fit_mod.FitResult.from_dict(jsonio.read(fit_path))
        except (OSError, ValueError, PmuError) as exc:
            why = f"fit.json could not be read ({getattr(exc, 'what', exc)})"
        else:
            ok, why = _fit_is_current(fit_path, fit, ds_dir)
            if ok:
                return fit, ""
    else:
        why = "there is no fit.json yet"
    fit = _fit_dataset(project, derived, root, fit_mod, on_progress)
    if save:
        jsonio.write(fit_path, fit.to_dict() if hasattr(fit, "to_dict") else fit)
    return fit, f"re-fitted the dataset: {why}"


def _fit_dataset(project: str, derived, root, fit_mod, on_progress):
    d = _load_derived(project, derived, root)
    ds_mod = importlib.import_module("pmukit.dataset")
    ds_dir = _project_dir(project, root) / "dataset"
    if not (ds_dir / "index.json").is_file():
        raise PmuError(
            what=f"no fitted model and no characterization dataset for project {project!r}.",
            why="the deliverable is emitted from the saved fit (fit.json), which does not exist, "
                f"and {ds_dir} does not exist either -- nothing has been measured yet.",
            do=["run the Plan and Run screens (or `pmukit run <project>`), then fit",
                "or pass fit=<FitResult> to deliver() if the fit already happened elsewhere"],
            where=str(ds_dir))
    kw = {}
    if on_progress is not None:
        def _step(done, total, port, cell):
            where = "/".join(str(v) for v in (cell or {}).values() if v is not None)
            on_progress(f"re-fitting {port}{' at ' + where if where else ''} -- step "
                        f"{done + 1} of {total}", done / max(1, total))
        kw["on_progress"] = _step
    return fit_mod.fit_project(ds_mod.Dataset.open(ds_dir), d, **kw)


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
            tb_state_note: str = "", netlist_sha: str = "", graded_by: str = "",
            provisional: str = "", on_progress=None) -> pathlib.Path:
    """Write the whole contract-4 deliverable and return its stamped directory.

    One `.va` per process corner -- every one defining the SAME module `PMU_<project>` -- the
    Spectre section library, `envelope.json`, `report.md` (with its `grades.json` sidecar),
    `provenance.json`, `interface.json` (the PMU's pins in order and the instance line) and
    `hb_check.txt` -- the full conditioning report.  `grades` comes from `pmukit.verify`; when it
    is not supplied the report says so honestly instead of inventing a verdict, and
    `provisional` is the sentence report.md prints when the grades are the fit's own.

    With no `fit=`, the saved fit (fit.json) is used -- see `saved_fit()`; nothing is re-fitted
    unless it is missing or older than the dataset.  `deliver_project()` is the whole step the
    CLI and the web shell run.  `on_progress(message, fraction)` is called once per corner.

    The model is marked HB-ready only when the conditioning lint passes on EVERY corner and
    `hb_robust` is True.
    """
    say = on_progress or (lambda _m, _f: None)
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
        netlist_sha=netlist_sha or _netlist_sha(project, root),
        tb_state_note=tb_state_note, host=sitenv.host().value,
        extra={"user": who} if who else {})

    writer = DeliverableWriter(project, root=root, stamp=stamp)
    modules, ports_by_corner, checks, notes, skipped = {}, {}, {}, [], {}
    iface_by_corner: dict = {}
    rails_seen, ls_seen = [], []
    n = len(corner_list)
    for i, corner in enumerate(corner_list):
        say(f"emitting corner {corner} ({i + 1} of {n}): the .va and its conditioning check",
            0.9 * i / n)
        built = build_va(fit, d, corner, provenance=prov, hb_robust=hb_robust, project=project,
                         flicker_mode=flicker_mode, ls_default_on=ls_default_on)
        writer.add_va(corner, built["text"], provenance=prov)
        modules[corner] = built["module"]
        ports_by_corner[corner] = built["ports"]
        iface_by_corner[corner] = built["interface"]
        checks[corner] = lint.report(built["netlist"], f_max_hz=float(
            (d.freq or {}).get("stop_hz", 0.0) or 0.0), harmonic=harmonic,
            grounds=built["grounds"], label=f"{built['module']} at {corner}")
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
        # The emitted module has no enable behaviour: EN is a pass-through pin (contract 4).
        # The ramp numbers exist in the fit and the grades; the model does not play them.
        env_notes.append("EN power-up ramp: characterized (usable, not signed off), but this "
                         "model has no enable behaviour -- EN is a pass-through pin and the "
                         "model is always on. Sign startup off on the real LDO.")
    off = [p for p in ls_seen if p not in set(ls_default_on)]
    if off:
        env_notes.append(
            "Large-signal load-event terms are OFF by default on " + ", ".join(off) +
            " (instance parameter load_en_<rail>); the `ls` tier may only default on after the "
            "HB first-step residual check, which lives in the verify milestone.")
    say(f"writing the section library, envelope, report and provenance ({n} corner"
        f"{'s' if n != 1 else ''} emitted)", 0.92)
    envelope = _envelope(d, rails_seen, corner_list, ls_default_on, env_notes,
                         ports=(set(rails_seen) | set(d.biases or {}) | set(d.en or {})))
    iface = d.interface or {}
    codes = [c for c in ((d.vset or {}).get("codes") or []) if c is not None]
    params = [("vset", int(codes[0]))] if codes else []
    writer.write_scs(provenance=prov,
                     extra_lines=scs.extra_lines(modules, library=f"PMU_{project}",
                                                 ports_by_corner=ports_by_corner,
                                                 envelope=envelope, params=params,
                                                 interface_by_corner=iface_by_corner,
                                                 inst=str(iface.get("inst") or ""),
                                                 master=str(iface.get("master") or "")))
    writer.write_envelope(envelope)
    pins = _pins_for_report(iface_by_corner)
    writer.write_interface(_interface_record(project, modules, iface_by_corner, iface, params,
                                             pins, ls_seen, bool(iface.get("pins"))))

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
                        stubs=list(d.stubs or {}), pins=pins, graded_by=graded_by,
                        provisional=provisional)
    writer.write_provenance(prov)
    (writer.path / HB_CHECK_NAME).write_text(
        _hb_check_text(project, checks, notes, hb_robust), encoding="utf-8", newline="\n")
    jsonio.write(writer.path / "hb_check.json",
                 {"project": project, "hb_ready": hb_ok, "hb_robust": hb_robust,
                  "corners": {k: v for k, v in checks.items()}, "notes": notes})
    out = writer.finish()
    say(f"deliverable {out.name} written", 1.0)
    return out


def verify_state(project: str, *, root=None) -> tuple:
    """(verify.json or None, state) with state one of "current" / "stale" / "missing".

    "stale" is the Model screen's rule: fit.json was rewritten AFTER verify.json, so verify's
    grades and its HB check judge a model that no longer exists."""
    d = _project_dir(project, root)
    vpath, fpath = d / "verify.json", d / "fit.json"
    try:
        ver = jsonio.read(vpath) if vpath.is_file() else None
    except (OSError, ValueError):
        ver = None
    if not isinstance(ver, dict):
        return None, "missing"
    try:
        stale = fpath.stat().st_mtime > vpath.stat().st_mtime
    except OSError:
        stale = False
    return ver, ("stale" if stale else "current")


def delivery_grades(project: str, fit, *, root=None, derived=None) -> tuple:
    """(deliver() keyword arguments, verify state) -- the grades the deliverable carries.

    verify.json current: its grades, its not-run list and the `ls` terms its HB check cleared.
    Older than the fit, or never run: the fit's own grades (`pmukit.verify.grades`, a pure
    function of the fit -- no simulator), every `ls` term OFF (no HB check has seen THIS model),
    and a `provisional` sentence report.md and grades.json print. Delivering is never blocked."""
    ver, state = verify_state(project, root=root)
    if state == "current":
        kw = verify_inputs(ver)
        kw["graded_by"] = "verify"
        return kw, state
    gmod = importlib.import_module("pmukit.verify.grades")
    ds = None
    ds_dir = _project_dir(project, root) / "dataset"
    if (ds_dir / "index.json").is_file():
        try:
            ds = importlib.import_module("pmukit.dataset").Dataset.open(ds_dir)
        except Exception:                                  # noqa: BLE001 -- holes are optional
            ds = None
    if not hasattr(fit, "fits"):
        fit = importlib.import_module("pmukit.fit").FitResult.from_dict(fit)
    rows = gmod.grade_project(fit, derived=_load_derived(project, derived, root), dataset=ds)
    not_run = [f"{g.port} {g.block} at corner {g.corner}: {g.detail}"
               for g in rows if g.grade == "not_run"]
    not_run += gmod.never_run_lines(ds)
    kw = {"grades": rows, "not_run": not_run, "graded_by": "fit",
          "provisional": STALE_VERIFY if state == "stale" else NO_VERIFY}
    return kw, state


def deliver_project(project: str, *, root=None, derived=None, on_progress=None,
                    stamp=None) -> dict:
    """The Deliver step: `pmukit deliver` and the Deliver screen both run exactly this.

    1. the saved fit (fit.json) -- re-fitted only when missing or older than the dataset,
       and then `refit` says why;
    2. the grades: verify.json when it is newer than the fit, else the fit's own, marked
       provisional (`delivery_grades`);
    3. `deliver()`, reporting per corner.

    `on_progress(message, fraction)` sees the whole step, 0..1. Returns {"path", "stamp",
    "refit", "verify", "graded_by", "provisional"}.
    """
    say = on_progress or (lambda _m, _f: None)
    d = _load_derived(project, derived, root)
    say("reading the saved fit (fit.json)", 0.03)
    fit, refit = saved_fit(project, root=root, derived=d,
                           on_progress=lambda m, f: say(m, 0.05 + 0.55 * f))
    if refit:
        say(refit, 0.6)
    start = 0.6 if refit else 0.08
    kw, state = delivery_grades(project, fit, root=root, derived=d)
    say({"current": "grades: verify.json",
         "stale": f"grades: {STALE_VERIFY}",
         "missing": f"grades: {NO_VERIFY}"}[state], start)
    out = deliver(project, root=root, fit=fit, derived=d, stamp=stamp,
                  on_progress=lambda m, f: say(m, start + (0.99 - start) * f), **kw)
    return {"path": out, "stamp": out.name, "refit": refit, "verify": state,
            "graded_by": kw.get("graded_by", ""), "provisional": kw.get("provisional", "")}


def _pins_for_report(iface_by_corner: dict) -> list:
    """The module's pins for report.md: one row per pin, pass-through if it is in ANY corner
    (a rail that could not be emitted at ss is pass-through there), with the corners named."""
    corners = list(iface_by_corner)
    if not corners:
        return []
    out = []
    for e in iface_by_corner[corners[0]]:
        where = [c for c in corners
                 if any(x["pin"] == e["pin"] and not x["modeled"] for x in iface_by_corner[c])]
        row = {"pin": e["pin"], "role": e["role"], "modeled": not where, "what": e["what"]}
        if where:
            hit = next(x for x in iface_by_corner[where[0]] if x["pin"] == e["pin"])
            row["what"] = hit["what"] + ("" if len(where) == len(corners)
                                         else f" (corners: {', '.join(where)})")
        out.append(row)
    return out


def _interface_record(project: str, modules: dict, iface_by_corner: dict, iface: dict, params,
                      pins: list, ls_ports: list, pmu_order: bool) -> dict:
    """interface.json: what the Deliver screen shows under "Use it in your testbench".

    `module` / `instance_line` are THE master and THE line -- one for every corner, since every
    section defines the same module. `modules` / `instance` (per corner) stay for a reader
    written against the older per-corner layout; on a new deliverable their values agree."""
    inst = str(iface.get("inst") or "")
    per_corner = {c: scs.instance_line(m, iface_by_corner.get(c) or [], inst=inst, params=params)
                  for c, m in modules.items()}
    names = sorted(set(modules.values()))
    return {
        "library": f"PMU_{project}",
        "module": names[0] if len(names) == 1 else "",
        "instance_line": next(iter(per_corner.values()), "") if len(names) == 1 else "",
        "sections": list(modules),
        "pmu_inst": inst, "pmu_master": str(iface.get("master") or ""),
        "pmu_order": pmu_order,
        "pins": pins,
        "pass_through": [p["pin"] for p in pins if not p["modeled"]],
        "modules": dict(modules),
        "instance": per_corner,
        "params": {"vset": dict(params).get("vset"),
                   "load_en": [f"load_en_{p}" for p in ls_ports]},
    }


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
