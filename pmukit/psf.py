# from LDO_modeling/cadence/psf.py @ d2c5b80
"""PSF reader -- ascii and binary, one entry point.

`read_psf(path)` sniffs the leading bytes and dispatches: psfascii is parsed here, binary PSF in
`pmukit.binpsf`.  Both return the SAME plain dict::

    {"<sweep axis>": ndarray,        # under its REAL psf name: 'freq' | 'dc' | 'time' | 'temp'
     "<signal>":     ndarray,        # complex where the simulator stored a pair, else real
     ...,
     "_sweep":  "<sweep axis name>", # "" for an operating-point file, which has no axis
     "_header": {...},               # the PSF HEADER properties, verbatim
     "_types":  {"<signal>": "..."}, # each trace's declared PSF type name ('V', 'V/sqrt(Hz)')
     "_groups": ["..."]}             # trace names DROPPED because they are structs/groups

`_types` is load-bearing, not decoration: it is how the importer knows whether a noise total is
V/sqrt(Hz) or V^2/Hz, which differ by twelve orders of magnitude and are otherwise unguessable.

This module is a **firewall**: it returns raw saved signals and nothing else.  No ratio, no sign
convention, no unit conversion happens here -- `pmukit.importer` does all of that in Python, which
is what keeps a PSF-format surprise from silently changing a fitted parameter.

Scars this reader exists to survive (TOOL_FACTS "PSF / binpsf"):

  * ADE and the cluster write **binary** PSF; a text-only reader simply cannot read them.
  * The **dc-sweep axis is named `dc`** and the **transient axis `time`** -- not `sweep`, and not
    the swept device's name.  A temperature sweep is `temp`.  Callers get the axis from
    `sweep_axis()` instead of guessing.
  * A psfascii **operating-point** file has a third token per line (`"name" "V" 1.23`) and NO
    SWEEP section.  Parsing it as a sweep silently makes the first signal the axis.
  * Noise psfascii carries per-instance **group** values spanning several lines; they are consumed
    and reported under `_groups`, never left ragged in the dict.
"""
from __future__ import annotations

import pathlib
import re

import numpy as np

from . import binpsf
from .binpsf import GROUPS_KEY, HEADER_KEY, SWEEP_KEY, TYPES_KEY, is_binary
from .errors import PmuError

__all__ = ["read_psf", "read_psfascii", "sweep_axis", "signals", "find_psf",
           "SWEEP_KEY", "HEADER_KEY", "GROUPS_KEY", "TYPES_KEY", "is_binary", "AXIS_UNIT"]

#: The PSF sweep-axis names we expect, and what they mean.  Informational: nothing dispatches on
#: this table -- `sweep_axis()` reports whatever the file actually declared.
AXIS_UNIT = {"freq": "Hz", "dc": "sweep variable", "time": "s", "temp": "C"}

#: `"quoted name"` | `(` | `)` | a bare token.  Parens are their OWN tokens because Spectre glues
#: them onto the numbers (`(-1.2e-3 4e-4)`), and quoting is honoured so a signal name containing a
#: space survives (a plain whitespace split would tear it in two).
_TOK = re.compile(r'"[^"]*"|[()]|[^\s()"]+')

#: psfascii section keywords, each on its own line.
_SECTION = re.compile(r"^(HEADER|TYPE|SWEEP|TRACE|VALUE|END)\s*$", re.M)


def _err(path, what: str, why: str, do) -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=str(path))


def _sections(text: str) -> dict[str, str]:
    """Split a psfascii file into {SECTION: body text}, in file order."""
    marks = [(m.group(1), m.start(), m.end()) for m in _SECTION.finditer(text)]
    out: dict[str, str] = {}
    for i, (name, _s, e) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out.setdefault(name, text[e:stop])
    return out


def _header_props(body: str) -> dict:
    """HEADER section -> {name: value}.  Values are str / int / float, as written."""
    props: dict = {}
    for line in body.splitlines():
        toks = _TOK.findall(line.strip())
        if len(toks) < 2 or not toks[0].startswith('"'):
            continue
        name = toks[0].strip('"')
        raw = toks[1]
        if raw.startswith('"'):
            props[name] = raw.strip('"')
            continue
        try:
            f = float(raw)
        except ValueError:
            props[name] = raw
            continue
        props[name] = int(f) if f.is_integer() and "." not in raw and "e" not in raw.lower() else f
    return props


def _sweep_name(body: str) -> str:
    """SWEEP section -> the axis name.  `"freq" "sweep" PROP(` -> 'freq'."""
    for line in body.splitlines():
        toks = _TOK.findall(line.strip())
        if toks and toks[0].startswith('"'):
            return toks[0].strip('"')
    return ""


#: psfascii TYPE keyword -> how the VALUE entry must be read.
_KIND = {"FLOAT": "real", "INT": "real", "BYTE": "real", "DOUBLE": "real",
         "COMPLEX": "complex", "STRUCT": "struct", "ARRAY": "struct", "STRING": "string"}

_QUOTED = re.compile(r'"[^"]*"')


def _type_kinds(body: str) -> dict[str, str]:
    """TYPE section -> {type name: 'real' | 'complex' | 'struct' | 'string'}.

    Only TOP-LEVEL declarations count: a STRUCT body (`"plv" STRUCT( "rd" FLOAT ... )`) declares
    member names that must not shadow a real type.  Depth is tracked by counting parentheses with
    quoted strings masked out, because a unit string can contain one (`"V/sqrt(Hz)"`)."""
    kinds: dict[str, str] = {}
    depth = 0
    for line in body.splitlines():
        bare = _QUOTED.sub('""', line)
        if depth == 0:
            toks = _TOK.findall(line.strip())
            if len(toks) >= 2 and toks[0].startswith('"'):
                kw = toks[1].rstrip("(").upper()
                if kw in _KIND:
                    kinds[toks[0].strip('"')] = _KIND[kw]
        depth += bare.count("(") - bare.count(")")
        depth = max(depth, 0)
    return kinds


def _trace_types(trace_body: str) -> dict[str, str]:
    """TRACE section (`"<signal>" "<type name>"`) -> {signal: declared PSF type name}."""
    out: dict[str, str] = {}
    for line in trace_body.splitlines():
        toks = _TOK.findall(line.strip())
        if len(toks) >= 2 and toks[0].startswith('"') and toks[1].startswith('"'):
            out[toks[0].strip('"')] = toks[1].strip('"')
    return out


def _trace_kinds(types: dict[str, str], type_kinds: dict[str, str]) -> dict[str, str]:
    """{signal: type name} x {type name: kind} -> {signal: kind}.

    This is what keeps a 2-member noise STRUCT (a resistor's `rn`/`total` pair) from being read
    as a complex number by arity alone -- the exact trap the binary reader avoids by consulting
    the TYPE section, made available to the ascii path too."""
    return {sig: type_kinds[tname] for sig, tname in types.items() if tname in type_kinds}


def read_psfascii(path) -> dict:
    """Parse one psfascii file.  See the module docstring for the returned shape."""
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise _err(p, f"Could not read the PSF file {p.name}.",
                   f"{exc.__class__.__name__}: {exc}",
                   ["Check the path, and that the results came back from the run host."]) from exc
    sec = _sections(text)
    if "VALUE" not in sec:
        raise _err(p, f"{p.name} has no VALUE section.",
                   "Every psfascii result file carries HEADER / TYPE / SWEEP / TRACE / VALUE; "
                   "without VALUE the analysis wrote no data points.",
                   ["Read the simulator log for the real error (the run's spectre.log).",
                    "Confirm the analysis was not skipped (a failed DC solve writes no VALUE)."])

    body = sec["VALUE"]
    axis = _sweep_name(sec.get("SWEEP", ""))         # "" for an operating point (no SWEEP section)
    type_kinds = _type_kinds(sec.get("TYPE", ""))
    types = _trace_types(sec.get("TRACE", ""))
    kinds = _trace_kinds(types, type_kinds)
    if axis:
        kinds.setdefault(axis, "real")
    toks = _TOK.findall(body)

    cols: dict[str, list] = {}
    order: list[str] = []
    groups: list[str] = []
    n, k = len(toks), 0
    while k < n:
        tok = toks[k]
        if not tok.startswith('"'):                  # stray token (a trailing END was sliced off)
            k += 1
            continue
        key = tok.strip('"')
        k += 1
        # Operating-point lines carry a quoted TYPE tag between the name and the value
        # (`"VDD0P8_A" "V" 0.8`).  A swept line goes straight to the number or to `(`.
        if k < n and toks[k].startswith('"'):
            k += 1
        if k >= n:
            break
        kind = kinds.get(key)
        if toks[k] == "(":
            k += 1
            nums = []
            while k < n and toks[k] != ")":
                nums.append(float(toks[k]))
                k += 1
            k += 1                                   # consume ')'
            # The declared TYPE wins; arity is only the fallback for a trace the TRACE/TYPE
            # sections did not describe.  Guessing from arity alone reads a 2-member noise
            # STRUCT (a resistor's rn/total) as a complex number.
            if kind == "complex" or (kind is None and len(nums) == 2):
                val = complex(nums[0], nums[1]) if len(nums) == 2 else float("nan")
            elif (kind == "real" and len(nums) == 1) or (kind is None and len(nums) == 1):
                val = nums[0]
            else:                                    # a group / struct value -> not a column
                if key not in groups:
                    groups.append(key)
                cols.pop(key, None)
                if key in order:
                    order.remove(key)
                continue
        else:
            try:
                val = float(toks[k])
            except ValueError as exc:
                raise _err(p, f"{p.name}: could not read a value for signal {key!r}.",
                           f"The token after the signal name was {toks[k]!r}, which is not a "
                           "number and not the '(' of a complex or group value.",
                           ["Confirm the file is psfascii and complete (a killed job truncates "
                            "it mid-point).",
                            "If this is a format pmukit has not seen, keep the file: the reader "
                            "is deliberately strict rather than guessing."]) from exc
            k += 1
        if key in groups:                            # a group name never becomes a column
            continue
        if key not in cols:
            cols[key] = []
            order.append(key)
        cols[key].append(val)

    out: dict = {}
    for name in order:
        out[name] = np.asarray(cols[name])
    if not axis and order:
        # No SWEEP section: an operating point.  Every signal appears exactly once and there is
        # no axis -- say so rather than promoting the first signal to one.
        axis = ""
    out[SWEEP_KEY] = axis
    out[HEADER_KEY] = _header_props(sec.get("HEADER", ""))
    out[TYPES_KEY] = types
    out[GROUPS_KEY] = groups
    return out


def read_psf(path) -> dict:
    """Read a PSF file, ascii or binary.  The dispatch is on the leading bytes, never on the
    file name -- ADE writes `.ac` for both formats."""
    p = pathlib.Path(path)
    if not p.is_file():
        raise _err(p, f"No PSF file at {p}.",
                   "The importer was pointed at a result file that is not on this machine.",
                   ["Check the run's psf_path in the ledger.",
                    "For a remote engine, confirm the results were fetched back."])
    return binpsf.read_binpsf(p) if is_binary(p) else read_psfascii(p)


def sweep_axis(parsed: dict) -> tuple[str, np.ndarray]:
    """``(axis name, values)`` of a parsed PSF -- so no caller has to guess the name.

    The name is the file's own: `freq` for ac/noise, `dc` for a DC sweep (whatever device was
    swept), `time` for a transient, `temp` for a temperature sweep.  An operating-point file has
    no axis and raises, because a caller asking for one has mistaken the analysis."""
    name = str(parsed.get(SWEEP_KEY) or "")
    if not name:
        known = ", ".join(signals(parsed)[:8]) or "(none)"
        raise PmuError(
            what="This PSF has no sweep axis.",
            why="It carries no SWEEP section, which is how an operating-point (`dc` with no "
                "sweep) result is written -- its signals are single values, not a sweep.",
            do=["Read the operating point directly: parsed['<signal>'] is a length-1 array.",
                f"Signals in this file: {known}."],
            where="pmukit.psf.sweep_axis")
    arr = parsed.get(name)
    if arr is None:
        raise PmuError(
            what=f"This PSF declares the sweep axis {name!r} but stored no column for it.",
            why="The SWEEP section names the axis and the VALUE section must repeat it once per "
                "point; this file's VALUE section never mentions it.",
            do=["Re-export the analysis; the result file is incomplete.",
                f"Signals actually present: {', '.join(signals(parsed)[:8]) or '(none)'}."],
            where="pmukit.psf.sweep_axis")
    return name, np.asarray(arr)


def signals(parsed: dict) -> list[str]:
    """Signal names in the parsed dict, in file order, without the axis and the `_` metadata."""
    axis = str(parsed.get(SWEEP_KEY) or "")
    return [k for k in parsed if not k.startswith("_") and k != axis]


def find_psf(psf_dir, stem: str) -> pathlib.Path:
    """The result file an analysis named `stem` wrote, inside `psf_dir`.

    Spectre names it `<stem>.<type>` (`acz.ac`, `nz.noise`, `dcz.dc`) and a transient may come
    back as `trz.tran.tran`; ALPS under `-ade` uses ADE-style names.  Matching is on the first
    dot-separated component, so every one of those spellings resolves."""
    d = pathlib.Path(psf_dir)
    if not d.is_dir():
        raise _err(d, f"No PSF directory at {d}.",
                   "The run's results directory is missing -- the simulation produced nothing, "
                   "or the fetch back from the run host did not happen.",
                   ["Check the run's log for a simulator error.",
                    "For a remote engine, re-fetch the run."])
    hits = [f for f in sorted(d.iterdir())
            if f.is_file() and f.name.split(".", 1)[0] == stem and f.name != "logFile"]
    if not hits:
        names = ", ".join(f.name for f in sorted(d.iterdir()) if f.is_file()) or "(empty)"
        raise _err(d, f"No result file for analysis {stem!r} in {d}.",
                   "pmukit names every analysis it writes, and reads the result back by that "
                   "name; the simulator wrote no file for this one, which means the analysis "
                   "was skipped or it failed.",
                   [f"Files present: {names}.",
                    "Read the run's simulator log -- a failed DC solve skips the analyses "
                    "that depend on it."])
    return hits[0]
