"""Contract 1 -- the fixed physics inventory.

Which blocks exist per port type, which parameters each block has, which observable every
parameter is measured from, which axes it varies over, and which tier it belongs to.  This
table does NOT vary by project: the project config supplies the axis VALUES, this module
supplies the physics.  Two things are derived from it and nothing else:

  * the measurement plan (M4 `plan.py`) -- `requirements()` is what the plan compiler
    inverts to answer "why does this run exist?";
  * the Model screen's per-block rows and its help panel (`explain()`).

Source of truth: `docs/CONTRACTS.md` section 1 (the table), `docs/REFACTOR_PLAN.md` section 6.1
("must build": who uses each quantity, why it matters, the minimum simulation per corner cell),
and the block structure of the ported modeling method (Zout ladder, PSRR real + complex
sections, noise white + 1/f + Lorentzian bank, current-bias idc/yout/noise/psrr, the
large-signal load_en tier).

DELIBERATELY NOT IN THIS SPEC (REFACTOR_PLAN 6.2) -- do not re-add without changing that
document first; `NOT_MODELED` below carries the same list with reasons:

  * rail-to-rail coupling (one rail's load moving another rail's voltage);
  * VSET switching transients (only the steady state of each VSET code is modeled);
  * PMU operating-mode sweeps (characterized in the state the user's bench is in, stamped
    into provenance instead of swept);
  * startup sign-off (the `en` tier is "usable, not signed off" -- sign startup off on the
    real LDO);
  * quiescent current, thermal shutdown, UVLO, ESD, bandgap internal nodes, digital control.

SUSPENDED, awaiting evidence (REFACTOR_PLAN 6.3): noise correlation.  Bandgap noise reaches
the rails and the biases together; this spec models the two as independent sources, which is
a known error source of roughly 3 dB on phase noise, not an oversight.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from . import jsonio
from .errors import PmuError

__all__ = [
    "PORT_TYPES", "TIERS", "AXES", "OBSERVABLES", "OBSERVABLE_DOC", "TIER_MEANING",
    "WHAT", "NOT_MODELED", "Param", "Block", "Requirement", "SPEC", "SPEC_SHA",
    "blocks_for", "block", "observables_for", "all_observables", "axes_for", "tier_of",
    "variable_name", "split_variable", "requirements", "to_dict", "explain",
]

# --- vocabularies -----------------------------------------------------------------------

PORT_TYPES = ("rail", "bias", "en")          # voltage rail / current bias / enable
TIERS = ("hb", "ls", "en")                   # default-on for RF/HB; per-item large-signal; usable-not-signed-off
AXES = ("process", "temp_c", "temp_cont", "vset", "load_a")

#: The closed observable vocabulary.  A parameter may only be measured from one of these,
#: and every one of them must have at least one consumer (checked at import).
OBSERVABLES = (
    "dc_load", "dc_temp", "ac_zout", "ac_psrr", "noise_v", "tran_load_on", "tran_load_off",
    "dc_iv", "ac_yout", "noise_i", "tran_en",
)

OBSERVABLE_DOC = {
    "dc_load": "DC sweep of the rail's load current: load regulation, dropout, current limit.",
    "dc_temp": "DC temperature sweep, the one continuous axis (rail vout drift, bias PTAT slope).",
    "ac_zout": "AC current injected at the rail pin, read at every port: output impedance.",
    "ac_psrr": "AC voltage injected at the supply, read at every port: supply to rail and to bias current.",
    "noise_v": "Noise analysis with the rail voltage as output: V^2/Hz.",
    "tran_load_on": "Transient, load stepping from its off current to its on current: the droop.",
    "tran_load_off": "Transient, load stepping back off: the overshoot.",
    "dc_iv": "DC sweep of the bias pin voltage: compliance range and I-V shape.",
    "ac_yout": "AC current injected at the bias pin: output admittance.",
    "noise_i": "Noise analysis with the bias current as output: A^2/Hz.",
    "tran_en": "Transient of the EN edge: how the rails and biases come up.",
}

TIER_MEANING = {
    "hb": ("on by default; it ships in the RF/HB deliverable"),
    "ls": ("a large-signal item with its own switch; it may only default on after it passes "
           "the HB first-step residual check"),
    "en": ("usable, not signed off: it only guarantees that a consumer bench toggling EN does "
           "not blow up"),
}

#: Deliberately out of scope (REFACTOR_PLAN 6.2).  `explain()` prints this when asked about
#: something that is not a block, so nobody re-adds an item by accident.
NOT_MODELED: tuple[tuple[str, str], ...] = (
    ("rail-to-rail coupling",
     "one rail's load moving another rail's voltage is not characterized; every rail is "
     "modeled as an independent two-port."),
    ("VSET switching transients",
     "only the steady state of each VSET code is modeled; changing the code at run time is "
     "outside the envelope."),
    ("PMU operating modes",
     "the PMU is characterized in whatever state the user's bench puts it in; that state is "
     "stamped into provenance instead of being swept."),
    ("startup sign-off",
     "the EN ramp is the `en` tier only -- sign startup off on the real LDO, never on the model."),
    ("quiescent current",
     "a housekeeping number, not a path the consumer's bench sees; it does not enter any block."),
    ("thermal shutdown",
     "a protection event outside the characterized temperature range; the model has no state "
     "to latch into."),
    ("UVLO",
     "a protection threshold on the supply; the model is only valid inside its declared supply "
     "range anyway."),
    ("ESD",
     "a device-level structure with no small-signal or large-signal role in the consumer's "
     "simulation."),
    ("bandgap internal nodes",
     "internal to the PMU; the consumer sees only rails and bias currents."),
    ("digital control logic",
     "register decode and control are not simulated; the characterized state is recorded in "
     "`state_note` instead."),
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --- table types ------------------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    """One fitted number (or one indexed family of them) inside a block."""

    name: str
    """e.g. "Ra", "Cout", "esr", "pole_i_hz", "white".  A trailing `_i` marks a bank whose
    section count the fitter chooses."""
    observable: str
    """Which measurement it is extracted from; one of `OBSERVABLES`."""
    axes: tuple[str, ...]
    """Which axes this parameter varies over; a subset of `AXES`.  Empty means it is one
    value for the port (a topology choice, not a fitted number)."""
    note: str = ""
    """One line, plain language, shown in the UI."""


@dataclass(frozen=True)
class Block:
    """One swappable physics block of one port type."""

    name: str
    port_type: str
    tier: str
    params: tuple[Param, ...] = ()
    observables: tuple[str, ...] = ()
    """De-duplicated, in declaration order.  DERIVED from `params` -- never passed in, so it
    cannot drift away from the parameter list."""
    why: str = ""
    """One sentence: who uses this block and what breaks without it (REFACTOR_PLAN 6.1)."""
    priority: int = 0
    """1 = highest; the ordering of REFACTOR_PLAN 6.1 "priority"."""

    def __post_init__(self) -> None:
        seen: list[str] = []
        for p in self.params:
            if p.observable not in seen:
                seen.append(p.observable)
        object.__setattr__(self, "observables", tuple(seen))

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.port_type)


class Requirement(NamedTuple):
    """One observable a port type needs, with the axes it must be swept over and the
    parameters that consume it.  The plan compiler inverts this to explain every run."""

    observable: str
    axes: tuple[str, ...]
    consumers: tuple[tuple[str, str], ...]   # (block, param)


#: What each block IS, in one line (the "who uses it" half lives in `Block.why`).
WHAT = {
    ("rail", "dc"): "the rail's DC behaviour: the vout table over load, temperature and VSET, "
                    "plus dropout and current limit.",
    ("rail", "zout"): "the rail's output impedance Zout(f): an RLC ladder hanging off the "
                      "active regulation branch.",
    ("rail", "psrr"): "the supply-to-rail transfer, identified as a coupling current i_c(s) "
                      "that the same Zout carries to the pin.",
    ("rail", "noise"): "the rail's output voltage noise: a white floor, a 1/f term and a "
                       "Lorentzian bank.",
    ("rail", "load_en"): "the two large-signal load events: the droop when the consumer's "
                         "load turns on and the overshoot when it turns off.",
    ("rail", "no_sink"): "the one-way-conduction constraint: a real LDO sources but does not "
                         "sink.",
    ("bias", "idc"): "the bias reference's DC current over pin voltage and temperature, "
                     "including its compliance limit.",
    ("bias", "yout"): "the bias pin's small-signal output admittance.",
    ("bias", "noise"): "the bias reference's output current noise.",
    ("bias", "psrr"): "the supply-to-bias-current transfer.",
    ("en", "ramp"): "how far and how fast every modeled rail and bias moves after the EN edge.",
}


# --- the table --------------------------------------------------------------------------

_RAIL_AC = ("process", "temp_c", "vset", "load_a")
_RAIL_DC = ("process", "temp_cont", "vset", "load_a")
_PT = ("process", "temp_c")

SPEC: tuple[Block, ...] = (

    # ---------------------------------------------------------------- voltage rail
    Block(
        name="dc", port_type="rail", tier="hb", priority=2,
        why="Everyone downstream, and the KVCO supply-pushing term: 10 mV of rail error moves "
            "the VCO frequency, and this is also the operating point every small-signal block "
            "is fitted at.",
        params=(
            Param("vout", "dc_load", _RAIL_DC,
                  "Regulated output voltage on the load grid; also the operating point the "
                  "Zout, PSRR and noise blocks are fitted at."),
            Param("vout_tc", "dc_temp", ("process", "vset", "load_a"),
                  "Continuous dVout/dT from the DC temperature sweep, so one corner section "
                  "covers the whole temperature range without interpolating between corners."),
            Param("dropout", "dc_load", ("process", "temp_cont", "vset"),
                  "Supply-to-output headroom below which regulation is lost; the low-supply "
                  "edge of the validity envelope."),
            Param("ilimit", "dc_load", ("process", "temp_cont", "vset"),
                  "Current limit. The hot corner can be several times lower than tt, so it is "
                  "a per-corner measurement, never a datasheet constant."),
        ),
    ),
    Block(
        name="zout", port_type="rail", tier="hb", priority=4,
        why="The consumer's own current ripple: a VCO or a buffer draws current at f0 and its "
            "harmonics, and that current times Zout is the rail ripple that becomes a spur.",
        params=(
            Param("Ra", "ac_zout", _RAIL_AC,
                  "Series resistance of the active (regulated) branch A. It stays a stiff "
                  "resistor, never a current clamp: 1/Ra holds the DC pin everywhere."),
            Param("La", "ac_zout", _RAIL_AC,
                  "Branch-A inductance: the loop's inductive rise above its unity-gain "
                  "bandwidth."),
            Param("Rpl", "ac_zout", _RAIL_AC,
                  "Damping resistor across La. Finite Rpl gives a resistive plateau, Rpl to "
                  "infinity gives the resonant peak; one degree of freedom covers both."),
            Param("La_i", "ac_zout", _RAIL_AC,
                  "Extra series (L||R) ladder stages i = 2..N inside branch A, for a "
                  "rising-then-plateau shelf. A stage is adopted only when it cuts the |Z| "
                  "dB-RMS by the keep-best margin."),
            Param("Rpl_i", "ac_zout", _RAIL_AC,
                  "Damping resistor of ladder stage i."),
            Param("Lb", "ac_zout", _RAIL_AC,
                  "Optional second parallel R-L branch, engaged only when it clearly beats the "
                  "single-branch fit."),
            Param("Rb", "ac_zout", _RAIL_AC,
                  "Series resistance of that second branch."),
            Param("Cout", "ac_zout", _RAIL_AC,
                  "Output capacitance, extracted from the capacitive band where the phase of Z "
                  "is below -45 degrees."),
            Param("esr", "ac_zout", _RAIL_AC,
                  "Series resistance of Cout. On a high-ESR or capless rail the capacitor is "
                  "nearly invisible; that residual is a documented floor, not a fit bug."),
        ),
    ),
    Block(
        name="psrr", port_type="rail", tier="hb", priority=4,
        why="Spurs: another block's supply ripple arrives on this rail through PSRR and lands "
            "as a sideband on the consumer's carrier.",
        params=(
            Param("G0", "ac_psrr", _RAIL_AC,
                  "Pole-less DC term of the supply coupling current. The block identifies only "
                  "i_c in PSRR = i_c(s)*Zout(s), so a Zout error is common-mode across Zout, "
                  "PSRR and noise."),
            Param("G_i", "ac_psrr", _RAIL_AC,
                  "Signed gain of real-pole section i; the fitter picks the section count."),
            Param("pole_i_hz", "ac_psrr", _RAIL_AC,
                  "Pole frequency of real-pole section i."),
            Param("pc_gain", "ac_psrr", _RAIL_AC,
                  "Numerator DC term of the one signed complex-conjugate 2nd-order section. It "
                  "must be realized as a small-coefficient gm-C biquad, never as a synthesized "
                  "R-L-C and never as a pair of large near-cancelling first-order sections, "
                  "which makes a coupled HB Jacobian singular."),
            Param("pc_zero", "ac_psrr", _RAIL_AC,
                  "Numerator s term of that section; it is what lets the section be "
                  "non-minimum-phase. Zero on a minimum-phase rail, which leaves the section "
                  "inert."),
            Param("pc_w0", "ac_psrr", _RAIL_AC,
                  "Resonant frequency of the complex section."),
            Param("pc_q", "ac_psrr", _RAIL_AC,
                  "Q of the complex section."),
            Param("c_ft", "ac_psrr", _RAIL_AC,
                  "Optional feedthrough capacitance across the pass device (its Cgd plus "
                  "package), an added s*C term at the top of the band. Keep-best gated."),
        ),
    ),
    Block(
        name="noise", port_type="rail", tier="hb", priority=3,
        why="VCO and PLL phase noise and receiver sensitivity: supply pushing turns rail noise "
            "into phase noise, and the 1/f part dominates close to the carrier.",
        params=(
            Param("nmode", "noise_v", (),
                  "Realization: `norton` (a current source at vout, In = Sv/|Zout|) or "
                  "`hybrid` (a series voltage bank in branch A plus the white Norton term). "
                  "A loop-shaped rail needs hybrid; one choice per rail, not per corner."),
            Param("white", "noise_v", ("process", "temp_c", "load_a"),
                  "White floor of the output-referred noise."),
            Param("flicker", "noise_v", ("process", "temp_c", "load_a"),
                  "1/f term. It dominates near the carrier, so an emitter that silently drops "
                  "it loses the number the phase-noise user came for."),
            Param("corner_i_hz", "noise_v", ("process", "temp_c"),
                  "Corner frequency of Lorentzian section i; the corners are shared across the "
                  "load points and only the amplitudes move."),
            Param("amp_i", "noise_v", ("process", "temp_c", "load_a"),
                  "Amplitude of Lorentzian section i, fitted in the log domain."),
        ),
    ),
    Block(
        name="load_en", port_type="rail", tier="ls", priority=5,
        why="System timing, VCO pulling and the HB initial guess: the droop when the consumer's "
            "load switches on and the overshoot when it switches off are the largest signal the "
            "rail ever sees.",
        params=(
            Param("iaG", "tran_load_on", _PT,
                  "Saturating current the class-AB assist adds on a load step: "
                  "i_assist = iaG*tanh(verr*|verr|/iaV^2). The function is odd with zero slope "
                  "at the operating point, so it is invisible to Zout, PSRR and noise."),
            Param("iaV", "tran_load_on", _PT,
                  "Error voltage that sets the knee of that assist."),
            Param("ovVdz", "tran_load_off", _PT,
                  "Deadzone above the regulated level before the unload discharge engages. It "
                  "is a transparency floor, not a margin: set below the largest legitimate "
                  "periodic ripple it clips the very spectra the model exists to produce."),
            Param("ovR", "tran_load_off", _PT,
                  "Slope of the reverse EMF that drains the fit inductor after an unload."),
            Param("ovVmax", "tran_load_off", _PT,
                  "Clamp on that reverse EMF, so the regulation keeps ~1/Ra conductance and the "
                  "DC pin can never be lost."),
            Param("ovVsc", "tran_load_off", _PT,
                  "Softness of the one-sided deadzone gate: zero value AND zero slope at the "
                  "operating point."),
            Param("ovIsc", "tran_load_off", _PT,
                  "Scale of the source/sink discriminator, so a sustained external sink never "
                  "engages the discharge."),
        ),
    ),
    Block(
        name="no_sink", port_type="rail", tier="hb", priority=5,
        why="HB convergence and the unload overshoot: a real LDO cannot sink, so an unloaded "
            "rail can only come down through its load.",
        # No params and no observables on purpose: this is an emitter constant, so the plan
        # compiler must never schedule a run for it.
    ),

    # ---------------------------------------------------------------- current bias
    Block(
        name="idc", port_type="bias", tier="hb", priority=1,
        why="KVCO, the VCO frequency and its temperature drift: the VCO is current-biased, so "
            "the PTAT slope goes straight into the drift.",
        params=(
            Param("idc", "dc_iv", ("process", "temp_cont", "vset"),
                  "Output current at the pin's operating voltage."),
            Param("pol", "dc_iv", ("process",),
                  "`source` or `sink`, detected from the sign of the probe current and never "
                  "assumed: the wrong sign makes the model draw the current the real reference "
                  "injects."),
            Param("knee_side", "dc_iv", ("process",),
                  "Which side the compliance knee is on -- `hi`, `lo` or `none` -- detected "
                  "from the I-V shape, with `none` kept as a candidate so a flat reference gets "
                  "no gate at all."),
            Param("vhi", "dc_iv", ("process", "temp_cont", "vset"),
                  "Compliance ceiling: above it the reference starves. The knee must be "
                  "one-sided; a symmetric gate reopens the sink above the ceiling."),
            Param("vknee", "dc_iv", ("process", "temp_cont", "vset"),
                  "Width of the compliance knee."),
            Param("knee_p", "dc_iv", ("process", "temp_cont", "vset"),
                  "Sharpness exponent of the knee."),
            Param("ptat_slope", "dc_temp", ("process", "vset"),
                  "Continuous dI/dT from the temperature sweep. This is the one place a "
                  "continuous temperature dependence is allowed, because it is the PTAT physics "
                  "the VCO's drift rides on."),
        ),
    ),
    Block(
        name="yout", port_type="bias", tier="hb", priority=4,
        why="The consumer's bias node: the output conductance and capacitance set how much the "
            "bias current moves when that node moves.",
        params=(
            Param("g0", "ac_yout", _PT,
                  "DC output conductance, taken from the real part of the AC admittance rather "
                  "than the full-sweep I-V chord, which crosses the turn-off knee and comes out "
                  "far too steep."),
            Param("Cp", "ac_yout", _PT,
                  "Output capacitance at the bias pin."),
            Param("wz", "ac_yout", _PT,
                  "Optional zero of a cascode or Wilson mirror; adopted only when it beats the "
                  "plain g0 + s*Cp form."),
            Param("wp", "ac_yout", _PT,
                  "The pole that pairs with wz."),
        ),
    ),
    Block(
        name="noise", port_type="bias", tier="hb", priority=1,
        why="VCO phase noise: bias noise upconverts into phase noise and is often more dominant "
            "than the rail noise.",
        params=(
            Param("white", "noise_i", _PT,
                  "White floor of the output current noise."),
            Param("flicker", "noise_i", _PT,
                  "1/f term of the output current noise; near-carrier phase noise lives here. "
                  "Bandgap noise reaches the rails and the biases together, but the model treats "
                  "them as independent sources -- a known ~3 dB error source, not an oversight."),
        ),
    ),
    Block(
        name="psrr", port_type="bias", tier="hb", priority=4,
        why="VCO spurs and AM/FM: supply ripple modulates the bias current through the mirror.",
        params=(
            Param("gdd", "ac_psrr", _PT,
                  "Signed supply-to-output-current transconductance in A/V. It comes from THE "
                  "SAME supply injection as the rail PSRR block -- one AC run injects at the "
                  "supply and reads every rail and every bias pin -- which is what lets the plan "
                  "compiler merge the two into one run. Never collapsed to a magnitude: the sign "
                  "and phase matter when several sinks share a reference and their ripple "
                  "currents add."),
            Param("psrr_pole_hz", "ac_psrr", _PT,
                  "Pole that rolls the supply-to-current transfer off."),
            Param("c_ft", "ac_psrr", _PT,
                  "Supply-to-output feedthrough capacitance. The transfer does not only roll "
                  "off: above the pole a real mirror's supply coupling RISES as j*w*c_ft "
                  "through device overlap capacitance. Measured on the synthetic PMU: the PTAT "
                  "reference's transfer is flat at 357 nS to ~10 kHz and then climbs 500x to "
                  "173 uS at 1 GHz, which is 28 fF. Without this term the block is a falling "
                  "form fitted to a rising curve, and it is exactly the band that makes VCO "
                  "spurs."),
        ),
    ),

    # ---------------------------------------------------------------- enable
    Block(
        name="ramp", port_type="en", tier="en", priority=6,
        why="A consumer bench that toggles EN: the rails and the biases must come up with the "
            "measured delay and rise time so that bench does not blow up.",
        params=(
            Param("t_delay", "tran_en", _PT,
                  "Delay from the EN edge until the rail starts to move."),
            Param("t_rise", "tran_en", _PT,
                  "Rise time of each modeled rail, measured on the EN transient."),
            Param("v_overshoot", "tran_en", _PT,
                  "Overshoot of each modeled rail at the end of its ramp."),
            Param("i_rise", "tran_en", _PT,
                  "Rise time of each modeled bias current."),
            Param("i_overshoot", "tran_en", _PT,
                  "Overshoot of each modeled bias current."),
        ),
    ),
)


# --- self-check (the table is code; a typo must not reach the plan compiler) --------------


def _bug(what: str, why: str) -> PmuError:
    return PmuError(
        what=what, why=why,
        do=["Fix the SPEC table in pmukit/spec.py.",
            "Run `python -m pytest tests/test_spec.py -q` to confirm the table is consistent."],
        where="pmukit/spec.py",
    )


def _validate() -> None:
    seen: set[tuple[str, str]] = set()
    used: set[str] = set()
    for b in SPEC:
        if b.port_type not in PORT_TYPES:
            raise _bug(f"Block {b.name!r} has an unknown port type {b.port_type!r}.",
                       f"Port types are fixed to {', '.join(PORT_TYPES)}.")
        if b.tier not in TIERS:
            raise _bug(f"Block {b.name!r} has an unknown tier {b.tier!r}.",
                       f"Tiers are fixed to {', '.join(TIERS)}.")
        if b.key in seen:
            raise _bug(f"Block {b.name!r} is declared twice for port type {b.port_type!r}.",
                       "block(name, port_type) must address exactly one row.")
        seen.add(b.key)
        if not b.why:
            raise _bug(f"Block {b.name!r} ({b.port_type}) has no `why`.",
                       "Every block states who uses it and what breaks without it.")
        if b.priority < 1:
            raise _bug(f"Block {b.name!r} ({b.port_type}) has priority {b.priority}.",
                       "Priority 1 is the highest; 0 means it was never filled in.")
        if (b.port_type, b.name) not in WHAT:
            raise _bug(f"Block {b.name!r} ({b.port_type}) has no entry in WHAT.",
                       "explain() needs a one-line description of what the block is.")
        pnames: set[str] = set()
        for p in b.params:
            if p.observable not in OBSERVABLES:
                raise _bug(f"Parameter {b.name}.{p.name} reads unknown observable "
                           f"{p.observable!r}.",
                           "Observables are a closed vocabulary (contract 2 variable names).")
            bad = [a for a in p.axes if a not in AXES]
            if bad:
                raise _bug(f"Parameter {b.name}.{p.name} declares unknown axes {bad}.",
                           f"Axes are fixed to {', '.join(AXES)}.")
            if len(set(p.axes)) != len(p.axes):
                raise _bug(f"Parameter {b.name}.{p.name} repeats an axis.",
                           "The axis tuple is a set, written in AXES order.")
            if p.name in pnames:
                raise _bug(f"Parameter {p.name!r} is declared twice in block {b.name!r}.",
                           "Parameter names are the UI's row keys and must be unique per block.")
            pnames.add(p.name)
            used.add(p.observable)
    dead = [o for o in OBSERVABLES if o not in used]
    if dead:
        raise _bug(f"Observables {dead} are declared but nothing consumes them.",
                   "An observable with no consumer would make the plan compiler schedule a run "
                   "no parameter needs.")
    missing = [o for o in OBSERVABLES if o not in OBSERVABLE_DOC]
    if missing:
        raise _bug(f"Observables {missing} have no line in OBSERVABLE_DOC.",
                   "The help panel prints one plain sentence per observable.")


_validate()


# --- lookups ----------------------------------------------------------------------------


def _check_port_type(port_type: str) -> None:
    if port_type not in PORT_TYPES:
        raise PmuError(
            what=f"Unknown port type {port_type!r}.",
            why=f"The model spec only knows {', '.join(repr(p) for p in PORT_TYPES)} -- "
                "a rail is a voltage output, a bias is a current output, en is the enable pin.",
            do=[f"Use one of: {', '.join(PORT_TYPES)}.",
                "If a pin has no role yet, assign one on the New screen before planning."],
            where="pmukit/spec.py:PORT_TYPES",
        )


def blocks_for(port_type: str) -> tuple[Block, ...]:
    """Every block of one port type, in declaration order."""
    _check_port_type(port_type)
    return tuple(b for b in SPEC if b.port_type == port_type)


def block(name: str, port_type: str) -> Block:
    """One block, addressed by (name, port type) -- `noise` and `psrr` exist for both."""
    for b in blocks_for(port_type):
        if b.name == name:
            return b
    known = ", ".join(b.name for b in blocks_for(port_type))
    raise PmuError(
        what=f"No block named {name!r} for port type {port_type!r}.",
        why="The model spec is a fixed physics inventory; it does not grow per project.",
        do=[f"Use one of the {port_type} blocks: {known}.",
            "Run `pmukit help` (or explain()) to see what each block covers."],
        where="pmukit/spec.py:SPEC",
    )


def observables_for(port_type: str) -> tuple[str, ...]:
    """Every observable one port type needs, de-duplicated, in declaration order."""
    out: list[str] = []
    for b in blocks_for(port_type):
        for o in b.observables:
            if o not in out:
                out.append(o)
    return tuple(out)


def all_observables() -> tuple[str, ...]:
    """Every observable the whole spec consumes, in OBSERVABLES order."""
    used = {p.observable for b in SPEC for p in b.params}
    return tuple(o for o in OBSERVABLES if o in used)


def _check_observable(observable: str) -> None:
    if observable not in all_observables():
        raise PmuError(
            what=f"Unknown observable {observable!r}.",
            why="Observables are a closed vocabulary: the dataset variable name is "
                "`<observable>.<port>`, so an unknown one would write a file nothing reads.",
            do=[f"Use one of: {', '.join(all_observables())}."],
            where="pmukit/spec.py:OBSERVABLES",
        )


def axes_for(observable: str) -> tuple[str, ...]:
    """The union, in AXES order, of the axes every parameter that reads this observable needs.

    This is the sweep the measurement plan must cover for that observable.
    """
    _check_observable(observable)
    need = {a for b in SPEC for p in b.params if p.observable == observable for a in p.axes}
    return tuple(a for a in AXES if a in need)


def tier_of(block_name: str, port_type: str) -> str:
    return block(block_name, port_type).tier


# --- contract-2 variable names ------------------------------------------------------------


def variable_name(observable: str, port: str) -> str:
    """The dataset variable name of contract 2: `<observable>.<port>`."""
    _check_observable(observable)
    if not isinstance(port, str) or not _IDENT.fullmatch(port):
        raise PmuError(
            what=f"Port name {port!r} is not a plain identifier.",
            why="The dataset variable name is `<observable>.<port>` and it is also a file name, "
                "so a dot, a space or a leading digit would make it ambiguous or unwritable.",
            do=["Use letters, digits and underscores, starting with a letter or underscore "
                "(for example VDD0P8_A or IB_PTAT).",
                "Rename the pin in the netlist if the real name cannot be used directly."],
            where="pmukit/spec.py:variable_name",
        )
    return f"{observable}.{port}"


def split_variable(name: str) -> tuple[str, str]:
    """Inverse of `variable_name`; raises on anything it did not produce."""
    observable, sep, port = str(name).partition(".")
    if not sep:
        raise PmuError(
            what=f"Variable name {name!r} has no `.` separator.",
            why="Contract 2 names every dataset variable `<observable>.<port>`.",
            do=["Pass a name produced by variable_name(), e.g. 'ac_zout.VDD0P8_A'."],
            where="pmukit/spec.py:split_variable",
        )
    _check_observable(observable)
    if not _IDENT.fullmatch(port):
        raise PmuError(
            what=f"Variable name {name!r} does not carry a plain port identifier.",
            why=f"Everything after the first `.` must be one identifier; got {port!r}.",
            do=["Pass a name produced by variable_name(), e.g. 'noise_v.VDD0P8_A'."],
            where="pmukit/spec.py:split_variable",
        )
    return (observable, port)


# --- what the plan compiler asks for ------------------------------------------------------


def requirements(port_type: str,
                 tier_filter: tuple[str, ...] = ("hb", "ls", "en")) -> tuple[Requirement, ...]:
    """Every observable this port type needs, with its axes and the parameters that consume it.

    Ordered by first appearance in `blocks_for(port_type)`.  Blocks whose tier is filtered out
    contribute nothing, and a block with no observable (`no_sink`) never produces a run.

    The axes are the union over the consumers LISTED HERE, not the global `axes_for()` union:
    a bias-only project must not get a load sweep on `ac_psrr` just because rails would need
    one.  When the plan compiler merges the rail and the bias `ac_psrr` into the one supply
    injection, it unions the two requirements itself.
    """
    _check_port_type(port_type)
    for t in tier_filter:
        if t not in TIERS:
            raise PmuError(
                what=f"Unknown tier {t!r} in the tier filter.",
                why=f"Tiers are fixed to {', '.join(TIERS)}: hb ships by default, ls is the "
                    "switchable large-signal set, en is usable but not signed off.",
                do=[f"Use a subset of: {', '.join(TIERS)}."],
                where="pmukit/spec.py:TIERS",
            )
    order: list[str] = []
    consumers: dict[str, list[tuple[str, str]]] = {}
    need: dict[str, set[str]] = {}
    for b in blocks_for(port_type):
        if b.tier not in tier_filter:
            continue
        for p in b.params:
            if p.observable not in consumers:
                consumers[p.observable] = []
                need[p.observable] = set()
                order.append(p.observable)
            consumers[p.observable].append((b.name, p.name))
            need[p.observable].update(p.axes)
    return tuple(
        Requirement(o, tuple(a for a in AXES if a in need[o]), tuple(consumers[o]))
        for o in order
    )


# --- provenance ---------------------------------------------------------------------------


def to_dict() -> dict:
    """The whole table as plain JSON-able data; `SPEC_SHA` is its canonical hash."""
    return {
        "spec_version": 1,
        "port_types": list(PORT_TYPES),
        "tiers": list(TIERS),
        "tier_meaning": dict(TIER_MEANING),
        "axes": list(AXES),
        "observables": {o: OBSERVABLE_DOC[o] for o in OBSERVABLES},
        "not_modeled": [[item, reason] for item, reason in NOT_MODELED],
        "blocks": [
            {
                "name": b.name,
                "port_type": b.port_type,
                "tier": b.tier,
                "priority": b.priority,
                "what": WHAT[(b.port_type, b.name)],
                "why": b.why,
                "observables": list(b.observables),
                "params": [
                    {"name": p.name, "observable": p.observable,
                     "axes": list(p.axes), "note": p.note}
                    for p in b.params
                ],
            }
            for b in SPEC
        ],
    }


#: 12 hex chars pinning this exact table (prose included).  A deliverable records it so the
#: spec version that produced it can always be identified.
SPEC_SHA = jsonio.sha(to_dict(), 12)


# --- help ---------------------------------------------------------------------------------


def _not_modeled_text() -> str:
    lines = ["Deliberately NOT modeled (REFACTOR_PLAN 6.2):"]
    lines += [f"  {item} -- {reason}" for item, reason in NOT_MODELED]
    return "\n".join(lines)


def explain(block_name: str, port_type: str) -> str:
    """The plain-language help paragraph for one block.

    An unknown name is not an error here -- this is the help path -- so it answers with the
    blocks that do exist and with the list of things the spec deliberately leaves out.
    """
    _check_port_type(port_type)
    match = [b for b in blocks_for(port_type) if b.name == block_name]
    if not match:
        known = ", ".join(b.name for b in blocks_for(port_type))
        head = (f"There is no {port_type} block called {block_name!r}. "
                f"The {port_type} blocks are: {known}.")
        hit = [(i, r) for i, r in NOT_MODELED if block_name.lower() in i.lower()]
        if hit:
            head += (f"\n\n{hit[0][0]} is not modeled on purpose: {hit[0][1]}")
        return f"{head}\n\n{_not_modeled_text()}"

    b = match[0]
    axes = tuple(a for a in AXES if any(a in p.axes for p in b.params))
    lines = [
        f"{b.name} ({b.port_type}) -- tier {b.tier}, priority {b.priority}.",
        f"What it is: {WHAT[(b.port_type, b.name)]}",
        f"Who needs it: {b.why}",
    ]
    if b.observables:
        lines.append("Measured from: " + "; ".join(
            f"{o} ({OBSERVABLE_DOC[o]})" for o in b.observables))
    else:
        lines.append("Measured from: nothing -- it is an emitter constant, so the measurement "
                     "plan never schedules a run for it.")
    lines.append("Varies over: " + (", ".join(axes) if axes else "no axis"))
    lines.append(f"Tier {b.tier}: {TIER_MEANING[b.tier]}.")
    if b.params:
        lines.append("Parameters:")
        width = max(len(p.name) for p in b.params)
        lines += [f"  {p.name:<{width}}  {p.note}" for p in b.params]
    else:
        lines.append("Parameters: none.")
    return "\n".join(lines)
