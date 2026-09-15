# pmukit

Turn a transistor-level **PMU** (LDO rails + current biases) into a behavioral model you can put in
your own harmonic-balance, phase-noise and transient simulations — one that runs fast, converges,
and is honest about where it stops being trustworthy.

You hand it **one** Spectre netlist, built to a naming convention, exported at the nominal corner.
It works out which simulations are actually needed, runs them, fits per-rail Zout / PSRR / noise and
per-bias I(V,T) / admittance / current-noise blocks, emits one HB-safe Verilog-A per process corner
inside a `.scs` section library, and tells you — in your words, not in fit residuals — which loads,
temperatures, frequencies and corners the model is backed by measurement for.

Successor to the (now private) `LDO_modeling` research repo. The method, the tool scars and the
list of **refuted** approaches carry over unchanged; the data contracts, the orchestration and the
interface are rebuilt. See `docs/REFACTOR_PLAN.md` for why.

---

## Five minutes from a netlist to a plan

```bash
bash apply                                 # on the box: offline install, prints the tcsh setenv lines
pmukit ui                                  # or drive it from the shell:

pmukit check tb/input.scs --pmu-inst PMU_TOP          # does my bench satisfy the convention?
pmukit new mypmu --netlist tb/input.scs --pmu-inst PMU_TOP \
    --corners tt,ss,ff --temps -40,25,125 --vset 3 --care-up-to 2e10 \
    --load VDD_A=5e-4,2e-6 --note "RX mode, reg 0x12=0x03"
pmukit plan mypmu                          # what will run, why, and what it costs
pmukit plan mypmu --submit && pmukit run mypmu
pmukit fit mypmu && pmukit verify mypmu
pmukit deliver mypmu                       # the .scs library, the .va per corner, the report
```

Everything the web shell does, the CLI does — that is what makes the `$ pmukit …` echo strip at the
bottom of every screen an honest answer to "what did I just do?".

**Start here:** `docs/TESTBENCH.md` — the naming convention is the entire interface between you and
the tool, and it is one page.

## The three questions

The tool is aimed at a sub-block designer for whom the LDO is a black box. It asks exactly three
things and derives the rest:

1. which process corners, temperatures and VSET codes,
2. what **your** module draws — on-state current, off-state current, and how fast it switches,
3. how high in frequency you care.

Everything else — the load grid, the sweep density, the noise band, the transient window, which
simulations can be merged — comes out of the model spec and your netlist. `pmukit config <p>
--derived` shows the derived configuration, and every field in it says which rule produced it.

## How it is put together

```
project config ──> model spec ──> measurement plan ──> run ledger ──> dataset ──> fit ──> deliverable
   (0a/0b)         (contract 1)     (derived)          (contract 3)  (contract 2)         (contract 4)
                                        └──────── digest (contract 5): the air-gap way home ────────┘
```

Five contracts, written before the code and in `docs/CONTRACTS.md`:

| | what it pins down |
|---|---|
| **0a / 0b** `config.py` | the three questions, and every derived characterization setting with its provenance |
| **1** `spec.py` | the fixed physics inventory: which blocks each port type has, which observable and which axes each parameter needs, which tier it is in |
| **2** `dataset.py` | dimensioned measurements — `process × temp × vset × load × sweep`. NaN means no data, and the fitter can tell "never run" from "ran and broke" |
| **3** `ledger.py` | one SQLite row per simulation, with the parameters it feeds. `run_id` is a content hash, so re-planning an unchanged run collides with the finished one — that *is* resume |
| **4** `deliverable.py` | the `.scs` section library, one `.va` per corner, the validity envelope, the report and the provenance |
| **5** `digest.py` | plain text you paste from the box back to the desk, truncated by priority with the dropped blocks named — never silently |

Two properties are worth calling out because they are what the old flow could not do:

- **"Why does this run exist?" is a lookup, not a story.** Every planned run carries the
  `(port, block, parameter)` triples it feeds; the ledger stores them; the Plan screen reads them
  back. Untick a group and the tool lists exactly which parameters go unmeasured and will be
  reported NOT RUN.
- **Simulations that one run can produce together are merged.** One supply injection reads every
  port, so the rail PSRR and the bias PSRR are literally the same simulation.

## Data rule (non-negotiable)

This repo is public, because the target box can only `git pull` public repos. Therefore:

- **No customer data in git.** No real netlists, PDK names, cell or net names, measured waveforms,
  fitted `.va` deliverables, or screenshots of them. They live under `$PMUKIT_DATA` (default
  `~/pmukit_data`), outside the tree, on every machine.
- **A commit gate** (`tools/guard.py`, wired in as a pre-commit hook) reads a local, git-ignored
  `.pmukit-denylist` and refuses any commit that contains one of those identifiers. When it fires,
  the fix is the file, never the gate.
- **Tests use synthetic devices only.** Real-part results enter the docs as numbers, never names.

## Layout

```
pmukit/        the package: config · spec · netlist · plan · ledger · runner · dataset ·
               fit · emit · verify · deliverable · digest · server · cli
pmukit/web/    the browser front-end: one inlined HTML file, stdlib server, no CDN, no Qt
docs/          CONTRACTS · REFACTOR_PLAN · TESTBENCH · UX_RULES · CONTEXT_OF_USE · DECISIONS
deploy/        offline package for the air-gapped box (manylinux2014 wheels, `bash apply`)
design/        the approved screen designs the web shell is built from
tests/         synthetic PMU + 14 synthetic LDOs, Spectre-native
tools/         the commit gate, the testbench template, the SKILL generator, webprobe
```

## Requirements

Python 3.11, numpy and scipy — **and nothing else at runtime**. Simulation is Spectre (locally over
ssh, or ALPS on a Donau queue); ngspice is retired and there is one Verilog-A emitter, not two.

## Where the decisions went

`docs/DECISIONS.md` — one line per decision: what was decided, why, and how hard it is to reverse.
`BUILD_REPORT.md` — what was built, what was measured, and what was deliberately left undone.
