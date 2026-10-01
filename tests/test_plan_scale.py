"""Compiling a plan on a bench the size of a real one.

A real ADE export is a few MB. Every run deck is the exported netlist with a handful of
statements rewritten (the corner's section=, the code, the temperature, the load, the hot
source) and the analyses appended, and every one of those rewrites used to re-read the whole
deck: O(runs x edits x netlist), minutes on the Plan screen for a 3 MB bench. Pinned here:

* the decks do not change. Every run's netlist text, run_id, recipe and feeds, and the plan's
  notes, are compared with goldens recorded from the compiler BEFORE it was made fast
  (`tests/fixtures/plan_golden.json`; regenerate only on a deliberate change of the decks with
  `python -m tests.test_plan_scale --regen`). run_id is a content hash: a changed deck would
  orphan every finished run in every user's cache.
* the deck now keeps the statements it read and rewrites them in place; on random decks of
  awkward statements, every rewrite agrees with re-reading the whole text at every step (the
  old way, kept here as `Reread`).
* a padded bench compiles in seconds (wall-clock guard, `PMUKIT_SKIP_TIMING=1` turns it off).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time

import pytest

from pmukit import netlist as netlist_mod
from pmukit.config import ProjectConfig, derive
from pmukit.errors import PmuError
from pmukit.jsonio import sha_bytes
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.site import SiteConfig
from tests.bigbench import bench_text

FIX = pathlib.Path(__file__).parent / "fixtures" / "pmu_demo"
GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "plan_golden.json"
DEMO_TEXT = (FIX / "input.scs").read_text(encoding="utf-8")

timing = pytest.mark.skipif(os.environ.get("PMUKIT_SKIP_TIMING") == "1",
                            reason="PMUKIT_SKIP_TIMING=1: wall-clock guards are off here")

DEMO_PORTS = {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
              "VDD0P8_C": "model", "IB_PTAT": "model", "IB_POLY": "model", "EN": "model",
              "TESTMODE": "ignore", "VSS_A": "ignore", "VSS_B": "ignore", "AGND": "ignore"}
DEMO_LOAD = {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True},
             "VDD0P8_B": {"on_a": 2e-3, "off_a": 2e-6, "switches": True},
             "VDD0P8_C": {"on_a": 1e-4, "off_a": 2e-6, "switches": True}}


def _cfg(**over) -> dict:
    d = {"project": "scale", "netlist": "input.scs", "pmu_inst": "PMU_TOP",
         "corners": ["tt"], "temps_c": [-40, 25, 125], "vset_codes": [3],
         "ports": dict(DEMO_PORTS), "my_load": dict(DEMO_LOAD), "care_up_to_hz": 1e10}
    d.update(over)
    return d


def pad(text: str, n_lines: int, *, before: str = "PMU_TOP (") -> str:
    """`text` with an `n_lines`-device dummy subckt in front of the line starting `before` --
    the bulk of a real export is subcircuit bodies the run decks never touch. Every 7th device
    is wrapped across a backslash continuation, as ADE wraps long instance lines."""
    body = ["subckt pad_dummy (a b c)"]
    for i in range(n_lines):
        if i % 7 == 3:
            body.append(f"    r{i} (n{i} n{i + 1}) resistor \\")
            body.append(f"        r={i + 1}")
        else:
            body.append(f"    r{i} (n{i} n{i + 1}) resistor r={i + 1}")
    body.append("ends pad_dummy")
    i = text.index(before)
    return text[:i] + "\n".join(body) + "\n\n" + text[i:]


def nasty(text: str) -> str:
    """The demo bench with what an export can throw at the rewriter: no `parameters VSET` and no
    `options temp=` (both get declared near the top), a convention source wrapped over two
    lines with a trailing comment, a supply left at mag=1, a bias whose mag= sits on its
    continuation line, a wrapped analysis, a parameters line wrapped, trailing blank lines."""
    text = text.replace("parameters VSET=3\n", "parameters rsc=1 \\\n    csc=1\n")
    text = text.replace("simOpts options temp=27 tnom=27\n", "")
    text = text.replace("IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u\n",
                        "IL_VDD0P8_A (VDD0P8_A 0) \\\n    isource dc=500u   // the A load\n")
    text = text.replace("VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0\n",
                        "VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0 mag=1\n")
    text = text.replace("VB_IB_PTAT  (IB_PTAT 0)  vsource dc=0.4\n",
                        "VB_IB_PTAT  (IB_PTAT 0)  vsource dc=0.4 \\\n    mag=0\n")
    text = text.replace("acZ  ac start=10 stop=1G dec=10\n",
                        "acZ  ac start=10 \\\n   stop=1G dec=10  // wrapped\n")
    return text + "\n\n\n"


def _big_bench(n_leaves: int) -> tuple[str, dict]:
    """tests.bigbench's synthetic bench (~30 convention sources), at a chosen size."""
    text = bench_text(n_leaves=n_leaves, dev_per_leaf=60, n_blocks=12)
    ports = {f"VRAIL{i}": "model" for i in range(2)}
    ports.update({f"IBIAS{i}": "model" for i in range(3)})
    ports.update({"VSUP0": "model", "EN0": "model"})
    cfg = {"project": "bigb", "netlist": "input.scs", "pmu_inst": "I_PMU", "corners": ["tt"],
           "temps_c": [25, 125], "vset_codes": [3, 0], "ports": ports,
           "my_load": {"VRAIL0": {"on_a": 1e-3, "off_a": 1e-6, "switches": True}},
           "care_up_to_hz": 1e9}
    return text, cfg


#: name -> (netlist text, config dict, engine). The netlist "lives" in the demo fixture
#: directory, so its pdk/ includes resolve (sections are verified) and, for an engine that
#: makes them absolute, the absolute path is that directory (normalized before hashing).
def scenarios() -> dict:
    big_text, big_cfg = _big_bench(6)
    return {
        "demo": (DEMO_TEXT, _cfg(corners=["tt", "ss"], vset_codes=[3, 0, 7]),
                 "spectre_ssh"),
        "demo_abs": (DEMO_TEXT, _cfg(corners=["tt", "ss"], vset_codes=[3, 1]), "dry_run"),
        "demo_composite": (DEMO_TEXT, _cfg(corners={"tt": {"toplevel.scs": "tt",
                                                           "rc.scs": "typ"},
                                                    "ss_rcff": {"toplevel.scs": "ss",
                                                                "rc.scs": "ff"}},
                                           temps_c=[25]), "dry_run"),
        "nasty": (nasty(DEMO_TEXT), _cfg(corners=["tt", "ff"]), "dry_run"),
        "nasty_padded": (pad(nasty(DEMO_TEXT), 300), _cfg(temps_c=[25, 125]), "spectre_ssh"),
        "padded": (pad(DEMO_TEXT, 2000), _cfg(), "spectre_ssh"),
        "bigbench": (big_text, big_cfg, "spectre_ssh"),
    }


def build(name: str):
    text, cfg_d, engine = scenarios()[name]
    site = SiteConfig(engine=engine)
    nl = Netlist(text, FIX / f"{name}.scs")
    cfg = ProjectConfig.from_dict(cfg_d)
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, site)
    return cfg, der, nl, pins, site


def _norm(s: str) -> str:
    return s.replace(FIX.resolve().as_posix(), "<FIX>").replace(FIX.as_posix(), "<FIX>")


def _h(s: str) -> str:
    return hashlib.sha256(_norm(s).encode("utf-8")).hexdigest()[:12]


def digest(name: str) -> dict:
    """What a plan IS, path-independent: one line per run -- its run_id, its deck, and the rest
    (recipe, cell, reads, feeds, group, cost) -- plus the plan's notes and groups."""
    cfg, der, nl, pins, site = build(name)
    plan = compile_plan(cfg, der, nl, pins, site=site)
    # With absolute includes the deck names this checkout's path, and so does the run_id: the
    # deck is compared normalized there, and the run_id only where it is portable.
    absolute = site.engine != "spectre_ssh"
    runs, cells = [], []
    for g in plan.groups:
        for r in g.runs:
            run = r.run
            assert run.netlist_sha == sha_bytes(r.netlist_text.encode("utf-8"), 12)
            rest = json.dumps([_norm(run.recipe), run.process,
                               None if run.temp_c != run.temp_c else run.temp_c, run.vset,
                               run.load_key, run.analysis, run.stimulus, list(run.reads),
                               [list(f) for f in r.feeds], g.id, r.check, r.cost_s])
            runs.append(f"{'-' if absolute else run.run_id} {_h(r.netlist_text)} {_h(rest)}")
            cells.append(f"{g.id} @ {run.process} {run.temp_c} {run.vset} {run.load_key}")
    return {"notes": [_norm(n) for n in plan.notes],
            "groups": [[g.id, g.title, g.enabled] for g in plan.groups], "runs": runs,
            "_cells": cells}


@pytest.mark.parametrize("name", sorted(scenarios()))
def test_decks_and_run_ids_match_the_recorded_plan(name):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[name]
    got = digest(name)
    assert got["notes"] == golden["notes"]
    assert got["groups"] == golden["groups"]
    assert len(got["runs"]) == len(golden["runs"])
    for g, w, cell in zip(got["runs"], golden["runs"], got["_cells"]):
        assert g == w, f"{name}: the run {cell} changed (run_id, deck, rest)"


def test_run_ids_stay_unique_and_bound_to_the_deck():
    cfg, der, nl, pins, site = build("padded")
    plan = compile_plan(cfg, der, nl, pins, site=site)
    runs = plan.runs(enabled_only=False)
    assert len({r.run_id for r in runs}) == len(runs)
    again = compile_plan(cfg, der, nl, pins, site=site)
    assert [r.run_id for r in again.runs(enabled_only=False)] == [r.run_id for r in runs]
    assert [r.netlist_text for r in again.runs(enabled_only=False)] == \
        [r.netlist_text for r in runs]


# ------------------------------------------------- the kept statements vs re-reading the text
class Reread(Netlist):
    """The rewriting as it was before the deck kept its statements: every step re-reads the
    whole text and writes the whole text back. The reference the fast paths must agree with."""

    def _units(self):
        return netlist_mod._scoped_units(self.text.splitlines())

    def _index(self):
        u = self._units()
        self._u, self._idx, self._n = u, None, 0      # read by _find, never kept
        return Netlist._index(self)

    def copy(self):
        n = Reread(self.text, self.path)
        n.origin, n.label = self.origin, self.label
        n.edits, n.corner_choice = list(self.edits), dict(self.corner_choice)
        return n

    def _rewrite_statement(self, match, transform, *, kind="~", heads=None):
        out, done = [], False
        for logical, phys, depth in netlist_mod._scoped_logical_lines(self.text):
            if not done and depth == 0 and match(logical):
                done = True
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

    def strip_analyses(self):
        out = []
        for logical, phys, depth in netlist_mod._scoped_logical_lines(self.text):
            if depth == 0 and netlist_mod._is_analysis_statement(logical):
                for raw in phys:
                    body = raw.rstrip()
                    if body.endswith("\\"):
                        body = body[:-1].rstrip()
                    out.append(netlist_mod.STRIP_MARKER + body)
                self.edits.append(f"- {logical.strip()}")
            else:
                out.extend(phys)
        self.text = "\n".join(out)
        return self

    def _insert_line(self, line):
        return False                                  # the text-splitting way, below it

    def _append_line(self, line):
        return False


#: Statements an export can hold, the awkward ones included: wrapped lines (also into the end
#: of the file), a `simulator` statement on one line and over several, subckt brackets at the
#: top level, comments, blank and whitespace-only lines, a form feed inside a line.
PIECES = [
    "simulator lang=spectre", "simulator lang=spice", "  simulator lang = spice  ",
    "simulator", "lang=spice", "simulator lang=", "// comment", "* star comment", "", "",
    "   ", "global 0", "parameters VSET=3", "parameters A=1 \\", "   B=2", "parametersX VSET=9",
    'include "pdk/toplevel.scs" section=tt', 'include "pdk/rc.scs" section=typ',
    "subckt foo (a b)", "  IL_A (a 0) isource dc=1 mag=1", "ends foo", ".subckt dc a b",
    "IL_A (A 0) isource dc=500u   // load", "IL_A (A 0) \\", "    isource dc=1u mag=0",
    "VS_X (X 0) vsource dc=1 mag=1", "VB_Y (Y 0) vsource dc=0.4 \\", "  mag=0",
    "simOpts options temp=27 tnom=27", "dcOp dc write=\"op\"", "acZ ac start=1 \\",
    "  stop=1G", "x \\", "\\", "save A B", "foo\x0cbar",
]


def _ops(rng, nl):
    for _ in range(rng.randint(1, 10)):
        op = rng.randrange(10)
        try:
            if op == 0:
                nl.set_dc(rng.choice(["IL_A", "VS_X", "VB_Y", "nope"]), rng.random())
            elif op == 1:
                nl.set_mag(rng.choice(["IL_A", "VS_X", "VB_Y"]), rng.choice([0, 1]))
            elif op == 2:
                nl.set_pwl(rng.choice(["IL_A", "VS_X"]), "0 0 1 1")
            elif op == 3:
                nl.set_param(rng.choice(["VSET", "B", "Q"]), rng.randint(0, 9))
            elif op == 4:
                nl.set_temperature(rng.choice([-40, 25, 125.5]))
            elif op == 5:
                nl.set_section(rng.choice(["toplevel.scs", "rc.scs"]), "ss")
            elif op == 6:
                nl.strip_analyses()
            elif op == 7:
                nl.append(rng.choice(["acz ac start=1", "", "x \\", "simulator lang=spice"]))
            elif op == 8:
                nl = nl.copy()
            else:
                nl.text = nl.text + rng.choice(["", "\n", "\nIL_A (A 0) isource dc=3"])
        except PmuError as e:
            yield f"refused: {e.what}", None
        if not isinstance(nl, Reread):           # what it keeps is what its text reads as
            assert [x[:3] for x in nl._units()] == \
                [x[:3] for x in netlist_mod._scoped_units(nl.text.splitlines())]
        yield (nl.text, list(nl.edits), nl.render(), nl.includes(), nl.parameters(),
               nl.analyses(), list(nl.instances(0)), list(nl.instances(None)))


def test_rewrites_on_the_kept_statements_match_rereading_the_text():
    """Random decks of awkward statements, random rewrites: the text, recipe, rendering and what
    is read back are those of re-reading the whole text at every step -- and what the deck
    keeps of itself is always what re-reading its text gives."""
    import random
    rng = random.Random(7)
    for seed in range(600):
        text = "\n".join(rng.choice(PIECES) for _ in range(rng.randint(0, 14)))
        text += rng.choice(["", "\n", "\n\n", " \n", "\r\n"])
        ops = list(_ops(random.Random(seed), Netlist(text, "x/input.scs")))
        ref = list(_ops(random.Random(seed), Reread(text, "x/input.scs")))
        assert ops == ref, f"seed {seed}: {text!r}"


@pytest.mark.timing
@timing
def test_a_padded_bench_compiles_in_seconds():
    """20k dummy devices (~0.9 MB) in front of the testbench, 3 temperatures x the load states
    x every family: 163 runs. ~34 s before, ~0.2 s now. The bound is generous on purpose -- it
    catches the per-edit full re-read coming back, not jitter."""
    text = pad(DEMO_TEXT, 20000)
    site = SiteConfig(engine="spectre_ssh")
    nl = Netlist(text, FIX / "padded20k.scs")
    cfg = ProjectConfig.from_dict(_cfg())
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    der = derive(cfg, pins, site)
    t0 = time.perf_counter()
    plan = compile_plan(cfg, der, nl, pins, site=site)
    dt = time.perf_counter() - t0
    n = len(plan.runs(enabled_only=False))
    assert n > 100
    assert dt < 10.0, f"compiling {n} runs on a 20k-line bench took {dt:.1f} s"


if __name__ == "__main__" and "--regen" in sys.argv:
    out = {}
    for n in sorted(scenarios()):
        out[n] = digest(n)
        out[n].pop("_cells")
    GOLDEN.write_text(json.dumps(out, indent=0) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {GOLDEN}")
