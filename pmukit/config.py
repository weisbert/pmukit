"""Contract 0: the user's project config (0a) and the characterization config derived from it (0b).

0a is the only layer the user touches (CONTRACTS.md section 0a): one netlist exported at the
nominal corner, which corners / temperatures / VSET codes to run, what his own module draws on
each rail, and how high in frequency he cares.  Everything else the tool decides for him -- that
is 0b, written to `$PMUKIT_DATA/<project>/derived.json` and hashed into provenance.

Site configuration (engine / queue / CPUs) belongs to neither: see `pmukit.site`.
"""
from __future__ import annotations

import math
import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import jsonio
from .errors import PmuError

#: A Spectre parameter name, as `vset_param` must be.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# ---------------------------------------------------------------------------- 0a vocabulary
FATES = ("model", "stub", "ignore")
"""What happens to a pin: characterize+fit+emit / keep the pin as an ideal dc source / leave wired."""

ROLES = ("rail", "bias", "supply", "en", "none")
"""Pin roles the netlist parser recognises from the source-name prefix (IL_/VB_/VS_/VEN_)."""

# ---------------------------------------------------------------------------- 0b rules
F_START_HZ = 10.0                    # AC sweep floor (0b row "sweep range")
PTS_PER_DECADE = 20                  # AC sweep density (0b)
NOISE_BAND_HZ = (10.0, 1.0e8)        # noise band (0b row "noise band")
LOAD_GRID_FACTORS = (0.2, 1.0, 2.0)  # times `on_a`; `off_a` is the fourth point (0b load grid)
TEMP_STEP_DIVISOR = 8                # continuous dc temperature sweep: span/8 ...
TEMP_STEP_MIN_C = 5.0                # ... clamped to at least 5 C ...
TEMP_STEP_MAX_C = 25.0               # ... and at most 25 C
HISTORY_CAP = 50                     # config_history.json keeps this many snapshots

# --- the transient footgun (retired bug; see LDO_modeling docs/reference/TOOL_FACTS.md) --------
# FREQUENCY and TIMESCALE are three DIFFERENT things and must never be conflated:
#   care_up_to_hz : the AC/PSRR/noise SWEEP band only -- it may reach the carrier.
#   edge          : how fast the consumer's load actually SWITCHES -- a physical ~ns edge.
#   t_settle      : the loop RECOVERY time -- a loop-bandwidth property, NOT 1/f_min.
# The retired bug used the sweep band for both: a 6 GHz carrier gave edge = 0.05/6e9 ~ 8 ps while a
# 32 kHz f_min gave tstop = 8/32e3 = 250 us -- one transient of ~3e7 points, un-runnable.
EDGE_DEFAULT_S = 1.0e-9      # load-switch edge when `my_load[rail].edge_s` was not measured
SETTLE_DEFAULT_S = 2.0e-6    # loop recovery default; the fitter refines it from the |Zout| peak
SETTLE_WINDOWS = 8.0         # tstop spans this many settling times
EDGE_WINDOWS = 50.0          # ... but never fewer than this many edges (safety floor)


def _err(what: str, why: str, do, where: str = "") -> PmuError:
    return PmuError(what=what, why=why, do=list(do),
                    where=where or "project config (CONTRACTS.md 0a)")


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dedup(values, rtol: float = 1e-12) -> list[float]:
    """Sort and drop numerically identical points, keeping the original floats (no rounding)."""
    out: list[float] = []
    for v in sorted(float(x) for x in values):
        if out and abs(v - out[-1]) <= rtol * max(abs(v), abs(out[-1]), 1e-300):
            continue
        out.append(v)
    return out


# ============================================================================= 0a
@dataclass
class MyLoad:
    """What the consumer module draws on one rail -- the only current numbers the user owns."""

    on_a: float
    off_a: float
    switches: bool = True
    edge_s: float | None = None
    """Measured switching edge [s]; None -> the 1 ns default (never the carrier period)."""

    @classmethod
    def from_dict(cls, d, *, rail: str = "?", where: str = "") -> "MyLoad":
        if not isinstance(d, Mapping):
            raise _err(f"my_load[{rail!r}] is not an object.",
                       "Each rail entry must be a JSON object describing that rail's load.",
                       [f'Write "my_load": {{"{rail}": {{"on_a": 5e-4, "off_a": 2e-6, '
                        f'"switches": true}}}}'], where)
        unknown = set(d) - {"on_a", "off_a", "switches", "edge_s"}
        if unknown:
            raise _err(f"my_load[{rail!r}] has unknown key(s): {sorted(unknown)}.",
                       "Only on_a, off_a, switches and edge_s describe a consumer load.",
                       [f'Keep my_load[{rail!r}] to {{"on_a": .., "off_a": .., "switches": true, '
                        f'"edge_s": 1e-9}}'], where)
        for key in ("on_a", "off_a"):
            if key not in d:
                raise _err(f"my_load[{rail!r}] is missing {key!r}.",
                           "The load grid and the load-EN events are built from on_a and off_a.",
                           [f'Add "{key}" to my_load[{rail!r}], e.g. "on_a": 5e-4, "off_a": 2e-6'],
                           where)
        return cls(on_a=d["on_a"], off_a=d["off_a"],
                   switches=d.get("switches", True),
                   edge_s=d.get("edge_s"))

    def to_dict(self) -> dict:
        out: dict = {"on_a": float(self.on_a), "off_a": float(self.off_a),
                     "switches": bool(self.switches)}
        if self.edge_s is not None:
            out["edge_s"] = float(self.edge_s)
        return out

    def validate(self, rail: str, where: str = "") -> None:
        for key, val in (("on_a", self.on_a), ("off_a", self.off_a)):
            if not _is_num(val):
                raise _err(f"my_load[{rail!r}].{key} is not a number ({val!r}).",
                           "Load currents are amperes, written as a plain JSON number.",
                           [f'Set my_load[{rail!r}].{key} to a current in A, e.g. 5e-4'], where)
            if float(val) < 0.0:
                raise _err(f"my_load[{rail!r}].{key} is negative ({val!r}).",
                           "A consumer module draws current out of the rail; the value is a magnitude.",
                           [f'Set my_load[{rail!r}].{key} to a positive current in A, e.g. 5e-4'],
                           where)
        if float(self.on_a) <= float(self.off_a):
            raise _err(f"my_load[{rail!r}] has on_a <= off_a ({self.on_a!r} <= {self.off_a!r}).",
                       "The ON state must draw more than the OFF state, otherwise there is no load event.",
                       [f'Set my_load[{rail!r}] so on_a > off_a, e.g. "on_a": 5e-4, "off_a": 2e-6'],
                       where)
        if self.edge_s is not None and (not _is_num(self.edge_s) or float(self.edge_s) <= 0.0):
            raise _err(f"my_load[{rail!r}].edge_s is not a positive time ({self.edge_s!r}).",
                       "edge_s is the measured load-switch edge in seconds; it sets the transient step.",
                       [f'Set my_load[{rail!r}].edge_s to seconds, e.g. 1e-9, or drop it for the '
                        f'1 ns default'], where)
        if not isinstance(self.switches, bool):
            raise _err(f"my_load[{rail!r}].switches is not a boolean ({self.switches!r}).",
                       "switches decides whether the load-EN events are characterized at all.",
                       [f'Set my_load[{rail!r}].switches to true or false'], where)


@dataclass
class ProjectConfig:
    """Contract 0a: exactly the JSON the user fills in on the New screen."""

    project: str
    netlist: str
    pmu_inst: str
    corners: list[str] | dict[str, dict[str, str]]
    temps_c: list[float]
    vset_codes: list[int]
    ports: dict[str, str]
    my_load: dict[str, MyLoad]
    care_up_to_hz: float
    #: Optional. The DC level a `stub` pin should be emitted at: VOLTS for a rail pin, AMPS for a
    #: bias pin. The convention source gives the DUAL quantity (a rail carries an IL_ current, a
    #: bias a VB_ voltage), so a stub's own level is the one number the netlist cannot supply and
    #: a stub is by definition never simulated. Without it the emitter weakly ties the pin and
    #: says so, rather than driving it at an invented level.
    stub_dc: dict = field(default_factory=dict)
    state_note: str = ""
    #: The name of the design variable that selects the output code -- whatever the designer
    #: called it (`VSET`, `vout_sel`, `ldo_trim`...). It is rewritten per `vset_codes` as
    #: `parameters <vset_param>=<code>`. Absent in configs written before it existed, which all
    #: meant `VSET`.
    vset_param: str = "VSET"

    # ---------------------------------------------------------------- serialization
    @classmethod
    def from_dict(cls, d, *, where: str = "") -> "ProjectConfig":
        if not isinstance(d, Mapping):
            raise _err("The project config is not a JSON object.",
                       "Contract 0a is a single JSON object with the ten intake keys.",
                       ["Start from the example in docs/CONTRACTS.md section 0a"], where)
        known = {"project", "netlist", "pmu_inst", "corners", "temps_c", "vset_codes",
                 "vset_param", "ports", "my_load", "care_up_to_hz", "state_note", "stub_dc"}
        unknown = set(d) - known
        if unknown:
            raise _err(f"The project config has unknown key(s): {sorted(unknown)}.",
                       "Contract 0a is closed: an unknown key is a typo the tool must not silently ignore.",
                       [f"Remove {sorted(unknown)}; the accepted keys are {sorted(known)}"], where)
        missing = [k for k in ("project", "netlist", "pmu_inst") if k not in d]
        if missing:
            raise _err(f"The project config is missing {missing}.",
                       "project / netlist / pmu_inst identify what is being characterized.",
                       ['Add e.g. "project": "demo_pmu", "netlist": "tb/input.scs", '
                        '"pmu_inst": "PMU_TOP"'], where)
        raw_load = d.get("my_load") or {}
        if not isinstance(raw_load, Mapping):
            raise _err(f"'my_load' is not an object ({raw_load!r}).",
                       "my_load maps a rail pin name to that rail's consumer load.",
                       ['Write "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6}}'], where)
        loads = {str(k): (v if isinstance(v, MyLoad)
                          else MyLoad.from_dict(v, rail=str(k), where=where))
                 for k, v in raw_load.items()}
        corners = d.get("corners")
        if isinstance(corners, Mapping):
            corners = {str(k): v for k, v in corners.items()}
        elif isinstance(corners, Sequence) and not isinstance(corners, (str, bytes)):
            corners = list(corners)
        ports = d.get("ports")
        if isinstance(ports, Mapping):
            ports = {str(k): v for k, v in ports.items()}
        cfg = cls(project=d.get("project", ""), netlist=d.get("netlist", ""),
                  pmu_inst=d.get("pmu_inst", ""), corners=corners,
                  temps_c=list(d.get("temps_c") or []),
                  vset_codes=list(d.get("vset_codes") or []),
                  vset_param=d.get("vset_param", "VSET"),
                  ports=ports, my_load=loads, care_up_to_hz=d.get("care_up_to_hz"),
                  state_note=d.get("state_note", ""),
                  stub_dc={str(k): float(v) for k, v in (d.get("stub_dc") or {}).items()})
        cfg.source_path = where
        cfg.validate()
        return cfg

    def to_dict(self) -> dict:
        if isinstance(self.corners, Mapping):
            corners: list | dict = {str(k): {str(f): str(s) for f, s in v.items()}
                                    for k, v in self.corners.items()}
        else:
            corners = [str(c) for c in self.corners]
        out = {"project": str(self.project), "netlist": str(self.netlist),
                "pmu_inst": str(self.pmu_inst), "corners": corners,
                "temps_c": [float(t) for t in self.temps_c],
                "vset_codes": [int(v) for v in self.vset_codes],
                "state_note": str(self.state_note),
                "ports": {str(k): str(v) for k, v in self.ports.items()},
                "my_load": {k: v.to_dict() for k, v in self.my_load.items()},
                "care_up_to_hz": float(self.care_up_to_hz)}
        # Omitted when empty, like MyLoad.edge_s: an optional key that is absent should stay
        # absent, so a config written back out is the config that was read in.
        if self.stub_dc:
            out["stub_dc"] = {str(k): float(v) for k, v in self.stub_dc.items()}
        # Omitted at its default for the same reason -- and so every config written before the
        # key existed keeps its sha.
        if self.vset_param != "VSET":
            out["vset_param"] = str(self.vset_param)
        return out

    @classmethod
    def load(cls, path) -> "ProjectConfig":
        p = pathlib.Path(path)
        if not p.is_file():
            raise _err(f"No project config at {p}.",
                       "ProjectConfig.load reads the contract-0a JSON written by the New screen.",
                       ["Run the New screen (or `pmukit new`) to create it",
                        f"Check the path: {p}"], str(p))
        try:
            d = jsonio.read(p)
        except (OSError, ValueError) as exc:
            raise _err(f"Could not read the project config at {p}.",
                       f"The file is not valid UTF-8 JSON: {exc}.",
                       ["Fix the JSON syntax, or re-create the config from the New screen"],
                       str(p)) from None
        return cls.from_dict(d, where=str(p))

    def save(self, path) -> pathlib.Path:
        self.validate()
        p = jsonio.write(path, self.to_dict())
        self.source_path = str(p)
        return p

    def sha(self) -> str:
        """`config_sha`: 12 hex over the canonical 0a JSON -- key order and int/float spelling free."""
        return jsonio.sha(self.to_dict(), 12)

    # ---------------------------------------------------------------- validation
    _WHY = {
        "project": "project names the directory under $PMUKIT_DATA that holds every artifact.",
        "netlist": "netlist is the one file the user exports; everything is rewritten from it.",
        "pmu_inst": "pmu_inst is the instance whose pins become the ports of the model.",
    }
    _EXAMPLE = {"project": "demo_pmu", "netlist": "tb/input.scs", "pmu_inst": "PMU_TOP"}

    def validate(self) -> None:
        """Raise the four-part PmuError naming the offending key. Called by `from_dict`."""
        where = getattr(self, "source_path", "") or "project config (CONTRACTS.md 0a)"

        for key in ("project", "netlist", "pmu_inst"):
            val = getattr(self, key)
            if not isinstance(val, str) or not val.strip():
                raise _err(f"{key!r} is empty or missing ({val!r}).", self._WHY[key],
                           [f'Set "{key}": "{self._EXAMPLE[key]}"'], where)

        self._validate_corners(where)

        if not isinstance(self.temps_c, list) or not self.temps_c:
            raise _err(f"'temps_c' is empty ({self.temps_c!r}).",
                       "Every characterization run is indexed by temperature; there is no default.",
                       ['Set "temps_c": [-40, 25, 125] (degrees C, at least one point)'], where)
        for t in self.temps_c:
            if not _is_num(t):
                raise _err(f"'temps_c' holds a non-number ({t!r}).",
                           "Temperatures go straight into the netlist temp option and must be "
                           "plain numbers.",
                           ['Set "temps_c": [-40, 25, 125] (degrees C)'], where)

        if not isinstance(self.vset_codes, list) or not self.vset_codes:
            raise _err(f"'vset_codes' is empty ({self.vset_codes!r}).",
                       f"The netlist parameter {self.vset_param!r} is rewritten per code; an empty list "
                       "characterizes nothing.",
                       ['Set "vset_codes": [3] (one integer per output code you care about)'], where)
        for v in self.vset_codes:
            if isinstance(v, bool) or not isinstance(v, int):
                raise _err(f"'vset_codes' holds a non-integer ({v!r}).",
                           "VSET is a register code written verbatim into the netlist parameter.",
                           ['Set "vset_codes": [3] -- integers only, not 3.0 or "3"'], where)
        if not isinstance(self.vset_param, str) or not _IDENT.fullmatch(self.vset_param):
            raise _err(f"'vset_param' is not a netlist parameter name ({self.vset_param!r}).",
                       "It names the design variable that selects the output code; pmukit "
                       "rewrites `parameters <vset_param>=<code>` in the netlist.",
                       ['Set "vset_param" to the variable as it appears in the netlist '
                        '`parameters` line, e.g. "VSET" or "vout_sel"'], where)

        if not isinstance(self.ports, Mapping) or not self.ports:
            raise _err(f"'ports' is empty or not an object ({self.ports!r}).",
                       "ports is the Model column of the New screen: it decides what is "
                       "characterized at all.",
                       ['Set "ports": {"VDD0P8_A": "model", "VDD0P8_C": "stub", '
                        '"TESTMODE": "ignore"}'], where)
        for pin, fate in self.ports.items():
            if not isinstance(pin, str) or not pin.strip():
                raise _err(f"'ports' has an empty pin name ({pin!r}).",
                           "Each key of ports is a pin of the PMU instance, spelled as in the netlist.",
                           ['Set "ports": {"VDD0P8_A": "model"}'], where)
            if fate not in FATES:
                raise _err(f"ports[{pin!r}] is {fate!r}, which is not one of {list(FATES)}.",
                           "A pin is either modeled, stubbed with its dc value, or left wired -- "
                           "there is no fourth fate.",
                           [f'Set ports[{pin!r}] to "model", "stub" or "ignore"'], where)

        if not _is_num(self.care_up_to_hz) or float(self.care_up_to_hz) <= 0.0:
            raise _err(f"'care_up_to_hz' is not a positive frequency ({self.care_up_to_hz!r}).",
                       "It is the top of the AC/PSRR sweep band and of the delivered validity "
                       "envelope.",
                       ['Set "care_up_to_hz": 2e10 (Hz, the highest frequency your simulation '
                        'cares about)'], where)

        for rail, ml in self.my_load.items():
            if rail not in self.ports:
                raise _err(f"my_load[{rail!r}] is not a pin of 'ports'.",
                           "Loads are declared per rail pin; a rail the ports table never mentions "
                           "cannot be characterized.",
                           [f'Add "{rail}" to ports (e.g. "{rail}": "model"), or fix the spelling '
                            f'in my_load', f"ports currently lists: {sorted(self.ports)}"], where)
            ml.validate(rail, where)

        if not isinstance(self.state_note, str):
            raise _err(f"'state_note' is not a string ({self.state_note!r}).",
                       "state_note is free text recorded into the deliverable provenance.",
                       ['Set "state_note": "RX mode, register 0x12=0x03"'], where)

    def _validate_corners(self, where: str) -> None:
        c = self.corners
        if isinstance(c, Mapping):
            if not c:
                raise _err("'corners' is empty.",
                           "Each corner rewrites the section= of the PDK include lines; with none "
                           "there is nothing to run.",
                           ['Set "corners": ["tt", "ss", "ff"] or {"MOSff_RCss": '
                            '{"toplevel.scs": "ff", "rc.scs": "ss"}}'], where)
            for name, spec in c.items():
                if not isinstance(name, str) or not name.strip():
                    raise _err(f"'corners' has an empty corner name ({name!r}).",
                               "The corner name becomes a section in the delivered .scs library.",
                               ['Set "corners": {"MOSff_RCss": {"toplevel.scs": "ff", '
                                '"rc.scs": "ss"}}'], where)
                if not isinstance(spec, Mapping) or not spec:
                    raise _err(f"corners[{name!r}] is {spec!r}, not a {{filename: section}} map.",
                               "A composite corner names one section per PDK include file; "
                               "anything else cannot be rewritten.",
                               [f'Set corners[{name!r}] to {{"toplevel.scs": "ff", "rc.scs": "ss"}}',
                                'Or use the simple form: "corners": ["tt", "ss", "ff"]'], where)
                for fn, sec in spec.items():
                    if (not isinstance(fn, str) or not fn.strip()
                            or not isinstance(sec, str) or not sec.strip()):
                        raise _err(f"corners[{name!r}] has a bad entry {fn!r}: {sec!r}.",
                                   "Both halves are strings: the include file name and the "
                                   "section to select in it.",
                                   [f'Set corners[{name!r}] to {{"toplevel.scs": "ff", '
                                    f'"rc.scs": "ss"}}'], where)
            return
        if not isinstance(c, list) or not c:
            raise _err(f"'corners' is empty or not a list/object ({c!r}).",
                       "Each corner rewrites the section= of the PDK include lines; with none "
                       "there is nothing to run.",
                       ['Set "corners": ["tt", "ss", "ff"]',
                        'Composite corners: {"MOSff_RCss": {"toplevel.scs": "ff", "rc.scs": "ss"}}'],
                       where)
        for name in c:
            if not isinstance(name, str) or not name.strip():
                raise _err(f"'corners' holds an empty corner name ({name!r}).",
                           "A simple corner is the section name used on every section= include line.",
                           ['Set "corners": ["tt", "ss", "ff"]'], where)

    # ---------------------------------------------------------------- accessors
    def corner_names(self) -> list[str]:
        """Corner names in declaration order: ["tt","ss"] or ["MOSff_RCss", ...]."""
        src = self.corners.keys() if isinstance(self.corners, Mapping) else self.corners
        return [str(k) for k in src]

    def corner_sections(self, name: str) -> dict[str, str] | str:
        """Simple corner -> the section string (it applies to every section= include line).
        Composite corner -> the per-include-file mapping."""
        if name not in self.corner_names():
            raise _err(f"No corner named {name!r}.",
                       "corner_sections looks the name up in config.corners.",
                       [f"Use one of {self.corner_names()}"], getattr(self, "source_path", ""))
        if isinstance(self.corners, Mapping):
            return {str(k): str(v) for k, v in self.corners[name].items()}
        return str(name)

    def _ports_with(self, fate: str) -> list[str]:
        return [p for p, f in self.ports.items() if f == fate]

    def modeled_ports(self) -> list[str]:
        """Pins that get characterized, fitted and emitted, in declaration order."""
        return self._ports_with("model")

    def stub_ports(self) -> list[str]:
        """Pins emitted as an ideal dc source, zero simulation, in declaration order."""
        return self._ports_with("stub")

    def ignored_ports(self) -> list[str]:
        """Pins left wired as they are, in declaration order."""
        return self._ports_with("ignore")


# ============================================================================= config history (Ctrl-Z)
class ConfigHistory:
    """Append-only stack of ProjectConfig snapshots kept in <project_dir>/config_history.json.

    UX_RULES: configuration changes are undoable (Ctrl-Z); submitted runs are not -- those are
    skipped or killed. The stack keeps the newest HISTORY_CAP snapshots, oldest dropped.
    """

    KIND = "pmukit.config_history/1"

    def __init__(self, path, cap: int = HISTORY_CAP):
        self.path = pathlib.Path(path)
        self.cap = int(cap)

    # -- storage
    def _read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            d = jsonio.read(self.path)
        except (OSError, ValueError) as exc:
            raise _err(f"Could not read the config history at {self.path}.",
                       f"The file is not valid UTF-8 JSON: {exc}.",
                       ["Delete the file to start a fresh history (the current config is unaffected)"],
                       str(self.path)) from None
        items = d.get("entries") if isinstance(d, Mapping) else None
        return list(items) if isinstance(items, list) else []

    def _write(self, entries: list[dict]) -> None:
        jsonio.write(self.path, {"kind": self.KIND, "cap": self.cap, "entries": entries})

    # -- api
    def push(self, cfg: ProjectConfig, note: str = "") -> None:
        """Snapshot cfg. A push that would repeat the current config is dropped -- Ctrl-Z must move."""
        entries = self._read()
        sha = cfg.sha()
        if entries and entries[-1].get("sha") == sha:
            return
        entries.append({"sha": sha, "note": str(note), "saved_at": _now(), "config": cfg.to_dict()})
        if len(entries) > self.cap:
            entries = entries[-self.cap:]
        self._write(entries)

    def undo(self) -> ProjectConfig:
        """Drop the newest snapshot and return the one before it."""
        entries = self._read()
        if len(entries) < 2:
            raise _err("Nothing to undo.",
                       f"The config history at {self.path} holds {len(entries)} snapshot(s); "
                       "undo needs a previous one to go back to.",
                       ["Change the configuration first -- every save pushes a snapshot",
                        "Submitted runs are never undone: skip or kill them instead"], str(self.path))
        entries.pop()
        self._write(entries)
        return ProjectConfig.from_dict(entries[-1]["config"], where=str(self.path))

    def current(self) -> ProjectConfig | None:
        """The newest snapshot, or None when nothing was ever pushed."""
        entries = self._read()
        if not entries:
            return None
        return ProjectConfig.from_dict(entries[-1]["config"], where=str(self.path))

    def entries(self) -> list[dict]:
        """[{"sha","note","saved_at"}], newest last -- the stored payloads stay out."""
        return [{"sha": e.get("sha", ""), "note": e.get("note", ""),
                 "saved_at": e.get("saved_at", "")} for e in self._read()]


# ============================================================================= 0b
@dataclass
class DerivedConfig:
    """Contract 0b: what the tool decided. Every field carries its own provenance string."""

    KIND = "pmukit.derived/1"

    project: str = ""
    config_sha: str = ""
    process: dict = field(default_factory=dict)
    temps_c: dict = field(default_factory=dict)
    dc_temp_sweep: dict = field(default_factory=dict)
    vset: dict = field(default_factory=dict)
    supply: dict = field(default_factory=dict)
    rails: dict = field(default_factory=dict)
    biases: dict = field(default_factory=dict)
    grounds: dict = field(default_factory=dict)
    en: dict = field(default_factory=dict)
    stubs: dict = field(default_factory=dict)
    interface: dict = field(default_factory=dict)
    ignored: list = field(default_factory=list)
    loads: dict = field(default_factory=dict)
    transient: dict = field(default_factory=dict)
    freq: dict = field(default_factory=dict)
    noise: dict = field(default_factory=dict)
    grouping: dict = field(default_factory=dict)
    site: dict = field(default_factory=dict)

    _FIELDS = ("project", "config_sha", "process", "temps_c", "dc_temp_sweep", "vset", "supply",
               "rails", "biases", "grounds", "en", "stubs", "interface", "ignored", "loads",
               "transient", "freq", "noise", "grouping", "site")

    def to_dict(self) -> dict:
        """What gets written to $PMUKIT_DATA/<project>/derived.json."""
        d: dict = {"kind": self.KIND}
        for name in self._FIELDS:
            d[name] = getattr(self, name)
        return d

    @classmethod
    def from_dict(cls, d) -> "DerivedConfig":
        if not isinstance(d, Mapping):
            raise _err("derived.json is not a JSON object.",
                       "DerivedConfig.from_dict reads what DerivedConfig.to_dict wrote.",
                       ["Re-run derive() to regenerate $PMUKIT_DATA/<project>/derived.json"], "")
        return cls(**{k: d[k] for k in cls._FIELDS if k in d})

    @classmethod
    def load(cls, path) -> "DerivedConfig":
        return cls.from_dict(jsonio.read(path))

    def save(self, path) -> pathlib.Path:
        return jsonio.write(path, self.to_dict())

    def sha(self, n: int = 12) -> str:
        """Hash of everything that defines the measurement. `site` is excluded on purpose: the
        engine/queue/CPU count is an install property, so the same characterization hashes the
        same on the desk and in the red zone."""
        d = self.to_dict()
        d.pop("site", None)
        return jsonio.sha(d, n)

    def freq_points(self) -> list[float]:
        """Expand the AC sweep spec into Hz points (endpoints exact, log spacing)."""
        f = self.freq or {}
        return log_points(f.get("start_hz", F_START_HZ), f.get("stop_hz", F_START_HZ),
                          int(f.get("n_points", 1) or 1))


def log_points(start: float, stop: float, n: int) -> list[float]:
    """n log-spaced points with both endpoints exact (pure python, no numpy)."""
    start, stop, n = float(start), float(stop), int(n)
    if n <= 1 or start <= 0.0 or stop <= 0.0:
        return [start]
    la, lb = math.log10(start), math.log10(stop)
    return [10.0 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]


def n_log_points(start: float, stop: float, per_decade: int = PTS_PER_DECADE) -> int:
    """How many points a `per_decade`-dense log sweep needs, endpoints included."""
    decades = math.log10(float(stop) / float(start)) if stop > start > 0 else 0.0
    return int(round(per_decade * decades)) + 1


# ---------------------------------------------------------------------------- pin-table adapter
def _pin_table(pins) -> dict[str, dict]:
    """Accept whatever the netlist parser hands over: a plain dict, an object with `.to_dict()`,
    or one with a `.pins` mapping. Duck-typed so config.py never imports netlist.py."""
    if pins is None:
        return {}
    obj = pins
    if not isinstance(obj, Mapping) and hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    if isinstance(obj, Mapping) and isinstance(obj.get("pins"), Mapping):
        obj = obj["pins"]
    if not isinstance(obj, Mapping) and hasattr(obj, "pins"):
        obj = obj.pins
    if not isinstance(obj, Mapping):
        raise _err(f"The pin table is not a mapping ({type(pins).__name__}).",
                   "derive() reads {pin: {role, net, gnd, src, dc, fate}} -- the netlist parser output.",
                   ["Pass the parser dict, or None to derive only the config-only axes"], "")
    out: dict[str, dict] = {}
    for name, entry in obj.items():
        if isinstance(entry, Mapping):
            out[str(name)] = dict(entry)
        else:
            out[str(name)] = {k: getattr(entry, k) for k in
                              ("role", "net", "gnd", "src", "dc", "fate", "ilimit", "index",
                               "is_ground")
                              if hasattr(entry, k)}
    return out


def _fate(cfg: ProjectConfig, pin: str, entry: Mapping) -> str:
    """The user Model column wins; the parser guess is only the fallback."""
    f = cfg.ports.get(pin) or entry.get("fate") or "model"
    return f if f in FATES else "model"


# ---------------------------------------------------------------------------- derive (0b)
def derive(cfg: ProjectConfig, pins=None, site=None) -> DerivedConfig:
    """Build the contract-0b characterization config from 0a (+ the pin table, + the site config).

    `pins=None` still yields the axes that need only `cfg` (process / temp / vset / freq / noise);
    the pin-derived fields stay empty instead of crashing, so the New screen can show the derived
    config before the netlist is parsed.

    FREQUENCY vs TIMESCALE (the retired bug, see the module header): `care_up_to_hz` sets the AC
    sweep band ONLY. The transient edge comes from the measured load switch (my_load[rail].edge_s,
    else 1 ns) and tstop from the loop recovery time (SETTLE_DEFAULT_S, refined later by
    `refine_from_zout` from the fitted |Zout| peak). Conflating them once produced an 8 ps edge
    inside a 250 us window -- a ~3e7-point transient that could not run.
    """
    if not isinstance(cfg, ProjectConfig):
        raise _err(f"derive() needs a ProjectConfig, got {type(cfg).__name__}.",
                   "The derived config is a pure function of the 0a intake plus the pin table.",
                   ["Call ProjectConfig.from_dict(...) (or .load(path)) first"], "")
    cfg.validate()
    table = _pin_table(pins)
    d = DerivedConfig(project=cfg.project, config_sha=cfg.sha())

    # -- process axis ---------------------------------------------------------
    composite = isinstance(cfg.corners, Mapping)
    d.process = {
        "corners": cfg.corner_names(),
        "composite": composite,
        "sections": {name: cfg.corner_sections(name) for name in cfg.corner_names()},
        "rewrite": ("per include file named in the corner map; include files not named keep "
                    "their section" if composite else
                    "every include line carrying section=<x> becomes section=<corner>"),
        "provenance": "config.corners -> include section= rewrite (0b row: process axis)",
    }

    # -- temperature ----------------------------------------------------------
    temps = [float(t) for t in cfg.temps_c]
    d.temps_c = {"points": sorted(temps),
                 "provenance": "config.temps_c -> one temp option per run (0b row: discrete "
                               "temperatures)"}
    lo, hi = min(temps), max(temps)
    span = hi - lo
    step = min(max(span / TEMP_STEP_DIVISOR, TEMP_STEP_MIN_C), TEMP_STEP_MAX_C)
    rule = f"step = span/{TEMP_STEP_DIVISOR} clamped to [{TEMP_STEP_MIN_C:g}, {TEMP_STEP_MAX_C:g}] degC"
    d.dc_temp_sweep = {
        "start_c": lo, "stop_c": hi, "step_c": step, "rule": rule,
        "n_points": (1 if span <= 0 else int(math.floor(span / step)) + 1),
        # A continuous sweep needs two ends. With one declared temperature there is nothing to
        # sweep, so the run is not generated and the parameters that read it are reported NOT RUN
        # -- inventing a temperature range the user did not ask for would be worse.
        "run": span > 0,
        "reason": ("" if span > 0 else
                   f"only one temperature declared ({lo:g} C); a continuous temperature sweep "
                   "needs at least two"),
        "provenance": f"min/max(config.temps_c) with {rule} -- DC quantities get a continuous "
                      "temperature sweep (0b row: discrete temperatures + continuous DC sweep)",
    }

    # -- vset -----------------------------------------------------------------
    name = cfg.vset_param
    declared = getattr(pins, "params", None)       # a PinTable carries them; a plain dict does not
    if isinstance(declared, Mapping) and len(cfg.vset_codes) > 1 and name not in declared:
        # Declaring the missing variable would "work" -- and every code would simulate the same
        # circuit, because nothing in the PMU reads it. That is a wrong answer, not a warning.
        have = ", ".join(sorted(declared)) or "(none)"
        raise _err(f"The netlist has no `parameters {name}=` but {len(cfg.vset_codes)} codes "
                   f"were asked for.",
                   f"Each code is produced by rewriting `{name}`; if the design does not read "
                   "that variable, every code simulates the same circuit.",
                   [f"Set vset_param to the design variable that selects the output code; the "
                    f"netlist declares: {have}",
                    "Or ask for one code only, if this PMU has no output-code variable"],
                   getattr(cfg, "source_path", "") or "project config (CONTRACTS.md 0a)")
    d.vset = {"codes": [int(v) for v in cfg.vset_codes], "param": name,
              "provenance": f"config.vset_codes -> the netlist parameter {name} (config."
                            f"vset_param) is rewritten per code (0b row: VSET)"}

    # -- frequency / noise / grouping (config-only) ---------------------------
    stop_hz = float(cfg.care_up_to_hz)
    n = n_log_points(F_START_HZ, stop_hz, PTS_PER_DECADE)
    d.freq = {"type": "log", "start_hz": F_START_HZ, "stop_hz": stop_hz,
              "points_per_decade": PTS_PER_DECADE, "n_points": n,
              "provenance": f"{F_START_HZ:g} Hz .. config.care_up_to_hz, {PTS_PER_DECADE} "
                            "points/decade (0b row: sweep range and density). This is the AC/PSRR "
                            "band ONLY -- it never sets a transient edge."}
    d.noise = {"start_hz": NOISE_BAND_HZ[0], "stop_hz": NOISE_BAND_HZ[1],
               "provenance": f"fixed {NOISE_BAND_HZ[0]:g} Hz .. {NOISE_BAND_HZ[1]:g} Hz "
                             "(0b row: noise band)"}
    d.grouping = {"mode": "ac_superposition",
                  "provenance": "0b row: one supply injection reads every port (AC superposition), "
                                "runs grouped per netlist variant"}

    # -- site (install-time, not per project; excluded from DerivedConfig.sha) -
    if site is not None:
        d.site = site.to_dict() if hasattr(site, "to_dict") else dict(site)
        d.site["provenance"] = "site.json (install time): engine / queue / CPUs (0b last row)"

    if not table:
        return d

    # -- pin roles ------------------------------------------------------------
    grounds: dict[str, str] = {}
    supply_pins: dict[str, dict] = {}
    for pin, e in table.items():
        role = str(e.get("role") or "none")
        fate = _fate(cfg, pin, e)
        gnd = e.get("gnd")
        if gnd:
            grounds[pin] = str(gnd)
        if fate == "ignore":
            d.ignored.append(pin)
            continue
        if role == "none":
            continue
        if fate == "stub":
            level = cfg.stub_dc.get(pin)
            d.stubs[pin] = {"role": role, "net": e.get("net"), "gnd": gnd, "src": e.get("src"),
                            "dc": e.get("dc"),
                            # Volts for a rail, amps for a bias -- the emitter reads exactly these.
                            "dc_v": (float(level) if level is not None and role != "bias" else None),
                            "dc_a": (float(level) if level is not None and role == "bias" else None),
                            "dc_source": ("config.stub_dc" if level is not None else
                                          "not given -- the pin will be weakly tied, not driven"),
                            "emit": "isource" if role == "bias" else "vsource",
                            "note": "stub, not modeled",
                            "provenance": f"ports[{pin!r}] = stub -> emitted as an ideal dc "
                                          "source, zero simulation"}
            continue
        if role == "supply":
            supply_pins[pin] = {"net": e.get("net"), "gnd": gnd, "src": e.get("src"),
                                "nominal_v": e.get("dc")}
        elif role == "rail":
            d.rails[pin] = {"net": e.get("net"), "gnd": gnd, "src": e.get("src"),
                            "i_typ_a": e.get("dc"), "ilimit_a": e.get("ilimit"),
                            "provenance": f"pin role 'rail' from the {e.get('src') or 'IL_'} "
                                          "source prefix (0b row: rail/bias pins and grounds)"}
        elif role == "bias":
            d.biases[pin] = {"net": e.get("net"), "gnd": gnd, "src": e.get("src"),
                             "vcomp_v": e.get("dc"),
                             "provenance": f"pin role 'bias' from the {e.get('src') or 'VB_'} "
                                           "source prefix (0b row: rail/bias pins and grounds)"}
        elif role == "en":
            d.en[pin] = {"net": e.get("net"), "gnd": gnd, "src": e.get("src"), "dc": e.get("dc"),
                         "provenance": f"pin role 'en' from the {e.get('src') or 'VEN_'} "
                                       "source prefix"}

    d.grounds = {"by_pin": grounds, "nets": sorted(set(grounds.values())),
                 "provenance": "the ground net wired to each pin, read from the netlist "
                               "(0b row: split grounds)"}

    # -- the PMU's own pin list, in ITS order ---------------------------------
    # Contract 4: the delivered module has the PMU's pins in the PMU's order, so the consumer
    # swaps the cell without rewiring. Every pin is here -- grounds and role-less pins too.
    def _pos(item):
        idx = item[1].get("index")
        return int(idx) if isinstance(idx, int) and not isinstance(idx, bool) else 1 << 30

    order = sorted(table.items(), key=_pos)                 # stable: no index -> table order
    d.interface = {
        "inst": str(getattr(pins, "pmu_inst", "") or ""),
        "master": str(getattr(pins, "pmu_master", "") or ""),
        "pins": [{"pin": pin, "index": i, "net": e.get("net"),
                  "role": str(e.get("role") or "none"), "fate": _fate(cfg, pin, e),
                  "ground": bool(e.get("is_ground"))} for i, (pin, e) in enumerate(order)],
        "provenance": "the PMU subcircuit's port list in its own order, with the testbench net on "
                      "each pin -- the delivered module declares exactly these pins in this "
                      "order (contract 4)"}

    # -- supply ---------------------------------------------------------------
    nominal = [float(v["nominal_v"]) for v in supply_pins.values() if _is_num(v.get("nominal_v"))]
    d.supply = {"pins": supply_pins, "sweep": False, "advanced": True,
                "nominal_v": (max(nominal) if nominal else None),
                "provenance": "the VS_ source dc, nominal point only; sweeping the supply range "
                              "is an advanced option (0b row: supply)"}

    # -- bias I-V sweep: 0 .. nominal supply ----------------------------------
    for pin, b in d.biases.items():
        if nominal:
            stop_v, why = max(nominal), "0 .. nominal supply (the largest VS_ source dc)"
        elif _is_num(b.get("vcomp_v")):
            stop_v, why = float(b["vcomp_v"]), "0 .. this pin VB_ compliance dc (no supply pin found)"
        else:
            stop_v, why = None, "no supply pin and no VB_ dc -- the I-V span is unknown"
        b["iv_sweep"] = {"start_v": 0.0, "stop_v": stop_v,
                         "provenance": f"{why} (0b row: bias compliance voltage and I-V sweep)"}

    # -- load grids and load-EN events ---------------------------------------
    for pin in d.rails:
        d.loads[pin] = _load_grid(cfg, pin, d.rails[pin])
        d.transient[pin] = _transient_window(cfg, pin, d.loads[pin])
    return d


def _load_grid(cfg: ProjectConfig, rail: str, rail_info: Mapping) -> dict:
    """[off, 0.2*on, on, 2*on] from my_load, clipped to the PMU current limit when one is known."""
    ilimit = rail_info.get("ilimit_a")
    ilimit = float(ilimit) if _is_num(ilimit) else None
    ml = cfg.my_load.get(rail)
    if ml is None:
        dc = rail_info.get("i_typ_a")
        points = [float(dc)] if _is_num(dc) else []
        # The load SWEEP (for load regulation, dropout and the current limit) still has to span a
        # range even when only one operating point is known: 0 .. 2x the testbench's own load.
        # That range comes from the netlist, not from a guess about the user's module.
        sweep = ({"start_a": 0.0, "stop_a": 2.0 * float(dc), "n_points": 9,
                  "provenance": f"my_load not declared -> 0 .. 2x the {rail_info.get('src') or 'IL_'} "
                                "source dc, so load regulation and dropout are still measured"}
                 if _is_num(dc) else {})
        return {"points_a": points, "load_en": False, "reason": "my_load not declared",
                "events": [], "sweep": sweep,
                "provenance": f"the {rail_info.get('src') or 'IL_'} source dc is the only load "
                              "point; my_load not declared -> no load-EN event, reported as NOT RUN "
                              "(0b row: load characterization grid)"}
    raw = [float(ml.off_a)] + [f * float(ml.on_a) for f in LOAD_GRID_FACTORS]
    clipped = [min(v, ilimit) for v in raw] if ilimit is not None else list(raw)
    points = _dedup(clipped)
    prov = (f"my_load[{rail!r}] -> [off, 0.2*on, on, 2*on] = "
            f"[{ml.off_a:g}, {0.2 * ml.on_a:g}, {ml.on_a:g}, {2 * ml.on_a:g}] A")
    prov += (f", clipped to the PMU current limit {ilimit:g} A" if ilimit is not None
             else " (no PMU current limit known -- nothing clipped)")
    prov += " (0b row: load characterization grid)"
    events: list[dict] = []
    reason = ""
    if ml.switches:
        edge = float(ml.edge_s) if ml.edge_s is not None else EDGE_DEFAULT_S
        events = [{"event": "tran_load_on", "from_a": float(ml.off_a), "to_a": float(ml.on_a),
                   "edge_s": edge},
                  {"event": "tran_load_off", "from_a": float(ml.on_a), "to_a": float(ml.off_a),
                   "edge_s": edge}]
    else:
        reason = f"my_load[{rail!r}].switches is false"
    sweep = {"start_a": min(points), "stop_a": max(points), "n_points": max(len(points), 9),
             "provenance": "the declared load grid's own span"} if len(points) > 1 else {}
    return {"points_a": points, "load_en": bool(ml.switches), "reason": reason, "events": events,
            "sweep": sweep, "provenance": prov}


def _transient_window(cfg: ProjectConfig, rail: str, load: Mapping) -> dict:
    """The transient window. Read the FREQUENCY vs TIMESCALE warning in the module header first:
    the edge is a load-switch property and tstop a loop-recovery property -- neither is 1/f."""
    ml = cfg.my_load.get(rail)
    if ml is not None and ml.edge_s is not None:
        edge = float(ml.edge_s)
        edge_src = f"my_load[{rail!r}].edge_s (the measured load switch)"
    else:
        edge = EDGE_DEFAULT_S
        edge_src = (f"default {EDGE_DEFAULT_S:g} s -- a fast but PHYSICAL load switch, never "
                    "1/care_up_to_hz")
    t_settle = SETTLE_DEFAULT_S
    tstop = max(SETTLE_WINDOWS * t_settle, EDGE_WINDOWS * edge)
    return {
        "edge_s": edge, "t_settle_s": t_settle, "tstop_s": tstop,
        "run": bool(load.get("events")),
        "provenance": (f"edge = {edge_src}; t_settle = default {SETTLE_DEFAULT_S:g} s (the loop "
                       "recovery time -- the fitter refines it from the fitted |Zout| peak via "
                       f"refine_from_zout); tstop = {SETTLE_WINDOWS:g} x t_settle, floored at "
                       f"{EDGE_WINDOWS:g} x edge. Frequency and timescale are decoupled: "
                       "care_up_to_hz sets the AC band only (0b row: transient duration/step)."),
    }


def refine_from_zout(derived: DerivedConfig, rail: str, f_peak_hz: float) -> DerivedConfig:
    """Phase B: replace the default settling time with the one the fitted |Zout| peak implies.

    t_settle = 8/(2*pi*f_peak) is the rail recovery time; tstop = 8 * t_settle. Mutates and returns
    `derived` so the caller can re-write derived.json with the new provenance.
    """
    if not isinstance(derived, DerivedConfig):
        raise _err(f"refine_from_zout() needs a DerivedConfig, got {type(derived).__name__}.",
                   "It rewrites the transient window inside the derived config.",
                   ["Pass the DerivedConfig returned by derive()"], "")
    tr = derived.transient.get(rail)
    if tr is None:
        raise _err(f"No transient window for rail {rail!r}.",
                   "refine_from_zout looks the rail up in derived.transient, which holds the "
                   "modeled rails.",
                   [f"Use one of {sorted(derived.transient)}",
                    "Re-run derive() with the pin table if the rail list is empty"], "")
    if not _is_num(f_peak_hz) or float(f_peak_hz) <= 0.0:
        raise _err(f"f_peak_hz is not a positive frequency ({f_peak_hz!r}).",
                   "It is the argmax of the fitted |Zout(f)| for this rail, in Hz.",
                   ["Pass the peak frequency of the fitted |Zout|, e.g. 1.2e6"], "")
    f_peak = float(f_peak_hz)
    t_settle = SETTLE_WINDOWS / (2.0 * math.pi * f_peak)
    tstop_old = float(tr["tstop_s"])
    tstop = max(SETTLE_WINDOWS * t_settle, EDGE_WINDOWS * float(tr["edge_s"]))
    tr["t_settle_s"] = t_settle
    tr["tstop_s"] = tstop
    tr["f_peak_hz"] = f_peak
    tr["provenance"] = (f"edge unchanged ({float(tr['edge_s']):g} s); t_settle = "
                        f"{SETTLE_WINDOWS:g}/(2*pi*f_peak) with the fitted |Zout| peak "
                        f"f_peak = {f_peak:g} Hz -> {t_settle:g} s (was the "
                        f"{SETTLE_DEFAULT_S:g} s default); tstop = {SETTLE_WINDOWS:g} x t_settle "
                        f"= {tstop:g} s, floored at {EDGE_WINDOWS:g} x edge (was {tstop_old:g} s). "
                        "Refined post-fit; frequency and timescale stay decoupled.")
    return derived
