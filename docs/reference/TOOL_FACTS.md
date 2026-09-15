# TOOL_FACTS — environment & tool pitfalls (with the exact fix)

> **Ported from the private `LDO_modeling` repo (@ d2c5b80), with customer identifiers replaced.**
> This repo is public, so real cell, net and project names were substituted throughout. The
> substitutions are mechanical and consistent, so the engineering content is unchanged:
>
> | in the original | here |
> |---|---|
> | the real part's name | "the real part" |
> | its two modelled rails | `VDD0P8_A` (rail A) / `VDD0P8_B` (rail B) |
> | their returns | `VSS_A` / `VSS_B` |
> | its analog supply | `VDDA_1V0` |
> | its bias references | `IB_PTAT_B` / `IB_POLY_B` / `IB_POLY_C` |
> | the package model | `PKG_NPORT` |
> | library and path names | `<work-lib>`, `<install-prefix>`, ... |
>
> Numbers, measurements and verdicts are verbatim.


> Durable archive. Each entry: the pitfall → the exact fix. Curate-only, no status.

## Spectre / Verilog-A

- **Compile VA only with `-64`.** `spectre -64` / `ahdlcmi -64`; else it compiles `gcc -m32` and dies on `gnu/stubs-32.h`. Local Spectre 18.1 binary `/home/yusheng/Program/eda/cadence/SPECTRE181/bin/spectre`, env in `cadence/spectre_run.py::_env()`, gate with `spectre_run.available()`. (Spectre 18.1.0.077 / SPECTRE181, Virtuoso IC618, skillbridge 1.8.0.)
- **Compile `.va` on COPIES in a scratch dir.** openvaf/lld drops an import-library `<name>.lib` beside its output → clobbers the emitted SPICE model if you compile in `model/`.
- **No `laplace_nd`** — synthesize passive RLC + controlled sources only (PSS/HB-robust); use native `white_noise`+`flicker_noise` for exact 1/f (valid in pnoise/hbnoise).
- **OpenVAF `$table_model` unsupported** (rc=65) → inline the dropout as closed-form sum-of-max PWL (== 1-D linear interp). `parameter string` table path crashes (rc=101 panic) → emit the absolute path literally, or inline. `$table_model` writes a BARE filename resolved against the run dir → embed the emit-time ABSOLUTE path.
- **VA `$temperature` is KELVIN** (328.15 = 55°C); `emit_pmu_model` uses `$temperature-328.15`, the `emit_isrc` ngspice twin uses degC `temper-55` — they MUST move in lockstep or crossval diverges.
- **VA numeric:** `(V/vk)^p` blows the OP Jacobian at Vo=0 when p<1 → sqrt-floor the base.
- **Emitted `.va` PSRR sign was inverted 180°:** `I(vout)<+X` removes current FROM vout but the `.lib` mirror `Gd 0 vout` injects INTO it → negate the PSRR `I(vout)<+` contributions in `emit_va` (spur tones were already negated; PSRR was missed). Only `ldo_model.va` was hand-fixed — regenerating the other `model/*.va` (esp. v4_ffpsrr) is an open TODO.
- **Emitted PMU module ground is VSS** — tie to 0 in any local TB/model or it floats to −100 MV. Split grounds are supported: `emit_pmu_va(..., port_grounds={pin: net})` gives each rail/bias its own return pin (the real part: VDD0P8_A→VSS_A, VDD0P8_B→VSS_B, IB_*→AGND).
- **Synthesized PSRR complex-section inductor blows up HB ("matrix singular during decomposition").** The complex 2nd-order PSRR section is realized as series R-L-C with `Lpc=(1/pcw0²)/Cpc`; the historical hard-coded `Cpc=1 pF` forces **thousands of henries** for a low `pcw0` (real rail B `pcw0≈1e4` → **Lpc≈8890 H**, PLL≈5 H). In a GHz-oscillator `oschb` the harmonics reach ~77 GHz (16× a 4.84 GHz VCO); that inductor's `~1/(jωL)` branch admittance (≈4e15 Ω) underflows against O(1) terms → a **singular Jacobian** ("complex fullfactor fail … singularCol", divergence pinned to `L*.flow2` at `harm=16`, Δ~1e41). The section transfer is EXACTLY invariant to `Cpc` (pure impedance scaling), BUT `Cpc` returns into the finite-Zout `vrf` tracker (Rtrk/Ctrk=1 nF) so scaling it naively LOADS `vrf` and moves PSRR (measured **78 dB** error). Fix (`emit_pmu_model.py`, **default-ON** since it's a footgun; `hb_robust=False` = the old-bytes escape hatch): scale `Cpc` so `Lpc→~1 mH` AND buffer the section's reference to a zero-Zout copy `vrfb` so the larger cap draws from the buffer not `vrf` → PSRR preserved to **<0.01 dB**, DC/AC/noise unchanged, HB well-conditioned. Real-pole caps (`Cps`) stay 1 pF (RC only, well-conditioned; scaling them would also load `vrf`). `harness/test_emit_hb_robust.py`. **Standalone driven-HB is too easy to expose this** (a near-LTI PMU converges without the oscillator limit cycle) — reproduce/verify only in the real `oschb`. **Twin fixed in `fit_model.emit_va` + `emit` (the crossval single-block `.va`/`.lib`), also default-ON:** there `vrf` is an IDEAL source (`V(vrf,gnd)<+vdd` / SPICE `Vrf`), so the cap does NOT load it -> just scale `Cpc` so `Lpc~1 mH`, NO buffer, EXACTLY transfer-invariant (Spectre AC on/off = 0.000 dB). `hb_robust=False` on both paths restores the old 1 pF. Committed `results/crossval/*` are regen-on-demand output (no byte-lock) so they just re-emit HB-safe on the next crossval run.
- **That Cpc-rescale is NECESSARY but NOT SUFFICIENT for the real `oschb` — the complex section needs a gm-C realization, and two more PSRR paths bite.** The complex section's Jacobian dynamic range at harm N is `(f_N/f0)²` — real rail B: 77 GHz / `pcw0`≈1.7 kHz ≈ **2.1e15**, INVARIANT to the Cpc/Lpc split. Rescaling only relocates the extreme reactance from L (`Lpc`≈8890 H → underflow → singular, the old 1 pF `L*.flow2`) to C (`Cpc`≈8.89 µF → `ωC`≈4.3e6 S → **NaN** `@…K0.flow4 harm16`). Robust realization = a **gm-C biquad**: 1 pF caps + `gm=C·w0` transconductors set the kHz pole, so pole-freq is DECOUPLED from element size → every internal-node admittance O(≤1) S at 77 GHz (4.3e6 → 0.487 S), transfer identical. The real oschb then exposed TWO more model-side HB killers that a driven-HB / AC never shows: **(a)** a rational PSRR fit can emit a **large near-cancelling first-order doublet** (real rail B `G1=+17.23 / G3=−17.23`, poles 0.004 % apart, DC-cancel to 0.3 %) → a ±17.2 S near-null-space direction in the SHARED supply-node harmonic Jacobian → `singularCol`/`Resdl=nan` surfaces at whatever SHARES the node (package nport `PKG_NPORT`, real `PMU_TOP` transistors), NOT at the model → consolidate into ONE small-coeff gm-C biquad; **(b)** the **flat-to-∞ `G0` supply→vout injection** → band-limit at ~2 GHz. All three transfer-preserving (desk AC ≤0.01 dB in-band). Currently HAND-EDITED in `cadence/real_tb/PMU_model_splitgnd_hbrobust.va` (desk, UNTRACKED); NOT yet in `emit_pmu_model.py`. Root-cause + reconstruction + box trail → `docs/threads/hb-oschb-conditioning.md`.
- **Sink PSRR sign:** probe reads `i(vout) = −I_pin` → `gdd_eff = −gdd` (sink) / `+gdd` (source); source drives `I(supply,o)`, sink drives `I(o,gnd)`. importmp stores `pi = −I/Vsup`; emit fits gdd on `−PI`; report must negate to match the `.va`.

## ngspice

- **Built from source** at `~/.local/bin` (v46) — EPEL el8 has no package. `AC_PREREQ([2.69])`, `make LIBS=-lstdc++`. Found via `$NGSPICE` → bundled exe → PATH.
- **BSIM3 → Spectre:** ngspice `level=8` → Spectre `level=49` (8=generic mos8, rejects BSIM3); strip `{param}` braces → bare in subckt body; instance in SPICE lang (`xdut`), stimuli in spectre-lang.
- **`.param` names are CASE-INSENSITIVE** — noise g1/g2/g3 silently overwrote PSRR G1/G2/G3 (35× gain). Fix: rename gnw/gn1..gn6.
- **`ng.amps()` suffix parse:** `float(il.replace("u","e-6"))` crashed on mA corners → use the canonical p/n/u/m/k parser at all 6 sites.

## ALPS / Donau (cluster, red zone)

- **Validated run:** `dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000" -x all -EP <netdir> -J /software/empyrean/alps/2026.03.hf1/bin/alps input.scs -format ps -o <psf>/<tag> -I <pdk>/alps -ahdllibdir <ahd> -mt 8 -ade`.
- **Call the WRAPPER `.../bin/alps`, not the raw binary** (raw fails `libsvadv.so`; the wrapper sets LD_LIBRARY_PATH).
- **`-format ps` = classic PSF** (hidden flag; ADE's psfxl downgraded to ps for ALPS) → binpsf reads it unchanged. Never `psfxl`.
- **`-ade`** = ADE output names (ac.ac / noise.noise) + the 0-byte `.simDone` completion sentinel; without it, native `.fd/.td` + logFile index.
- **`-mt 8` MUST equal Donau `cpu=8`.** `-x all` propagates the submit-shell env (FlexLM `LM_LICENSE_FILE`) to the node — required for CLI licensing.
- **PDK `-I` is a DIRECTORY** (`$MODEL_ROOT` → `-I $MODEL_ROOT/alps`); a `toplevel.scs` FILE is wrong (→ `-I .../toplevel.scs/alps`). Consumer = `cadence/cluster/alps_cli.py`. Pass only `-I <pdk>/alps` so `include "toplevel.scs"` resolves to the `.alps` selector (never let the spectre tree win on an ALPS run).
- **`dsub --json`** returns `{"data":{"jobId":"…"}}` (numeric-only; ignore requestId).
- **ALPS-keyword caveat:** deck statements (`options temp=`, `noise oprobe=`) are Spectre-18.1-validated but ALPS-keyword-UNVERIFIED — if ALPS silently no-ops, the panel comes up wrong with NO error. After a real run, sanity-check PTAT Idc(T) slopes + non-blank current-noise; the fix lives in `netlist_augment`, not the manifest. (The `nz oprobe=V10 noise...` token-order bug was exactly this: param before the `noise` type → parse error. Correct = `nz {noise} oprobe={probe}`.)

## ADE / skillbridge

- **`axlGetRunStatus` returns `(completed,total)` POINTS, not an idle code.** A finished run RESTS at `(N,N)`, not `[0,0]` — the famous "run hangs" was a misread status. Poll PER-HISTORY (`insituHistStatus`), not the session aggregate (it poisons after renames). Locate PSF via `axlGetResultsLocation`.
- **ADE ASSEMBLER-1610/1707** (missing per-test vars + disabled analyses): design vars are per-test not globals → inherit the OP via the `axlGetToolSession → asiGetSession` bridge (`asiGetDesignVarList`/`asiAddDesignVarList`), then `asiSetAnalysisFieldVal` + `asiEnableAnalysis`.
- **ADE field names:** ac uses `start/stop/dec` (not from/to). Noise needs `outType='voltage'` + `p=net,n=gnd!` + clear `oprobe` + `inType='none'`; the default `outType='probe'` emits `oprobe=<net>` → SFE-1997.
- **ADE session degrades** after ~6 runs + history renames (runs slow 3 s → 197 s, rename-collision modals) → don't rename histories; **Session → Reset** fully recovers.
- **Fresh-session `axlGetCurrentHistory` returns 0**, which is TRUTHY in SKILL → guard in `insituCurHist`/`_cur_hist()`.
- **skillbridge is live-Virtuoso-only** — import it LAZILY inside live functions (eager import crashed the airgapped deploy). A modal Virtuoso dialog WEDGES the whole channel (even `plus(2,3)` times out); killing the client doesn't abort the in-Virtuoso call → close the dialog or restart Cadence. Agents can't pop X11/Qt/CIW — the USER must launch Virtuoso + load the SKILL helpers per session (resolve_nets.il, pmu_top_symbol.il, ldo_cellview.il).
- **Currents need explicit save** (`probe:p`), not `allpub`.
- **OCEAN standalone binary is OS-broken on this box** (`sysname` → "unknown" on RHEL8) → run analyses in the live ADE session, feed PSF/CSV through `import_cadence.py`. Tight CLI loop (no GUI): `spectre tb.scs +escchars =log run.log -format psfascii -raw ./psf`; headless OCEAN `ocean -nograph -replay run.ocn`.

## PSF / binpsf

- **ADE/cluster write BINARY PSF**; `cadence/psf.py` was ASCII-only → standalone big-endian `cadence/binpsf.py`, `psf.read_psf` auto-dispatches on bytes; per-instance STRUCT noise traces handled. 5 sections, big-endian header `…PSFversion…BINPSF…`.
- **PSF axis names (confirmed local Spectre):** dc-sweep axis = `'dc'`, transient axis = `'time'`. `-format psfbin` (binary) / `-format psfascii` (ascii).
- **Windowed transient PSF** (`PSF window size != 0`) is signal-major buffered with a NaN-padded last window — reverse-engineered reader in binpsf.
- **Grouped PSF (groups=1)** is NOT an error wall — it just means every device noise contribution was saved; the VALUE section is the same flat per-point layout, `out` is the last decl (scalar real). Read it directly by constant stride; don't crawl the whole file. (See DATA.md §15 for the exact byte layout.)

## Deploy / install / shell

- **Red box is tcsh:** `VAR=val` errors → use `$PWD`; backticks/`|&`/`set`. `/opt` is unwritable on the shared box → install self-contained under one user folder.
- **Install PREFIX** = `/data/RFIC3/<project-area>/w84368867/workarea/LDO_modeling`; update via `bash apply` (auto-detects incremental/full). `~/.ldo_modeler/` is the GUI config dir (distinct). skillbridge==1.8.0 in `.venv`.
- **glibc-2.17 wheel audit:** Windows `pip download` succeeds but CentOS7 import dies `GLIBC_2.28 not found` → cross-download `--platform manylinux2014/_2_17`, REJECT any `_2_28/_2_31/_2_34`. `PyQt5-Qt5` must be `5.15.2` (5.15.11+ needs glibc 2.28).
- **Qt ↔ Cadence conflict:** Virtuoso puts a conflicting `libQt5Core.so.5` on `$LD_LIBRARY_PATH` (`/software/public/qt/5.15.3_xcb/lib`) → PyQt5 dies `symbol _ZdaPvm, version Qt_5`. Fix = prepend the wheel's `PyQt5/Qt5/lib`; launch the GUI from a CLEAN shell (separate process over the skillbridge socket).
- **`deploy/apply` must be LF** (`.gitattributes`) — Windows CRLF gave `set -euo pipefail\r` → "invalid option name". Install launchers atomically (temp + `mv`) so the running script doesn't self-overwrite (`syntax error near '('` after the work succeeded).
- **PowerShell 5.1 zh-CN traps:** save scripts UTF-8 BOM; PS strips embedded `"` to native exes (use a quote-free version probe); text artifacts must be LF (`newline="\n"`) or `sha256sum -c` fails on `\r`; MANIFEST keys via `.as_posix()` (a WindowsPath str = backslash → every file reads "missing" on Linux).
- **`LDO_NOISE_FAST`** env caps the noise fit budget (nfev) + skips ladder/admittance escalation, deploy-smoke only; UNSET on the real box = full budget, byte-identical for converging fits.

## Manifest / coverage

- **`coverage.temps` is the ONLY temperature run axis** (`manifest.temps()`); it MUST be an explicit number list — `manifest._validate_coverage` RAISES on a string. The `start:step:stop` / comma-mixed expansion happens at the GUI/build boundary, not in `temps()`. Tier T4 only selects machinery.
- **Transient label keys:** real npz `tr_<o>_<label>_<load>` (e.g. `tr_pll_2m_tt_25c`); load currents must come from manifest `coverage.transient.steps`, NOT parsed from the opaque key. `_settled_step` must search the edge in `[15%,98%]` of span, else a t=0 startup drop hijacks argmax onto startup (0 settled pts). The digest DROPS transient arrays → fit the DC/vreg layer from the FULL run npz, not the digest.
- **`cdecap` is a SINGLE source of truth: sim==fit.** The external output decap loaded on the LOAD-STEP transient char ONLY (AC/PSRR/noise/I-V stay INTRINSIC — no decap); it is de-embedded at fit so the emitted model stays DECAP-FREE. Resolution (`fit_iassist.resolve_cdecap`): per-rail `coverage.transient[rail].cdecap` else global `coverage.cdecap`. FOOTGUN (fixed 2026-07-02): if a transient rail declares NO cdecap, `netlist_augment` adds NO cap while the fit used to default 20 pF → a GT-vs-model physics mismatch (the fit's replay over/under-predicts the dip). Now the fit FAILS LOUDLY (skips the derive, names the missing key) — never a silent default. Surfaced in the GUI (global coverage field + per-rail `trans` cell `@cdecap=…`).
- **The 4× bare-current gotcha:** a box replay TB that drives the BARE load current (no decap) into the `.va` over-predicts the dip ~4× vs silicon; the replay REQUIRES the same decap the transient char used (the real part's 20 pF). This is the physical reason cdecap must be sim==fit — a decap mismatch is a large-signal error, not a small one.
- **Coverage auto-design (`manifest.synth_transient_coverage`):** turns current LEVELS {MIN,TYP,MAX} + `cdecap` (REQUIRED) into the `coverage.transient` steps (worst-case dip MIN→MAX + overshoot MAX→MIN + nominal band TYP±α both directions) + `coverage.loads` DC points + the AC sweep range. **FREQUENCY vs TIMESCALE are DECOUPLED (3 different things — do not conflate):** `[f_min,f_max]` = the AC/PSRR/spur SWEEP band ONLY (may reach the CARRIER); `f_event` = the fastest LOAD-SWITCH rate → step EDGE=0.05/f_event (absent → ~1 ns default, NEVER the carrier); `t_settle` = the loop RECOVERY time (a loop-BW property, NOT 1/f_min) → tstop=8·t_settle (absent → ~2 µs). The retired bug conflated them: a 6 GHz carrier gave edge=0.05/6e9≈8 ps AND 32 kHz f_min gave tstop=8/32e3=250 µs → one tran needing ~3e7 points (un-runnable). `edge`/`tstop` are explicit overrides; the return carries a `note` documenting the provenance. Pure/Qt-free; the GUI "Auto-design…" button surfaces f_event/t_settle as editable fields. `refine_transient_coverage` (Phase B) locates the fitted-|Zout| peak → worst-case glitch width + transparency-floor lower bound + a coverage-gap warning (fit-time auto-emission deferred — BACKLOG).
- **Self-fulfilling-test trap:** lock tests that hand-fabricate the input shape the code wants (vreg key-format, Idc(T) temps) PASS while the real manifest path fails — always drive the REAL manifest.

## pmukit-era additions (2026-09-15/16, found while building this repo)

- **tcsh has no `2>`.** `ssh host 'tcsh -c "... 2>/dev/null"'` fails with **"Ambiguous output
  redirect"**. tcsh spells it `>&` (both streams) or `>& /dev/null`. Every remote command in this
  repo goes through `tools/vmrun.sh`, which keeps the redirection on the *bash* side.
- **A `section=`d include silently DROPS statements written outside any section.** Measured on
  Spectre 18.1 while building `tests/fixtures/pmu_demo/pdk/toplevel.scs`: model cards at file
  scope simply never loaded, with no warning. Each section must `include` the card file itself
  (a nested include resolves relative to the including file).
- **`python -m <pkg>` needs `__main__.py`.** The installed launcher execs `python -m pmukit`; a
  package with only `cli.py` dies with `No module named pmukit.__main__`. Caught by the deploy
  milestone's post-install check, which now names the missing file.
- **argparse reads `--temps -40,25,125` as an unknown option.** Any option whose value can start
  with a minus (temperature, most obviously) needs either `--opt=value` or an argv pre-pass;
  `pmukit/cli.py` does the pre-pass so the natural spelling works.
- **A `float('nan')` stored in SQLite comes back as `NULL`.** A run that SWEEPS temperature has no
  single temperature, and NaN is the honest value; `ledger.Run` therefore reads `NULL` back as NaN
  instead of raising, and renders it as "T swept".
- **Windows memmap vs atomic replace:** a dataset variable over 8 MB is memory-mapped on read, and
  Windows refuses `os.replace` while a caller still holds the array. `dataset.py` surfaces that as
  a four-part error telling the caller to drop the reference -- never as a partial write.
- **The commit gate earns its keep.** It fired three times during this build on content nobody
  meant to publish: the design artboards, this build's own report quoting the names it had just
  removed, and two customer cell names in a vendored ngspice header. Each time the fix was the
  file. Run `python tools/guard.py --all` before every commit; the pre-commit hook does it for you.

## Misc python / numeric

- **`np.trapz` is GONE** in this numpy — use a manual trapezoid.
- **Headless Qt screenshots:** `QT_QPA_PLATFORM=offscreen` + monkeypatch `QDialog.exec_` to `grab().save()`.
