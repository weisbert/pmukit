"""The fake engine: ANALYTIC results, no simulator anywhere.

This exists so the whole pipeline -- plan, run directory, ledger, PSF read, ratio maths, dataset
import, coverage -- is testable on a laptop with no Cadence, no ssh and no cluster.  It is not a
mock of the runner: it writes real psfascii files into `raw/`, which the real reader parses and
the real importer derives from.  The only thing that is not real is the physics.

It must be IMPOSSIBLE to mistake its output for a measurement, so it says so three times:

  * the ledger `engine` column reads `fake` for every run it produced;
  * every psfascii file it writes carries `"simulator" "pmukit-fake"` and a
    `"pmukit synthetic" "..."` header property;
  * `available()` returns a reason that leads with SYNTHETIC.

The physics, deliberately simple and deliberately named (`MODEL` below is the whole truth):

    Zout      a parallel RLC -- a resistive floor with one resonant peak
    PSRR      one real pole
    bias Yout g0 + s*Cp
    bias gdd  one real pole
    noise     a white floor with a 1/f corner
    DC        linear load regulation, linear bias I-V, linear tempco
    transient a damped cosine settling back to the regulated level

Every quantity is nudged by a small deterministic factor derived from the corner cell, so two
cells never come back byte-identical and a mis-wired cell mapping shows up instead of hiding.
"""
from __future__ import annotations

import hashlib
import math
import pathlib
import re

from ..errors import PmuError

__all__ = ["FakeBackend", "MODEL", "write_psfascii"]

#: The analytic model.  Read this instead of reverse-engineering the code.
MODEL = {
    "zout": {"r_ohm": 5.0, "l_h": 1.0e-6, "c_f": 1.0e-9},     # peak at 1/(2*pi*sqrt(LC)) ~ 5.03 MHz
    "psrr": {"h0": 1.0e-3, "fp_hz": 1.0e4},                   # -60 dB, one pole
    "gdd": {"a_per_v": 5.0e-7, "fp_hz": 1.0e5},               # supply -> bias current
    "yout": {"g0_s": 1.0e-6, "cp_f": 5.0e-14},
    "noise_v": {"white_v_rthz": 1.0e-8, "corner_hz": 1.0e4},
    "noise_i": {"white_a_rthz": 1.0e-13, "corner_hz": 1.0e4},
    "dc": {"v0_v": 0.8, "rload_ohm": 50.0, "tc_per_c": -1.0e-4},
    "iv": {"i0_a": 1.0e-5, "g0_s": 1.0e-6, "tc_per_c": 3.3e-3},
    "tran": {"dv_v": 0.02, "tau_s": 2.0e-7, "ring_hz": 5.03e6, "points": 801},
}

_TINY = 1.0e-12          # what an un-driven port reads under someone else's injection

_ANALYSIS_KIND = {"ac", "noise", "dc", "tran"}
_PSF_SUFFIX = {"ac": "ac", "noise": "noise", "dc": "dc", "tran": "tran"}
_WAVE_RE = re.compile(r"\bwave\s*=\s*\[([^\]]*)\]")


# --------------------------------------------------------------------------- deck reading
def _logical_lines(text: str):
    """Netlist lines with backslash continuations joined, comments and strips dropped."""
    out, parts = [], []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith(";"):
            continue
        body = s.split("//", 1)[0].rstrip()
        if body.endswith("\\"):
            parts.append(body[:-1].rstrip())
            continue
        parts.append(body)
        joined = " ".join(p for p in parts if p).strip()
        parts = []
        if joined:
            out.append(joined)
    if parts:
        joined = " ".join(p for p in parts if p).strip()
        if joined:
            out.append(joined)
    return out


def _params(tokens) -> dict:
    return dict(t.split("=", 1) for t in tokens if "=" in t)


def parse_deck(text: str) -> dict:
    """Everything the fake needs out of a deck: sources, analyses, saves.

    Returns ``{"sources": {name: {...}}, "analyses": [{...}], "saves": [...]}``.  Only statements
    pmukit itself wrote are interpreted; a stripped analysis is a comment and never seen.
    """
    sources: dict[str, dict] = {}
    analyses: list[dict] = []
    saves: list[str] = []
    for line in _logical_lines(text):
        toks = line.split()
        if not toks:
            continue
        if toks[0] == "save":
            saves.extend(toks[1:])
            continue
        if len(toks) < 2:
            continue
        rest = toks[1:]
        nodes: list[str] = []
        if rest and rest[0].startswith("("):
            blob = []
            while rest:
                t = rest.pop(0)
                blob.append(t)
                if t.endswith(")"):
                    break
            nodes = " ".join(blob).strip("()").split()
        if not rest:
            continue
        kind = rest[0]
        tail = rest[1:]
        if kind in ("isource", "vsource") and nodes:
            # `wave=[0 1e-6 ...]` has spaces in it, so it must be read off the whole statement:
            # a k=v token split keeps only `wave=[0` and then every pwl looks the same.
            wave = _WAVE_RE.search(line)
            sources[toks[0]] = {"nodes": nodes, "master": kind, **_params(tail),
                                "wave": " ".join(wave.group(1).split()) if wave else ""}
        elif kind in _ANALYSIS_KIND:
            analyses.append({"name": toks[0], "kind": kind, "nodes": nodes, **_params(tail)})
    return {"sources": sources, "analyses": analyses, "saves": saves}


def _f(d: dict, key: str, default: float) -> float:
    try:
        return float(d[key])
    except (KeyError, TypeError, ValueError):
        return default


def _log_sweep(start: float, stop: float, per_decade: int) -> list[float]:
    if stop <= start or start <= 0:
        return [max(start, 1e-30)]
    n = int(round(per_decade * math.log10(stop / start))) + 1
    la, lb = math.log10(start), math.log10(stop)
    return [10.0 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]


def _lin_sweep(start: float, stop: float, n: int) -> list[float]:
    n = max(int(n), 1)
    if n == 1:
        return [start]
    return [start + (stop - start) * i / (n - 1) for i in range(n)]


# --------------------------------------------------------------------------- psfascii writer
def write_psfascii(path, axis: str, axis_unit: str, xs, traces: dict, header: dict,
                   types: dict | None = None) -> pathlib.Path:
    """Write one psfascii result file: HEADER / TYPE / SWEEP / TRACE / VALUE / END.

    `traces` maps signal name -> sequence of values, complex or real.  `types` gives each signal
    its PSF TYPE NAME, which is not decoration: it is where the importer reads a noise total's
    unit from (`V/sqrt(Hz)` vs `V^2/Hz` differ by twelve orders of magnitude), so a fake that
    omitted it would produce files the real importer rightly refuses.
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    kinds = {name: ("COMPLEX" if any(isinstance(v, complex) for v in vals) else "FLOAT")
             for name, vals in traces.items()}
    names = {name: (types or {}).get(name) or ("cplx" if kinds[name] == "COMPLEX" else "real")
             for name in traces}
    lines = ["HEADER"]
    for k, v in header.items():
        lines.append(f'"{k}" "{v}"' if isinstance(v, str) else f'"{k}" {v!r}')
    lines.append("TYPE")
    lines += ['"sweep" FLOAT DOUBLE PROP(', '"key" "sweep"', ")"]
    written = set()
    for name, tname in names.items():
        if tname in written:
            continue
        written.add(tname)
        lines += [f'"{tname}" {kinds[name]} DOUBLE PROP(', f'"units" "{tname}"',
                  '"key" "node"', ")"]
    lines.append("SWEEP")
    lines += [f'"{axis}" "sweep" PROP(', '"sweep_direction" 0', f'"units" "{axis_unit}"', ")"]
    lines.append("TRACE")
    for name in traces:
        lines.append(f'"{name}" "{names[name]}"')
    lines.append("VALUE")
    for i, x in enumerate(xs):
        lines.append(f'"{axis}" {float(x):.15e}')
        for name, vals in traces.items():
            v = vals[i]
            if isinstance(v, complex):
                lines.append(f'"{name}" ({v.real:.15e} {v.imag:.15e})')
            else:
                lines.append(f'"{name}" {float(v):.15e}')
    lines.append("END")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------- the backend
class FakeBackend:
    """Analytic, clearly-labelled results.  Nothing is simulated."""

    name = "fake"

    def __init__(self, site=None, *, timeout_s: float | None = None):
        self.site = site
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ interface
    def available(self) -> tuple[bool, str]:
        return True, ("SYNTHETIC analytic results -- nothing is simulated; every run is tagged "
                      "engine=fake and every file says pmukit-fake")

    def submit(self, job) -> str:
        wd = pathlib.Path(job.workdir)
        raw = wd / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        deck = parse_deck(job.netlist_text)
        if not deck["analyses"]:
            job.state = "failed"
            job.detail = ("the deck carries no analysis statement, so there is nothing to "
                          "synthesize")
            return f"fake-{job.run.run_id}"
        written = []
        for an in deck["analyses"]:
            written.append(self._one(job, deck, an, raw))
        (wd / "spectre.log").write_text(
            self._log(job, written), encoding="utf-8", newline="\n")
        job.state = "done"
        job.detail = f"SYNTHETIC: wrote {', '.join(p.name for p in written)}"
        return f"fake-{job.run.run_id}"

    def poll(self, job) -> str:
        return job.state or "done"

    def fetch(self, job) -> pathlib.Path:
        job.log_path = pathlib.Path(job.workdir) / "spectre.log"
        return pathlib.Path(job.workdir) / "raw"

    def kill(self, job) -> None:
        return None

    # ------------------------------------------------------------------ synthesis
    def _cell_factor(self, job) -> float:
        """A small, deterministic, cell-dependent multiplier (0.95 .. 1.05).

        Without it every corner would come back byte-identical and a cell-mapping bug in the
        importer would be invisible -- the dataset would look perfectly filled and be wrong.
        """
        run = job.run
        temp = run.temp_c if run.temp_c == run.temp_c else 0.0     # NaN -> 0 (a swept run)
        key = f"{run.process}|{temp:g}|{run.vset}|{run.load_key}".encode("utf-8")
        h = int(hashlib.sha256(key).hexdigest()[:8], 16)
        return 1.0 + ((h % 1001) / 1000.0 - 0.5) * 0.1

    def _log(self, job, written) -> str:
        names = ", ".join(p.name for p in written)
        return ("pmukit fake backend -- SYNTHETIC RESULTS, NO SIMULATOR WAS RUN\n"
                f"run_id: {job.run.run_id}\n"
                f"cell:   {job.run.cell_text()}\n"
                f"analysis: {job.run.analysis}  stimulus: {job.run.stimulus}\n"
                f"wrote: {names}\n"
                "Time used: CPU = 1 ms, elapsed = 1 ms, util. = 100%.\n"
                "Peak memory used = 1 Mbytes.\n"
                "pmukit-fake completes with 0 errors, 0 warnings.\n")

    def _header(self, job, an) -> dict:
        return {"PSFversion": "1.00", "simulator": "pmukit-fake",
                "pmukit synthetic": "analytic result, NOT a simulation",
                "analysis type": an["kind"], "analysis name": an["name"],
                "pmukit run": job.run.run_id, "pmukit cell": job.run.cell_text()}

    def _one(self, job, deck, an, raw: pathlib.Path) -> pathlib.Path:
        kind = an["kind"]
        out = raw / f"{an['name']}.{_PSF_SUFFIX[kind]}"
        if kind == "ac":
            xs, traces, axis, unit, types = self._ac(job, deck, an)
        elif kind == "noise":
            xs, traces, axis, unit, types = self._noise(job, deck, an)
        elif kind == "dc":
            xs, traces, axis, unit, types = self._dc(job, deck, an)
        else:
            xs, traces, axis, unit, types = self._tran(job, deck, an)
        return write_psfascii(out, axis, unit, xs, traces, self._header(job, an), types)

    # -- which node does the hot source sit on, and what is it? ---------------
    def _hot(self, job, deck):
        stim = str(job.run.stimulus or "")
        src = deck["sources"].get(stim)
        net = src["nodes"][0] if src and src["nodes"] else ""
        return stim, src, net

    def _ac(self, job, deck, an):
        f0 = _f(an, "start", 10.0)
        f1 = _f(an, "stop", 1e9)
        dec = int(_f(an, "dec", 20))
        xs = _log_sweep(f0, f1, dec)
        k = self._cell_factor(job)
        stim, _src, hot_net = self._hot(job, deck)
        supply_hot = stim.startswith("VS_")
        traces: dict[str, list] = {}
        for sig in deck["saves"]:
            if sig.endswith(":p"):
                probe = sig[:-2]
                if probe == stim:                     # bias admittance: V(pin) = 1 V AC
                    traces[sig] = [-_yout(f, k) for f in xs]
                elif supply_hot:
                    traces[sig] = [_gdd(f, k) for f in xs]
                else:
                    traces[sig] = [complex(_TINY, 0.0) for _ in xs]
            elif sig == hot_net:                      # the injected rail: the isource SINKS 1 A
                traces[sig] = [-_zout(f, k) for f in xs]
            elif supply_hot:
                traces[sig] = [_psrr(f, k) for f in xs]
            else:
                traces[sig] = [complex(_TINY, 0.0) for _ in xs]
        return xs, traces, "freq", "Hz", _signal_types(traces)

    def _noise(self, job, deck, an):
        f0 = _f(an, "start", 10.0)
        f1 = _f(an, "stop", 1e8)
        dec = int(_f(an, "dec", 20))
        xs = _log_sweep(f0, f1, dec)
        k = self._cell_factor(job)
        current = "oprobe" in an                      # a bias current-noise run
        m = MODEL["noise_i"] if current else MODEL["noise_v"]
        white = (m["white_a_rthz"] if current else m["white_v_rthz"]) * k
        fc = m["corner_hz"]
        unit = "A/sqrt(Hz)" if current else "V/sqrt(Hz)"
        traces = {"out": [white * math.sqrt(1.0 + fc / f) for f in xs]}
        # The unit is written into the TRACE type, exactly where the importer looks for it.
        return xs, traces, "freq", "Hz", {"out": unit}

    def _dc(self, job, deck, an):
        k = self._cell_factor(job)
        start, stop = _f(an, "start", 0.0), _f(an, "stop", 1.0)
        n = int(_f(an, "lin", 5))
        xs = _lin_sweep(start, stop, n)
        traces: dict[str, list] = {}
        if an.get("param") == "temp":                 # the continuous temperature sweep
            for sig in deck["saves"]:
                if sig.endswith(":p"):
                    traces[sig] = [MODEL["iv"]["i0_a"] * k
                                   * (1.0 + MODEL["iv"]["tc_per_c"] * (t - 27.0)) for t in xs]
                else:
                    traces[sig] = [MODEL["dc"]["v0_v"] * k
                                   * (1.0 + MODEL["dc"]["tc_per_c"] * (t - 27.0)) for t in xs]
            return xs, traces, "temp", "C", _signal_types(traces)
        dev = str(an.get("dev") or "")
        if dev.startswith("VB_"):                     # bias I-V: the swept axis is the pin volts
            for sig in deck["saves"]:
                traces[sig] = [MODEL["iv"]["i0_a"] * k + MODEL["iv"]["g0_s"] * v for v in xs]
            return xs, traces, "dc", "V", _signal_types(traces)
        for sig in deck["saves"]:                     # rail load sweep: the axis is amps
            traces[sig] = [MODEL["dc"]["v0_v"] * k - MODEL["dc"]["rload_ohm"] * i for i in xs]
        return xs, traces, "dc", "A", _signal_types(traces)

    def _tran(self, job, deck, an):
        tstop = _f(an, "stop", 2e-5)
        npts = int(MODEL["tran"]["points"])
        xs = _lin_sweep(0.0, tstop, npts)
        k = self._cell_factor(job)
        stim, src, _net = self._hot(job, deck)
        t0, rising = _pwl_edge(src, tstop)
        enable = stim.startswith("VEN_")
        traces: dict[str, list] = {}
        for sig in deck["saves"]:
            base = MODEL["iv"]["i0_a"] * k if sig.endswith(":p") else MODEL["dc"]["v0_v"] * k
            if enable:
                traces[sig] = [_ramp(t, t0, base) for t in xs]
            else:
                traces[sig] = [_step(t, t0, base, rising, k) for t in xs]
        return xs, traces, "time", "s", _signal_types(traces)


def _signal_types(traces: dict) -> dict:
    """PSF type name per signal: a `:p` probe is a branch current, anything else is a node."""
    return {name: ("I" if name.endswith(":p") else "V") for name in traces}


# --------------------------------------------------------------------------- the analytic model
def _zout(f: float, k: float) -> complex:
    """Parallel RLC: a resistive floor with one resonance -- the shape a real rail has."""
    m = MODEL["zout"]
    w = 2.0 * math.pi * f
    y = 1.0 / (m["r_ohm"] * k) + 1.0 / (1j * w * m["l_h"]) + 1j * w * m["c_f"]
    return 1.0 / y


def _psrr(f: float, k: float) -> complex:
    m = MODEL["psrr"]
    return (m["h0"] * k) / (1.0 + 1j * f / m["fp_hz"])


def _gdd(f: float, k: float) -> complex:
    m = MODEL["gdd"]
    return (m["a_per_v"] * k) / (1.0 + 1j * f / m["fp_hz"])


def _yout(f: float, k: float) -> complex:
    m = MODEL["yout"]
    return m["g0_s"] * k + 1j * 2.0 * math.pi * f * m["cp_f"]


def _pwl_edge(src, tstop: float) -> tuple[float, bool]:
    """`(edge time, is the driven level rising)` from a pwl source, else (0.1*tstop, True)."""
    wave = str((src or {}).get("wave") or "")
    nums = [float(x) for x in re.findall(r"[-+0-9.eE]+", wave)
            if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", x)]
    if len(nums) >= 6:
        pairs = list(zip(nums[0::2], nums[1::2]))
        for (t_a, v_a), (t_b, v_b) in zip(pairs, pairs[1:]):
            if v_b != v_a:
                return t_a, v_b > v_a
    return 0.1 * tstop, True


def _step(t: float, t0: float, base: float, rising: bool, k: float) -> float:
    """A damped cosine settling back to `base` -- droop on a load step, overshoot on unload."""
    if t < t0:
        return base
    m = MODEL["tran"]
    dt = t - t0
    env = m["dv_v"] * k * math.exp(-dt / m["tau_s"]) * math.cos(2 * math.pi * m["ring_hz"] * dt)
    return base - env if rising else base + env


def _ramp(t: float, t0: float, base: float) -> float:
    """The enable ramp: flat at zero, then a first-order rise to `base`."""
    if t < t0:
        return 0.0
    return base * (1.0 - math.exp(-(t - t0) / MODEL["tran"]["tau_s"]))


def selftest() -> None:
    """Sanity: the model is the shape its docstring claims (used by tests/test_runner.py)."""
    peak = 1.0 / (2 * math.pi * math.sqrt(MODEL["zout"]["l_h"] * MODEL["zout"]["c_f"]))
    if abs(abs(_zout(peak, 1.0)) - MODEL["zout"]["r_ohm"]) > 1e-6:
        raise PmuError(
            what="The fake backend's Zout does not peak at its own resonance.",
            why="A parallel RLC must read exactly R at 1/(2*pi*sqrt(LC)); it did not, so MODEL "
                "and _zout() have drifted apart.",
            do=["Fix _zout() or MODEL['zout'] in pmukit/backends/fake.py."],
            where="pmukit/backends/fake.py")
