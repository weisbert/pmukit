#!/usr/bin/env python3
"""Prove the pmu_demo fixture on a real Spectre.

This is the fixture's own acceptance harness, not a pmukit module and not a
pytest test: the box that runs `pytest tests -q` has no simulator, so this lives
here and is run by hand (or by M5, which will grow tools/vmrun.sh into the real
backend).

    python tests/fixtures/pmu_demo/acceptance.py            # everything
    python tests/fixtures/pmu_demo/acceptance.py dc ac      # only those cases

What it does, for each case: take `input.scs`, rewrite it the way pmukit will
(the PDK `section=`, `parameters VSET=`, `options temp=`, and the `mag=` on one
convention source), strip the shipped analyses and append its own, then shell
out to tools/vmrun.sh and read the psfascii back.

Cases
    dc      DC operating point at tt / ss / ff and at the composite corner
            MOSff_RCss -- rail voltages, bias currents, per-ground return current
    temp    DC operating point at -40 / 27 / 125 C -- the PTAT slope check
    ac      Zout (inject on IL_VDD0P8_A, then IL_VDD0P8_B), PSRR (inject on
            VS_VDDA_1V0) and output noise on VDD0P8_A
"""
from __future__ import annotations

import cmath
import math
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
VMRUN = REPO / "tools" / "vmrun.sh"
WORK = pathlib.Path(__file__).resolve().parent / "work"    # git-ignored (.gitignore: work*/)

ANALYSIS_MARKER = "// ---- analyses"


# --------------------------------------------------------------------------- #
# netlist rewriting -- exactly the four edits the contract says pmukit makes
# --------------------------------------------------------------------------- #
def load_netlist() -> str:
    """The fixture, with its shipped analyses/saves stripped off."""
    return (HERE / "input.scs").read_text(encoding="utf-8").split(ANALYSIS_MARKER)[0]


def set_section(text: str, pdk_file: str, section: str) -> str:
    pat = re.compile(rf'(include\s+"[^"]*{re.escape(pdk_file)}"\s+section=)(\w+)')
    out, n = pat.subn(rf"\g<1>{section}", text)
    assert n == 1, f"expected exactly one section= include of {pdk_file}, found {n}"
    return out


def set_vset(text: str, code: int) -> str:
    out, n = re.subn(r"(^parameters\s+VSET=)\S+", rf"\g<1>{code}", text, flags=re.M)
    assert n == 1, f"expected exactly one 'parameters VSET=' line, found {n}"
    return out


def set_mag(text: str, inst: str, mag: float) -> str:
    """Add / replace `mag=` on one convention source instance line."""
    pat = re.compile(rf"^({re.escape(inst)}\s+\(.*?\)\s+\w+source\b.*?)(\s+mag=\S+)?$", re.M)
    out, n = pat.subn(rf"\g<1> mag={mag}", text)
    assert n == 1, f"expected exactly one instance line for {inst}, found {n}"
    return out


# --------------------------------------------------------------------------- #
# psfascii readers (ascii on purpose -- no PSF library, no extra dependency)
# --------------------------------------------------------------------------- #
def _values(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").split("\nVALUE\n", 1)[1]


def read_dcop(path: pathlib.Path) -> dict[str, float]:
    """`"name" "V" 1.23e+00` -> {name: value}."""
    out = {}
    for line in _values(path).splitlines():
        m = re.match(r'"([^"]+)"\s+"(\w+)"\s+(\S+)\s*$', line.strip())
        if m:
            out[m.group(1)] = float(m.group(3))
    return out


def read_ac(path: pathlib.Path) -> dict[str, list[tuple[float, complex]]]:
    """`"freq" f` then `"node" (re im)` -> {node: [(f, value)]}."""
    out: dict[str, list[tuple[float, complex]]] = {}
    freq = None
    for line in _values(path).splitlines():
        line = line.strip()
        m = re.match(r'"freq"\s+(\S+)$', line)
        if m:
            freq = float(m.group(1))
            continue
        m = re.match(r'"([^"]+)"\s+\((\S+)\s+(\S+)\)$', line)
        if m and freq is not None:
            out.setdefault(m.group(1), []).append(
                (freq, complex(float(m.group(2)), float(m.group(3)))))
    return out


def read_noise(path: pathlib.Path) -> list[tuple[float, float]]:
    """The total output-noise trace `out`, in V/sqrt(Hz)."""
    out = []
    freq = None
    for line in _values(path).splitlines():
        line = line.strip()
        m = re.match(r'"freq"\s+(\S+)$', line)
        if m:
            freq = float(m.group(1))
            continue
        m = re.match(r'"out"\s+(\S+)$', line)
        if m and freq is not None:
            out.append((freq, float(m.group(1))))
    return out


def at(curve, f_target):
    """Nearest sampled point of a [(f, y)] curve."""
    return min(curve, key=lambda fy: abs(math.log10(fy[0]) - math.log10(f_target)))


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def run(tag: str, deck: str) -> pathlib.Path:
    d = WORK / tag
    (d / "pdk").mkdir(parents=True, exist_ok=True)
    for f in (HERE / "pdk").glob("*.scs"):
        (d / "pdk" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8",
                                        newline="\n")
    (d / "input.scs").write_text(deck, encoding="utf-8", newline="\n")
    # bash (Git Bash on Windows) wants forward slashes; a backslash path is not a directory to it
    cmd = ["bash", VMRUN.as_posix(), d.as_posix(), f"pmu_demo_{tag}"]
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"acceptance: spectre failed for '{tag}'")
    return d / "raw"


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #
RAILS = ["VDD0P8_A", "VDD0P8_B", "VDD0P8_C"]
GROUNDS = ["VGND_VSS_A:p", "VGND_VSS_B:p", "VGND_AGND:p"]
VSET_TARGET = {  # the divider maths in input.scs, for VSET=3
    "VDD0P8_A": 0.800, "VDD0P8_B": 0.800, "VDD0P8_C": 0.800,
}


def case_dc():
    print("\n=== DC operating point vs corner (VSET=3, 27 C) ===")
    base = load_netlist()
    corners = {
        "tt": ("tt", "typ"), "ss": ("ss", "typ"), "ff": ("ff", "typ"),
        "MOSff_RCss": ("ff", "ss"),
    }
    rows = {}
    for name, (mos, rc) in corners.items():
        deck = set_section(set_section(base, "toplevel.scs", mos), "rc.scs", rc)
        deck += ('\nsaveOpts options save=allpub currents=all temp=27 tnom=27\n'
                 'dcOp dc write="op.dc"\n')
        raw = run(f"dc_{name}", deck)
        rows[name] = read_dcop(raw / "dcOp.dc")

    hdr = f"{'corner':<12}" + "".join(f"{r:>12}" for r in RAILS) + \
          f"{'IB_PTAT':>11}{'IB_POLY':>11}" + "".join(f"{g:>16}" for g in GROUNDS)
    print(hdr)
    for name, op in rows.items():
        line = f"{name:<12}" + "".join(f"{op[r]:>12.4f}" for r in RAILS)
        line += f"{op['VB_IB_PTAT:p']*1e6:>10.3f}u{op['VB_IB_POLY:p']*1e6:>10.3f}u"
        line += "".join(f"{op[g]*1e6:>15.3f}u" for g in GROUNDS)
        print(line)
    for r in RAILS:
        err = [(op[r] - VSET_TARGET[r]) / VSET_TARGET[r] * 100 for op in rows.values()]
        print(f"  {r}: worst deviation from the VSET=3 target "
              f"{VSET_TARGET[r]:.3f} V = {max(err, key=abs):+.2f} %")
    spread = {r: max(op[r] for op in rows.values()) - min(op[r] for op in rows.values())
              for r in RAILS}
    print(f"  corner spread (max-min): " +
          ", ".join(f"{r} {spread[r]*1e3:.1f} mV" for r in RAILS))
    return rows


def case_vset():
    print("\n=== VSET codes 0..3 (tt/typ, 27 C) ===")
    base = load_netlist()
    print(f"{'VSET':<6}" + "".join(f"{r:>12}" for r in RAILS))
    for code in (0, 1, 2, 3):
        deck = set_vset(base, code)
        deck += ('\nsaveOpts options temp=27 tnom=27\n'
                 'dcOp dc write="op.dc"\n'
                 'save ' + " ".join(RAILS) + "\n")
        op = read_dcop(run(f"vset{code}", deck) / "dcOp.dc")
        print(f"{code:<6}" + "".join(f"{op[r]:>12.4f}" for r in RAILS))


def case_temp():
    print("\n=== bias currents vs temperature (tt/typ, VSET=3) ===")
    deck = load_netlist()
    deck += """
saveOpts options temp=27 tnom=27
save VB_IB_PTAT:p VB_IB_POLY:p
op27 dc write="op27.dc"
tm40 alter param=temp value=-40
opm40 dc write="opm40.dc"
tp125 alter param=temp value=125
op125 dc write="op125.dc"
"""
    raw = run("temp", deck)
    out = {}
    for t, f in ((-40, "opm40.dc"), (27, "op27.dc"), (125, "op125.dc")):
        out[t] = read_dcop(raw / f)
    print(f"{'T [C]':>8}{'IB_PTAT':>14}{'IB_POLY':>14}")
    for t in (-40, 27, 125):
        print(f"{t:>8}{out[t]['VB_IB_PTAT:p']*1e6:>12.3f}u"
              f"{out[t]['VB_IB_POLY:p']*1e6:>12.3f}u")
    p40, p125 = out[-40]["VB_IB_PTAT:p"], out[125]["VB_IB_PTAT:p"]
    q40, q125 = out[-40]["VB_IB_POLY:p"], out[125]["VB_IB_POLY:p"]
    print(f"  IB_PTAT  -40 -> 125 C : x{p125/p40:.3f}   "
          f"(ideal PTAT on absolute T = x{(125+273.15)/(-40+273.15):.3f})   "
          f"{'RISES  OK' if p125 > p40 * 1.3 else 'NOT PTAT -- FAIL'}")
    print(f"  IB_POLY  -40 -> 125 C : x{q125/q40:.3f}   "
          f"spread {(max(q40, q125)/min(q40, q125)-1)*100:.1f} %  "
          f"{'FLAT-ish OK' if max(q40, q125)/min(q40, q125) < 1.25 else 'NOT FLAT'}")


def case_en():
    print("\n=== EN gating (tt/typ, 27 C): everything must collapse with EN low ===")
    base = load_netlist()

    def deck_for(en, loads):
        t, n = re.subn(r"^(VEN_EN\s+\(EN 0\)\s+vsource dc=)\S+", rf"\g<1>{en}", base, flags=re.M)
        assert n == 1
        if not loads:
            # A module whose supply is down draws nothing.  The fixture's IL_ sources are
            # IDEAL current sinks, so if they are left on at EN=0 they drag the dead rails
            # negative -- a testbench artefact, not PMU behaviour.  Zero them.
            t, n = re.subn(r"^(IL_\w+\s+\([^)]*\)\s+isource dc=)\S+", r"\g<1>0", t, flags=re.M)
            assert n == 3
        return t + ('\nsaveOpts options temp=27 tnom=27\n'
                    'dcOp dc write="op.dc"\n'
                    'save ' + " ".join(RAILS) + ' VB_IB_PTAT:p VB_IB_POLY:p VS_VDDA_1V0:p\n')

    rows = {
        "EN=1": read_dcop(run("en1", deck_for(1.0, True)) / "dcOp.dc"),
        "EN=0": read_dcop(run("en0", deck_for(0.0, True)) / "dcOp.dc"),
        "EN=0*": read_dcop(run("en0nl", deck_for(0.0, False)) / "dcOp.dc"),
    }
    print(f"{'':<7}" + "".join(f"{r:>12}" for r in RAILS) +
          f"{'IB_PTAT':>11}{'IB_POLY':>11}{'I(VDDA)':>12}")
    for label, op in rows.items():
        print(f"{label:<7}" + "".join(f"{op[r]:>12.5f}" for r in RAILS) +
              f"{op['VB_IB_PTAT:p']*1e6:>10.3f}u{op['VB_IB_POLY:p']*1e6:>10.3f}u"
              f"{-op['VS_VDDA_1V0:p']*1e6:>11.2f}u")
    print("  EN=0* = EN low with the module loads also removed (see note in the source).")
    off, offnl, on = rows["EN=0"], rows["EN=0*"], rows["EN=1"]
    ok = (all(abs(offnl[r]) < 0.05 for r in RAILS)
          and abs(off["VB_IB_PTAT:p"]) < 1e-7 and abs(off["VB_IB_POLY:p"]) < 1e-7
          and abs(off["VS_VDDA_1V0:p"]) < 0.02 * abs(on["VS_VDDA_1V0:p"]))
    print("  " + ("rails collapse, biases go to zero, supply current drops >50x  OK"
                  if ok else "EN DOES NOT GATE -- FAIL"))


def case_ac():
    print("\n=== AC (Zout / PSRR) and noise (tt/typ, VSET=3, 27 C) ===")
    deck = load_netlist()
    deck = set_mag(deck, "IL_VDD0P8_A", 1)
    deck += """
saveOpts options temp=27 tnom=27
save VDD0P8_A VDD0P8_B VDD0P8_C
zoutA ac start=10 stop=1G dec=20
a1 alter dev=IL_VDD0P8_A param=mag value=0
a2 alter dev=IL_VDD0P8_B param=mag value=1
zoutB ac start=10 stop=1G dec=20
a3 alter dev=IL_VDD0P8_B param=mag value=0
a4 alter dev=VS_VDDA_1V0 param=mag value=1
psrr ac start=10 stop=1G dec=20
a5 alter dev=VS_VDDA_1V0 param=mag value=0
nz (VDD0P8_A 0) noise start=10 stop=100M dec=10
"""
    raw = run("ac", deck)
    za = read_ac(raw / "zoutA.ac")["VDD0P8_A"]
    zb = read_ac(raw / "zoutB.ac")["VDD0P8_B"]
    ps = read_ac(raw / "psrr.ac")
    nz = read_noise(raw / "nz.noise")

    # the load isource sinks out of the rail, so V/I carries a minus sign; |Z| is unaffected
    print(f"{'freq':>10}{'|Zout| A':>12}{'|Zout| B':>12}"
          f"{'PSRR A':>10}{'PSRR B':>10}{'PSRR C':>10}")
    for f in (10, 1e3, 1e5, 1e6, 3e6, 1e7, 1e8, 1e9):
        fa, va = at(za, f)
        _, vb = at(zb, f)
        row = f"{fa:>10.3g}{abs(va):>12.4g}{abs(vb):>12.4g}"
        for r in RAILS:
            _, v = at(ps[r], f)
            row += f"{20*math.log10(abs(v)):>10.2f}"
        print(row)
    for name, z in (("VDD0P8_A", za), ("VDD0P8_B", zb)):
        fpk, vpk = max(z, key=lambda fy: abs(fy[1]))
        lf = abs(z[0][1])
        print(f"  Zout {name}: LF {lf:.3f} ohm, peak {abs(vpk):.3f} ohm at "
              f"{fpk/1e6:.3f} MHz  (peak/LF = {abs(vpk)/lf:.1f})")
    for name in RAILS:
        worst = max(ps[name], key=lambda fy: abs(fy[1]))
        print(f"  PSRR {name}: DC {20*math.log10(abs(ps[name][0][1])):.2f} dB, "
              f"worst {20*math.log10(abs(worst[1])):.2f} dB at {worst[0]/1e6:.3f} MHz")
    print("  output noise on VDD0P8_A [V/sqrt(Hz)]:")
    for f in (10, 100, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8):
        ff, v = at(nz, f)
        print(f"    {ff:>10.3g} Hz  {v:.4g}")


CASES = {"dc": case_dc, "vset": case_vset, "temp": case_temp, "en": case_en, "ac": case_ac}


def main(argv):
    wanted = argv[1:] or list(CASES)
    bad = [w for w in wanted if w not in CASES]
    if bad:
        raise SystemExit(f"unknown case(s) {bad}; known: {list(CASES)}")
    for w in wanted:
        CASES[w]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
