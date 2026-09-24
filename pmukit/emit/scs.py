"""The Spectre section library body: what goes INSIDE each `section <corner>`.

`pmukit.deliverable.DeliverableWriter.write_scs` owns the container -- the `library` /
`section` / `ahdl_include` scaffolding and the file itself.  This module supplies the extra lines
that belong inside a section, so the consumer can read the library and know, without opening the
`.va`, which module a corner selects and how to instantiate it.

Contract 4 rule 4: corners are selected by Spectre `section`, so the consumer adds exactly ONE
include line to their corner setup and switches corners by section name.  Every section defines
the SAME module name (`PMU_<project>`) from its own corner's `.va`; `include ... section=<x>` reads
only section <x>, so one definition is live and the instance's master never changes.
"""
from __future__ import annotations

from ..deliverable import eng as _eng

__all__ = ["extra_lines", "instance_template", "instance_line", "usage"]


def instance_template(module: str, ports, *, params=(), name: str = "I_PMU") -> str:
    """The one-line Spectre instantiation the consumer copies."""
    pins = " ".join(str(p) for p in ports)
    tail = "".join(f" {k}={v}" for k, v in params)
    return f"{name} ({pins}) {module}{tail}"


def instance_line(module: str, interface, *, inst: str = "", params=()) -> str:
    """The consumer's OWN PMU instance line with the model as its master.

    The module's ports are the PMU's pins in the PMU's order (contract 4), so the testbench's
    instance -- its name and the net on every pin, as read from the netlist -- stays exactly as
    it was; only the master changes. A pin whose net was not recorded shows its pin name."""
    nets = [str(e.get("net") or e.get("pin")) for e in interface]
    return instance_template(module, nets, params=params, name=inst or "I_PMU")


def usage(library: str, section_names, module: str = "") -> list[str]:
    """The lines a consumer reads before adding their one include line, as comments."""
    names = [str(s) for s in section_names]
    first = names[0] if names else "tt"
    out = [
        f"// consumer setup: include \"{library}.scs\" section={first}",
        f"// switch corners by changing section= to one of: {', '.join(names)}",
        f"// library name: {library}",
    ]
    if module:
        out.append(f"// every section defines the same module {module} from its own corner's "
                   f".va -- include ONE section only (two would define {module} twice); the "
                   f"instance's master stays {module} on every corner")
    return out


def extra_lines(modules: dict, *, library: str = "PMU_<project>",
                ports_by_corner: dict | None = None, params=(), envelope=None,
                common=(), interface_by_corner: dict | None = None, inst: str = "",
                master: str = "") -> dict:
    """Build the `extra_lines` mapping `DeliverableWriter.write_scs` accepts.

    `modules` maps corner -> emitted module name.  Each section gets a comment naming the module
    and a commented instantiation template; `'*'` carries the lines common to every section (the
    usage note and, when an envelope is given, the validity range in one line).

    Everything here is a COMMENT: the library must not silently add a `parameters` statement to
    the consumer's netlist.  Instance parameters (`vset`, `load_en_*`) belong on the instance the
    consumer writes, which is exactly what the template shows.
    """
    out: dict[str, list[str]] = {}
    names = sorted(set(modules.values()))
    shared = list(usage(library, list(modules), module=names[0] if len(names) == 1 else ""))
    if envelope is not None:
        loads = "; ".join(f"{p} {_eng(lo, 'A')}..{_eng(hi, 'A')}"
                          for p, (lo, hi) in envelope.load_a.items())
        shared.append(
            f"// valid: {loads or 'no rail characterized'} | "
            f"{envelope.temp_c[0]:g}..{envelope.temp_c[1]:g} C | up to "
            f"{_eng(envelope.freq_max_hz, 'Hz')} | VSET "
            f"{', '.join(str(v) for v in envelope.vset_codes) or '(none)'} "
            f"-- see envelope.json and report.md")
    shared += [str(x) for x in common]
    out["*"] = shared
    for corner, module in modules.items():
        ports = (ports_by_corner or {}).get(corner, [])
        iface = (interface_by_corner or {}).get(corner) or []
        lines = [f"// module {module} (process corner {corner}, from {library}_{corner}.va)"]
        if iface:
            who = f"{master}'s" if master else "the PMU's"
            lines.append(f"// pins, in {who} order: {' '.join(e['pin'] for e in iface)}")
            through = [e["pin"] for e in iface if not e.get("modeled", True)]
            if through:
                lines.append(f"// pass-through (declared, not modeled): {' '.join(through)}")
            lines.append("// your PMU instance, master swapped -- no rewiring:")
            lines.append(f"// {instance_line(module, iface, inst=inst, params=params)}")
        elif ports:
            lines.append(f"// {instance_template(module, ports, params=params)}")
        out[corner] = lines
    return out
