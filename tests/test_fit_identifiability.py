"""The identifiability gate itself, on transfers whose answer is known by construction."""
import numpy as np

from pmukit.fit import identifiability as ident


def test_a_dead_knob_is_named_and_the_live_ones_are_not():
    """`g` ignores its third parameter entirely, so the relative column norm of that column is
    zero and the gate must say the data cannot determine it."""
    f = np.logspace(1, 8, 141)

    def g(p):
        return p[0] + 1j * 2 * np.pi * f * p[1] + 0.0 * p[2]

    res = ident.gate(g, ["g0", "Cp", "dead"], [2e-9, 1.5e-14, 1.0])
    assert res["unidentifiable"] == ["dead"]
    assert res["rank_deficient"] is True
    assert not np.isfinite(res["cond"]) or res["cond"] > 1e12
    assert res["sigma"]["g0"] < 10.0 and res["sigma"]["Cp"] < 10.0


def test_a_well_posed_block_has_no_flags():
    f = np.logspace(1, 8, 141)

    def g(p):
        return p[0] + 1j * 2 * np.pi * f * p[1]

    res = ident.gate(g, ["g0", "Cp"], [2e-9, 1.5e-14])
    assert res["unidentifiable"] == []
    assert res["poorly_determined"] == []
    assert np.isfinite(res["cond"])


def test_a_parameter_sitting_at_zero_reads_as_unidentifiable():
    """A RELATIVE perturbation of a zero-valued coefficient stays zero, which is exactly what an
    inert section should look like.  An ABSOLUTE step would instead inject a huge fake column
    and present a switched-off section as a measurement."""
    f = np.logspace(1, 8, 141)

    def g(p):
        s = 1j * 2 * np.pi * f
        return p[0] + p[1] * s / (1 + s / 1e6)

    res = ident.gate(g, ["G0", "b1"], [1e-3, 0.0])
    assert "b1" in res["unidentifiable"]


def test_describe_reports_and_never_raises():
    lines = ident.describe({"unidentifiable": ["Rpl"], "poorly_determined": ["Cout"]})
    assert any("does not determine" in ln for ln in lines)
    assert any("pinned far more loosely" in ln for ln in lines)
    assert ident.describe({}) == []


def test_a_weakly_seen_parameter_is_flagged_as_poorly_determined():
    """The softer half of the same question: a parameter the data DOES see, but 1000x more
    weakly than its neighbours -- the near-invisible output capacitor of a high-ESR rail."""
    f = np.logspace(1, 8, 141)

    def g(p):
        # `faint` moves the response by about 0.25 % -- visible, so not "unidentifiable", but
        # pinned far more loosely than its neighbours
        return p[0] + 1j * 2 * np.pi * f * p[1] + 5e-12 * p[2] * np.ones_like(f)

    res = ident.gate(g, ["g0", "Cp", "faint"], [2e-9, 1.5e-14, 1.0])
    assert res["unidentifiable"] == []
    assert res["poorly_determined"] == ["faint"], res["colnorm_rel"]
    assert res["sigma"]["faint"] > 20.0 * res["sigma"]["g0"]
