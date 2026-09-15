# from LDO_modeling/harness/emit_pmu_model.py @ d2c5b80
"""One Verilog-A module per process corner, built only from the HB-safe primitive whitelist.

    emit_va(port_fits, derived, corner, *, provenance, hb_robust=True) -> str

What the module looks like

    module PMU_<project>_<corner>(<supply pins>, <rail pins>, <bias pins>, <stub pins>,
                                  <ground pins>);

  * every rail and every bias returns to ITS OWN ground pin, read from
    `derived.grounds["by_pin"]`.  The module ground is a real PIN, never an implicit 0: a
    previous emitted module floated to -100 MV when its VSS was not tied.
  * `vset` and `load_en_<rail>` are the only INSTANCE parameters.  Every fitted number is a
    `localparam`, so a re-emit always takes effect (a stale instance override otherwise shadows
    the new default -- the "it still shows my old value" bug).
  * temperature is CONTINUOUS inside the corner: `$temperature` is read exactly once, in
    `Netlist.temp_c_var`, and drives the rail `vout_tc` and the bias `ptat_slope` terms.
  * the small-signal blocks are baked at ONE cell (the nominal temperature and the rail's typical
    load); which cell that was is written into the module header, so the deliverable never hides
    the operating point behind the numbers.

The rail, term by term

    Zout(s) = [Ra + (sLa || Rpl) + sum_i (sLa_i || Rpl_i)] || (Rb + sLb) || (esr + 1/sCout)

  Branch A's regulation is the STIFF resistor `V(nA,vrg) <+ Ra*I`.  A saturating "DC current
  compliance" is refuted: its knee is a VOLTAGE `Icomp*Ra`, ~1-5 mV with the fitted Ra, so a few
  mV off regulation the branch becomes a zero-conductance current source, the DC pin is lost and
  the FF corner runs away (2.31e6 V observed).

    PSRR(s) = i_c(s) * Zout(s)
    i_c(s)  = G0 + sum_i G_i/(1 + s/w_i)
              + (pc_gain + pc_zero*s)/(1 + s/(pc_q*pc_w0) + (s/pc_w0)^2) + s*c_ft

  * `G0` is BAND-LIMITED by one gm-C pole at `PSRR_BANDLIMIT_MARGIN * care_up_to_hz`; a
    flat-to-infinity supply->output injection is the only PSRR path still at full strength at the
    top HB harmonic.  The corner is derived from the project, never hard-coded.
  * any near-cancelling first-order DOUBLET is consolidated into ONE small-coefficient gm-C
    biquad.  A rational fit can emit `G1 = +17.23 / G3 = -17.23` with poles 0.004 % apart;
    harmless alone, but in a coupled oscillator HB that +-17.2 S pair is a near-null-space
    direction in the SHARED supply-node Jacobian and the singular column surfaces at the package
    or at neighbouring transistors, never at the model.
  * the complex 2nd-order section is a gm-C biquad, never a synthesized R-L-C: a kHz pole needs
    thousands of henries (8890 H in one real case) whose branch admittance underflows against
    O(1) terms at a 77 GHz harmonic, and rescaling the L/C split only MOVES the extreme from the
    inductor to the capacitor (8.89 uF -> wC = 4.3e6 S -> NaN).  gm-C turns that into 0.487 S
    with an identical transfer.  The tap coefficients are derived in `Netlist.gm_c_biquad`.

    norton: Sv = |Zout| * sqrt(white^2 + flicker^2/f + sum_k amp_k^2/(1 + (f/corner_k)^2))
    hybrid: Sv = sqrt( bank * |Zout/ZA|^2  +  white^2 * |Zout|^2 )

  which is `pmukit.fit.noise.sv_model` exactly.  `white` is always the Norton floor at the pin;
  what moves between the two modes is the SHAPED part -- in `hybrid` it becomes a SERIES EMF
  inside the branch-A regulation and reaches the pin through `T = Zout/ZA`, which is how a
  loop-shaped rail's spectrum is actually reproduced.  A previous emitter only ever wrote the
  Norton form, so for a hybrid rail the shaped list was empty and the deployed model shipped only
  the white floor: the entire 1/f tail vanished (~306x at 100 Hz), a user-visible bug.  The hybrid
  path is gated on `nmode`, so a Norton rail is unchanged, and `_check_hybrid_coupled` refuses a
  module where the bank was written but never spliced.

  Every rail `white_noise` first argument is pinned to the 4kT * 300 K FIT basis: the gains were
  backed out at 300 K, so `4kT*$temperature` would add a non-physical sqrt(T/300) (+0.39 dB at
  55 C, measured) to a spectrum that is nearly temperature independent.

The current bias

  Direction is DATA-DETECTED, never assumed: `pol = "source" if idc >= 0 else "sink"`; a source
  drives `I(supply, o)` and a sink drives `I(o, gnd)`.  Getting it wrong makes the model DRAW the
  current the real reference INJECTS.  The compliance knee is ONE-SIDED (`max(vhi - Vo, 0)` /
  `max(Vo, 0)`): a symmetric `|vhi - Vo|` climbs back to 1 above the ceiling and the sink
  spuriously reopens to full current where the real device is starved.

Stub ports are emitted as ideal DC sources at their measured DC value with zero simulation behind
them, and listed in the header as "stub, not modeled".
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

from ..config import DerivedConfig
from ..errors import PmuError
from .primitives import C_NOM, GM_SOFT, OFF_OHM, Netlist, balanced_gain, biquad_from_doublet, num

__all__ = ["emit_va", "build_va", "module_name", "normalize_fits", "select_cell",
           "blocks_by_port", "dc_by_vset", "PSRR_BANDLIMIT_MARGIN", "DOUBLET_CANCEL",
           "DOUBLET_POLE", "FLICKER_PER_DECADE", "FLICKER_BAND_MARGIN_DECADES"]

#: The `G0` band-limit corner is this multiple of the project's `care_up_to_hz`.
#:
#: The margin is set by measurement, not by taste.  At the top of the characterized band one pole
#: this far out costs 20*log10(1/sqrt(1 + (1/200)^2)) = 0.00011 dB of magnitude and atan(1/200) =
#: 0.29 deg of phase on the flat term itself.  The phase matters more than it looks: when the rest
#: of `i_c` carries a quadrature component (a feedthrough cap's s*C_ft tail is the usual one),
#: rotating the flat term by that angle shows up as a MAGNITUDE error of roughly
#: 8.686 * (f/f_bl) * |quadrature|/|i_c| dB.  Measured on the real Spectre acceptance bench with a
#: 174 fF C_ft rail: margin 50 cost 0.021 dB, margin 200 costs 0.0053 dB -- inside the 0.01 dB
#: acceptance with room to spare.  Everything ABOVE the band still rolls off at 6 dB/octave
#: instead of staying flat forever, and for a rail whose PSRR band ends at 10 MHz this lands the
#: corner at 2 GHz, which is the value that was hand-validated on the real oscillator HB.
PSRR_BANDLIMIT_MARGIN = 200.0
#: A signed first-order pair cancelling to within this fraction, with poles this close, is the
#: coupled-HB doublet and is consolidated into ONE gm-C biquad.  Same numbers as the fitter's own
#: detector (`pmukit.fit.psrr.doublet_notes`), so what the fit flags is what the emitter merges.
DOUBLET_CANCEL = 0.05
DOUBLET_POLE = 0.05
#: Lorentzian sections per decade when a pure 1/f term is synthesized as a bank (the default).
FLICKER_PER_DECADE = 2
#: ... over the noise band WIDENED by this many decades on each side.  The margin is what costs:
#: the ladder is truncated at its ends, so a ladder that stops at the band edge under-delivers
#: there.  Measured against `fit.noise.predict` over 10 Hz .. 100 MHz: one decade of margin left
#: 0.146 dB at the edges, two decades leave 0.012 dB, which is the synthesis ripple itself.
FLICKER_BAND_MARGIN_DECADES = 2
#: ... but never more than this many sections, however wide the noise band is (a truncated ladder
#: is reported in the notes, never silently).
FLICKER_MAX_SECTIONS = 32
#: `flicker_mode='native'` on a HYBRID rail needs an R||C carrier node; its band limit sits this
#: multiple above the characterized noise band, so the 1/f is exact in band (-4e-5 dB at the top)
#: and the source is not flat to infinity above it.
FLICKER_NATIVE_POLE = 100.0
#: The supply DC tracker's corner, as a fraction of the AC sweep's first point.  It removes the
#: supply's DC component so the PSRR path injects nothing at DC for ANY supply value, while
#: sitting a hundredfold below the measured band (-0.0004 dB at the first swept point).
TRACKER_BELOW_BAND = 0.01
#: The tracker resistor: large enough to give that corner with a small cap, small enough that its
#: conductance does not underflow at the top harmonic.
TRACKER_R = 1.0e10


# --------------------------------------------------------------------------- input shaping
def _err(what: str, why: str, do, where: str = "pmukit/emit/va.py") -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=where)


def _as_derived(derived) -> DerivedConfig:
    if isinstance(derived, DerivedConfig):
        return derived
    if isinstance(derived, Mapping):
        return DerivedConfig.from_dict(derived)
    raise _err(f"emit_va needs the derived config, got {type(derived).__name__}.",
               "the emitter reads the rails, biases, split grounds, stubs, load grid and "
               "frequency band out of contract 0b; without it the module has no ports.",
               ["pass the DerivedConfig returned by pmukit.config.derive()",
                "or the dict loaded from $PMUKIT_DATA/<project>/derived.json"])


def normalize_fits(port_fits) -> dict[tuple[str, str], list[tuple[dict, dict]]]:
    """Accept either shape the pipeline can hand over, return `{(port, block): [(cell, params)]}`.

    Accepted:
      * a flat iterable of `BlockFit`-like objects or dicts carrying `port` / `block` / `params`
        (and optionally `cell`, `missing`) -- what `pmukit.fit` produces;
      * a nested mapping `{port: {block: params}}` -- what a hand-built fixture is.

    A fit marked `missing` contributes nothing: a never-run measurement must not turn into a
    fabricated parameter in the emitted model.
    """
    out: dict[tuple[str, str], list[tuple[dict, dict]]] = {}

    def add(port, block, cell, params, missing):
        if missing or not params or port is None or block is None:
            return
        out.setdefault((str(port), str(block)), []).append((dict(cell or {}), dict(params)))

    # `pmukit.fit.FitResult` (or its to_dict()) -- the normal path
    inner = getattr(port_fits, "fits", None)
    if inner is None and isinstance(port_fits, Mapping):
        inner = port_fits.get("fits")
    if isinstance(inner, Mapping):
        port_fits = list(inner.values())

    if isinstance(port_fits, Mapping):
        nested = bool(port_fits) and all(
            isinstance(v, Mapping) and not {"port", "block", "params"} <= set(v)
            for v in port_fits.values())
        if nested:
            for port, blocks in port_fits.items():
                if not isinstance(blocks, Mapping):
                    continue
                for block, params in blocks.items():
                    if isinstance(params, Mapping):
                        add(port, block, {}, params, False)
                    else:
                        for cell, p in params:
                            add(port, block, cell, p, False)
            return out
        items = list(port_fits.values())
    else:
        try:
            items = list(port_fits)
        except TypeError:
            raise _err(f"emit_va could not read the fit result ({type(port_fits).__name__}).",
                       "the emitter accepts a list of BlockFit records or a nested "
                       "{port: {block: params}} mapping; anything else carries no port or block "
                       "name.",
                       ["pass the list `pmukit.fit` returned",
                        "or build {port: {block: {param: value}}} by hand"]) from None

    for it in items:
        if isinstance(it, Mapping):
            add(it.get("port"), it.get("block"), it.get("cell"), it.get("params"),
                bool(it.get("missing")))
        else:
            add(getattr(it, "port", None), getattr(it, "block", None),
                getattr(it, "cell", None), getattr(it, "params", None),
                bool(getattr(it, "missing", False)))
    return out


def _nearest(values, target):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return min(vals, key=lambda v: abs(float(v) - float(target)))


def select_cell(candidates, corner, *, temp_c=None, load_a=None, vset=None):
    """Pick ONE `(cell, params)` from a block's fitted cells for this corner.

    The emitted module is per PROCESS corner with temperature continuous inside it, so the
    small-signal blocks -- fitted per discrete temperature and per load point -- are baked at the
    NOMINAL cell.  Returns `None` when nothing was fitted for this corner.
    """
    same = [(c, p) for c, p in candidates
            if "process" not in c or str(c.get("process")) == str(corner)]
    if not same:
        return None
    if temp_c is not None:
        t = _nearest([c.get("temp_c") for c, _ in same], temp_c)
        if t is not None:
            same = [(c, p) for c, p in same
                    if c.get("temp_c") is None or float(c["temp_c"]) == float(t)]
    if load_a is not None:
        il = _nearest([c.get("load_a") for c, _ in same], load_a)
        if il is not None:
            same = [(c, p) for c, p in same
                    if c.get("load_a") is None or float(c["load_a"]) == float(il)]
    if vset is not None:
        exact = [(c, p) for c, p in same
                 if c.get("vset") is None or str(c["vset"]) == str(vset)]
        if exact:
            same = exact
    return same[0]


def blocks_by_port(fits, port, corner, *, temp_c=None, load_a=None, vset=None):
    """`{block: params}` for one port at one corner, plus the cell each block was baked at."""
    blocks, cells = {}, {}
    for (p, block), cands in fits.items():
        if p != port:
            continue
        chosen = select_cell(cands, corner, temp_c=temp_c, load_a=load_a, vset=vset)
        if chosen and chosen[1]:
            cells[block], blocks[block] = chosen[0], chosen[1]
    return blocks, cells


def dc_by_vset(fits, port, corner, codes, *, temp_c=None, load_a=None) -> dict:
    """`{vset code: dc params}` -- the ONE block that is genuinely per-code.

    VSET is what sets the rail voltage, so the DC block is selected per code and the emitted
    reference carries a selector over the instance parameter.  Every other block is baked at the
    DEFAULT code and the module header says so: their VSET dependence is second order, and
    duplicating the whole Zout/PSRR/noise network per code would multiply the node count for a
    sensitivity nobody measured separately.
    """
    out = {}
    cands = fits.get((port, "dc")) or []
    for code in codes:
        chosen = select_cell(cands, corner, temp_c=temp_c, load_a=load_a, vset=code)
        if chosen and chosen[1]:
            out[code] = chosen[1]
    return out


def _f(params: Mapping, key: str, default=None):
    """One fitted scalar, tolerant of the JSON round-trip that spells NaN as a string."""
    v = params.get(key, default)
    if v is None or isinstance(v, (list, tuple, dict)):
        return default
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("nan", "inf", "+inf", "-inf"):
            return default
        try:
            v = float(v)
        except ValueError:
            return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _list(params: Mapping, key: str) -> list[float]:
    out = []
    for v in (params.get(key) or []):
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            out.append(x)
    return out


def _pick_vset(params: Mapping, key: str, code, default=None):
    """`vout` / `vout_tc` may be a scalar, `{vset: value}` or `{vset: {load: value}}`."""
    v = params.get(key)
    if isinstance(v, Mapping):
        picked = None
        for k in (code, str(code)):
            if k in v:
                picked = v[k]
                break
        if picked is None:
            try:
                picked = v[int(code)]
            except (KeyError, TypeError, ValueError):
                picked = next(iter(v.values()), None)
        if isinstance(picked, Mapping):
            picked = next(iter(picked.values()), None)
        try:
            out = float(picked)
        except (TypeError, ValueError):
            return default
        return out if math.isfinite(out) else default
    return _f(params, key, default)


# --------------------------------------------------------------------------- doublets
def _consolidate(gains, poles_hz):
    """Split the real-pole bank into (kept first-order sections, merged gm-C biquads, notes).

    A pair is merged when it is signed-opposite, cancels to within `DOUBLET_CANCEL` of the larger
    residue and its poles agree to within `DOUBLET_POLE` -- the emit-time HB hazard.
    """
    n = len(gains)
    used = [False] * n
    singles, merged, notes = [], [], []
    for i in range(n):
        if used[i]:
            continue
        gi, pi = float(gains[i]), float(poles_hz[i]) if i < len(poles_hz) else 0.0
        for j in range(i + 1, n):
            if used[j]:
                continue
            gj, pj = float(gains[j]), float(poles_hz[j]) if j < len(poles_hz) else 0.0
            top = max(abs(gi), abs(gj))
            if top <= 0 or gi * gj >= 0 or pi <= 0 or pj <= 0:
                continue
            if abs(gi + gj) > DOUBLET_CANCEL * top:
                continue
            if abs(pi - pj) > DOUBLET_POLE * max(pi, pj):
                continue
            wi, wj = 2 * math.pi * pi, 2 * math.pi * pj
            b0, b1, w0, q = biquad_from_doublet(gi, wi, gj, wj)
            merged.append((b0, b1, w0, q))
            notes.append(
                f"PSRR doublet consolidated: G={gi:+.5g} S @ {pi:.5g} Hz and G={gj:+.5g} S @ "
                f"{pj:.5g} Hz (residues cancel to {b0:+.3g} S, poles "
                f"{abs(pi - pj) / max(pi, pj) * 100:.4f} % apart) -> ONE gm-C biquad at "
                f"{w0 / (2 * math.pi):.5g} Hz, Q={q:.4g}. Left as a pair, the +-{top:.4g} S "
                f"residues are a near-null-space direction in the SHARED supply-node harmonic "
                f"Jacobian and the singular column surfaces at the package, not at the model.")
            used[i] = used[j] = True
            break
        if not used[i]:
            used[i] = True
            if gi != 0.0 and pi > 0.0:
                singles.append((gi, 2 * math.pi * pi))
    return singles, merged, notes


# --------------------------------------------------------------------------- noise helpers
def _flicker_sections(flicker: float, f_lo: float, f_hi: float, per_decade: int):
    """Synthesize a pure `flicker^2/f` term as a Lorentzian ladder (the validated realization).

    A sum of Lorentzians with geometric corners approximates 1/f: with corner ratio `r`,
    `a_k = (2*K*ln r/pi)/f_k` reproduces `S(f) = K/f` across the covered band (the continuum
    limit of `sum a_k/(1 + (f/f_k)^2)`).  Returns `[(corner_hz, amplitude)]`.
    """
    if flicker <= 0.0 or f_hi <= f_lo:
        return []
    decades = math.log10(f_hi / f_lo)
    n = min(max(int(round(per_decade * decades)) + 1, 2), FLICKER_MAX_SECTIONS)
    r = (f_hi / f_lo) ** (1.0 / (n - 1))
    k = flicker * flicker
    out = []
    for i in range(n):
        fc = f_lo * (r ** i)
        out.append((fc, math.sqrt((2.0 * k * math.log(r) / math.pi) / fc)))
    return out


# --------------------------------------------------------------------------- the rail
def _rail_block(nl: Netlist, port: str, gnd: str, supply: str, vrf: str, blocks: dict,
                derived: DerivedConfig, *, tnom_c: float, vset_codes, i_typ: float,
                hb_robust: bool, flicker_mode: str, ls_on: bool, dc_table=None,
                ls_ports: list | None = None) -> list[str]:
    """Emit one voltage rail.  Returns the header lines describing what was baked."""
    pre = port
    head: list[str] = []
    z = blocks.get("zout") or {}
    dc = blocks.get("dc") or {}

    Ra = _f(z, "Ra", 0.0) or 0.0
    La, Rpl = _f(z, "La"), _f(z, "Rpl")
    Rb, Lb = _f(z, "Rb"), _f(z, "Lb")
    Cout, esr = _f(z, "Cout", 0.0) or 0.0, _f(z, "esr", 0.0) or 0.0
    la_i, rpl_i = _list(z, "La_i"), _list(z, "Rpl_i")
    vout_tc = _pick_vset(dc, "vout_tc", vset_codes[0], 0.0) or 0.0

    vrg = nl.node(f"{pre}_vrg")
    tdegc = nl.temp_c_var()

    # ---- the regulated reference, with VSET selection and continuous temperature -------
    vset_terms = []
    for c in vset_codes:
        per_code = (dc_table or {}).get(c) or dc
        v = _pick_vset(per_code, "vout", c, None)
        if v is None:
            v = _pick_vset(dc, "vout", c, 0.0) or 0.0
        vset_terms.append((c, float(v) + Ra * float(i_typ or 0.0)))
    for c, v in vset_terms:
        nl.localparam(f"{pre}_vreg_{_codename(c)}", v,
                      f"{port}: internal reference at VSET={c} (= measured vout + "
                      f"Ra*{float(i_typ or 0.0):g} A, so branch A's Ra reproduces the load "
                      f"regulation around the typical load)")
    nl.localparam(f"{pre}_vtc", vout_tc, f"{port}: dVout/dT [V/degC], continuous in this corner")
    nl.comment(f"===== rail {port} (ground {gnd}) =====")
    nl.ideal_vsource(f"{port}.vrg", vrg, gnd,
                     f"{_vset_selector(pre, vset_terms)} + {pre}_vtc*({tdegc} - {num(tnom_c)})",
                     f"{port}: regulated reference, temperature continuous")
    head.append(f"//   rail {port}: "
                + ", ".join(f"VSET={c} -> {v:.6g} V" for c, v in vset_terms)
                + f"; dVout/dT = {vout_tc:.4g} V/C; ground {gnd}")

    # ---- Zout ---------------------------------------------------------------------------
    nl.comment(f"{port} Zout branch A: Ra + (sLa || Rpl)"
               + (f" + {len(la_i)} extra (L||R) ladder stage(s)" if la_i else ""))
    node = port
    if La and La > 0.0:
        nxt = nl.node(f"{pre}_nA")
        nl.damped_inductor(f"{port}.zout.La", node, nxt, La, Rpl, f"{port}: branch-A shelf")
        node = nxt
    for k, (li, ri) in enumerate(zip(la_i, rpl_i), start=2):
        nxt = nl.node(f"{pre}_nA{k}")
        nl.damped_inductor(f"{port}.zout.La{k}", node, nxt, li, ri,
                           f"{port}: branch-A ladder stage {k}")
        node = nxt

    ld = blocks.get("load_en") or {}
    ovd_keys = ("ovVdz", "ovR", "ovVmax", "ovVsc", "ovIsc")
    have_ovd = bool(ld) and all(_f(ld, k) is not None for k in ovd_keys)
    have_assist = bool(ld) and (_f(ld, "iaG") or 0.0) > 0 and (_f(ld, "iaV") or 0.0) > 0
    en_name = ""
    if have_ovd or have_assist:
        if ls_ports is not None:
            ls_ports.append(port)
        en_name = nl.parameter(
            f"load_en_{pre}", 1.0 if ls_on else 0.0,
            f"{port}: large-signal load-event terms (droop assist + unload discharge). 1 = on, "
            f"0 = off. Default " + ("ON -- it passed the HB first-step residual check"
                                    if ls_on else
                                    "OFF until the HB first-step residual check passes"))
    extra = ""
    if en_name and have_ovd:
        extra += Netlist.bounded_reverse_emf_term(port, vrg, node, en_name,
                                                  {k: _f(ld, k) for k in ovd_keys})

    nb = blocks.get("noise") or {}
    nmode = str(nb.get("nmode", "norton") or "norton")
    if nb:
        # The bank is emitted HERE, before the regulation, because a hybrid rail's shaped part
        # has to be spliced INTO that regulation as a series EMF.
        extra += Netlist.series_emf_term(
            _noise_bank(nl, port, gnd, nb, derived, series=(nmode == "hybrid"),
                        flicker_mode=flicker_mode))
    nl.resistor_expr(f"{port}.zout.Ra", node, vrg, Ra, extra,
                     f"{port}: branch-A regulation -- the STIFF resistor (never a current clamp)")

    if Rb is not None and Rb < OFF_OHM and Lb and Lb > 0.0:
        nbb = nl.node(f"{pre}_nbb")
        nl.comment(f"{port} Zout branch B: Rb + sLb")
        nl.inductor(f"{port}.zout.Lb", port, nbb, Lb, f"{port}: branch B")
        nl.resistor(f"{port}.zout.Rb", nbb, vrg, Rb, f"{port}: branch B")
    elif Rb is not None:
        nl.note(f"{port}: branch B is the fitter's OFF sentinel (Rb = {Rb:.3g} ohm) -> not "
                f"emitted; a 1e9 ohm branch is a ~1e-9 S near-null column with no transfer")

    if Cout and Cout > 0.0:
        nl.comment(f"{port} Zout branch C: Cout + esr")
        if esr > 1.0e-6:
            nc = nl.node(f"{pre}_nC")
            nl.capacitor(f"{port}.zout.Cout", port, nc, Cout, f"{port}: output capacitance")
            nl.resistor(f"{port}.zout.esr", nc, vrg, esr, f"{port}: output-cap ESR")
        else:
            nl.capacitor(f"{port}.zout.Cout", port, vrg, Cout,
                         f"{port}: output capacitance (esr = {esr:g} ohm -> no series node)")
    head.append(f"//     Zout: Ra={Ra:.4g} ohm, La={(La or 0.0):.4g} H, Rpl={(Rpl or 0.0):.4g} "
                f"ohm, {len(la_i)} extra ladder stage(s), Cout={Cout:.4g} F, esr={esr:.4g} ohm"
                + (f", branch B Rb={Rb:.4g} ohm Lb={Lb:.4g} H"
                   if (Rb is not None and Rb < OFF_OHM and Lb) else ", branch B off"))

    # ---- PSRR ---------------------------------------------------------------------------
    ps = blocks.get("psrr") or {}
    if ps:
        head += _rail_psrr(nl, port, gnd, supply, vrf, ps, derived, hb_robust=hb_robust)

    # ---- noise --------------------------------------------------------------------------
    if nb:
        shaped = (f"{len(_list(nb, 'corner_i_hz'))} fitted Lorentzian(s)"
                  + (f" + 1/f ({flicker_mode})" if (_f(nb, 'flicker', 0.0) or 0.0) > 0 else ""))
        if nmode == "hybrid":
            head.append(f"//     noise: HYBRID -- the shaped part ({shaped}) is a SERIES EMF "
                        f"inside branch A reaching the pin through Zout/ZA; white "
                        f"{_f(nb, 'white', 0.0) or 0.0:.4g} A/rtHz stays the Norton floor")
        else:
            head.append(f"//     noise: NORTON at the pin -- white "
                        f"{_f(nb, 'white', 0.0) or 0.0:.4g} A/rtHz + {shaped}")

    # ---- large-signal load events --------------------------------------------------------
    if en_name:
        if have_assist:
            iaG, iaV = _f(ld, "iaG"), _f(ld, "iaV")
            nl.comment(f"{port} load-step assist: ODD with f'(0) = 0 EXACTLY -> invisible to "
                       f"Zout/PSRR/noise at the operating point")
            nl.behavioral_current(f"{port}.load_en.assist", port, vrg,
                                  Netlist.odd_assist_expr(port, vrg, en_name, iaG, iaV),
                                  "odd_current_assist", f"{port}: compressive class-AB assist")
        head.append(f"//     load_en: "
                    + (f"assist iaG={_f(ld, 'iaG', 0.0) or 0.0:.4g} A / "
                       f"iaV={_f(ld, 'iaV', 0.0) or 0.0:.4g} V; " if have_assist else "")
                    + (f"unload discharge ovVdz={_f(ld, 'ovVdz', 0.0) or 0.0:.4g} V; "
                       if have_ovd else "")
                    + f"instance parameter {en_name} defaults to {'1' if ls_on else '0'}")
        if have_ovd:
            nl.note(f"{port}: the `no_sink` constraint is realized by the unload discharge's "
                    f"source/sink discriminator, so it is only active while {en_name} = 1")
    elif ld:
        nl.note(f"{port}: the load_en fit is incomplete (the unload discharge needs all of "
                f"{', '.join(ovd_keys)}; the assist needs iaG and iaV) -> no large-signal term "
                f"emitted")
    nl.blank()
    return head


def _codename(code) -> str:
    if code is None:
        return "nom"
    try:
        return f"v{int(code)}"
    except (TypeError, ValueError):
        return "v" + "".join(ch for ch in str(code) if ch.isalnum())


def _vset_selector(pre: str, terms) -> str:
    """A ternary chain over the VSET codes -- resolved at elaboration from the instance param."""
    if len(terms) == 1:
        return f"{pre}_vreg_{_codename(terms[0][0])}"
    ordered = sorted(terms, key=lambda t: (float(t[0]) if t[0] is not None else 0.0))
    expr = f"{pre}_vreg_{_codename(ordered[-1][0])}"
    for (c, _), (c_next, _) in zip(reversed(ordered[:-1]), reversed(ordered[1:])):
        mid = (float(c) + float(c_next)) / 2.0
        expr = f"(vset <= {num(mid)} ? {pre}_vreg_{_codename(c)} : {expr})"
    return expr


def _rail_psrr(nl: Netlist, port: str, gnd: str, supply: str, vrf: str, ps: Mapping,
               derived: DerivedConfig, *, hb_robust: bool) -> list[str]:
    """The supply coupling current `i_c`, injected INTO the pin so that PSRR = i_c * Zout."""
    pre = port
    head = []
    f_max = float((derived.freq or {}).get("stop_hz", 1e9) or 1e9)
    w_bl = 2.0 * math.pi * PSRR_BANDLIMIT_MARGIN * f_max

    nl.comment(f"{port} PSRR: i_c injected INTO the pin (I(pin,gnd) <+ -gm*V() ADDS current "
               f"there) so PSRR = i_c * Zout, NOT its negative")
    G0 = _f(ps, "G0", 0.0) or 0.0
    if G0 != 0.0:
        nl.band_limited_tap(f"{pre}_psG0", G0, w_bl, supply, vrf, port, gnd,
                            f"{port}: flat supply coupling, band-limited at "
                            f"{PSRR_BANDLIMIT_MARGIN:g} x care_up_to_hz")
        head.append(f"//     PSRR G0={G0:.4g} S, band-limited at "
                    f"{PSRR_BANDLIMIT_MARGIN * f_max:.4g} Hz ({PSRR_BANDLIMIT_MARGIN:g} x "
                    f"care_up_to_hz = {f_max:.4g} Hz; -0.0001 dB at the top of the band)")

    singles, merged, dnotes = _consolidate(_list(ps, "G_i"), _list(ps, "pole_i_hz"))
    for n in dnotes:
        nl.note(n)
    for k, (g, w) in enumerate(singles, start=1):
        a, _, (tap,) = balanced_gain(C_NOM * w, [g])
        if a != 1.0:
            nl.note(f"{port}: PSRR section {k} has |G| = {abs(g):.4g} S at or above {GM_SOFT:g} "
                    f"S -> rebalanced through an internal gain of {a:.4g} so no single "
                    f"coefficient is large in the shared supply-node Jacobian (transfer "
                    f"unchanged)")
        node = nl.gm_c_lowpass(f"{pre}_ps{k}", f"{pre}_ps{k}", supply, vrf, w, a, gnd,
                               f"{port}: PSRR real pole {w / (2 * math.pi):.4g} Hz")
        nl.inject(f"{port}.psrr.G{k}", port, node, gnd, tap, gnd,
                  f"{port}: PSRR section {k}, residue {g:+.4g} S")
    for k, (b0, b1, w0, q) in enumerate(merged, start=1):
        nl.gm_c_biquad(f"{pre}_psd{k}", b0, b1, w0, q, supply, vrf, port, gnd,
                       f"{port}: CONSOLIDATED near-cancelling doublet")
    if singles or merged:
        head.append(f"//     PSRR real bank: {len(singles)} first-order section(s), "
                    f"{len(merged)} consolidated doublet(s)")

    b0, b1 = _f(ps, "pc_gain", 0.0) or 0.0, _f(ps, "pc_zero", 0.0) or 0.0
    w0, q = _f(ps, "pc_w0", 0.0) or 0.0, _f(ps, "pc_q", 0.0) or 0.0
    if (b0 != 0.0 or b1 != 0.0) and w0 > 0.0 and q > 0.0:
        if hb_robust:
            nl.gm_c_biquad(f"{pre}_pc", b0, b1, w0, q, supply, vrf, port, gnd,
                           f"{port}: signed complex 2nd-order section")
            head.append(f"//     PSRR complex section: gm-C biquad at "
                        f"{w0 / (2 * math.pi):.4g} Hz, Q={q:.3g}")
        else:
            _rlc_complex_section(nl, port, gnd, supply, vrf, b0, b1, w0, q)
            head.append("//     PSRR complex section: *** SYNTHESIZED R-L-C (hb_robust=False) "
                        "-- THE REFUTED REALIZATION ***")
    c_ft = _f(ps, "c_ft", 0.0) or 0.0
    if c_ft > 0.0:
        nl.capacitor(f"{port}.psrr.Cft", supply, port, c_ft,
                     f"{port}: pass-device/package feedthrough capacitance")
        head.append(f"//     PSRR feedthrough cap c_ft={c_ft * 1e15:.1f} fF")
    return head


def _rlc_complex_section(nl: Netlist, port: str, gnd: str, supply: str, vrf: str,
                         b0: float, b1: float, w0: float, q: float) -> None:
    """The REFUTED synthesized R-L-C realization, reachable only with `hb_robust=False`.

    Kept as a documented escape hatch and as the thing the conditioning lint must catch: with the
    historical 1 pF realizing cap a kHz pole forces THOUSANDS of henries, and that branch's
    1/(jwL) admittance underflows against O(1) terms at a high harmonic -- "matrix singular
    during decomposition".  Rescaling the cap only moves the extreme to the capacitor.
    """
    pre = port
    a1, a2 = 1.0 / (q * w0), 1.0 / (w0 * w0)
    cpc = 1.0e-12
    rpc, lpc = a1 / cpc, a2 / cpc
    nl.comment(f"*** hb_robust=False: {port} complex PSRR section as a SYNTHESIZED R-L-C, "
               f"Lpc = {lpc:.4g} H. This is the REFUTED realization and the conditioning lint "
               f"WILL fire on it.")
    n1, n2 = nl.node(f"{pre}_ncs1"), nl.node(f"{pre}_ncs2")
    nl.resistor(f"{port}.psrr.Rpc", supply, n1, rpc, "SYNTHESIZED complex-section resistor")
    nl.inductor(f"{port}.psrr.Lpc", n1, n2, lpc,
                "SYNTHESIZED complex-section inductor -- REFUTED, see WHITELIST['inductor']")
    nl.capacitor(f"{port}.psrr.Cpc", n2, vrf, cpc, "SYNTHESIZED complex-section capacitor")
    nl.inject(f"{port}.psrr.pcb0", port, n2, vrf, b0, gnd, "SYNTHESIZED section b0 tap")
    nl.inject(f"{port}.psrr.pcb1", port, supply, n1, b1 / a1, gnd, "SYNTHESIZED section b1 tap")
    nl.note(f"{port}: hb_robust=False -> the complex PSRR section is the REFUTED synthesized "
            f"R-L-C with Lpc = {lpc:.4g} H. This model is NOT HB-ready.")


def _noise_bank(nl: Netlist, port: str, gnd: str, nb: Mapping, derived: DerivedConfig,
                *, series: bool, flicker_mode: str) -> list[str]:
    """The rail noise bank, in EXACTLY the split `pmukit.fit.noise.sv_model` identified.

        norton:  Sv = |Zout| * sqrt(white^2 + flicker^2/f + sum amp_k^2/(1+(f/corner_k)^2))
        hybrid:  Sv = sqrt( bank * |Zout/ZA|^2  +  white^2 * |Zout|^2 )

    So `white` is ALWAYS the Norton floor at the pin; what moves between the two modes is the
    SHAPED part (the 1/f term and the Lorentzian bank).  `series=True` returns its terms for the
    caller to splice into the branch-A regulation, where they reach the pin through T = Zout/ZA.
    """
    pre = port
    white = _f(nb, "white", 0.0) or 0.0
    flicker = _f(nb, "flicker", 0.0) or 0.0
    corners, amps = _list(nb, "corner_i_hz"), _list(nb, "amp_i")
    band = derived.noise or {}
    margin = 10.0 ** FLICKER_BAND_MARGIN_DECADES
    f_band_hi = float(band.get("stop_hz", 1.0e8) or 1.0e8)
    f_lo = float(band.get("start_hz", 10.0) or 10.0) / margin
    f_hi = f_band_hi * margin

    nl.comment(f"{port} intrinsic noise: white floor as a NORTON current at the pin; the shaped "
               f"part as "
               f"{'a SERIES EMF inside branch A (hybrid)' if series else 'more Norton current'}"
               f". Every white_noise is on the 4kT*300 K FIT basis, never $temperature")
    terms: list[str] = []

    if white > 0.0:
        nl.white_noise(f"{port}.noise.white", port, gnd, white * white, f"{pre}_nw",
                       f"{port}: white floor {white:.4g} A/rtHz, the fitted PSD written directly")

    sections = list(zip(corners, amps))
    if flicker > 0.0:
        if flicker_mode == "native":
            if series:
                node, gain = nl.noise_flicker_node(
                    f"{port}.noise.flicker", f"{pre}_nvf", f"{pre}_nvf", flicker,
                    f_band_hi * FLICKER_NATIVE_POLE, gnd)
                terms.append(f"{num(gain)}*V({node}, {gnd})")
            else:
                nl.flicker_noise(f"{port}.noise.flicker", port, gnd, flicker * flicker,
                                 f"{pre}_flk",
                                 f"{port}: native 1/f (flicker_mode='native', opt-in)")
            nl.note(f"{port}: the 1/f term is ONE native flicker_noise() line "
                    f"(flicker_mode='native'). The default is the validated Lorentzian bank; "
                    f"native is exact and cheaper but has never been scored against silicon here.")
        elif flicker_mode == "bank":
            synth = _flicker_sections(flicker, f_lo, f_hi, FLICKER_PER_DECADE)
            sections += synth
            want = int(round(FLICKER_PER_DECADE * math.log10(f_hi / f_lo))) + 1
            nl.note(f"{port}: the pure 1/f term ({flicker:.4g} at 1 Hz) is synthesized as "
                    f"{len(synth)} Lorentzian section(s), {FLICKER_PER_DECADE}/decade over "
                    f"{f_lo:.4g}..{f_hi:.4g} Hz ({FLICKER_BAND_MARGIN_DECADES} decade(s) of "
                    f"margin each side of the noise band) -- the validated realization, 0.012 dB "
                    f"in band. flicker_mode='native' replaces all of them with one "
                    f"flicker_noise() line."
                    + (f" TRUNCATED at the {FLICKER_MAX_SECTIONS}-section cap (wanted {want}), "
                       f"so the ladder ends inside its margin and the band edges lose accuracy."
                       if want > FLICKER_MAX_SECTIONS else ""))
        elif flicker_mode != "off":
            raise _err(f"unknown flicker_mode {flicker_mode!r}.",
                       "the 1/f term is either the validated Lorentzian bank, one native "
                       "flicker_noise() line, or deliberately dropped.",
                       ["use 'bank' (the default), 'native' or 'off'"])

    for k, (fc, amp) in enumerate(sections, start=1):
        if not (fc > 0.0 and amp > 0.0):
            continue
        tag = "nvk" if series else "nk"
        node, vref = nl.noise_shaped_node(f"{port}.noise.L{k}", f"{pre}_{tag}{k}", fc,
                                          f"{pre}_{tag}{k}", gnd,
                                          detail=f"{port}: Lorentzian {k}, corner {fc:.4g} Hz")
        gain = amp / vref
        if series:
            terms.append(f"{num(gain)}*V({node}, {gnd})")
        else:
            nl.inject(f"{port}.noise.L{k}.tap", port, node, gnd, gain, gnd,
                      f"{port}: Lorentzian {k}, amplitude {amp:.4g} A/rtHz")
    return terms


# --------------------------------------------------------------------------- the bias
def _bias_block(nl: Netlist, port: str, gnd: str, supply: str, vrf: str, blocks: dict,
                info: Mapping, *, tnom_c: float) -> list[str]:
    pre = port
    head: list[str] = []
    idc_b = blocks.get("idc") or {}
    idc = _f(idc_b, "idc", 0.0) or 0.0
    pol = str(idc_b.get("pol") or ("source" if idc >= 0.0 else "sink"))
    side = str(idc_b.get("knee_side") or "none")
    vhi = _f(idc_b, "vhi", 0.0) or 0.0
    vknee = _f(idc_b, "vknee", 0.1) or 0.1
    knee_p = _f(idc_b, "knee_p", 1.0) or 1.0
    ptat = _f(idc_b, "ptat_slope", 0.0) or 0.0
    sgn = 1.0 if idc >= 0.0 else -1.0        # the drive carries |idc|; pol carries the sign

    y = blocks.get("yout") or {}
    g0, Cp = _f(y, "g0", 0.0) or 0.0, _f(y, "Cp", 0.0) or 0.0
    wz, wp = _f(y, "wz"), _f(y, "wp")
    vcomp = float(_f(info, "vcomp_v", 0.0) or 0.0) if isinstance(info, Mapping) else 0.0

    pb = blocks.get("psrr") or {}
    gdd = _f(pb, "gdd", 0.0) or 0.0
    gdd_pole = _f(pb, "psrr_pole_hz")

    tdegc = nl.temp_c_var()
    nl.comment(f"===== current bias {port} ({pol}, {side}-knee, ground {gnd}) =====")
    nl.localparam(f"{pre}_idc", abs(idc), f"{port}: DC output current at {tnom_c:g} C [A]")
    nl.localparam(f"{pre}_ptat", sgn * ptat,
                  f"{port}: continuous dI/dT [A/degC] -- the PTAT physics the VCO drift rides on")
    nl.localparam(f"{pre}_g0", g0, f"{port}: DC output conductance [S]")
    nl.localparam(f"{pre}_vc", vcomp,
                  f"{port}: pin operating voltage the output conductance is referenced to [V]")

    supply_term = ""
    if gdd != 0.0:
        # sign folded for the drive-node convention: a sink's probe reads -I_pin
        nl.localparam(f"{pre}_gdd", -gdd if pol == "sink" else gdd,
                      f"{port}: supply-to-output-current transconductance [A/V], sign folded for "
                      f"the {pol} drive convention")
        if gdd_pole and gdd_pole > 0.0:
            src = nl.gm_c_lowpass(f"{pre}_pdd", f"{pre}_pdd", supply, vrf,
                                  2.0 * math.pi * gdd_pole, 1.0, gnd,
                                  f"{port}: supply-to-current pole {gdd_pole:.4g} Hz")
        else:
            src = nl.gm_c_lowpass(f"{pre}_pdd", f"{pre}_pdd", supply, vrf,
                                  2.0 * math.pi * 1.0e12, 1.0, gnd,
                                  f"{port}: supply-to-current transfer, no fitted pole -> flat "
                                  f"in band, band-limited at 1 THz")
            nl.note(f"{port}: the bias PSRR fit carries no pole, so the supply-to-current "
                    f"transfer is emitted flat in band and band-limited at 1 THz rather than "
                    f"flat to infinity")
        supply_term = f" + {pre}_gdd*V({src}, {gnd})"

    if side != "none":
        nl.localparam(f"{pre}_vk", vknee, f"{port}: compliance-knee width [V]")
        nl.localparam(f"{pre}_kp", knee_p, f"{port}: compliance-knee sharpness exponent")
        nl.localparam(f"{pre}_vhi", vhi, f"{port}: compliance ceiling [V]")
        nl.note(f"{port}: the compliance knee is ONE-SIDED -- a symmetric |vhi - Vo| climbs back "
                f"to 1 above the ceiling and the sink reopens to full current where the real "
                f"device is starved")
    gate = Netlist.one_sided_knee(f"V({port}, {gnd})", f"{pre}_vk", f"{pre}_kp", side,
                                  f"{pre}_vhi")
    core = (f"{pre}_idc + {pre}_ptat*({tdegc} - {num(tnom_c)})"
            f"\n                   + {pre}_g0*(V({port}, {gnd}) - {pre}_vc){supply_term}")
    if pol == "sink":
        nl.gated_current(f"{port}.idc", port, gnd, core, gate,
                         f"{port}: SINK draws pin->ground (direction detected from the I-V sign)")
    else:
        nl.gated_current(f"{port}.idc", supply, port, core, gate,
                         f"{port}: SOURCE injects supply->pin (direction detected from the I-V "
                         f"sign)")
    head.append(f"//   bias {port}: {pol}, idc={idc:+.4g} A at {tnom_c:g} C, "
                f"dI/dT={ptat:+.4g} A/C, knee={side}"
                + (f" (vhi={vhi:g} V)" if side == "hi" else "") + f", ground {gnd}")

    if Cp > 0.0:
        nl.capacitor(f"{port}.yout.Cp", port, gnd, Cp, f"{port}: output capacitance")
    if wz and wp and wp > wz and g0 != 0.0:
        g0a = abs(g0)
        Rz = 1.0 / (g0a * (wp / wz - 1.0))
        Cz = g0a * (wp - wz) / (wz * wp)
        nz = nl.node(f"{pre}_nz")
        nl.comment(f"{port}: cascode/Wilson admittance zero as a PASSIVE lossy series C-R")
        nl.capacitor(f"{port}.yout.Cz", port, nz, Cz, f"{port}: admittance-zero cap")
        nl.conductance(f"{port}.yout.Rz", nz, gnd, 1.0 / Rz, f"{port}: admittance-zero loss")
        head.append(f"//     yout: g0={g0:.4g} S, Cp={Cp:.4g} F, zero "
                    f"{wz / (2 * math.pi):.4g} Hz / pole {wp / (2 * math.pi):.4g} Hz")
    else:
        head.append(f"//     yout: g0={g0:.4g} S, Cp={Cp:.4g} F")

    nb = blocks.get("noise") or {}
    white, flicker = _f(nb, "white", 0.0) or 0.0, _f(nb, "flicker", 0.0) or 0.0
    if white > 0.0:
        nl.white_noise(f"{port}.noise.white", port, gnd, white * white, f"{pre}_wht",
                       f"{port}: output-current white floor {white:.4g} A/rtHz")
    if flicker > 0.0:
        nl.flicker_noise(f"{port}.noise.flicker", port, gnd, flicker * flicker, f"{pre}_flk",
                         f"{port}: output-current 1/f -- native, as the ported emitter has always "
                         f"emitted a bias 1/f tail (the bias has no network to shape)")
    if white > 0.0 or flicker > 0.0:
        head.append(f"//     noise: white={white:.4g} A/rtHz, 1/f={flicker:.4g} A/rtHz at 1 Hz")
    if gdd != 0.0:
        head.append(f"//     psrr: gdd={gdd:+.4g} A/V"
                    + (f", pole {gdd_pole:.4g} Hz" if gdd_pole else ""))
    nl.blank()
    return head


# --------------------------------------------------------------------------- the module
def module_name(project: str, corner: str) -> str:
    return f"PMU_{project}_{corner}"


def _supply_tracker(nl: Netlist, supply: str, gnd: str, f_start: float) -> str:
    """One shared DC tracker per supply: `V(supply, vrf)` is the supply's ripple with its DC
    removed, so every PSRR path injects exactly nothing at DC for ANY supply value.

    The corner sits a hundredfold below the first swept point (-0.0004 dB there).  Every consumer
    only SENSES the node through a VCCS control, so it is never loaded and one tracker can serve
    every rail and bias -- which is why the old "buffer the PSRR reference" workaround, needed
    when a synthesized cap hung off it, has no counterpart here.
    """
    node = nl.node(f"{supply}_vrf")
    f_trk = max(float(f_start) * TRACKER_BELOW_BAND, 1.0e-3)
    C = 1.0 / (2.0 * math.pi * f_trk * TRACKER_R)
    nl.comment(f"supply {supply}: DC tracker, corner {f_trk:.4g} Hz ({TRACKER_BELOW_BAND:g} x the "
               f"first swept point) -> V({supply},{node}) is the ripple with its DC removed")
    nl.conductance(f"{supply}.trk.G", supply, node, 1.0 / TRACKER_R, "PSRR DC tracker resistor")
    nl.capacitor(f"{supply}.trk.C", node, gnd, C, "PSRR DC tracker cap")
    return node


def emit_va(port_fits, derived, corner: str, *, provenance=None, hb_robust: bool = True,
            project: str | None = None, module: str | None = None, flicker_mode: str = "bank",
            ls_default_on=(), temp_c=None, notes: list | None = None) -> str:
    """Build the Verilog-A module TEXT for one process corner.

    `port_fits` is what `pmukit.fit` produced (a list of BlockFit records) or a nested
    `{port: {block: params}}` mapping.  `derived` is contract 0b -- it supplies the ports, the
    split grounds, the stubs, the load grid, the VSET codes and the frequency band.  The
    provenance block is written by `DeliverableWriter.add_va`; `provenance` is accepted here only
    so the header can name the config and dataset the model was baked from.

    `hb_robust=False` is a DIAGNOSTIC escape hatch: it emits the complex PSRR section as the
    refuted synthesized R-L-C, which the conditioning lint then fires on.  It must never ship.

    `notes` (optional) collects the emitter's plain-language notes for report.md.
    """
    built = build_va(port_fits, derived, corner, provenance=provenance, hb_robust=hb_robust,
                     project=project, module=module, flicker_mode=flicker_mode,
                     ls_default_on=ls_default_on, temp_c=temp_c)
    if notes is not None:
        notes.extend(built["notes"])
    return built["text"]


def build_va(port_fits, derived, corner: str, *, provenance=None, hb_robust: bool = True,
             project: str | None = None, module: str | None = None, flicker_mode: str = "bank",
             ls_default_on=(), temp_c=None) -> dict:
    """`emit_va` plus the structure behind it, for the conditioning lint and for report.md.

    Returns `{"text", "netlist", "module", "grounds", "rails", "biases", "stubs", "skipped",
    "notes"}`.  The `netlist` is what `pmukit.emit.lint.report()` measures: the lint works on the
    ELEMENTS the emitter recorded, never on a regex over the text.
    """
    d = _as_derived(derived)
    project = project or d.project or "pmu"
    name = module or module_name(project, corner)
    fits = normalize_fits(port_fits)

    temps = [float(t) for t in ((d.temps_c or {}).get("points") or [25.0])]
    tnom = float(temp_c if temp_c is not None else (_nearest(temps, 25.0) or 25.0))
    vset_codes = list((d.vset or {}).get("codes") or []) or [None]
    f_start = float((d.freq or {}).get("start_hz", 10.0) or 10.0)

    by_pin = dict((d.grounds or {}).get("by_pin") or {})
    supplies = list((d.supply or {}).get("pins") or {})
    if not supplies:
        raise _err("the derived config names no supply pin.",
                   "every PSRR path is referenced to a supply pin, and that pin is the module's "
                   "input side; with none there is nothing to inject from.",
                   ["check that the netlist carries a VS_<pin> voltage source",
                    "re-run pmukit.config.derive() with the parsed pin table"])
    supply = supplies[0]

    nl = Netlist()
    head: list[str] = []

    # -- which ports can actually be emitted ------------------------------------------
    rail_blocks, bias_blocks, skipped, dc_tables = {}, {}, [], {}
    code0 = vset_codes[0]
    for p in (d.rails or {}):
        i_typ = _f((d.rails or {}).get(p, {}), "i_typ_a", 0.0) or 0.0
        blocks, _cells = blocks_by_port(fits, p, corner, temp_c=tnom, load_a=i_typ, vset=code0)
        dc_tables[p] = dc_by_vset(fits, p, corner, vset_codes, temp_c=tnom, load_a=i_typ)
        if dc_tables[p]:
            blocks = dict(blocks)
            blocks["dc"] = dc_tables[p].get(code0, blocks.get("dc") or {})
        why = ""
        if not blocks.get("zout"):
            why = "the Zout block is missing -- a rail without its output impedance has no model"
        elif _pick_vset(blocks.get("dc") or {}, "vout", vset_codes[0], None) is None:
            why = ("the DC block carries no `vout` -- emitting a regulated reference at a guessed "
                   "voltage would drive the consumer's circuit to the wrong level")
        if why:
            skipped.append((p, why))
        else:
            rail_blocks[p] = blocks
    for p in (d.biases or {}):
        blocks, _cells = blocks_by_port(fits, p, corner, temp_c=tnom, vset=code0)
        if not blocks.get("idc"):
            skipped.append((p, "the idc block is missing -- a current bias without its DC law "
                               "has no model"))
        else:
            bias_blocks[p] = blocks

    rails, biases = list(rail_blocks), list(bias_blocks)
    stubs = list(d.stubs or {})
    for p in supplies + rails + biases + stubs:
        nl.port(p)
    grounds: list[str] = []
    for p in supplies + rails + biases + stubs:
        g = by_pin.get(p)
        if g and g not in grounds:
            grounds.append(g)
    if not grounds:
        grounds = ["VSS"]
        nl.note("no per-pin ground net was recorded, so a single 'VSS' ground PIN is emitted. "
                "The module ground must be a real pin: an emitted module whose VSS was not tied "
                "once floated to -100 MV.")
    for g in grounds:
        nl.port(g)
    nl.ground = grounds[0]

    vrf = _supply_tracker(nl, supply, by_pin.get(supply, grounds[0]), f_start)
    if vset_codes != [None]:
        nl.parameter("vset", float(vset_codes[0]),
                     "output code; characterized codes: "
                     + ", ".join(str(c) for c in vset_codes))

    ls_ports: list[str] = []
    for p in rails:
        head += _rail_block(nl, p, by_pin.get(p, grounds[0]), supply, vrf, rail_blocks[p], d,
                            dc_table=dc_tables.get(p), ls_ports=ls_ports,
                            tnom_c=tnom, vset_codes=vset_codes,
                            i_typ=_f((d.rails or {}).get(p, {}), "i_typ_a", 0.0) or 0.0,
                            hb_robust=hb_robust, flicker_mode=flicker_mode,
                            ls_on=(p in set(ls_default_on)))
    for p in biases:
        head += _bias_block(nl, p, by_pin.get(p, grounds[0]), supply, vrf, bias_blocks[p],
                            (d.biases or {}).get(p, {}), tnom_c=tnom)
    head += _stub_block(nl, stubs, d, supply, by_pin, grounds[0], skipped)
    for p, why in skipped:
        head.append(f"//   port {p}: NOT EMITTED -- {why}")
        nl.note(f"{p}: not emitted -- {why}")

    _check_hybrid_coupled(nl, rails)
    text = _assemble(nl, name, corner, head, d, provenance, hb_robust, tnom, supplies, rails,
                     biases, stubs, grounds)
    return {"text": text, "netlist": nl, "module": name, "corner": corner,
            "grounds": grounds, "supplies": supplies, "rails": rails, "biases": biases,
            "stubs": stubs, "skipped": skipped, "notes": nl.notes, "temp_c": tnom,
            "ports": nl.ports, "ls_ports": ls_ports}


def _stub_block(nl: Netlist, stubs, d: DerivedConfig, supply: str, by_pin, gnd0: str,
                skipped: list) -> list[str]:
    """Stub ports: an ideal DC source at the pin's measured DC value, zero simulation behind it."""
    head = []
    for p in stubs:
        info = (d.stubs or {}).get(p, {}) or {}
        g = by_pin.get(p, gnd0)
        role = str(info.get("role") or "")
        dc_v, dc_a = info.get("dc_v"), info.get("dc_a")
        if role == "bias" and dc_a is not None:
            v = float(dc_a)
            if v >= 0.0:
                nl.ideal_isource(f"{p}.stub", supply, p, num(v), v,
                                 f"{p}: STUB, not modeled -- ideal {v:g} A source")
            else:
                nl.ideal_isource(f"{p}.stub", p, g, num(-v), -v,
                                 f"{p}: STUB, not modeled -- ideal {-v:g} A sink")
            head.append(f"//   stub {p}: stub, not modeled -- ideal {v:+.4g} A source, "
                        f"ground {g}")
        elif role != "bias" and dc_v is not None:
            nl.ideal_vsource(f"{p}.stub", p, g, num(float(dc_v)),
                             f"{p}: STUB, not modeled -- ideal {float(dc_v):g} V source")
            head.append(f"//   stub {p}: stub, not modeled -- ideal {float(dc_v):.4g} V "
                        f"source, ground {g}")
        else:
            nl.gleak(f"{p}.stub", p, 1.0e-9, g,
                     f"{p}: STUB with NO characterized DC value -- weakly tied, NOT driven")
            skipped.append((p, "stub with no characterized DC value -- the pin is weakly tied, "
                               "NOT driven at a guessed level"))
            nl.note(f"{p}: stub with no characterized DC value. A stub runs no simulation, so "
                    f"the only honest emission is a weak tie, not an ideal source at a guessed "
                    f"value. Put 'dc_v' (rail) or 'dc_a' (bias) into derived.stubs[{p!r}] to get "
                    f"the ideal source contract 4 asks for.")
            head.append(f"//   stub {p}: stub, not modeled -- AND no DC value was "
                        f"characterized, so the pin is weakly tied to {g}, NOT driven")
    return head


def _check_hybrid_coupled(nl: Netlist, rails) -> None:
    """A hybrid rail that writes the series-voltage bank but never splices it into the regulation
    ships a model with NO 1/f tail.  That bug shipped once; this refuses to let it ship again."""
    body = "\n".join(nl.body)
    statements = body.split(";")
    for p in rails:
        wrote = bool(re.search(rf'(?:white|flicker)_noise\([^)]*"{re.escape(p)}_nv', body))
        if not wrote:
            continue
        coupled = any(f"{p}_vrg) <+" in st and f"V({p}_nv" in st for st in statements)
        if not coupled:
            raise _err(f"rail {p}: the hybrid series-voltage noise bank was emitted but never "
                       f"coupled into the branch-A regulation.",
                       "a series-EMF bank that nothing reads is a zero-gain orphan, so the "
                       "deployed model would ship only the white floor and the entire 1/f tail "
                       "would vanish (~306x at 100 Hz) -- exactly the bug that shipped once.",
                       ["emit the bank through _noise_bank(series=True) and splice its terms "
                        "into the regulation with Netlist.series_emf_term"])


def _assemble(nl: Netlist, name, corner, head, d: DerivedConfig, provenance, hb_robust, tnom,
              supplies, rails, biases, stubs, grounds) -> str:
    ports = nl.ports
    decl = nl.declarations()
    body = nl.text()
    f_max = float((d.freq or {}).get("stop_hz", 0.0) or 0.0)
    prov = ""
    if provenance is not None:
        prov = (f"// built from config {getattr(provenance, 'config_sha', '') or '(none)'}"
                f" / dataset {getattr(provenance, 'dataset_sha', '') or '(none)'}\n")
    warn = ("" if hb_robust else
            "//\n// *** hb_robust=False: this module contains the REFUTED synthesized R-L-C\n"
            "// *** complex PSRR section. It is a DIAGNOSTIC build and must never ship.\n")
    return f"""// ============================================================
// {name} -- behavioral PMU model, process corner {corner}
// Generated by pmukit.emit. Every construct comes from the HB-safe primitive whitelist
// (pmukit/emit/primitives.py): no s-domain filter primitive, no synthesized inductor,
// no saturating DC compliance, no symmetric compliance knee, no output-side clamp.
{prov}{warn}//
// Interface: supply {', '.join(supplies)} | rails {', '.join(rails) or '(none)'} |
//            biases {', '.join(biases) or '(none)'} | stubs {', '.join(stubs) or '(none)'} |
//            ground pins {', '.join(grounds)} -- REAL pins, never an implicit 0
// Baked at : process {corner}, {tnom:g} C nominal. Temperature is CONTINUOUS inside this corner
//            through the rail dVout/dT and the bias dI/dT terms; the small-signal blocks are
//            baked at the nominal temperature and the rail's typical load.
// Band     : characterized up to {f_max:.4g} Hz. Outside envelope.json the model extrapolates,
//            and report.md marks that RED.
//
{chr(10).join(head) if head else '//   (no port was emitted)'}
// ============================================================
`include "constants.vams"
`include "disciplines.vams"

module {name}({', '.join(ports)});
  inout {', '.join(ports)};
  electrical {', '.join(ports)};
{decl}

  analog begin
{body}
  end
endmodule
"""
