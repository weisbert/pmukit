"""A SYNTHETIC testbench the size of a real ADE export, for the New screen's timing guards.

Everything here is generated and invented: the cell names, nets and values are made up, and none
of them comes from any real design. The shape is what matters -- the size a real top-level PMU
bench reaches (a few MB of text), a PMU subckt with ~100 ports and thousands of devices across
nested subckts, ~30 convention sources, three toplevel `include ... section=` lines, dozens of
`parameters`, other top-level instances next to the PMU and the analyses an export carries.

    path = write_bench(tmp_path / "bench")          # -> bench/input.scs (+ bench/pdk/*.scs)
"""
from __future__ import annotations

import pathlib

N_RAILS = 4
N_BIASES = 20
N_SUPPLIES = 3
N_ENABLES = 2
N_GROUNDS = 2
N_PORTS = 99            # the rest are role-less: control bits, trims, test pins


def pin_names(n_ports: int = N_PORTS) -> dict[str, list[str]]:
    rails = [f"VRAIL{i}" for i in range(N_RAILS)]
    biases = [f"IBIAS{i}" for i in range(N_BIASES)]
    supplies = [f"VSUP{i}" for i in range(N_SUPPLIES)]
    enables = [f"EN{i}" for i in range(N_ENABLES)]
    grounds = [f"GND{i}" for i in range(N_GROUNDS)]
    n_other = n_ports - len(rails) - len(biases) - len(supplies) - len(enables) - len(grounds)
    other = [f"CTRL{i}<{j}>" for i in range(n_other // 4 + 1) for j in range(4)][:n_other]
    return {"rail": rails, "bias": biases, "supply": supplies, "en": enables,
            "ground": grounds, "none": other}


def _leaf(name: str, n_dev: int, seed: int) -> list[str]:
    ports = ["A", "B", "Y", "VDD", "VSS"]
    out = [f"subckt {name} {' '.join(ports)}"]
    for d in range(n_dev):
        k = (seed * 31 + d * 7) % 5
        n1, n2 = f"n{(d + seed) % 23}", f"n{(d * 3 + seed) % 29}"
        if k == 0:
            out.append(f"    M{d} ({n1} A {n2} VSS) nch_lvt_mac l=60n w={1 + d % 9}u multi=1 "
                       f"nf=2 sd=200n ad=1.2e-13 as=1.2e-13 pd=2.4u ps=2.4u")
        elif k == 1:
            out.append(f"    M{d} ({n1} B {n2} VDD) pch_lvt_mac l=60n w={2 + d % 7}u multi=1 "
                       f"nf=2 sd=200n ad=1.2e-13 as=1.2e-13 pd=2.4u ps=2.4u")
        elif k == 2:
            out.append(f"    R{d} ({n1} {n2}) rppolywo_m l={3 + d % 5}u w=1u m=1 "
                       f"segments=1 lr=2u wr=1u")
        elif k == 3:
            out.append(f"    C{d} ({n1} VSS) cfmom_2t nr=32 lr=5u w=50n s=50n m=1 stm=2 spm=6")
        else:
            out.append(f"    M{d} (Y {n1} VSS VSS) nch_mac l=100n w={1 + d % 4}u multi=1 nf=1 "
                       f"sd=200n ad=6e-14 as=6e-14 pd=1.2u ps=1.2u")
    out.append(f"ends {name}")
    return out


def bench_text(n_ports: int = N_PORTS, n_leaves: int = 320, dev_per_leaf: int = 140,
               n_blocks: int = 40) -> str:
    names = pin_names(n_ports)
    ports = (names["supply"] + names["rail"] + names["bias"] + names["en"] + names["none"]
             + names["ground"])
    lines = ["// synthetic top-level PMU bench: generated, no design content",
             "simulator lang=spectre", "global 0"]
    params = [f"p{i}={i % 7}" for i in range(40)] + ["VSET=3", "TRIM=0", "FC=1"]
    for i in range(0, len(params), 10):
        lines.append("parameters " + " ".join(params[i:i + 10]))
    lines += ['include "pdk/models.scs" section=tt',
              'include "pdk/rc.scs" section=typ',
              'include "pdk/mom.scs" section=typ']
    # leaves, then blocks built from them, then the PMU built from the blocks
    for i in range(n_leaves):
        lines += _leaf(f"leaf{i}", dev_per_leaf, i)
    for b in range(n_blocks):
        lines.append(f"subckt blk{b} VDD VSS IN OUT")
        for j in range(8):
            lines.append(f"    X{j} (IN n{j} OUT{j % 2} VDD VSS) leaf{(b * 8 + j) % n_leaves}")
        lines.append(f"    R0 (OUT0 OUT) rppolywo_m l=2u w=1u m=1")
        lines.append(f"ends blk{b}")
    lines.append("subckt pmu_synth " + " ".join(ports))
    gnds = names["ground"]
    for b in range(n_blocks):
        vdd = ports[b % len(names["supply"])]
        out = names["rail"][b % N_RAILS] if b % 3 == 0 else names["bias"][b % N_BIASES]
        lines.append(f"    XB{b} ({vdd} {gnds[b % len(gnds)]} {ports[b % len(ports)]} {out}) blk{b}")
    for i, p in enumerate(names["none"]):
        lines.append(f"    RPD{i} ({p} {gnds[i % len(gnds)]}) rppolywo_m l=20u w=1u m=1")
    lines.append("ends pmu_synth")
    # a second, small subckt instance next to the PMU (a candidate the picker offers)
    lines += ["subckt refsynth VREF VIN GND", "    R1 (VIN VREF) resistor r=100k",
              "    R2 (VREF GND) resistor r=100k", "ends refsynth"]

    # the testbench: nets named after the pins (a few differ), sources on the convention
    nets = {p: ("0" if p in gnds else f"n_{p.replace('<', '_').replace('>', '')}"
                if p.startswith("CTRL") else p) for p in ports}
    lines.append(f"I_PMU ({' '.join(nets[p] for p in ports)}) pmu_synth")
    lines.append("XREF (vref VSUP0 0) refsynth")
    for i, p in enumerate(names["supply"]):
        lines.append(f"VS_{p} ({p} 0) vsource dc={1.0 + 0.4 * i} type=dc")
    for i, p in enumerate(names["rail"]):
        lines.append(f"IL_{p} ({p} 0) isource dc={(i + 1) * 250}u type=dc")
    for i, p in enumerate(names["bias"]):
        lines.append(f"VB_{p} ({p} 0) vsource dc=0.{4 + i % 5} type=dc")
    for i, p in enumerate(names["en"]):
        lines.append(f"VEN_{p} ({p} 0) vsource dc=0.8 type=dc")
    for i, p in enumerate(names["none"][:24]):
        lines.append(f"V_{i} ({nets[p]} 0) vsource dc=0 type=dc")
    lines.append("VB_VREF (vref 0) vsource dc=0.6")
    lines.append("simulatorOptions options psfversion=\"1.4.0\" reltol=1e-3 vabstol=1e-6 "
                 "iabstol=1e-12 temp=27 tnom=27 gmin=1e-12")
    for a in ("dcOp dc write=\"spectre.dc\" maxiters=150 maxsteps=10000 annotate=status",
              "dcOpInfo info what=oppoint where=rawfile",
              "ac ac start=1 stop=10G dec=20 annotate=status",
              "noise ( n_VRAIL0 0 ) noise start=1 stop=10G dec=20",
              "tran tran stop=10u errpreset=conservative write=\"spectre.ic\"",
              "stb stb start=1 stop=10G probe=IL_VRAIL0 annotate=status",
              "pss pss fund=1M harms=10 errpreset=moderate",
              "pnoise ( n_VRAIL0 0 ) pnoise start=1 stop=10M pnoisemethod=fullspectrum"):
        lines.append(a)
    lines.append("modelParameter info what=models where=rawfile")
    lines.append("saveOptions options save=allpub")
    return "\n".join(lines) + "\n"


def write_bench(where, **kw) -> pathlib.Path:
    """Write the bench (input.scs) and its three PDK includes under `where`; return input.scs."""
    d = pathlib.Path(where)
    (d / "pdk").mkdir(parents=True, exist_ok=True)
    for f, secs in (("models.scs", ("tt", "ss", "ff")), ("rc.scs", ("typ", "cmax")),
                    ("mom.scs", ("typ", "max"))):
        body = "".join(f"section {s}\nparameters k_{s}=1\nendsection {s}\n" for s in secs)
        (d / "pdk" / f).write_text("simulator lang=spectre\n" + body, encoding="utf-8",
                                   newline="\n")
    p = d / "input.scs"
    p.write_text(bench_text(**kw), encoding="utf-8", newline="\n")
    return p
