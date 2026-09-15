# pmukit

Behavioral **PMU / LDO model builder** for Cadence Spectre and ALPS harmonic-balance sims:
characterize a transistor-level PMU across PVT, fit per-rail Zout / PSRR / noise / bias-current
blocks plus opt-in large-signal terms, emit HB-safe Verilog-A, and prove it against the ground truth.

Successor of the (now private) `LDO_modeling` research repo. Method, tool facts and the list of
refuted approaches carry over; the interaction model, data contracts and GUI are rebuilt.
See `docs/REFACTOR_PLAN.md`.

## Data rule (non-negotiable)

This repo is public because the target box can only `git pull` public repos. Therefore:

- **No customer data in git.** No real netlists, PDK names, cell/net names, manifests, measured
  waveforms, fitted `.va` deliverables or screenshots of them. They live under `$PMUKIT_DATA`
  (outside the tree) on every machine.
- **A pre-commit guard** reads a local, git-ignored `.pmukit-denylist` (one identifier per line)
  and refuses any commit whose diff contains one of them.
- **Tests use synthetic devices only.** Real-part validation enters the docs as numbers, never names.

## Layout (planned)

```
pmukit/        the package: spec -> plan -> run -> fit -> emit -> verify
web/           browser front-end (single inlined HTML, served by a stdlib server; no CDN)
docs/          REFACTOR_PLAN.md, METHODOLOGY.md, TOOL_FACTS.md (carried over, curated)
tests/         synthetic-LDO regression (Spectre-native)
tools/         webprobe.py and other stand-alone helpers
```
