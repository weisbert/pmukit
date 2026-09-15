"""The self-oscillating bench: does the model survive being INSIDE a limit cycle?

METHODOLOGY is blunt about why this file exists:

    *"Standalone driven-HB is too easy to expose this (a near-LTI PMU converges without the
    oscillator limit cycle) -- reproduce/verify only in the real `oschb`."*

Every model-side harmonic-balance pathology the private repo ever found -- the synthesized
8890 H PSRR inductor, its 8.89 uF dual, the +-17.2 S near-cancelling first-order doublet, the
flat-to-infinity supply injection -- was invisible to AC, invisible to a driven HB, and
surfaced only in a coupled autonomous run.  Worse, it surfaced *at the package model and at the
neighbouring transistors*, never at the model, which is how "the behavioral block converges
standalone but not in the system" became the signature failure.

So this bench asks one question and reports the two answers side by side:

    does an oscillator converge when it is powered THROUGH the emitted model,
    and does it still converge when the same oscillator is powered by an IDEAL supply?

**Why an LC negative-resistance tank and not a ring.**  Three reasons, in order:

1. The historical failure was pinned at *harmonic 16 of a 4.84 GHz VCO* -- about 77 GHz -- and
   the mechanism was the dynamic range of a branch admittance across that harmonic ladder.  An
   LC tank produces exactly that ladder.  A ring oscillator's spectrum is set by its stage
   delay and its waveform is dominated by switching, which is a different stress.
2. An LC tank's frequency is set by L and C, so the bench can be PLACED -- here at a fixed
   fraction of the model's own declared frequency ceiling, so the run stays inside the envelope
   the model claims while still reaching high harmonics.  A ring's frequency falls out of the
   delay and cannot be aimed.
3. A centre-tapped tank is the natural way to power an oscillator THROUGH the rail, which is
   the whole point: the amplitude-dependent supply pump puts a real 2*f0 ripple through the
   model's Zout, so the model is inside the loop rather than sitting next to it.

The negative resistance is one behavioural source with a cubic limiting term -- the smallest
thing that produces a genuine limit cycle -- and the supply pump is `k*v(op,on)^2`, the 2*f0
current a real switching pair draws.  Both are `bsource`, so the bench needs no PDK and runs
anywhere Spectre does.

**The control run is the same oscillator on an ideal source at the model's OWN regulated DC**,
so the one thing that differs between the two runs is the supply impedance.
"""
from __future__ import annotations

import math
import pathlib
import tempfile

from .. import psf
from ..errors import PmuError
from . import hb as hbmod

__all__ = ["oscillator_check", "render", "F_FRACTION", "HARMONICS", "TANK_Q", "TANK_L_H"]

#: Where the oscillator is placed, as a fraction of the model's declared frequency ceiling.
#: 0.6 is the fraction the private repo's system acceptance test used for its carrier, for the
#: same reason: high enough that the harmonic ladder is a real stress, low enough that the
#: fundamental itself is inside the characterized band and the model is not being extrapolated.
F_FRACTION = 0.6
#: Harmonics of the autonomous run.  The failure that motivated this file was at harmonic 16;
#: 8 keeps the autonomous solve quick and still reaches well past the model's ceiling.
HARMONICS = 8
#: Tank quality factor and inductance.  Q = 10 is an ordinary integrated tank; the inductance
#: is fixed and the capacitance is solved for the target frequency, so the tank impedance stays
#: in a sane few-hundred-ohm range at any placement.
TANK_Q = 10.0
TANK_L_H = 2.0e-9
#: Excess loop gain over the tank loss -- 3x is a normal start-up margin.
GM_EXCESS = 3.0
#: Oscillation amplitude target, as a fraction of the rail voltage.
AMPL_FRACTION = 0.5
#: Fraction of the rail's operating current the tank pumps at 2*f0.  This is what makes the
#: model's Zout part of the loop instead of a bystander.
PUMP_FRACTION = 0.2


def tank(f0_hz: float, vreg_v: float, i_typ_a: float) -> dict:
    """Size the LC tank, the negative resistance and the supply pump for one placement.

    Differential tank: two `TANK_L_H` halves in series across the pair, so L_tank = 2 L.
    `beta` follows from the describing function of a cubic limiter: the loop settles where
    `gm = 3/4 * beta * A^2 + 1/R_tank`, which fixes the amplitude without a hand-tuned number.
    """
    f0 = float(f0_hz)
    l_tank = 2.0 * TANK_L_H
    w0 = 2.0 * math.pi * f0
    c_tank = 1.0 / (w0 * w0 * l_tank)
    r_tank = TANK_Q * w0 * l_tank
    gm = GM_EXCESS / r_tank
    ampl = max(AMPL_FRACTION * float(vreg_v), 0.05)
    beta = 4.0 * (gm - 1.0 / r_tank) / (3.0 * ampl * ampl)
    pump = 2.0 * PUMP_FRACTION * max(float(i_typ_a), 1e-6) / (ampl * ampl)
    return {"f0_hz": f0, "l_half_h": TANK_L_H, "c_tank_f": c_tank, "r_tank_ohm": r_tank,
            "gm_s": gm, "beta_a_v3": beta, "ampl_target_v": ampl, "pump_a_v2": pump,
            "q": TANK_Q, "i_tail_a": float(i_typ_a)}


def _osc_lines(t: dict, supply_net: str) -> list[str]:
    """The oscillator itself -- identical text in both runs, only `supply_net` differs."""
    return [
        f"// LC negative-resistance oscillator, centre-tapped to {supply_net}.",
        f"Lp (op {supply_net}) inductor l={t['l_half_h']:.6g}",
        f"Ln (on {supply_net}) inductor l={t['l_half_h']:.6g}",
        f"Ct (op on) capacitor c={t['c_tank_f']:.6g}",
        f"Rt (op on) resistor r={t['r_tank_ohm']:.6g}",
        "// negative resistance with a cubic limiter = the limit cycle",
        f"Gneg (op on) bsource i=-{t['gm_s']:.6g}*v(op,on)"
        f"+{t['beta_a_v3']:.6g}*v(op,on)*v(op,on)*v(op,on)",
        "// the tail, plus the 2*f0 supply pump that puts the model's Zout inside the loop",
        f"Itail ({supply_net} 0) isource dc={t['i_tail_a']:.6g}",
        f"Ipump ({supply_net} 0) bsource i={t['pump_a_v2']:.6g}*v(op,on)*v(op,on)",
    ]


def _analysis(t: dict, harmonics: int) -> list[str]:
    return ['dcOp dc write="op.dc"',
            f"osc ( op on ) hb fundfreqs=[{t['f0_hz']:.6g}] maxharms=[{int(harmonics)}] "
            f"oscic=lin annotate=detailed_hb"]


def model_deck(built: dict, derived, *, va_name: str, rail: str, t: dict, vset=None,
               harmonics: int = HARMONICS, temp_c: float = 25.0) -> str:
    """The oscillator powered THROUGH the emitted model: its centre tap IS the rail pin."""
    grounds = set(built["grounds"])
    nets = ["0" if p in grounds else p for p in built["ports"]]
    params = [f"vset={int(vset)}"] if vset is not None else []
    params += [f"load_en_{p}=0" for p in built["ls_ports"]]

    lines = ["// pmukit verify: an oscillator powered THROUGH the emitted model.",
             "simulator lang=spectre", "global 0", "", f'ahdl_include "{va_name}"', "",
             f"X1 ({' '.join(nets)}) {built['module']} {' '.join(params)}", ""]
    for s in built["supplies"]:
        v = float(((getattr(derived, "supply", None) or {}).get("pins") or {})
                  .get(s, {}).get("nominal_v", 1.0) or 1.0)
        lines.append(f"VS_{s} ({s} 0) vsource dc={v:g}")
    for r in built["rails"]:
        if r == rail:
            continue          # the oscillator IS this rail's load
        i_typ = float(((getattr(derived, "rails", None) or {}).get(r) or {})
                      .get("i_typ_a", 0.0) or 0.0)
        lines.append(f"IL_{r} ({r} 0) isource dc={i_typ:g}")
    for b in built["biases"]:
        vb = float(((getattr(derived, "biases", None) or {}).get(b) or {})
                   .get("vcomp_v", 0.4) or 0.4)
        lines.append(f"VB_{b} ({b} 0) vsource dc={vb:g}")
    for s in built["stubs"]:
        lines.append(f"RSTUB_{s} ({s} 0) resistor r=1T")
    lines += ["", *_osc_lines(t, rail), "",
              f"simOpts options temp={temp_c:g} tnom={temp_c:g}",
              f"save {rail} op on", *_analysis(t, harmonics), ""]
    return "\n".join(lines)


def ideal_deck(t: dict, *, vreg_v: float, harmonics: int = HARMONICS,
               temp_c: float = 25.0) -> str:
    """The control: the SAME oscillator on an ideal source at the model's own regulated DC."""
    return "\n".join([
        "// pmukit verify: the control -- the same oscillator on an IDEAL supply.",
        "simulator lang=spectre", "global 0", "",
        f"Vosc (vosc 0) vsource dc={float(vreg_v):.9g}", "",
        *_osc_lines(t, "vosc"), "",
        f"simOpts options temp={temp_c:g} tnom={temp_c:g}",
        "save vosc op on", *_analysis(t, harmonics), ""])


# --------------------------------------------------------------------------- reading
def _f_osc(log: str) -> float:
    """`Fundamental frequency is 1.19664 GHz.` -- what Spectre prints when an autonomous HB
    lands.  The per-iteration `Frequency=` lines are the same number converging; this is the
    one the analysis committed to."""
    unit = {"H": 1.0, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    for line in log.splitlines():
        if "Fundamental frequency is" in line:
            parts = line.split("Fundamental frequency is", 1)[1].strip().rstrip(".").split()
            try:
                value = float(parts[0])
            except (ValueError, IndexError):
                continue
            scale = 1.0
            if len(parts) > 1 and parts[1][:1].upper() in unit:
                scale = unit[parts[1][:1].upper()]
            return value * scale
    return float("nan")


def _dc_node(psf_dir, node: str) -> float:
    """One node of the run's DC operating point, read through the project's own PSF reader."""
    try:
        parsed = psf.read_psf(psf.find_psf(psf_dir, "dcOp"))
    except (PmuError, OSError):
        return float("nan")
    col = parsed.get(node)                 # psf.py returns {signal: ndarray} plus `_` metadata
    if col is None:
        return float("nan")
    try:
        return float(col[0] if hasattr(col, "__len__") and len(col) else col)
    except (TypeError, ValueError, IndexError):
        return float("nan")


#: Spectre prints this and then carries on printing a frequency, so a run that hit it looks
#: finished unless you read for it.  It is the difference between "converged" and "gave up".
_GAVE_UP = "Maximum number of iterations reached"


def _row(res: dict) -> dict:
    out = hbmod.parse_log(res["log"])
    out.pop("steps", None)
    gave_up = _GAVE_UP in res["log"]
    out.update({"ok": res["ok"], "command": res["command"], "detail": res["detail"],
                "workdir": res["workdir"], "f_osc_hz": _f_osc(res["log"]),
                "hit_iteration_limit": gave_up})
    # `parse_log` calls a run converged when the analysis finished.  An autonomous HB that ran
    # out of Newton iterations ALSO finishes, prints a frequency, and only says so in one
    # warning line -- so that line overrides the optimistic reading.
    out["converged"] = bool(out["converged"] and not gave_up)
    out["oscillated"] = bool(out["f_osc_hz"] == out["f_osc_hz"] and not gave_up)
    return out


def _vreg(fit, rail: str, corner: str, vset) -> float:
    """The rail's regulated voltage, from the FITTED dc block -- no simulator involved."""
    bf = hbmod._pick(fit, rail, "dc", corner, temp_c=25.0)
    if bf is None:
        return 0.8
    v = bf.params.get("vout")
    if isinstance(v, dict):
        v = v.get(str(vset), v.get(vset)) if vset is not None else next(iter(v.values()), None)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.8
    return v if v > 0.05 else 0.8




# --------------------------------------------------------------------------- the check
def _tag(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(name))


def _bench_rail(fit, derived, *, built, rail, corner, work, va_path, va_name, site, be,
                harmonics, f0_hz, timeout_s, vset, tnom, f_max) -> dict:
    """One rail: the oscillator through the model, then the same one on an ideal source."""
    i_typ = float(((getattr(derived, "rails", None) or {}).get(rail) or {})
                  .get("i_typ_a", 0.0) or 0.0)
    f0 = float(f0_hz or F_FRACTION * f_max)
    # ONE tank, sized ONCE, used by both runs.  The amplitude and the pump scale with the rail
    # voltage, so taking it from the FITTED dc block (pure python, no simulator) rather than
    # from whichever run goes first is what keeps the two decks electrically identical --
    # otherwise the comparison measures two different oscillators.
    vreg = _vreg(fit, rail, corner, vset)
    t = tank(f0, vreg_v=vreg, i_typ_a=i_typ)

    deck = model_deck(built, derived, va_name=va_name, rail=rail, t=t, vset=vset,
                      harmonics=harmonics, temp_c=tnom)
    res = hbmod.run_deck(work / "with_model", deck, f"pmukit_osc_{corner}_{_tag(rail)}_model",
                         site=site, backend=be, timeout_s=timeout_s, aux=[va_path])
    with_model = _row(res)
    with_model["vreg_dc_v"] = _dc_node(pathlib.Path(res["workdir"]) / "raw", rail)

    ideal = _row(hbmod.run_deck(
        work / "ideal_supply",
        ideal_deck(t, vreg_v=vreg, harmonics=harmonics, temp_c=tnom),
        f"pmukit_osc_{corner}_{_tag(rail)}_ideal", site=site, backend=be, timeout_s=timeout_s))

    fm, fi = with_model["f_osc_hz"], ideal["f_osc_hz"]
    both = bool(with_model["converged"] and ideal["converged"])
    row = {
        "rail": rail, "tank": t, "vreg_v": vreg, "f0_target_hz": f0,
        "with_model": with_model, "ideal_supply": ideal,
        "delta": {"f_hz": (fm - fi) if (fm == fm and fi == fi) else float("nan"),
                  "f_ppm": ((fm - fi) / fi * 1e6) if (fm == fm and fi == fi and fi)
                  else float("nan"),
                  "iterations": with_model["iterations"] - ideal["iterations"],
                  "means": "how far the model's finite output impedance moved the limit cycle, "
                           "and how many extra Newton iterations it cost."},
        "status": "pass" if (both and with_model["oscillated"]) else "fail",
        "notes": [],
    }
    if both:
        row["notes"].append(
            "the oscillator converged with the model in the loop AND on an ideal supply, so the "
            "model does not break an autonomous harmonic balance at this placement.")
    elif ideal["converged"]:
        row["notes"].append(
            "the oscillator converges on an IDEAL supply but NOT through the model. That is the "
            "signature failure this bench exists for. The stall usually surfaces at whatever "
            "shares the supply node rather than at the model itself, so read the Newton trace "
            "in " + str(with_model.get("workdir", "")) + " and compare the emitter's STATIC "
            "conditioning report (hb_check.txt) for the same corner -- a static PASS there and "
            "a stall here is exactly the gap this bench was built to find.")
    else:
        row["notes"].append(
            "the CONTROL did not converge either, so this rail's bench says nothing about the "
            "model: the oscillator itself did not start. Re-place it (f0_hz=) before reading "
            "the model column.")
    return row


def oscillator_check(fit, derived, *, corner: str = "", project: str = "pmu", rail: str = "",
                     site=None, backend=None, root=None, harmonics: int = HARMONICS,
                     f0_hz: float | None = None, timeout_s: float = 1800.0) -> dict:
    """Run the oscillator both ways on EVERY modeled rail (or just `rail`, when named).

    Every rail, because the rails do not behave alike: on the synthetic PMU the peaked, low-ESR
    rail stalls the autonomous solve at the Newton iteration limit where its ESR-damped sibling
    converges in seven steps, and benching only the first rail would have reported either one
    as the whole answer.
    """
    from .. import emit

    corners = [str(c) for c in ((getattr(derived, "process", None) or {}).get("corners") or [])]
    corner = str(corner or (corners[0] if corners else "tt"))
    vset = next(iter((getattr(derived, "vset", None) or {}).get("codes") or []), None)
    temps = [float(t) for t in
             ((getattr(derived, "temps_c", None) or {}).get("points") or [25.0])]
    tnom = float(min(temps, key=lambda t: abs(t - 25.0)))
    f_max = float((getattr(derived, "freq", None) or {}).get("stop_hz", 1e9) or 1e9)

    built = emit.build_va(fit, derived, corner, project=project, ls_default_on=())
    rails = [rail] if rail else list(built["rails"])
    report = {"status": "not_run", "corner": corner, "rails": {}, "order": rails,
              "topology": "differential LC tank with a cubic-limited negative resistance, "
                          "centre-tapped to the rail, plus a 2*f0 supply pump",
              "placement": f"{F_FRACTION:g} x the model's declared frequency ceiling",
              "f_max_hz": f_max, "harmonics": int(harmonics), "notes": []}
    if not rails:
        report["notes"].append("this model emits no voltage rail, so there is nothing to power "
                               "an oscillator through.")
        return report

    if site is None:
        from ..site import SiteConfig
        site = SiteConfig.load()
    be = hbmod._backend(site, backend)
    report["engine"] = getattr(be, "name", "?")
    ok, why = hbmod.usable(be)
    if not ok:
        report["notes"].append(
            f"no simulator: {why}. The autonomous bench is the one test that catches what a "
            f"driven HB cannot, so an unrun one is not a pass -- it is a gap.")
        return report

    # see the note in hb.py: never the caller's current directory.
    base = (pathlib.Path(root) if root is not None else
            pathlib.Path(tempfile.mkdtemp(prefix="pmukit_osc_"))) / "verify" / "system"
    for r in rails:
        # one directory per (corner, rail): two rails benched in the same session must not
        # overwrite each other's deck and log, or the evidence for the first one is gone.
        work = base / corner / _tag(r)
        work.mkdir(parents=True, exist_ok=True)
        va_name = f"{built['module']}.va"
        va_path = work / va_name
        va_path.write_text(built["text"], encoding="utf-8", newline="\n")
        report["rails"][r] = _bench_rail(
            fit, derived, built=built, rail=r, corner=corner, work=work, va_path=va_path,
            va_name=va_name, site=site, be=be, harmonics=harmonics, f0_hz=f0_hz,
            timeout_s=timeout_s, vset=vset, tnom=tnom, f_max=f_max)

    rows = list(report["rails"].values())
    report["status"] = "pass" if all(r["status"] == "pass" for r in rows) else "fail"
    report["notes"].append(
        "this is a SYNTHETIC oscillator with behavioural devices and no package. It reproduces "
        "the limit cycle a driven HB cannot, which is the point, but it is not the consumer's "
        "own coupled run: the historical failures surfaced at a package n-port and at real "
        "transistors sharing the supply node. A pass here is necessary, not sufficient.")
    return report


def render(report: dict) -> str:
    """The bench as plain text."""
    g = hbmod._g
    out = [f"Oscillator bench -- corner {report.get('corner', '?')}, engine "
           f"{report.get('engine') or '(none)'}",
           f"status: {report.get('status', '?')}",
           f"topology: {report.get('topology', '?')}",
           f"placement: {report.get('placement', '?')} (ceiling {g(report.get('f_max_hz'))} Hz),"
           f" {report.get('harmonics', '?')} harmonics", ""]
    rails = report.get("rails") or {}
    if rails:
        out += [f"{'rail':<12}{'supply':<15}{'converged':>11}{'f_osc [Hz]':>14}{'iters':>7}"
                f"{'final resid':>13}  note", "-" * 86]
    for name in (report.get("order") or list(rails)):
        row = rails.get(name)
        if not row:
            continue
        t = row.get("tank") or {}
        for key, label in (("with_model", "through model"), ("ideal_supply", "ideal source")):
            r = row.get(key) or {}
            out.append(f"{(name if key == 'with_model' else ''):<12}{label:<15}"
                       f"{('yes' if r.get('converged') else 'NO'):>11}"
                       f"{g(r.get('f_osc_hz')):>14}{r.get('iterations', 0):>7}"
                       f"{g(r.get('final')):>13}  "
                       + ("HIT THE NEWTON ITERATION LIMIT -- the printed frequency is the last "
                          "iterate, not a solution" if r.get("hit_iteration_limit") else ""))
        d = row.get("delta") or {}
        out.append(f"{'':<12}-> the model moved it by {g(d.get('f_hz'))} Hz "
                   f"({g(d.get('f_ppm'))} ppm) and cost {d.get('iterations', 0)} extra "
                   f"iteration(s); tank {g(t.get('c_tank_f'))} F / "
                   f"{g(t.get('r_tank_ohm'))} ohm at {g(row.get('f0_target_hz'))} Hz")
        for n in row.get("notes") or []:
            out.append(f"{'':<12}   note: {n}")
        out.append("")
    for n in report.get("notes") or []:
        out.append(f"  note: {n}")
    return "\n".join(out) + "\n"
