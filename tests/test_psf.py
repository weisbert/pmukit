"""The PSF readers: ascii, binary, and the axis every caller used to guess.

Everything here is synthetic.  The binary fixtures are BUILT in the test from the byte grammar in
`pmukit/binpsf.py`, so no captured customer PSF is needed and the test runs anywhere.

The properties that matter are the scars:
  * the dc-sweep axis is named `dc` and the transient axis `time` -- `sweep_axis()` reports the
    file's own name instead of every caller inventing one;
  * an operating-point file has a THIRD token per line and no SWEEP section, and must not be read
    as a sweep whose first signal is the axis;
  * a noise trace's declared TYPE decides real vs complex, not the number of values in the
    parentheses -- a 2-member resistor STRUCT is not a complex number;
  * grouped PSF (`PSF groups` >= 1) is not an error wall, and struct traces are dropped by name
    rather than left ragged;
  * a windowed transient PSF is signal-major with a NaN-padded last window.
"""
import pathlib
import struct

import numpy as np
import pytest

from pmukit import binpsf, psf
from pmukit.errors import PmuError

# --------------------------------------------------------------------------- ascii fixtures

AC = """\
HEADER
"PSFversion" "1.00"
"simulator" "spectre"
"analysis type" "ac"
TYPE
"sweep" FLOAT DOUBLE PROP(
"key" "sweep"
)
"V" COMPLEX DOUBLE PROP(
"units" "V"
"key" "node"
)
SWEEP
"freq" "sweep" PROP(
"sweep_direction" 0
"units" "Hz"
)
TRACE
"VDD0P8_A" "V"
"VDD0P8_B" "V"
VALUE
"freq" 1.000000000000000e+01
"VDD0P8_A" (-6.41661 -0.000199722)
"VDD0P8_B" (1.23388e-13 -6.59057e-09)
"freq" 1.000000000000000e+02
"VDD0P8_A" (-6.41662 -0.00199722)
"VDD0P8_B" (1.23388e-12 -6.59057e-08)
END
"""

# A DC sweep: the axis is literally named `dc`, whatever device was swept.
DC = """\
HEADER
"PSFversion" "1.00"
"analysis type" "dc"
TYPE
"sweep" FLOAT DOUBLE PROP(
"key" "sweep"
)
"V" FLOAT DOUBLE PROP(
"units" "V"
)
SWEEP
"dc" "sweep" PROP(
"units" "A"
)
TRACE
"VDD0P8_A" "V"
VALUE
"dc" 0.000000000000000e+00
"VDD0P8_A" 8.000000000000000e-01
"dc" 5.000000000000000e-04
"VDD0P8_A" 7.750000000000000e-01
END
"""

# An operating point: three tokens per line, and NO SWEEP section.
OP = """\
HEADER
"PSFversion" "1.00"
"analysis type" "dc"
TYPE
"V" FLOAT DOUBLE PROP(
"units" "V"
)
TRACE
VALUE
"VB_IB_PTAT:p" "I" 1.040000000000000e-05
"VDD0P8_A" "V" 8.000000000000000e-01
END
"""

# Noise: the total `out` is a scalar, every per-instance trace is a STRUCT.  The resistor structs
# have exactly TWO members, which arity alone would read as a complex number.
NOISE = """\
HEADER
"PSFversion" "1.00"
"analysis type" "noise"
TYPE
"sweep" FLOAT DOUBLE PROP(
"key" "sweep"
)
"V/sqrt(Hz)" FLOAT DOUBLE PROP(
"units" "V/sqrt(Hz)"
"key" "noise"
)
"rsil" STRUCT(
"rn" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
"total" FLOAT DOUBLE PROP(
"units" "V^2/Hz"
)
) PROP(
"key" "inst"
)
SWEEP
"freq" "sweep" PROP(
"units" "Hz"
)
TRACE
"PMU_TOP.R1a" "rsil"
"out" "V/sqrt(Hz)"
VALUE
"freq" 1.000000000000000e+01
"PMU_TOP.R1a" (
1.000000000000000e-16
2.000000000000000e-16
)
"out" 7.703342364720425e-06
"freq" 1.000000000000000e+02
"PMU_TOP.R1a" (
1.100000000000000e-16
2.100000000000000e-16
)
"out" 2.436000000000000e-06
END
"""

TRAN = """\
HEADER
"PSFversion" "1.00"
"analysis type" "tran"
TYPE
"sweep" FLOAT DOUBLE PROP(
"key" "sweep"
)
"V" FLOAT DOUBLE PROP(
"units" "V"
)
SWEEP
"time" "sweep" PROP(
"units" "s"
)
TRACE
"VDD0P8_A" "V"
VALUE
"time" 0.000000000000000e+00
"VDD0P8_A" 8.000000000000000e-01
"time" 1.000000000000000e-09
"VDD0P8_A" 7.900000000000000e-01
END
"""


def write(tmp_path, name, text):
    p = pathlib.Path(tmp_path) / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------------------- ascii


def test_ac_values_and_axis(tmp_path):
    d = psf.read_psf(write(tmp_path, "acz.ac", AC))
    name, axis = psf.sweep_axis(d)
    assert name == "freq"
    assert np.allclose(axis, [10.0, 100.0])
    assert psf.signals(d) == ["VDD0P8_A", "VDD0P8_B"]
    assert d["VDD0P8_A"].dtype == np.complex128
    assert d["VDD0P8_A"][0] == complex(-6.41661, -0.000199722)
    assert d["_header"]["analysis type"] == "ac"
    assert d["_types"]["VDD0P8_A"] == "V"


def test_dc_axis_is_named_dc(tmp_path):
    """TOOL_FACTS: the dc-sweep axis is `dc`, not the swept device and not `sweep`."""
    name, axis = psf.sweep_axis(psf.read_psf(write(tmp_path, "dcz.dc", DC)))
    assert name == "dc"
    assert np.allclose(axis, [0.0, 5e-4])


def test_tran_axis_is_named_time(tmp_path):
    name, axis = psf.sweep_axis(psf.read_psf(write(tmp_path, "trz.tran", TRAN)))
    assert name == "time"
    assert np.allclose(axis, [0.0, 1e-9])


def test_operating_point_has_no_axis_and_says_so(tmp_path):
    d = psf.read_psf(write(tmp_path, "op.dc", OP))
    assert d["_sweep"] == ""
    assert d["VDD0P8_A"][0] == pytest.approx(0.8)
    assert d["VB_IB_PTAT:p"][0] == pytest.approx(1.04e-5)
    with pytest.raises(PmuError) as exc:
        psf.sweep_axis(d)
    assert "no sweep axis" in exc.value.what.lower()
    assert exc.value.do                        # it tells the caller what to do instead


def test_noise_struct_is_not_a_complex_number(tmp_path):
    """A 2-member STRUCT has the arity of a complex pair; only the TYPE tells them apart."""
    d = psf.read_psf(write(tmp_path, "nz.noise", NOISE))
    assert psf.signals(d) == ["out"]
    assert d["out"].dtype == np.float64
    assert d["out"][0] == pytest.approx(7.703342364720425e-06)
    assert d["_groups"] == ["PMU_TOP.R1a"]     # dropped, but named -- nothing vanishes silently
    assert d["_types"]["out"] == "V/sqrt(Hz)"  # the unit the importer needs


def test_missing_value_section_is_a_clear_error(tmp_path):
    p = write(tmp_path, "broken.ac", "HEADER\n\"PSFversion\" \"1.00\"\nTYPE\nEND\n")
    with pytest.raises(PmuError) as exc:
        psf.read_psf(p)
    assert "VALUE" in exc.value.what
    assert exc.value.do


def test_find_psf_by_analysis_name(tmp_path):
    write(tmp_path, "acz.ac", AC)
    write(tmp_path, "nz.noise", NOISE)
    (pathlib.Path(tmp_path) / "logFile").write_text("x", encoding="utf-8")
    assert psf.find_psf(tmp_path, "acz").name == "acz.ac"
    assert psf.find_psf(tmp_path, "nz").name == "nz.noise"
    with pytest.raises(PmuError) as exc:
        psf.find_psf(tmp_path, "nope")
    assert "nope" in exc.value.what


def test_find_psf_names_what_is_there(tmp_path):
    write(tmp_path, "acz.ac", AC)
    with pytest.raises(PmuError) as exc:
        psf.find_psf(tmp_path, "trz")
    assert "acz.ac" in " ".join(exc.value.do)


# --------------------------------------------------------------------------- binary fixtures
# Built here from the grammar in pmukit/binpsf.py, so no captured PSF is needed.

_MAJOR, _MINOR, _DECL = 0x15, 0x16, 0x10
_PROP_INT = 0x22
_DT_REAL, _DT_COMPLEX, _DT_STRUCT = 0x0B, 0x0C, 0x10


def _s(txt):
    """A PSF `str`: length-prefixed latin1, padded to a 4-byte boundary."""
    bb = txt.encode("latin1")
    return struct.pack(">I", len(bb)) + bb + b"\x00" * (((len(bb) + 3) & ~3) - len(bb))


def _frame(bodies, minor_sections=(1, 2, 3)):
    """Wrap section bodies in their 0x15 headers and patch each minor section's end offset."""
    starts, pos = [], 0
    for body in bodies:
        starts.append(pos)
        pos += 8 + len(body)
    total = pos
    out = bytearray()
    for i, body in enumerate(bodies):
        end = starts[i + 1] if i + 1 < len(bodies) else total
        out += struct.pack(">II", _MAJOR, end) + body
    for i in minor_sections:
        struct.pack_into(">I", out, starts[i] + 8 + 4, starts[i + 1])
    return bytes(out)


def make_grouped(path, freqs, outs, widths=None):
    """A grouped (`PSF groups` = 1) noise PSF: freq + two STRUCT instances + a real `out`.

    `widths` per point lets the test forge a VARIABLE-stride file, which must fall back to the
    per-entry walk and still read freq and `out` exactly.
    """
    npoints = len(freqs)
    widths = widths or [(3, 4)] * npoints
    sweep_id, i1, i2, out_id = 1, 2, 3, 4
    t_real, t_struct = 100, 102
    hb = (struct.pack(">I", _PROP_INT) + _s("PSF groups") + struct.pack(">I", 1)
          + struct.pack(">I", _PROP_INT) + _s("PSF sweep points") + struct.pack(">I", npoints))
    minor = struct.pack(">I", _MINOR) + struct.pack(">I", 0)
    types = (struct.pack(">I", _DECL) + struct.pack(">I", t_real) + _s("real")
             + struct.pack(">I", 0) + struct.pack(">I", _DT_REAL)
             + struct.pack(">I", _DECL) + struct.pack(">I", t_struct) + _s("noiseStruct")
             + struct.pack(">I", 0) + struct.pack(">I", _DT_STRUCT))
    sweep = struct.pack(">I", _DECL) + struct.pack(">I", sweep_id) + _s("freq")
    traces = b"".join(struct.pack(">I", _DECL) + struct.pack(">I", tid) + _s(nm)
                      + struct.pack(">I", ty)
                      for tid, nm, ty in ((i1, "inst1", t_struct), (i2, "inst2", t_struct),
                                          (out_id, "out", t_real)))
    value = b""
    for (f, ov), (w1, w2) in zip(zip(freqs, outs), widths):
        value += struct.pack(">I", _DECL) + struct.pack(">I", sweep_id) + struct.pack(">d", f)
        value += (struct.pack(">I", _DECL) + struct.pack(">I", i1)
                  + struct.pack(f">{w1}d", *range(1, w1 + 1)))
        value += (struct.pack(">I", _DECL) + struct.pack(">I", i2)
                  + struct.pack(f">{w2}d", *range(1, w2 + 1)))
        value += struct.pack(">I", _DECL) + struct.pack(">I", out_id) + struct.pack(">d", ov)
    pathlib.Path(path).write_bytes(_frame([hb, minor + types, minor + sweep,
                                           minor + traces, value]))


def make_ac_binary(path, freqs, values):
    """A plain (groups=0) binary AC PSF with one complex trace."""
    npoints = len(freqs)
    sweep_id, tid, t_real, t_cplx = 1, 2, 100, 101
    hb = (struct.pack(">I", _PROP_INT) + _s("PSF sweep points") + struct.pack(">I", npoints))
    minor = struct.pack(">I", _MINOR) + struct.pack(">I", 0)
    types = (struct.pack(">I", _DECL) + struct.pack(">I", t_real) + _s("real")
             + struct.pack(">I", 0) + struct.pack(">I", _DT_REAL)
             + struct.pack(">I", _DECL) + struct.pack(">I", t_cplx) + _s("V")
             + struct.pack(">I", 0) + struct.pack(">I", _DT_COMPLEX))
    sweep = struct.pack(">I", _DECL) + struct.pack(">I", sweep_id) + _s("freq")
    traces = (struct.pack(">I", _DECL) + struct.pack(">I", tid) + _s("VDD0P8_A")
              + struct.pack(">I", t_cplx))
    value = b""
    for f, v in zip(freqs, values):
        value += struct.pack(">I", _DECL) + struct.pack(">I", sweep_id) + struct.pack(">d", f)
        value += (struct.pack(">I", _DECL) + struct.pack(">I", tid)
                  + struct.pack(">dd", v.real, v.imag))
    pathlib.Path(path).write_bytes(_frame([hb, minor + types, minor + sweep,
                                           minor + traces, value]))


def make_windowed(path, times, values, window_bytes=128):
    """A windowed transient PSF: signal-major buffers, NaN-padded last window."""
    per = window_bytes // 8 - 2                 # data doubles per block
    nbuf = (len(times) + per - 1) // per
    hdr_len = 16
    header = struct.pack(">II", 0x14, hdr_len) + b"\x00" * (hdr_len - 8)
    body = b""
    for w in range(nbuf):
        for sig in (times, values):
            chunk = list(sig[w * per:(w + 1) * per])
            chunk += [float("nan")] * (per - len(chunk))
            body += struct.pack(">dd", 0.0, 0.0) + struct.pack(f">{per}d", *chunk)
    hb = (struct.pack(">I", _PROP_INT) + _s("PSF window size") + struct.pack(">I", window_bytes)
          + struct.pack(">I", _PROP_INT) + _s("PSF sweep points")
          + struct.pack(">I", len(times)))
    minor = struct.pack(">I", _MINOR) + struct.pack(">I", 0)
    types = (struct.pack(">I", _DECL) + struct.pack(">I", 100) + _s("V")
             + struct.pack(">I", 0) + struct.pack(">I", _DT_REAL))
    sweep = struct.pack(">I", _DECL) + struct.pack(">I", 1) + _s("time")
    traces = (struct.pack(">I", _DECL) + struct.pack(">I", 2) + _s("VDD0P8_A")
              + struct.pack(">I", 100))
    pathlib.Path(path).write_bytes(_frame([hb, minor + types, minor + sweep,
                                           minor + traces, header + body]))


# --------------------------------------------------------------------------- binary


def test_grouped_psf_is_not_an_error_wall(tmp_path):
    """TOOL_FACTS: groups>=1 only means every device's noise was saved."""
    p = tmp_path / "nz.noise"
    freqs = [10.0, 100.0, 1e3, 1e4, 1e5]
    outs = [8.6e-05, 4.0e-06, 9.1e-07, 5.5e-08, 4.58e-09]
    make_grouped(p, freqs, outs)
    assert binpsf.is_binary(p)
    d = psf.read_psf(p)
    assert d["_sweep"] == "freq"
    assert psf.signals(d) == ["out"]                  # the structs are dropped
    assert sorted(d["_groups"]) == ["inst1", "inst2"]  # and named
    assert np.array_equal(d["freq"], freqs)
    assert np.array_equal(d["out"], outs)              # exact: the same doubles we wrote


def test_grouped_psf_variable_stride_falls_back_to_the_walk(tmp_path):
    """A file whose struct widths change per point must not be read by a fixed stride."""
    p = tmp_path / "nz.noise"
    freqs = [10.0, 100.0, 1e3, 1e4]
    outs = [1e-5, 2e-5, 3e-5, 4e-5]
    make_grouped(p, freqs, outs, widths=[(3, 4), (3, 7), (5, 4), (3, 4)])
    d = psf.read_psf(p)
    assert np.array_equal(d["freq"], freqs)
    assert np.array_equal(d["out"], outs)


def test_grouped_psf_single_point_uses_the_walk(tmp_path):
    p = tmp_path / "nz.noise"
    make_grouped(p, [10.0], [8.6e-05])
    d = psf.read_psf(p)
    assert np.array_equal(d["freq"], [10.0])
    assert np.array_equal(d["out"], [8.6e-05])


def test_binary_ac_is_complex_and_full_precision(tmp_path):
    p = tmp_path / "acz.ac"
    freqs = [10.0, 100.0, 1000.0]
    vals = [complex(-6.416612345678901, -1.9972e-4), complex(-6.4, -2e-3), complex(-1.0, 0.5)]
    make_ac_binary(p, freqs, vals)
    d = psf.read_psf(p)
    assert d["_sweep"] == "freq"
    assert d["VDD0P8_A"].dtype == np.complex128
    assert d["VDD0P8_A"][0] == vals[0]                 # psfascii would have rounded this
    assert d["_types"]["VDD0P8_A"] == "V"


def test_windowed_transient_is_signal_major(tmp_path):
    """The layout a per-point walk would read as 'sweep column only, every trace lost'."""
    p = tmp_path / "trz.tran"
    times = [i * 1e-9 for i in range(37)]              # not a whole number of windows
    values = [0.8 - 0.01 * i for i in range(37)]
    make_windowed(p, times, values)
    d = psf.read_psf(p)
    name, axis = psf.sweep_axis(d)
    assert name == "time"
    assert axis.size == len(times)
    assert np.allclose(axis, times)
    assert np.allclose(d["VDD0P8_A"], values)          # the trace survived the NaN pad


def test_empty_and_truncated_files_are_clear_errors(tmp_path):
    empty = tmp_path / "empty.ac"
    empty.write_bytes(b"")
    with pytest.raises(PmuError) as exc:
        psf.read_psf(empty)
    assert "empty" in exc.value.what.lower()

    short = tmp_path / "short.ac"
    short.write_bytes(struct.pack(">II", _MAJOR, 16) + b"\x00" * 8)
    with pytest.raises(PmuError) as exc:
        psf.read_psf(short)
    assert "section" in exc.value.why.lower() or "section" in exc.value.what.lower()


def test_dispatch_is_on_bytes_not_on_the_name(tmp_path):
    """ADE writes `.ac` for both formats, so the file name can never decide."""
    ascii_named_like_binary = write(tmp_path, "a.ac", AC)
    binary_named_the_same = tmp_path / "b.ac"
    make_ac_binary(binary_named_the_same, [10.0], [complex(1.0, 2.0)])
    assert not binpsf.is_binary(ascii_named_like_binary)
    assert binpsf.is_binary(binary_named_the_same)
    assert psf.read_psf(ascii_named_like_binary)["_sweep"] == "freq"
    assert psf.read_psf(binary_named_the_same)["_sweep"] == "freq"


# --------------------------------------------------------------------------- real captures
# The fixture's `work/` tree is git-ignored (it is simulator output), so these only run on a
# machine where acceptance.py has been executed.

REAL = pathlib.Path(__file__).resolve().parent / "fixtures" / "pmu_demo" / "work" / "ac" / "raw"


@pytest.mark.skipif(not (REAL / "zoutA.ac").is_file(),
                    reason="no captured Spectre output (tests/fixtures/.../work is git-ignored)")
def test_real_spectre_ac_capture():
    d = psf.read_psf(REAL / "zoutA.ac")
    name, axis = psf.sweep_axis(d)
    assert name == "freq"
    assert axis.size == 161 and axis[0] == pytest.approx(10.0)
    assert d["VDD0P8_A"].dtype == np.complex128
    assert d["VDD0P8_A"][0].real < 0        # the load isource sinks: V/I carries a minus sign


@pytest.mark.skipif(not (REAL / "nz.noise").is_file(),
                    reason="no captured Spectre output (tests/fixtures/.../work is git-ignored)")
def test_real_spectre_noise_capture():
    d = psf.read_psf(REAL / "nz.noise")
    assert psf.signals(d) == ["out"]        # 55 per-instance structs dropped
    assert len(d["_groups"]) > 10
    assert d["_types"]["out"] == "V/sqrt(Hz)"
