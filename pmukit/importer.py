# from LDO_modeling/cadence/import_cadence.py @ d2c5b80
"""Raw PSF in, contract-2 dataset out -- and every ratio computed HERE, in Python.

Two jobs.

**(a) pmukit's own results.**  `import_run(run, psf_dir, dataset)` takes a finished ledger row and
the directory the simulator wrote, derives each variable the run `reads`, and `put`s it into the
dataset at the right cell.  A cell that cannot be derived is `mark_missing`ed with the reason --
never silently skipped, never guessed.

**(b) results the user already has.**  `import_external(dirs, plan, ledger, dataset)` reads an
ADE or cluster result directory (the `input.scs` that produced it, plus its PSF), recovers the
corner / temperature / VSET / load state / excited source FROM THAT NETLIST, matches it against
the plan's cells and fills the dataset.  Every directory that does not match says why, in one
sentence.

The npz contract firewall (METHODOLOGY: "raw V / probe:p saved, all ratios computed in Python")
-------------------------------------------------------------------------------------------------
The simulator is asked only for raw saved signals: a node voltage, a probe branch current, a noise
total.  Zout, PSRR, Yout and gdd are DIVISIONS PERFORMED HERE.  That is not a stylistic choice: a
ratio computed inside a testbench hides its own sign and scale, and two of the worst bugs in the
predecessor were exactly that.  Both are encoded below as detection, not as constants:

  * **the injection direction is read off the netlist.**  `IL_<pin> (<rail> 0) isource` SINKS its
    current out of the rail, so a 1 A AC injection is -1 A INTO the pin and `Zout = -V(rail)`.
    Getting this wrong leaves |Z| perfect and the phase 180 degrees out across the whole band.
  * **source vs sink is read off the probe, never hardcoded.**  For `VB_<pin> (<pin> 0) vsource`
    the branch current is the current leaving the pin, so a reference that SOURCES into its pin
    reads `+VB:p` and one that SINKS reads `-VB:p`.  Hardcoding `sink` shipped once and emitted
    every current source with its sign flipped.

Sign and unit conventions of the stored variables, stated once
-------------------------------------------------------------
    ac_zout.<rail>      ohm     V(rail) / I_into_rail                      complex
    ac_psrr.<rail>      V/V     V(rail) / V(supply)                        complex
    ac_psrr.<bias>      A/V     d I_out(bias) / d V(supply)                complex
    ac_yout.<bias>      S       -d I_out(bias) / d V(pin)  (looking IN)    complex
    noise_v.<rail>      V^2/Hz  the noise total, squared if it came as V/sqrt(Hz)
    noise_i.<bias>      A^2/Hz  likewise
    dc_load.<rail>      V       V(rail) over the swept load current
    dc_iv.<bias>        A       I_out over the swept pin voltage (+ = sourcing)
    dc_temp.<rail>      V       V(rail) over the swept temperature
    dc_temp.<bias>      A       I_out over the swept temperature
    tran_*.<rail>       V       V(rail) over time
    tran_en.<bias>      A       I_out over time

`I_out` is the current the PMU delivers OUT of the pin: positive means the reference sources.
"""
from __future__ import annotations

import math
import pathlib
import re

import numpy as np

from . import psf as psfmod
from . import spec as modelspec
from .dataset import CELL_DIMS, Dataset, cell_key
from .errors import PmuError
from .ledger import Ledger, Run
from .netlist import PREFIX_ROLE, Netlist, parse_number

__all__ = ["import_run", "import_external", "import_csv", "mark_run_missing",
           "dims_from_plan", "open_or_create", "variable_spec", "TRAN_POINTS"]

#: A transient is resampled onto this many uniform points.  Contract 2 gives ONE coordinate per
#: variable, and a solver picks different time points at every corner, so the stored grid has to
#: be the tool's, not the solver's.  `import_run` reports the peak-preservation error it costs.
TRAN_POINTS = 2001

#: observable -> (trailing coordinate name, dtype, unit)
COORD = {
    "ac_zout": ("freq_hz", "complex128", "ohm"),
    "ac_psrr": ("freq_hz", "complex128", ""),          # V/V on a rail, A/V on a bias
    "ac_yout": ("freq_hz", "complex128", "S"),
    "noise_v": ("freq_hz", "float64", "V^2/Hz"),
    "noise_i": ("freq_hz", "float64", "A^2/Hz"),
    "dc_load": ("iload_a", "float64", "V"),
    "dc_iv": ("vpin_v", "float64", "A"),
    "dc_temp": ("temp_sweep_c", "float64", ""),        # V on a rail, A on a bias
    "tran_load_on": ("time_s", "float64", "V"),
    "tran_load_off": ("time_s", "float64", "V"),
    "tran_en": ("time_s", "float64", ""),              # V on a rail, A on a bias
}

#: ledger analysis bucket -> the netlist analysis KIND that produces it
ANALYSIS_KIND = {"ac": "ac", "noise": "noise", "dc_load": "dc", "dc_iv": "dc", "dc_temp": "dc",
                 "tran_load_on": "tran", "tran_load_off": "tran", "tran_en": "tran"}

_KIND_SUFFIX = {"ac": (".ac",), "noise": (".noise",), "dc": (".dc",),
                "tran": (".tran", ".tran.tran")}

_TEMP_RE = re.compile(r"(?:^|\s)(?:\w+\s+)?options\b[^\n]*?\btemp\s*=\s*([-+0-9.eE]+)")
_ANALYSIS_KINDS = ("ac", "noise", "dc", "tran")

_RTOL_AXIS = 1e-3          # snapping a measured axis value onto a declared dataset axis
_RTOL_COORD = 1e-6         # "the same sweep" when comparing two coordinate arrays


def _err(what: str, why: str, do, where: str = "") -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=where)


# =============================================================================== the deck
class Deck:
    """What one `input.scs` says about roles, cell and analyses -- the netlist as truth.

    Roles come ONLY from the source-name prefixes of contract 0a (`IL_`/`VB_`/`VS_`/`VEN_`); the
    net, the injection orientation and the DC level all come from the same statement, so the
    importer never needs a second manifest to agree with.

    PIN names, though, are the PMU subcircuit's port names, not the source names -- the plan's
    variables are `<observable>.<pin>`.  So the pin table is built the same way the New screen
    builds it, with `Netlist.scan(pmu_inst)`: pin -> net from the instance, net -> role from the
    source on it.  `pmu_inst` is passed in when the caller knows it (the project config does);
    otherwise the single top-level instance of a subcircuit DEFINED in this netlist is used, and
    failing even that the source-name suffix is taken as the pin name, which is what contract 0a
    asks for (`IL_<pin name>`).
    """

    def __init__(self, text: str, path: str = "", pmu_inst: str = ""):
        self.text = text
        self.path = str(path)
        self.netlist = Netlist(text, path)
        self.sources: dict[str, dict] = {}
        for name, nodes, master, rest in self.netlist.instances(0):
            if master not in ("isource", "vsource") or not nodes:
                continue
            params = dict(t.split("=", 1) for t in rest[1:] if "=" in t)
            role = next((r for pre, r in PREFIX_ROLE.items() if name.startswith(pre)), None)
            self.sources[name] = {"name": name, "nodes": list(nodes), "master": master,
                                  "role": role,
                                  "dc": parse_number(params.get("dc", "")),
                                  "mag": parse_number(params.get("mag", "")),
                                  "wave": _wave_of(" ".join(rest))}
        self.pmu_inst = pmu_inst or self._detect_pmu()
        self.by_pin, self.pin_net = self._pin_table()
        self.grounds = _ground_nets(self.sources)
        self.analyses = _analyses(text)
        self.params = self.netlist.parameters()
        self.sections = tuple(sorted((pathlib.PurePosixPath(f).name, s)
                                     for f, s in self.netlist.includes() if s))
        m = _TEMP_RE.search(text)
        self.temp_c = float(m.group(1)) if m else None
        vset = self.params.get("VSET")
        self.vset = int(float(vset)) if vset not in (None, "") and _isnum(vset) else None

    # -- the pin table -------------------------------------------------------
    def _detect_pmu(self) -> str:
        """The top-level instance of a subcircuit this netlist defines, when there is exactly one.

        Deliberately conservative: with none or several, pmukit says nothing and falls back
        rather than picking a DUT for the user.
        """
        hits = []
        for name, nodes, master, _rest in self.netlist.instances(0):
            if master in ("isource", "vsource", "resistor", "capacitor", "inductor", "bsource"):
                continue
            # pylint: disable=protected-access  -- the same lookup Netlist.scan uses
            if self.netlist._subckt_ports(master) is not None:
                hits.append((len(nodes), name))
        return hits[0][1] if len(hits) == 1 else ""

    def _pin_table(self) -> tuple[dict, dict]:
        """`({pin: source entry}, {pin: net})` -- pin names as the PMU subcircuit declares them."""
        by_pin: dict[str, dict] = {}
        pin_net: dict[str, str] = {}
        if self.pmu_inst:
            try:
                table = self.netlist.scan(self.pmu_inst)
            except PmuError:
                table = None
            if table is not None:
                for pin_name, pin in table.pins.items():
                    entry = self.sources.get(pin.src or "")
                    if pin.role in ("rail", "bias", "supply", "en") and entry:
                        by_pin[pin_name] = entry
                        pin_net[pin_name] = pin.net
                    elif not pin.is_ground and pin.net:
                        # `Netlist.scan` matches a convention source only by its FIRST node, so a
                        # source written the other way round (`VB_x (0 <pin>)`) leaves the pin
                        # unclassified.  The role is still unambiguous -- exactly one prefixed
                        # source touches the net -- and the reversed order is precisely what the
                        # orientation sign exists to carry, so it is picked up here rather than
                        # dropped.
                        hits = [e for e in self.sources.values()
                                if e.get("role") and pin.net in e["nodes"]]
                        if len(hits) == 1:
                            by_pin[pin_name] = hits[0]
                            pin_net[pin_name] = pin.net
        if by_pin:
            return by_pin, pin_net
        for name, entry in self.sources.items():      # contract 0a: IL_<pin name>
            if entry.get("role") and "_" in name:
                pin = name.split("_", 1)[1]
                by_pin[pin] = entry
                pin_net[pin] = entry["nodes"][0]
        return by_pin, pin_net

    # -- roles ---------------------------------------------------------------
    def role_of(self, port: str) -> str:
        e = self.by_pin.get(port)
        return (e or {}).get("role") or ""

    def port_type(self, port: str) -> str:
        """The `spec` port type of a pin: rail / bias / en."""
        role = self.role_of(port)
        return {"rail": "rail", "bias": "bias", "en": "en"}.get(role, "")

    def net_of(self, port: str, parsed: dict | None = None) -> str:
        """The pin's SIGNAL net: the node of its convention source that is not a ground.

        When a parsed PSF is to hand, a node that the simulator actually saved wins -- that is
        the strongest evidence of which end of the source the pin sits on.
        """
        e = self.by_pin.get(port)
        if not e:
            return port
        known = self.pin_net.get(port)
        if known and known in e["nodes"]:
            return known
        nodes = e["nodes"]
        if parsed:
            for nd in nodes:
                if nd in parsed:
                    return nd
        for nd in nodes:
            if nd not in self.grounds:
                return nd
        return nodes[0]

    def probe_of(self, port: str) -> str:
        """The saved probe name for a bias pin: `<VB source>:p`."""
        e = self.by_pin.get(port)
        return f"{e['name']}:p" if e else ""

    def orientation(self, port: str, parsed: dict | None = None) -> int:
        """+1 when the convention source's FIRST node is the pin's signal net, else -1.

        This one integer carries both scars.  For a rail it says which way an injected current
        flows.  For a bias it says which way the probe branch current runs: Spectre's branch
        current of `vsource (p n)` is positive flowing p -> n THROUGH the source, so with
        `VB_<pin> (<pin> 0)` the branch current is exactly the current leaving the pin -- a
        reference that SOURCES reads `+VB:p`, one that SINKS reads `-VB:p`.  Reading it off the
        netlist is what stops `sink` from being hardcoded (it was, once, for every bias).
        """
        e = self.by_pin.get(port)
        if not e:
            return 1
        return 1 if e["nodes"][0] == self.net_of(port, parsed) else -1

    def supply(self) -> dict | None:
        for e in self.sources.values():
            if e.get("role") == "supply":
                return e
        return None

    def hot(self) -> str:
        """The one excited source (`mag` set and non-zero), or "" when nothing is driven."""
        for name, e in self.sources.items():
            if e.get("mag") not in (None, 0.0):
                return name
        return ""

    def drives(self) -> tuple:
        """How every convention source is driven: `((name, dc, mag, pwl wave), ...)`, sorted.

        This IS the cell, operationally: the load state lives in the `IL_` dc values, the AC
        injection in the one non-zero `mag`, and the two load-step transients differ ONLY in
        their pwl wave.  Anything left out here lets two different simulations look like the
        same one -- and then one waveform lands in the other's cell.
        """
        return tuple(sorted(
            (e["name"],
             None if e["dc"] is None else float(e["dc"]),
             None if e["mag"] is None else float(e["mag"]),
             e.get("wave") or "")
            for e in self.sources.values() if e.get("role")))

    def analysis_of_kind(self, kind: str) -> list[dict]:
        return [a for a in self.analyses if a["kind"] == kind]

    def analysis_key_for(self, analysis: str) -> tuple | None:
        """The fingerprint of the ONE statement this deck runs for a ledger analysis bucket."""
        stmts = self.analysis_of_kind(ANALYSIS_KIND.get(analysis, ""))
        return analysis_key(stmts[0]) if len(stmts) == 1 else None

    def signature(self) -> dict:
        """Everything that identifies the CELL this deck simulates (not which analysis)."""
        return {"sections": self.sections, "temp_c": self.temp_c, "vset": self.vset,
                "drives": self.drives(), "hot": self.hot()}


_WAVE_RE = re.compile(r"\bwave\s*=\s*\[([^\]]*)\]")


def _wave_of(statement: str) -> str:
    """The pwl `wave=[...]` of a source statement, whitespace-normalised.

    It has to be read from the WHOLE statement, not from whitespace-split `k=v` tokens: the wave
    is a bracketed list with spaces in it, so a token split keeps only `wave=[0` -- and then a
    load-ON deck and a load-OFF deck look identical, which is exactly the confusion that would
    put one transient into the other's dataset cell.
    """
    m = _WAVE_RE.search(statement or "")
    return " ".join(m.group(1).split()) if m else ""


#: Nets that are ground by name alone, before any wiring is read.
_GLOBAL_GROUND = {"0", "gnd", "gnd!", "vss", "vss!"}


def _ground_nets(sources: dict) -> set:
    """Every net that behaves as a ground in this testbench.

    Starts from the global names and then follows the wiring: a 0 V `vsource` between a net and a
    known ground makes that net a ground too.  That is how the split-ground convention is written
    (`VGND_VSS_A (VSS_A 0) vsource dc=0`), and it is the only way to know which end of a
    convention source the PMU pin is on when the return is not the global 0.
    """
    grounds = set(_GLOBAL_GROUND)
    for _ in range(4):                     # a tie chain deeper than this is not a ground net
        grew = False
        for e in sources.values():
            if e["master"] != "vsource" or e.get("dc") not in (0.0, 0):
                continue
            a, b = (e["nodes"] + ["", ""])[:2]
            for x, y in ((a, b), (b, a)):
                if x.lower() in grounds and y and y.lower() not in grounds:
                    grounds.add(y.lower())
                    grew = True
        if not grew:
            break
    return _CaseSet(grounds)


class _CaseSet(frozenset):
    """A set of net names that compares case-insensitively (Spectre net names are not)."""

    def __contains__(self, item) -> bool:
        return frozenset.__contains__(self, str(item).lower())


def analysis_key(stmt: dict) -> tuple:
    """What identifies an analysis STATEMENT, independent of its name and its sweep range.

    Two DC sweeps in the same corner differ only by what they sweep (`dev=IL_A` vs `dev=IL_B`
    vs `param=temp`); two noise analyses differ by their output (a node pair vs an `oprobe`).
    Matching an external result directory to a planned run needs exactly that, and nothing more:
    the analysis NAME is ours, not the user's, and the start/stop/points are allowed to differ
    (the importer resamples).
    """
    return (stmt.get("kind", ""), stmt.get("dev", ""), stmt.get("param", ""),
            stmt.get("oprobe", ""), tuple(stmt.get("nodes") or ()))


def _isnum(text) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _analyses(text: str) -> list[dict]:
    """Top-level analysis statements: `[{"name", "kind", "nodes", <params>}]`.

    A stripped analysis is a comment, so it is invisible here -- which is the point: this reads
    what the deck will actually run, not what it once ran.
    """
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("//") or s.startswith("*") or s.startswith(";"):
            continue
        toks = s.split("//", 1)[0].split()
        if len(toks) < 2:
            continue
        rest = toks[1:]
        nodes: list[str] = []
        if rest[0].startswith("("):
            blob = []
            while rest:
                t = rest.pop(0)
                blob.append(t)
                if t.endswith(")"):
                    break
            nodes = " ".join(blob).strip("()").split()
        if not rest or rest[0] not in _ANALYSIS_KINDS:
            continue
        params = dict(t.split("=", 1) for t in rest[1:] if "=" in t)
        out.append({"name": toks[0], "kind": rest[0], "nodes": nodes, **params})
    return out


# =============================================================================== dataset shape
def variable_spec(observable: str, port_type: str) -> dict:
    """``{"dims", "coord", "dtype", "unit"}`` for one contract-2 variable.

    The axes come from `spec` (contract 1), translated into contract 2's storage order:

      * `temp_cont` is a MODEL axis, not a storage axis.  For every observable except `dc_temp`
        the plan runs one simulation per declared temperature, so it becomes the `temp_c` CELL
        dim.  For `dc_temp` -- which IS the continuous sweep -- it becomes the trailing
        COORDINATE `temp_sweep_c`, and `temp_c` is dropped because that run has no single
        temperature (its ledger `temp_c` is NaN).  This translation has to happen somewhere:
        `spec.AXES` lists `temp_cont` in the MIDDLE of the rail DC tuple, and `Dataset.declare`
        rejects a sweep coordinate that is not last, saying so.
      * everything else keeps the fixed order `process, temp_c, vset, load_a, <coordinate>`.
    """
    if observable not in COORD:
        raise _err(f"pmukit does not know how to store the observable {observable!r}.",
                   "Contract 2 names every variable `<observable>.<port>`, and each observable "
                   "has one trailing coordinate and one dtype; this name has neither.",
                   [f"Use one of: {', '.join(sorted(COORD))}."],
                   "pmukit/importer.py:COORD")
    if port_type not in modelspec.PORT_TYPES:
        raise _err(f"Cannot store {observable!r}: the port's role is unknown.",
                   "A variable's dimensions come from the model spec for its PORT TYPE (rail / "
                   "bias / en), and this pin has no convention source to read a role from.",
                   ["Give the pin its IL_/VB_/VEN_ source in the testbench (contract 0a), then "
                    "re-parse and re-plan."],
                   "pmukit/importer.py:variable_spec")
    # `tran_en` is measured ON a rail or a bias but BELONGS to the enable: the `ramp` block that
    # consumes it is an `en` block, so its axes come from there.  Only the unit follows the port.
    axes_from = "en" if observable == "tran_en" else port_type
    need: set[str] = set()
    for block in modelspec.blocks_for(axes_from):
        for param in block.params:
            if param.observable == observable:
                need.update(param.axes)
    coord, dtype, unit = COORD[observable]
    if observable == "dc_temp":
        need.discard("temp_c")
        need.discard("temp_cont")
    elif "temp_cont" in need:
        need.discard("temp_cont")
        need.add("temp_c")
    dims = tuple(d for d in CELL_DIMS if d in need) + (coord,)
    if not unit:
        unit = {"ac_psrr": {"rail": "V/V", "bias": "A/V"},
                "dc_temp": {"rail": "V", "bias": "A"},
                "tran_en": {"rail": "V", "bias": "A"}}.get(observable, {}).get(port_type, "")
    return {"dims": dims, "coord": coord, "dtype": dtype, "unit": unit}


def dims_from_plan(plan) -> dict:
    """The dataset axes a plan will fill: its corners, temperatures, VSET codes and load grids.

    Built from the plan rather than from the config so the hyper-rectangle is exactly the cells
    that will be simulated -- the dataset never declares an axis point nobody ran.
    """
    process: list[str] = []
    temps: list[float] = []
    vsets: list[int] = []
    for pr in plan.runs(enabled_only=False):
        run = pr.run
        if run.process and run.process not in process:
            process.append(run.process)
        t = float(run.temp_c)
        if t == t and not any(abs(t - x) <= 1e-9 * max(1.0, abs(x)) for x in temps):
            temps.append(t)
        if int(run.vset) not in vsets:
            vsets.append(int(run.vset))
    loads: dict[str, list[float]] = {}
    for state in getattr(plan, "states", []) or []:
        for rail, amps in (state.currents or {}).items():
            grid = loads.setdefault(str(rail), [])
            a = float(amps)
            if not any(abs(a - x) <= 1e-12 * max(1.0, abs(x)) for x in grid):
                grid.append(a)
    dims: dict = {"process": process or ["tt"],
                  "temp_c": sorted(temps) or [25.0],
                  "vset": sorted(vsets) or [0]}
    if loads:
        dims["load_a"] = {rail: sorted(grid) for rail, grid in loads.items() if grid}
    return dims


def open_or_create(path, plan, *, project: str, config_sha: str = "") -> Dataset:
    """Open the project's dataset, or create it from the plan's own axes."""
    p = pathlib.Path(path)
    if (p / "index.json").is_file():
        return Dataset.open(p)
    return Dataset.create(p, project=project, config_sha=config_sha, dims=dims_from_plan(plan))


# =============================================================================== psf lookup
def psf_for(psf_dir, analysis: str, deck: Deck, want: tuple | None = None
            ) -> tuple[pathlib.Path, dict]:
    """The result file and the analysis statement that produced it, for one ledger analysis.

    pmukit names the analyses it writes, so the file is normally `<name>.<type>`.  ALPS under
    `-ade` renames output ADE-style, so a unique file with the right TYPE suffix is accepted as
    the fallback -- and the fallback only fires when there is exactly one candidate, because two
    `ac` files with no name to tell them apart is an ambiguity, not a choice to make silently.
    """
    d = pathlib.Path(psf_dir)
    kind = ANALYSIS_KIND.get(analysis)
    if kind is None:
        raise _err(f"pmukit does not know which analysis writes {analysis!r}.",
                   "The ledger `analysis` column is a closed vocabulary (contract 3); this value "
                   "is outside it.",
                   [f"Use one of: {', '.join(sorted(ANALYSIS_KIND))}."], str(d))
    wanted = deck.analysis_of_kind(kind)
    if want is not None:
        exact = [a for a in wanted if analysis_key(a) == want]
        if exact:
            wanted = exact
    for an in wanted:
        try:
            return psfmod.find_psf(d, an["name"]), an
        except PmuError:
            continue
    if not d.is_dir():
        raise _err(f"No PSF directory at {d}.",
                   "The run wrote no results directory -- the simulation produced nothing, or "
                   "the fetch from the run host did not happen.",
                   ["Read the run's simulator log.", "Re-run, or re-fetch the run."], str(d))
    hits = [f for f in sorted(d.iterdir())
            if f.is_file() and f.name != "logFile"
            and any(f.name.endswith(s) for s in _KIND_SUFFIX[kind])]
    if len(hits) == 1:
        return hits[0], (wanted[0] if wanted else {"name": hits[0].name.split(".")[0],
                                                   "kind": kind})
    names = ", ".join(f.name for f in sorted(d.iterdir()) if f.is_file()) or "(empty)"
    if len(hits) > 1:
        raise _err(f"{d} holds {len(hits)} {kind} results and none carries the analysis name "
                   f"pmukit wrote.",
                   "The file is normally `<analysis name>.<type>`; with several unnamed "
                   f"candidates ({', '.join(f.name for f in hits)}) there is no way to tell which "
                   "one belongs to this cell, and guessing would silently mis-assign a corner.",
                   ["Keep one analysis per result directory.",
                    "Or import the files explicitly with import_csv(spec=...)."], str(d))
    raise _err(f"No {kind} result in {d}.",
               f"This run's analysis is {analysis!r}, which is a Spectre `{kind}` analysis; the "
               "simulator wrote no file of that type, so the analysis was skipped or it failed.",
               [f"Files present: {names}.",
                "Read the run's simulator log -- a failed DC solve skips everything after it."],
               str(d))


# =============================================================================== derivation
def _mag(entry: dict | None, default: float = 1.0) -> float:
    """The AC drive level of a source; 1 is the plan's convention when `mag` is absent."""
    if not entry:
        return default
    m = entry.get("mag")
    return float(m) if m not in (None, 0.0) else default


def _get(parsed: dict, name: str, where: str) -> np.ndarray:
    arr = parsed.get(name)
    if arr is None:
        have = ", ".join(psfmod.signals(parsed)[:12]) or "(none)"
        raise _err(f"The result file has no signal {name!r}.",
                   "Every run writes an explicit `save` line naming exactly what the importer "
                   "will read; this signal is not in the file, so the save and the read "
                   "disagree.",
                   [f"Signals present: {have}.",
                    "Currents need an explicit `save <source>:p` -- `allpub` does not include "
                    "them."], where)
    return np.asarray(arr)


def _noise_total(parsed: dict, where: str) -> np.ndarray:
    """The noise total as a POWER density, squaring it when the file says V/sqrt(Hz).

    Spectre can write either; the TRACE type name says which, so this is detected and not
    assumed.  An unrecognised unit is a hard error rather than a silent factor of 10^12.
    """
    out = _get(parsed, "out", where)
    unit = str((parsed.get("_types") or {}).get("out", "")) or \
        str((parsed.get("_header") or {}).get("noise unit", ""))
    low = unit.lower().replace(" ", "")
    if "sqrt" in low or "rthz" in low:
        return np.asarray(np.abs(out), dtype=float) ** 2
    if "^2" in low or "**2" in low:
        return np.asarray(np.abs(out), dtype=float)
    raise _err(f"The noise result does not say whether `out` is an amplitude or a power density "
               f"(its type reads {unit!r}).",
               "Contract 2 stores noise as a power density (V^2/Hz, A^2/Hz); Spectre writes "
               "either V/sqrt(Hz) or V^2/Hz and names the unit in the TRACE type, which is the "
               "only way to tell them apart -- they differ by twelve orders of magnitude.",
               ["Re-export the noise analysis with its units (the default psfascii carries them).",
                "Do not convert by hand: the whole point of the firewall is that pmukit sees the "
                "unit the simulator wrote."], where)


def _derive(var: str, parsed: dict, deck: Deck, run: Run, where: str
            ) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """``(coordinate, values, notes)`` for one contract-2 variable.  All ratios are taken here."""
    observable, port = modelspec.split_variable(var)
    axis_name, axis = psfmod.sweep_axis(parsed)
    notes: list[str] = []
    entry = deck.by_pin.get(port)
    if entry is None:
        raise _err(f"The deck has no convention source for port {port!r}.",
                   "Roles are read only from the source-name prefixes IL_/VB_/VS_/VEN_ "
                   "(contract 0a); without one, pmukit cannot tell what this pin is, and it does "
                   "not guess.",
                   [f"Add e.g. `IL_{port} (<net> 0) isource dc=<typical load>` to the testbench.",
                    "Then re-parse the netlist and re-plan."], where)
    net = deck.net_of(port, parsed)
    probe = deck.probe_of(port)
    orient = deck.orientation(port, parsed)

    if observable == "ac_zout":
        v = _get(parsed, net, where)
        drive = _mag(deck.sources.get(run.stimulus))
        # `IL_<pin> (<rail> 0) isource` SINKS its current out of the rail, so mag=1 is -1 A INTO
        # the pin: Zout = V / I_into = -V / mag.  Reading this off the netlist is what keeps a
        # reversed testbench from producing a perfect |Z| with the phase 180 degrees out.
        into = -drive if entry["nodes"][0] == net else drive
        z = v / into
        lf = axis <= axis.min() * 10.0
        if np.median(z[lf].real) < 0:
            notes.append(
                f"{var}: the low-frequency real part of Zout is negative "
                f"({np.median(z[lf].real):.3g} ohm). A stable rail's LF Zout is its (positive) "
                "load-regulation resistance, so the testbench injection direction is suspect. "
                "Stored as measured -- pmukit does not flip a sign it cannot justify.")
        return axis, z, notes

    if observable == "ac_psrr":
        sup = deck.supply()
        vsup, note = _supply_drive(parsed, sup, where)
        if note:
            notes.append(f"{var}: {note}")
        if deck.port_type(port) == "bias":
            i_out = orient * _get(parsed, probe, where)
            return axis, i_out / vsup, notes
        return axis, _get(parsed, net, where) / vsup, notes

    if observable == "ac_yout":
        vpin = _mag(deck.sources.get(run.stimulus))
        # The VB_ source IS the stimulus, so V(pin) = mag.  Yout looks INTO the pin, and the
        # probe reads the current coming OUT of it: Yout = -dI_out/dV_pin.
        return axis, -(orient * _get(parsed, probe, where)) / vpin, notes

    if observable in ("noise_v", "noise_i"):
        return axis, _noise_total(parsed, where), notes

    if observable == "dc_load":
        return axis, np.asarray(_get(parsed, net, where), dtype=float), notes

    if observable == "dc_iv":
        return axis, orient * np.asarray(_get(parsed, probe, where), dtype=float), notes

    if observable == "dc_temp":
        if deck.port_type(port) == "bias":
            return axis, orient * np.asarray(_get(parsed, probe, where), dtype=float), notes
        return axis, np.asarray(_get(parsed, net, where), dtype=float), notes

    if observable in ("tran_load_on", "tran_load_off", "tran_en"):
        if deck.port_type(port) == "bias":
            y = orient * np.asarray(_get(parsed, probe, where), dtype=float)
        else:
            y = np.asarray(_get(parsed, net, where), dtype=float)
        if axis_name != "time":
            notes.append(f"{var}: the transient axis is named {axis_name!r}, not 'time'.")
        return axis, y, notes

    raise _err(f"pmukit has no derivation for the observable {observable!r}.",
               "Every contract-2 variable is derived from raw saved signals by an explicit rule; "
               "this observable has none, so importing it would be a guess.",
               [f"Known observables: {', '.join(sorted(COORD))}."], where)


def _supply_drive(parsed: dict, sup: dict | None, where: str) -> tuple[np.ndarray | float, str]:
    """`V(supply)` for the PSRR denominator: the measured node if it was saved, else the drive.

    The supply is an ideal `vsource`, so its AC node voltage IS its `mag` -- but when the node
    was saved we divide by what the simulator actually reported, because that also catches a
    testbench where the supply is not ideal.
    """
    if sup is None:
        raise _err("The deck has no supply source, so PSRR has no denominator.",
                   "PSRR is V(rail)/V(supply) under a supply injection; the supply is the pin "
                   "driven by a `VS_` source (contract 0a) and there is none in this netlist.",
                   ["Add `VS_<supply pin> (<net> 0) vsource dc=<nominal>` to the testbench."],
                   where)
    net = sup["nodes"][0]
    if net in parsed:
        return np.asarray(parsed[net]), ""
    drive = _mag(sup)
    return drive, (f"the supply node {net!r} was not saved, so PSRR is referred to the injection "
                   f"level mag={drive:g} of {sup['name']} (an ideal vsource holds its node at "
                   "exactly that)")


# =============================================================================== coordinates
def _resample(x_new: np.ndarray, x_old: np.ndarray, y: np.ndarray, *, log_x: bool
              ) -> tuple[np.ndarray, str]:
    """Interpolate `y(x_old)` onto `x_new`; points outside the measured range come back NaN.

    Extrapolation is refused on purpose: a fitter cannot tell an extrapolated point from a
    measured one, and the whole dataset exists so that it can.
    """
    xo = np.asarray(x_old, dtype=float)
    xn = np.asarray(x_new, dtype=float)
    if log_x and np.all(xo > 0) and np.all(xn > 0):
        xo, xn = np.log(xo), np.log(xn)
    order = np.argsort(xo)
    xo, y = xo[order], np.asarray(y)[order]
    inside = (xn >= xo[0]) & (xn <= xo[-1])
    if np.iscomplexobj(y):
        out = np.full(xn.shape, np.nan + 1j * np.nan, dtype=complex)
        out[inside] = (np.interp(xn[inside], xo, y.real)
                       + 1j * np.interp(xn[inside], xo, y.imag))
    else:
        out = np.full(xn.shape, np.nan, dtype=float)
        out[inside] = np.interp(xn[inside], xo, np.asarray(y, dtype=float))
    n_out = int(np.count_nonzero(~inside))
    note = ""
    if n_out:
        note = (f"{n_out} of {xn.size} stored points are outside the measured range "
                f"[{np.exp(xo[0]) if log_x else xo[0]:.4g}, "
                f"{np.exp(xo[-1]) if log_x else xo[-1]:.4g}] and are stored as NaN "
                "(pmukit never extrapolates)")
    return out, note


def _target_coord(coord_name: str, axis: np.ndarray, analysis_stmt: dict) -> np.ndarray:
    """The coordinate a variable is STORED on.

    For a frequency, DC or temperature sweep that is the simulator's own axis: pmukit asked for
    those points, so they come back exactly.  A transient is different -- the solver picks its
    own time points and they differ at every corner, while contract 2 gives a variable ONE
    coordinate.  So a transient is stored on a uniform grid over the requested window.
    """
    if coord_name != "time_s":
        return np.asarray(axis, dtype=float)
    stop = axis[-1] if axis.size else 0.0
    try:
        stop = float(analysis_stmt.get("stop", stop))
    except (TypeError, ValueError):
        pass
    return np.linspace(0.0, float(stop), TRAN_POINTS)


# =============================================================================== the cell
def _cell_for(var: str, run: Run, deck: Deck, dataset: Dataset, plan=None
              ) -> tuple[dict, list[str]]:
    """The dataset cell one run fills for one variable, with every axis value snapped to the
    declared axis.  Raises when a value is not on an axis -- a cell nobody declared is a plan /
    dataset mismatch, not something to invent a slot for."""
    _obs, port = modelspec.split_variable(var)
    cell_dims = [d for d in CELL_DIMS if d in variable_spec(_obs, deck.port_type(port))["dims"]]
    notes: list[str] = []
    cell: dict = {}
    for dim in cell_dims:
        if dim == "process":
            cell["process"] = run.process
        elif dim == "temp_c":
            t = float(run.temp_c)
            if t != t:
                raise _err(f"{var}: this run has no single temperature.",
                           "Its ledger temp_c is NaN, which marks a run that SWEEPS temperature; "
                           f"but {var} is stored per discrete temperature.",
                           ["This is a plan/spec mismatch -- only dc_temp may sweep temperature."],
                           run.run_id)
            cell["temp_c"] = _snap(dataset.axis("temp_c"), t, "temp_c", var, notes)
        elif dim == "vset":
            cell["vset"] = _snap(dataset.axis("vset"), int(run.vset), "vset", var, notes)
        elif dim == "load_a":
            amps = _load_of(run, port, deck, plan)
            cell["load_a"] = _snap(dataset.axis("load_a", port), amps, "load_a", var, notes)
    return cell, notes


def _load_of(run: Run, port: str, deck: Deck, plan) -> float:
    """This run's load current on `port`: from the plan's load state, else from the deck itself."""
    if plan is not None and run.load_key:
        for state in getattr(plan, "states", []) or []:
            if state.key == run.load_key:
                amps = state.of(port)
                if amps is not None:
                    return float(amps)
    entry = deck.by_pin.get(port)
    dc = (entry or {}).get("dc")
    if dc is None:
        raise _err(f"Cannot tell what load {port!r} was carrying.",
                   "The load axis value comes from the plan's load state, or failing that from "
                   f"the dc of the IL_ source in the deck; neither was readable.",
                   [f"Give IL_{port} a numeric dc in the testbench.",
                    "Or import with the plan that produced the run."], run.run_id)
    return float(dc)


def _snap(axis: list, value, dim: str, var: str, notes: list[str]):
    """The declared axis point `value` means, within a small relative tolerance."""
    best, err = None, math.inf
    for a in axis:
        try:
            d = abs(float(a) - float(value)) / max(1.0, abs(float(a)))
        except (TypeError, ValueError):
            d = 0.0 if a == value else math.inf
        if d < err:
            best, err = a, d
    if best is None or err > _RTOL_AXIS:
        raise _err(f"{var}: {dim}={value!r} is not on the dataset's {dim} axis.",
                   f"The dataset was created with {dim} = {axis}; this run sat at a point that "
                   "was never declared, so there is no cell to write it into.",
                   [f"Re-create the dataset from the plan that produced this run.",
                    f"Or re-run at one of: {axis}."],
                   "pmukit/importer.py")
    if err > 0:
        notes.append(f"{var}: {dim}={value!r} stored at the declared axis point {best!r} "
                     f"(relative difference {err:.2g})")
    return best


# =============================================================================== (a) own runs
def import_run(run: Run, psf_dir, dataset: Dataset, *, plan=None, netlist_text: str | None = None,
               source_path: str = "", pmu_inst: str = "", want: tuple | None = None) -> dict:
    """Derive every variable this run `reads` and put it into the dataset.

    Returns ``{"run_id", "filled": [var...], "missing": ["var: why"...], "notes": [...]}``.  A
    variable that cannot be derived is registered with `mark_missing` (when its storage exists)
    and always reported -- the fitter must be able to tell "ran and broke" from "never run".
    """
    d = pathlib.Path(psf_dir)
    report = {"run_id": run.run_id, "filled": [], "missing": [], "notes": []}
    text = netlist_text
    if text is None:
        deck_path = _find_deck(d)
        if deck_path is None:
            report["missing"].append(
                f"{', '.join(run.reads) or '(nothing)'}: no input.scs next to {d}, so the roles, "
                "the injection direction and the load state cannot be read")
            return report
        text = deck_path.read_text(encoding="utf-8", errors="replace")
    deck = Deck(text, str(d), pmu_inst=pmu_inst)

    try:
        psf_path, stmt = psf_for(d, run.analysis, deck, want)
    except PmuError as exc:
        for var in run.reads:
            _mark(dataset, var, run, deck, plan, f"{exc.what} {exc.why}", report)
        return report
    try:
        parsed = psfmod.read_psf(psf_path)
    except PmuError as exc:
        for var in run.reads:
            _mark(dataset, var, run, deck, plan, f"{exc.what} {exc.why}", report)
        return report

    reads, expand_notes = _expand_reads(run.reads, deck, parsed)
    report["notes"].extend(expand_notes)
    for var in reads:
        try:
            self_coord, values, notes = _derive(var, parsed, deck, run, str(psf_path))
            report["notes"].extend(notes)
            _store(var, self_coord, values, run, deck, dataset, plan, stmt, report)
        except PmuError as exc:
            _mark(dataset, var, run, deck, plan, f"{exc.what} {exc.why}", report)
    if source_path:
        report["source_path"] = source_path
    return report


def _expand_reads(reads, deck: Deck, parsed: dict) -> tuple[list[str], list[str]]:
    """Resolve `tran_en.<EN pin>` into one variable per port the EN run actually measured.

    The plan schedules the enable ramp against the EN pin, because that is the source it drives.
    But the `ramp` block measures the rise of EVERY modeled rail and bias (contract 1: "Rise time
    of each modeled rail", "Rise time of each modeled bias current"), and contract 2 keys a
    variable by the port it DESCRIBES.  So one `tran_en.EN` read becomes `tran_en.<rail>` /
    `tran_en.<bias>` for exactly the ports this run saved -- read off the deck and the result
    file, never assumed.
    """
    out: list[str] = []
    notes: list[str] = []
    for var in reads:
        observable, port = modelspec.split_variable(var)
        if observable != "tran_en" or deck.port_type(port) != "en":
            out.append(var)
            continue
        measured = []
        for pin, entry in deck.by_pin.items():
            if entry.get("role") not in ("rail", "bias"):
                continue
            signal = (deck.probe_of(pin) if entry["role"] == "bias"
                      else deck.net_of(pin, parsed))
            if signal in parsed:
                measured.append(pin)
        if not measured:
            out.append(var)
            notes.append(f"{var}: the enable run saved no rail or bias signal, so there is "
                         "nothing to describe the ramp of")
            continue
        out.extend(modelspec.variable_name("tran_en", pin) for pin in measured)
        notes.append(f"{var} describes the EN edge, so it is stored per measured port: "
                     + ", ".join(modelspec.variable_name("tran_en", pin) for pin in measured))
    return out, notes


def _store(var, axis, values, run, deck, dataset, plan, stmt, report) -> None:
    observable, port = modelspec.split_variable(var)
    vspec = variable_spec(observable, deck.port_type(port))
    coord_name = vspec["coord"]
    target = _target_coord(coord_name, axis, stmt or {})

    if var not in dataset.variables():
        dataset.declare(var, dims=vspec["dims"], dtype=vspec["dtype"], unit=vspec["unit"],
                        coord=target)
    stored = np.asarray(dataset.coord(var), dtype=float)

    y = np.asarray(values)
    same = (stored.size == axis.size
            and np.allclose(stored, np.asarray(axis, dtype=float), rtol=_RTOL_COORD, atol=0.0))
    if not same:
        log_x = coord_name == "freq_hz"
        y, note = _resample(stored, axis, y, log_x=log_x)
        if note:
            report["notes"].append(f"{var}: {note}")
        else:
            report["notes"].append(
                f"{var}: resampled from {axis.size} simulated points onto the stored "
                f"{coord_name} grid of {stored.size}")
        if coord_name == "time_s":
            true_pk = float(np.nanmax(np.abs(np.asarray(values, dtype=float))))
            kept_pk = float(np.nanmax(np.abs(np.asarray(y, dtype=float))))
            if true_pk > 0 and abs(true_pk - kept_pk) / true_pk > 1e-3:
                report["notes"].append(
                    f"{var}: resampling onto {TRAN_POINTS} uniform points moved the waveform "
                    f"extremum by {abs(true_pk - kept_pk) / true_pk * 100:.2f} % "
                    f"({true_pk:.6g} -> {kept_pk:.6g})")

    cell, notes = _cell_for(var, run, deck, dataset, plan)
    report["notes"].extend(notes)
    if dataset.has(var, cell):
        old = np.asarray(dataset.get(var, cell))
        if not np.allclose(np.nan_to_num(old), np.nan_to_num(np.asarray(y)),
                           rtol=1e-9, atol=0.0, equal_nan=True):
            report["notes"].append(
                f"{var} {cell_key(cell)}: this cell was already filled by another run and the "
                "new values DIFFER; the newer run wins. Two runs mapping to one cell means the "
                "plan varies an axis this variable does not depend on.")
        else:
            report["notes"].append(
                f"{var} {cell_key(cell)}: already filled by an equivalent run (the plan varies "
                "an axis this variable does not depend on); re-written identically")
    dataset.put(var, cell, y)
    report["filled"].append(f"{var} @ {cell_key(cell)}")


def _mark(dataset, var, run, deck, plan, reason, report) -> None:
    """Register one cell as 'ran and broke', or say why even that was not possible."""
    text = f"run {run.run_id} ({run.analysis}): {reason}".strip()
    try:
        if var in dataset.variables():
            cell, _notes = _cell_for(var, run, deck, dataset, plan)
            dataset.mark_missing(var, cell, text)
            report["missing"].append(f"{var} @ {cell_key(cell)}: {reason}")
            return
    except PmuError as exc:
        report["missing"].append(f"{var}: {reason} (and the cell is unknown: {exc.what})")
        return
    report["missing"].append(
        f"{var}: {reason} -- no cell was registered because this variable has no storage yet "
        "(no run has produced it), so the reason lives in the ledger only")


def mark_run_missing(run: Run, dataset: Dataset, reason: str, *, plan=None,
                     netlist_text: str | None = None, pmu_inst: str = "") -> dict:
    """Register every cell a FAILED run should have filled, with the failure reason."""
    report = {"run_id": run.run_id, "filled": [], "missing": [], "notes": []}
    if dataset is None:
        return report
    text = netlist_text
    if text is None:
        deck_path = None
        if run.netlist_path and pathlib.Path(run.netlist_path).is_file():
            deck_path = pathlib.Path(run.netlist_path)
        elif run.psf_path:
            deck_path = _find_deck(pathlib.Path(run.psf_path))
        text = deck_path.read_text(encoding="utf-8", errors="replace") if deck_path else ""
    deck = Deck(text or "", run.netlist_path, pmu_inst=pmu_inst)
    for var in run.reads:
        _mark(dataset, var, run, deck, plan, reason, report)
    return report


def _find_deck(psf_dir: pathlib.Path) -> pathlib.Path | None:
    """The `input.scs` that produced a result directory: in it, or beside it."""
    d = pathlib.Path(psf_dir)
    for cand in (d / "input.scs", d.parent / "input.scs"):
        if cand.is_file():
            return cand
    if d.is_dir():
        hits = sorted(d.glob("*.scs")) or sorted(d.parent.glob("*.scs"))
        if len(hits) == 1:
            return hits[0]
    return None


# =============================================================================== (b) external
def import_external(dirs, plan, ledger: Ledger, dataset: Dataset, *,
                    spec: dict | None = None, pmu_inst: str = "") -> dict:
    """Fill the dataset from result directories the user already has.

    Each directory must hold the `input.scs` that produced it plus its PSF.  The corner, the
    temperature, the VSET code, the load state and the excited source are read BACK OUT of that
    netlist and matched against the plan's cells -- no side-car manifest, no naming convention
    beyond contract 0a's own source prefixes.

    Returns ``{"filled": [...], "unmatched": [{"dir", "why"}], "still_to_run": [...]}``; every
    unmatched directory carries one sentence saying why.  `spec_files` adds explicit CSV imports
    (see `import_csv`) -- a CSV carries no netlist, so pmukit refuses to infer what it is.
    """
    report: dict = {"filled": [], "unmatched": [], "still_to_run": [], "notes": []}
    planned = list(plan.runs(enabled_only=False))
    wanted = {p.run_id: p for p in planned}
    cache: dict[str, Deck] = {}
    sigs: dict[str, dict] = {}
    keys: dict[str, tuple | None] = {}
    for pr in planned:                     # one Deck per DISTINCT netlist, not per run
        ckey = pr.run.netlist_sha or pr.netlist_text
        if ckey not in cache:
            cache[ckey] = Deck(pr.netlist_text, pmu_inst=pmu_inst)
        sigs[pr.run_id] = cache[ckey].signature()
        keys[pr.run_id] = cache[ckey].analysis_key_for(pr.run.analysis)
    matched: set[str] = set()

    for raw in list(dirs or []):
        d = pathlib.Path(raw)
        deck_path = _find_deck(d)
        if deck_path is None:
            report["unmatched"].append({"dir": str(d), "why":
                "there is no input.scs in or beside this directory, so pmukit cannot tell which "
                "corner, temperature, VSET code or load state produced it"})
            continue
        try:
            deck = Deck(deck_path.read_text(encoding="utf-8", errors="replace"),
                        str(deck_path), pmu_inst=pmu_inst)
        except PmuError as exc:
            report["unmatched"].append({"dir": str(d), "why": exc.what})
            continue
        psf_dir = _find_psf_dir(d)
        if psf_dir is None:
            report["unmatched"].append({"dir": str(d), "why":
                "no PSF result files were found in this directory (pmukit looked in it, in "
                "raw/ and in psf/)"})
            continue

        sig = deck.signature()
        hits = [rid for rid, s in sigs.items()
                if rid not in matched and _same_cell(s, sig)]
        if not hits:
            report["unmatched"].append({"dir": str(d), "why": _why_no_match(sig, sigs)})
            continue

        used = 0
        reasons: list[str] = []
        # Several planned runs can share a cell: at one corner the DC load sweep of rail A, the
        # DC load sweep of rail B and the bias I-V sweep differ ONLY in what their dc statement
        # sweeps.  So the cell match is narrowed by the analysis fingerprint (`dev=` / `param=` /
        # `oprobe=` / the noise output nodes) before anything is imported.
        by_kind: dict[str, list[str]] = {}
        for rid in hits:
            by_kind.setdefault(ANALYSIS_KIND.get(wanted[rid].run.analysis, ""), []).append(rid)
        for rid in hits:
            run = wanted[rid].run
            kind = ANALYSIS_KIND.get(run.analysis, "")
            stmts = deck.analysis_of_kind(kind)
            want = keys.get(rid)
            exact = [s for s in stmts if want is not None and analysis_key(s) == want]
            if exact:
                use = analysis_key(exact[0])
            elif len(stmts) == 1 and len(by_kind.get(kind, [])) == 1:
                # No fingerprint match, but this directory runs exactly ONE analysis of the right
                # type and exactly ONE planned run of that type wants this cell: unambiguous.
                use = analysis_key(stmts[0])
                report["notes"].append(
                    f"{d.name} -> {rid}: the deck's {kind} analysis is spelled differently from "
                    f"the planned one ({analysis_key(stmts[0])} vs {want}); accepted because it "
                    "is the only one of its type here and the only planned run that wants it")
            else:
                reasons.append(
                    f"{run.analysis}: this directory runs {len(stmts)} {kind} analysis/analyses "
                    f"and none matches what the planned run sweeps ({want}), so which one belongs "
                    "to this cell is ambiguous")
                continue
            sub = import_run(run, psf_dir, dataset, plan=plan, netlist_text=deck.text,
                             source_path=str(d), pmu_inst=pmu_inst, want=use)
            if not sub["filled"]:
                reasons.append(f"{run.analysis}: " + (sub["missing"][0] if sub["missing"]
                                                      else "nothing could be derived"))
                continue
            ledger.upsert(Run(**{**run.to_dict(), "status": "imported", "source_path": str(d),
                                 "psf_path": str(psf_dir)}))
            if wanted[rid].feeds:
                ledger.add_consumes(rid, wanted[rid].feeds)
            matched.add(rid)
            used += 1
            report["filled"].append({"dir": str(d), "run_id": rid, "analysis": run.analysis,
                                     "cell": run.cell_text(), "variables": sub["filled"]})
            report["notes"].extend(sub["notes"])
            report["notes"].extend(f"{rid}: {m}" for m in sub["missing"])
        if not used:
            report["unmatched"].append({"dir": str(d), "why":
                "the cell matched a planned run but no result could be used: "
                + ("; ".join(reasons[:2]) if reasons else "no usable analysis in the directory")})

    if spec:
        csv = import_csv(spec, dataset)
        report["filled"].extend({"dir": "(csv)", "run_id": "", "analysis": "csv",
                                 "cell": "", "variables": [f]} for f in csv["filled"])
        report["notes"].extend(csv["notes"])
        report["notes"].extend(csv["missing"])

    for rid, pr in wanted.items():
        stored = ledger.get(rid)
        if rid in matched or (stored is not None and stored.status in ("done", "imported")):
            continue
        report["still_to_run"].append({"run_id": rid, "analysis": pr.run.analysis,
                                       "cell": pr.run.cell_text(),
                                       "reads": list(pr.run.reads)})
    return report


def _find_psf_dir(d: pathlib.Path) -> pathlib.Path | None:
    for cand in (d / "raw", d / "psf", d):
        if cand.is_dir() and any(
                f.is_file() and f.name != "logFile"
                and any(f.name.endswith(s) for suf in _KIND_SUFFIX.values() for s in suf)
                for f in cand.iterdir()):
            return cand
    return None


def _same_cell(a: dict, b: dict) -> bool:
    """Do two decks simulate the same cell?  Corner sections, temperature, VSET, loads, drive."""
    if a["sections"] != b["sections"]:
        return False
    if not _close(a["temp_c"], b["temp_c"]):
        return False
    if a["vset"] != b["vset"]:
        return False
    if (a["hot"] or "") != (b["hot"] or ""):
        return False
    if len(a["drives"]) != len(b["drives"]):
        return False
    for (na, dca, ma, wa), (nb, dcb, mb, wb) in zip(a["drives"], b["drives"]):
        if na != nb or wa != wb:
            return False
        if not _close(dca, dcb) or not _close(ma, mb):
            return False
    return True


def _close(a, b, rtol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is b or (a is None and b is None)
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if fa != fa and fb != fb:            # NaN == NaN for our purposes
        return True
    return abs(fa - fb) <= rtol * max(1.0, abs(fa), abs(fb))


def _why_no_match(sig: dict, sigs: dict) -> str:
    """One sentence: the FIRST axis on which this deck disagrees with every planned run."""
    for field, label in (("sections", "the PDK include section(s)"), ("temp_c", "the temperature"),
                         ("vset", "the VSET code"), ("hot", "the excited source"),
                         ("drives", "how the convention sources are driven "
                                    "(load dc / ac mag / pwl wave)")):
        theirs = {_render(s[field]) for s in sigs.values()}
        mine = _render(sig[field])
        if mine not in theirs:
            some = ", ".join(sorted(theirs)[:3]) or "(none)"
            return (f"{label} does not match any planned run: this deck has {mine}, the plan "
                    f"needs one of {some}")
    return ("every axis matches a planned run individually but no single run matches all of them "
            "at once -- this combination of corner, temperature, VSET and load was never planned")


def _render(value) -> str:
    if isinstance(value, tuple):
        parts = []
        for item in value:
            if isinstance(item, tuple) and len(item) == 4:
                name, dc, mag, wave = item
                bits = [] if dc is None else [f"dc={dc:g}"]
                if mag:
                    bits.append(f"mag={mag:g}")
                if wave:
                    bits.append("pwl")
                parts.append(f"{name}({','.join(bits) or 'unset'})")
            else:
                parts.append(str(item))
        return "; ".join(parts) or "(none)"
    return "(unset)" if value is None else f"{value}"


# =============================================================================== CSV
def import_csv(files: dict, dataset: Dataset) -> dict:
    """Import plain tables, each one DECLARED by the user.

    `files` maps a path to `{"variable": "<observable>.<port>", "cell": {...}}`.  pmukit refuses
    to infer either: a CSV carries no netlist, so there is nothing to read the corner, the
    injection direction or the units out of, and a wrong guess is a silently wrong model.

    Column layout: `x, y` for a real variable, `x, re, im` for a complex one (already the ratio;
    a CSV has no raw signals to divide).
    """
    report = {"filled": [], "missing": [], "notes": []}
    for path, decl in (files or {}).items():
        p = pathlib.Path(path)
        var = (decl or {}).get("variable")
        cell = (decl or {}).get("cell")
        if not var or cell is None:
            report["missing"].append(
                f"{p}: skipped -- a CSV must be declared as "
                "{'variable': '<observable>.<port>', 'cell': {...}}; pmukit does not infer which "
                "observable or which corner a table is")
            continue
        try:
            table = _read_table(p)
            observable, port = modelspec.split_variable(var)
            complex_wanted = COORD[observable][1] == "complex128"
            if table.shape[1] < (3 if complex_wanted else 2):
                raise _err(f"{p.name} has {table.shape[1]} column(s).",
                           f"{var} is stored as {COORD[observable][1]}, so the table needs "
                           f"{'x, re, im' if complex_wanted else 'x, y'}.",
                           ["Re-export with the missing column, or pick the right variable."],
                           str(p))
            x = table[:, 0]
            y = (table[:, 1] + 1j * table[:, 2]) if complex_wanted else table[:, 1]
            if var not in dataset.variables():
                vspec = variable_spec(observable, _port_type_of(port, dataset, observable))
                dataset.declare(var, dims=vspec["dims"], dtype=vspec["dtype"],
                                unit=vspec["unit"], coord=x)
            stored = np.asarray(dataset.coord(var), dtype=float)
            if stored.size != x.size or not np.allclose(stored, x, rtol=_RTOL_COORD, atol=0.0):
                y, note = _resample(stored, x, y, log_x=COORD[observable][0] == "freq_hz")
                report["notes"].append(
                    f"{p.name} -> {var}: {note or 'resampled onto the stored coordinate'}")
            dataset.put(var, dict(cell), y)
            report["filled"].append(f"{var} @ {cell_key(dict(cell))} from {p.name}")
        except PmuError as exc:
            report["missing"].append(f"{p}: {exc.what} {exc.why}")
    return report


def _port_type_of(port: str, dataset: Dataset, observable: str) -> str:
    """Which port type a CSV-declared variable belongs to, from the observable alone."""
    rail_only = {"ac_zout", "noise_v", "dc_load", "tran_load_on", "tran_load_off"}
    bias_only = {"ac_yout", "noise_i", "dc_iv"}
    if observable in rail_only:
        return "rail"
    if observable in bias_only:
        return "bias"
    try:
        dataset.axis("load_a", port)
        return "rail"
    except PmuError:
        return "bias"


def _read_table(path: pathlib.Path) -> np.ndarray:
    """A comma- or whitespace-delimited numeric table; header, comment and unit rows are skipped."""
    rows: list[list[float]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#*;":
            continue
        toks = [t for t in line.replace(",", " ").split() if t]
        try:
            rows.append([float(t) for t in toks])
        except ValueError:
            continue                    # a header or a unit row
    if not rows:
        raise _err(f"{path.name} has no numeric rows.",
                   "Every line was blank, a comment, or not a number, so there is no data to "
                   "import.",
                   ["Check the delimiter (comma or whitespace) and that the file is the export "
                    "you meant."], str(path))
    width = max(len(r) for r in rows)
    return np.array([r + [np.nan] * (width - len(r)) for r in rows], dtype=float)
