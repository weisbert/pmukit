"""The Spectre section library body: what goes INSIDE each `section <corner>`.

`pmukit.deliverable.DeliverableWriter.write_scs` owns the container -- the `library` /
`section` / `ahdl_include` scaffolding and the file itself.  This module supplies the extra lines
that belong inside a section, so the consumer can read the library and know, without opening the
`.va`, which module a corner selects and how to instantiate it.

Contract 4 rule 4: corners are selected by Spectre `section`, so the consumer adds exactly ONE
include line to their corner setup and switches corners by section name.
"""
from __future__ import annotations

__all__ = ["extra_lines", "instance_template", "usage"]


def instance_template(module: str, ports, *, params=()) -> str:
    """The one-line Spectre instantiation the consumer copies."""
    pins = " ".join(str(p) for p in ports)
    tail = "".join(f" {k}={v}" for k, v in params)
    return f"I_PMU ({pins}) {module}{tail}"


def usage(library: str, section_names) -> list[str]:
    """The two lines a consumer adds to their own netlist, as comments."""
    first = list(section_names)[0] if list(section_names) else "tt"
    return [
        f"// consumer setup: include \"{library}.scs\" section={first}",
        f"// switch corners by changing section= to one of: "
        f"{', '.join(str(s) for s in section_names)}",
        f"// library name: {library}",
    ]


def extra_lines(modules: dict, *, library: str = "PMU_<project>",
                ports_by_corner: dict | None = None, params=(), envelope=None,
                common=()) -> dict:
    """Build the `extra_lines` mapping `DeliverableWriter.write_scs` accepts.

    `modules` maps corner -> emitted module name.  Each section gets a comment naming the module
    and a commented instantiation template; `'*'` carries the lines common to every section (the
    usage note and, when an envelope is given, the validity range in one line).

    Everything here is a COMMENT: the library must not silently add a `parameters` statement to
    the consumer's netlist.  Instance parameters (`vset`, `load_en_*`) belong on the instance the
    consumer writes, which is exactly what the template shows.
    """
    out: dict[str, list[str]] = {}
    shared = list(usage(library, list(modules)))
    if envelope is not None:
        loads = "; ".join(f"{p} {lo:g}..{hi:g} A" for p, (lo, hi) in envelope.load_a.items())
        shared.append(
            f"// valid: {loads or 'no rail characterized'} | "
            f"{envelope.temp_c[0]:g}..{envelope.temp_c[1]:g} C | up to "
            f"{envelope.freq_max_hz:g} Hz | VSET "
            f"{', '.join(str(v) for v in envelope.vset_codes) or '(none)'} "
            f"-- see envelope.json and report.md")
    shared += [str(x) for x in common]
    out["*"] = shared
    for corner, module in modules.items():
        ports = (ports_by_corner or {}).get(corner, [])
        lines = [f"// module {module} (process corner {corner})"]
        if ports:
            lines.append(f"// {instance_template(module, ports, params=params)}")
        out[corner] = lines
    return out
