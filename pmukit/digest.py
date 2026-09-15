"""Contract 5: the digest -- the air-gap return path from the box to the desk.

The box has no network and no agent (docs/CONTEXT_OF_USE.md). The only way back to the desk is
the user copying PLAIN TEXT out of the browser and pasting it through the relay. This module
renders that text and reads it back.

    [pmukit-digest v1] project=... created=... budget=64KB parts=2 part 1/2
    [D0 provenance]  config sha, dataset sha, pmukit version, TB state
    [D1 ledger]      one line per run: status, cell, analysis, CPU seconds (+ one error line)
    [D2 params]      fitted parameters, all cells, LOSSLESS JSON
    [D3 grades]      the Model screen's green/yellow/red and the four trust tiles, as text
    [D4 curves/...]  GT and model side by side on the SAME resampled grid, 12 points per decade
    [D5 transients]  decimated, extremes preserved exactly
    [D6 faillog]     last 40 log lines of each failed run + the netlist lines pmukit changed
    [D9 trailer]     blocks kept, KB, the dropped blocks NAMED, sha256 over the body

Why the model numbers travel with the digest instead of being recomputed at the desk: D4 is
resampled to a 12-points-per-decade grid, and resampling changes what a refit converges to (the
old repo proved this on Zout and PSRR). So the digest carries the MODEL curve next to the ground
truth rather than only the ground truth -- the desk compares numbers instead of re-deriving them.
D2 is the exception that makes the desk self-sufficient: it is never resampled and never rounded,
so `.va` re-emission from D2 alone reproduces the box's model bit for bit.

Budget rule (CONTRACTS.md section 5): blocks that do not fit are DROPPED BY PRIORITY, lowest
first, and NAMED in the D9 trailer. Never a silent truncation.

Text rules: pure ASCII, LF endings, no trailing whitespace on any line, one `[Dn <title>]` header
per block at column 0 and every content line indented -- so a paste that loses its final newline
or gets word-wrapped by a mail client is still either parseable or caught by the sha.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import re
from typing import Iterable

import numpy as np

from .errors import PmuError

VERSION = "v1"
BLOCKS = ("D0", "D1", "D2", "D3", "D4", "D5", "D6", "D9")     # D9 is the trailer
PRIORITY = ("D0", "D1", "D2", "D6", "D4bias", "D4rail", "D5")  # highest first
BUDGETS = (32_000, 64_000, 128_000)

DEFAULT_BUDGET = 64_000
DEFAULT_PART_SIZE = 32_000
CURVE_POINTS_PER_DECADE = 12
CURVE_LINEAR_POINTS = 41          # for axes that are not log-able (temperature sweeps)
TRAN_POINTS = 200
FAILLOG_TAIL = 40
LOG_LINE_MAX = 300
D2_CHUNK = 110

# internal block id -> (header id, header title)
_HEAD = {
    "D0": ("D0", "provenance"),
    "D1": ("D1", "ledger"),
    "D2": ("D2", "params"),
    "D3": ("D3", "grades"),
    "D4bias": ("D4", "curves/bias"),
    "D4rail": ("D4", "curves/rail"),
    "D5": ("D5", "transients"),
    "D6": ("D6", "faillog"),
    "D9": ("D9", "trailer"),
}
_FROM_HEAD = {v: k for k, v in _HEAD.items()}
ORDER = ("D0", "D1", "D2", "D3", "D4bias", "D4rail", "D5", "D6")
CONTENT_BLOCKS = ORDER

_TITLES = {
    "D0": "Provenance + config", "D1": "Ledger summary", "D2": "Fitted parameters, all cells",
    "D3": "Grades + trust summary", "D4bias": "Curves, bias (idc / inoise / yout)",
    "D4rail": "Curves, rail (zout / psrr / noise)", "D5": "Transients, load-EN",
    "D6": "Failed-run logs + netlist diff",
}

# D3 is not named in the contract's priority list (provenance > ledger > params > failed logs >
# bias curves > rail curves > transients). It is a few hundred bytes and it is the "can I trust
# this" answer, so it ranks immediately after params.
_RANK: dict[str, float] = {bid: float(i) for i, bid in enumerate(PRIORITY)}
_RANK["D3"] = _RANK["D2"] + 0.5

_BIAS_KINDS = {"idc", "inoise", "yout", "iv", "dc_iv", "ac_yout", "noise_i", "bias"}
_RAIL_KINDS = {"zout", "psrr", "noise", "vnoise", "ac_zout", "ac_psrr", "noise_v", "rail"}

_PART_HEAD = re.compile(r"^\[pmukit-digest (\S+)\]\s*(.*)$")
_BLOCK_HEAD = re.compile(r"^\[(D\d+)\s+([^\]]*)\]\s*$")
_RUN_LINE = re.compile(r"^run (\S+) (\S+) (\S+) (.*?) cpu=(\S+)$")
_GRADE_LINE = re.compile(r"^grade (\S+) (\S+) (\S+) (\S+) score=(\S+) \| (.*)$")
_TRUST_LINE = re.compile(r"^trust ([^:]+): (.*)$")
_SERIES_LINE = re.compile(
    r"^(curve|tran) (\S+) kind=(\S*) port=(\S*) cell=(\S*) x=(\S*) cols=(\S+) n=(\d+)$")
_KV = re.compile(r"(\w+)=(\S+)")

_ASCII_MAP = {
    "·": ".", "µ": "u", "μ": "u", "—": "-", "–": "-", "…": "...",
    "°": "deg", "Ω": "ohm", "×": "x", "→": "->", "≤": "<=",
    "≥": ">=", "²": "^2", "“": '"', "”": '"', "‘": "'", "’": "'",
    "›": ">", " ": " ",
}


# --------------------------------------------------------------------------- small helpers
def _ascii(text) -> str:
    s = str(text)
    for bad, good in _ASCII_MAP.items():
        s = s.replace(bad, good)
    return s.encode("ascii", "replace").decode("ascii")


def _oneline(text) -> str:
    return " ".join(_ascii(text).split())


def _token(text, default: str = "-") -> str:
    """A whitespace-free token safe for the fixed-field record lines."""
    t = "_".join(_ascii(text).split())
    return t or default


def _jsonable(obj):
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _budget_text(budget: int) -> str:
    return f"{budget // 1000}KB" if budget % 1000 == 0 else f"{budget}B"


def _budget_value(text: str) -> int:
    t = text.strip().upper()
    if t.endswith("KB"):
        return int(float(t[:-2]) * 1000)
    if t.endswith("B"):
        return int(float(t[:-1]))
    return int(float(t))


def _rank(bid: str) -> float:
    return _RANK.get(bid, 99.0)


def _block_text(bid: str, lines: list[str]) -> str:
    head_id, title = _HEAD[bid]
    out = [f"[{head_id} {title}]"]
    out += [_ascii(line).rstrip() for line in lines]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- decimation
def _decimate_indices(y, n: int) -> np.ndarray:
    """Uniform sample indices with the argmin / argmax slots forced in (endpoints protected)."""
    y = np.asarray(y, dtype=float)
    size = y.size
    if size == 0:
        return np.zeros(0, dtype=int)
    if n >= size or n <= 0:
        return np.arange(size)
    idx = np.unique(np.linspace(0, size - 1, n).round().astype(int))
    finite = np.isfinite(y)
    extremes = []
    if finite.any():
        where = np.flatnonzero(finite)
        extremes = [int(where[np.argmin(y[finite])]), int(where[np.argmax(y[finite])])]
    for k in extremes:
        if k in idx:
            continue
        inner = np.arange(1, idx.size - 1)
        if inner.size == 0:
            idx = np.unique(np.append(idx, k))
            continue
        j = int(inner[np.argmin(np.abs(idx[inner] - k))])
        idx[j] = k
        idx = np.unique(idx)
    return idx


def decimate_preserving_extremes(t, y, n: int):
    """Decimate (t, y) to about `n` samples, keeping the dip minimum, the overshoot maximum and
    the settled (last) value VERBATIM -- those samples are copied, never interpolated."""
    t = np.asarray(t)
    y = np.asarray(y)
    if t.shape[0] != y.shape[0]:
        raise PmuError(
            what="cannot decimate a transient: t and y have different lengths.",
            why=f"t has {t.shape[0]} samples and y has {y.shape[0]}; they must be the same sweep.",
            do=["pass the time vector and the waveform of the same run"],
            where="pmukit.digest.decimate_preserving_extremes",
        )
    idx = _decimate_indices(y, n)
    return t[idx], y[idx]


# --------------------------------------------------------------------------- resampling
def _as_columns(side, prefix: str) -> dict:
    if side is None:
        return {}
    if isinstance(side, dict):
        return {f"{prefix}.{k}": v for k, v in side.items()}
    return {prefix: side}


def _curve_columns(entry: dict) -> dict:
    cols: dict = {}
    for key in ("gt", "model"):
        cols.update(_as_columns(entry.get(key), key))
    for key, value in entry.get("columns", {}).items():
        cols[str(key)] = value
    return cols


def _resample(x, cols: dict, name: str) -> tuple[np.ndarray, dict]:
    """GT and model onto ONE grid: 12 points per decade in log x, linear when x is not log-able.
    Never upsamples -- if the target grid is not sparser than the data, the data grid is kept."""
    x = np.asarray(x, dtype=float)
    if x.size < 2:
        return x, {k: np.asarray(v, dtype=float) for k, v in cols.items()}
    for key, values in cols.items():
        if np.asarray(values).shape[0] != x.shape[0]:
            raise PmuError(
                what=f"curve {name!r} column {key!r} does not match its x axis.",
                why=f"x has {x.shape[0]} points and {key!r} has {np.asarray(values).shape[0]}; "
                    f"GT and model must be sampled on the same sweep before resampling.",
                do=["fix the curve payload so every column has one value per x point"],
                where="pmukit.digest.export",
            )
    order = np.argsort(x, kind="stable")
    x = x[order]
    cols = {k: np.asarray(v, dtype=float)[order] for k, v in cols.items()}
    lo, hi = float(x[0]), float(x[-1])
    log_ok = lo > 0 and hi / lo >= 10.0
    if log_ok:
        n = int(round(CURVE_POINTS_PER_DECADE * math.log10(hi / lo))) + 1
        grid = np.logspace(math.log10(lo), math.log10(hi), max(2, n))
    else:
        grid = np.linspace(lo, hi, min(CURVE_LINEAR_POINTS, x.size))
    if grid.size >= x.size:
        return x, cols                       # never upsample: keep the measured grid
    if log_ok:
        out = {k: np.interp(np.log10(grid), np.log10(x), v) for k, v in cols.items()}
    else:
        out = {k: np.interp(grid, x, v) for k, v in cols.items()}
    return grid, out


def _sub_of(entry: dict) -> str:
    sub = str(entry.get("sub", "")).lower()
    if sub in ("bias", "rail"):
        return sub
    kind = str(entry.get("kind", "")).lower()
    if kind in _BIAS_KINDS:
        return "bias"
    if kind in _RAIL_KINDS:
        return "rail"
    return "rail"


# --------------------------------------------------------------------------- block renderers
def _render_d0(payload: dict) -> list[str]:
    prov = payload.get("provenance") or {}
    if not prov:
        return []
    lines = ["  # config sha, dataset sha, pmukit version, TB state at characterization"]
    for key in sorted(prov, key=str):
        value = prov[key]
        text = _oneline(value) if isinstance(value, str) else _oneline(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=_jsonable))
        lines.append(f"  {_token(key)} = {text}")
    return lines


def _cell_of(run: dict) -> str:
    """The compact cell tag. Accepts a ready-made `cell`, or builds one from a contract-3 ledger
    row (`process` / `temp_c` / `vset` / `load_key`) so `Run.to_dict()` can be passed straight in."""
    if run.get("cell"):
        return _token(run["cell"])
    bits = []
    if run.get("process"):
        bits.append(str(run["process"]))
    temp = run.get("temp_c")
    if temp is not None:
        try:
            value = float(temp)
            bits.append("Tsweep" if value != value else f"{value:g}c")
        except (TypeError, ValueError):
            pass
    if run.get("vset") is not None:
        bits.append(f"v{run['vset']}")
    if run.get("load_key"):
        bits.append(str(run["load_key"]))
    return _token("/".join(bits)) if bits else "-"


def _cpu_of(run: dict):
    value = run.get("cpu_s", run.get("cpu_seconds"))
    try:
        return repr(float(value)) if value is not None else "-"
    except (TypeError, ValueError):
        return "-"


def _render_d1(payload: dict) -> list[str]:
    ledger = payload.get("ledger") or []
    if not ledger:
        return []
    by_status: dict[str, int] = {}
    cpu = 0.0
    for run in ledger:
        by_status[str(run.get("status", "unknown"))] = \
            by_status.get(str(run.get("status", "unknown")), 0) + 1
        try:
            cpu += float(run.get("cpu_s", run.get("cpu_seconds")) or 0.0)
        except (TypeError, ValueError):
            pass
    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    lines = [f"  # {len(ledger)} runs: {summary}; cpu_s total {cpu:.1f}"]
    for run in ledger:
        lines.append("  run {rid} {st} {cell} {an} cpu={cpu}".format(
            rid=_token(run.get("run_id", "-")), st=_token(run.get("status", "unknown")),
            cell=_cell_of(run), an=_oneline(run.get("analysis", "-")) or "-",
            cpu=_cpu_of(run)))
        err = run.get("error")
        if err:
            lines.append("    error: " + (_oneline(err)[:LOG_LINE_MAX] or "-"))
    return lines


def _render_d2(payload: dict) -> list[str]:
    params = payload.get("params")
    if not params:
        return []
    text = json.dumps(params, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=_jsonable, allow_nan=True)
    lines = ["  # lossless: strip the 2-space indent and the trailing '|', join, json.loads()",
             "  # never resampled, never rounded -- the desk re-emits the .va from this block"]
    for i in range(0, len(text), D2_CHUNK):
        lines.append("  " + text[i:i + D2_CHUNK] + "|")
    return lines


def _render_d3(payload: dict) -> list[str]:
    grades = payload.get("grades") or []
    trust = payload.get("trust") or {}
    if not grades and not trust:
        return []
    lines = ["  # can I trust this model in my simulation?"]
    for key in sorted(trust, key=str):
        lines.append(f"  trust {_token(key)}: {_oneline(trust[key])}")
    for g in grades:
        score = g.get("score")
        try:
            score_txt = repr(float(score)) if score is not None else "-"
        except (TypeError, ValueError):
            score_txt = "-"
        cell = g.get("cell", g.get("corner", "-"))
        detail = _oneline(g.get("detail", "")).replace("|", "/") or "-"
        lines.append(f"  grade {_token(g.get('port', '-'))} {_token(cell)} "
                     f"{_token(g.get('block', '-'))} {_token(g.get('grade', 'not_run'))} "
                     f"score={score_txt} | {detail}")
    return lines


def _render_series(name: str, entry: dict, keyword: str, x_key: str,
                   default_x_label: str, fmt) -> list[str]:
    x = entry.get(x_key, entry.get("x"))
    cols = _curve_columns(entry)
    if x is None or not cols:
        return []
    if keyword == "curve":
        grid, out = _resample(x, cols, name)
    else:
        grid, out = _decimate(x, cols, int(entry.get("n") or TRAN_POINTS), name)
    names = sorted(out)
    head = ("  {kw} {name} kind={kind} port={port} cell={cell} x={xl} cols={cols} n={n}").format(
        kw=keyword, name=_token(name), kind=_token(entry.get("kind", "-")),
        port=_token(entry.get("port", "-")), cell=_token(entry.get("cell", "-")),
        xl=_token(entry.get("x_label", default_x_label)), cols=",".join(names), n=grid.size)
    lines = [head]
    for i in range(grid.size):
        row = [fmt(float(grid[i]))] + [fmt(float(out[c][i])) for c in names]
        lines.append("    " + " ".join(row))
    return lines


def _decimate(x, cols: dict, n: int, name: str) -> tuple[np.ndarray, dict]:
    x = np.asarray(x, dtype=float)
    cols = {k: np.asarray(v, dtype=float) for k, v in cols.items()}
    for key, values in cols.items():
        if values.shape[0] != x.shape[0]:
            raise PmuError(
                what=f"transient {name!r} column {key!r} does not match its time axis.",
                why=f"t has {x.shape[0]} samples and {key!r} has {values.shape[0]}.",
                do=["pass the time vector and the waveforms of the same run"],
                where="pmukit.digest.export",
            )
    ref = cols.get("gt", cols[sorted(cols)[0]])
    idx = _decimate_indices(ref, n)
    return x[idx], {k: v[idx] for k, v in cols.items()}


def _render_d4(payload: dict, sub: str) -> list[str]:
    curves = payload.get("curves") or {}
    picked = [(k, v) for k, v in curves.items() if _sub_of(v) == sub]
    if not picked:
        return []
    lines = [f"  # GT and model on the SAME grid, {CURVE_POINTS_PER_DECADE} points per decade;",
             "  # resampled -- the MODEL numbers travel here, do not refit from this block"]
    for name, entry in picked:
        lines += _render_series(name, entry, "curve", "x", "f[Hz]", lambda v: f"{v:.6e}")
    return lines


def _render_d5(payload: dict) -> list[str]:
    trans = payload.get("transients") or {}
    if not trans:
        return []
    lines = ["  # decimated; dip minimum, overshoot maximum and the settled value are exact",
             "  # values use full repr precision so those samples round-trip verbatim"]
    for name, entry in trans.items():
        lines += _render_series(name, entry, "tran", "t", "t[s]", repr)
    return lines


def _render_d6(payload: dict) -> list[str]:
    fails = payload.get("faillog") or []
    if not fails:
        return []
    lines = [f"  # last {FAILLOG_TAIL} log lines of each failed run + the netlist lines pmukit "
             f"changed"]
    for item in fails:
        lines.append(f"  fail {_token(item.get('run_id', '-'))}")
        edits = item.get("netlist_edits") or []
        if isinstance(edits, str):
            edits = edits.splitlines()
        for line in edits:
            lines.append("    netlist: " + _oneline(line)[:LOG_LINE_MAX])
        tail = item.get("log_tail") or []
        if isinstance(tail, str):
            tail = tail.splitlines()
        for line in list(tail)[-FAILLOG_TAIL:]:
            lines.append("    log: " + _oneline(line)[:LOG_LINE_MAX])
    return lines


_RENDER = {
    "D0": _render_d0,
    "D1": _render_d1,
    "D2": _render_d2,
    "D3": _render_d3,
    "D4bias": lambda p: _render_d4(p, "bias"),
    "D4rail": lambda p: _render_d4(p, "rail"),
    "D5": _render_d5,
    "D6": _render_d6,
}


def _render_all(payload: dict, ids: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for bid in ids:
        lines = _RENDER[bid](payload)
        if lines:
            out[bid] = _block_text(bid, lines)
    return out


def _render_trailer(kept: list[str], dropped: list[str], body: str) -> str:
    total = len(kept) + len(dropped)
    sha = hashlib.sha256(body.encode("ascii")).hexdigest()
    names = ", ".join(sorted(dropped, key=lambda b: (_rank(b), b))) if dropped else "none"
    return _block_text("D9", [
        f"  kept {len(kept)} of {total} blocks, {len(body) / 1000.0:.1f} KB",
        f"  dropped (budget): {names}",
        f"  sha256 {sha}",
    ])


# --------------------------------------------------------------------------- selection
def _select_ids(blocks, payload: dict) -> list[str]:
    if blocks is None:
        return list(CONTENT_BLOCKS)
    wanted: list[str] = []
    for raw in blocks:
        bid = str(raw)
        if bid == "D4":
            wanted += ["D4bias", "D4rail"]
        elif bid == "D9":
            continue                                    # the trailer is always written
        elif bid in CONTENT_BLOCKS:
            wanted.append(bid)
        else:
            raise PmuError(
                what=f"unknown digest block {bid!r}.",
                why="the digest is made of the blocks named in CONTRACTS.md section 5; an unknown "
                    "id would silently produce an empty paste.",
                do=[f"use one of: {', '.join(CONTENT_BLOCKS)} (or 'D4' for both curve blocks)"],
                where="pmukit.digest.export(blocks=...)",
            )
    seen, out = set(), []
    for bid in wanted:
        if bid not in seen:
            seen.add(bid)
            out.append(bid)
    return out


def _fit(rendered: dict[str, str], budget: int) -> tuple[list[str], list[str]]:
    """Greedy by priority, highest first; a block that does not fit is dropped and the next one
    is still tried (CONTRACTS.md section 5: drop the low-priority blocks, never truncate)."""
    order = sorted(rendered, key=lambda b: (_rank(b), b))
    reserve = 220
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for bid in order:
        size = len(rendered[bid])
        if bid == "D0" or used + size + reserve <= budget:
            kept.append(bid)
            used += size
        else:
            dropped.append(bid)
    # exact correction: the reserve above is an estimate, the trailer is not
    while True:
        body = "".join(rendered[b] for b in ORDER if b in kept)
        trailer = _render_trailer(kept, dropped, body)
        if len(body) + len(trailer) <= budget:
            break
        droppable = [b for b in kept if b != "D0"]
        if not droppable:
            break
        worst = max(droppable, key=lambda b: (_rank(b), b))
        kept.remove(worst)
        dropped.append(worst)
    return kept, dropped


def _assemble(payload: dict, ids: list[str], budget: int) -> tuple[str, list[str], list[str]]:
    rendered = _render_all(payload, ids)
    if not rendered:
        rendered = {"D0": _block_text(
            "D0", ["  # no block selected, or every selected block is empty in this payload"])}
    kept, dropped = _fit(rendered, budget)
    body = "".join(rendered[b] for b in ORDER if b in kept)
    trailer = _render_trailer(kept, dropped, body)
    return body + trailer, kept, dropped


# --------------------------------------------------------------------------- part splitting
def _part_header(project: str, created: str, budget: int, i: int, n: int) -> str:
    return (f"[pmukit-digest {VERSION}] project={_token(project, '-')} "
            f"created={_token(created, '-')} budget={_budget_text(budget)} parts={n} part {i}/{n}")


def _chunk_lines(lines: list[str], cap: int) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if cost > cap:
            raise PmuError(
                what="one digest line is longer than a single relay part.",
                why=f"the line is {cost} bytes and a part holds {cap}; splitting inside a line "
                    f"would corrupt the block it belongs to.",
                do=["raise part_size", "or drop the block that carries this line"],
                where=line[:80],
            )
        if used + cost > cap and cur:
            chunks.append("\n".join(cur) + "\n")
            cur, used = [], 0
        cur.append(line)
        used += cost
    chunks.append("\n".join(cur) + "\n" if cur else "\n")
    return chunks


def _split_parts(full: str, project: str, created: str, budget: int,
                 part_size: int) -> list[str]:
    lines = full.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = 1
    chunks = [full]
    for _ in range(6):
        cap = part_size - len(_part_header(project, created, budget, n, n)) - 1
        if cap <= 0:
            raise PmuError(
                what=f"part_size {part_size} is too small for a digest part header.",
                why="every part repeats the [pmukit-digest] header so parts can be pasted in any "
                    "order; the header alone does not fit.",
                do=[f"use one of the budget steps: {', '.join(str(b) for b in BUDGETS)}"],
                where="pmukit.digest.export(part_size=...)",
            )
        chunks = _chunk_lines(lines, cap)
        if len(chunks) == n:
            break
        n = len(chunks)
    return [_part_header(project, created, budget, i + 1, len(chunks)) + "\n" + c
            for i, c in enumerate(chunks)]


# --------------------------------------------------------------------------- public: export
def export(payload: dict, *, budget: int = DEFAULT_BUDGET, blocks: Iterable[str] | None = None,
           project: str = "", part_size: int = DEFAULT_PART_SIZE) -> list[str]:
    """Render the digest and split it into parts. Returns the list of part texts, each starting
    with its own `[pmukit-digest v1] ... part i/N` header. Blocks that do not fit the budget are
    DROPPED BY PRIORITY (lowest first) and NAMED in the D9 trailer -- never silently truncated."""
    meta = payload.get("meta") or {}
    project = project or str(meta.get("project", "") or "")
    created = str(meta.get("created", "") or _now_iso())
    ids = _select_ids(blocks, payload)
    full, _kept, _dropped = _assemble(payload, ids, int(budget))
    return _split_parts(full, project, created, int(budget), int(part_size))


def blocks_available(payload: dict) -> list[dict]:
    """[{"id","title","bytes","priority","included_by_default"}] -- the Digest screen's list."""
    rendered = _render_all(payload, CONTENT_BLOCKS)
    ranked = sorted(CONTENT_BLOCKS, key=lambda b: (_rank(b), b))
    out = []
    for bid in ORDER:
        if bid not in rendered:
            continue
        out.append({"id": bid, "title": _TITLES[bid], "bytes": len(rendered[bid]),
                    "priority": ranked.index(bid), "included_by_default": True})
    return out


def estimate(payload: dict, blocks, budget) -> dict:
    """{"bytes","parts","dropped"} for a selection -- the Digest screen's budget bar. `parts` is
    counted at the default part size, the same one `export` uses."""
    ids = _select_ids(blocks, payload)
    full, _kept, dropped = _assemble(payload, ids, int(budget))
    parts = _split_parts(full, str((payload.get("meta") or {}).get("project", "")),
                         str((payload.get("meta") or {}).get("created", "") or _now_iso()),
                         int(budget), DEFAULT_PART_SIZE)
    return {"bytes": len(full), "parts": len(parts),
            "dropped": sorted(dropped, key=lambda b: (_rank(b), b))}


# --------------------------------------------------------------------------- public: parse
def _normalize(text: str) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def _split_pasted(text: str) -> list[str]:
    """One paste may hold several parts; cut at every [pmukit-digest ...] header line."""
    lines = _normalize(text).split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("[pmukit-digest")]
    if not starts:
        raise PmuError(
            what="this text is not a pmukit digest.",
            why="no line starts with '[pmukit-digest' -- the part header is the first line of "
                "every part and carries the version, the project and the part index.",
            do=["paste the whole block the box printed, header line included",
                "on the box: Copy for desk, then relay one part at a time"],
            where="pmukit.digest.parse",
        )
    out = []
    for k, start in enumerate(starts):
        stop = starts[k + 1] if k + 1 < len(starts) else len(lines)
        out.append("\n".join(lines[start:stop]))
    return out


def _parse_header(text: str) -> tuple[dict, str]:
    head, _, rest = _normalize(text).partition("\n")
    m = _PART_HEAD.match(head.strip())
    if not m:
        raise PmuError(
            what="a digest part has no readable header.",
            why="the first line of a part must be '[pmukit-digest v1] project=... part i/N'; "
                f"this one is {head.strip()[:60]!r}.",
            do=["re-copy the part from the box without editing the first line"],
            where="pmukit.digest.parse",
        )
    version, fields = m.group(1), m.group(2)
    if version != VERSION:
        raise PmuError(
            what=f"digest version {version!r} cannot be read by this pmukit.",
            why=f"this build reads {VERSION!r}; the block grammar changed between versions, so "
                f"parsing it anyway would silently mis-assign numbers.",
            do=[f"use a pmukit that writes {VERSION!r}", "or re-export the digest on the box"],
            where="pmukit.digest.parse",
        )
    kv = dict(_KV.findall(fields))
    pm = re.search(r"part (\d+)/(\d+)", fields)
    if not pm:
        raise PmuError(
            what="a digest part does not say which part it is.",
            why="the header must end with 'part i/N' so parts pasted in any order can be "
                "reassembled and a missing one can be named.",
            do=["re-copy the part from the box without editing the first line"],
            where=head.strip()[:80],
        )
    meta = {"version": version, "project": kv.get("project", ""),
            "created": kv.get("created", ""),
            "budget": _budget_value(kv.get("budget", "0B")),
            "index": int(pm.group(1)), "parts": int(pm.group(2))}
    body = rest.rstrip("\n")
    return meta, (body + "\n" if body else "")


def _reassemble(texts: list[str]) -> tuple[dict, str]:
    seen: dict[int, str] = {}
    metas: list[dict] = []
    for text in texts:
        meta, body = _parse_header(text)
        metas.append(meta)
        i = meta["index"]
        if i in seen:
            raise PmuError(
                what=f"digest part {i} was pasted twice.",
                why="each part appears exactly once; a duplicate means one paste overwrote "
                    "another and a different part is probably missing.",
                do=[f"paste each part of {meta['parts']} exactly once",
                    "check the 'part i/N' line at the top of every paste"],
                where=f"part {i}/{meta['parts']}",
            )
        seen[i] = body
    total = metas[0]["parts"]
    for meta in metas:
        if meta["parts"] != total:
            raise PmuError(
                what="the pasted parts come from two different digests.",
                why=f"one header says parts={total} and another says parts={meta['parts']}; "
                    f"their bodies cannot be concatenated.",
                do=["re-export the digest on the box and paste all parts of that one export"],
                where="pmukit.digest.parse",
            )
    missing = [i for i in range(1, total + 1) if i not in seen]
    if missing:
        raise PmuError(
            what=f"digest part {missing[0]} of {total} is missing.",
            why=f"parts {', '.join(str(i) for i in missing)} were never pasted, so the body is "
                f"incomplete and its sha256 cannot be checked.",
            do=[f"paste the missing part(s): {', '.join(str(i) for i in missing)}",
                "on the box the Copy button steps through the parts one at a time"],
            where=f"{len(seen)} of {total} parts present",
        )
    full = "".join(seen[i] for i in range(1, total + 1))
    return metas[0], full


def _check_sha(full: str) -> tuple[str, dict]:
    marker = re.search(r"^\[D9 trailer\]$", full, re.M)
    pos = marker.start() if marker else -1
    if pos < 0:
        raise PmuError(
            what="the digest has no D9 trailer.",
            why="the trailer carries the sha256 of the body and names the blocks dropped for "
                "budget; without it nothing can be verified and a silent truncation is possible.",
            do=["paste the last part as well -- the trailer is always at the end"],
            where="pmukit.digest.parse",
        )
    body, trailer = full[:pos], full[pos:]
    m = re.search(r"^\s*sha256 (\S+)\s*$", trailer, re.M)
    if not m:
        raise PmuError(
            what="the digest trailer has no sha256 line.",
            why="the trailer must end with 'sha256 <hex>' over the concatenated body.",
            do=["re-copy the last part from the box"],
            where=trailer[:80],
        )
    want = m.group(1)
    got = hashlib.sha256(body.encode("ascii", "replace")).hexdigest()
    if got != want:
        raise PmuError(
            what="the digest body does not match the sha256 in its trailer.",
            why=f"the trailer expects {want[:12]}... and the pasted body hashes to {got[:12]}...; "
                f"the text was edited, wrapped or truncated in transit.",
            do=["re-copy each part from the box without editing or re-wrapping it",
                "paste into a plain-text buffer, not a rich-text editor"],
            where=f"body {len(body)} bytes",
        )
    dropped: list[str] = []
    dm = re.search(r"^\s*dropped \(budget\): (.*)$", trailer, re.M)
    if dm and dm.group(1).strip() != "none":
        dropped = [x.strip() for x in dm.group(1).split(",") if x.strip()]
    kept = 0
    km = re.search(r"^\s*kept (\d+) of (\d+) blocks", trailer, re.M)
    if km:
        kept = int(km.group(1))
    return body, {"sha256": want, "dropped": dropped, "kept": kept}


def _split_blocks(body: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for line in body.split("\n"):
        m = _BLOCK_HEAD.match(line)
        if m:
            bid = _FROM_HEAD.get((m.group(1), m.group(2).strip()), m.group(1))
            cur = []
            out.append((bid, cur))
        elif cur is not None:
            cur.append(line)
    return out


def _content(lines: list[str]) -> list[str]:
    """Drop comments and blank lines; keep the 2-space indent stripped exactly once."""
    out = []
    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
        out.append(line[2:] if line.startswith("  ") else line.lstrip())
    return out


def _parse_series(lines: list[str]) -> dict:
    out: dict = {}
    cur: dict | None = None
    names: list[str] = []
    for raw in _content(lines):
        m = _SERIES_LINE.match(raw.strip())
        if m:
            _kw, name, kind, port, cell, xlabel, cols, _n = m.groups()
            names = cols.split(",")
            cur = {"kind": kind, "port": port, "cell": cell, "x_label": xlabel,
                   "x": [], "_cols": {c: [] for c in names}}
            out[name] = cur
            continue
        if cur is None:
            continue
        bits = raw.split()
        if len(bits) != len(names) + 1:
            continue
        try:
            values = [float(b) for b in bits]
        except ValueError:
            continue
        cur["x"].append(values[0])
        for col, value in zip(names, values[1:]):
            cur["_cols"][col].append(value)
    for entry in out.values():
        cols = entry.pop("_cols")
        for col, values in cols.items():
            if "." in col:
                head, tail = col.split(".", 1)
                entry.setdefault(head, {})[tail] = values
            else:
                entry[col] = values
    return out


def parse(text_or_parts) -> dict:
    """Reassemble the parts (in any order), verify the sha256 in the trailer, and return the
    payload. PmuError on a missing part, a duplicate part, a version mismatch or a sha mismatch --
    each naming exactly which part and what to do about it."""
    if isinstance(text_or_parts, str):
        texts = _split_pasted(text_or_parts)
    else:
        texts = []
        for item in text_or_parts:
            texts += _split_pasted(item)
    if not texts:
        raise PmuError(
            what="nothing to parse: no digest parts were given.",
            why="parse() needs the pasted text of at least one part.",
            do=["paste the text the box printed under Copy for desk"],
            where="pmukit.digest.parse",
        )
    meta, full = _reassemble(texts)
    body, trailer = _check_sha(full)

    payload: dict = {"meta": {k: meta[k] for k in ("version", "project", "created", "budget",
                                                   "parts")},
                     "provenance": {}, "ledger": [], "params": {}, "grades": [], "trust": {},
                     "curves": {}, "transients": {}, "faillog": [],
                     "dropped": trailer["dropped"]}

    for bid, lines in _split_blocks(body):
        if bid == "D0":
            for raw in _content(lines):
                key, sep, value = raw.partition(" = ")
                if sep:
                    payload["provenance"][key.strip()] = value.strip()
        elif bid == "D1":
            for raw in _content(lines):
                raw = raw.strip()
                m = _RUN_LINE.match(raw)
                if m:
                    cpu = m.group(5)
                    payload["ledger"].append({
                        "run_id": m.group(1), "status": m.group(2), "cell": m.group(3),
                        "analysis": m.group(4).strip(),
                        "cpu_s": float(cpu) if cpu not in ("-", "") else None, "error": ""})
                elif raw.startswith("error: ") and payload["ledger"]:
                    payload["ledger"][-1]["error"] = raw[len("error: "):]
        elif bid == "D2":
            # Select D2 chunk lines structurally (indent + trailing '|'), not via _content():
            # a JSON chunk may legitimately start with '#' and must not be taken for a comment.
            text = "".join(line[2:-1] for line in lines
                           if line.startswith("  ") and line.endswith("|"))
            if text:
                try:
                    payload["params"] = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise PmuError(
                        what="the D2 params block is not valid JSON.",
                        why=f"json.loads failed at character {exc.pos}: {exc.msg}; a line of the "
                            f"lossless block was edited or re-wrapped in transit.",
                        do=["re-copy the part that carries [D2 params] without editing it"],
                        where="[D2 params]",
                    ) from exc
        elif bid == "D3":
            for raw in _content(lines):
                raw = raw.strip()
                m = _GRADE_LINE.match(raw)
                if m:
                    score = m.group(5)
                    payload["grades"].append({
                        "port": m.group(1), "cell": m.group(2), "block": m.group(3),
                        "grade": m.group(4),
                        "score": float(score) if score != "-" else None,
                        "detail": "" if m.group(6) == "-" else m.group(6)})
                    continue
                t = _TRUST_LINE.match(raw)
                if t:
                    payload["trust"][t.group(1).strip()] = t.group(2).strip()
        elif bid in ("D4", "D4bias", "D4rail"):
            sub = "bias" if bid == "D4bias" else "rail" if bid == "D4rail" else ""
            for name, entry in _parse_series(lines).items():
                if sub:
                    entry["sub"] = sub
                payload["curves"][name] = entry
        elif bid == "D5":
            for name, entry in _parse_series(lines).items():
                entry["t"] = entry.pop("x")
                payload["transients"][name] = entry
        elif bid == "D6":
            cur = None
            for raw in _content(lines):
                raw = raw.strip()
                if raw.startswith("fail "):
                    cur = {"run_id": raw[5:].strip(), "netlist_edits": [], "log_tail": []}
                    payload["faillog"].append(cur)
                elif cur is not None and raw.startswith("netlist: "):
                    cur["netlist_edits"].append(raw[len("netlist: "):])
                elif cur is not None and raw.startswith("log: "):
                    cur["log_tail"].append(raw[len("log: "):])
    return payload


# --------------------------------------------------------------------------- convenience
def failure_bundle(payload: dict, budget: int = 32_000) -> list[str]:
    """The Run screen's 'Copy failure bundle': provenance, ledger and the failed-run logs."""
    return export(payload, budget=budget, blocks=("D0", "D1", "D6"))


def desk_bundle(payload: dict, *, cells=None, budget: int = DEFAULT_BUDGET) -> list[str]:
    """The Model screen's 'Copy for desk': everything, optionally narrowed to a few cells."""
    if cells is not None:
        wanted = {str(c) for c in cells}
        payload = dict(payload)
        payload["curves"] = {k: v for k, v in (payload.get("curves") or {}).items()
                             if str(v.get("cell", "")) in wanted}
        payload["transients"] = {k: v for k, v in (payload.get("transients") or {}).items()
                                 if str(v.get("cell", "")) in wanted}
    return export(payload, budget=budget)
