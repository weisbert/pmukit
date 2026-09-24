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
  * split grounds read from the wiring: ground PINS are the PMU pins the testbench ties to `0`,
    and each rail/bias is attached to the ground pin nearest to it in the subcircuit's device
    graph.  When the subcircuit body is not in the netlist we say so instead of guessing.
  * every mutation appends a recipe line (`~` edited in place with the old value in a comment,
    `+` added, `-` stripped) so contract 3's `recipe` column is a byproduct, not an afterthought.
"""
from __future__ import annotations

import collections
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


def _logical_lines(text: str):
    """[(logical_text, [physical lines])] -- an untouched statement re-emits byte-identical."""
    units, phys, parts = [], [], []
    for raw in text.splitlines():
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


def _subckt_delta(logical: str) -> int:
    s = logical.strip()
    if not s:
        return 0
    toks = s.lower().split()
    first = toks[0]
    if first in ("subckt", ".subckt"):
        return +1
    if first == "inline" and len(toks) >= 2 and toks[1] == "subckt":
        return +1
    if first in ("ends", ".ends"):
        return -1
    return 0


def _scoped_logical_lines(text: str):
    """(logical, physical, depth) -- depth 0 is top level; a header is reported at its OUTER depth."""
    depth = 0
    for logical, phys in _logical_lines(text):
        delta = _subckt_delta(logical)
        if delta < 0:
            depth = max(0, depth + delta)
            yield logical, phys, depth
        else:
            yield logical, phys, depth
            depth += delta


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

    def to_dict(self) -> dict:
        return {"role": self.role, "net": self.net, "index": self.index, "gnd": self.gnd,
                "gnd_from": self.gnd_from, "src": self.src, "src_master": self.src_master,
                "src_reversed": self.src_reversed, "dc": self.dc, "fate": self.fate,
                "is_ground": self.is_ground, "reason": self.reason}


@dataclass
class PinTable:
    """Everything the New screen shows and everything `config.derive()` needs."""

    pmu_inst: str
    pmu_master: str
    pins: dict[str, Pin] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)   # include file -> current section
    params: dict[str, str] = field(default_factory=dict)     # top-level `parameters` variables
    analyses: list[str] = field(default_factory=list)        # the analyses that will be stripped
    notes: list[str] = field(default_factory=list)

    def of_role(self, role: str) -> list[Pin]:
        return [p for p in self.pins.values() if p.role == role]

    def unclassified(self) -> list[Pin]:
        """Pins with no convention source and not tied to ground. We report; we never guess."""
        return [p for p in self.pins.values() if p.role == "none" and not p.is_ground]

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
        n = Netlist(self.text, self.path)
        n.origin = self.origin
        n.label = self.label
        n.edits = list(self.edits)
        return n

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
        for logical, phys, depth in _scoped_logical_lines(self.text):
            if depth == 0:
                inst = _parse_instance(logical)
                if inst and inst[0] == name:
                    return n
            n += len(phys)
        return None

    # ---- reading
    def instances(self, depth: int | None = 0):
        for logical, _phys, d in _scoped_logical_lines(self.text):
            if depth is not None and d != depth:
                continue
            inst = _parse_instance(logical)
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
        for logical, _phys, d in _scoped_logical_lines(self.text):
            if d != 0 or not logical.strip().startswith("parameters"):
                continue
            for t in logical.split()[1:]:
                if "=" in t:
                    k, _, v = t.partition("=")
                    out[k] = v
        return out

    def includes(self) -> list[tuple[str, str | None]]:
        """[(file, section or None)] for every `include`/`ahdl_include` line, in file order."""
        out = []
        for logical, _phys, _d in _scoped_logical_lines(self.text):
            s = logical.strip()
            if not (s.startswith("include ") or s.startswith("ahdl_include ")):
                continue
            m = re.search(r'["\']([^"\']+)["\']', s)
            if not m:
                continue
            sec = re.search(r"\bsection\s*=\s*([A-Za-z0-9_.+-]+)", s)
            out.append((m.group(1), sec.group(1) if sec else None))
        return out

    def analyses(self) -> list[str]:
        return [lg for lg, _p, d in _scoped_logical_lines(self.text)
                if d == 0 and _is_analysis_statement(lg)]

    # ---- role scanning (the convention)
    def _sources_by_net(self) -> dict[str, list[tuple]]:
        """net -> [(name, nodes, master, rest, position)] for every source TOUCHING that net.

        `position` is the node index the net sits at. A convention source is normally written
        `IL_<pin> (<pin> 0)`, i.e. position 0; but `(0 <pin>)` is an easy thing for a person to
        draw, and silently failing to classify the pin would be worse than noting the polarity.
        """
        by_net: dict[str, list[tuple]] = collections.defaultdict(list)
        for name, nodes, master, rest in self.instances(0):
            if master not in ("isource", "vsource"):
                continue
            for pos, net in enumerate(nodes[:2]):
                by_net[net].append((name, nodes, master, rest, pos))
        return by_net

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

        by_net = self._sources_by_net()
        for i, (pin, net) in enumerate(zip(port_names, nodes)):
            p = Pin(name=pin, net=net, index=i)
            # ground pins: tied to the global 0 net in the testbench
            if net in ("0", "gnd!", "gnd"):
                p.is_ground = True
                p.role = "none"
                p.fate = "ignore"
                table.pins[pin] = p
                continue
            # role from the source-name prefix on this pin's net; a source wired the normal way
            # round (the pin is its first node) always wins over a reversed one.
            candidates = [c for c in by_net.get(net, [])
                          if any(c[0].startswith(pre) for pre in PREFIX_ROLE)]
            candidates.sort(key=lambda c: c[4])
            if len({c[0] for c in candidates if c[4] == 0}) > 1:
                names = sorted({c[0] for c in candidates if c[4] == 0})
                raise PmuError(
                    what=f"net '{net}' is driven by more than one convention source: "
                         f"{', '.join(names)}.",
                    why="A pin's role is read from the ONE source the convention puts on it; with "
                        "two, the role and the dc value are both ambiguous.",
                    do=[f"Keep one of {', '.join(names)} and rename or remove the others."],
                    where=f"{self.where(self.line_of(names[0]))}: net {net}")
            for src_name, _snodes, src_master, rest, pos in candidates:
                role = next((r for pre, r in PREFIX_ROLE.items() if src_name.startswith(pre)), None)
                if role is None:
                    continue
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
                p.src_reversed = pos != 0
                if p.src_reversed:
                    table.notes.append(
                        f"{src_name} is wired ({_snodes[0]} {net}), not ({net} {_snodes[0]}) -- "
                        "the pin is classified, but its polarity is inverted relative to the "
                        "convention; the importer detects the sign from the operating point")
                p.dc = parse_number(_params_of(rest[1:]).get("dc", ""))
                break
            if p.role == "none":
                p.reason = f"no source named IL_*/VB_*/VS_*/VEN_* drives net '{net}'"
                p.fate = "ignore"
            table.pins[pin] = p

        self._check_rail_loads(table, pmu_inst)
        self._attach_grounds(table, master, home[0] if home else None)

        table.sections = {f: s for f, s in self.includes() if s is not None}
        table.params = self.parameters()
        table.analyses = self.analyses()
        if ports:
            table.apply_fates(ports)
        return table

    def _check_rail_loads(self, table: PinTable, pmu_inst: str) -> None:
        """A rail is characterized INTRINSIC: nothing but its IL_ source may hang on its net.

        A decap on a rail is fitted INTO the model's Zout, and the designer then adds the same
        decap again in the system bench -- counted twice. That is refused. Anything else found
        there (a probe, a resistor) is named in the notes: it becomes part of what is measured.
        """
        rails = {p.net: p for p in table.pins.values() if p.role == "rail"}
        if not rails:
            return
        caps, others = [], []
        for name, nodes, master, _rest in self.instances(0):
            if name == pmu_inst or not nodes:
                continue
            hit = [rails[n] for n in dict.fromkeys(nodes) if n in rails]
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
        for logical, _phys, d in _scoped_logical_lines(self.text):
            s = logical.strip()
            if d != 0 or not s.startswith("include ") or re.search(r"\bsection\s*=", s):
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
        for logical, _phys, _d in _scoped_logical_lines(self.text if text is None else text):
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
        for logical, _phys, _d in _scoped_logical_lines(self.text if text is None else text):
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

    def _attach_grounds(self, table: PinTable, master: str, text: str | None = None) -> None:
        """Each rail/bias returns to the ground PIN nearest to it in the subcircuit's device graph.

        This is "read the ground from the wiring" (contract 0a) taken literally: build the node
        graph of the subcircuit body, breadth-first from every ground port at once, and attach
        each signal port to whichever ground reaches it first.  With one ground there is nothing
        to decide; with none, or with no subcircuit body to read, we say so rather than invent one.
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

        body = self._subckt_body(master, text)
        if not body:
            for p in signal:
                p.gnd_from = "subcircuit body not in this netlist"
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

        dist: dict[str, tuple[int, str]] = {}
        queue: collections.deque = collections.deque()
        for g in gnd_pins:                      # multi-source BFS; first ground to arrive wins
            dist[g.name] = (0, g.name)
            queue.append(g.name)
        while queue:
            node = queue.popleft()
            d, owner = dist[node]
            for nxt in sorted(adj.get(node, ())):
                if nxt not in dist:
                    dist[nxt] = (d + 1, owner)
                    queue.append(nxt)
        for p in signal:
            hit = dist.get(p.name)
            if hit:
                p.gnd, p.gnd_from = hit[1], f"nearest ground in the subcircuit graph ({hit[0]} hops)"
            else:
                p.gnd_from = "not reachable from any ground pin in the subcircuit graph"

    # ---------------------------------------------------------------------- rewriting
    def _rewrite_statement(self, match, transform, *, kind: str = "~") -> bool:
        """Replace the first top-level logical statement for which `match(logical)` is true.

        True when a statement matched, whether or not the transform changed it: a statement that
        already says what is asked (`section=tt` asked of `section=tt`) is left byte-identical and
        records no recipe line -- the recipe is exactly the diff between the exported netlist and
        the run deck, never a list of no-ops.
        """
        out, done = [], False
        for logical, phys, depth in _scoped_logical_lines(self.text):
            if not done and depth == 0 and match(logical):
                done = True
                # A single-line statement is rewritten on the RAW line, so its indent and the exact
                # spacing before a trailing comment survive. A continued statement has no single raw
                # line to keep, so it collapses to one clean line (never a live dangling backslash).
                old = phys[0] if len(phys) == 1 else logical
                new = transform(old)
                if new == old or (len(phys) > 1 and new.strip() == logical.strip()):
                    out.extend(phys)
                    continue
                out.append(new)
                self._record_edit(kind, new.strip(), logical.strip())
            else:
                out.extend(phys)
        if done:
            self.text = "\n".join(out)
        return done

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
                                     lambda lg: _set_kv_on_line(lg, "mag", f"{mag}"))
        self._require(ok, src_name, f"set mag on '{src_name}'")
        return self

    def set_dc(self, src_name: str, value: float) -> "Netlist":
        ok = self._rewrite_statement(self._named_source(src_name),
                                     lambda lg: _set_kv_on_line(lg, "dc", f"{float(value):g}"))
        self._require(ok, src_name, f"set dc on '{src_name}'")
        return self

    def set_pwl(self, src_name: str, wave_tokens: str) -> "Netlist":
        """Drive a source with a piecewise-linear wave (the load-EN and enable transients)."""
        ok = self._rewrite_statement(self._named_source(src_name),
                                     lambda lg: _set_pwl_on_line(lg, wave_tokens))
        self._require(ok, src_name, f"make '{src_name}' a pwl source")
        return self

    def set_param(self, name: str, value) -> "Netlist":
        """Rewrite `parameters <name>=<value>` -- this is how VSET codes are produced."""
        pat = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*=\s*\S+")

        def match(logical):
            return logical.strip().startswith("parameters") and bool(pat.search(logical))

        ok = self._rewrite_statement(match, lambda lg: pat.sub(f"{name}={value}", lg, count=1))
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

    def set_section(self, file_pattern: str, section: str) -> "Netlist":
        """Rewrite `include "<file>" section=<x>` -- this is how process corners are produced.

        `file_pattern` matches the include's basename or any suffix of its path, so a config may
        say `toplevel.scs` for `include "/long/pdk/path/toplevel.scs"`.
        """
        sec_re = re.compile(r"(\bsection\s*=\s*)([A-Za-z0-9_.+-]+)")

        def match(logical):
            s = logical.strip()
            if not (s.startswith("include ") or s.startswith("ahdl_include ")):
                return False
            m = re.search(r'["\']([^"\']+)["\']', s)
            if not m:
                return False
            path = m.group(1)
            if not (path.endswith(file_pattern)
                    or pathlib.PurePosixPath(path).name == file_pattern):
                return False
            return bool(sec_re.search(s))

        ok = self._rewrite_statement(match, lambda lg: sec_re.sub(rf"\g<1>{section}", lg, count=1))
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

    # Cache of {resolved include path: set of section names}. A PDK toplevel is read once per
    # process, not once per planned run.
    _SECTION_CACHE: dict[str, set[str] | None] = {}

    def _resolve_include(self, file_path: str) -> pathlib.Path | None:
        """Where an include line's path actually points, relative to this netlist.

        Relative to the ORIGINAL file first: a deck copied into the project still means the
        `include "models/x.scs"` next to where the user exported it.
        """
        p = pathlib.Path(file_path)
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
            bases.append(pathlib.Path(pdk) / sitenv.simulator().value)
            bases.append(pathlib.Path(pdk))
        for base in ([p] if p.is_absolute() else [b / p for b in bases]):
            if base.is_file():
                return base
        return None

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
        for logical, _phys, d in _scoped_logical_lines(self.text):
            s = logical.strip()
            if d != 0 or not s.startswith(("include ", "ahdl_include ")):
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
            while self._rewrite_statement(match, swap):
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
        """The `section <name>` declarations inside an included file, or None if unreadable.

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
        names = {m.group(1) for m in re.finditer(r"^\s*section\s+([A-Za-z0-9_.+-]+)", text,
                                                 re.MULTILINE)}
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
        row of the Model Library table (the corner, then e.g. `pre_Sim`, `Noise_Worst`). Only
        one of those is the process corner, and `set_section` rewrites the FIRST line naming the
        file -- so only the first occurrence is ever touched, and the notes say which lines were
        left alone (they used to claim every occurrence had been rewritten).  Keep the corner row
        first in the Model Library, or use the composite corner form.

        Returns the notes worth showing the user.
        """
        notes: list[str] = []
        applied: list[str] = []
        seen: dict[str, str] = {}
        for file_path, current in self.includes():
            if current is None:
                continue
            if file_path in seen:
                notes.append(f"{file_path} section={current}: left as is -- {file_path} is "
                             f"included more than once and only its FIRST include "
                             f"(section={seen[file_path]}) is treated as the process corner")
                continue
            seen[file_path] = current
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
                notes.append(f"{file_path}: left at section={current}; it declares "
                             f"{{{', '.join(sorted(names))}}} and has no '{section}'. Use the "
                             "composite corner form to set it explicitly.")
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
        out = []
        for logical, phys, depth in _scoped_logical_lines(self.text):
            if depth == 0 and _is_analysis_statement(logical):
                for raw in phys:
                    body = raw.rstrip()
                    if body.endswith("\\"):
                        body = body[:-1].rstrip()      # neutralise the continuation
                    out.append(STRIP_MARKER + body)
                self.edits.append(f"- {logical.strip()}")
            else:
                out.extend(phys)
        self.text = "\n".join(out)
        return self

    def append(self, line: str) -> "Netlist":
        """Add one statement (an analysis, a save, an inserted source) and record it.

        If the netlist ends in another language (a `simulator lang=spice` section), switch back
        first -- the same guard LDO_modeling's appended block carried on the box."""
        langs = re.findall(r"^\s*simulator\s+lang\s*=\s*(\w+)", self.text, re.MULTILINE)
        if langs and langs[-1].lower() != "spectre":
            self.text = self.text.rstrip("\n") + "\nsimulator lang=spectre"
            self.edits.append("+ simulator lang=spectre")
        self.text = self.text.rstrip("\n") + "\n" + line + "\n"
        self.edits.append(f"+ {line}")
        return self

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
        return self.text.rstrip("\n") + "\n"

    def write(self, path) -> pathlib.Path:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.render(), encoding="utf-8", newline="\n")
        return p

    def recipe_edits(self) -> list[str]:
        """The `~ + -` edit lines for contract 3's recipe column, in the order applied."""
        return list(self.edits)
