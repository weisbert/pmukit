"""Fifteen synthetic LDOs through the whole flow, against a stored baseline.

`tests/fixtures/ldo_gt/` holds fifteen transistor-level LDOs that were built, one at a time, to
BREAK something.  Running them all through netlist -> plan -> run -> dataset -> fit and storing
every block's score is what stops a refactor from quietly making the tool worse.

Three rules keep this suite honest, and each one is here because the alternative teaches the
wrong lesson:

1. **A baseline must never be ambiguous about which engine produced it.**  The numbers from real
   Spectre and the numbers from the `fake` backend are not comparable -- `fake`'s DUT is inside
   the model's own form, so it round-trips to hundredths of a dB, and a `fake` baseline compared
   against a Spectre run would fire on everything.  The engine is recorded in the file, and the
   comparison refuses to run across engines instead of producing a meaningless verdict.

2. **A variant that is SUPPOSED to be hard says so, in the file.**  The private repo's headline
   finding was that the in-band composite score is BLIND to real defects -- HF features above
   100 MHz, large-signal behaviour -- and that four of eight adversarial DUTs passed the
   in-sample composite while failing a new observational gate.  A regression suite that cannot
   express "this one is meant to score badly" trains its reader to treat every high number as a
   bug, which is exactly backwards.  `KNOWN_HARD` carries what each variant breaks, in the words
   of the fixture's own header.

3. **The gate is the GRADE BAND first and the number second.**  What a user sees is
   green/yellow/red; a score that moves without changing the band is drift, and a score that
   crosses a band is a regression.  The numeric tolerance below is set so a block cannot walk
   from comfortably-green to the band edge without tripping.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import re
import shutil
import tempfile

from .. import __version__, jsonio, spec
from ..errors import PmuError
from . import grades as _grades

__all__ = ["VARIANTS", "KNOWN_HARD", "BASELINE_PATH", "GT_DIR", "KIND", "RTOL", "ATOL_FRACTION",
           "variants", "testbench", "run_variant", "run_all", "write_baseline", "load_baseline",
           "compare", "tolerance_for", "render"]

KIND = "pmukit.regression/1"

#: Where the fixtures and the stored baseline live, relative to the repo.
_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[2]
GT_DIR = _REPO / "tests" / "fixtures" / "ldo_gt"
BASELINE_PATH = _REPO / "tests" / "regression" / "baseline.json"

#: A score may grow by this fraction before it counts as a regression.
#: 25 % is chosen against the grade bands, not plucked: a block sitting just inside its green
#: limit (0.8 of it) reaches the limit exactly at +25 %, so the tolerance is the largest one
#: that still cannot let a comfortably-green block walk to the band edge unnoticed.
RTOL = 0.25
#: ... plus an absolute floor, so a score near zero does not trip on its last digit.  The floor
#: is 2 % OF THAT METRIC'S OWN GREEN LIMIT, so it scales with what the metric means: 0.02 dB on
#: a spectrum, 0.2 % on a large-signal droop.  For scale, the emitter's own acceptance is
#: 0.01 dB, so this floor is small enough that it cannot hide a real move.
ATOL_FRACTION = 0.02

#: The bench every variant is characterized at -- the same operating point the fixtures'
#: own acceptance harness uses, so these numbers can be read next to that one.
VIN_V = 1.05
ILOAD_A = 121e-6
ILOAD_OFF_A = 12.1e-6
TEMPS_C = (25.0, 85.0)
CARE_UP_TO_HZ = 1.0e9
"""1 GHz, not the fixtures' own 100 MHz: two of these variants (`ldo_v7_esl`, `ldo_v8_dlc`) put
their defect ABOVE 100 MHz, and the private repo's own finding was that an in-band composite is
BLIND to exactly that. A suite that stops at 100 MHz could not see what it was built to see."""

#: What each variant was built to break, from the fixture's own header.  A high score on one of
#: these is the suite working, not the suite failing.
KNOWN_HARD: dict[str, str] = {
    "ldo_v1_nmos": "an NMOS source follower with a deliberately LOW loop gain, so the rail sits "
                   "about 5 % off its divider target. That offset is the circuit, not the model.",
    "ldo_v2_capless": "cap-less: the output capacitor is nearly invisible behind its ESR, so "
                      "Cout/ESR are underdetermined. Expect the identifiability gate to fire; a "
                      "joint least-squares that pretends to pin them is REJECTED (it diverges).",
    "ldo_v3_miller": "Miller-compensated, and its ground-truth Zout is genuinely NON-PASSIVE "
                     "(Re(Z) < 0 over part of the band). A passive-by-construction RLC cannot "
                     "reproduce that: the residual is a documented representational FLOOR.",
    "ldo_v4_ffpsrr": "a feed-forward PSRR path makes the supply transfer NON-MINIMUM-PHASE. It "
                     "needs the signed complex-conjugate second-order section; historically this "
                     "was the dominant system-level failure that block metrics masked.",
    "ldo_v5_spur": "carries deterministic intrinsic SPUR tones. pmukit's block list has no spur "
                   "block, so the tones ride in the ground truth the rail blocks are fitted to.",
    "ldo_v6_spur2": "two INCOMMENSURATE spur tones, the multi-tone stress. Same caveat as "
                    "ldo_v5_spur, with two fundamentals instead of one.",
    "ldo_v7_esl": "the output cap has a series ESL, so |Zout| dips at the cap's self-resonance "
                  "and then RISES inductively above it. The Zout block has no series-output-"
                  "inductance term, so the HF tail is a MODEL-FORM gap -- and it is invisible "
                  "below 100 MHz, which is why this suite sweeps to 1 GHz.",
    "ldo_v8_dlc": "a double-LC PI output network: the loop senses the on-die node while the "
                  "measurement is at the pin, so |Zout| carries a deep series-resonance NOTCH "
                  "above the loop resonance. Also an HF feature the in-band score cannot see.",
    "ldo_v9_vldo": "only ~50 mV of dropout headroom, so the pass device sits in deep triode. "
                   "Small signal is still fittable and DOES fit here; the defect is large "
                   "signal, and the correct outcome is a NARROWER validity envelope rather "
                   "than a worse fit. CALIBRATION NOTE: this bench's load step is 121 uA to "
                   "12 uA, which is far too small to push the device into dropout, so the "
                   "large-signal block scores WELL in this baseline. That is the bench being "
                   "gentle, not the defect being absent -- the fixture's own numbers come from "
                   "a milliamp-class step.",
    "ldo_v10_3lc": "a three-stage decoupling ladder puts THREE-plus resonances on |Zout| while "
                   "the Zout block realizes at most two R-L branches. The un-modeled resonance "
                   "is a documented ORDER floor; this is the worst-scoring variant by design.",
    "ldo_classab": "a class-AB push-pull output stage. Small signal is base-like and should fit; "
                   "the LARGE-signal droop and recovery are asymmetric in a way a symmetric "
                   "linear model cannot reproduce.",
    "ldo_swbleed": "a load-threshold MODE SWITCH: past ~170 uA a shunt damper switches in a "
                   "second resonant branch, so the Zout topology itself CHANGES with load. The "
                   "parameter schedule is continuous in ln(iload) and cannot step.",
    "ldo_pzmig": "a pole-zero pair that MIGRATES with load, which is the load-scheduling "
                 "assumption under stress rather than the block form.",
    "ldo_qbow": "a mid-band bow on |Zout| that no single resonance reproduces -- the shape gate "
                "under stress.",
}

_SUBCKT = re.compile(r"^subckt\s+(\S+)\s*\(", re.M)

#: The testbench.  It is the CONVENTION testbench of docs/TESTBENCH.md, reduced to the two pins
#: these fixtures have: `VS_<pin>` marks the supply, `IL_<pin>` marks the rail and carries the
#: typical load.  `parameters VSET=` is present because the contract says a testbench has one,
#: even though these fixtures have no output-level code to rewrite.
TB_TEMPLATE = """\
// pmukit regression testbench -- GENERATED by pmukit/verify/regression.py, do not edit.
// One synthetic ground-truth LDO, wired to the netlist convention of docs/CONTRACTS.md 0a:
//   VS_<pin> on the supply pin, IL_<pin> on the rail pin carrying the typical load.
// pmukit strips the analyses below and writes its own.
simulator lang=spectre
global 0

parameters VSET=0

include "nmos_lv.scs"
include "pmos_lv.scs"
include "{lib}"

PMU_TOP (vin vout) {cell}

VS_vin  (vin 0)  vsource dc={vin:g}
IL_vout (vout 0) isource dc={iload:g}

// ---- analyses and saves: pmukit strips all of this and writes its own -------
simOpts options temp=27 tnom=27
dcOp dc write="op.dc"
"""


# --------------------------------------------------------------------------- the fixtures
def variants(gt_dir=None) -> list[tuple[str, str]]:
    """`[(cell, library file)]` for every synthetic LDO, in a stable order.

    Discovered by scanning, not listed: a fixture added to the tree joins the suite, and a
    fixture removed from it stops being silently compared against a stale baseline row.
    """
    d = pathlib.Path(gt_dir or GT_DIR)
    if not d.is_dir():
        raise PmuError(
            what=f"the ground-truth fixture directory {d} does not exist.",
            why="the regression suite runs the synthetic LDOs that live there; without them "
                "there is nothing to compare against the baseline.",
            do=["run this from a source checkout (the fixtures are not shipped in the wheel)",
                "or pass gt_dir=<path to tests/fixtures/ldo_gt>"],
            where=str(d))
    out: list[tuple[str, str]] = []
    for lib in sorted(d.glob("ldo_*.scs")):
        for cell in _SUBCKT.findall(lib.read_text(encoding="utf-8")):
            out.append((cell, lib.name))
    return out


def testbench(cell: str, lib: str) -> str:
    return TB_TEMPLATE.format(lib=lib, cell=cell, vin=VIN_V, iload=ILOAD_A)


# --------------------------------------------------------------------------- one variant
_BAND_ORDER = {"green": 0, "yellow": 1, "not_run": 2, "red": 3}


def _scores(result) -> dict:
    """`{"<port>/<block>": {...}}` -- the WORST cell of each block, which is what a roll-up is.

    The band comes from `grades.grade_block`, not from a second copy of the rule here: the
    suite and the report must never disagree about what colour a score earns, and an emitter
    constant (no observable, no residual) is green in both.
    """
    out: dict[str, dict] = {}
    ports = dict(getattr(result, "ports", None) or {})
    for bf in result:
        key = f"{bf.port}/{bf.block}"
        port_type = ports.get(bf.port, "rail")
        row = out.setdefault(key, {"metric": bf.metric, "worst": None, "cells": 0,
                                   "missing_cells": 0, "flagged": [], "band": "green",
                                   "port_type": port_type})
        row["cells"] += 1
        band, _detail = _grades.grade_block(bf, port_type=port_type)
        if _BAND_ORDER[band] > _BAND_ORDER[row["band"]]:
            row["band"] = band
        if bf.missing:
            row["missing_cells"] += 1
            continue
        score = float(bf.score)
        if score == score and (row["worst"] is None or score > row["worst"]):
            row["worst"] = score
            row["metric"] = bf.metric
        for name in _grades.flagged_parameters(bf):
            if name not in row["flagged"]:
                row["flagged"].append(name)
    for key, row in out.items():
        try:
            row["tier"] = spec.tier_of(key.split("/", 1)[1], row["port_type"])
        except Exception:                              # noqa: BLE001 -- tier is decoration
            row["tier"] = ""
    return out


def run_variant(cell: str, lib: str, *, engine: str = "fake", root=None, jobs: int = 4,
                gt_dir=None, site=None) -> dict:
    """Take one synthetic LDO all the way to fitted scores.  No baseline is read or written."""
    from .. import fit as fitmod
    from ..config import ProjectConfig, derive
    from ..dataset import Dataset
    from ..ledger import Ledger
    from ..netlist import Netlist
    from ..plan import compile_plan
    from ..runner import Runner
    from ..site import SiteConfig

    gt = pathlib.Path(gt_dir or GT_DIR)
    base = pathlib.Path(root or tempfile.mkdtemp(prefix="pmukit_reg_"))
    work = base / cell
    work.mkdir(parents=True, exist_ok=True)
    deck = work / "input.scs"
    deck.write_text(testbench(cell, lib), encoding="utf-8", newline="\n")

    site = site or SiteConfig(engine=engine, ssh_host="ewave-vm")
    site.engine = engine
    nl = Netlist.from_file(deck)
    cfg = ProjectConfig.from_dict({
        "project": cell, "netlist": str(deck), "pmu_inst": "PMU_TOP",
        "corners": ["tt"], "temps_c": list(TEMPS_C), "vset_codes": [0],
        "state_note": f"pmukit regression bench: vin {VIN_V} V, load {ILOAD_A} A",
        "ports": {"vin": "model", "vout": "model"},
        "my_load": {"vout": {"on_a": ILOAD_A, "off_a": ILOAD_OFF_A, "switches": True}},
        "care_up_to_hz": CARE_UP_TO_HZ})
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, site)
    plan = compile_plan(cfg, der, nl, pins, site=site)

    led = Ledger(work / "runs.sqlite")
    plan.commit(led)
    dims = {"process": der.process["corners"], "temp_c": der.temps_c["points"],
            "vset": der.vset["codes"],
            "load_a": {r: der.loads[r]["points_a"] for r in der.rails}}
    ds = Dataset.create(work / "dataset", project=cfg.project, config_sha=cfg.sha(), dims=dims)
    runner = Runner(cfg, plan, led, site, dataset=ds, root=work / "runs",
                    aux=sorted(gt.glob("*.scs")), jobs=jobs)
    summary = runner.run_all()
    result = fitmod.fit_project(ds, der)
    row = {
        "cell": cell, "library": lib,
        "runs": {"planned": summary.get("planned", 0), "done": summary.get("done", 0),
                 "failed": summary.get("failed", 0), "cpu_seconds": summary.get("cpu_seconds")},
        "engine": summary.get("engine", engine),
        "engine_note": summary.get("engine_note", ""),
        "blocks": _scores(result),
        "notes": list(getattr(result, "notes", None) or []),
        "known_hard": KNOWN_HARD.get(cell),
        "workdir": str(work),
    }
    row["status"] = "ok" if not summary.get("failed") else "runs_failed"
    if summary.get("failed"):
        row["failed_runs"] = [r for r in summary.get("runs", []) if r.get("status") != "done"]
    ds.close()
    led.close()
    return row


# --------------------------------------------------------------------------- the whole suite
def run_all(*, engine: str = "", root=None, jobs: int = 4, gt_dir=None, only=None,
            site=None, on_event=None) -> dict:
    """Run every variant and build the baseline payload.

    `engine=""` picks the best available: real Spectre when the host answers, `fake` otherwise.
    Whichever it lands on is recorded IN THE PAYLOAD -- a baseline that does not say how it was
    made cannot be compared against anything.
    """
    from ..backends import make_backend
    from ..site import SiteConfig

    site = site or SiteConfig.load()
    degraded = ""
    if not engine:
        engine = "spectre_ssh"
        probe_site = SiteConfig(**{**site.to_dict(), "engine": "spectre_ssh"})
        ok, why = make_backend("spectre_ssh", probe_site).available()
        if not ok:
            engine, degraded = "fake", why
    site = SiteConfig(**{**site.to_dict(), "engine": engine})
    temp_root = None
    if root is None:
        temp_root = tempfile.mkdtemp(prefix="pmukit_reg_")
        root = temp_root

    rows: dict[str, dict] = {}
    wanted = set(only or [])
    for cell, lib in variants(gt_dir):
        if wanted and cell not in wanted:
            continue
        if on_event:
            on_event(cell)
        rows[cell] = run_variant(cell, lib, engine=engine, root=root, jobs=jobs, gt_dir=gt_dir,
                                 site=site)

    payload = {
        "kind": KIND,
        "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine,
        "engine_note": next((r.get("engine_note") for r in rows.values()
                             if r.get("engine_note")), ""),
        "pmukit_version": __version__,
        "spec_sha": spec.SPEC_SHA,
        "bench": {"vin_v": VIN_V, "iload_a": ILOAD_A, "iload_off_a": ILOAD_OFF_A,
                  "temps_c": list(TEMPS_C), "care_up_to_hz": CARE_UP_TO_HZ,
                  "corners": ["tt"], "vset_codes": [0],
                  "testbench": "pmukit.verify.regression.testbench(cell, lib)"},
        "tolerance": {"rtol": RTOL, "atol_fraction_of_green_limit": ATOL_FRACTION,
                      "rule": "a block regresses when its GRADE BAND gets worse, when it stops "
                              "being fitted at all, or when its score exceeds "
                              "max(base * (1 + rtol), base + atol)."},
        "limits": {m: {"green": lim.green, "yellow": lim.yellow, "unit": lim.unit}
                   for m, lim in _grades.LIMITS.items()},
        "variants": rows,
    }
    if temp_root:
        payload["_temp_root"] = temp_root     # the caller owns it; `_main` removes it
    if degraded:
        payload["engine_degraded_from"] = "spectre_ssh"
        payload["engine_degraded_why"] = why
        payload["notes"] = [
            "THIS BASELINE WAS NOT MEASURED. The `fake` backend synthesizes analytic results "
            "whose DUT is inside the model's own form, so every score here is a pipeline "
            "round-trip, not a fit to a transistor-level circuit. It is still a useful "
            "regression baseline -- it catches a sign, a unit, an axis or a cell lookup moving "
            "-- but it must never be compared against a Spectre run, and it must be "
            "regenerated on a machine with a simulator before it means anything about "
            "modeling quality. Reason the simulator was unavailable: " + why]
    return payload


#: Paths that would make a COMMITTED file machine-specific (and carry the operator's home
#: directory and user name into a public repo).  They are useful while a run is live and have no
#: business in the stored baseline.
def _committable(payload: dict) -> dict:
    """The payload with the local paths taken out.

    A baseline is checked in, so it must read the same on every machine: a run directory under
    someone's home, or an engine note carrying the absolute path of their Spectre install, is
    both noise and a small privacy leak.  The engine VERSION is provenance and stays.
    """
    def version_only(note: str) -> str:
        m = re.search(r"(spectre\s+[0-9][0-9.]*)", str(note), re.I)
        return (m.group(1) if m else str(note).split(" at ", 1)[0]).strip()

    out = {k: v for k, v in payload.items() if k != "_temp_root"}
    if out.get("engine_note"):
        out["engine_note"] = version_only(out["engine_note"])
    out["variants"] = {}
    for cell, row in (payload.get("variants") or {}).items():
        row = {k: v for k, v in row.items() if k != "workdir"}
        if row.get("engine_note"):
            row["engine_note"] = version_only(row["engine_note"])
        for bad in row.get("failed_runs") or []:
            bad.pop("psf_path", None)
            bad.pop("netlist_path", None)
        out["variants"][cell] = row
    return out


def write_baseline(payload: dict, path=None) -> pathlib.Path:
    return jsonio.write(pathlib.Path(path or BASELINE_PATH), _committable(payload))


def load_baseline(path=None) -> dict:
    p = pathlib.Path(path or BASELINE_PATH)
    if not p.is_file():
        raise PmuError(
            what=f"there is no regression baseline at {p}.",
            why="the suite compares today's scores against a stored baseline; without one there "
                "is nothing to compare to.",
            do=["generate one: python -m pmukit.verify.regression --write",
                "on a machine with no simulator it records itself as a `fake` baseline"],
            where=str(p))
    payload = jsonio.read(p)
    if payload.get("kind") != KIND:
        raise PmuError(
            what=f"{p} is not a pmukit regression baseline.",
            why=f"its `kind` is {payload.get('kind')!r}, not {KIND!r}.",
            do=["regenerate it: python -m pmukit.verify.regression --write"],
            where=str(p))
    return payload


# --------------------------------------------------------------------------- comparison
def tolerance_for(metric: str, base: float) -> float:
    """The largest score this block may show before it counts as a regression."""
    lim, _exact = _grades.limit_for(metric)
    floor = ATOL_FRACTION * (lim.green if lim is not None else 1.0)
    return max(float(base) * (1.0 + RTOL), float(base) + floor)


_BAND_RANK = {"green": 0, "yellow": 1, "not_run": 2, "red": 3}


def compare(current: dict, baseline: dict) -> dict:
    """`{"engine_match", "regressions", "improvements", "new", "gone", "summary"}`.

    A cross-engine comparison is REFUSED, not fudged: `fake` results and Spectre results are
    different measurements of different things.
    """
    out = {"engine": current.get("engine"), "baseline_engine": baseline.get("engine"),
           "engine_match": current.get("engine") == baseline.get("engine"),
           "regressions": [], "improvements": [], "new": [], "gone": [], "summary": ""}
    if not out["engine_match"]:
        out["summary"] = (
            f"refusing to compare: this run used {current.get('engine')!r} and the baseline was "
            f"made with {baseline.get('engine')!r}. The `fake` backend's DUT is inside the "
            f"model's own form, so its scores are a pipeline round-trip and are not comparable "
            f"with a fit to a transistor-level circuit.")
        return out

    base_v = baseline.get("variants") or {}
    cur_v = current.get("variants") or {}
    for cell in sorted(set(base_v) | set(cur_v)):
        if cell not in cur_v:
            out["gone"].append(cell)
            continue
        if cell not in base_v:
            out["new"].append(cell)
            continue
        b_blocks = (base_v[cell].get("blocks") or {})
        c_blocks = (cur_v[cell].get("blocks") or {})
        for key in sorted(set(b_blocks) | set(c_blocks)):
            b, c = b_blocks.get(key), c_blocks.get(key)
            if b is None:
                out["new"].append(f"{cell} {key}")
                continue
            if c is None:
                out["regressions"].append({
                    "cell": cell, "block": key, "what": "the block disappeared from the fit",
                    "was": b.get("worst"), "now": None, "band_was": b.get("band"),
                    "band_now": None})
                continue
            was, now = b.get("worst"), c.get("worst")
            band_was, band_now = b.get("band", "not_run"), c.get("band", "not_run")
            row = {"cell": cell, "block": key, "metric": c.get("metric") or b.get("metric"),
                   "was": was, "now": now, "band_was": band_was, "band_now": band_now,
                   "known_hard": bool(cur_v[cell].get("known_hard"))}
            if was is not None and now is None:
                row["what"] = ("the block stopped producing a score (its measurement is now "
                               "missing)")
                out["regressions"].append(row)
                continue
            if _BAND_RANK.get(band_now, 3) > _BAND_RANK.get(band_was, 0):
                row["what"] = f"the grade band fell from {band_was} to {band_now}"
                out["regressions"].append(row)
                continue
            if was is not None and now is not None:
                cap = tolerance_for(row["metric"], was)
                if now > cap:
                    row["what"] = "the score grew past the tolerance"
                    row["limit"] = cap
                    out["regressions"].append(row)
                elif now < was * 0.75:
                    row["what"] = "the score improved"
                    out["improvements"].append(row)
    n = len(out["regressions"])
    out["summary"] = (f"{len(cur_v)} variant(s), {n} regression(s), "
                      f"{len(out['improvements'])} improvement(s) against the "
                      f"{baseline.get('engine')} baseline of {baseline.get('created')}.")
    return out


# --------------------------------------------------------------------------- rendering
def render(payload: dict) -> str:
    """The baseline as a table -- what the CLI prints after a run."""
    out = [f"regression suite -- engine {payload.get('engine')} "
           f"({payload.get('engine_note') or 'no engine note'})",
           f"pmukit {payload.get('pmukit_version')}, spec {payload.get('spec_sha')}, "
           f"created {payload.get('created')}", ""]
    for note in payload.get("notes") or []:
        out += [f"  !! {note}", ""]
    blocks: list[str] = []
    for row in (payload.get("variants") or {}).values():
        for key in row.get("blocks") or {}:
            if key not in blocks:
                blocks.append(key)
    blocks.sort()
    out.append(f"{'variant':<16}" + "".join(f"{b.split('/')[-1]:>11}" for b in blocks)
               + "   built to break")
    out.append("-" * (16 + 11 * len(blocks) + 18))
    for cell, row in sorted((payload.get("variants") or {}).items()):
        line = f"{cell:<16}"
        for b in blocks:
            blk = (row.get("blocks") or {}).get(b)
            if blk is None or blk.get("worst") is None:
                line += f"{'--':>11}"
            else:
                line += f"{blk['worst']:>11.3g}"
        line += "   " + ("yes" if row.get("known_hard") else "")
        out.append(line)
    out.append("")
    for cell, row in sorted((payload.get("variants") or {}).items()):
        if row.get("known_hard"):
            out.append(f"  {cell}: {row['known_hard']}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- cli
def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m pmukit.verify.regression",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="store the result as the baseline")
    ap.add_argument("--engine", default="", help="spectre_ssh | fake (default: best available)")
    ap.add_argument("--only", action="append", help="one variant (repeatable)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--root", default="", help="where run directories go (default: a temp dir)")
    ap.add_argument("--out", default="", help="write the payload here instead of the baseline")
    a = ap.parse_args(argv)

    root = pathlib.Path(a.root) if a.root else None
    payload = run_all(engine=a.engine, root=root, jobs=a.jobs, only=a.only,
                      on_event=lambda cell: print(f"  [{cell}]", flush=True))
    temp_root = payload.pop("_temp_root", "")
    print(render(payload))
    if a.out:
        print("written:", write_baseline(payload, a.out))
    elif a.write:
        print("written:", write_baseline(payload))
    else:
        try:
            print(render_compare(compare(payload, load_baseline())))
        except PmuError as exc:
            print(f"(no comparison: {exc.what})")
    if temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)
    return 0


def render_compare(result: dict) -> str:
    out = [result.get("summary", "")]
    for row in result.get("regressions") or []:
        out.append(f"  REGRESSION {row['cell']} {row['block']}: {row.get('what')} "
                   f"({row.get('was')} -> {row.get('now')})"
                   + ("  [this variant is a KNOWN-HARD one]" if row.get("known_hard") else ""))
    for row in result.get("improvements") or []:
        out.append(f"  improved   {row['cell']} {row['block']}: "
                   f"{row.get('was')} -> {row.get('now')}")
    for cell in result.get("new") or []:
        out.append(f"  new        {cell}")
    for cell in result.get("gone") or []:
        out.append(f"  GONE       {cell} -- the baseline has it and this run does not")
    return "\n".join(out) + "\n"


if __name__ == "__main__":                              # pragma: no cover
    raise SystemExit(_main())
