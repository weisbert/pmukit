"""Contract 4: the deliverable -- one stamped directory the consumer takes away.

    $PMUKIT_DATA/<project>/deliver/<stamp>/
        PMU_<project>.scs          Spectre library: one `section` per process corner
        PMU_<project>_<corner>.va  one Verilog-A per corner, every one defining module PMU_<project>
        envelope.json              validity envelope
        report.md                  per-corner per-block grades, HB health check, what never ran
        grades.json                machine-readable sidecar of report.md (so diff never parses MD)
        provenance.json            config/dataset/spec sha, pmukit version, TB state, date
        interface.json             the PMU's pins in order, the pass-through ones, the instance line

This module owns the CONTAINER, not the model math: the directory layout, the section library,
the envelope, the provenance header repeated inside every .va, the report renderer, and reading
a deliverable back.  The `.va` body text is supplied by the emitter.

Rules taken straight from CONTRACTS.md section 4:
  1. every .va repeats provenance.json in its header, so a file that leaves the directory is
     still traceable;
  2. anything outside envelope.json must appear as RED text in the report -- the model never
     silently extrapolates;
  3. the deliverable directory never enters git; report.md carries numbers, not customer names;
  4. corners are selected by Spectre `section`, so the consumer adds ONE include line.

Contract 0c also fixes the first paragraph of report.md: valid range / usable but not signed off
/ never run / one green-yellow-red line per corner per rail, with **no internal score numbers**.
Scores survive in grades.json, which is what the digest and the version diff read.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
import pathlib
import re
from dataclasses import dataclass, field

from . import __version__, jsonio, paths
from .errors import PmuError

# A Spectre-native library file (`library` / `section` / `ahdl_include`) comments with `//`.
# `Provenance.header_text` still accepts '*' for SPICE-flavoured files, but the .scs we write is
# spectre-native, so emitting '*' there would be a syntax risk for no gain.
SCS_COMMENT = "//"
VA_COMMENT = "//"

GRADES = ("green", "yellow", "red", "not_run")
# Worst-first ranking used to collapse per-block grades into one line per corner per rail.
# `red` outranks `not_run`: a measured failure is a stronger statement than missing data, but
# neither may ever be masked by a `green` sibling block.
_GRADE_RANK = {"green": 0, "yellow": 1, "not_run": 2, "red": 3}
_GRADE_MEANING = {
    "green": "use it as delivered",
    "yellow": "usable -- read the note",
    "red": "do not rely on this cell",
    "not_run": "never characterized; nothing was fitted here",
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STAMP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

REPORT_NAME = "report.md"
GRADES_NAME = "grades.json"
ENVELOPE_NAME = "envelope.json"
PROVENANCE_NAME = "provenance.json"
INTERFACE_NAME = "interface.json"


# --------------------------------------------------------------------------- helpers
def _lf(text: str) -> str:
    """Normalize to LF and guarantee a trailing newline (the box checks digests with sha256sum)."""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if t and not t.endswith("\n"):
        t += "\n"
    return t


def _oneline(text: str) -> str:
    return " ".join(str(text).split())


def _num(x: float) -> str:
    """Compact, stable number text for the report (no internal scores are ever formatted here)."""
    return f"{float(x):g}"


def eng(x: float, unit: str = "") -> str:
    """Engineering notation for the prose of report.md: 2e-06 A -> '2 uA', 1e+09 Hz -> '1 GHz'.

    Only human-read text uses it (report.md, the conditioning lines of `pmukit.emit.lint`, the
    .scs comments); envelope.json, grades.json and provenance.json keep plain floats so a script
    reads exactly what was characterized."""
    v = float(x)
    if v == 0 or not math.isfinite(v):
        return f"{_num(v)} {unit}".strip()
    sign, a = ("-" if v < 0 else ""), abs(v)
    for exp, prefix in ((12, "T"), (9, "G"), (6, "M"), (3, "k"), (0, ""), (-3, "m"), (-6, "u"),
                        (-9, "n"), (-12, "p"), (-15, "f")):
        if a >= 10.0 ** exp * (1 - 1e-12) or exp == -15:
            return f"{sign}{a / 10.0 ** exp:.4g} {prefix}{unit}".strip()
    return f"{_num(v)} {unit}".strip()                                 # pragma: no cover


_eng = eng


def ratio(x: float) -> str:
    """A dimensionless ratio for human-read text: 1e+06 -> '1e6', 3.2e+07 -> '3.2e7', 250 -> '250'.

    A range or a gain has no unit to hang an SI prefix on ('1 M' reads like a resistance), so it
    keeps a power of ten -- without the '+0' of Python's float formatting."""
    v = float(x)
    if not math.isfinite(v):
        return "n/a" if v != v else ("inf" if v > 0 else "-inf")
    if v == 0 or 1e-3 <= abs(v) < 1e4:
        return f"{v:.4g}"
    mant, _, exp = f"{v:.3e}".partition("e")
    mant = mant.rstrip("0").rstrip(".")
    return f"{mant}e{int(exp)}"


def _stamp_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_ident(value: str, kind: str, where: str) -> str:
    if not isinstance(value, str) or not _IDENT.match(value):
        raise PmuError(
            what=f"{kind} {value!r} is not a plain identifier.",
            why=f"{kind} becomes part of a file name and of a Spectre `section` name, so it must "
                f"match [A-Za-z_][A-Za-z0-9_]* -- no dots, slashes or spaces.",
            do=[f"rename the {kind} (for example 'ss' or 'MOSff_RCss')",
                "if it came from the PDK, map it to a plain name in the project config"],
            where=where,
        )
    return value


# --------------------------------------------------------------------------- envelope
@dataclass
class Envelope:
    """CONTRACTS.md section 4: anything outside this must appear as RED text in the report; the
    model never silently extrapolates."""

    freq_max_hz: float
    load_a: dict[str, tuple[float, float]]      # port -> (min, max) characterized load
    temp_c: tuple[float, float]
    corners: list[str]
    vset_codes: list[int]
    ls_default_on: list[str]                    # large-signal items that passed the HB check
    notes: list[str] = field(default_factory=list)
    #: Every port that was characterized, INCLUDING those with no load axis. A bias pin IS
    #: characterized but has no load range, so `load_a` alone cannot answer "was this port
    #: characterized?" -- asking it that marked every fully-characterized bias RED in report.md.
    #: Empty means "fall back to the rails", so an older envelope still reads correctly.
    ports: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            self.freq_max_hz = float(self.freq_max_hz)
            self.load_a = {str(k): (float(v[0]), float(v[1])) for k, v in dict(self.load_a).items()}
            self.temp_c = (float(self.temp_c[0]), float(self.temp_c[1]))
            self.corners = [str(c) for c in self.corners]
            self.vset_codes = [int(v) for v in self.vset_codes]
            self.ls_default_on = [str(s) for s in self.ls_default_on]
            self.notes = [str(s) for s in self.notes]
            self.ports = [str(s) for s in self.ports]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise PmuError(
                what="the validity envelope is malformed.",
                why=f"a field could not be read as its declared type: {exc}.",
                do=["freq_max_hz: float; load_a: {port: (min, max)}; temp_c: (min, max); "
                    "corners/ls_default_on: list of str; vset_codes: list of int"],
                where="pmukit.deliverable.Envelope",
            ) from exc

    # -- queries ----------------------------------------------------------
    def contains(self, *, freq_hz=None, temp_c=None, port=None, load_a=None,
                 corner=None, vset=None) -> tuple[bool, list[str]]:
        """(inside?, [reasons it is outside]). The report and the CLI both print the reasons."""
        why: list[str] = []

        if freq_hz is not None and float(freq_hz) > self.freq_max_hz:
            why.append(f"frequency {_num(freq_hz)} Hz is above the characterized ceiling "
                       f"{_num(self.freq_max_hz)} Hz")

        if temp_c is not None:
            lo, hi = self.temp_c
            if not (lo <= float(temp_c) <= hi):
                why.append(f"temperature {_num(temp_c)} C is outside the characterized range "
                           f"{_num(lo)} to {_num(hi)} C")

        characterized = list(self.ports) or list(self.load_a)
        known_ports = ", ".join(sorted(characterized)) or "(none)"
        rails = ", ".join(sorted(self.load_a)) or "(none)"
        if port is not None and port not in characterized:
            why.append(f"port {port!r} was not characterized (characterized: {known_ports})")

        if load_a is not None:
            if port is None:
                why.append(f"load {_num(load_a)} A was given without a port, so it cannot be "
                           f"checked against a per-rail range (rails: {rails})")
            elif port in self.load_a:
                lo, hi = self.load_a[port]
                if not (lo <= float(load_a) <= hi):
                    why.append(f"load {_num(load_a)} A on {port} is outside the characterized "
                               f"range {_num(lo)} to {_num(hi)} A")

        if corner is not None and str(corner) not in self.corners:
            why.append(f"corner {str(corner)!r} was not characterized "
                       f"(characterized: {', '.join(self.corners) or '(none)'})")

        if vset is not None:
            try:
                code = int(vset)
            except (TypeError, ValueError):
                why.append(f"VSET code {vset!r} is not an integer code")
            else:
                if code not in self.vset_codes:
                    why.append(f"VSET code {code} was not characterized (characterized: "
                               f"{', '.join(str(v) for v in self.vset_codes) or '(none)'})")

        return (not why), why

    # -- io ---------------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "freq_max_hz": self.freq_max_hz,
            "load_a": {k: [v[0], v[1]] for k, v in self.load_a.items()},
            "temp_c": [self.temp_c[0], self.temp_c[1]],
            "corners": list(self.corners),
            "vset_codes": list(self.vset_codes),
            "ls_default_on": list(self.ls_default_on),
            "ports": list(self.ports),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Envelope":
        try:
            return cls(freq_max_hz=obj["freq_max_hz"], load_a=obj["load_a"],
                       temp_c=obj["temp_c"], corners=obj["corners"],
                       vset_codes=obj["vset_codes"], ls_default_on=obj.get("ls_default_on", []),
                       ports=obj.get("ports", []),
                       notes=obj.get("notes", []))
        except KeyError as exc:
            raise PmuError(
                what=f"envelope.json is missing the key {exc.args[0]!r}.",
                why="the validity envelope is the contract with the consumer; a partial envelope "
                    "would let the model be used outside what was characterized.",
                do=["re-run `pmukit deliver` for this project",
                    "or repair envelope.json by hand against CONTRACTS.md section 4"],
                where=ENVELOPE_NAME,
            ) from exc


# --------------------------------------------------------------------------- provenance
@dataclass
class Provenance:
    """What every delivered file carries so it stays traceable away from its directory."""

    config_sha: str
    dataset_sha: str
    spec_sha: str
    pmukit_version: str
    created: str
    tb_state_note: str
    netlist_sha: str = ""
    host: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def now(cls, *, config_sha: str = "", dataset_sha: str = "", spec_sha: str = "",
            tb_state_note: str = "", **kw) -> "Provenance":
        """Convenience constructor: pmukit version and UTC timestamp filled in."""
        return cls(config_sha=config_sha, dataset_sha=dataset_sha, spec_sha=spec_sha,
                   pmukit_version=__version__, created=_now_iso(),
                   tb_state_note=tb_state_note, **kw)

    def header_text(self, comment: str = "//") -> str:
        """The block repeated at the top of every .va so a file that leaves the directory is still
        traceable (CONTRACTS.md section 4 rule 1). `comment` is '//' for Verilog-A, '*' for .scs
        (SPICE flavour); the spectre-native library we write uses '//'."""
        c = str(comment)
        rule = f"{c} " + "-" * 72
        lines = [
            rule,
            f"{c} pmukit {self.pmukit_version or '(unknown)'} -- generated model, do not edit",
            f"{c} created     : {_oneline(self.created) or '(unknown)'}",
            f"{c} config sha  : {_oneline(self.config_sha) or '(none)'}",
            f"{c} dataset sha : {_oneline(self.dataset_sha) or '(none)'}",
            f"{c} spec sha    : {_oneline(self.spec_sha) or '(none)'}",
        ]
        if self.netlist_sha:
            lines.append(f"{c} netlist sha : {_oneline(self.netlist_sha)}")
        if self.host:
            lines.append(f"{c} host        : {_oneline(self.host)}")
        lines.append(f"{c} TB state    : {_oneline(self.tb_state_note) or '(not recorded)'}")
        for key in sorted(self.extra):
            lines.append(f"{c} {str(key)[:11]:<11} : {_oneline(self.extra[key])}")
        lines.append(f"{c} valid range, grades, what never ran: envelope.json and report.md")
        lines.append(rule)
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict:
        return {"config_sha": self.config_sha, "dataset_sha": self.dataset_sha,
                "spec_sha": self.spec_sha, "pmukit_version": self.pmukit_version,
                "created": self.created, "tb_state_note": self.tb_state_note,
                "netlist_sha": self.netlist_sha, "host": self.host, "extra": dict(self.extra)}

    @classmethod
    def from_json(cls, obj: dict) -> "Provenance":
        try:
            return cls(config_sha=obj["config_sha"], dataset_sha=obj["dataset_sha"],
                       spec_sha=obj["spec_sha"], pmukit_version=obj["pmukit_version"],
                       created=obj["created"], tb_state_note=obj["tb_state_note"],
                       netlist_sha=obj.get("netlist_sha", ""), host=obj.get("host", ""),
                       extra=obj.get("extra", {}))
        except KeyError as exc:
            raise PmuError(
                what=f"provenance.json is missing the key {exc.args[0]!r}.",
                why="provenance is what makes a delivered file reproducible; a partial record "
                    "cannot be tied back to a config and a dataset.",
                do=["re-run `pmukit deliver` for this project"],
                where=PROVENANCE_NAME,
            ) from exc


# --------------------------------------------------------------------------- grade
@dataclass
class Grade:
    """One (port, corner, block) verdict. `detail` is plain language; `score` never reaches the
    per-rail table in report.md (contract 0c) but does travel in grades.json and the digest."""

    port: str
    corner: str
    block: str
    grade: str
    detail: str = ""
    score: float | None = None

    def __post_init__(self) -> None:
        if self.grade not in GRADES:
            raise PmuError(
                what=f"grade {self.grade!r} is not one of {'/'.join(GRADES)}.",
                why="the report shows one green/yellow/red line per corner per rail; an unknown "
                    "verdict cannot be rendered or compared between two deliverables.",
                do=[f"use one of {', '.join(GRADES)}",
                    "use 'not_run' when the data behind the block was never produced"],
                where=f"{self.port}/{self.corner}/{self.block}",
            )
        if self.score is not None:
            self.score = float(self.score)

    @property
    def rank(self) -> int:
        return _GRADE_RANK[self.grade]

    def to_json(self) -> dict:
        return {"port": self.port, "corner": self.corner, "block": self.block,
                "grade": self.grade, "detail": self.detail, "score": self.score}

    @classmethod
    def from_json(cls, obj: dict) -> "Grade":
        return cls(port=obj["port"], corner=obj["corner"], block=obj["block"],
                   grade=obj["grade"], detail=obj.get("detail", ""), score=obj.get("score"))


# --------------------------------------------------------------------------- writer
class DeliverableWriter:
    """Builds `$PMUKIT_DATA/<project>/deliver/<stamp>/`."""

    def __init__(self, project: str, *, root: pathlib.Path | None = None,
                 stamp: str | None = None) -> None:
        """`root` overrides $PMUKIT_DATA; `stamp` defaults to UTC 'YYYYmmdd-HHMMSS'."""
        _check_ident(project, "project name", "project config")
        self.project = project
        self.stamp = str(stamp) if stamp else _stamp_now()
        if not _STAMP.match(self.stamp):
            raise PmuError(
                what=f"deliverable stamp {self.stamp!r} is not a safe directory name.",
                why="the stamp becomes a directory under deliver/, so it must not contain path "
                    "separators or start with a dot.",
                do=["leave `stamp` unset to get the UTC 'YYYYmmdd-HHMMSS' default"],
                where="DeliverableWriter(stamp=...)",
            )
        base = pathlib.Path(root) if root is not None else paths.data_root()
        self.path = base / project / "deliver" / self.stamp
        self.path.mkdir(parents=True, exist_ok=True)
        self._corners: dict[str, str] = {}          # corner -> .va file name, insertion ordered

    # -- names ------------------------------------------------------------
    @property
    def scs_name(self) -> str:
        return f"PMU_{self.project}.scs"

    def va_name(self, corner: str) -> str:
        return f"PMU_{self.project}_{corner}.va"

    def _write(self, name: str, text: str) -> pathlib.Path:
        p = self.path / name
        p.write_text(_lf(text), encoding="utf-8", newline="\n")
        return p

    # -- parts ------------------------------------------------------------
    def add_va(self, corner: str, body: str, *, provenance: Provenance) -> pathlib.Path:
        """Writes PMU_<project>_<corner>.va = provenance header + body. LF. Refuses a corner name
        that is not a plain identifier."""
        _check_ident(corner, "corner name", f"{self.path}")
        name = self.va_name(corner)
        self._corners[corner] = name
        return self._write(name, provenance.header_text(VA_COMMENT) + _lf(body))

    def write_scs(self, *, provenance: Provenance,
                  extra_lines: dict[str, list[str]] | None = None) -> pathlib.Path:
        """The section library. One `section <corner> ... endsection` per added .va, each with the
        matching `ahdl_include` line. The consumer adds ONE include line to their corner setup and
        switches corners by section name (CONTRACTS.md section 4 rule 4).

        Every .va defines the same module name, which is only legal because each lives in its
        own section: `include "<lib>.scs" section=<x>` makes Spectre read section <x> and skip
        the others, so exactly one `ahdl_include` -- one definition -- is live.

        `extra_lines` maps a corner (or '*' for every section) to extra lines placed inside that
        section, e.g. a `parameters` line the emitter needs."""
        if not self._corners:
            raise PmuError(
                what="cannot write the Spectre section library: no .va was added.",
                why="the library is one `section` per emitted corner; with no corner there is "
                    "nothing for the consumer to select.",
                do=["call add_va(corner, body, provenance=...) for each corner first"],
                where=str(self.path / self.scs_name),
            )
        extra = {k: [str(x) for x in v] for k, v in (extra_lines or {}).items()}
        for key in extra:
            if key != "*" and key not in self._corners:
                raise PmuError(
                    what=f"extra_lines names corner {key!r}, which has no .va in this deliverable.",
                    why="every section in the library must correspond to a file that was written, "
                        "or the consumer's include would fail at elaboration.",
                    do=[f"add_va({key!r}, ...) first, or drop the key",
                        f"corners present: {', '.join(self._corners) or '(none)'}"],
                    where=str(self.path / self.scs_name),
                )
        common = extra.get("*", [])
        lib = f"PMU_{self.project}"
        out = [provenance.header_text(SCS_COMMENT).rstrip("\n"), "", f"library {lib}"]
        for corner, va in self._corners.items():
            out.append("")
            out.append(f"section {corner}")
            out.append(f'    ahdl_include "{va}"')
            for line in common + extra.get(corner, []):
                out.append(f"    {line}")
            out.append(f"endsection {corner}")
        out += ["", f"endlibrary {lib}"]
        return self._write(self.scs_name, "\n".join(out))

    def write_envelope(self, env: Envelope) -> pathlib.Path:
        return jsonio.write(self.path / ENVELOPE_NAME, env.to_json())

    def write_provenance(self, p: Provenance) -> pathlib.Path:
        return jsonio.write(self.path / PROVENANCE_NAME, p.to_json())

    # -- report -----------------------------------------------------------
    def write_report(self, *, envelope: Envelope, grades: list[Grade],
                     hb_check: dict | None = None, not_run: list[str],
                     stubs: list[str] | None = None,
                     pins: list[dict] | None = None, graded_by: str = "",
                     provisional: str = "") -> pathlib.Path:
        """Renders report.md plus its machine-readable sidecar grades.json.

        `pins` is the module's pin list in the PMU's order ({pin, modeled, what, role}); the
        report says which pins are pass-through and what that means.

        `graded_by` says where the grades came from ("verify" or "fit"); `provisional` is the
        sentence saying why they are not verify's verdict on THIS fit (verify is older than the
        fit, or never ran). Both land in grades.json, and `provisional` is printed in report.md
        next to the stamp and above the trust table."""
        grades = [g if isinstance(g, Grade) else Grade.from_json(g) for g in grades]
        not_run = [str(x) for x in (not_run or [])]
        stubs = [str(x) for x in (stubs or [])]
        pins = [dict(p) for p in (pins or [])]
        text = render_report(project=self.project, stamp=self.stamp, envelope=envelope,
                             grades=grades, hb_check=hb_check, not_run=not_run, stubs=stubs,
                             pins=pins, provisional=provisional)
        jsonio.write(self.path / GRADES_NAME, {
            "project": self.project, "stamp": self.stamp,
            "graded_by": str(graded_by or ("fit" if provisional else
                                           "verify" if grades else "")),
            "provisional": str(provisional or ""),
            "grades": [g.to_json() for g in grades],
            "hb_check": hb_check, "not_run": not_run, "stubs": stubs,
            "pass_through": [p.get("pin") for p in pins if not p.get("modeled", True)],
        })
        return self._write(REPORT_NAME, text)

    def write_interface(self, interface: dict) -> pathlib.Path:
        """interface.json: how the consumer instantiates the model -- the PMU's pins in order,
        which are pass-through, and the instance line per corner (the Deliver screen shows it).
        It carries the testbench's instance and net names, so it is never excerpted into git,
        exactly like the .scs whose comments say the same."""
        return jsonio.write(self.path / INTERFACE_NAME, interface)

    # -- close ------------------------------------------------------------
    def finish(self) -> pathlib.Path:
        """Asserts every required file exists; PmuError listing what is missing."""
        missing = [n for n in (self.scs_name, ENVELOPE_NAME, PROVENANCE_NAME, REPORT_NAME)
                   if not (self.path / n).is_file()]
        if not any(p.suffix == ".va" for p in self.path.iterdir() if p.is_file()):
            missing.append(f"PMU_{self.project}_<corner>.va (at least one corner)")
        if missing:
            raise PmuError(
                what=f"the deliverable is incomplete: {len(missing)} required file(s) missing.",
                why="a deliverable without " + ", ".join(missing) + " cannot be used or trusted: "
                    "CONTRACTS.md section 4 lists every file the consumer needs.",
                do=[f"write the missing file(s): {', '.join(missing)}",
                    "or delete the stamped directory and re-run `pmukit deliver`"],
                where=str(self.path),
            )
        return self.path


# --------------------------------------------------------------------------- report renderer
def _fixed_paragraph(project: str, envelope: Envelope, not_run: list[str],
                     stubs: list[str]) -> list[str]:
    """Contract 0c: the first paragraph is fixed -- four items, in this order."""
    loads = "; ".join((f"{port} {_eng(lo, 'A')} only" if lo == hi else
                       f"{port} {_eng(lo, 'A')} to {_eng(hi, 'A')}")
                      for port, (lo, hi) in envelope.load_a.items()) or "no rail characterized"
    t_lo, t_hi = envelope.temp_c
    temp = (f"{_num(t_lo)} C only" if t_lo == t_hi else f"{_num(t_lo)} to {_num(t_hi)} C")
    valid = (f"load per rail {loads}; temperature {temp}; "
             f"frequency up to {_eng(envelope.freq_max_hz, 'Hz')}; "
             f"corners {', '.join(envelope.corners) or '(none)'}; "
             f"VSET codes {', '.join(str(v) for v in envelope.vset_codes) or '(none)'}.")
    # The "en" tier (usable, not signed off) has no dedicated field in the envelope dataclass,
    # so it travels in `notes` -- one line per item, the way report.md prints them.
    usable = " ".join(envelope.notes) if envelope.notes else "nothing in this tier."
    never = list(not_run) + [f"{s} (stub, not modeled)" for s in stubs]
    never_txt = "; ".join(never) + "." if never else "nothing -- every planned item ran."
    return [
        f"# {project} -- can I trust this model in my simulation?",
        "",
        f"- **Valid range:** {valid}",
        f"- **Usable, not signed off:** {usable}",
        f"- **Never run:** {never_txt}",
        "- **Trust per corner and rail:** one green/yellow/red line per corner per rail below. "
        "Internal fit scores are deliberately not printed here; they live in `grades.json`.",
        "",
    ]


def _worst_by_cell(grades: list[Grade]) -> dict[tuple[str, str], Grade]:
    worst: dict[tuple[str, str], Grade] = {}
    for g in grades:
        key = (g.port, g.corner)
        cur = worst.get(key)
        if cur is None or g.rank > cur.rank:
            worst[key] = g
    return worst


def _md_cell(text: str) -> str:
    return _oneline(text).replace("|", "/") or "--"


def _pins_section(pins: list[dict]) -> list[str]:
    """Contract 4: the model has the PMU's pins in the PMU's order. Say which ones are only
    declared -- a consumer who drives EN and sees nothing happen must find the reason here."""
    through = [p for p in pins if not p.get("modeled", True)]
    out = ["## Pins", "",
           "The model has the same pins, in the same order, as the PMU subcircuit: replace the "
           "PMU's master with the model and keep the instance's wiring.", "",
           "| # | pin | in the model |", "|---|---|---|"]
    for i, p in enumerate(pins, 1):
        mark = "**pass-through:** " if not p.get("modeled", True) else ""
        out.append(f"| {i} | {_md_cell(p.get('pin', ''))} | {mark}{_md_cell(p.get('what', ''))} |")
    out.append("")
    if through:
        out.append(f"- **Pass-through pins** ({', '.join(str(p.get('pin')) for p in through)}): "
                   "declared so the instance wires up exactly like the PMU, but NOT modeled. "
                   "Whatever the bench drives onto them has no effect on the model; each is tied "
                   "to the model's ground through 1 GOhm so it never floats.")
        if any(p.get("role") == "en" for p in through):
            out.append("- **EN has no effect:** the model is always on. Driving EN low in the "
                       "system bench does not turn the rails or the biases off.")
        out.append("")
    return out


def render_report(*, project: str, stamp: str, envelope: Envelope, grades: list[Grade],
                  hb_check: dict | None, not_run: list[str], stubs: list[str],
                  pins: list[dict] | None = None, provisional: str = "") -> str:
    """report.md as CONTRACTS.md section 0c and section 4 describe it."""
    out = _fixed_paragraph(project, envelope, not_run, stubs)
    out += [f"Deliverable stamp `{stamp}`. Anything outside the valid range above is marked "
            "**RED:** below; the model never silently extrapolates.", ""]
    warn = []
    if provisional:
        warn = [f"> **Provisional grades:** {_oneline(provisional)}. Every grade below is the "
                "fit's own verdict, not the HB health check's; run verify, then deliver again "
                "for the signed-off grades.", ""]
    out += warn

    # 2 -- usable but not signed off ("en" tier), then the large-signal terms that are on
    out += ["## Usable but not signed off", ""]
    if envelope.notes:
        for note in envelope.notes:
            out.append(f"- {note}")
    else:
        out.append("- Nothing in this tier.")
    if envelope.ls_default_on:
        out.append(f"- Large-signal terms on by default (each passed the HB health check): "
                   f"{', '.join(envelope.ls_default_on)}.")
    else:
        out.append("- No large-signal term is enabled in this deliverable.")
    out.append("")

    # 3 -- never run
    out += ["## Never run", ""]
    if not not_run and not stubs:
        out.append("- Nothing. Every planned item produced data.")
    for item in not_run:
        out.append(f"- **RED:** {item} -- never run, nothing was fitted from it.")
    for port in stubs:
        out.append(f"- **RED:** {port} -- stub, not modeled. The pin is emitted as an ideal "
                   f"source at its DC value.")
    out.append("")

    # 3b -- the pins: the same pins in the same order as the PMU, and which ones do nothing
    if pins:
        out += _pins_section(pins)

    # 4 -- one line per corner per rail, no internal scores
    out += ["## Trust per corner and rail", ""] + warn + [
            "| rail | corner | grade | worst block | what it means |",
            "|---|---|---|---|---|"]
    worst = _worst_by_cell(grades)
    if not worst:
        out.append("| -- | -- | not_run | -- | no block was graded in this deliverable |")
    for (port, corner), g in worst.items():
        inside, why = envelope.contains(port=port, corner=corner)
        meaning = _md_cell(g.detail) if g.detail else _GRADE_MEANING[g.grade]
        if not inside:
            meaning = f"{meaning} ({'; '.join(why)})"
        mark = "**RED:** " if g.grade in ("red", "not_run") or not inside else ""
        out.append(f"| {_md_cell(port)} | {_md_cell(corner)} | {g.grade} | "
                   f"{_md_cell(g.block)} | {mark}{meaning} |")
    out.append("")

    # 5 -- per-block detail and the HB health check
    out += ["## Per-block detail", "",
            "| rail | corner | block | grade | note |", "|---|---|---|---|---|"]
    if not grades:
        out.append("| -- | -- | -- | not_run | nothing was fitted |")
    for g in grades:
        mark = "**RED:** " if g.grade in ("red", "not_run") else ""
        note = _md_cell(g.detail) if g.detail else _GRADE_MEANING[g.grade]
        out.append(f"| {_md_cell(g.port)} | {_md_cell(g.corner)} | {_md_cell(g.block)} | "
                   f"{g.grade} | {mark}{note} |")
    out.append("")

    out += ["## HB health check", ""]
    if not hb_check:
        out.append("- **RED:** the harmonic-balance health check was never run, so no "
                   "large-signal term is signed off for HB use.")
    else:
        status = str(hb_check.get("status", "unknown"))
        if status.lower() not in ("pass", "ok", "green"):
            out.append(f"- **RED:** status `{status}`.")
        else:
            out.append(f"- status `{status}`.")
        for key in sorted(k for k in hb_check if k != "status"):
            out.append(f"- {key}: {_oneline(hb_check[key])}")
    out.append("")

    # envelope violations seen in the grade list, gathered in one place
    viol: list[str] = []
    for (port, corner) in worst:
        inside, why = envelope.contains(port=port, corner=corner)
        if not inside:
            viol.append(f"- **RED:** {port} / {corner}: {'; '.join(why)}")
    if viol:
        out += ["## Outside the validity envelope", ""] + viol + [""]

    out += ["---", "",
            "Numbers only -- this report carries no net or cell names from the customer design.",
            ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- reader
class Deliverable:
    """Reads back a stamped deliverable directory."""

    def __init__(self, path: pathlib.Path, envelope: Envelope, provenance: Provenance) -> None:
        self.path = pathlib.Path(path)
        self.envelope = envelope
        self.provenance = provenance
        self.stamp = self.path.name

    def __repr__(self) -> str:                                    # pragma: no cover - debug aid
        return f"<Deliverable {self.path.parent.parent.name}/{self.stamp}>"

    @classmethod
    def open(cls, path) -> "Deliverable":
        p = pathlib.Path(path)
        if not p.is_dir():
            raise PmuError(
                what=f"no deliverable directory at {p}.",
                why="a deliverable is the stamped directory written by `pmukit deliver`; this "
                    "path does not exist or is a file.",
                do=["run `pmukit deliver <project>`",
                    "or pick an existing stamp from `pmukit list <project> --deliverables`"],
                where=str(p),
            )
        missing = [n for n in (ENVELOPE_NAME, PROVENANCE_NAME) if not (p / n).is_file()]
        if missing:
            raise PmuError(
                what=f"the deliverable at {p.name} is missing {', '.join(missing)}.",
                why="the envelope and the provenance record are what make a deliverable usable "
                    "and traceable; without them nothing can be read back.",
                do=["re-run `pmukit deliver` for this project",
                    "or delete the incomplete stamp directory"],
                where=str(p),
            )
        return cls(p, Envelope.from_json(jsonio.read(p / ENVELOPE_NAME)),
                   Provenance.from_json(jsonio.read(p / PROVENANCE_NAME)))

    @classmethod
    def list(cls, project: str, root=None) -> "list[Deliverable]":
        """Every readable deliverable of a project, newest first."""
        base = (pathlib.Path(root) if root is not None else paths.data_root())
        deliver = base / project / "deliver"
        if not deliver.is_dir():
            return []
        out = []
        for d in sorted((x for x in deliver.iterdir() if x.is_dir()),
                        key=lambda x: x.name, reverse=True):
            if (d / ENVELOPE_NAME).is_file() and (d / PROVENANCE_NAME).is_file():
                out.append(cls.open(d))
        return out

    @classmethod
    def latest(cls, project: str, root=None) -> "Deliverable | None":
        found = cls.list(project, root)
        return found[0] if found else None

    # -- content ----------------------------------------------------------
    def files(self) -> "list[str]":
        return sorted(p.name for p in self.path.iterdir() if p.is_file())

    def read_file(self, name: str) -> str:
        """Read one file of this deliverable by name. Refuses anything that is not a bare name."""
        bad = (not isinstance(name, str) or not name or name in (".", "..")
               or "/" in name or "\\" in name or ".." in name
               or name.startswith("~") or pathlib.PurePath(name).is_absolute())
        if bad:
            raise PmuError(
                what=f"refusing to read {name!r} from a deliverable.",
                why="only a bare file name inside the stamped directory may be read; a path "
                    "separator, '..' or an absolute path would reach outside it.",
                do=[f"pass one of: {', '.join(self.files())}"],
                where=str(self.path),
            )
        p = self.path / name
        try:
            inside = p.resolve().parent == self.path.resolve()
        except OSError:                                           # pragma: no cover - fs oddity
            inside = False
        if not inside or not p.is_file():
            raise PmuError(
                what=f"{name!r} is not a file of this deliverable.",
                why="the name does not resolve to a regular file directly inside the stamped "
                    "directory.",
                do=[f"pass one of: {', '.join(self.files())}"],
                where=str(self.path),
            )
        return p.read_text(encoding="utf-8")

    def grades(self) -> "list[Grade]":
        """The machine-readable sidecar of report.md (empty when the report was never written)."""
        p = self.path / GRADES_NAME
        if not p.is_file():
            return []
        return [Grade.from_json(g) for g in jsonio.read(p).get("grades", [])]

    def interface(self) -> dict | None:
        """interface.json (the pins and the instance line), or None for a deliverable written
        before pmukit recorded it -- the caller then has no instance line to show, not a guess."""
        p = self.path / INTERFACE_NAME
        if not p.is_file():
            return None
        try:
            d = jsonio.read(p)
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def grades_meta(self) -> dict:
        """{"graded_by", "provisional"} from grades.json: where the grades came from and, when
        they are not verify's verdict on this fit, why. Empty strings for an older deliverable
        that did not record it."""
        p = self.path / GRADES_NAME
        try:
            d = jsonio.read(p) if p.is_file() else {}
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        return {"graded_by": str(d.get("graded_by") or ""),
                "provisional": str(d.get("provisional") or "")}

    def scs_name(self) -> str:
        """The section library's file name ('' when this deliverable has none)."""
        return next((n for n in self.files() if n.endswith(".scs")), "")

    def include_line(self, corner: str | None = None) -> str:
        """The ONE line the consumer adds: the library's path as this OS writes it -- one
        separator throughout, POSIX on the red zone -- and the section to select."""
        name = self.scs_name()
        if not name:
            return ""
        sec = corner or (self.envelope.corners[0] if self.envelope.corners else "tt")
        return f'include "{self.path / name}" section={sec}'

    def _file_shas(self) -> dict[str, str]:
        return {n: jsonio.sha_file(self.path / n, 16) for n in self.files()}

    def _text(self, name: str) -> str:
        try:
            return (self.path / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    # -- compare ----------------------------------------------------------
    def diff(self, other: "Deliverable") -> dict:
        """What the Home screen's 'compare two delivered versions' route returns."""
        a_prov, b_prov = self.provenance.to_json(), other.provenance.to_json()
        prov = {k: (a_prov.get(k), b_prov.get(k))
                for k in sorted(set(a_prov) | set(b_prov)) if a_prov.get(k) != b_prov.get(k)}

        a_env, b_env = self.envelope.to_json(), other.envelope.to_json()
        env = {k: (a_env.get(k), b_env.get(k))
               for k in sorted(set(a_env) | set(b_env)) if a_env.get(k) != b_env.get(k)}

        a_files, b_files = self._file_shas(), other._file_shas()
        files = {
            "added": sorted(set(b_files) - set(a_files)),
            "removed": sorted(set(a_files) - set(b_files)),
            "changed": sorted(n for n in set(a_files) & set(b_files) if a_files[n] != b_files[n]),
        }

        a_g = {(g.port, g.corner, g.block): g.grade for g in self.grades()}
        b_g = {(g.port, g.corner, g.block): g.grade for g in other.grades()}
        grades = {k: (a_g.get(k), b_g.get(k))
                  for k in sorted(set(a_g) | set(b_g)) if a_g.get(k) != b_g.get(k)}

        return {"provenance": prov, "envelope": env, "files": files, "grades": grades,
                "pins": _pins_diff(self.interface(), other.interface()),
                "text": _text_diffs(self, other, files["changed"])}


# --------------------------------------------------------------------------- diff helpers
#: A text diff is for reading, not for patching: bounded per file and in total.
DIFF_LINES_PER_FILE = 160
DIFF_LINES_TOTAL = 480
DIFF_CONTEXT = 2


def _pins_diff(a: dict | None, b: dict | None) -> dict:
    """What changed in the module's pin list (interface.json). {} when nothing did."""
    if not a or not b:
        if not a and not b:
            return {}
        return {"note": ("the older" if not a else "the newer") + " deliverable predates "
                "interface.json, so its pin list was not recorded"}
    pa = [(str(p.get("pin")), bool(p.get("modeled", True))) for p in a.get("pins") or []]
    pb = [(str(p.get("pin")), bool(p.get("modeled", True))) for p in b.get("pins") or []]
    if pa == pb:
        return {}
    na, nb = [p for p, _m in pa], [p for p, _m in pb]
    ma, mb = dict(pa), dict(pb)
    out = {"a": na, "b": nb,
           "added": [p for p in nb if p not in ma],
           "removed": [p for p in na if p not in mb],
           "modeled": {p: [ma[p], mb[p]] for p in nb if p in ma and ma[p] != mb[p]}}
    common_a = [p for p in na if p in mb]
    common_b = [p for p in nb if p in ma]
    out["order_changed"] = common_a != common_b
    return out


def _strip_provenance(text: str) -> str:
    """Drop the provenance header block at the top of a .va/.scs: it changes on every delivery
    (the date, the shas) and the diff reports provenance on its own."""
    lines = text.splitlines()
    rule = re.compile(r"^\s*(//|\*)\s-{20,}\s*$")
    if lines and rule.match(lines[0]):
        for i in range(1, len(lines)):
            if rule.match(lines[i]):
                return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def _text_diffs(a: "Deliverable", b: "Deliverable", changed) -> list[dict]:
    """A unified diff of every changed .scs / .va, provenance header left out, trimmed."""
    import difflib
    out, budget = [], DIFF_LINES_TOTAL
    for name in sorted(changed, key=lambda n: (not n.endswith(".scs"), n)):
        if not name.endswith((".scs", ".va")):
            continue
        ta, tb = _strip_provenance(a._text(name)), _strip_provenance(b._text(name))
        if ta == tb:
            out.append({"file": name, "diff": "", "added": 0, "removed": 0, "truncated": False,
                        "note": "only the provenance header changed"})
            continue
        lines = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(),
                                          fromfile=f"{a.stamp}/{name}", tofile=f"{b.stamp}/{name}",
                                          n=DIFF_CONTEXT, lineterm=""))
        added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        keep = max(0, min(DIFF_LINES_PER_FILE, budget))
        cut = len(lines) > keep
        shown = lines[:keep]
        if cut:
            shown.append(f"... {len(lines) - keep} more diff line(s) not shown -- open both "
                         f"files for the rest")
        budget -= min(len(lines), keep)
        out.append({"file": name, "diff": "\n".join(shown), "added": added, "removed": removed,
                    "truncated": cut})
    return out


def sha_text(text: str, n: int = 16) -> str:
    """sha256 of LF-normalized text -- the same number the box computes with sha256sum."""
    return hashlib.sha256(_lf(text).encode("utf-8")).hexdigest()[:n]
