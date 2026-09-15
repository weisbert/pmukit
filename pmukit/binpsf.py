# from LDO_modeling/cadence/binpsf.py @ d2c5b80
"""Reader for Cadence **binary** PSF (BINPSF) -- what ADE/Maestro and the cluster write.

`pmukit.psf` dispatches here on the leading bytes, so callers never choose a format.  The
return shape is identical to the ascii reader's: ``{signal: ndarray, "_sweep": name,
"_header": {...}}``.

Self-contained (struct + numpy only, no live session, no Cadence library) -- the PSF read is a
firewall: raw signals come in, nothing is interpreted here.  Ratios are computed in
`pmukit.importer`, never in the reader.

Format (reverse-engineered, cross-validated against `spectre -format psfascii` on real ADE AC
*and* noise runs).  Big-endian throughout::

  file      := [leading word] section* toc
  section   := 0x15(MAJOR=21) endOffset:u32  body            # body spans [.. endOffset)
  HEADER    := property*                                      # name/value pairs
  property  := 0x21 str str (string) | 0x22 str u32 (int) | 0x23 str f64 (double)
  TYPE/...  := 0x16(MINOR=22) minorEnd:u32  decl*            # decls span [.. minorEnd)
  decl      := 0x10(DECL=16) id:u32 name:str ...            # type / trace declaration
  VALUE     := entry*                                         # one run of points
  entry     := 0x10(DECL=16) id:u32 value                    # value = N doubles
  str       := len:u32 bytes (padded up to a 4-byte boundary)

Each VALUE entry is ``0x10 id <N doubles>`` with NO internal markers, and one sweep point is the
fixed id-sequence ``[sweepId, trace0, trace1, ...]`` repeated `sweepPoints` times.  AC traces are
complex (2 doubles); a noise analysis adds a real scalar `out` (1 double) plus per-instance STRUCT
traces whose width VARIES by instance.  Rather than trust a fixed width, the VALUE reader walks the
KNOWN id sequence and delimits each entry by scanning (in whole-double steps) to the next
``0x10 <expectedNextId>`` -- so any struct width parses and stays aligned.  Datatype (real vs
complex vs struct) is read from the referenced TYPE; struct traces are consumed but dropped (their
names are reported under `_groups`, so nothing vanishes silently).

Two scars are load-bearing here (TOOL_FACTS "PSF / binpsf"):

  * **grouped PSF (`PSF groups` >= 1) is NOT an error wall.**  It only means every device's noise
    contribution was saved; the VALUE section is the same flat per-point layout.  Read it by the
    constant stride measured from point 0; never crawl the whole file (a 192 MB grouped-noise file
    has ~20k per-instance struct traces).
  * **windowed transient PSF (`PSF window size` != 0)** is signal-major buffered with a NaN-padded
    last window -- a completely different layout that the per-point walk would read as "sweep
    column only, every trace lost".
"""
from __future__ import annotations

import pathlib
import struct

import numpy as np

from .errors import PmuError

__all__ = ["read_binpsf", "is_binary", "SWEEP_KEY", "HEADER_KEY", "TYPES_KEY",
           "GROUPS_KEY"]

_MAJOR, _MINOR, _DECL = 0x15, 0x16, 0x10           # section / sub-section / declaration
_PROP_STR, _PROP_INT, _PROP_DBL = 0x21, 0x22, 0x23
_DT_REAL, _DT_COMPLEX, _DT_STRUCT = 0x0B, 0x0C, 0x10   # datatype codes
_GROUP = 0x11                                          # a grouped TRACE section opens with this

SWEEP_KEY = "_sweep"
HEADER_KEY = "_header"
TYPES_KEY = "_types"
GROUPS_KEY = "_groups"


def _bad(path, what: str, why: str, do=None) -> PmuError:
    return PmuError(
        what=what, why=why,
        do=list(do or ["Re-export the analysis (ADE: PSF; ALPS: `-format ps`, never psfxl).",
                       "Check the file is a complete PSF -- a killed job leaves a truncated one."]),
        where=str(path))


class _Cur:
    """A big-endian cursor over the PSF byte buffer."""

    __slots__ = ("b", "o")

    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def u32(self, at=None):
        o = self.o if at is None else at
        return struct.unpack_from(">I", self.b, o)[0]

    def f64(self, at):
        return struct.unpack_from(">d", self.b, at)[0]

    def take_u32(self):
        v = self.u32()
        self.o += 4
        return v

    def take_str(self):
        n = self.take_u32()
        s = self.b[self.o:self.o + n].decode("latin1")
        self.o += (n + 3) & ~3                       # pad up to 4-byte boundary
        return s.replace("\\<", "<").replace("\\>", ">")   # bus pins: psfascii is un-escaped


def is_binary(path) -> bool:
    """True when `path` is a binary PSF rather than psfascii.

    psfascii is plain text whose first section keyword is `HEADER`; BINPSF opens with a 0x15
    MAJOR-section marker (at word 0, or word 1 after a one-word format tag) and carries NUL
    bytes.  The marker check is the primary signal; the NUL fallback only catches odd encodings.
    """
    p = pathlib.Path(path)
    try:
        with open(p, "rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        raise PmuError(
            what=f"Could not open the PSF file {p.name}.",
            why=f"{exc.__class__.__name__}: {exc}",
            do=["Check the path, and that the results were fetched back from the run host."],
            where=str(p)) from exc
    if not head:
        raise _bad(p, f"The PSF file {p.name} is empty.",
                   "A zero-byte result file means the simulator wrote nothing -- the analysis "
                   "never produced output.",
                   ["Read the simulator log for the real error (the run's spectre.log).",
                    "Re-run the analysis."])
    if head[:6] == b"HEADER":
        return False
    if len(head) >= 8 and _MAJOR in struct.unpack_from(">II", head, 0):
        return True
    return b"\x00" in head and not head.lstrip().startswith(b"HEADER")


# ----------------------------------------------------------------- section locator
def _sections(b):
    """Top-level major sections -> [(start, end)] body ranges, in file order:
    HEADER, TYPE, SWEEP, TRACE, VALUE.  The file may begin with one leading word before the
    first 0x15 marker (a format tag); skip it if present."""
    n = len(b)
    pos = 0 if struct.unpack_from(">I", b, 0)[0] == _MAJOR else 4
    secs = []
    while pos + 8 <= n and struct.unpack_from(">I", b, pos)[0] == _MAJOR:
        end = struct.unpack_from(">I", b, pos + 4)[0]
        if end <= pos or end > n:                    # the VALUE end points at the TOC
            end = n
        secs.append((pos + 8, end))
        pos = end
    return secs


def _minor_range(b, a, z):
    """A TYPE/SWEEP/TRACE body opens with a 0x16 minor-section header (0x16 + minorEnd); the
    declarations span [a+8, minorEnd).  HEADER/VALUE have none -> (a, z)."""
    if struct.unpack_from(">I", b, a)[0] == _MINOR:
        return a + 8, struct.unpack_from(">I", b, a + 4)[0]
    return a, z


# --------------------------------------------------------------------- per-section
def _read_header(c, a, z) -> dict:
    """Parse the HEADER property list -> {name: value} (str / int / float)."""
    c.o = a
    props: dict = {}
    while c.o < z:
        code = c.u32()
        if code not in (_PROP_STR, _PROP_INT, _PROP_DBL):
            break
        c.o += 4
        name = c.take_str()
        if code == _PROP_STR:
            props[name] = c.take_str()
        elif code == _PROP_INT:
            props[name] = c.take_u32()
        else:
            props[name] = c.f64(c.o)
            c.o += 8
    return props


def _read_traces(c, a, z):
    """TRACE section -> ordered [(traceid, name, typeid)].  Each decl: 0x10 id name typeid.

    A GROUPED trace section (windowed / grouped saves, e.g. ALPS transient with every device
    trace) opens with a `0x11 <gid> <name> <count>` group header before the decls -- skip it,
    else we would read ZERO traces."""
    lo, hi = _minor_range(c.b, a, z)
    c.o = lo
    if c.o + 8 <= hi and c.u32() == _GROUP:          # 0x11 <gid> <name> <count> then the decls
        c.o += 4
        c.take_u32()                                 # group id
        c.take_str()                                 # group name
        c.take_u32()                                 # member count
    traces = []
    while c.o + 8 <= hi and c.u32() == _DECL:
        c.o += 4
        tid = c.take_u32()
        name = c.take_str()
        typeid = c.take_u32()
        traces.append((tid, name, typeid))
    return traces


def _type_datatypes(b, a, z, typeids) -> dict:
    """Map each referenced type id -> its datatype code (real / complex / struct).

    Located by the `0x10 <id>` decl marker inside the TYPE section; the datatype sits two words
    past the (padded) type name (after a 0-flag word).  Robust to the recursive struct grammar
    (whose bodies contain 0x10 words) because each needed id is looked up directly and only a
    valid datatype code is accepted."""
    lo, hi = _minor_range(b, a, z)
    out: dict = {}
    for tid in set(typeids):
        pat = struct.pack(">II", _DECL, tid)
        i = b.find(pat, lo, hi)
        while i != -1:
            o = i + 8
            n = struct.unpack_from(">I", b, o)[0]            # type name string
            o += 4 + ((n + 3) & ~3)
            dt = struct.unpack_from(">I", b, o + 4)[0]       # skip 0-flag word -> datatype
            if dt in (_DT_REAL, _DT_COMPLEX, _DT_STRUCT):
                out[tid] = dt
                break
            i = b.find(pat, i + 4, hi)                       # false hit (id inside a double)
    return out


def _read_sweep_name(c, a, z) -> str:
    """SWEEP section -> the sweep variable name ('freq' / 'dc' / 'time' / 'temp')."""
    lo, hi = _minor_range(c.b, a, z)
    c.o = lo
    if c.o < hi and c.u32() == _DECL:
        c.o += 4
        c.take_u32()                                 # sweep id (the VALUE entry id is used)
        return c.take_str()
    return "sweep"


def _scan_to_marker(b, o, limit, next_id):
    """From payload start `o`, step in whole-double (8-byte) strides to the next entry marker
    `0x10 <next_id>`; return that offset.  Stepping by 8 only ever lands on a real entry
    boundary, so a 0x10 inside a double's low word cannot false-match.  Falls back to `limit`
    (the final entry runs to the section end)."""
    p = o
    while p + 8 <= limit:
        if (struct.unpack_from(">I", b, p)[0] == _DECL
                and struct.unpack_from(">I", b, p + 4)[0] == next_id):
            return p
        p += 8
    return limit


def _measure_point(c, a, z, seq, sweep_id):
    """Measure POINT 0: walk the known id sequence once, delimiting each entry by scanning to the
    next expected marker, and record per-id (payload width in doubles, byte offset within the
    point) plus the constant point STRIDE in bytes.

    For grouped / flat PSF the per-point layout is CONSTANT, so every later point i is read
    directly at `v0 + i*stride + off` -- no full-file scan.  Returns (widths, offsets, stride),
    or None when the point-0 walk loses alignment (caller -> the slow per-entry walk)."""
    b = c.b
    widths, offs = {}, {}
    o = a
    for k, eid in enumerate(seq):
        if o + 8 > z or c.u32(o) != _DECL or c.u32(o + 4) != eid:
            return None                              # alignment lost -> not the flat layout
        payload = o + 8
        nxt = seq[k + 1] if k + 1 < len(seq) else sweep_id   # last entry -> next point's sweep
        end = _scan_to_marker(b, payload, z, nxt)
        widths[eid] = (end - payload) // 8
        offs[eid] = payload - a
        o = end
    return widths, offs, o - a


def _keep_dt(dt, width):
    """The effective scalar datatype of a measured trace: real (1 double) or complex (2 doubles),
    or None to DROP it (a struct / any wider-than-scalar trace).  Shared by the fast and slow
    paths so both keep EXACTLY the same columns."""
    if dt == _DT_REAL or (dt is None and width == 1):
        return _DT_REAL
    if dt == _DT_COMPLEX or (dt is None and width == 2):
        return _DT_COMPLEX
    return None                                      # struct / unexpected width -> dropped


def _read_values_fast(c, a, sweep_name, sweep_id, seq, name_of, dt_of,
                      widths, offs, stride, npoints):
    """Read every WANTED column by the constant stride measured from point 0 -- N x (#kept cols)
    double reads, never a full-file scan."""
    cols, order, dropped = {}, [], []
    for eid in seq:
        w = widths.get(eid)
        if w is None:
            continue
        dt = _DT_REAL if eid == sweep_id else _keep_dt(dt_of.get(eid), w)
        nm = sweep_name if eid == sweep_id else name_of[eid]
        if dt is None:                               # struct / wide -> dropped (no column)
            dropped.append(nm)
            continue
        off = offs[eid]
        if dt == _DT_REAL:
            cols[nm] = np.fromiter((c.f64(a + i * stride + off) for i in range(npoints)),
                                   dtype=float, count=npoints)
        else:
            arr = np.empty(npoints, dtype=complex)
            for i in range(npoints):
                base = a + i * stride + off
                arr[i] = complex(c.f64(base), c.f64(base + 8))
            cols[nm] = arr
        order.append(nm)
    out = {nm: cols[nm] for nm in order}              # sweep first (seq[0]), then trace order
    out[SWEEP_KEY] = sweep_name
    out[GROUPS_KEY] = dropped
    return out


def _read_values_walk(c, a, z, sweep_name, sweep_id, seq, name_of, dt_of, npoints):
    """The per-entry walk: delimit each entry by scanning to the next expected marker (handles
    variable-width struct traces).  Used for npoints<=1 and when the constant-stride invariant
    does not hold."""
    b = c.b
    cols = {sweep_name: []}
    order = [sweep_name]
    dropped = []
    for eid in seq:                                  # non-struct traces get a column, in order
        if eid == sweep_id:
            continue
        if dt_of.get(eid) != _DT_STRUCT:
            cols[name_of[eid]] = []
            order.append(name_of[eid])
        else:
            dropped.append(name_of[eid])
    total = npoints * len(seq) if npoints else 1 << 30
    o = a
    for k in range(total):
        eid = seq[k % len(seq)]
        if o + 8 > z or c.u32(o) != _DECL or c.u32(o + 4) != eid:
            break                                    # alignment lost / truncated -> stop clean
        o += 8
        last = (npoints and k == total - 1)
        nxt = None if last else seq[(k + 1) % len(seq)]
        end = z if nxt is None else _scan_to_marker(b, o, z, nxt)
        dt, nd = dt_of.get(eid), (end - o) // 8
        nm = sweep_name if eid == sweep_id else name_of[eid]
        if dt == _DT_REAL or (dt is None and nd == 1):
            cols[nm].append(c.f64(o))                # real scalar (sweep var, noise 'out')
        elif dt == _DT_COMPLEX or (dt is None and nd == 2):
            cols[nm].append(complex(c.f64(o), c.f64(o + 8)))
        # else: struct / unexpected width -> consumed but not stored (column left short)
        o = end
    # keep only columns that filled to the sweep length -- drops any trace that turned out wider
    # than a scalar (a mis-typed struct), so the returned arrays are never ragged.
    npts = len(cols[sweep_name])
    out = {sweep_name: np.asarray(cols[sweep_name])}
    for nm in order[1:]:
        if len(cols[nm]) == npts and npts:
            out[nm] = np.asarray(cols[nm])
        else:
            dropped.append(nm)
    out[SWEEP_KEY] = sweep_name
    out[GROUPS_KEY] = dropped
    return out


def _read_values_windowed(c, a, z, sweep_name, traces, npoints, window_size):
    """VALUE section of a WINDOWED PSF (`PSF window size` != 0) -- Cadence/ALPS use it for big
    TRANSIENT data.  Layout (reverse-engineered, validated on a real ALPS trz.tran)::

        VALUE = [header: 0x14, then a u32 = header length H, then zero pad to H bytes]
                [nbuf BUFFERS, each (1 sweep + #traces) SIGNAL blocks of `window_size` bytes,
                 SIGNAL-MAJOR: block 0 = sweep, block 1+i = trace i in decl order]
                [a short trailer]

    Each signal block = window_size bytes = W doubles, of which the FIRST 2 are a per-block
    header and the remaining W-2 are this signal's data for up to W-2 points of THIS window.
    Points run contiguously across buffers; the last window is short -- unused slots are NaN, so
    the true point count is where the sweep first goes non-finite.

    Vectorized with numpy (one reshape per buffer) so 17k+ traces stay fast.  Transient traces
    are real."""
    b = c.b
    hlen = c.u32(a + 4)                              # header length (the word after 0x14)
    data = a + hlen
    w_doubles = window_size // 8                     # doubles per block
    meta = 2                                         # per-block header doubles (16 B)
    per = w_doubles - meta
    nsig = len(traces) + 1                           # sweep + traces
    bufsz = nsig * window_size
    nbuf = (z - data) // bufsz if bufsz else 0       # trailer (< bufsz) dropped by floor-div
    if nbuf <= 0 or per <= 0:
        return {sweep_name: np.asarray([]), SWEEP_KEY: sweep_name, GROUPS_KEY: []}
    cols = np.empty((nsig, nbuf * per), dtype=float)
    for w in range(nbuf):
        off = data + w * bufsz
        arr = np.frombuffer(b, dtype=">f8", count=nsig * w_doubles, offset=off)
        arr = arr.reshape(nsig, w_doubles)
        cols[:, w * per:(w + 1) * per] = arr[:, meta:meta + per]
    sweep = cols[0]
    finite = np.isfinite(sweep)                      # the sweep goes NaN past the last point
    n = int(np.argmax(~finite)) if not finite.all() else sweep.size
    if npoints:
        n = min(n, npoints)
    out = {sweep_name: np.array(sweep[:n])}
    for i, (_tid, nm, _typeid) in enumerate(traces):
        out[nm] = np.array(cols[1 + i, :n])          # transient node/branch values are real
    out[SWEEP_KEY] = sweep_name
    out[GROUPS_KEY] = []
    return out


def _read_values(c, a, z, sweep_name, traces, dt_map, npoints):
    """VALUE section -> {name: ndarray}.

    The per-point layout is the fixed id sequence [sweepId, trace0, trace1, ...]; a GROUPED
    (`PSF groups` >= 1) noise PSF uses the SAME flat layout, only with ~20k per-instance STRUCT
    traces appended.  Measure point 0 first (each entry's width + offset + the constant stride),
    VALIDATE that the stride tiles the section and that the 2nd AND last points open with the
    sweep marker at the predicted offset, then read each wanted column directly by stride.  If
    that invariant does not hold (or npoints<=1) fall back to the per-entry walk."""
    b = c.b
    sweep_id = c.u32(a + 4)                          # first VALUE entry id (after 0x10 marker)
    seq = [sweep_id] + [tid for tid, _, _ in traces]
    name_of = {tid: nm for tid, nm, _ in traces}
    dt_of = {sweep_id: _DT_REAL}
    for tid, _, typeid in traces:
        dt_of[tid] = dt_map.get(typeid)
    meas = _measure_point(c, a, z, seq, sweep_id) if (npoints and npoints > 1) else None
    if meas is not None:
        widths, offs, stride = meas
        seclen = z - a
        # STRICT constant-stride invariant, so a genuinely variable-stride file (per-instance
        # struct widths that differ across sweep points) falls back to the proven walk instead of
        # being silently misread.  Real grouped files carry an exactly 4-byte trailer, so require
        # a SUB-DOUBLE trailer; AND spot-check BOTH the 2nd and the LAST point (a width change
        # that first appears late cannot be caught by point 1 alone).
        trailer = seclen - npoints * stride
        tiles = stride > 0 and 0 <= trailer < 8
        nextok = (a + stride + 8 <= z and c.u32(a + stride) == _DECL
                  and c.u32(a + stride + 4) == sweep_id)
        lp = a + (npoints - 1) * stride
        lastok = (lp + 8 <= z and c.u32(lp) == _DECL and c.u32(lp + 4) == sweep_id)
        if tiles and nextok and lastok:
            return _read_values_fast(c, a, sweep_name, sweep_id, seq, name_of, dt_of,
                                     widths, offs, stride, npoints)
    return _read_values_walk(c, a, z, sweep_name, sweep_id, seq, name_of, dt_of, npoints)


# --------------------------------------------------------------------------- API
def read_binpsf(path) -> dict:
    """Read a binary PSF -> ``{signal: ndarray, "_sweep": axis name, "_header": {...},
    "_groups": [dropped struct trace names]}``.

    Complex where Spectre stored a pair, real where it stored a scalar.  Per-instance noise
    STRUCT traces are dropped (only the scalar total `out` is part of the contract) but their
    names are listed under `_groups`."""
    p = pathlib.Path(path)
    with open(p, "rb") as fh:
        b = fh.read()
    c = _Cur(b)
    secs = _sections(b)
    if len(secs) < 5:
        raise _bad(p, f"{p.name} is not a readable binary PSF "
                      f"(found {len(secs)} sections, expected at least 5).",
                   "A binary PSF carries HEADER / TYPE / SWEEP / TRACE / VALUE as big-endian "
                   "0x15 major sections; this file's section walk stopped early, which means it "
                   "is truncated or not classic PSF (psfxl is a different container).",
                   ["Re-export with classic PSF (ALPS `-format ps`, never psfxl).",
                    "Check the simulator log -- a killed job leaves a truncated PSF."])
    (h0, h1), (t0, t1), (s0, s1), (r0, r1), (v0, v1) = secs[:5]
    hdr = _read_header(c, h0, h1)
    npoints = int(hdr.get("PSF sweep points", 0))
    traces = _read_traces(c, r0, r1)
    sweep_name = _read_sweep_name(c, s0, s1)
    window_size = int(hdr.get("PSF window size", 0) or 0)
    if window_size:
        out = _read_values_windowed(c, v0, v1, sweep_name, traces, npoints, window_size)
    else:
        dt_map = _type_datatypes(b, t0, t1, [tid for _, _, tid in traces])
        out = _read_values(c, v0, v1, sweep_name, traces, dt_map, npoints)
    out[HEADER_KEY] = hdr
    # Each trace's declared PSF type NAME ('V', 'V/sqrt(Hz)'), not its storage kind: the importer
    # reads the noise unit off it, and a unit that has to be guessed is a 1e12 error waiting.
    type_names = _type_names(b, t0, t1)
    out[TYPES_KEY] = {nm: type_names.get(ty, "") for _tid, nm, ty in traces}
    return out


def _type_names(b, a, z) -> dict:
    """TYPE section -> {type id: type name}.  Top-level declarations only."""
    lo, hi = _minor_range(b, a, z)
    out: dict = {}
    c = _Cur(b, lo)
    while c.o + 8 <= hi and c.u32() == _DECL:
        c.o += 4
        tid = c.take_u32()
        name = c.take_str()
        out[tid] = name
        # skip the rest of this declaration: scan to the next top-level 0x10 marker.
        nxt = b.find(struct.pack(">I", _DECL), c.o, hi)
        if nxt == -1 or (nxt - lo) % 4:
            break
        c.o = nxt
    return out
