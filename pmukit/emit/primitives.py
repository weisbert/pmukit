# from LDO_modeling/harness/emit_pmu_model.py @ d2c5b80
"""The HB-safe primitive whitelist -- the hard constraint of the deliverable.

Everything downstream of this module runs inside somebody else's harmonic-balance simulation,
so the emitter may only write a CLOSED set of constructs.  The whitelist exists because every
item OUTSIDE it broke a real simulation; `WHITELIST` carries the one-line reason per entry and
`FORBIDDEN` carries the failure each banned construct caused.

The enforcement is structural, not advisory:

  * `Netlist` is the ONLY way this package produces text.  There is no `add_raw`; every public
    method is a whitelist entry and records a structured `Element` alongside the text, which is
    what `pmukit.emit.lint` then measures.
  * `Netlist.text()` re-scans the assembled body for `FORBIDDEN` and raises before returning, so
    a banned construct cannot reach a file even if a future builder is written carelessly.

Two emit-time facts are carried here and must not drift:

  * `$temperature` in Verilog-A is KELVIN (328.15 = 55 C).  The conversion happens ONCE, in
    `Netlist.temp_c_var`, against the named constant `KELVIN_TO_C`; nothing else may touch
    `$temperature`.
  * Rail intrinsic noise is emitted on the 4kT * 300 K FIT basis, never `4kT*$temperature`: the
    gains were backed out at 300 K, so re-scaling by `$temperature/300` adds a non-physical
    sqrt(T/300) to a spectrum that is nearly temperature independent (+0.39 dB at 55 C, measured).
    `T_NOISE_FIT_K` is that constant; the shaped-noise nodes are its only users.

Every node-referencing method takes an explicit `gnd`, because each rail and each bias returns to
ITS OWN ground pin (split grounds) and a filter hung off the wrong return would silently pick up
another domain's bounce.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..errors import PmuError

__all__ = [
    "WHITELIST", "FORBIDDEN", "Element", "Netlist",
    "C_NOM", "GM_FLOOR", "GM_CEIL", "GM_SOFT", "GAIN_MAX", "KELVIN_TO_C", "T_NOISE_FIT_K",
    "KT4_FIT",
    "OFF_OHM", "biquad_from_doublet", "balanced_gain", "num",
]

# --------------------------------------------------------------------------- constants
#: Nominal filter capacitance.  Every synthesized filter node uses the SAME small cap and puts
#: the pole in the transconductance (`gm = C*w0`), so the pole frequency is DECOUPLED from
#: element size and no node admittance blows up or underflows at a high harmonic.
C_NOM = 1.0e-12
#: Transconductance floor: below this a controlled source underflows against O(1) terms.  A
#: section whose `C*w0` lands below it gets a LARGER cap instead (the transfer is invariant).
GM_FLOOR = 1.0e-11
#: ... and the ceiling, which the band limit needs: its pole sits far above the band, so
#: `gm = C_NOM*w0` would be several siemens.  The cap is SHRUNK instead (again transfer
#: invariant), which keeps every emitted transconductance small.
GM_CEIL = 0.1
#: |coefficient| at or above this is the documented coupled-HB hazard: a large residue in the
#: SHARED supply-node Jacobian.  Such a tap is re-balanced through an internal gain.
GM_SOFT = 1.0
#: Cap on that internal gain, so an internal node never carries an absurd multiple of its input.
GAIN_MAX = 1.0e4
#: `$temperature` is KELVIN.  This is the only place the offset is written.
KELVIN_TO_C = -273.15
#: The rail-noise FIT basis.  Never `$temperature` (see the module docstring).
T_NOISE_FIT_K = 300.0
#: 4kT at the fit basis, used as the reference level of a shaped-noise node.
KT4_FIT = 4.0 * 1.380649e-23 * T_NOISE_FIT_K
#: A fitted branch at or above this resistance is the fitter's explicit OFF sentinel; emitting it
#: would add a ~1e-9 S near-null column for no transfer at all.
OFF_OHM = 1.0e8

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


# --------------------------------------------------------------------------- the whitelist
#: name -> (what it writes, why it is ON the list).  One line each; report.md prints this table.
WHITELIST: dict[str, tuple[str, str]] = {
    "resistor": (
        "V(a,b) <+ R*I(a,b)",
        "the stiff regulation branch: 1/R holds the DC pin at EVERY excursion. A saturating "
        "'DC current compliance' is banned -- its knee is a voltage Icomp*Ra, ~1-5 mV with the "
        "fitted Ra, so a few mV off regulation the branch becomes a zero-conductance current "
        "source, the DC pin is lost and the FF corner ran away to 2.31e6 V."),
    "conductance": (
        "I(a,b) <+ G*V(a,b)",
        "the current-type twin of a resistor: no extra branch unknown, so a current-type branch "
        "is used wherever a voltage-type one is not strictly needed."),
    "capacitor": (
        "I(a,b) <+ C*ddt(V(a,b))",
        "the only reactance that stays safe at a high harmonic when C is held small; every "
        "synthesized filter here uses C = C_NOM and puts the pole in the transconductance."),
    "inductor": (
        "I(a,b) <+ idt(V(a,b))/L",
        "allowed ONLY for a FITTED physical branch inductance (the Zout ladder). A SYNTHESIZED "
        "inductor is banned: a kHz pole needs thousands of henries (8890 H in one real case) and "
        "its 1/(jwL) admittance underflows against O(1) terms at the 77 GHz top harmonic -> "
        "singular Jacobian, 'matrix singular during decomposition'."),
    "vccs": (
        "I(out_p,out_n) <+ gm*V(ctrl_p,ctrl_n)",
        "every coupling in the model -- PSRR taps, noise taps, filter transconductors -- is one "
        "of these, with the coefficient kept small (see gm_c_biquad / balanced_gain)."),
    "gm_c_lowpass": (
        "one gm-C pole: a cap to ground plus a self transconductance gm = C*w",
        "the REQUIRED realization of a first-order section: the pole frequency is decoupled from "
        "element size, so the node admittance stays O(<=1) S at the top harmonic."),
    "gm_c_biquad": (
        "two gm-C integrator nodes realizing (b0 + b1*s)/(1 + s/(Q*w0) + (s/w0)^2)",
        "the REQUIRED realization of a low-frequency complex 2nd-order PSRR section AND of any "
        "large near-cancelling first-order doublet. The R-L-C twin is banned: rescaling the L/C "
        "split only MOVES the extreme (8890 H -> 8.89 uF, wC = 4.3e6 S -> NaN); the gm-C form "
        "turns 4.3e6 S into 0.487 S with an identical transfer."),
    "gleak": (
        "I(n,gnd) <+ Gleak*V(n,gnd) on an otherwise purely capacitive node",
        "a pure capacitive node has no DC path, so the DC solve has nothing to pin it with. The "
        "biquad's design is corrected for the leak EXACTLY, so the transfer is unchanged."),
    "band_limited_tap": (
        "a gm-C one-pole in front of an otherwise flat supply->output injection",
        "a flat-to-infinity G0 injection never rolls off: it is the only PSRR path still at full "
        "strength at the top HB harmonic. The corner is derived from the project's care_up_to_hz "
        "with a documented margin, never hard-coded."),
    "white_noise": (
        "I(a,b) <+ white_noise(pwr, name)",
        "the flat part of any fitted spectrum, emitted as the FITTED PSD directly."),
    "noise_shaped_node": (
        "an R||C node carrying white_noise(4kT*300/R) plus a VCCS tap",
        "one Lorentzian of the validated noise bank. The 4kT*300 basis is pinned: the gains were "
        "backed out at 300 K, so 4kT*$temperature would add a non-physical sqrt(T/300) "
        "(+0.39 dB at 55 C, measured) to a spectrum that is nearly temperature independent."),
    "flicker_noise": (
        "I(a,b) <+ flicker_noise(pwr, 1.0, name)",
        "native 1/f, valid in pnoise/hbnoise. Used for the CURRENT BIAS (which has no network to "
        "shape) as the ported emitter always did; for a RAIL it is opt-in, the default staying "
        "the validated Lorentzian bank."),
    "one_sided_gate": (
        "max(x, 0.0) inside a compliance or deadzone gate",
        "a symmetric knee |vhi - Vo| climbs back to 1 above the ceiling and the sink spuriously "
        "REOPENS to full current where the real device is starved (Spectre-confirmed on a "
        "deployed .va: I(3V)/I(1V) = 1.005)."),
    "sqrt_floored_pow": (
        "pow(sqrt(u*u + eps)/vk, p)",
        "(V/vk)^p with p < 1 blows the OP Jacobian at Vo = 0; the sqrt floor keeps it finite."),
    "bounded_reverse_emf": (
        "a source-gated, voltage-BOUNDED series term inside the regulation",
        "the shipped Route-1 unload discharge. An OUTPUT-side clamp and any stateful/leaky "
        "discharge gate are both refuted with measurements; a bounded series EMF keeps ~1/Ra "
        "conductance so the DC pin can never be lost."),
    "odd_current_assist": (
        "I(o,vrg) <+ -iaG*tanh(verr*abs(verr)/iaV^2)",
        "the compressive class-AB load-step assist. It is ODD with f'(0) = 0 EXACTLY, so its "
        "small-signal conductance at the operating point is zero and Zout/PSRR/noise stay "
        "bit-identical."),
    "ideal_vsource": (
        "V(a,b) <+ expr",
        "the regulated reference node and a stubbed rail pin. Used deliberately and sparingly: a "
        "voltage-type branch adds an unknown, so it is only used where a current-type branch "
        "cannot express the thing (an ideal reference)."),
    "ideal_isource": (
        "I(a,b) <+ expr",
        "a stubbed bias pin, emitted at its measured DC value with zero simulation behind it."),
}

#: pattern -> the failure it caused.  `Netlist.text()` refuses to return text that matches.
FORBIDDEN: dict[str, str] = {
    r"\blaplace_[a-z]{2}\b":
        "laplace_nd and friends are not PSS/HB-robust; synthesize passive RLC plus controlled "
        "sources instead.",
    r"\bzi_[a-z]{2}\b":
        "the z-domain filter primitives are the discrete twins of laplace_* and are equally "
        "unsafe in PSS/HB.",
    r"\blimexp\b":
        "limexp hides a convergence problem behind a clamp; this emitter has no exponential "
        "device law to damp.",
    r"\$table_model":
        "OpenVAF does not support $table_model (rc=65) and it resolves a BARE filename against "
        "the run directory; inline the curve instead.",
    r"\$temperature\s*\)?\s*[/*]\s*3\.?0*[eE]?\+?0*2?\b":
        "rail noise re-scaled by $temperature/300 adds a non-physical sqrt(T/300) to a spectrum "
        "that is nearly temperature independent (+0.39 dB at 55 C, measured).",
    r"white_noise\s*\([^)]*\$temperature":
        "every rail white_noise is pinned to the 4kT*300 K FIT basis, never $temperature.",
    r"\babs\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*-\s*V\s*\(":
        "a symmetric compliance knee |vhi - Vo| reopens the sink above the ceiling; the knee must "
        "be one-sided max(., 0).",
    r"\bsqrt\s*\(\s*V\s*\([^)]*\)\s*\*\s*V\s*\(":
        "sqrt(Vo*Vo) is the symmetric knee again, spelled as a square root of a square.",
}
_FORBIDDEN_RE = [(re.compile(p), why) for p, why in FORBIDDEN.items()]


# --------------------------------------------------------------------------- helpers
def num(x) -> str:
    """One stable number spelling for the whole emitter (6 significant digits, always a float)."""
    v = float(x)
    if not math.isfinite(v):
        raise PmuError(
            what=f"the emitter was asked to write a non-finite number ({x!r}).",
            why="a NaN or an infinity in a .va is a silent wrong answer in the consumer's "
                "simulation: Spectre elaborates it and produces garbage rather than failing.",
            do=["check the fitted parameter that produced it -- a missing measurement must reach "
                "the emitter as `missing`, never as a NaN parameter",
                "re-run the fit for this cell, or drop the block from the deliverable"],
            where="pmukit/emit/primitives.py:num",
        )
    return f"{v:.6e}"


def _check_node(name: str, where: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise PmuError(
            what=f"{name!r} is not a usable Verilog-A node name.",
            why="node names become identifiers in the emitted module, so they must match "
                "[A-Za-z_][A-Za-z0-9_]* -- a dot, a dash or a leading digit would not compile.",
            do=["rename the pin in the netlist, or map it to a plain name in the project config"],
            where=where,
        )
    return name


def _filter_cap(w0: float) -> float:
    """The cap of a synthesized filter node.

    A gm-C section's transfer depends only on `gm/C`, so the SPLIT is free and is spent entirely
    on conditioning: start from `C_NOM` and move it only far enough to land `gm = C*w0` inside
    `[GM_FLOOR, GM_CEIL]`.  That is what keeps both a kHz PSRR pole and a 400 GHz band limit out
    of the extremes the synthesized R-L-C could not avoid.
    """
    w0 = float(w0)
    return min(max(C_NOM, GM_FLOOR / w0), GM_CEIL / w0)


def balanced_gain(drive_gm: float, taps) -> tuple[float, float, list[float]]:
    """Split a large coupling coefficient between an internal gain and the output tap.

    A section whose output tap would be a LARGE transconductance (the +17.23 / -17.23 doublet
    case) is a near-null-space direction in the shared supply-node Jacobian.  The transfer
    `tap * node(v_in)` is invariant under `node -> A*node`, `tap -> tap/A`, so choosing
    `A = sqrt(max|tap| / drive_gm)` makes the input transconductance and the output tap EQUAL at
    `sqrt(max|tap| * drive_gm)` -- the split that minimizes the largest coefficient.

    Returns `(A, drive_gm*A, [tap/A ...])`.  `A` is 1.0 (nothing changes) unless a tap reaches
    `GM_SOFT`, and it is clamped to `GAIN_MAX` so an internal node never carries an absurd
    multiple of its input.
    """
    peak = max((abs(float(t)) for t in taps), default=0.0)
    if peak < GM_SOFT or drive_gm <= 0.0 or peak <= drive_gm:
        return 1.0, float(drive_gm), [float(t) for t in taps]
    a = min(math.sqrt(peak / drive_gm), GAIN_MAX)
    return a, float(drive_gm) * a, [float(t) / a for t in taps]


def biquad_from_doublet(g_i: float, w_i: float, g_j: float, w_j: float):
    """Merge a near-cancelling first-order residue PAIR into ONE 2nd-order section.

        G_i/(1 + s/w_i) + G_j/(1 + s/w_j) = (b0 + b1*s)/(1 + s/(Q*w0) + (s/w0)^2)

    with `b0 = G_i + G_j` (the small residual the pair cancels to), `b1 = G_i/w_j + G_j/w_i`,
    `w0 = sqrt(w_i*w_j)` and `Q = w0/(w_i + w_j)`.  Exact, not an approximation.

    This is the EMIT-side rule: the pair is harmless alone and perfect in AC, but in a coupled
    oscillator HB the +-17.2 S pair is a near-null-space direction in the SHARED supply-node
    Jacobian, and the singular column surfaces at the package or at neighbouring transistors,
    never at the model.
    """
    gi, wi, gj, wj = float(g_i), float(w_i), float(g_j), float(w_j)
    if wi <= 0.0 or wj <= 0.0:
        raise PmuError(
            what=f"cannot merge a PSRR doublet with a non-positive pole ({wi:g}, {wj:g} rad/s).",
            why="the merged section's w0 is sqrt(w_i*w_j) and its Q is w0/(w_i+w_j); a "
                "non-positive pole makes both undefined.",
            do=["check the pole_i_hz values the PSRR fit produced for this cell"],
            where="pmukit/emit/primitives.py:biquad_from_doublet",
        )
    return gi + gj, gi / wj + gj / wi, math.sqrt(wi * wj), math.sqrt(wi * wj) / (wi + wj)


# --------------------------------------------------------------------------- elements
@dataclass
class Element:
    """One emitted branch, recorded structurally so the conditioning lint can measure it."""

    kind: str
    """resistor | conductance | capacitor | inductor | vccs | vsource | isource | noise."""
    name: str
    """`<port>.<block>.<symbol>` -- what the lint report names."""
    nodes: tuple[str, ...]
    value: float
    rule: str
    """Which `WHITELIST` entry produced it."""
    detail: str = ""
    stiff: bool = False
    """A voltage-defined ideal source: it has no finite admittance and pins its first node."""
    controlled: bool = False
    """A VCCS: an OFF-diagonal Jacobian entry, not a node self-admittance."""

    def admittance(self, f_hz: float) -> float:
        """|Y| of this branch at `f_hz` [S].  `nan` for a stiff (voltage-defined) branch."""
        w = 2.0 * math.pi * float(f_hz)
        v = float(self.value)
        if self.kind in ("conductance", "vccs"):
            return abs(v)
        if self.kind == "resistor":
            return 1.0 / abs(v) if v else float("inf")
        if self.kind == "capacitor":
            return w * abs(v)
        if self.kind == "inductor":
            return 1.0 / (w * abs(v)) if (w and v) else float("inf")
        return float("nan")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "nodes": list(self.nodes),
                "value": self.value, "rule": self.rule, "detail": self.detail,
                "stiff": self.stiff, "controlled": self.controlled}


# --------------------------------------------------------------------------- the builder
class Netlist:
    """The only writer of Verilog-A text in this package.

    Every public method is one `WHITELIST` entry: it appends the text AND records an `Element`,
    so `pmukit.emit.lint` measures exactly what was written.  There is deliberately no method
    that takes free-form text.
    """

    def __init__(self, ground: str = "gnd") -> None:
        self.ground = ground
        self.body: list[str] = []
        self.elements: list[Element] = []
        self._nodes: list[str] = []
        self._ports: list[str] = []
        self._locals: list[str] = []
        self._localnames: set[str] = set()
        self._params: list[str] = []
        self._reals: list[str] = []
        self._realnames: set[str] = set()
        self._pinned: set[str] = set()
        self._notes: list[str] = []

    # -- declarations --------------------------------------------------------
    def port(self, name: str) -> str:
        n = _check_node(name, "module port list")
        if n not in self._ports:
            self._ports.append(n)
        return n

    def node(self, name: str) -> str:
        """Declare an INTERNAL node (never a port)."""
        n = _check_node(name, "internal node")
        if n not in self._nodes and n not in self._ports:
            self._nodes.append(n)
        return n

    def localparam(self, name: str, value, comment: str = "") -> str:
        n = _check_node(name, "localparam")
        if n not in self._localnames:
            self._localnames.add(n)
            self._locals.append(f"  localparam real {n} = {num(value)};"
                                + (f"   // {comment}" if comment else ""))
        return n

    def parameter(self, name: str, value, comment: str = "") -> str:
        """An INSTANCE parameter -- the consumer can set it in their schematic."""
        n = _check_node(name, "instance parameter")
        self._params.append(f"  parameter real {n} = {num(value)};"
                            + (f"   // {comment}" if comment else ""))
        return n

    def real(self, name: str, comment: str = "") -> str:
        n = _check_node(name, "real variable")
        if n not in self._realnames:
            self._realnames.add(n)
            self._reals.append(f"  real {n};" + (f"   // {comment}" if comment else ""))
        return n

    def comment(self, text: str) -> None:
        self.body.append(f"    // {text}")

    def blank(self) -> None:
        self.body.append("")

    def note(self, text: str) -> None:
        """A machine-readable note for report.md (never emitted into the .va body)."""
        self._notes.append(str(text))

    @property
    def notes(self) -> list[str]:
        return list(self._notes)

    @property
    def ports(self) -> list[str]:
        return list(self._ports)

    @property
    def internal_nodes(self) -> list[str]:
        return list(self._nodes)

    @property
    def pinned_nodes(self) -> set[str]:
        """Nodes held by an ideal voltage source: they carry no free unknown."""
        return set(self._pinned)

    def _g(self, gnd: str | None) -> str:
        return gnd if gnd else self.ground

    # -- the temperature contract -------------------------------------------
    def temp_c_var(self) -> str:
        """The ONE place `$temperature` is read.  Returns the name of the degC variable.

        `$temperature` is KELVIN (328.15 = 55 C); the offset is the named `KELVIN_TO_C`.
        """
        name = "tdegc"
        if name not in self._realnames:
            self.real(name, "ambient in degC -- $temperature is KELVIN, converted ONCE here")
            self.localparam("KELVIN_TO_C", KELVIN_TO_C,
                            "$temperature is KELVIN (328.15 = 55 C)")
            self.body.insert(0, f"    {name} = $temperature + KELVIN_TO_C;"
                                f"   // the ONLY $temperature read in this module")
        return name

    # -- whitelist: passive --------------------------------------------------
    def resistor(self, name: str, a: str, b: str, R: float, detail: str = "") -> None:
        """`V(a,b) <+ R*I(a,b)` -- the stiff branch. Use for the regulation, never a clamp."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    V({a}, {b}) <+ {num(R)}*I({a}, {b});"
                         + (f"   // {detail}" if detail else ""))
        self._add("resistor", name, (a, b), R, "resistor", detail)

    def resistor_expr(self, name: str, a: str, b: str, R: float, extra: str = "",
                      detail: str = "") -> None:
        """The regulation resistor plus an extra SERIES term.

        `extra` may only come from `bounded_reverse_emf_term` / `series_emf_term`; both produce a
        term that is zero in VALUE and in SLOPE at the operating point, so Zout/PSRR/noise/DC
        stay bit-identical.
        """
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    V({a}, {b}) <+ {num(R)}*I({a}, {b}){extra};"
                         + (f"   // {detail}" if detail else ""))
        self._add("resistor", name, (a, b), R, "resistor", detail)

    def conductance(self, name: str, a: str, b: str, G: float, detail: str = "") -> None:
        """`I(a,b) <+ G*V(a,b)` -- the current-type twin of a resistor."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ {num(G)}*V({a}, {b});"
                         + (f"   // {detail}" if detail else ""))
        self._add("conductance", name, (a, b), G, "conductance", detail)

    def capacitor(self, name: str, a: str, b: str, C: float, detail: str = "") -> None:
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ {num(C)}*ddt(V({a}, {b}));"
                         + (f"   // {detail}" if detail else ""))
        self._add("capacitor", name, (a, b), C, "capacitor", detail)

    def inductor(self, name: str, a: str, b: str, L: float, detail: str = "") -> None:
        """A FITTED physical branch inductance only.  A synthesized one is banned (see WHITELIST)."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ idt(V({a}, {b}))/{num(L)};"
                         + (f"   // {detail}" if detail else ""))
        self._add("inductor", name, (a, b), L, "inductor", detail)

    def damped_inductor(self, name: str, a: str, b: str, L: float, Rpl: float | None,
                        detail: str = "") -> None:
        """One (L||Rpl) rising-shelf ladder stage.

        `Rpl >= OFF_OHM` (or None) is the fitter's UNDAMPED sentinel and the damping resistor is
        simply omitted: emitting a 1e9 ohm branch adds a ~1e-9 S near-null column and no transfer.
        """
        a, b = _check_node(a, name), _check_node(b, name)
        if Rpl is None or float(Rpl) >= OFF_OHM:
            self.inductor(name, a, b, L, detail)
            if Rpl is not None:
                self.note(f"{name}: Rpl = {float(Rpl):.3g} ohm is the fitter's undamped sentinel "
                          f"-> the damping resistor is omitted ({1.0 / float(Rpl):.1e} S, a "
                          f"near-null column with no transfer)")
            return
        self.body.append(f"    I({a}, {b}) <+ idt(V({a}, {b}))/{num(L)}"
                         f" + V({a}, {b})/{num(Rpl)};" + (f"   // {detail}" if detail else ""))
        self._add("inductor", name, (a, b), L, "inductor", detail)
        self._add("resistor", name + ".Rpl", (a, b), Rpl, "resistor", detail)

    def gleak(self, name: str, n: str, G: float, gnd: str | None = None,
              detail: str = "") -> None:
        """The DC path of an otherwise purely capacitive node."""
        g = self._g(gnd)
        n = _check_node(n, name)
        self.body.append(f"    I({n}, {g}) <+ {num(G)}*V({n}, {g});"
                         f"   // Gleak: DC path for a capacitive node"
                         + (f" ({detail})" if detail else ""))
        self._add("conductance", name, (n, g), G, "gleak", detail)

    # -- whitelist: controlled sources --------------------------------------
    def vccs(self, name: str, out_p: str, out_n: str, ctrl_p: str, ctrl_n: str, gm: float,
             detail: str = "") -> None:
        """`I(out_p,out_n) <+ gm*V(ctrl_p,ctrl_n)` -- current flows OUT of `out_p`."""
        for x in (out_p, out_n, ctrl_p, ctrl_n):
            _check_node(x, name)
        self.body.append(f"    I({out_p}, {out_n}) <+ {num(gm)}*V({ctrl_p}, {ctrl_n});"
                         + (f"   // {detail}" if detail else ""))
        self._add("vccs", name, (out_p, out_n, ctrl_p, ctrl_n), gm, "vccs", detail,
                  controlled=True)

    def inject(self, name: str, node: str, ctrl_p: str, ctrl_n: str, gm: float,
               gnd: str | None = None, detail: str = "") -> None:
        """Add `gm*V(ctrl_p,ctrl_n)` INTO `node`.

        THE SIGN RULE: `I(node, gnd) <+ X` removes current FROM `node`, while a mirrored source
        injects INTO it.  A previous emitter shipped PSRR inverted by 180 degrees on exactly this
        point, so everything that must ADD current at a node goes through here.
        """
        self.vccs(name, node, self._g(gnd), ctrl_p, ctrl_n, -float(gm),
                  detail or "inject INTO the node (I(n,gnd) <+ -gm*V() ADDS current at n)")

    # -- whitelist: synthesized filters --------------------------------------
    def gm_c_lowpass(self, name: str, out: str, in_p: str, in_n: str, w0: float,
                     gain: float = 1.0, gnd: str | None = None, detail: str = "") -> str:
        """One gm-C pole: `V(out,gnd) = gain * V(in_p,in_n) / (1 + s/w0)`.

        The cap is held at `C_NOM` (raised only when that would push `gm` under `GM_FLOOR`) and
        the pole is set by `gm = C*w0`, so the pole frequency is decoupled from element size and
        the node admittance stays O(w*C_NOM) at the top harmonic.
        """
        g = self._g(gnd)
        out = self.node(out)
        w0 = float(w0)
        if w0 <= 0.0:
            raise PmuError(
                what=f"gm-C low-pass {name!r} was given a non-positive corner ({w0:g} rad/s).",
                why="the transconductance is gm = C*w0; a non-positive w0 has no realization.",
                do=["check the pole the fit produced for this section"],
                where="pmukit/emit/primitives.py:gm_c_lowpass")
        C = _filter_cap(w0)
        gm_p = C * w0
        self.capacitor(f"{name}.C", out, g, C, detail or f"{name}: gm-C pole cap")
        self.conductance(f"{name}.gm_p", out, g, gm_p,
                         f"{name}: gm = C*w0 sets the {w0 / (2 * math.pi):.4g} Hz pole")
        self.inject(f"{name}.gm_in", out, in_p, in_n, gm_p * float(gain), g,
                    f"{name}: input transconductance (dc gain {float(gain):g})")
        return out

    def gm_c_biquad(self, name: str, b0: float, b1: float, w0: float, q: float,
                    in_p: str, in_n: str, out_node: str, gnd: str | None = None,
                    detail: str = "") -> None:
        """Realize `i_out = (b0 + b1*s)/(1 + s/(q*w0) + (s/w0)^2) * V(in_p,in_n)` INTO `out_node`.

        Two integrator nodes, both with a `C_NOM` cap, `gm = C*w0` and damping `gm/q`.  The LP
        node's `Gleak` is folded into the design EXACTLY, so the leak that keeps the DC solve
        well-posed costs nothing in accuracy:

            gm_d = C*w0/q - Gleak,      gm = sqrt((C*w0)^2 - gm_d*Gleak)
            i    = (w0^2*C/gm_in)*b1 * V_bp + (w0^2*C^2/(gm*gm_in))*(b0 - b1*Gleak/C) * V_lp

        This is the required realization of a low-frequency complex 2nd-order PSRR section and of
        any large near-cancelling first-order doublet -- see `WHITELIST['gm_c_biquad']`.
        """
        g = self._g(gnd)
        w0, q = float(w0), float(q)
        if w0 <= 0.0 or q <= 0.0:
            raise PmuError(
                what=f"gm-C biquad {name!r} was given w0 = {w0:g} rad/s, Q = {q:g}.",
                why="the realization needs a positive resonant frequency and a positive Q; the "
                    "transconductances are C*w0 and C*w0/Q.",
                do=["check pc_w0 / pc_q (or the merged doublet) the PSRR fit produced"],
                where="pmukit/emit/primitives.py:gm_c_biquad")
        C = _filter_cap(w0)
        # the leak is bounded by 0.05*C*w0*q so gm^2 = (C*w0)^2 - gm_d*gleak stays positive for
        # ANY Q, and floored at GM_FLOOR so it does not underflow at the top harmonic
        gleak = min(max(1.0e-3 * C * w0, GM_FLOOR), 0.05 * C * w0 * q)
        gm_d = C * w0 / q - gleak
        gm = math.sqrt(max((C * w0) ** 2 - gm_d * gleak, 0.0))
        gm_in_nom = C * w0
        k_bp = (w0 * w0 * C / gm_in_nom) * float(b1)
        k_lp = (w0 * w0 * C * C / (gm * gm_in_nom)) * (float(b0) - float(b1) * gleak / C)
        a, gm_in, (k_bp, k_lp) = balanced_gain(gm_in_nom, [k_bp, k_lp])
        if a != 1.0:
            self.note(f"{name}: output taps rebalanced through an internal gain of {a:.4g} so no "
                      f"coefficient reaches {GM_SOFT:g} S (transfer unchanged)")

        bp, lp = self.node(f"{name}_bp"), self.node(f"{name}_lp")
        self.comment(f"gm-C biquad {name}: (b0 + b1*s)/(1 + s/(Q*w0) + (s/w0)^2), "
                     f"f0 = {w0 / (2 * math.pi):.4g} Hz, Q = {q:.3g}"
                     + (f" -- {detail}" if detail else ""))
        self.capacitor(f"{name}.Cbp", bp, g, C, f"{name}: band-pass integrator cap")
        self.capacitor(f"{name}.Clp", lp, g, C, f"{name}: low-pass integrator cap")
        self.conductance(f"{name}.gm_d", bp, g, gm_d, f"{name}: damping gm = C*w0/Q - Gleak")
        self.gleak(f"{name}.gleak", lp, gleak, g, f"{name}: folded into the design exactly")
        self.inject(f"{name}.gm_in", bp, in_p, in_n, gm_in, g, f"{name}: input transconductance")
        self.vccs(f"{name}.gm_f", bp, g, lp, g, gm,
                  f"{name}: low-pass feedback into the band-pass node")
        self.inject(f"{name}.gm_i", lp, bp, g, gm, g,
                    f"{name}: band-pass integrated into the low-pass node")
        self.inject(f"{name}.tap_bp", out_node, bp, g, k_bp, g, f"{name}: b1*s numerator tap")
        self.inject(f"{name}.tap_lp", out_node, lp, g, k_lp, g, f"{name}: b0 numerator tap")

    def band_limited_tap(self, name: str, gain: float, w_bl: float, in_p: str, in_n: str,
                         out_node: str, gnd: str | None = None, detail: str = "") -> None:
        """A flat `gain` injection, band-limited by one gm-C pole at `w_bl`.

        A flat-to-infinity supply->output injection is the only PSRR path that never rolls off; at
        the top HB harmonic it is still at full strength.  The corner comes from the project's
        `care_up_to_hz` times a documented margin, never from a hard-coded frequency.
        """
        g = self._g(gnd)
        a, _, (tap,) = balanced_gain(C_NOM * float(w_bl), [float(gain)])
        if a != 1.0:
            self.note(f"{name}: flat injection {float(gain):+.4g} S rebalanced through an "
                      f"internal gain of {a:.4g} (transfer unchanged)")
        node = self.gm_c_lowpass(f"{name}_bl", f"{name}_bl", in_p, in_n, w_bl, a, g,
                                 detail or f"{name}: band limit at "
                                           f"{w_bl / (2 * math.pi):.4g} Hz")
        self.inject(f"{name}.tap", out_node, node, g, tap, g,
                    f"{name}: band-limited flat injection ({float(gain):+.4g} S in band)")

    # -- whitelist: noise ----------------------------------------------------
    def white_noise(self, name: str, a: str, b: str, psd: float, handle: str,
                    detail: str = "") -> None:
        """`I(a,b) <+ white_noise(psd, handle)` with the FITTED PSD written directly."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ white_noise({num(psd)}, \"{handle}\");"
                         + (f"   // {detail}" if detail else ""))
        self._add("noise", name, (a, b), psd, "white_noise", detail)

    def flicker_noise(self, name: str, a: str, b: str, psd_1hz: float, handle: str,
                      detail: str = "") -> None:
        """`I(a,b) <+ flicker_noise(psd_1hz, 1.0, handle)` -- native, exact 1/f."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ flicker_noise({num(psd_1hz)}, 1.0, \"{handle}\");"
                         + (f"   // {detail}" if detail else ""))
        self._add("noise", name, (a, b), psd_1hz, "flicker_noise", detail)

    def noise_shaped_node(self, name: str, node: str, corner_hz: float, handle: str,
                          gnd: str | None = None, detail: str = "") -> tuple[str, float]:
        """An R||C node carrying `white_noise(4kT*300/R)` -- one Lorentzian of the noise bank.

        The R/C split is transfer-invariant at fixed R*C, so C is pinned at `C_NOM` and R follows:
        the node's admittance at the top harmonic is then O(w*C_NOM) like every other synthesized
        node.  Returns `(node, v_ref)` with `v_ref = sqrt(4kT*300*R)` the node's low-frequency
        noise amplitude, so the caller's tap is simply `amplitude / v_ref`.
        """
        g = self._g(gnd)
        node = self.node(node)
        fc = float(corner_hz)
        if fc <= 0.0:
            raise PmuError(
                what=f"noise section {name!r} was given a non-positive corner ({fc:g} Hz).",
                why="the section is an R||C whose corner is 1/(2*pi*R*C); a non-positive corner "
                    "has no realization.",
                do=["check corner_i_hz in the noise fit for this cell"],
                where="pmukit/emit/primitives.py:noise_shaped_node")
        C = _filter_cap(2.0 * math.pi * fc)
        R = 1.0 / (2.0 * math.pi * fc * C)
        self.conductance(f"{name}.G", node, g, 1.0 / R,
                         f"{name}: Lorentzian corner {fc:.4g} Hz (R||C, C pinned at C_NOM)")
        self.capacitor(f"{name}.C", node, g, C, f"{name}: Lorentzian corner cap")
        self.white_noise(f"{name}.src", node, g, KT4_FIT / R, handle,
                         detail or f"{name}: 4kT*{T_NOISE_FIT_K:g}K FIT basis, never $temperature")
        return node, math.sqrt(KT4_FIT * R)

    def noise_flicker_node(self, name: str, node: str, handle: str, amp_1hz: float,
                           pole_hz: float, gnd: str | None = None,
                           detail: str = "") -> tuple[str, float]:
        """An R||C node whose voltage noise is `amp_1hz^2/f` in band -- the native 1/f carrier
        for a SERIES (hybrid) bank.  Returns `(node, 1.0)`: the caller's tap is unity.

        The cap is not decoration.  A bare resistor node would sit at a FIXED small admittance at
        every frequency, which is exactly the node-row underflow the conditioning lint exists to
        catch (it caught this one).  `C_NOM` with `R = 1/(2*pi*pole_hz*C_NOM)` puts the node on
        the same O(w*C_NOM) footing as every other synthesized node and, with `pole_hz` set well
        above the noise band, band-limits a 1/f source that would otherwise be flat to infinity.
        """
        g = self._g(gnd)
        node = self.node(node)
        pole = float(pole_hz)
        if pole <= 0.0:
            raise PmuError(
                what=f"1/f carrier {name!r} was given a non-positive pole ({pole:g} Hz).",
                why="the node is an R||C whose corner band-limits the source; a non-positive "
                    "corner has no realization.",
                do=["pass a pole well above the noise band, e.g. 100 x its top frequency"],
                where="pmukit/emit/primitives.py:noise_flicker_node")
        C = _filter_cap(2.0 * math.pi * pole)
        R = 1.0 / (2.0 * math.pi * pole * C)
        self.conductance(f"{name}.G", node, g, 1.0 / R,
                         f"{name}: 1/f carrier, band-limited at {pole:.4g} Hz")
        self.capacitor(f"{name}.C", node, g, C, f"{name}: 1/f carrier band-limit cap")
        self.flicker_noise(f"{name}.src", node, g, (float(amp_1hz) / R) ** 2, handle,
                           detail or f"{name}: native 1/f, {float(amp_1hz):.4g} V/rtHz at 1 Hz "
                                     f"in band")
        return node, 1.0

    # -- whitelist: ideal sources -------------------------------------------
    def ideal_vsource(self, name: str, a: str, b: str, expr: str, detail: str = "") -> None:
        """`V(a,b) <+ expr` -- the regulated reference, or a stubbed rail pin."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    V({a}, {b}) <+ {expr};" + (f"   // {detail}" if detail else ""))
        self._add("vsource", name, (a, b), float("nan"), "ideal_vsource", detail, stiff=True)
        self._pinned.add(a)

    def ideal_isource(self, name: str, a: str, b: str, expr: str, value: float = float("nan"),
                      detail: str = "") -> None:
        """`I(a,b) <+ expr` -- a stubbed bias pin at its measured DC value."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ {expr};" + (f"   // {detail}" if detail else ""))
        self._add("isource", name, (a, b), value, "ideal_isource", detail, stiff=True)

    def gated_current(self, name: str, a: str, b: str, core_expr: str, gate_expr: str,
                      detail: str = "") -> None:
        """`I(a,b) <+ (core)*(gate)` -- the bias I-V law behind its ONE-SIDED compliance gate."""
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ ({core_expr})\n"
                         f"                 * {gate_expr};"
                         + (f"   // {detail}" if detail else ""))
        self._add("isource", name, (a, b), float("nan"), "one_sided_gate", detail, stiff=True)

    def behavioral_current(self, name: str, a: str, b: str, expr: str, rule: str,
                           detail: str = "") -> None:
        """A large-signal current term from a whitelisted expression builder.

        `rule` must name a `WHITELIST` entry -- this is how the odd current assist reaches the
        body without opening a free-text door.
        """
        if rule not in WHITELIST:
            raise PmuError(
                what=f"{name!r} tried to emit a behavioral term under the unknown rule {rule!r}.",
                why="every line of the emitted model must name a whitelist entry; the whitelist "
                    "exists because each item outside it broke a real simulation.",
                do=[f"use one of: {', '.join(sorted(WHITELIST))}",
                    "or add the construct to WHITELIST with the failure it prevents"],
                where="pmukit/emit/primitives.py:behavioral_current")
        a, b = _check_node(a, name), _check_node(b, name)
        self.body.append(f"    I({a}, {b}) <+ {expr};" + (f"   // {detail}" if detail else ""))
        self._add("isource", name, (a, b), float("nan"), rule, detail, stiff=True)

    # -- whitelist: expression builders (no text reaches the body from here) --
    @staticmethod
    def one_sided_knee(v_expr: str, vk: str, p: str, side: str, vhi: str = "") -> str:
        """The compliance gate `tanh(pow(sqrt(u*u + 1e-12)/vk, p))` with a ONE-SIDED `u`.

        `side='hi'` -> `u = max(vhi - Vo, 0)`; `side='lo'` -> `u = max(Vo, 0)`; `side='none'` ->
        no gate at all.  The symmetric `|vhi - Vo|` is banned: it climbs back to 1 above the
        ceiling and the sink REOPENS to full current where the real device is starved.  The sqrt
        floor keeps the `pow()` Jacobian finite at Vo = 0 when p < 1.
        """
        if side == "none":
            return "1.0"
        if side == "hi":
            u = f"max({vhi} - {v_expr}, 0.0)"
        elif side == "lo":
            u = f"max({v_expr}, 0.0)"
        else:
            raise PmuError(
                what=f"unknown compliance knee side {side!r}.",
                why="the knee side is data-detected from the I-V shape and is one of hi / lo / "
                    "none; anything else would emit a gate nobody measured.",
                do=["use 'hi', 'lo' or 'none' (knee_side in the bias idc fit)"],
                where="pmukit/emit/primitives.py:one_sided_knee")
        return f"tanh(pow(sqrt(({u})*({u}) + 1e-12)/{vk}, {p}))"

    @staticmethod
    def bounded_reverse_emf_term(out: str, vrg: str, reg_node: str, en: str, p: dict) -> str:
        """The Route-1 unload discharge, as a SERIES term of the regulation voltage.

        Three properties are load-bearing, and each one is a refuted alternative:
          * the gate is a ONE-SIDED deadzone in the overshoot with zero value AND zero slope at
            the operating point, so Zout/PSRR/noise/DC stay bit-identical;
          * `srcblk` blocks it while branch A SINKS, so a sustained external sink never engages
            it -- this is also the rail's `no_sink` constraint;
          * the EMF is voltage-BOUNDED, so the regulation always keeps ~1/Ra conductance and the
            DC pin can never be lost, which is the difference from the reverted DC compliance.
        An OUTPUT-side clamp and any stateful/leaky-integral gate are refuted with measurements.
        """
        vov = f"V({out}, {vrg})"
        cur = f"I({reg_node}, {vrg})"
        dz, vsc = num(p["ovVdz"]), num(p["ovVsc"])
        gate = (f"({vov} > {dz} ? tanh(({vov}-{dz})*({vov}-{dz})/({vsc}*{vsc})) : 0.0)")
        srcblk = f"(0.5*(1.0 - tanh({cur}/{num(p['ovIsc'])})))"
        emf = f"{num(p['ovVmax'])}*tanh(({num(p['ovR'])}/{num(p['ovVmax'])})*{cur})"
        return f"\n        + {en}*{gate}*{srcblk}*{emf}"

    @staticmethod
    def series_emf_term(terms) -> str:
        """The hybrid series-voltage noise bank, spliced into the regulation as a series EMF.

        It is exactly 0 in DC and in transient (a noise source contributes only to .noise/pnoise),
        so Zout/PSRR/DC are untouched; it reaches the pin through T = Zout/ZA, which is what the
        hybrid fit identified.  An emitter that writes the bank but never splices it here ships a
        model with NO 1/f tail at all -- the bug that shipped once (~306x at 100 Hz).
        """
        terms = list(terms or [])
        if not terms:
            return ""
        out = ""
        for i, t in enumerate(terms):
            out += ("\n        + " if i % 4 == 0 else " + ") + t
        return out

    @staticmethod
    def odd_assist_expr(out: str, vrg: str, en: str, iaG: float, iaV: float) -> str:
        """`-iaG*tanh(verr*|verr|/iaV^2)` -- ODD with f'(0) = 0 EXACTLY, so it is invisible to
        every small-signal analysis at the operating point and engages only large-signal."""
        verr = f"V({vrg}, {out})"
        return f"-{en}*{num(iaG)}*tanh( {verr}*abs({verr}) / ({num(iaV)}*{num(iaV)}) )"

    # -- assembly ------------------------------------------------------------
    def _add(self, kind, name, nodes, value, rule, detail, stiff=False, controlled=False):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = float("nan")
        self.elements.append(Element(kind=kind, name=name, nodes=tuple(nodes), value=v,
                                     rule=rule, detail=detail, stiff=stiff,
                                     controlled=controlled))

    def declarations(self) -> str:
        """The module's declaration block: parameters, localparams, reals, internal nodes."""
        out: list[str] = []
        out += self._params
        out += self._locals
        out += self._reals
        if self._nodes:
            out.append(_wrap("  electrical", self._nodes))
        return "\n".join(out)

    def text(self) -> str:
        """The analog body.  Refuses to return text that matches any `FORBIDDEN` pattern."""
        body = "\n".join(self.body)
        for pat, why in _FORBIDDEN_RE:
            m = pat.search(body)
            if m:
                raise PmuError(
                    what=f"the emitter produced a forbidden construct: {m.group(0)!r}.",
                    why=why,
                    do=["build the line through a pmukit.emit.primitives.Netlist method "
                        "(the whitelist) instead",
                        "if the construct is genuinely needed, add it to WHITELIST with the "
                        "failure it prevents and remove it from FORBIDDEN"],
                    where="pmukit/emit/primitives.py:Netlist.text",
                )
        return body


def _wrap(keyword: str, names, width: int = 92) -> str:
    """Wrap a long declaration so no emitted line is unwieldy."""
    names = list(names)
    lines, cur = [], keyword + " "
    for i, n in enumerate(names):
        piece = n + ("," if i < len(names) - 1 else ";")
        if len(cur) + len(piece) + 1 > width:
            lines.append(cur.rstrip())
            cur = "      " + piece + " "
        else:
            cur += piece + " "
    lines.append(cur.rstrip())
    return "\n".join(lines)
