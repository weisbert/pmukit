"""The measurement plan: model spec x project config -> the smallest set of simulations.

This is the answer to the two questions the old flow could not answer -- *what will run* and
*why does this particular run exist* (REFACTOR_PLAN section 1, pain points 3 and 4).

How a plan is built
-------------------
1. `spec.requirements(port_type)` says, for every observable, which axes it varies over and which
   `(block, param)` consume it.  That list is inverted here: every run carries its `feeds`, and the
   ledger's `consumes` table is written from it, so the Plan screen's "why" panel is a lookup, not
   a story someone wrote by hand.
2. Requirements that would be produced by the SAME simulation -- same analysis, same hot source --
   form a FAMILY, and a family is one run per cell.  That is AC superposition made literal
   (CONTRACTS.md 0b, "one supply injection reads every port"): the rail PSRR and the bias PSRR are
   the same supply injection, so they are one simulation reading both.
3. A family runs the UNION of its members' axes.  This is not an over-estimate: a netlist has one
   load state, one VSET and one temperature, so if any member needs the load swept then every
   member of that family gets the extra points for free -- the simulation happens anyway.
   `vset` in the union -> one run per VSET code, otherwise the nominal code only; `load_a` -> one
   run per load state, otherwise the nominal state; `temp_c` or `temp_cont` -> one run per declared
   temperature.  The single exception is `dc_temp`, which IS the continuous temperature sweep: one
   run sweeps the whole range internally, and its ledger `temp_c` is NaN ("not at one temperature").
4. `run_id` is a content hash (netlist sha + corner + analysis + stimulus + cell), so re-planning
   an unchanged run collides with the finished one and is reported `skipped_cached`.  Resume is a
   property of the identifier, not a separate mechanism.

Load states
-----------
A simulation sets the load on EVERY rail at once, so "rail A at four loads" is not a free axis per
rail -- the runs walk a shared LOAD STATE.  State `Lk` puts every rail on the k-th point of its own
grid (a rail with a shorter grid holds its last point).  The state carries a human label so the
screens and the report never show a bare `L3`: the user sees `A=500u B=2m`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import spec
from .config import DerivedConfig, ProjectConfig
from .errors import PmuError
from .ledger import Ledger, Recipe, Run, make_run_id
from .netlist import Netlist, PinTable

# Stable analysis statement names, so a log or a PSF directory is recognisable.
AC_NAME = "acz"
NOISE_NAME = "nz"
DC_NAME = "dcz"
TRAN_NAME = "trz"

# observable -> the ledger `analysis` bucket it runs in.
OBSERVABLE_ANALYSIS = {
    "dc_load": "dc_load", "dc_temp": "dc_temp", "dc_iv": "dc_iv",
    "ac_zout": "ac", "ac_psrr": "ac", "ac_yout": "ac",
    "noise_v": "noise", "noise_i": "noise",
    "tran_load_on": "tran_load_on", "tran_load_off": "tran_load_off", "tran_en": "tran_en",
}

# Plain-language group titles and the one sentence the Plan screen shows under each.
GROUP_TITLE = {
    "dc_load": ("DC load sweep", "rail voltage vs load: load regulation, dropout and the current "
                                 "limit -- and the operating point every small-signal run sits on"),
    "dc_temp": ("DC temperature sweep", "how the rails and the bias currents drift with "
                                        "temperature; the PTAT slope lives here"),
    "dc_iv": ("Bias I-V sweep", "each bias current vs its pin voltage: the compliance range and "
                                "the output conductance"),
    "ac": ("AC (impedance, PSRR, admittance)", "one injection per source, every port read at once "
                                               "-- output impedance, supply rejection, bias "
                                               "admittance"),
    "noise": ("Noise", "output noise spectra: rail voltage noise and bias current noise, the two "
                       "things that become phase noise"),
    "tran_load_on": ("Load turn-on transient", "the droop when your module switches its load on"),
    "tran_load_off": ("Load turn-off transient", "the overshoot when your module switches off"),
    "tran_en": ("Enable ramp", "how the rails and biases come up when EN goes high -- usable, "
                               "not signed off"),
}


# ------------------------------------------------------------------------------ load states
@dataclass(frozen=True)
class LoadState:
    """One simulated load condition: what every rail is drawing at the same time."""

    key: str                       # "L0"... -- short, stable, hashable into run_id
    currents: dict                 # rail pin -> amps
    label: str                     # "A=500u B=2m" -- what a person reads

    def of(self, rail: str) -> float | None:
        return self.currents.get(rail)


def _eng(a: float) -> str:
    """500e-6 -> '500u'. Used only for labels."""
    if a == 0:
        return "0"
    for exp, suf in ((-15, "f"), (-12, "p"), (-9, "n"), (-6, "u"), (-3, "m"), (0, ""), (3, "k")):
        if abs(a) < 10 ** (exp + 3):
            v = a / (10.0 ** exp)
            return f"{v:.3g}{suf}"
    return f"{a:.3g}"


def load_states(derived: DerivedConfig) -> list[LoadState]:
    """The shared load states. State k puts every rail on the k-th point of its own grid."""
    grids = {rail: list(derived.loads.get(rail, {}).get("points_a", []))
             for rail in derived.rails}
    grids = {r: g for r, g in grids.items() if g}
    if not grids:
        return [LoadState("L0", {}, "(no load grid)")]
    n = max(len(g) for g in grids.values())
    out = []
    for k in range(n):
        cur = {r: g[min(k, len(g) - 1)] for r, g in grids.items()}
        label = " ".join(f"{r}={_eng(v)}" for r, v in cur.items())
        out.append(LoadState(f"L{k}", cur, label))
    return out


def nominal_state(states: Sequence[LoadState], derived: DerivedConfig) -> LoadState:
    """The state closest to the testbench's own typical load -- what a "held at nominal" run uses."""
    typ = {r: v.get("i_typ_a") for r, v in derived.rails.items()}
    typ = {r: float(v) for r, v in typ.items() if isinstance(v, (int, float))}
    if not typ:
        return states[len(states) // 2]

    def dist(s: LoadState) -> float:
        d = 0.0
        for r, t in typ.items():
            got = s.of(r)
            if got is None or t <= 0:
                continue
            d += abs(math.log10(max(got, 1e-15)) - math.log10(t))
        return d

    return min(states, key=dist)


# ------------------------------------------------------------------------------- cost model
CostFn = Callable[[Run, DerivedConfig], float]


def default_cost(run: Run, derived: DerivedConfig) -> float:
    """A rough CPU-seconds estimate. Deliberately crude: it is replaced by measured times as soon
    as the ledger has any (see `measured_cost`), and the Plan screen labels it an estimate."""
    nf = int((derived.freq or {}).get("n_points", 200) or 200)
    a = run.analysis
    if a == "ac":
        return 4.0 + 0.02 * nf
    if a == "noise":
        return 8.0 + 0.05 * nf
    if a == "dc_load":
        return 3.0
    if a == "dc_temp":
        return 2.0 + 0.4 * float((derived.dc_temp_sweep or {}).get("n_points", 5) or 5)
    if a == "dc_iv":
        return 4.0
    if a.startswith("tran"):
        rails = list(derived.transient.values())
        tstop = max([float(t.get("tstop_s", 2e-5)) for t in rails] or [2e-5])
        edge = min([float(t.get("edge_s", 1e-9)) for t in rails] or [1e-9])
        pts = min(tstop / max(edge / 10.0, 1e-15), 2e6)
        return 10.0 + 2e-4 * pts
    return 5.0


def measured_cost(ledger: Ledger, fallback: CostFn = default_cost) -> CostFn:
    """Cost from this project's own finished runs: the median CPU seconds per analysis type.

    This is the "pluggable cost estimate" of M4 -- the estimate stops being a guess after the
    first real sweep, and the Plan screen's number converges on what the queue actually charges.
    """
    seen: dict[str, list[float]] = {}
    for r in ledger.all(status="done"):
        if r.cpu_seconds and r.cpu_seconds > 0:
            seen.setdefault(r.analysis, []).append(float(r.cpu_seconds))
    median = {}
    for a, vals in seen.items():
        vals.sort()
        median[a] = vals[len(vals) // 2]

    def cost(run: Run, derived: DerivedConfig) -> float:
        return median.get(run.analysis) or fallback(run, derived)

    cost.measured_for = tuple(sorted(median))          # type: ignore[attr-defined]
    return cost


# ------------------------------------------------------------------------------- plan objects
@dataclass
class PlannedRun:
    """One simulation, with the netlist it would run and the parameters it feeds."""

    run: Run
    netlist_text: str
    feeds: tuple[tuple[str, str, str], ...]        # (port, block, param)
    group_id: str
    cost_s: float = 0.0

    @property
    def run_id(self) -> str:
        return self.run.run_id

    def why(self) -> str:
        """The Plan screen's Why panel, one sentence, built from `feeds`."""
        if not self.feeds:
            return "no parameter claims this run -- it would produce nothing"
        by_port: dict[str, list[str]] = {}
        for port, block, param in self.feeds:
            by_port.setdefault(port, []).append(f"{block}.{param}")
        parts = [f"{port} needs {', '.join(sorted(set(v)))}" for port, v in sorted(by_port.items())]
        return (f"This run exists because {'; '.join(parts)} -- measured as "
                f"{', '.join(sorted(set(self.run.reads)))} at {self.run.cell_text()}.")


@dataclass
class Group:
    """A row on the Plan screen: runs that share a purpose, ticked on or off together."""

    id: str
    title: str
    why: str
    analysis: str
    runs: list[PlannedRun] = field(default_factory=list)
    enabled: bool = True

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    @property
    def cost_s(self) -> float:
        return sum(r.cost_s for r in self.runs)

    def ports(self) -> list[str]:
        seen = []
        for r in self.runs:
            for v in r.run.reads:
                port = v.split(".", 1)[1] if "." in v else v
                if port not in seen:
                    seen.append(port)
        return seen

    def observables(self) -> list[str]:
        seen = []
        for r in self.runs:
            for v in r.run.reads:
                obs = v.split(".", 1)[0]
                if obs not in seen:
                    seen.append(obs)
        return seen

    def to_row(self) -> dict:
        return {"id": self.id, "title": self.title, "why": self.why, "analysis": self.analysis,
                "runs": self.n_runs, "cpu_seconds": round(self.cost_s, 1),
                "ports": self.ports(), "observables": self.observables(), "enabled": self.enabled}


@dataclass
class Plan:
    """The whole measurement plan for one project configuration."""

    project: str
    config_sha: str
    derived_sha: str
    groups: list[Group] = field(default_factory=list)
    states: list[LoadState] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---- access
    def group(self, gid: str) -> Group:
        for g in self.groups:
            if g.id == gid:
                return g
        raise PmuError(what=f"no plan group named '{gid}'.",
                       why="Groups are identified by the ids the plan compiler produced.",
                       do=[f"Pick one of: {', '.join(g.id for g in self.groups)}."],
                       where="plan")

    def runs(self, *, enabled_only: bool = True) -> list[PlannedRun]:
        return [r for g in self.groups if g.enabled or not enabled_only for r in g.runs]

    def set_enabled(self, gid: str, on: bool) -> None:
        self.group(gid).enabled = bool(on)

    # ---- numbers
    def cost_summary(self) -> dict:
        by: dict[str, dict] = {}
        for g in self.groups:
            if not g.enabled:
                continue
            e = by.setdefault(g.analysis, {"runs": 0, "cpu_seconds": 0.0})
            e["runs"] += g.n_runs
            e["cpu_seconds"] = round(e["cpu_seconds"] + g.cost_s, 1)
        total = round(sum(e["cpu_seconds"] for e in by.values()), 1)
        return {"by_analysis": by, "runs": sum(e["runs"] for e in by.values()),
                "cpu_seconds": total, "cpu_hours": round(total / 3600.0, 3)}

    # ---- consequences of un-ticking a group
    def consequences(self) -> list[dict]:
        """What is lost with the current tick state: the parameters that now have no run.

        Contract 0c requires the report to list what was never run, in the user's words. This is
        that list, computed before anything is submitted so the Plan screen can show it in red.
        """
        have: set[tuple[str, str, str]] = set()
        for r in self.runs(enabled_only=True):
            have.update(r.feeds)
        lost: dict[tuple[str, str], dict] = {}
        for r in self.runs(enabled_only=False):
            for feed in r.feeds:
                if feed in have:
                    continue
                port, block, param = feed
                e = lost.setdefault((port, block), {"port": port, "block": block, "params": [],
                                                    "observables": []})
                if param not in e["params"]:
                    e["params"].append(param)
                for v in r.run.reads:
                    obs = v.split(".", 1)[0]
                    if v.endswith("." + port) and obs not in e["observables"]:
                        e["observables"].append(obs)
        out = []
        for (port, block), e in sorted(lost.items()):
            e["params"].sort()
            e["effect"] = (f"{port} {block} will be reported NOT RUN; anything the model says "
                           f"about it is outside the validity envelope")
            out.append(e)
        return out

    # ---- output
    def to_rows(self) -> list[dict]:
        return [g.to_row() for g in self.groups]

    def commit(self, ledger: Ledger) -> dict:
        """Write the enabled runs into the ledger, with their `consumes` rows.

        Already-finished runs keep their status (that is the cache); the return value is the
        new / cached / updated split the Plan screen shows before it submits anything.
        """
        planned = [r for r in self.runs(enabled_only=True)]
        counts = ledger.plan_many([r.run for r in planned])
        for r in planned:
            if r.feeds:
                ledger.add_consumes(r.run_id, r.feeds)
        counts["groups"] = sum(1 for g in self.groups if g.enabled)
        return counts


# --------------------------------------------------------------------------- the compiler
def _iterate_axes(axes, analysis: str, derived: DerivedConfig,
                  states: Sequence[LoadState], nominal: LoadState):
    """Turn a family's axes into the concrete list of cells to run.

    `dc_temp` is the one analysis that sweeps temperature INSIDE a single run -- it is the
    continuous DC temperature sweep of contract 0b. Every other analysis that depends on
    temperature (discrete `temp_c`, or continuous `temp_cont` reached through the DC tables) runs
    once per declared temperature point.
    """
    axes = set(axes)
    corners = list((derived.process or {}).get("corners", [])) or ["tt"]
    temps = list((derived.temps_c or {}).get("points", [])) or [25.0]
    codes = list((derived.vset or {}).get("codes", [])) or [0]

    use_vset = codes if "vset" in axes else codes[:1]
    use_loads = list(states) if "load_a" in axes else [nominal]
    if analysis == "dc_temp":
        use_temps = [None]                       # swept inside the run
    elif "temp_c" in axes or "temp_cont" in axes:
        use_temps = temps
    else:
        use_temps = temps[:1]

    for corner in corners:
        for temp in use_temps:
            for code in use_vset:
                for state in use_loads:
                    yield corner, temp, code, state


def _apply_corner(nl: Netlist, cfg: ProjectConfig, corner: str) -> None:
    """Rewrite the PDK include section(s) for one corner name."""
    sections = cfg.corner_sections(corner)
    if isinstance(sections, dict):               # composite: each include file named explicitly
        for file_pattern, section in sections.items():
            nl.set_section(file_pattern, section)
        return
    for file_name, current in nl.includes():     # simple: every include carrying a section=
        if current is not None:
            nl.set_section(file_name, sections)


def _base_variant(base: Netlist, cfg: ProjectConfig, derived: DerivedConfig, corner: str,
                  temp: float | None, code: int, state: LoadState) -> Netlist:
    """The netlist every run of one cell starts from: corner, VSET, temperature, load state."""
    nl = base.copy()
    nl.edits.clear()
    _apply_corner(nl, cfg, corner)
    nl.set_param("VSET", code)
    if temp is not None:
        nl.set_temperature(float(temp))
    for rail, amps in sorted(state.currents.items()):
        src = (derived.rails.get(rail) or {}).get("src")
        if src:
            nl.set_dc(src, float(amps))
    nl.strip_analyses()
    return nl


def _sweep_clause(start: float, stop: float, n: int) -> str:
    return f"start={start:g} stop={stop:g} lin={int(n)}"


def _ac_clause(derived: DerivedConfig) -> str:
    f = derived.freq or {}
    return (f"start={float(f.get('start_hz', 10)):g} stop={float(f.get('stop_hz', 1e9)):g} "
            f"dec={int(f.get('points_per_decade', 20))}")


def _noise_clause(derived: DerivedConfig) -> str:
    n = derived.noise or {}
    return (f"start={float(n.get('start_hz', 10)):g} stop={float(n.get('stop_hz', 1e8)):g} "
            f"dec={int((derived.freq or {}).get('points_per_decade', 20))}")


def _ground_of(derived: DerivedConfig, port: str) -> str:
    """The ground net a port returns to, or the global 0 when the netlist had only one."""
    gnd = ((derived.grounds or {}).get("by_pin") or {}).get(port)
    return gnd or "0"


def _submit_line(site, corner: str, run_id: str) -> str:
    """The literal command this run will be launched with -- the last line of every recipe."""
    engine = getattr(site, "engine", "dry_run") if site is not None else "dry_run"
    host = getattr(site, "ssh_host", "ewave-vm") if site is not None else "ewave-vm"
    wd = getattr(site, "remote_workdir", "~/pmukit_work") if site is not None else "~/pmukit_work"
    cpus = getattr(site, "cpus", 8) if site is not None else 8
    if engine == "spectre_ssh":
        return (f"ssh {host} 'tcsh -c \"source ~/.cshrc; cd {wd}/{run_id}; "
                f"spectre -64 input.scs -format psfascii -raw raw +log spectre.log -E\"'")
    if engine == "donau_alps":
        return (f"dsub -q short -R \"cpu={cpus};mem=8000\" -x all -EP <netdir> "
                f"-J <alps>/bin/alps input.scs -format ps -o <psf>/{run_id} "
                f"-I <pdk>/alps -ahdllibdir <ahd> -mt {cpus} -ade")
    return f"[{engine}] {run_id}  (no simulator invoked)"


def compile_plan(cfg: ProjectConfig, derived: DerivedConfig, netlist: Netlist,
                 pins: PinTable | None = None, *, site=None,
                 cost: CostFn = default_cost,
                 tiers: tuple[str, ...] = ("hb", "ls", "en")) -> Plan:
    """Compile the measurement plan. Pure text and arithmetic -- no simulator is touched."""
    if not derived.rails and not derived.biases:
        raise PmuError(
            what="the derived config has no rails and no biases to characterize.",
            why="A plan is generated only for ports whose fate is `model`; this project has none "
                "(every pin is stub, ignore, or unclassified).",
            do=["Set at least one port to `model` on the New screen.",
                "If the pins look wrong, re-parse the netlist -- roles come from the IL_/VB_/VS_/"
                "VEN_ source prefixes."],
            where=f"derived config for {cfg.project}")

    states = load_states(derived)
    nominal = nominal_state(states, derived)
    plan = Plan(project=cfg.project, config_sha=cfg.sha(), derived_sha=derived.sha(),
                states=states)

    modeled_rails = list(derived.rails)
    modeled_biases = list(derived.biases)
    supplies = list((derived.supply or {}).get("pins", {}))
    enables = list(derived.en)

    # 1) collect every requirement, tagged with the port it belongs to
    wanted: list[tuple[str, str, spec.Requirement]] = []       # (port, port_type, requirement)
    for port in modeled_rails:
        for req in spec.requirements("rail", tiers):
            wanted.append((port, "rail", req))
    for port in modeled_biases:
        for req in spec.requirements("bias", tiers):
            wanted.append((port, "bias", req))
    for port in enables:
        for req in spec.requirements("en", tiers):
            wanted.append((port, "en", req))

    # 2) group by the SIMULATION that would produce them: (analysis, hot source).
    #    This is AC superposition made literal -- the rail PSRR and the bias PSRR are the same
    #    supply injection, so they are one family and therefore one run per cell.
    #
    #    A family runs the UNION of its members' axes. That is not an approximation: a netlist has
    #    one load state, one VSET and one temperature, so if any member of the family needs the
    #    load swept, every member gets the extra points for free -- the simulation happens anyway.
    families: dict[tuple, dict] = {}
    family_order: list[tuple] = []
    for port, _port_type, req in wanted:
        obs = req.observable
        analysis = OBSERVABLE_ANALYSIS.get(obs)
        if analysis is None:                       # e.g. no_sink: an emitter constant, never a run
            continue
        stim = _stimulus_for(obs, port, derived, supplies, enables)
        if stim is None:
            plan.notes.append(f"{obs} for {port}: no source to drive it, skipped")
            continue
        key = (analysis, stim)
        f = families.get(key)
        if f is None:
            f = families[key] = {"analysis": analysis, "stimulus": stim, "axes": set(),
                                 "reads": [], "feeds": [], "observables": []}
            family_order.append(key)
        f["axes"].update(req.axes)
        var = spec.variable_name(obs, port)
        if var not in f["reads"]:
            f["reads"].append(var)
        if obs not in f["observables"]:
            f["observables"].append(obs)
        for block, param in req.consumers:
            f["feeds"].append((port, block, param))

    # 3) enumerate each family's cells and build one run per cell
    groups: dict[str, Group] = {}
    for key in family_order:
        f = families[key]
        feeds = tuple(dict.fromkeys(f["feeds"]))
        for corner, temp, code, state in _iterate_axes(f["axes"], f["analysis"], derived,
                                                       states, nominal):
            b = dict(f, corner=corner, temp=temp, code=code, state=state,
                     load_axis=("load_a" in f["axes"]))
            pr = _build_run(cfg, derived, netlist, b, site=site)
            pr.cost_s = cost(pr.run, derived)
            object.__setattr__(pr, "feeds", feeds)
            gid = _group_id(b)
            g = groups.get(gid)
            if g is None:
                title, why = GROUP_TITLE.get(b["analysis"], (b["analysis"], ""))
                if b["analysis"] == "ac":
                    title = f"{title} -- inject {b['stimulus']}"
                if b["analysis"] == "noise":
                    title = f"{title} -- {b['observables'][0]} {b['reads'][0].split('.', 1)[1]}"
                g = groups[gid] = Group(id=gid, title=title, why=why, analysis=b["analysis"])
                plan.groups.append(g)
            g.runs.append(pr)
    return plan


def _group_id(b: dict) -> str:
    """Groups are what the user ticks: one per analysis and stimulus, across all cells."""
    if b["analysis"] == "ac":
        return f"ac:{b['stimulus']}"
    if b["analysis"] == "noise":
        return f"noise:{b['reads'][0]}"
    if b["analysis"] in ("dc_load", "dc_iv", "tran_load_on", "tran_load_off"):
        return f"{b['analysis']}:{b['stimulus']}"
    return b["analysis"]


def _stimulus_for(obs: str, port: str, derived: DerivedConfig, supplies: list[str],
                  enables: list[str]) -> str | None:
    """Which single source is excited (or probed) for this observable. One hot source per run."""
    rail = (derived.rails or {}).get(port) or {}
    bias = (derived.biases or {}).get(port) or {}
    if obs in ("dc_load", "ac_zout", "tran_load_on", "tran_load_off"):
        return rail.get("src")
    if obs in ("dc_iv", "ac_yout"):
        return bias.get("src")
    if obs == "ac_psrr":
        if not supplies:
            return None
        src = ((derived.supply or {}).get("pins", {}).get(supplies[0]) or {}).get("src")
        return src
    if obs == "noise_v":
        return f"oprobe:{rail.get('net') or port}"
    if obs == "noise_i":
        src = bias.get("src")
        return f"oprobe:{src}" if src else None
    if obs == "dc_temp":
        return "temp"                              # the sweep is the global temperature
    if obs == "tran_en":
        en = (derived.en or {}).get(port) or {}
        if en.get("src"):
            return en["src"]
        return ((derived.en or {}).get(enables[0]) or {}).get("src") if enables else None
    return None


def _build_run(cfg: ProjectConfig, derived: DerivedConfig, base: Netlist, b: dict, *,
               site=None) -> PlannedRun:
    """Write the netlist variant for one bucket and wrap it in a ledger Run."""
    analysis, stim, state = b["analysis"], b["stimulus"], b["state"]
    nl = _base_variant(base, cfg, derived, b["corner"], b["temp"], b["code"], state)
    saves: list[str] = []
    analyses: list[str] = []

    if analysis == "ac":
        nl.set_mag(stim, 1)
        analyses.append(f"{AC_NAME} ac {_ac_clause(derived)}")
        for var in b["reads"]:
            obs, port = var.split(".", 1)
            if obs == "ac_yout":
                src = (derived.biases.get(port) or {}).get("src")
                if src:
                    saves.append(f"{src}:p")
            elif obs == "ac_psrr" and port in derived.biases:
                src = (derived.biases.get(port) or {}).get("src")
                if src:
                    saves.append(f"{src}:p")
            else:
                saves.append((derived.rails.get(port) or {}).get("net") or port)

    elif analysis == "noise":
        var = b["reads"][0]
        obs, port = var.split(".", 1)
        if obs == "noise_v":
            net = (derived.rails.get(port) or {}).get("net") or port
            analyses.append(f"{NOISE_NAME} ({net} {_ground_of(derived, port)}) noise "
                            f"{_noise_clause(derived)}")
            saves.append(net)
        else:
            probe = (derived.biases.get(port) or {}).get("src")
            # oprobe is a noise-analysis PARAMETER: it must come AFTER the `noise` keyword.
            # `nz oprobe=<src> noise ...` is a parse error (a scar from the cluster runs).
            analyses.append(f"{NOISE_NAME} noise {_noise_clause(derived)} oprobe={probe}")
            saves.append(f"{probe}:p")

    elif analysis == "dc_load":
        port = b["reads"][0].split(".", 1)[1]
        grid = list((derived.loads.get(port) or {}).get("points_a", []))
        lo, hi = (min(grid), max(grid)) if grid else (0.0, 1e-3)
        n = max(len(grid), 5)
        analyses.append(f"{DC_NAME} dc dev={stim} param=dc {_sweep_clause(lo, hi, n)}")
        saves.append((derived.rails.get(port) or {}).get("net") or port)

    elif analysis == "dc_iv":
        port = b["reads"][0].split(".", 1)[1]
        sw = (derived.biases.get(port) or {}).get("iv_sweep") or {}
        lo = float(sw.get("start_v", 0.0))
        hi = sw.get("stop_v")
        if hi is None:
            raise PmuError(
                what=f"bias {port} has no I-V sweep range.",
                why="The sweep runs 0 .. nominal supply, and neither a VS_ source dc nor this "
                    "pin's VB_ dc was readable from the netlist.",
                do=[f"Give VB_{port} a dc value in the testbench.",
                    "Or add the supply's VS_ source so the nominal supply is known."],
                where=f"derived.biases[{port!r}]")
        analyses.append(f"{DC_NAME} dc dev={stim} param=dc {_sweep_clause(lo, float(hi), 21)}")
        saves.append(f"{stim}:p")

    elif analysis == "dc_temp":
        sw = derived.dc_temp_sweep or {}
        analyses.append(f"{DC_NAME} dc param=temp "
                        f"{_sweep_clause(float(sw.get('start_c', -40)), float(sw.get('stop_c', 125)), int(sw.get('n_points', 5) or 5))}")
        for var in b["reads"]:
            port = var.split(".", 1)[1]
            if port in derived.rails:
                saves.append(derived.rails[port].get("net") or port)
            else:
                src = (derived.biases.get(port) or {}).get("src")
                if src:
                    saves.append(f"{src}:p")

    elif analysis in ("tran_load_on", "tran_load_off"):
        port = b["reads"][0].split(".", 1)[1]
        ev = next((e for e in (derived.loads.get(port) or {}).get("events", [])
                   if e["event"] == analysis), None)
        tw = derived.transient.get(port) or {}
        edge = float((ev or {}).get("edge_s", tw.get("edge_s", 1e-9)))
        tstop = float(tw.get("tstop_s", 2e-5))
        i0 = float((ev or {}).get("from_a", 0.0))
        i1 = float((ev or {}).get("to_a", 0.0))
        t0 = tstop * 0.1
        nl.set_pwl(stim, f"0 {i0:g} {t0:g} {i0:g} {t0 + edge:g} {i1:g} {tstop:g} {i1:g}")
        analyses.append(f"{TRAN_NAME} tran stop={tstop:g} step={max(edge / 10.0, 1e-12):g}")
        saves.append((derived.rails.get(port) or {}).get("net") or port)

    elif analysis == "tran_en":
        en_pin = next(iter(derived.en), None)
        en = (derived.en.get(en_pin) or {}) if en_pin else {}
        vhi = float(en.get("dc") or 1.0)
        tws = [float(t.get("tstop_s", 2e-5)) for t in derived.transient.values()] or [2e-5]
        tstop = max(tws)
        nl.set_pwl(stim, f"0 0 {tstop * 0.05:g} 0 {tstop * 0.05 + 1e-9:g} {vhi:g} {tstop:g} {vhi:g}")
        analyses.append(f"{TRAN_NAME} tran stop={tstop:g} step={tstop / 2000.0:g}")
        for var in b["reads"]:
            port = var.split(".", 1)[1]
            if port in derived.rails:
                saves.append(derived.rails[port].get("net") or port)
            else:
                src = (derived.biases.get(port) or {}).get("src")
                if src:
                    saves.append(f"{src}:p")

    seen: list[str] = []
    for s in saves:
        if s and s not in seen:
            seen.append(s)
    save_line = "save " + " ".join(seen) if seen else ""
    for line in analyses:
        nl.append(line)
    if save_line:
        nl.append(save_line)

    netlist_text = nl.render()
    load_key = state.key if b.get("load_axis") else ""
    run_id = make_run_id(netlist_sha=nl.sha(), process=b["corner"],
                         temp_c=(b["temp"] if b["temp"] is not None else float("nan")),
                         vset=int(b["code"]), load_key=load_key,
                         analysis=analysis, stimulus=stim)
    recipe = Recipe(edits=nl.recipe_edits(), analyses=list(analyses),
                    saves=[save_line] if save_line else [],
                    submit=_submit_line(site, b["corner"], run_id))
    run = Run(run_id=run_id, process=b["corner"],
              temp_c=(float(b["temp"]) if b["temp"] is not None else float("nan")),
              vset=int(b["code"]), load_key=load_key, analysis=analysis, stimulus=stim,
              reads=list(b["reads"]), netlist_sha=nl.sha(), recipe=recipe.text(),
              engine=getattr(site, "engine", "") if site is not None else "")
    feeds = tuple(dict.fromkeys(b["feeds"]))
    return PlannedRun(run=run, netlist_text=netlist_text, feeds=feeds, group_id=_group_id(b))
