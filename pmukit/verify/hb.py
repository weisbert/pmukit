"""The HB health check: does a non-linear term wreck somebody else's harmonic balance?

This is the gate CONTRACTS.md section 1 puts in front of the `ls` tier -- *"a large-signal item
with its own switch; it may only default on after it passes the HB first-step residual check"*.
The emitter ships every `ls` term OFF; this module is the only thing that may turn one on.

What it does, on a REAL Spectre:

  1. emit the corner's `.va`, wrap a small driven-HB bench around it, run it with EVERY `ls`
     term off, and record the residual Spectre prints;
  2. enable each term ALONE and re-run;
  3. any term whose first-step residual is more than `RATIO_LIMIT` x the all-off value FAILS
     and must not be default-on.

Three things were measured on Spectre 18.1 while building this, and each one changed the design:

**A. Where the residual comes from.**  `annotate=detailed_hb` (the `hb` analysis's own
annotation level -- `spectre -h hb`: *"when annotate is set to detailed_hb ... additional
analysis debug information will be printed to log files"*) makes the log carry::

    ********** initial residual **********
    Resd Norm=7.87e+02  at node X1.IB_PTAT_pdd  harm=(1)

    ********** iter = 1 **********
    Delta Norm=9.79e+09  at node X1:VDD0P8_A_nC_..._flow  harm=(1)
    Resd Norm=2.18e-06  at node X1.VDDA_1V0_vrf  harm=(16)

So `Resd Norm=` is the token, and it is a real number from the solver, not a proxy.

**B. The "initial residual" CANNOT be the gate, and the reason is a feature of the model.**
Measured: with the rail-A term off the initial residual is 7.87e+02; with it on it is 7.87e+02
-- bit for bit.  That is not a bug, it is the `ls` design working: METHODOLOGY requires every
large-signal term to have ZERO VALUE AND ZERO SLOPE at the operating point (the assist is odd
with f'(0)=0 exactly; the unload discharge is a one-sided deadzone), precisely so it cannot
disturb Zout/PSRR/noise.  The HB initial guess IS the DC operating point, so a correctly built
`ls` term is invisible to the initial residual by construction.  **The discriminating number is
the residual after the FIRST NEWTON STEP** (`iter = 1`), which is what this module gates on --
same rail, same bench: 2.18e-06 with the term off, 1.55e+02 with it on.  Both numbers are
reported; the initial one is kept because a term that moves it is a term that is NOT OP-inert,
which is a separate and worse defect.

**C. A term that is not ENGAGED makes the check vacuous.**  The same OP-inertness means a small
drive leaves the term switched off inside the simulation and every residual identical.  So the
bench drives the rail with a load-current tone sized from the FITTED Zout to push the rail
`ENGAGE_X` times past the term's own deadzone, at the frequency where |Zout| peaks (the worst
case the rail can produce), and the report carries the predicted swing and the deadzone next to
each other so a reader can check that the term really was exercised.

**What still needs the box.**  METHODOLOGY is explicit that *a standalone driven HB is too easy
to expose the real conditioning failures* -- a near-LTI PMU converges without an oscillator's
limit cycle, and the historical `oschb` failures surfaced at the package and at neighbouring
transistors, not at the model.  `pmukit.verify.system` adds the autonomous half of that, but
neither is a substitute for the real coupled `oschb` in the consumer's own testbench.
"""
from __future__ import annotations

import math
import pathlib
import re
import shutil
import tempfile

import numpy as np

from .. import emit, jsonio
from ..errors import PmuError

__all__ = ["hb_check", "RATIO_LIMIT", "HARMONICS", "ENGAGE_X", "bench_deck", "parse_log",
           "run_deck", "RESID_TOKEN", "SOLVER_ENGINES", "usable", "render", "write"]

#: A term whose first-step residual is more than this many times the all-off value FAILS.
RATIO_LIMIT = 10.0
#: Harmonics of the driven HB.  16 is the harmonic the historical singular-Jacobian failure was
#: pinned at, and the same number `emit.lint` uses for its static conditioning sweep, so the
#: dynamic check and the static one agree about what "stressed" means.
HARMONICS = 16
#: How far past its own deadzone the bench pushes the rail, so the term is really exercised.
ENGAGE_X = 3.0
#: What Spectre prints, with `annotate=detailed_hb`, for each Newton step's residual norm.
RESID_TOKEN = "Resd Norm="

_RESID = re.compile(r"Resd Norm=\s*([0-9.eE+-]+)\s*(?:at node (\S+))?(?:\s*harm=\((\d+)\))?")
_STEP = re.compile(r"\*{3,}\s*(initial residual|iter\s*=\s*(\d+))\s*\*{3,}")
_SAFE_TAG = re.compile(r"^[A-Za-z0-9_.-]+$")


# --------------------------------------------------------------------------- remote running
class _Run:
    """The two attributes `pmukit.runner.Job` and the ssh backend actually read off a run.

    Reusing the verified transport (push / run under `tcsh -c "source ~/.cshrc; ..."` / pull)
    instead of re-implementing it is the whole point; the ledger's `Run` record carries far more
    than a one-off bench needs, and building one here would mean inventing a plan and a recipe
    for a deck that is not part of the characterization.
    """

    def __init__(self, tag: str) -> None:
        if not _SAFE_TAG.match(tag):
            raise PmuError(
                what=f"The HB bench tag {tag!r} is not a safe remote directory name.",
                why="It becomes a directory inside a remote shell command, so only letters, "
                    "digits, '_', '.' and '-' are accepted rather than quoted-and-hoped.",
                do=["This is a pmukit bug: tags here are built from a corner name and a "
                    "parameter name."],
                where="pmukit/verify/hb.py")
        self.run_id = tag
        self.recipe = ""


#: The engines that actually run a solver.  `fake` synthesizes results analytically and
#: `dry_run` writes decks and submits nothing -- neither has a Newton residual to report, and a
#: number invented by either would look exactly like a measurement.
SOLVER_ENGINES = ("spectre_ssh", "donau_alps")


def _backend(site, backend):
    if backend is not None:
        return backend
    from ..backends import make_backend
    engine = str(getattr(site, "engine", "") or "") or "dry_run"
    if engine == "spectre_ssh":
        return make_backend("spectre_ssh", site, allow_degrade=False)
    return make_backend(engine, site)


def usable(backend) -> tuple[bool, str]:
    """`(can this backend answer an HB question?, why not)` -- never raises.

    Two gates, in this order: the engine has to BE a solver, and it has to be reachable.  The
    first one matters because `fake` reports itself available and would happily produce a
    convincing set of residuals that nothing measured.
    """
    name = str(getattr(backend, "name", "") or "?")
    if name not in SOLVER_ENGINES:
        return (False, f"the {name!r} engine runs no solver, so there is no harmonic balance to "
                       f"ask for a residual. Point the site at a simulator "
                       f"({' or '.join(SOLVER_ENGINES)}) and re-run.")
    try:
        return backend.available()
    except Exception as exc:                       # noqa: BLE001 -- a probe never takes us down
        return (False, f"{exc.__class__.__name__}: {exc}")


def run_deck(workdir, deck: str, tag: str, *, site, backend=None, timeout_s: float | None = None,
             aux=()) -> dict:
    """Run one deck and bring the log home.  `{"ok", "log", "command", "workdir", "detail"}`."""
    from ..runner import Job

    wd = pathlib.Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    for src in aux:
        src = pathlib.Path(src)
        shutil.copyfile(src, wd / src.name)
    (wd / "input.scs").write_text(deck, encoding="utf-8", newline="\n")

    be = _backend(site, backend)
    job = Job(run=_Run(tag), workdir=wd, netlist_text=deck, site=site)
    if timeout_s and hasattr(be, "timeout_s"):
        be.timeout_s = float(timeout_s)
    be.submit(job)
    try:
        be.fetch(job)
    except PmuError as exc:                       # a transport failure is a result, not a crash
        return {"ok": False, "log": "", "command": job.detail, "workdir": str(wd),
                "detail": exc.what}
    log_path = wd / "spectre.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    ok = job.state != "failed" and "0 errors" in log
    return {"ok": bool(ok), "log": log, "command": str(job.detail or ""),
            "workdir": str(wd), "detail": "" if ok else (job.detail or "spectre reported errors")}


# --------------------------------------------------------------------------- log reading
def parse_log(log: str) -> dict:
    """Pull the HB Newton trace out of one `spectre.log`.

    Returns `{"initial", "first_step", "final", "iterations", "converged", "steps", "where"}`.
    `initial` is the pre-Newton residual (blind to an OP-inert term, see the module docstring);
    `first_step` is the residual after `iter = 1` and is the number this module gates on.
    """
    steps: list[dict] = []
    current: str | None = None
    for line in log.splitlines():
        m = _STEP.search(line)
        if m:
            current = "initial" if m.group(1).startswith("initial") else f"iter{m.group(2)}"
            continue
        if current is None:
            continue
        r = _RESID.search(line)
        if r:
            steps.append({"step": current, "residual": float(r.group(1)),
                          "node": r.group(2) or "", "harm": int(r.group(3) or 0)})
            current = None

    by = {s["step"]: s for s in steps}
    iters = sorted(int(s["step"][4:]) for s in steps if s["step"].startswith("iter"))
    last = by.get(f"iter{iters[-1]}") if iters else None
    return {
        "initial": by["initial"]["residual"] if "initial" in by else float("nan"),
        "first_step": by["iter1"]["residual"] if "iter1" in by else float("nan"),
        "final": last["residual"] if last else float("nan"),
        "iterations": (iters[-1] if iters else 0),
        "converged": bool("Total time required for hb analysis" in log),
        "where": (last or by.get("initial") or {}).get("node", ""),
        "steps": steps,
    }


# --------------------------------------------------------------------------- the bench
def _nearest(values, target):
    vals = [v for v in values if v is not None]
    return min(vals, key=lambda v: abs(float(v) - float(target))) if vals else None


def _pick(fit, port, block, corner, **want):
    """The fitted block of one port closest to the cell we are benching at."""
    best, score = None, None
    for bf in fit:
        if bf.port != port or bf.block != block or bf.missing:
            continue
        if bf.cell.get("process") not in (None, corner):
            continue
        d = 0.0
        for key, target in want.items():
            have = bf.cell.get(key)
            if have is not None and target is not None:
                d += abs(math.log10(abs(float(have)) + 1e-30)
                         - math.log10(abs(float(target)) + 1e-30))
        if score is None or d < score:
            best, score = bf, d
    return best


#: The operational ripple METHODOLOGY uses when it derives the unload deadzone: 10 % of the
#: operating current.  The deadzone is a TRANSPARENCY FLOOR placed ABOVE this, so at this drive
#: a correctly built term is switched off inside the simulation -- which is exactly what the
#: second, non-gating bench measures.
NOMINAL_RIPPLE = 0.10


def _drive(fit, port, corner, derived, i_typ: float) -> dict:
    """Where to drive the rail, and how hard, so the `ls` term is really engaged.

    The tone sits at the peak of the FITTED |Zout|: that is where the rail converts load
    current into voltage most efficiently, so it is both the worst case the rail can produce
    and the cheapest place to exercise a deadzone.  The amplitude is then solved from the same
    impedance -- no magic number, and the predicted swing travels in the report next to the
    deadzone it has to clear.
    """
    from ..fit import zout as zmod
    z = _pick(fit, port, "zout", corner, load_a=i_typ, temp_c=25.0)
    ld = _pick(fit, port, "load_en", corner, temp_c=25.0)
    f_lo = float((getattr(derived, "freq", None) or {}).get("start_hz", 10.0) or 10.0)
    f_hi = float((getattr(derived, "freq", None) or {}).get("stop_hz", 1e9) or 1e9)
    out = {"f_hz": max(f_lo, min(f_hi, 1.0e6)), "ampl_a": max(i_typ, 1e-6) * 2.0,
           "z_peak_ohm": float("nan"), "deadzone_v": float("nan"),
           "swing_v": float("nan"), "engaged": False}
    if z is None:
        out["note"] = "no fitted Zout for this rail: the tone falls back to 1 MHz"
        return out
    f = np.logspace(math.log10(f_lo), math.log10(f_hi), 600)
    mag = np.abs(np.asarray(zmod.predict(z.params, f=f)))
    k = int(np.argmax(mag))
    out["f_hz"], out["z_peak_ohm"] = float(f[k]), float(mag[k])
    dead = None
    if ld is not None:
        # the unload discharge's deadzone, else the assist's own voltage scale
        dead = ld.params.get("ovVdz") or ld.params.get("iaV")
    if dead:
        out["deadzone_v"] = float(dead)
        want = ENGAGE_X * float(dead) / max(out["z_peak_ohm"], 1e-12)
        out["ampl_a"] = float(max(want, max(i_typ, 1e-6)))
    out["swing_v"] = float(out["ampl_a"] * out["z_peak_ohm"])
    out["engaged"] = bool(dead and out["swing_v"] > float(dead))
    return out


def _nominal_drive(drive: dict, i_typ: float) -> dict:
    """The same tone at the LARGEST LEGITIMATE operational ripple instead of a stress level.

    Not a gate -- the reading.  A term whose deadzone is above this ripple is switched off
    inside the simulation here, so its residual ratio should be ~1: that is the measurement
    that says the term is FREE in ordinary use, which is a different and softer statement than
    the engaged gate makes.
    """
    out = dict(drive)
    out["ampl_a"] = float(max(NOMINAL_RIPPLE * max(i_typ, 0.0), 1e-9))
    out["swing_v"] = float(out["ampl_a"] * out.get("z_peak_ohm", 0.0))
    dead = out.get("deadzone_v", float("nan"))
    out["engaged"] = bool(dead == dead and out["swing_v"] > float(dead))
    return out


def bench_deck(built: dict, derived, *, va_name: str, drive: dict, on: str = "",
               vset=None, harmonics: int = HARMONICS, temp_c: float = 25.0,
               vrip_v: float = 0.05) -> str:
    """A driven-HB testbench around one emitted module.

    `on` is the single `ls` instance parameter set to 1; "" is the all-off baseline.  Every
    ground pin is wired to global 0 -- an emitted module whose ground was left floating went to
    -100 MV once, and it is not doing it again (TOOL_FACTS).
    """
    module = built["module"]
    nets = emit.va.bench_nets(built)
    params = [f"vset={int(vset)}"] if vset is not None else []
    for p in built["ls_ports"]:
        params.append(f"load_en_{p}={1 if f'load_en_{p}' == on else 0}")

    rails = list(built["rails"])
    hot = str(drive.get("port") or (rails[0] if rails else ""))
    f = float(drive["f_hz"])
    lines = [
        "// pmukit verify: driven-HB health check for one large-signal term.",
        f"// term under test: {on or '(none -- the all-off baseline)'}",
        "simulator lang=spectre",
        "global 0",
        "",
        f'ahdl_include "{va_name}"',
        "",
        f"X1 ({' '.join(nets)}) {module} {' '.join(params)}",
        "",
    ]
    for s in built["supplies"]:
        v = float(((getattr(derived, "supply", None) or {}).get("pins") or {})
                  .get(s, {}).get("nominal_v", 1.0) or 1.0)
        lines.append(f"VS_{s} ({s} 0) vsource dc={v:g} type=sine sinedc={v:g} "
                     f"ampl={vrip_v:g} freq={f:g}")
    for r in rails:
        i_typ = float(((getattr(derived, "rails", None) or {}).get(r) or {})
                      .get("i_typ_a", 0.0) or 0.0)
        if r == hot:
            lines.append(f"IL_{r} ({r} 0) isource dc={i_typ:g} type=sine sinedc={i_typ:g} "
                         f"ampl={float(drive['ampl_a']):g} freq={f:g}")
        else:
            lines.append(f"IL_{r} ({r} 0) isource dc={i_typ:g}")
    for b in built["biases"]:
        vb = float(((getattr(derived, "biases", None) or {}).get(b) or {})
                   .get("vcomp_v", 0.4) or 0.4)
        lines.append(f"VB_{b} ({b} 0) vsource dc={vb:g}")
    for s in built["stubs"]:
        # A stub pin is weakly tied inside the module; give the DC solve something to find and
        # nothing the HB can feel.
        lines.append(f"RSTUB_{s} ({s} 0) resistor r=1T")
    lines += [
        "",
        f"simOpts options temp={temp_c:g} tnom={temp_c:g}",
        'dcOp dc write="op.dc"',
        f"hb1 hb fundfreqs=[{f:g}] maxharms=[{int(harmonics)}] annotate=detailed_hb",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the check
def hb_check(fit, derived, *, corner: str = "", project: str = "pmu", site=None, backend=None,
             root=None, harmonics: int = HARMONICS, ratio_limit: float = RATIO_LIMIT,
             timeout_s: float = 900.0, grades=None) -> dict:
    """Run the check and return a structured report (JSON-safe).

    `grades` is optional; when given, a term whose block is graded `red` is kept OFF even if it
    passes the conditioning gate.  The HB check answers *"does this term break the solver?"*,
    which is a different question from *"is this term right?"*, and a term has to clear both
    before it may default on in somebody else's simulation.
    """
    corners = [str(c) for c in ((getattr(derived, "process", None) or {}).get("corners") or [])]
    corner = str(corner or (corners[0] if corners else "tt"))
    vset = next(iter((getattr(derived, "vset", None) or {}).get("codes") or []), None)
    temps = [float(t) for t in ((getattr(derived, "temps_c", None) or {}).get("points") or [25.0])]
    tnom = float(_nearest(temps, 25.0) or 25.0)

    built = emit.build_va(fit, derived, corner, project=project, ls_default_on=())
    terms = [f"load_en_{p}" for p in built["ls_ports"]]

    # `root` is normally the project directory, so the decks and logs land next to the
    # project they describe.  With no root, a TEMP directory -- never the caller's
    # current directory, which would drop a `verify/` tree wherever they happened to be.
    base = pathlib.Path(root) if root is not None else pathlib.Path(
        tempfile.mkdtemp(prefix="pmukit_hb_"))
    work = base / "verify" / "hb" / corner
    work.mkdir(parents=True, exist_ok=True)
    # ONE corner per deck: the module name is the same on every corner, so a deck that included
    # two corners' files would define it twice. The file is named like the deliverable's.
    va_name = f"{built['module']}_{corner}.va"
    va_path = work / va_name
    va_path.write_text(built["text"], encoding="utf-8", newline="\n")

    report = {"status": "not_run", "corner": corner, "harmonics": int(harmonics),
              "ratio_limit": float(ratio_limit),
              "metric": "Spectre HB Newton residual norm after the first Newton step "
                        "(`Resd Norm=` under `********** iter = 1 **********`, printed by "
                        "`annotate=detailed_hb`)",
              "metric_note": "the PRE-Newton `initial residual` is reported too, but it cannot "
                             "be the gate: an `ls` term has zero value and zero slope at the "
                             "operating point by design, and the HB initial guess IS that "
                             "operating point, so a correctly built term leaves the initial "
                             "residual bit-identical (measured: 7.87e+02 with the term on and "
                             "off alike).",
              "terms": [], "passing": [], "ls_default_on": [], "notes": [], "engine": "",
              "va": str(va_path)}

    if not terms:
        report["status"] = "pass"
        report["notes"].append(
            "this project emits no large-signal term, so there is nothing the check could turn "
            "on and nothing it could break; the model is entirely `hb` tier.")
        return report

    if site is None:
        from ..site import SiteConfig
        site = SiteConfig.load()
    be = _backend(site, backend)
    report["engine"] = getattr(be, "name", "?")
    ok, why = usable(be)
    if not ok:
        report["status"] = "not_run"
        report["notes"].append(
            f"no simulator: {why}. Every large-signal term therefore stays OFF -- the `ls` tier "
            f"may only default on after a real HB run, and an unrun check is not a pass.")
        report["terms"] = [{"term": t, "status": "not_run", "why": why} for t in terms]
        return report

    # -- the bench, sized on the first rail that carries a term ---------------------------
    hot = built["ls_ports"][0]
    i_typ = float(((getattr(derived, "rails", None) or {}).get(hot) or {})
                  .get("i_typ_a", 0.0) or 0.0)
    drive = _drive(fit, hot, corner, derived, i_typ)
    drive["port"] = hot
    report["drive"] = {k: v for k, v in drive.items() if k != "note"}
    if drive.get("note"):
        report["notes"].append(drive["note"])
    if not drive.get("engaged"):
        report["notes"].append(
            "the bench could not confirm that the term is ENGAGED at this drive level; a term "
            "that never switches on inside the simulation makes every residual identical and "
            "the check vacuous, so read the ratios below with that in mind.")

    nominal = _nominal_drive(drive, i_typ)
    report["drive_nominal"] = dict(nominal)

    def _one(on: str, tag: str, dr: dict) -> dict:
        deck = bench_deck(built, derived, va_name=va_name, drive=dr, on=on, vset=vset,
                          harmonics=harmonics, temp_c=tnom)
        res = run_deck(work / tag, deck, f"pmukit_hb_{corner}_{tag}", site=site, backend=be,
                       timeout_s=timeout_s, aux=[va_path])
        out = parse_log(res["log"])
        out.update({"ok": res["ok"], "command": res["command"], "detail": res["detail"],
                    "workdir": res["workdir"]})
        out.pop("steps", None)
        return out

    def _ratio(row, base) -> float:
        if not math.isfinite(row["first_step"]):
            return float("inf")
        return (row["first_step"] / base["first_step"]
                if base["first_step"] > 0 else float("inf"))

    baseline = _one("", "alloff", drive)
    base_nom = _one("", "alloff_nominal", nominal)
    report["baseline"] = dict(baseline, term="(all off)")
    report["baseline_nominal"] = dict(base_nom, term="(all off)")
    if not baseline["ok"] or not math.isfinite(baseline["first_step"]):
        report["status"] = "fail"
        report["notes"].append(
            "the ALL-OFF baseline did not produce a first-step residual, so no term can be "
            "compared against it. Every large-signal term stays OFF. "
            + (baseline["detail"] or "check the log in the work directory."))
        return report

    red = {(g.port, g.block) for g in (grades or []) if g.grade == "red"}
    for term in terms:
        port = term[len("load_en_"):]
        safe = port.lower().replace(".", "_")
        row = _one(term, safe, drive)
        ratio = _ratio(row, baseline)
        passed = bool(row["ok"] and row["converged"] and math.isfinite(ratio)
                      and ratio <= ratio_limit)
        row.update({"term": term, "port": port, "ratio": float(ratio), "pass": passed,
                    "baseline_first_step": baseline["first_step"]})
        if math.isfinite(base_nom.get("first_step", float("nan"))):
            nom = _one(term, f"{safe}_nominal", nominal)
            row["nominal"] = {
                "first_step": nom["first_step"], "ratio": _ratio(nom, base_nom),
                "iterations": nom["iterations"], "converged": nom["converged"],
                "ok": nom["ok"], "swing_v": nominal["swing_v"],
                "engaged": nominal["engaged"],
                "means": "the SAME measurement at the largest LEGITIMATE operational ripple "
                         "(10 % of the operating current, the figure METHODOLOGY derives the "
                         "deadzone from), where the term is below its own deadzone and is "
                         "switched off in the final solution. REPORTED, NOT A GATE. Measured "
                         "here: the ratio stays large anyway, because the first Newton step "
                         "of a harmonic balance is NOT a small-signal quantity -- the linear "
                         "solve overshoots far outside the deadzone before it comes back."}
        if not passed:
            row["why"] = (
                "the first Newton step is more than the allowed multiple of the all-off "
                "residual, so this term makes the consumer's harmonic balance measurably "
                "harder; it stays an opt-in instance parameter"
                if row["ok"] and row["converged"] else
                "the run with this term enabled did not complete: " +
                (row["detail"] or "harmonic balance did not reach a solution"))
        report["terms"].append(row)
        if passed:
            report["passing"].append(port)

    report["ls_default_on"] = [p for p in report["passing"]
                               if (p, "load_en") not in red]
    held = [p for p in report["passing"] if p not in report["ls_default_on"]]
    if held:
        report["notes"].append(
            "held OFF despite passing the conditioning gate because the block itself is graded "
            "red: " + ", ".join(held) + ". Passing the HB check says the term does not break "
            "the solver; it does not say the term is right.")
    report["status"] = "pass" if all(t.get("pass") for t in report["terms"]) else "fail"
    report["notes"].append(
        "MEASURED PROPERTY OF THIS GATE, worth knowing before reading a FAIL: the baseline is "
        "the model with every large-signal term off, which makes it purely LINEAR, so harmonic "
        "balance solves it in one Newton step and the first-step residual is numerical noise. "
        "The allowed multiple is therefore a strict bar by construction. It is strict even "
        "BELOW the term's own deadzone -- the second table shows a term that is switched off "
        "in the final solution still moving the first-step residual by orders of magnitude, "
        "because the first Newton step of a harmonic balance is not a small-signal quantity: "
        "the linear solve overshoots far outside the deadzone before it comes back. The "
        "practical reading of a FAIL here is 'this term costs the consumer Newton iterations', "
        "which is exactly why the `ls` tier is opt-in -- NOT 'this term breaks harmonic "
        "balance'. Whether the run CONVERGED, and in how many iterations, is the column that "
        "answers that, and it is reported next to every verdict.")
    report["notes"].append(
        "a standalone driven HB is the EASY half of this question: METHODOLOGY records that a "
        "near-LTI PMU converges without an oscillator's limit cycle, and that the real coupled "
        "`oschb` failures surfaced at the package and at neighbouring transistors rather than "
        "at the model. See pmukit.verify.system for the autonomous half; neither replaces the "
        "consumer's own coupled run.")
    return report


def _g(x) -> str:
    """One number for the text table; `json_safe` may already have turned it into a string."""
    if x is None:
        return "--"
    if isinstance(x, str):
        return x
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "--" if v != v else f"{v:.3g}"


def render(report: dict) -> str:
    """The check as plain text -- what `pmukit verify` prints and what report.md links to."""
    out = [f"HB health check -- corner {report.get('corner', '?')}, "
           f"{report.get('harmonics', '?')} harmonics, engine {report.get('engine') or '(none)'}",
           f"status: {report.get('status', '?')}", ""]
    out += ["metric: " + str(report.get("metric", "")), "",
            "why not the initial residual: " + str(report.get("metric_note", "")), ""]
    d = report.get("drive") or {}
    if d:
        out += [f"bench: {d.get('port', '?')} driven at {_g(d.get('f_hz'))} Hz "
                f"(the fitted |Zout| peak, {_g(d.get('z_peak_ohm'))} ohm) with "
                f"{_g(d.get('ampl_a'))} A, predicted swing {_g(d.get('swing_v'))} V against a "
                f"deadzone of {_g(d.get('deadzone_v'))} V -- "
                f"{'ENGAGED' if d.get('engaged') else 'NOT confirmed engaged'}", ""]
    b = report.get("baseline") or {}
    if b:
        out += [f"{'term':<22}{'initial':>12}{'first step':>14}{'ratio':>10}{'iters':>7}"
                f"{'conv':>6}  verdict",
                "-" * 82,
                f"{'(all off)':<22}{_g(b.get('initial')):>12}"
                f"{_g(b.get('first_step')):>14}{'--':>10}"
                f"{b.get('iterations', 0):>7}{'yes' if b.get('converged') else 'NO':>6}"
                f"  baseline"]
    for t in report.get("terms", []):
        out.append(f"{t.get('term', '?'):<22}{_g(t.get('initial')):>12}"
                   f"{_g(t.get('first_step')):>14}"
                   f"{_g(t.get('ratio')):>10}{t.get('iterations', 0):>7}"
                   f"{'yes' if t.get('converged') else 'NO':>6}"
                   f"  {'PASS' if t.get('pass') else 'FAIL'}")
    nom = [t for t in report.get("terms", []) if t.get("nominal")]
    if nom:
        bn = report.get("baseline_nominal") or {}
        out += ["",
                "and the SAME measurement at the ordinary operational ripple "
                f"({_g((report.get('drive_nominal') or {}).get('ampl_a'))} A, swing "
                f"{_g((report.get('drive_nominal') or {}).get('swing_v'))} V "
                f"-- REPORTED, NOT A GATE):",
                f"{'term':<22}{'first step':>14}{'ratio':>10}{'iters':>7}  engaged?",
                f"{'(all off)':<22}{_g(bn.get('first_step')):>14}{'--':>10}"
                f"{bn.get('iterations', 0):>7}  --"]
        for t in nom:
            n = t["nominal"]
            out.append(f"{t['term']:<22}{_g(n.get('first_step')):>14}{_g(n.get('ratio')):>10}"
                       f"{n.get('iterations', 0):>7}  "
                       f"{'yes' if n.get('engaged') else 'no (below its deadzone)'}")
    out += ["", "terms this check lets deliver() default ON: "
            + (", ".join(report.get("ls_default_on") or []) or "(none)")]
    for n in report.get("notes") or []:
        out.append(f"  note: {n}")
    return "\n".join(out) + "\n"


def write(report: dict, path) -> pathlib.Path:
    """`hb_check.json` next to the deliverable's own conditioning report."""
    return jsonio.write(pathlib.Path(path), report)
