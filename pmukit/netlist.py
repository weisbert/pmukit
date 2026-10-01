# from LDO_modeling/cadence/cluster/netlist_augment.py @ d2c5b80
"""Read and rewrite the user's Spectre testbench.

This is the interface between the user and pmukit (CONTRACTS.md section 0a).  The user hands over
ONE netlist, exported at the nominal corner, built to a naming convention.  We read the roles OUT
of it and write the per-run variations back IN.  Nothing else is asked of the user.

Ported verbatim from the old repo (the netlist text machinery -- do not rewrite it, only the
lookup it serves):

  * logical-line joining across backslash continuations, so a wrapped instance statement is
    never half-rewritten;
  * subckt depth tracking, so a pass device named `I1` inside the DUT can never shadow a
    top-level source named `I1`;
  * the instance parser `<name> (<nodes>) <master> <params>`;
  * analysis detection (the SECOND token is an analysis keyword and does not start with `(`),
    and stripping analyses by commenting every physical line with a visible marker;
  * the mag= / dc= / type=pwl in-place setters, which preserve indent and trailing comments.

New here:

  * **roles come from the source-name PREFIX, not a manifest.**  `IL_<pin>` = a voltage rail (and
    its dc is the testbench's typical load), `VB_<pin>` = a current bias (its dc is the pin's
    compliance voltage), `VS_<pin>` = a supply (its dc is nominal), `VEN_<pin>` = the enable.
    A pin with no matching source is reported as unclassifiable -- **we never guess a role.**
  * `include "<file>" section=<corner>` rewriting, which is how process corners are produced;
  * `parameters VSET=<n>` rewriting, which is how output codes are produced;
  * `options temp=<c>`, which is how temperature is set;
  * split grounds read from the wiring: a ground NET is `0`, the reference terminal of a
    convention source (`VS_AVDD (AVDD AGND)`), or a net shorted to one; the PMU pins on a ground
    net are its ground PINS -- unless the subcircuit shows a pin reaching only gates / logic,
    which is a control input the bench ties low (passed through). Each rail/bias is attached to
    the ground pin its source returns to, else the one nearest to it in the subcircuit's device
    graph.  When the subcircuit body is not in the netlist we say so instead of guessing.
  * near-zero impedances (`L<=10 fH`, `R<=1 uOhm`, 0 V vsources, iprobes) are shorts: the nets
    they join are one node for all of the above;
  * every mutation appends a recipe line (`~` edited in place with the old value in a comment,
    `+` added, `-` stripped) so contract 3's `recipe` column is a byproduct, not an afterthought.
"""
from __future__ import annotations

import bisect
import collections
import itertools
import operator
import os
import pathlib
import re
from dataclasses import dataclass, field

from . import jsonio
from .errors import PmuError
from .jsonio import sha_bytes

# ---------------------------------------------------------------- the naming convention (0a)
PREFIX_ROLE = {"IL_": "rail", "VB_": "bias", "VS_": "supply", "VEN_": "en"}
# The master each convention source must be.  A rail is READ as a voltage, so it is driven by a
# current source; a bias is READ as a current, so it is driven by a voltage source.
ROLE_MASTER = {"rail": "isource", "bias": "vsource", "supply": "vsource", "en": "vsource"}
ROLE_MEANING = {
    "rail": "voltage rail -- its IL_ source's dc is the testbench's typical load",
    "bias": "current bias -- its VB_ source's dc is the pin's compliance voltage",
    "supply": "supply -- its VS_ source's dc is the nominal supply",
    "en": "enable",
    "none": "no convention source on this pin",
}

STRIP_MARKER = "// [pmukit stripped analysis] "

# Conservative Spectre analysis keywords: a top-level statement whose SECOND token is one of
# these (and does not start with '(') is an analysis, and gets stripped.
ANALYSIS_KEYWORDS = frozenset({
    "tran", "dc", "ac", "noise", "dcmatch", "stb", "pz", "sp", "pss", "pac",
    "pnoise", "pstb", "hb", "hbac", "hbnoise", "envlp", "montecarlo", "sweep",
    "info", "xf", "pxf", "qpss", "qpac", "qpnoise", "qpsp", "qpxf", "tdr", "sens",
})

_SUFFIX = {"T": 1e12, "G": 1e9, "M": 1e6, "K": 1e3, "k": 1e3,
           "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}
_NUM = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([TGMKkmunpfa]?)(?:[A-Za-z]*)$")


def parse_number(tok: str) -> float | None:
    """Spectre/SPICE engineering number: `500u`, `2m`, `1.0`, `1e-9`, `1MEG`.

    Note the classic ambiguity: in Spectre-lang -- which is what we read -- bare `M` is mega and
    `m` is milli.  `MEG` is accepted as mega too.  Returns None when the token is not a number
    (an expression, a parameter reference), which every caller treats as "unknown", never as 0.
    """
    t = tok.strip()
    if not t:
        return None
    if t[-3:].upper() == "MEG":
        t = t[:-3] + "M"
    m = _NUM.match(t)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v * _SUFFIX.get(m.group(2), 1.0) if m.group(2) else v


def recorded_origin(copy_path) -> str:
    """The ORIGINAL netlist path recorded beside a project's copy of it, or ''.

    The web shell copies the user's netlist to `<project>/netlists/input.scs` and writes where
    it came from into `source.json` next to it. Whoever reads that copy (the CLI as well as the
    server) sets `Netlist.origin` from this, so a relative include keeps meaning what it meant
    next to the exported file."""
    meta = pathlib.Path(copy_path).parent / "source.json"
    if not meta.is_file():
        return ""
    try:
        d = jsonio.read(meta)
    except (OSError, ValueError):
        return ""
    return str(d.get("path") or "") if isinstance(d, dict) else ""


# ---------------------------------------------------------------------- base-netlist parsing
def _statement_tokens(line: str) -> list[str]:
    """Whitespace tokens of a netlist line, or [] when it is not an instance-ish statement."""
    s = line.strip()
    if not s:
        return []
    if s.startswith("//") or s.startswith("*") or s.startswith(";"):
        return []
    head = s.split(None, 1)[0]
    if head in ("simulator", "parameters", "global", "include", "ahdl_include", "save",
                "saveOptions", "subckt", "ends", "model", "options", "statistics", "library",
                "endlibrary", "section", "endsection", "if", "else"):
        return []
    return s.split()


def _continues(raw: str) -> bool:
    """A physical line continues when it ends with a backslash (ignoring a trailing comment)."""
    return raw.split("//", 1)[0].rstrip().endswith("\\")


def _logical_units(lines):
    """[(logical_text, [physical lines])] of a deck's physical lines -- an untouched statement
    re-emits byte-identical."""
    units, phys, parts = [], [], []
    for raw in lines:
        phys.append(raw)
        if _continues(raw):
            parts.append(raw.split("//", 1)[0].rstrip()[:-1].rstrip())
            continue
        parts.append(raw)
        units.append((" ".join(p.strip() for p in parts).strip(), phys))
        phys, parts = [], []
    if phys:
        units.append((" ".join(p.strip() for p in parts).strip(), phys))
    return units


def _logical_lines(text: str):
    """[(logical_text, [physical lines])] -- an untouched statement re-emits byte-identical."""
    return _logical_units(text.splitlines())


def _delta_of(toks: list[str]) -> int:
    """+1 a subckt header, -1 its `ends`, 0 anything else -- from the statement's first tokens."""
    if not toks:
        return 0
    first = toks[0].lower()
    if first in ("subckt", ".subckt"):
        return +1
    if first == "inline" and len(toks) >= 2 and toks[1].lower() == "subckt":
        return +1
    if first in ("ends", ".ends"):
        return -1
    return 0


def _subckt_delta(logical: str) -> int:
    return _delta_of(logical.split(None, 2))


def _scoped_units(lines, depth: int = 0) -> list[tuple]:
    """[(logical, physical lines, depth, depth after it, first token, the physical lines
    joined)] of a deck's physical lines, the subckt depth starting at `depth`. Depth 0 is top
    level; a header is reported at its OUTER depth (an `ends` at the depth it returns to)."""
    out = []
    for logical, phys in _logical_units(lines):
        toks = logical.split(None, 2)
        delta = _delta_of(toks)
        head = toks[0] if toks else ""
        text = phys[0] if len(phys) == 1 else "\n".join(phys)
        if delta < 0:
            depth = max(0, depth + delta)
            out.append((logical, phys, depth, depth, head, text))
        else:
            out.append((logical, phys, depth, depth + delta, head, text))
            depth += delta
    return out


_TEXT = operator.itemgetter(5)          # a statement's physical lines, joined


def _scoped_logical_lines(text: str):
    """(logical, physical, depth) -- depth 0 is top level; a header is reported at its OUTER depth."""
    for logical, phys, depth, *_ in _scoped_units(text.splitlines()):
        yield logical, phys, depth


#: What `str.splitlines` breaks a line at: a deck line holding one of these is not one line.
_LINE_BREAK = re.compile(r"[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
#: A `simulator lang=<x>` statement, read on its line (`append` switches back to spectre).
_LANG_LINE = re.compile(r"\s*simulator\s+lang\s*=\s*(\w+)")
_SPECTRE_LINE = re.compile(r"\s*simulator\s+lang\s*=\s*spectre\b")
#: The unread-statement count past which a deck's first-token index is rebuilt.
_INDEX_SLACK = 256
_UNKNOWN = object()
_RISKY = object()
_INCLUDE_HEADS = frozenset({"include", "ahdl_include"})


def _lang_of(line: str, last):
    """The deck's last `simulator lang=` language once `line` is read after `last`. A line
    starting `simulator` but not saying `lang=<x>` on itself is _RISKY: the regex `append` reads
    the joined text with could find the rest on the lines below."""
    if last is _RISKY or "simulator" not in line:
        return last
    m = _LANG_LINE.match(line)
    if m:
        return m.group(1)
    return _RISKY if line.lstrip().startswith("simulator") else last


def _parse_instance(logical: str):
    """(name, nodes, master, rest_tokens) or None when the statement is not an instance."""
    toks = _statement_tokens(logical)
    if len(toks) < 2 or not toks[1].startswith("("):
        return None
    name = toks[0]
    joined = " ".join(toks[1:])
    if ")" not in joined:
        return None
    node_blob, _, rest = joined.partition(")")
    nodes = node_blob.lstrip("(").split()
    rest_toks = rest.split()
    master = rest_toks[0] if rest_toks else ""
    return name, nodes, master, rest_toks


def _params_of(tokens: list[str]) -> dict[str, str]:
    """`dc=1.0 mag=1 type=pwl` -> {"dc": "1.0", ...}."""
    out = {}
    for t in tokens:
        if "=" in t:
            k, _, v = t.partition("=")
            out[k] = v
    return out


def _is_analysis_statement(logical: str) -> bool:
    toks = _statement_tokens(logical)
    if len(toks) < 2:
        return False
    second = toks[1]
    return (not second.startswith("(")) and second in ANALYSIS_KEYWORDS


# ------------------------------------------------------------- parameters: the code variable
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLAIN_NUM = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def param_followers(params: dict[str, str]) -> dict[str, list[str]]:
    """parameter -> every parameter whose expression depends on it, directly or through another
    (`B=0.0125*A+0.7`, `C=2*B` -> A: [B, C]), in declaration order. Only parameters that have
    followers are keys."""
    deps = {k: {t for t in _IDENT_RE.findall(str(v)) if t in params and t != k}
            for k, v in params.items()}
    out: dict[str, list[str]] = {}
    for root in params:
        seen: set[str] = set()
        frontier = {root}
        while frontier:
            nxt = {k for k, d in deps.items() if d & frontier and k not in seen and k != root}
            seen |= nxt
            frontier = nxt
        if seen:
            out[root] = [k for k in params if k in seen]
    return out


def code_variable(params: dict[str, str]) -> dict:
    """The design variable that selects the output code, as far as the parameters say.

    `VSET` when it is declared (the convention). Otherwise the ONE parameter whose name contains
    "vset" (any case) and whose value is a plain integer -- `CORE_VSET=10` -- is a SUGGESTION the
    user confirms; with none, or several, nothing is guessed and the user chooses.

    Returns {"param", "value", "suggested", "candidates"}: `param`/`value` are None when there
    is nothing to seed, `candidates` every parameter whose name contains "vset".
    """
    cands = [k for k in params if "vset" in k.lower()]

    def as_code(v):
        v = str(v).strip()
        if not _PLAIN_NUM.fullmatch(v):
            return None
        f = float(v)
        return int(f) if f == int(f) else None

    if "VSET" in params:
        return {"param": "VSET", "value": as_code(params["VSET"]), "suggested": False,
                "candidates": cands}
    plain = [k for k in cands if as_code(params[k]) is not None]
    if len(plain) == 1:
        return {"param": plain[0], "value": as_code(params[plain[0]]), "suggested": True,
                "candidates": cands}
    return {"param": None, "value": None, "suggested": False, "candidates": cands}


# ------------------------------------------------------ includes: which line is the corner
#: A section name reads as a PROCESS CORNER when one of its words (split on `_ - .`) is one of
#: these -- `tt`, `TOP_TT_X`, `tt_lib`, `mos_ss` -- or the whole name is one of _CORNER_NAMES
#: (the RC-extraction and long spellings). `Noise_Worst`, `pre_Sim` are none of them: those
#: are fixed model-library rows, kept as exported for every run.
_CORNER_WORDS = frozenset({"tt", "ss", "ff", "sf", "fs", "snfp", "fnsp", "tttt", "ssss", "ffff"})
_CORNER_NAMES = frozenset({"typ", "typical", "slow", "fast", "rcworst", "rcbest", "cworst",
                           "cbest", "rctyp", "rctypical", "typ_rc"})


#: A section DECLARATION in a model file: Spectre `section tt`, or spice-language `.LIB TOP_TT`
#: alone on its line (optionally followed by a `$` comment). `.lib 'file.l' TT` is a call.
_SECTION_DECL = re.compile(r"^[ \t]*(?:section[ \t]+([A-Za-z0-9_.+-]+)"
                           r"|(?i:\.lib)[ \t]+([A-Za-z0-9_.+-]+)[ \t]*(?:\$.*)?$)", re.MULTILINE)


def looks_like_corner(section: str | None) -> bool:
    s = str(section or "").strip().lower()
    return bool(s) and (s in _CORNER_NAMES
                        or any(w in _CORNER_WORDS for w in re.split(r"[_\-.]+", s)))


def _file_matches(path: str, pattern: str) -> bool:
    """`pattern` names the include `path`: the same, a suffix of it, or its basename."""
    return path == pattern or path.endswith(pattern) or pathlib.PurePosixPath(path).name == pattern


# ------------------------------------------------- ground pins vs control pins tied low
_GLOBAL_GROUNDS = ("0", "gnd!", "gnd")
#: A pin or subcircuit port named like a ground: VSS*, *GND*, PSUB/VSUB/SUB, substrate.
_GROUND_NAME = re.compile(r"(?i)(vss|gnd|psub|vsub|substrate|^sub(?:$|[_<\[\\]))")
#: One bit of a bus: `TRIM<3>` (escaped `TRIM\<3\>` in a Spectre netlist) or `TRIM[3]`.
_BUS_BIT = re.compile(r"(\\?<\d+\\?>|\[\d+\])$")
#: Last resort only, when the devices say nothing: a digital control's name.
_CONTROL_NAME = re.compile(r"(?i)(^d_|_en(?:_|$)|^en(?:_|$)|enable|_sel|^sel|trim|ctrl|ctl|"
                           r"test|reserve|_rsv|mode|cfg)")
_MOS_MASTER = re.compile(r"(?i)(^|_)[np](ch|mos|fet)|nmos|pmos|nfet|pfet")
_BJT_MASTER = re.compile(r"(?i)pnp|npn")
_STDCELL_MASTER = re.compile(r"(?i)^(inv|nand|nor|and|or|xor|xnor|buf|dff|dfr|lat|mux|aoi|oai|"
                             r"ao\d|oa\d|tie|dly|ckbd|ckin|sdf|sync)[a-z0-9_]*$")


#: Near-zero impedances ADE benches carry as placeholders (bondwire / package `L=0`, `L=1f`,
#: `R=0`): one node, not an element. 10 fH is ~0.6 mOhm at 10 GHz and 1 uOhm is below any wire,
#: both negligible against any LDO's Zout, so neither is a load, a decap path or a separate net.
SHORT_L_H = 1e-14
SHORT_R_OHM = 1e-6


def _value(tok: str | None, params: dict[str, str] | None = None, depth: int = 0):
    """A number, an engineering number, or a top-level parameter resolving to one; else None."""
    if tok is None:
        return None
    v = parse_number(tok)
    if v is None and params and tok.strip() in params and depth < 8:
        return _value(params[tok.strip()], params, depth + 1)
    return v


def _is_short(master: str, rest: list[str], params: dict[str, str] | None = None):
    """True when a two-terminal element is one node at DC and at every frequency pmukit looks
    at: an iprobe, an inductor of at most SHORT_L_H, a resistor of at most SHORT_R_OHM, a 0 V
    (or dc-less, non-transient) vsource. None when its `l=` / `r=` is an expression that does
    not evaluate (it is then NOT a short, and the caller says so); False otherwise."""
    kv = _params_of(rest[1:])
    if master == "iprobe":
        return True
    if master in ("inductor", "resistor"):
        key, limit = ("l", SHORT_L_H) if master == "inductor" else ("r", SHORT_R_OHM)
        if key not in kv:
            return False
        v = _value(kv[key], params)
        if v is None:
            return None
        return abs(v) <= limit * (1 + 1e-9)          # `10f` is 1.0000000000000002e-14
    if master == "vsource":
        if kv.get("type", "dc") != "dc" or "wave" in kv or "file" in kv:
            return False
        dc = kv.get("dc")
        return dc is None or _value(dc, params) == 0
    return False


def _closure(seeds, adj: dict[str, set[str]]) -> set[str]:
    out, stack = set(seeds), list(seeds)
    while stack:
        for n in adj.get(stack.pop(), ()):
            if n not in out:
                out.add(n)
                stack.append(n)
    return out


def _subckt_header(logical: str):
    """(name, ports) of a `subckt`/`.subckt`/`inline subckt` header line, or None."""
    s = logical.strip()
    toks = s.split()
    if len(toks) < 2:
        return None
    low = toks[0].lower()
    if low in ("subckt", ".subckt"):
        name, rest = toks[1], (s.split(None, 2)[2] if len(toks) > 2 else "")
    elif low == "inline" and len(toks) > 2 and toks[1].lower() == "subckt":
        name, rest = toks[2], (s.split(None, 3)[3] if len(toks) > 3 else "")
    else:
        return None
    if "(" in rest:
        return name, rest[rest.index("(") + 1:].split(")", 1)[0].split()
    ports = []                              # spice-style: nodes up to the first name=value
    for t in rest.split():
        if "=" in t:
            break
        ports.append(t)
    return name, ports


# ------------------------------------------------------------------------ in-place setters
def _set_kv_on_line(line: str, key: str, value: str) -> str:
    """Replace or append `key=value`, preserving the indent and any trailing comment."""
    body, sep, comment = line.partition("//")
    toks = body.split()
    for i, t in enumerate(toks):
        if t.startswith(key + "="):
            toks[i] = f"{key}={value}"
            break
    else:
        toks.append(f"{key}={value}")
    indent = line[:len(line) - len(line.lstrip())]
    gap = body[len(body.rstrip()):] or " "      # keep the author's spacing before the comment
    out = indent + " ".join(toks)
    return out + gap + sep + comment if sep else out


def _set_pwl_on_line(line: str, wave_tokens: str) -> str:
    """Turn a source into a PWL source: drop dc=/mag=, append type=pwl wave=[...]."""
    body, sep, comment = line.partition("//")
    toks = [t for t in body.split() if not (t.startswith("dc=") or t.startswith("mag="))]
    toks += ["type=pwl", f"wave=[{wave_tokens}]"]
    indent = line[:len(line) - len(line.lstrip())]
    gap = body[len(body.rstrip()):] or " "
    out = indent + " ".join(toks)
    return out + gap + sep + comment if sep else out


# ------------------------------------------------------------------------------- pin table
@dataclass
class Pin:
    """One pin of the PMU instance, as read from the testbench."""

    name: str
    net: str
    index: int
    role: str = "none"          # rail | bias | supply | en | none
    src: str | None = None      # the convention source instance
    src_master: str | None = None
    dc: float | None = None
    gnd: str | None = None      # the ground PIN this one returns to (split grounds)
    gnd_from: str = ""          # how the ground was determined
    is_ground: bool = False
    src_reversed: bool = False  # the convention source is wired (gnd pin) instead of (pin gnd)
    fate: str = "model"         # model | stub | ignore
    reason: str = ""            # why unclassifiable
    tied: str = ""              # a control input the bench ties to this ground net

    def to_dict(self) -> dict:
        return {"role": self.role, "net": self.net, "index": self.index, "gnd": self.gnd,
                "gnd_from": self.gnd_from, "src": self.src, "src_master": self.src_master,
                "src_reversed": self.src_reversed, "dc": self.dc, "fate": self.fate,
                "is_ground": self.is_ground, "reason": self.reason, "tied": self.tied}


@dataclass
class PinTable:
    """Everything the New screen shows and everything `config.derive()` needs."""

    pmu_inst: str
    pmu_master: str
    pins: dict[str, Pin] = field(default_factory=dict)
    #: include file -> the section of its FIRST include line: the process-corner row, the one
    #: `set_section_all` rewrites (ADE writes one line per Model Library row, corner first).
    sections: dict[str, str] = field(default_factory=dict)
    #: every include line in file order: {"file", "section", "kind", "corner"}; `corner` marks
    #: the line a corner rewrites, the others are left alone.
    includes: list[dict] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)     # top-level `parameters` variables
    #: parameter -> the parameters whose expressions follow it (transitively), file order.
    param_followers: dict[str, list[str]] = field(default_factory=dict)
    #: the output-code variable read from the parameters -- see `code_variable`.
    code_var: dict = field(default_factory=dict)
    analyses: list[str] = field(default_factory=list)        # the analyses that will be stripped
    notes: list[str] = field(default_factory=list)

    def of_role(self, role: str) -> list[Pin]:
        return [p for p in self.pins.values() if p.role == role]

    def unclassified(self) -> list[Pin]:
        """Pins with no convention source and not tied to ground. We report; we never guess."""
        return [p for p in self.pins.values()
                if p.role == "none" and not p.is_ground and not p.tied]

    def grounds(self) -> list[Pin]:
        return [p for p in self.pins.values() if p.is_ground]

    def to_dict(self) -> dict:
        """The plain dict `config.derive(pins=...)` consumes -- keeps the two modules decoupled."""
        return {name: p.to_dict() for name, p in self.pins.items()}

    def apply_fates(self, ports: dict[str, str]) -> None:
        """Stamp the config's model/stub/ignore decision onto the table."""
        for name, fate in (ports or {}).items():
            if name in self.pins:
                self.pins[name].fate = fate

    def require_classified(self) -> None:
        """Contract 0a: an unclassifiable pin is an error the user resolves, not a guess."""
        bad = sorted(self.unclassified(), key=lambda p: p.index)
        if not bad:
            return
        names = ", ".join(p.name for p in bad)
        first = bad[0]
        raise PmuError(
            what=f"{len(bad)} pin(s) of {self.pmu_inst} could not be classified: {names}.",
            why="A pin's role is read only from the name prefix of the source driving it "
                "(IL_ rail, VB_ bias, VS_ supply, VEN_ enable). These pins have no such source, "
                "and pmukit does not guess roles.",
            do=[f"Add a convention source, e.g. "
                f"`IL_{first.name} ({first.net} 0) isource dc=<typical load>`.",
                "Or mark the pin `ignore` on the New screen if it carries no role (a test pin).",
                "Right-click the pin on the New screen to assign a role and have pmukit insert "
                "the source for you."],
            where=f"pin list of instance {self.pmu_inst}")


# -------------------------------------------------------------------------------- the netlist
class Netlist:
    """A parsed, rewritable Spectre testbench. Every mutation records a recipe line."""

    def __init__(self, text: str, path: str | pathlib.Path | None = None):
        # The deck is read ONCE into its statements (`_units`), and every rewrite edits that
        # list in place: a run deck is the exported netlist with a handful of statements changed,
        # and re-reading a multi-MB bench per change made a plan O(runs x edits x netlist).
        # `text` is the list joined back -- byte for byte what the rewrites made it before.
        self._text: str | None = None   # the text, or None until it is asked for again
        self._u: list[tuple] | None = None   # _scoped_units of the text, None until read
        self._tail = ""                 # text = the joined lines + this ("" or "\n")
        self._joined: bool | None = None    # whether text is exactly that (None: not checked)
        self._idx: tuple | None = None  # (first token -> positions, top-level only, top-level)
        self._n = 0                     # the index holds positions [0, _n); later ones are read
        self._lang = _UNKNOWN           # the last `simulator lang=`, see `append`
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.path = str(path or "")
        #: Where the user's file really lives, when `path` is a copy of it (the web shell copies
        #: the netlist into the project). Relative `include` lines resolve against it first.
        self.origin = ""
        #: How an error names the file when it is neither a path nor a copy of one (a netlist
        #: dropped into the browser: "input.scs (dropped in the browser)").
        self.label = ""
        self.edits: list[str] = []
        self._inc_texts: dict[str, tuple[str, str] | None] = {}
        self._sidx_key: str | None = None    # the text the subckt index below was built from
        self._sidx: dict = {}
        #: config.corner_include: {file: index of its section= line that is the process corner}
        self.corner_choice: dict[str, int] = {}

    # ---- construction
    @classmethod
    def from_file(cls, path) -> "Netlist":
        p = pathlib.Path(path)
        if not p.exists():
            raise PmuError(what=f"netlist not found: {p}",
                           why="The project config points at a file that is not on this machine.",
                           do=["Check the path on the New screen.",
                               "Copy the exported netlist next to the project, then re-parse."],
                           where=str(p))
        return cls(p.read_text(encoding="utf-8", errors="replace"), p)

    def copy(self) -> "Netlist":
        """An independent deck with the same text and recipe. The statements read from the text
        (and their index) are shared, not read again: a plan copies its bench once per run."""
        n = Netlist("", self.path)
        if self._text is not None and "\r" in self._text:
            n.text = self._text.replace("\r\n", "\n").replace("\r", "\n")
        else:
            u = self._units()
            self._index()
            self._last_lang()
            n._text, n._tail, n._joined, n._lang = self._text, self._tail, self._joined, self._lang
            n._u, n._idx, n._n = list(u), self._idx, self._n
        n.origin = self.origin
        n.label = self.label
        n.edits = list(self.edits)
        n.corner_choice = dict(self.corner_choice)
        return n

    # ---- the deck as its statements
    @property
    def text(self) -> str:
        if self._text is None:
            self._text = "\n".join(map(_TEXT, self._u)) + self._tail
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        if value is self._text:
            return                               # the same text: what was read of it holds
        self._text, self._u, self._idx, self._n = value, None, None, 0
        self._tail, self._joined, self._lang = "", None, _UNKNOWN

    def _units(self) -> list[tuple]:
        """The statements of the text, read once (`_scoped_units`)."""
        if self._u is None:
            self._u = _scoped_units(self._text.splitlines())
            self._idx, self._n = None, 0
        return self._u

    def _of(self, text: str | None = None) -> list[tuple]:
        """The statements of `text`: this deck's own (read once) when None or its text."""
        if text is None or text is self._text:
            return self._units()
        return _scoped_units(text.splitlines())

    def _index(self) -> tuple:
        """({first token: positions}, {first token: top-level positions}, top-level positions),
        for the statements before `_n`. A copy shares it; a rewrite never changes it in place."""
        u = self._units()
        if self._idx is None or len(u) - self._n > _INDEX_SLACK:
            every: dict[str, list[int]] = {}
            top: dict[str, list[int]] = {}
            tops: list[int] = []
            for i, x in enumerate(u):
                every.setdefault(x[4], []).append(i)
                if x[2] == 0:
                    top.setdefault(x[4], []).append(i)
                    tops.append(i)
            self._idx, self._n = (every, top, tops), len(u)
        return self._idx

    def _find(self, heads=None, *, top: bool = True) -> list[int]:
        """Positions, in file order, of the statements whose first token is in `heads` (a set,
        or for top-level ones a test of it; None: every top-level statement) -- top-level ones
        only unless `top` is False. A superset of what a caller matches, never less."""
        every, topidx, tops = self._index()
        u, n = self._u, min(self._n, len(self._u))
        if heads is None:
            if not top:
                raise ValueError("_find(None) reads the top level only")
            pos = tops[:bisect.bisect_left(tops, n)]
        else:
            table = topidx if top else every
            keys = [h for h in table if heads(h)] if callable(heads) else heads
            pos = sorted(p for k in keys for p in table.get(k, ()) if p < n)
        for p in range(n, len(u)):                   # statements changed since the index
            x = u[p]
            if top and x[2] != 0:
                continue
            if heads is None or (heads(x[4]) if callable(heads) else x[4] in heads):
                pos.append(p)
        return pos

    def _is_joined(self) -> bool:
        """Is the text exactly the statements' physical lines joined by "\\n" (+ `_tail`)? Always
        after a rewrite; for a text as read, unless it holds a line break `str.splitlines` splits
        at and "\\n" does not (a form feed)."""
        if self._joined is None:
            u = self._units()
            joined = "\n".join(map(_TEXT, u))
            if u and self._text == joined:
                self._tail, self._joined = "", True
            elif u and self._text == joined + "\n":
                self._tail, self._joined = "\n", True
            else:
                self._joined = False
        return self._joined

    def _rejoin(self) -> None:
        """The text becomes its physical lines joined by "\\n" -- what every rewrite did to it."""
        self._units()
        if self._joined and self._tail == "":
            return
        self._text, self._tail, self._joined = None, "", True
        self._trim_tail()

    def _trim_tail(self) -> None:
        """A joined text ending in an empty line ends in "\\n": `splitlines` never reads that
        empty line back, so it is the tail, not a statement."""
        u = self._u
        if self._tail or not u or u[-1][1][-1] != "":
            return
        if len(u[-1][1]) > 1:                     # a wrapped statement ending in it: re-read
            self.text = "\n".join(map(_TEXT, u))
            self._is_joined()
            return
        u.pop()
        if u:
            self._tail = "\n"
        self._n = min(self._n, len(u))

    def _splice(self, a: int, b: int, lines: list[str], tail: str, *, trim: bool = True) -> None:
        """Statements a..b-1 become the ones on the physical `lines`, and the text after the last
        line becomes `tail`; the text is then exactly the lines joined, as the rewrites always
        made it. Read locally when the edit cannot reach past its own lines -- no line break in
        a line, no continuation into the next statement, the subckt depth after it unchanged --
        else the whole text is re-read."""
        u = self._units()
        depth_in = u[a - 1][3] if a else 0
        new = None
        if (not any(_LINE_BREAK.search(ln) for ln in lines)
                and not (a and _continues(u[a - 1][1][-1]))
                and not (lines and b < len(u) and _continues(lines[-1]))):
            new = _scoped_units(lines, depth_in)
            after = new[-1][3] if new else depth_in
            if b < len(u) and after != (u[b - 1][3] if b > a else depth_in):
                new = None
        if new is None:
            self.text = "\n".join([*map(_TEXT, u[:a]), *lines, *map(_TEXT, u[b:])]) + tail
            self._is_joined()
            return
        old = u[a:b]
        if len(new) != len(old) or any(x[4] != y[4] or x[2] != y[2] for x, y in zip(new, old)):
            self._n = min(self._n, a)             # positions from `a` on are read, not indexed
        if self._lang is not _UNKNOWN and any("simulator" in ln for x in old for ln in x[1]):
            self._lang = _UNKNOWN
        elif self._lang is not _UNKNOWN and any("simulator" in ln for ln in lines):
            if b == len(u) and self._lang is not _RISKY:   # appended: the last is the last now
                for ln in lines:
                    self._lang = _lang_of(ln, self._lang)
            else:
                self._lang = _UNKNOWN
        u[a:b] = new
        self._text, self._tail, self._joined = None, tail, True
        if trim:
            self._trim_tail()

    def _last_lang(self):
        """The language of the deck's last `simulator lang=` line, None without one -- what
        `append`'s regex over the joined text finds -- or _RISKY when a line starts a
        `simulator` statement that regex could read across lines."""
        if self._lang is _UNKNOWN:
            last = None
            for x in self._units():
                for ln in x[1]:
                    if "simulator" in ln:
                        last = _lang_of(ln, last)
                        if last is _RISKY:
                            break
                if last is _RISKY:
                    break
            self._lang = last
        return self._lang

    def sha(self, n: int = 12) -> str:
        return sha_bytes(self.render().encode("utf-8"), n)

    # ---- where an error points
    def where(self, line: int | None = None) -> str:
        """The USER's file -- the exported netlist, not pmukit's copy of it -- and, when the
        statement is known, `:<line>` in it (the copy is that file's text, so the lines agree)."""
        f = self.label or self.origin or self.path or "(netlist text)"
        return f"{f}:{line}" if line else f

    def line_of(self, name: str) -> int | None:
        """1-based line of the top-level instance statement `name` (its first physical line)."""
        n = 1
        for logical, phys, depth, *_ in self._units():
            if depth == 0:
                inst = _parse_instance(logical)
                if inst and inst[0] == name:
                    return n
            n += len(phys)
        return None

    # ---- reading
    def instances(self, depth: int | None = 0):
        u = self._units()
        for x in ([u[p] for p in self._find()] if depth == 0 else u[:]):
            if depth is not None and x[2] != depth:
                continue
            inst = _parse_instance(x[0])
            if inst:
                yield inst

    def find_instance(self, name: str, depth: int | None = 0):
        for inst in self.instances(depth):
            if inst[0] == name:
                return inst
        return None

    def top_nets(self) -> set[str]:
        nets = set()
        for _n, nodes, _m, _r in self.instances(0):
            nets.update(nodes)
        return nets

    def parameters(self) -> dict[str, str]:
        """Top-level `parameters a=1 b=2` declarations, merged in file order."""
        out: dict[str, str] = {}
        u = self._units()
        for p in self._find(lambda h: h.startswith("parameters")):
            logical = u[p][0]
            if not logical.strip().startswith("parameters"):
                continue
            for t in logical.split()[1:]:
                if "=" in t:
                    k, _, v = t.partition("=")
                    out[k] = v
        return out

    def _include_statements(self) -> list[str]:
        """The logical text of every statement (at any depth) that may be an include line."""
        u = self._units()
        return [u[p][0] for p in self._find(_INCLUDE_HEADS, top=False)]

    def includes(self) -> list[tuple[str, str | None]]:
        """[(file, section or None)] for every `include`/`ahdl_include` line, in file order."""
        out = []
        for logical in self._include_statements():
            s = logical.strip()
            if not (s.startswith("include ") or s.startswith("ahdl_include ")):
                continue
            m = re.search(r'["\']([^"\']+)["\']', s)
            if not m:
                continue
            sec = re.search(r"\bsection\s*=\s*([A-Za-z0-9_.+-]+)", s)
            out.append((m.group(1), sec.group(1) if sec else None))
        return out

    def corner_lines(self) -> dict[str, dict]:
        """Per include file with section= lines: which ONE of them is the process corner.

        ADE writes `include "toplevel.scs" section=<x>` once per Model Library row: the process
        corner, plus fixed rows (`pre_Sim`, `Noise_Worst`...) that are constants -- kept exactly as
        exported for every run, never offered or seeded as a corner. The corner line is, in order:
        the one `corner_choice` (config.corner_include) names; the one line whose section reads
        as a corner (`looks_like_corner`); none at all when no line of the file reads as a corner
        but another file's does (a lone `include "x.scs" section=pre_Sim` is a constant too);
        the only line; else the first, flagged unsure so the New screen asks the user to pick.

        {file: {"index", "section", "how": chosen|pattern|only|first|none, "sure", "sections"}}
        with `index` among that file's section= lines (None for `none`) and `sections` all of
        them in order. Only files with at least one section= line are keys."""
        by_file: dict[str, list[str]] = {}
        for f, s in self.includes():
            if s is not None:
                by_file.setdefault(f, []).append(s)
        some_corner = any(looks_like_corner(s) for secs in by_file.values() for s in secs)
        out: dict[str, dict] = {}
        for f, secs in by_file.items():
            pick = next((int(v) for k, v in self.corner_choice.items() if _file_matches(f, k)),
                        None)
            hits = [i for i, s in enumerate(secs) if looks_like_corner(s)]
            if pick is not None and 0 <= pick < len(secs):
                idx, how = pick, "chosen"
            elif len(hits) == 1:
                idx, how = hits[0], "pattern"
            elif not hits and some_corner:
                idx, how = None, "none"
            elif len(secs) == 1:
                idx, how = 0, "only"
            else:
                idx, how = 0, "first"
            out[f] = {"index": idx, "section": secs[idx] if idx is not None else None,
                      "how": how, "sure": how != "first", "sections": secs}
        return out

    def include_lines(self) -> list[dict]:
        """Every include line in file order: {"file", "section", "kind", "index" (among the
        file's section= lines, None without one), "corner" (the line a corner rewrites), "how",
        "sure" (see `corner_lines`)}. A sectioned line that is not the corner is a constant."""
        corners = self.corner_lines()
        out, count = [], collections.Counter()
        for logical in self._include_statements():
            s = logical.strip()
            if not (s.startswith("include ") or s.startswith("ahdl_include ")):
                continue
            m = re.search(r'["\']([^"\']+)["\']', s)
            if not m:
                continue
            sec = re.search(r"\bsection\s*=\s*([A-Za-z0-9_.+-]+)", s)
            f = m.group(1)
            idx = None
            if sec:
                idx = count[f]
                count[f] += 1
            c = corners.get(f) or {}
            out.append({"file": f, "section": sec.group(1) if sec else None,
                        "kind": s.split(None, 1)[0], "index": idx,
                        "corner": idx is not None and idx == c.get("index"),
                        "how": c.get("how"), "sure": c.get("sure", True)})
        return out

    def analyses(self) -> list[str]:
        u = self._units()
        return [u[p][0] for p in self._find() if _is_analysis_statement(u[p][0])]

    # ---- role scanning (the convention)
    def scan(self, pmu_inst: str, *, ports: dict[str, str] | None = None) -> PinTable:
        """Read the pin roles out of the testbench. Roles come ONLY from source-name prefixes."""
        inst = self.find_instance(pmu_inst)
        if inst is None:
            candidates = sorted({n for n, _nodes, m, _r in self.instances(0)
                                 if m not in ("isource", "vsource", "resistor", "capacitor",
                                              "inductor")})
            raise PmuError(
                what=f"no top-level instance named '{pmu_inst}' in the netlist.",
                why="The PMU instance is located by the name given in the project config; this "
                    "netlist has no top-level instance with that name.",
                do=[f"Set `pmu_inst` to one of: "
                    f"{', '.join(candidates[:8]) or '(no subcircuit instances found)'}.",
                    "Or rename the instance in your testbench to match the config."],
                where=self.where())
        _name, nodes, master, _rest = inst

        home = self.subckt_home(master)
        port_names = self._subckt_ports(master, home[0]) if home else None
        table = PinTable(pmu_inst=pmu_inst, pmu_master=master)
        if home and home[1]:
            table.notes.append(f"subcircuit '{master}' is read from the include {home[1]}")
        if port_names is None:
            table.notes.append(
                f"subcircuit '{master}' is not defined in this netlist (nor in an include "
                "pmukit could read) -- pin names fall back to "
                "the connected net names, and per-pin grounds cannot be read from the wiring")
            # Several pins can share a net (every ground tied to 0), so de-duplicate positionally
            # rather than let one pin silently swallow the others.
            port_names, seen = [], {}
            for i, net in enumerate(nodes):
                n = seen.get(net, 0)
                seen[net] = n + 1
                port_names.append(net if n == 0 else f"{net}#{i}")

        if len(port_names) != len(nodes):
            raise PmuError(
                what=f"instance {pmu_inst} connects {len(nodes)} nets but subcircuit '{master}' "
                     f"declares {len(port_names)} ports.",
                why="The instance node list and the subcircuit port list must be the same length; "
                    "otherwise every pin-to-net mapping is off by the difference.",
                do=["Re-export the netlist from ADE.",
                    f"Or fix the `{master}` port list / the `{pmu_inst}` instance line by hand."],
                where=f"{self.where(self.line_of(pmu_inst))}: instance {pmu_inst}")

        pins_on: dict[str, list[str]] = collections.defaultdict(list)
        for pin, net in zip(port_names, nodes):
            pins_on[net].append(pin)
        conv, ground_nodes, ground_why, cn = self._read_sources(pmu_inst, pins_on)
        rep = cn["rep"]
        owners: dict[str, list[dict]] = collections.defaultdict(list)
        for c in conv:
            owners[c["sig"]].append(c)

        reversed_pins: dict[str, list[str]] = collections.defaultdict(list)
        unnamed: dict[str, list[str]] = collections.defaultdict(list)
        on_ground: list[Pin] = []
        for i, (pin, net) in enumerate(zip(port_names, nodes)):
            p = Pin(name=pin, net=net, index=i)
            table.pins[pin] = p
            node = rep(net)
            if node in ground_nodes:
                # a ground pin, or a control input the bench ties low: told apart below
                on_ground.append(p)
                continue
            if not owners.get(node):
                p.reason = f"no source named IL_*/VB_*/VS_*/VEN_* drives net '{net}'"
                p.fate = "ignore"
                continue
            c = self._owner(owners[node], pin, net)
            role, src_name, src_master = c["role"], c["name"], c["master"]
            if src_master != ROLE_MASTER[role]:
                prefix = next(pre for pre, r in PREFIX_ROLE.items() if r == role)
                raise PmuError(
                    what=f"source '{src_name}' on net '{net}' is a {src_master}, but the "
                         f"'{prefix}' prefix means '{role}', which must be "
                         f"{'an' if ROLE_MASTER[role][0] in 'aeiou' else 'a'} "
                         f"{ROLE_MASTER[role]}.",
                    why="The read math depends on the master: a rail is read as a voltage "
                        "under a current injection (isource), a bias is read as a probe "
                        "current under a voltage drive (vsource).",
                    do=[f"Change '{src_name}' to "
                        f"{'an' if ROLE_MASTER[role][0] in 'aeiou' else 'a'} "
                        f"{ROLE_MASTER[role]}.",
                        f"Or rename it if it is not the {role} source for this pin."],
                    where=f"{self.where(self.line_of(src_name))}: instance {src_name}")
            p.role, p.src, p.src_master = role, src_name, src_master
            p.src_reversed = c["rev"]
            if p.src_reversed:
                reversed_pins[src_name].append(pin)
            if c["suffix"] not in {pin, net} | cn["members"].get(node, set()):
                unnamed[src_name].append(pin)
            p.dc = parse_number(_params_of(c["rest"][1:]).get("dc", ""))

        def _pins_text(names):
            return (f" -- {len(names)} pins ({', '.join(names[:6])}"
                    f"{', ...' if len(names) > 6 else ''})" if len(names) > 1 else "")

        by_name = {c["name"]: c for c in conv}
        for src_name, names in reversed_pins.items():
            c = by_name[src_name]
            ws, wr = c["wired_sig"], c["wired_ref"]
            table.notes.append(
                f"{src_name} is wired ({wr} {ws}), not ({ws} {wr}) -- "
                "the pin is classified, but its polarity is inverted relative to the "
                "convention; the importer detects the sign from the operating point"
                + _pins_text(names))
        for src_name, names in unnamed.items():
            table.notes.append(
                f"{src_name} gives {', '.join(names[:6])}{', ...' if len(names) > 6 else ''} "
                f"its role, but its name after the prefix names neither the pin nor its net "
                f"({by_name[src_name]['wired_sig']}) -- check it is the source meant for "
                f"{'them' if len(names) > 1 else 'it'}")
        if cn["listed"]:
            ex = cn["listed"][:6]
            table.notes.append(
                f"{len(cn['listed'])} zero-impedance element(s) (L <= 10 fH, R <= 1 uOhm, 0 V "
                f"vsources) treated as shorts: the nets they join are one node, e.g. "
                f"{', '.join(ex)}{', ...' if len(cn['listed']) > len(ex) else ''}")
        if cn["unresolved"]:
            ex = cn["unresolved"][:6]
            table.notes.append(
                f"{', '.join(ex)}{', ...' if len(cn['unresolved']) > len(ex) else ''}: the l= / "
                "r= value is an expression pmukit cannot evaluate, so "
                f"{'they are' if len(cn['unresolved']) > 1 else 'it is'} NOT treated as a short")

        self._split_ground_pins(table, on_ground, master, port_names if home else None,
                                {n: ground_why.get(rep(n), "a ground")
                                 for n in {p.net for p in on_ground}})
        self._check_rail_loads(table, pmu_inst, cn)
        returns = {p.name: by_name[p.src]["ref"] for p in table.pins.values()
                   if p.src in by_name}
        self._attach_grounds(table, master, home[0] if home else None, returns=returns,
                             node_of=rep)

        table.includes = self.include_lines()
        table.sections = {}
        for inc in table.includes:
            if inc["corner"]:
                table.sections[inc["file"]] = inc["section"]
        table.params = self.parameters()
        table.param_followers = param_followers(table.params)
        table.code_var = code_variable(table.params)
        table.analyses = self.analyses()
        table.notes = list(dict.fromkeys(table.notes))       # one line per thing noticed
        if ports:
            table.apply_fates(ports)
        return table

    # ---- the convention sources and the ground nets they reveal
    def _connectivity(self, pmu_inst: str, pin_nets: set[str]) -> dict:
        """The top-level nets merged into NODES across the near-zero impedances (`_is_short`).

        A pin's net joined to its IL_ source by an `L=0` bondwire placeholder is one node with
        the source's; a ground joined to 0 through `R=0` is 0. Returns {"rep": net -> node name
        (a global ground, else a PMU pin's net, else the alphabetically first), "members": node
        -> its nets, "shorts": {instance names}, "listed": the shorts worth naming (not
        iprobes), "unresolved": elements whose l=/r= did not evaluate, "tops": the top-level
        instances other than the PMU}."""
        params = self.parameters()
        tops = [t for t in self.instances(0) if t[0] != pmu_inst]
        parent: dict[str, str] = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        shorts, listed, unresolved = set(), [], []
        for name, nodes, master, rest in tops:
            if len(nodes) != 2 or any(name.startswith(pre) for pre in PREFIX_ROLE):
                continue                        # a convention source is never a short
            s = _is_short(master, rest, params)
            if s is None:
                unresolved.append(name)
            elif s:
                shorts.add(name)
                if master != "iprobe":
                    listed.append(name)
                for n in nodes:
                    parent.setdefault(n, n)
                a, b = find(nodes[0]), find(nodes[1])
                if a != b:
                    parent[b] = a
        groups: dict[str, set[str]] = collections.defaultdict(set)
        for n in list(parent):
            groups[find(n)].add(n)
        canon: dict[str, str] = {}
        members: dict[str, set[str]] = {}
        for grp in groups.values():
            head = min(grp, key=lambda n: (n not in _GLOBAL_GROUNDS, n not in pin_nets, n))
            members[head] = grp
            for n in grp:
                canon[n] = head
        return {"rep": lambda n: canon.get(n, n), "members": members, "shorts": shorts,
                "listed": listed, "unresolved": unresolved, "tops": tops}

    def _read_sources(self, pmu_inst: str, pins_on: dict[str, list[str]]):
        """(sources, ground nodes, {ground node: why}, connectivity) read from the top level of
        the bench, on the nodes `_connectivity` merges across shorts.

        A convention source is a signal node and a reference node. Its signal node is its FIRST
        node -- unless that first node is a ground (`VB_x (0 X)`, an easy thing to draw), or the
        name after the prefix names the second node's pin or net and not the first's; then the
        source is reversed and the second node is the signal. The reference node of a source
        whose signal is a PMU pin's net is a GROUND net of the bench (`VS_AVDD (AVDD AGND)`
        makes AGND one), as is anything tied to a ground by a short (a 0 ohm resistor, an
        iprobe, an inductor, a 0 V vsource). A net some convention source drives is never a
        ground, and neither is a net a non-zero vsource drives.
        """
        cn = self._connectivity(pmu_inst, set(pins_on))
        rep, tops = cn["rep"], cn["tops"]
        pin_nodes: dict[str, set[str]] = collections.defaultdict(set)   # node -> pins + nets
        for net, pins in pins_on.items():
            pin_nodes[rep(net)].update(pins)
            pin_nodes[rep(net)].add(net)
        driven: set[str] = set()
        raw = []
        for name, nodes, master, rest in tops:
            if len(nodes) < 2 or name in cn["shorts"]:
                continue
            role = next((r for pre, r in PREFIX_ROLE.items() if name.startswith(pre)), None)
            if role is not None and master in ("isource", "vsource"):
                prefix = next(pre for pre in PREFIX_ROLE if name.startswith(pre))
                raw.append({"name": name, "nodes": nodes, "master": master, "rest": rest,
                            "role": role, "suffix": name[len(prefix):]})
            elif master == "vsource":
                driven.update(rep(n) for n in nodes[:2])
        base = {rep(n) for n in _GLOBAL_GROUNDS}
        driven -= base
        why = {n: "the global ground" for n in base}

        def names(node, suffix):
            return suffix in pin_nodes.get(node, ()) or suffix in cn["members"].get(node, ())

        for c in raw:
            a, b = c["nodes"][:2]
            ra, rb = rep(a), rep(b)
            if ra in base and rb not in base:
                rev = True
            elif rb in base or ra in base:
                rev = False
            else:
                rev = names(rb, c["suffix"]) and not names(ra, c["suffix"])
            # sig/ref: the nodes as merged; wired_*: the nets as written on the source's line
            c["sig"], c["ref"], c["rev"] = (rb, ra, True) if rev else (ra, rb, False)
            c["wired_sig"], c["wired_ref"] = (b, a) if rev else (a, b)
        sig_nodes = {c["sig"] for c in raw}
        grounds = set(base)
        for c in raw:
            if c["sig"] in pin_nodes and c["ref"] not in sig_nodes | driven | base:
                grounds.add(c["ref"])
                why.setdefault(c["ref"], f"the return of {c['name']}")
        grounds -= sig_nodes - base
        return raw, grounds, why, cn

    def _owner(self, cands: list[dict], pin: str, net: str) -> dict:
        """The one source a pin takes its role from. Several: the one named after the pin, then
        after its net, then the one wired the normal way round; still several is an error."""
        if len(cands) == 1:
            return cands[0]
        for key in (pin, net):
            hit = [c for c in cands if c["suffix"] == key]
            if len(hit) == 1:
                return hit[0]
        normal = [c for c in cands if not c["rev"]]
        if len(normal) == 1:
            return normal[0]
        names = sorted(c["name"] for c in (normal or cands))
        raise PmuError(
            what=f"net '{net}' is driven by more than one convention source: "
                 f"{', '.join(names)}.",
            why="A pin's role is read from the ONE source the convention puts on it; with "
                "two, the role and the dc value are both ambiguous.",
            do=[f"Keep one of {', '.join(names)} and rename or remove the others.",
                f"Or name the one that is meant after the pin: e.g. "
                f"{cands[0]['name'][:len(cands[0]['name']) - len(cands[0]['suffix'])]}{pin}."],
            where=f"{self.where(self.line_of(names[0]))}: net {net}")

    # ---- ground pins vs control inputs tied low
    def _subckt_index(self) -> dict:
        """{master: (ports, [(name, nodes, master, rest)])} for every subcircuit this netlist
        and the includes it can read define -- the first definition wins. Built once per text."""
        if self._sidx_key is self.text:
            return self._sidx
        idx: dict = {}
        for text, _where in self._definition_texts():
            stack: list[str | None] = []
            for logical, _phys, _d, *_ in self._of(text):
                delta = _subckt_delta(logical)
                if delta > 0:
                    head = _subckt_header(logical)
                    name = head[0] if head else None
                    if name is not None and name not in idx:
                        idx[name] = (head[1], [])
                        stack.append(name)
                    else:
                        stack.append(None)          # a duplicate definition: ignored
                    continue
                if delta < 0:
                    if stack:
                        stack.pop()
                    continue
                if stack and stack[-1] is not None:
                    inst = _parse_instance(logical)
                    if inst:
                        idx[stack[-1]][1].append(inst)
        self._sidx_key, self._sidx = self.text, idx
        return idx

    def _terminals(self, idx: dict, master: str, net: str, memo: dict, busy: set) -> set[str]:
        """What `net` of subcircuit `master` reaches, through the hierarchy: `src_bulk` (a MOS
        source or bulk, a BJT collector or substrate), `gnd_port` (a child port named like a
        ground), `global_gnd` (shorted to 0 inside), `gate` (a MOS gate), `digital` (a
        standard cell's pin)."""
        key = (master, net)
        if key in memo:
            return memo[key]
        if key in busy or len(busy) > 64:
            return set()
        busy.add(key)
        ports, insts = idx[master]
        adj: dict[str, set[str]] = collections.defaultdict(set)
        for _n, nodes, m, rest in insts:
            if len(nodes) == 2 and _is_short(m, rest):
                adj[nodes[0]].add(nodes[1])
                adj[nodes[1]].add(nodes[0])
        group = _closure([net], adj)
        ev: set[str] = set()
        if group & set(_GLOBAL_GROUNDS):
            ev.add("global_gnd")
        for _n, nodes, m, rest in insts:
            for i, node in enumerate(nodes):
                if node not in group:
                    continue
                if m in idx:
                    cports = idx[m][0]
                    if i < len(cports):
                        if _GROUND_NAME.search(cports[i]):
                            ev.add("gnd_port")
                        ev |= self._terminals(idx, m, cports[i], memo, busy)
                elif (len(nodes) in (4, 5) and (_MOS_MASTER.search(m) or
                      {"w", "l"} <= set(_params_of(rest[1:])))) or \
                        (len(nodes) == 3 and _MOS_MASTER.search(m)):
                    ev.add("gate" if i == 1 else "src_bulk" if i >= 2 else "drain")
                elif _BJT_MASTER.search(m) and len(nodes) in (3, 4):
                    if i in (0, 3):
                        ev.add("src_bulk")
                elif _STDCELL_MASTER.match(m):
                    ev.add("digital")
        busy.discard(key)
        memo[key] = ev
        return ev

    def _split_ground_pins(self, table: PinTable, on_ground: list[Pin], master: str,
                           port_names: list[str] | None, ground_why: dict[str, str]) -> None:
        """Every PMU pin on a ground net of the bench is either one of the PMU's GROUND pins or a
        control input the bench ties low (a trim bit, an enable held off). The subcircuit says
        which: a ground reaches device sources and bulks (or a ground port, or 0); a control
        reaches only gates and standard cells, or is one bit of a bus. Without the subcircuit
        every pin on a ground net is taken as a ground, and the notes say so."""
        if not on_ground:
            return
        idx = self._subckt_index() if port_names is not None else {}
        memo: dict = {}
        tied: dict[str, list[str]] = collections.defaultdict(list)
        defaulted: list[str] = []
        for p in on_ground:
            p.role, p.fate = "none", "ignore"
            if master not in idx:
                verdict, why = "ground", f"on ground net {p.net}"
            else:
                ev = self._terminals(idx, master, p.name, memo, set())
                if ev & {"src_bulk", "gnd_port", "global_gnd"}:
                    verdict, why = "ground", f"reaches device sources/bulks inside {master}"
                elif _GROUND_NAME.search(p.name):
                    verdict, why = "ground", "named like a ground"
                elif ev & {"gate", "digital"}:
                    verdict, why = "control", "reaches only gates / logic cells"
                elif _BUS_BIT.search(p.name):
                    verdict, why = "control", "one bit of a control bus"
                elif _CONTROL_NAME.search(p.name):
                    verdict, why = "control", "named like a control input"
                else:
                    verdict, why = "ground", "nothing inside says otherwise"
                    defaulted.append(p.name)
            if verdict == "ground":
                p.is_ground = True
                p.gnd_from = f"ground pin: {why}"
            else:
                p.tied = p.net
                p.reason = (f"tied to {p.net} (a ground) in the bench: a control input, "
                            f"passed through")
                p.gnd_from = why
                tied[p.net].append(p.name)
        nets = sorted({p.net for p in on_ground if p.net not in _GLOBAL_GROUNDS})
        if nets:
            table.notes.append("ground nets read from the bench: " + ", ".join(
                f"{n} ({ground_why.get(n, 'a ground')})" for n in nets))
        for net, names in tied.items():
            table.notes.append(
                f"{len(names)} pin(s) tied to {net} (a ground) read as control inputs held low, "
                f"not ground pins -- passed through, not modeled: {', '.join(names[:8])}"
                f"{', ...' if len(names) > 8 else ''}")
        if master not in idx:
            names = [p.name for p in on_ground]
            if len(set(p.net for p in on_ground) - set(_GLOBAL_GROUNDS)):
                table.notes.append(
                    f"subcircuit '{master}' is not readable, so every pin on a ground net is "
                    f"taken as a ground pin ({', '.join(names[:8])}"
                    f"{', ...' if len(names) > 8 else ''}); a control input tied low among "
                    "them cannot be told apart")
        elif defaulted:
            table.notes.append(
                f"{', '.join(defaulted)}: on a ground net and nothing inside {master} says "
                "whether ground pin or control input -- taken as ground")

    def _check_rail_loads(self, table: PinTable, pmu_inst: str, cn: dict | None = None) -> None:
        """A rail is characterized INTRINSIC: nothing but its IL_ source may hang on its net.

        A decap on a rail is fitted INTO the model's Zout, and the designer then adds the same
        decap again in the system bench -- counted twice. That is refused. Anything else found
        there (a probe, a resistor) is named in the notes: it becomes part of what is measured.

        The rail is its merged NODE (`_connectivity`): a cap behind an `L=0` placeholder is still
        on the rail, and the placeholder itself is a short, not a load.
        """
        rep = cn["rep"] if cn else (lambda n: n)
        shorts = cn["shorts"] if cn else set()
        rails = {rep(p.net): p for p in table.pins.values() if p.role == "rail"}
        if not rails:
            return
        caps, others = [], []
        for name, nodes, master, _rest in self.instances(0):
            if name == pmu_inst or not nodes or name in shorts:
                continue
            hit = [rails[n] for n in dict.fromkeys(rep(x) for x in nodes) if n in rails]
            for p in hit:
                if name == p.src:
                    continue
                if master == "capacitor" or "cap" in master.lower():
                    caps.append((name, master, p))
                elif master != "iprobe":
                    others.append((name, master, p))
        if caps:
            raise PmuError(
                what="decap on a rail: " + ", ".join(
                    f"{n} ({m}) on rail {p.name} (net {p.net}, line {self.line_of(n)})"
                    for n, m, p in caps) + ".",
                why="Rails are characterized without any decap and the delivered model contains "
                    "none. A decap in this bench is fitted into the model's Zout, and the one in "
                    "your system bench then counts a second time.",
                do=[f"Remove {', '.join(n for n, _m, _p in caps)} from the bench and re-export; "
                    "put the decap in the system bench that uses the model."],
                where=f"{self.where(self.line_of(caps[0][0]))}: instance {caps[0][0]} on rail "
                      f"net(s) {', '.join(sorted({p.net for _n, _m, p in caps}))}")
        for n, m, p in others:
            table.notes.append(
                f"{n} ({m}) also hangs on rail {p.name} (net {p.net}): it is part of what gets "
                "characterized and fitted into the model; remove it unless that is intended")

    def _plain_includes(self) -> list[str]:
        """Top-level `include "<file>"` lines WITHOUT a section= -- the only includes that can
        hold the PMU's subckt. A section= include is a PDK model library (large, and never the
        DUT); an ahdl_include is Verilog-A."""
        out = []
        for logical in [self._u[p][0] for p in self._find(_INCLUDE_HEADS)]:
            s = logical.strip()
            if not s.startswith("include ") or re.search(r"\bsection\s*=", s):
                continue
            m = re.search(r'["\']([^"\']+)["\']', s)
            if m:
                out.append(m.group(1))
        return out

    def _definition_texts(self):
        """(text, where): the deck itself, then each plain include this machine can read.

        One level deep, read lazily and once per Netlist: scan only needs them when the PMU's
        master is not defined in the deck.
        """
        yield self.text, ""
        for f in self._plain_includes():
            if f not in self._inc_texts:
                hit = self._resolve_include(f)
                try:
                    self._inc_texts[f] = ((hit.read_text(encoding="utf-8", errors="replace"),
                                           str(hit)) if hit is not None else None)
                except OSError:
                    self._inc_texts[f] = None
            if self._inc_texts[f] is not None:
                yield self._inc_texts[f]

    def subckt_home(self, master: str) -> tuple[str, str] | None:
        """(text, include path or '') of the first file defining `subckt <master>`, or None."""
        for text, where in self._definition_texts():
            # the substring test keeps a large include from being parsed for every master
            if master in text and self._subckt_ports(master, text) is not None:
                return text, where
        return None

    def _subckt_ports(self, master: str, text: str | None = None) -> list[str] | None:
        """Port list of `subckt <master> (a b c)` / `subckt <master> a b c`, or None if absent."""
        for logical, _phys, _d, _after, head, _text in self._of(text):
            if head.lower() not in ("subckt", ".subckt", "inline"):
                continue
            s = logical.strip()
            toks = s.split()
            if len(toks) < 2:
                continue
            low = toks[0].lower()
            if low in ("subckt", ".subckt") and toks[1] == master:
                rest = s.split(None, 2)[2] if len(toks) > 2 else ""
            elif (low == "inline" and len(toks) > 2 and toks[1].lower() == "subckt"
                  and toks[2] == master):
                rest = s.split(None, 3)[3] if len(toks) > 3 else ""
            else:
                continue
            if "(" in rest:
                blob = rest[rest.index("(") + 1:].split(")", 1)[0]
                return blob.split()
            out = []                       # spice-style: nodes up to the first name=value
            for t in rest.split():
                if "=" in t:
                    break
                out.append(t)
            return out
        return None

    def _subckt_body(self, master: str, text: str | None = None) -> list[str]:
        """Logical statements inside `subckt <master> ... ends`."""
        body, inside, depth = [], False, 0
        for logical, _phys, _d, *_ in self._of(text):
            toks = logical.strip().split()
            if not toks:
                continue
            low = toks[0].lower()
            if not inside:
                if low in ("subckt", ".subckt") and len(toks) > 1 and toks[1] == master:
                    inside, depth = True, 1
                elif (low == "inline" and len(toks) > 2 and toks[1].lower() == "subckt"
                      and toks[2] == master):
                    inside, depth = True, 1
                continue
            d = _subckt_delta(logical)
            if d < 0:
                depth -= 1
                if depth == 0:
                    break
                continue
            depth += d
            body.append(logical)
        return body

    def _attach_grounds(self, table: PinTable, master: str, text: str | None = None, *,
                        returns: dict[str, str] | None = None, node_of=None) -> None:
        """Each rail/bias returns to the ground PIN nearest to it in the subcircuit's device graph.

        This is "read the ground from the wiring" (contract 0a) taken literally: build the node
        graph of the subcircuit body, breadth-first from every ground port at once, and attach
        each signal port to whichever ground reaches it first.  With one ground there is nothing
        to decide; with none, or with no subcircuit body to read, we say so rather than invent one.

        Only real ground pins take part (a control input the bench ties low is not one). When the
        pin's own convention source returns to a net that carries ground pins (`returns`: pin ->
        the source's reference net), the choice is among those: the bench already says where the
        rail's current comes back.
        """
        gnd_pins = sorted([p for p in table.pins.values() if p.is_ground], key=lambda p: p.index)
        signal = [p for p in table.pins.values() if p.role in ("rail", "bias")]
        if not signal:
            return
        if len(gnd_pins) == 1:
            for p in signal:
                p.gnd, p.gnd_from = gnd_pins[0].name, "the only ground pin"
            return
        if not gnd_pins:
            for p in signal:
                p.gnd_from = "no ground pin found on the instance"
            table.notes.append("no ground pin on the PMU instance -- the emitted model will use a "
                               "single global reference")
            return

        returns = returns or {}
        node_of = node_of or (lambda n: n)
        pools: dict[str, list[Pin]] = {}
        for p in signal:
            ret = returns.get(p.name)
            on_ret = [g for g in gnd_pins if ret is not None and node_of(g.net) == ret]
            if len(on_ret) == 1:
                p.gnd = on_ret[0].name
                p.gnd_from = f"the only ground pin on {ret}, where {p.src} returns in the bench"
            else:
                pools[p.name] = on_ret or gnd_pins
        if not pools:
            return

        body = self._subckt_body(master, text)
        if not body:
            for name in pools:
                table.pins[name].gnd_from = "subcircuit body not in this netlist"
            table.notes.append(
                f"{len(gnd_pins)} ground pins but subcircuit '{master}' is not defined here -- "
                "assign each rail's return pin on the New screen before delivering a split-ground "
                "model")
            return

        adj: dict[str, set[str]] = collections.defaultdict(set)
        for logical in body:
            inst = _parse_instance(logical)
            if not inst:
                continue
            for a in inst[1]:
                for b in inst[1]:
                    if a != b:
                        adj[a].add(b)

        def nearest(pool: list[Pin]) -> dict[str, tuple[int, str]]:
            dist: dict[str, tuple[int, str]] = {}
            queue: collections.deque = collections.deque()
            for g in pool:                      # multi-source BFS; first ground to arrive wins
                dist[g.name] = (0, g.name)
                queue.append(g.name)
            while queue:
                node = queue.popleft()
                d, owner = dist[node]
                for nxt in sorted(adj.get(node, ())):
                    if nxt not in dist:
                        dist[nxt] = (d + 1, owner)
                        queue.append(nxt)
            return dist

        done: dict[tuple, dict] = {}
        for name, pool in pools.items():
            key = tuple(g.name for g in pool)
            if key not in done:
                done[key] = nearest(pool)
            p = table.pins[name]
            hit = done[key].get(p.name)
            if hit:
                among = "" if pool is gnd_pins else f", among the ground pins on {pool[0].net}"
                p.gnd, p.gnd_from = hit[1], (f"nearest ground in the subcircuit graph "
                                             f"({hit[0]} hops{among})")
            else:
                p.gnd_from = "not reachable from any ground pin in the subcircuit graph"

    # ---------------------------------------------------------------------- rewriting
    def _rewrite_statement(self, match, transform, *, kind: str = "~", heads=None) -> bool:
        """Replace the first top-level logical statement for which `match(logical)` is true.

        True when a statement matched, whether or not the transform changed it: a statement that
        already says what is asked (`section=tt` asked of `section=tt`) is left byte-identical and
        records no recipe line -- the recipe is exactly the diff between the exported netlist and
        the run deck, never a list of no-ops.

        `heads` narrows the statements `match` is asked about to those whose first token is in
        it (a set, or a test of it) -- it must admit every statement `match` could accept.
        """
        u = self._units()
        for i in self._find(heads):
            logical, phys = u[i][0], u[i][1]
            if not match(logical):
                continue
            # A single-line statement is rewritten on the RAW line, so its indent and the exact
            # spacing before a trailing comment survive. A continued statement has no single raw
            # line to keep, so it collapses to one clean line (never a live dangling backslash).
            old = phys[0] if len(phys) == 1 else logical
            new = transform(old)
            if new == old or (len(phys) > 1 and new.strip() == logical.strip()):
                self._rejoin()
            else:
                self._record_edit(kind, new.strip(), logical.strip())
                self._splice(i, i + 1, [new], "")
            return True
        return False

    _WAS = "        // was: "

    def _record_edit(self, kind: str, new: str, was: str) -> None:
        """One recipe line per statement, from the ORIGINAL text to the final one.

        A statement edited twice (the corner's section=, then the include made absolute) is one
        line `~ <final> // was: <as exported>`, not two -- and when the second edit undoes the
        first, the line goes. A statement pmukit added itself (`+`) and then edits stays one `+`
        line carrying its final text.
        """
        prefix = f"{kind} {was}{self._WAS}"
        for i in range(len(self.edits) - 1, -1, -1):
            e = self.edits[i]
            if e.startswith(prefix):
                original = e[len(prefix):]
                if new == original:
                    del self.edits[i]
                else:
                    self.edits[i] = f"{kind} {new}{self._WAS}{original}"
                return
            if e == f"+ {was}":
                self.edits[i] = f"+ {new}"
                return
        self.edits.append(f"{kind} {new}{self._WAS}{was}")

    @staticmethod
    def _named_source(src_name: str):
        def match(logical):
            toks = _statement_tokens(logical)
            return len(toks) >= 2 and toks[0] == src_name and toks[1].startswith("(")
        return match

    def _require(self, found: bool, src_name: str, what: str) -> None:
        if found:
            return
        raise PmuError(
            what=f"could not {what}: no top-level instance named '{src_name}'.",
            why="The rewriter matches a source by name at the top level only, so a device of the "
                "same name inside a subcircuit can never be hit by accident.",
            do=[f"Check the convention source name in the testbench (expected '{src_name}').",
                "Re-parse the netlist on the New screen to refresh the pin table."],
            where=self.where())

    def set_mag(self, src_name: str, mag: str | float) -> "Netlist":
        """AC superposition: exactly one source is hot (mag=1), the rest stay 0."""
        ok = self._rewrite_statement(self._named_source(src_name),
                                     lambda lg: _set_kv_on_line(lg, "mag", f"{mag}"),
                                     heads={src_name})
        self._require(ok, src_name, f"set mag on '{src_name}'")
        return self

    def set_dc(self, src_name: str, value: float) -> "Netlist":
        ok = self._rewrite_statement(self._named_source(src_name),
                                     lambda lg: _set_kv_on_line(lg, "dc", f"{float(value):g}"),
                                     heads={src_name})
        self._require(ok, src_name, f"set dc on '{src_name}'")
        return self

    def set_pwl(self, src_name: str, wave_tokens: str) -> "Netlist":
        """Drive a source with a piecewise-linear wave (the load-EN and enable transients)."""
        ok = self._rewrite_statement(self._named_source(src_name),
                                     lambda lg: _set_pwl_on_line(lg, wave_tokens),
                                     heads={src_name})
        self._require(ok, src_name, f"make '{src_name}' a pwl source")
        return self

    def set_param(self, name: str, value) -> "Netlist":
        """Rewrite `parameters <name>=<value>` -- this is how VSET codes are produced."""
        pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*=\s*\S+")

        def match(logical):
            return logical.strip().startswith("parameters") and bool(pat.search(logical))

        ok = self._rewrite_statement(match, lambda lg: pat.sub(f"{name}={value}", lg, count=1),
                                     heads=lambda h: h.startswith("parameters"))
        if not ok:                           # not declared yet -- declare it rather than fail
            self._insert_near_top(f"parameters {name}={value}")
        return self

    def _insert_near_top(self, line: str) -> None:
        """Declare a statement near the top, but BELOW the header -- never as line 1.

        ADE netlists open with `// Generated for: ...` comments and `simulator lang=spectre`.
        A statement pushed above that is read as a SPICE title line (or in SPICE mode) by some
        engines; LDO_modeling only ever added lines behind a `simulator lang=spectre`, which is
        what ran on ALPS.  So: after the first top-level `simulator lang=spectre`; failing that,
        after the leading comment block; failing that, at the top.
        """
        if self._insert_line(line):
            self.edits.append(f"+ {line}")
            return
        lines = self.text.split("\n")
        at = None
        for i, raw in enumerate(lines):
            if re.match(r"\s*simulator\s+lang\s*=\s*spectre\b", raw):
                at = i + 1
                break
        if at is None:
            at = 0
            while at < len(lines) and (lines[at].lstrip().startswith(("//", "*"))
                                       or not lines[at].strip()):
                at += 1
        lines.insert(at, line)
        self.text = "\n".join(lines)
        self.edits.append(f"+ {line}")

    def _insert_line(self, line: str) -> bool:
        """`_insert_near_top` on the statements, without re-reading the deck: the same place
        (counted over `text.split("\\n")`), as one more statement. False -- nothing done -- when
        that place is not between two statements or the line would not stand alone."""
        if not self._is_joined() or _LINE_BREAK.search(line) or _continues(line):
            return False
        u = self._units()
        at = None
        i = 0
        for x in u:
            for raw in x[1]:
                i += 1
                if "simulator" in raw and _SPECTRE_LINE.match(raw):
                    at = i
                    break
            if at is not None:
                break
        if at is None:
            at, found = 0, False
            for x in u:
                for raw in x[1]:
                    if not (raw.lstrip().startswith(("//", "*")) or not raw.strip()):
                        found = True
                        break
                    at += 1
                if found:
                    break
            if not found:
                return False                     # comments to the end: the old way
        k = n = 0
        while k < len(u) and n < at:             # the statement the line goes in front of
            n += len(u[k][1])
            k += 1
        if n != at:
            return False
        self._splice(k, k, [line], self._tail)
        return True

    def set_section(self, file_pattern: str, section: str) -> "Netlist":
        """Rewrite `include "<file>" section=<x>` -- this is how process corners are produced.

        `file_pattern` matches the include's basename or any suffix of its path, so a config may
        say `toplevel.scs` for `include "/long/pdk/path/toplevel.scs"`.

        Of a file included several times, only its process-corner line (`corner_lines`) is
        rewritten; the other section= lines are constants and stay as exported.
        """
        sec_re = re.compile(r"(\bsection\s*=\s*)([A-Za-z0-9_.+-]+)")
        corners = self.corner_lines()
        target = (file_pattern if file_pattern in corners else
                  next((f for f in corners if _file_matches(f, file_pattern)), None))
        # a file with no corner line is rewritten only when named explicitly: its first line
        want = (corners[target]["index"] or 0) if target is not None else 0
        seen = [0]

        def match(logical):
            s = logical.strip()
            if not (s.startswith("include ") or s.startswith("ahdl_include ")):
                return False
            m = re.search(r'["\']([^"\']+)["\']', s)
            if not m or not sec_re.search(s):
                return False
            path = m.group(1)
            if not (path == target if target is not None else _file_matches(path, file_pattern)):
                return False
            seen[0] += 1
            return seen[0] - 1 == want

        ok = self._rewrite_statement(match, lambda lg: sec_re.sub(rf"\g<1>{section}", lg, count=1),
                                     heads=_INCLUDE_HEADS)
        if not ok:
            have = [f"{f} section={s}" for f, s in self.includes() if s]
            raise PmuError(
                what=f"no `include ... section=` line matching '{file_pattern}'.",
                why="Process corners are produced by rewriting the PDK include's section, so the "
                    "netlist must carry a section= on the include the corner names refer to.",
                do=[f"Include lines that do carry a section: {', '.join(have) or '(none)'}.",
                    "Add `section=<nominal>` to the PDK include in your testbench and re-export."],
                where=self.where())
        return self

    # Cache of {resolved include path: section names in file order}. A PDK toplevel is read once
    # per process, not once per planned run.
    _SECTION_CACHE: dict[str, list[str] | None] = {}

    def include_search_paths(self, file_path: str) -> list[pathlib.Path]:
        """Every place an include line's path is looked for, in order: relative to the ORIGINAL
        file first (a deck copied into the project still means the `include "models/x.scs"` next
        to where the user exported it), then this copy, the cwd, and the model root."""
        p = pathlib.Path(file_path)
        if p.is_absolute():
            return [p]
        bases = []
        if self.origin:
            bases.append(pathlib.Path(self.origin).resolve().parent)
        if self.path:
            bases.append(pathlib.Path(self.path).resolve().parent)
        bases.append(pathlib.Path.cwd())
        # ADE writes `include "toplevel.scs"` bare; the simulator finds it through `-I
        # <model root>/<simulator>`, so look there too (read-only: it only verifies sections).
        from . import sitenv
        pdk = sitenv.pdk_root().value
        if pdk:
            if pdk.lower().endswith(".scs"):       # the model FILE pasted as the root
                bases.append(pathlib.Path(pdk).parent)
            bases.append(pathlib.Path(pdk) / sitenv.simulator().value)
            bases.append(pathlib.Path(pdk))
        out: list[pathlib.Path] = []
        for b in bases:
            if b / p not in out:
                out.append(b / p)
        return out

    def _resolve_include(self, file_path: str) -> pathlib.Path | None:
        """Where an include line's path actually points (`include_search_paths`), or None."""
        return next((c for c in self.include_search_paths(file_path) if c.is_file()), None)

    # ---- relative includes in the run decks
    def _netlist_dirs(self) -> list[pathlib.Path]:
        """Where a relative include line was written relative to: the ORIGINAL netlist's
        directory, then this copy's. Never the cwd and never the PDK search path -- a bare
        `toplevel.scs` found through `-I` is the simulator's business, not a file of this deck.

        `abspath`, not `resolve`: a workarea reached through a symlink keeps the path the user
        typed, which is the one that also exists on the queue's compute nodes."""
        out: list[pathlib.Path] = []
        for p in (self.origin, self.path):
            if p:
                d = pathlib.Path(os.path.abspath(p)).parent
                if d not in out:
                    out.append(d)
        return out

    @staticmethod
    def _is_relative(file_path: str) -> bool:
        """`pdk/rc.scs`, `../m.scs`, `foo.va` -- not `/pdk/x.scs`, `C:/x`, `$PDK/x`, `~/x`.
        Both path flavours are asked, so a Linux path tested on Windows is still absolute."""
        f = str(file_path).strip()
        return bool(f) and not (f.startswith(("$", "~", "/", "\\"))
                                or pathlib.PurePosixPath(f).is_absolute()
                                or pathlib.PureWindowsPath(f).is_absolute())

    def local_include(self, file_path: str) -> pathlib.Path | None:
        """The file a RELATIVE include line names, next to the netlist; None when it is not there
        (or the line is absolute, which is left exactly as the user wrote it)."""
        if not self._is_relative(file_path):
            return None
        for base in self._netlist_dirs():
            hit = pathlib.Path(os.path.normpath(base / file_path))
            if hit.is_file():
                return hit
        return None

    def _top_includes(self) -> list[str]:
        """Every top-level `include`/`ahdl_include` path, in file order, each once."""
        out: list[str] = []
        for logical in [self._u[p][0] for p in self._find(_INCLUDE_HEADS)]:
            s = logical.strip()
            if not s.startswith(("include ", "ahdl_include ")):
                continue
            m = re.search(r'["\']([^"\']+)["\']', s)
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return out

    def absolutize_includes(self) -> list[str]:
        """Point every relative include that exists next to the netlist at its absolute path.

        A run's deck is written into its own run directory (`$WORK_ROOT/pmukit/<project>/runs/
        <id>/`), far from where the user exported the netlist, so `include "pdk/rc.scs"` would
        resolve against the wrong directory there. A bare name that is NOT next to the netlist
        (ADE's `include "toplevel.scs"`, found through `-I $MODEL_ROOT/alps`) and an absolute
        path are left untouched. Every include line naming the file is rewritten -- ADE writes
        `toplevel.scs` three times -- and each rewrite is a recipe line.

        Returns one "include <as written> -> <absolute>" per file, for the plan notes.
        """
        notes: list[str] = []
        for f in self._top_includes():
            hit = self.local_include(f)
            if hit is None:
                continue
            target = hit.as_posix()
            quoted = re.compile(r'(["\'])' + re.escape(f) + r"\1")

            def match(logical, quoted=quoted):
                s = logical.strip()
                return s.startswith(("include ", "ahdl_include ")) and bool(quoted.search(s))

            def swap(line, quoted=quoted, target=target):
                return quoted.sub(lambda m: f"{m.group(1)}{target}{m.group(1)}", line, count=1)

            n = 0
            while self._rewrite_statement(match, swap, heads=_INCLUDE_HEADS):
                n += 1
            if n:
                notes.append(f"include {f} -> {target}")
        return notes

    def include_trees(self) -> list[pathlib.Path]:
        """What a run directory needs copied beside its deck for the relative includes to keep
        resolving there: the top directory of each (`pdk/`, not one file of it), or the file.

        Only for a run that leaves this filesystem (spectre_ssh ships the run dir to another
        host, which cannot see this machine's paths); every other engine gets absolute includes
        instead. A `../` include cannot be made to resolve inside the run dir and is skipped.
        """
        out: list[pathlib.Path] = []
        for f in self._top_includes():
            hit = self.local_include(f)
            parts = pathlib.PurePosixPath(f.replace("\\", "/")).parts
            if hit is None or not parts or parts[0] == "..":
                continue
            top = hit
            for _ in parts[1:]:
                top = top.parent
            if top not in out:
                out.append(top)
        return out

    def section_names(self, file_path: str) -> set[str] | None:
        """The sections an included file declares, or None if unreadable (`section_list`)."""
        names = self.section_list(file_path)
        return set(names) if names is not None else None

    def section_list(self, file_path: str) -> list[str] | None:
        """The sections an included file declares, in file order, or None if unreadable.

        Both spellings a simulator accepts: Spectre's `section <name>` and, under `simulator
        lang=spice`, `.LIB <name>` ... `.ENDL` (any case). A `.lib '<file>' <name>` line with a
        file in front is a CALL of another file's section, not a declaration.

        None is not a failure: a PDK often lives behind a path this machine cannot see. The
        caller treats None as "cannot verify" and says so, rather than assuming either way.
        """
        resolved = self._resolve_include(file_path)
        if resolved is None:
            return None
        key = str(resolved)
        if key in Netlist._SECTION_CACHE:
            return Netlist._SECTION_CACHE[key]
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            Netlist._SECTION_CACHE[key] = None
            return None
        names: list[str] = []
        for m in _SECTION_DECL.finditer(text):
            name = m.group(1) or m.group(2)
            if name not in names:
                names.append(name)
        Netlist._SECTION_CACHE[key] = names or None
        return names or None

    def set_section_all(self, section: str) -> list[str]:
        """The simple-corner rewrite: point every include that HAS this section at it.

        CONTRACTS.md 0a says a simple corner name rewrites "every include line carrying a
        `section=`". Taken literally that also rewrites a second include whose sections are named
        differently (an RC skew file with typ/ss/ff has no `tt`), and Spectre then fails with
        "No section found with name 'tt'" -- which is how this was found. So: when the included
        file can be read, rewrite it only if it really declares that section; when it cannot be
        read, rewrite it (the contract's behaviour) and say the choice was unverified.

        One file, several include lines: ADE writes one `include "toplevel.scs" section=<x>` per
        row of the Model Library table (the corner, and fixed rows such as `pre_Sim`,
        `Noise_Worst`). Only one of those is the process corner (`corner_lines`: chosen on the
        New screen, else the one whose section reads as a corner, else the first) and only that
        one is rewritten; the others are constants, and the notes say they were kept.

        Returns the notes worth showing the user.
        """
        notes: list[str] = []
        applied: list[str] = []
        missing: list[tuple[str, set[str], str]] = []
        for file_path, c in self.corner_lines().items():
            current = c["section"]
            for k, other in enumerate(c["sections"]):
                if k != c["index"]:
                    notes.append(f"{file_path} section={other}: left as is -- a fixed "
                                 f"model-library section, kept as exported"
                                 + (f"; the process corner is its section={current} line"
                                    if current is not None else ""))
            if c["index"] is None:
                continue
            if not c["sure"]:
                notes.append(f"{file_path}: which of its {len(c['sections'])} section= lines is "
                             f"the process corner is not clear from the names "
                             f"({', '.join(c['sections'])}); the first is rewritten -- pick "
                             "the corner line on the New screen")
            names = self.section_names(file_path)
            if names is None:
                self.set_section(file_path, section)
                applied.append(f"{file_path}={section}")
                notes.append(f"{file_path}: rewrote section={current} -> {section} without being "
                             "able to read the file, so the section was not verified to exist")
            elif section in names:
                self.set_section(file_path, section)
                applied.append(f"{file_path}={section}")
            else:
                missing.append((file_path, names, current))
                notes.append(f"{file_path}: left at section={current}; it declares "
                             f"{{{', '.join(sorted(names))}}} and has no '{section}'. Use the "
                             "composite corner form to set it explicitly.")
        if missing and not applied:
            # No include took the corner: every run of it would simulate the exported corner
            # under this corner's name. A second include without the name (an RC file with
            # typ/ss/ff next to a toplevel with tt) is fine; NO include having it is not.
            f, names, was = missing[0]
            raise PmuError(
                what=f"corner '{section}' is not a section of {f}.",
                why=f"A corner name is the section= its process-corner include line is rewritten "
                    f"to; {f} declares {', '.join(self.section_list(f) or sorted(names))} and no "
                    f"'{section}', so its runs would simulate section={was} "
                    "under the name of another corner.",
                do=[f"Pick the corners from the sections {f} declares (New screen, corners).",
                    "If the name is right, check that this machine reads the same model file "
                    "the simulator does (pmukit site: pdk_root)."],
                where=f)
        if len(applied) > 1:
            notes.append(f"corner '{section}' set on {len(applied)} includes: "
                         f"{', '.join(applied)}")
        return notes

    def set_temperature(self, temp_c: float) -> "Netlist":
        """`options temp=<c>` -- replaced in place when present, otherwise added at the top."""
        pat = re.compile(r"(\btemp\s*=\s*)\S+")

        def match(logical):
            # Spectre spells it `<name> options temp=...`; spice-lang allows a bare `options`.
            toks = logical.strip().split()
            is_opts = bool(toks) and (toks[0] == "options"
                                      or (len(toks) > 1 and toks[1] == "options"))
            return is_opts and bool(pat.search(logical))

        ok = self._rewrite_statement(match, lambda lg: pat.sub(rf"\g<1>{temp_c:g}", lg, count=1))
        if not ok:
            self._insert_near_top(f"pmukit_opts options temp={temp_c:g}")
        return self

    def strip_analyses(self) -> "Netlist":
        """Comment out every top-level analysis. pmukit always writes its own."""
        u = self._units()
        hits = [p for p in self._find() if _is_analysis_statement(u[p][0])]
        stripped = []
        for p in hits:
            out = []
            for raw in u[p][1]:
                body = raw.rstrip()
                if body.endswith("\\"):
                    body = body[:-1].rstrip()      # neutralise the continuation
                out.append(STRIP_MARKER + body)
            stripped.append(out)
            self.edits.append(f"- {u[p][0].strip()}")
        if not hits:
            self._rejoin()
        elif any(u[p][2] != u[p][3] for p in hits):     # one opens a subckt: re-read it all
            done = dict(zip(hits, stripped))
            self.text = "\n".join(ln for p, x in enumerate(u) for ln in done.get(p, x[1]))
        else:
            for p, out in reversed(list(zip(hits, stripped))):   # back to front: positions hold
                self._splice(p, p + 1, out, "", trim=False)
            self._trim_tail()                     # once, as the one join before did
        return self

    def append(self, line: str) -> "Netlist":
        """Add one statement (an analysis, a save, an inserted source) and record it.

        If the netlist ends in another language (a `simulator lang=spice` section), switch back
        first -- the same guard LDO_modeling's appended block carried on the box."""
        if self._append_line(line):
            return self
        langs = re.findall(r"^\s*simulator\s+lang\s*=\s*(\w+)", self.text, re.MULTILINE)
        if langs and langs[-1].lower() != "spectre":
            self.text = self.text.rstrip("\n") + "\nsimulator lang=spectre"
            self.edits.append("+ simulator lang=spectre")
        self.text = self.text.rstrip("\n") + "\n" + line + "\n"
        self.edits.append(f"+ {line}")
        return self

    def _append_line(self, line: str) -> bool:
        """`append` on the statements, without re-reading the deck: the text loses its trailing
        newlines and gains the line(s) and one newline, exactly as before. False -- nothing done
        -- for a text that is not its joined lines, or a `simulator` line read differently
        across lines than on one."""
        if not self._is_joined() or _LINE_BREAK.search(line):
            return False
        lang = self._last_lang()
        if lang is _RISKY:
            return False
        u = self._units()
        k = len(u)
        while k and u[k - 1][1] == [""]:         # `rstrip("\n")`: the trailing empty lines go
            k -= 1
        if k and u[k - 1][1][-1] == "":
            return False
        new = [] if k else [""]
        if lang is not None and lang.lower() != "spectre":
            new.append("simulator lang=spectre")
            self.edits.append("+ simulator lang=spectre")
        new.append(line)
        self._splice(k, len(u), new, "\n")
        self.edits.append(f"+ {line}")
        return True

    def insert_role_source(self, pin: Pin, role: str, *, dc: float, ground: str = "0") -> str:
        """Give a role-less pin the convention source it is missing; return the source name.

        This is the New screen's right-click "assign a role": pmukit writes the source the
        convention requires, so the netlist stays the single source of truth for roles.
        """
        if role not in ROLE_MASTER:
            raise PmuError(what=f"unknown role '{role}'.",
                           why="A pin can only take one of the convention roles.",
                           do=[f"Use one of: {', '.join(sorted(ROLE_MASTER))}."],
                           where=f"pin {pin.name}")
        prefix = next(p for p, r in PREFIX_ROLE.items() if r == role)
        name = f"{prefix}{pin.name}"
        self.append(f"{name} ({pin.net} {ground}) {ROLE_MASTER[role]} dc={float(dc):g}")
        pin.role, pin.src, pin.src_master = role, name, ROLE_MASTER[role]
        pin.dc, pin.reason, pin.fate = float(dc), "", "model"
        return name

    # ---- output
    def render(self) -> str:
        """The netlist text, LF, exactly one trailing newline."""
        if self._text is None:                   # the joined statements: one join, no copies
            u = self._u
            k = len(u)
            while k and u[k - 1][1] == [""]:
                k -= 1
            if k and u[k - 1][1][-1] != "":
                return "\n".join(itertools.chain(map(_TEXT, u[:k]) if k < len(u)
                                                 else map(_TEXT, u), ("",)))
        return self.text.rstrip("\n") + "\n"

    def write(self, path) -> pathlib.Path:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.render(), encoding="utf-8", newline="\n")
        return p

    def recipe_edits(self) -> list[str]:
        """The `~ + -` edit lines for contract 3's recipe column, in the order applied."""
        return list(self.edits)
