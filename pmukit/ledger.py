"""Contract 3: the run ledger -- one SQLite row per simulation, at
`$PMUKIT_DATA/<project>/runs.sqlite`.

The ledger is the single source of truth for "what did we ask the simulator to do, what came
back, and which fitted parameter needed it".  The Plan screen and the Run screen read ONLY this
table (OVERNIGHT_BRIEF route table), so every question those routes ask is answered here:

    Plan  /plan/runs, /runs/<id>/recipe      -> `all()`, `get()`, `Run.recipe`
    Plan  "why does this run exist?"         -> `consumers()`, `why()`
    Run   /ledger?status=                    -> `all(status=...)`, `counts_by_status()`
    Run   /runs/<id>                         -> `get()`, `to_rows()`
    Report "NOT RUN" section (contract 0c)   -> `not_run()`
    Cost account (contract 3 rule)           -> `cost_by_analysis()`, `total_cpu_hours()`

Two tables only, with exactly the columns of CONTRACTS.md section 3, in the contract's order.
The journal is SQLite's default rollback journal, NOT WAL: on the box the ledger sits on an NFS
workarea, and WAL's shared-memory index does not work on a network filesystem.  The web shell
reading while a runner thread writes is covered by `busy_timeout`.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import pathlib
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from . import jsonio, paths
from .errors import PmuError

__all__ = ["STATUSES", "ANALYSES", "Run", "Recipe", "Ledger", "make_run_id", "canon_load_key"]

STATUSES = ("planned", "submitted", "running", "done", "failed", "skipped_cached", "imported")
ANALYSES = ("dc_load", "dc_temp", "dc_iv", "ac", "noise",
            "tran_load_on", "tran_load_off", "tran_en")

#: Stored statuses a re-plan is never allowed to reset back to "planned".
PROTECTED = ("done", "failed", "imported")
#: Statuses that mean "we have results for this cell".
HAVE_RESULTS = ("done", "imported", "skipped_cached")

# Columns of `runs`, in the exact order of CONTRACTS.md section 3.
RUN_COLUMNS = ("run_id", "process", "temp_c", "vset", "load_key", "analysis", "stimulus",
               "reads", "netlist_sha", "netlist_path", "psf_path", "engine", "job_id",
               "recipe", "status", "source_path", "submitted_at", "finished_at",
               "cpu_seconds", "peak_mem_mb", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,        -- 12-hex content hash: netlist + corner + analysis
  process TEXT, temp_c REAL, vset INTEGER, load_key TEXT,
  analysis TEXT,                  -- dc_load | dc_temp | dc_iv | ac | noise | tran_load_on | tran_load_off | tran_en
  stimulus TEXT,                  -- one hot source: which source is excited
  reads TEXT,                     -- JSON array of "<observable>.<port>" names (contract 2)
  netlist_sha TEXT, netlist_path TEXT, psf_path TEXT,
  engine TEXT, job_id TEXT,
  recipe TEXT,                    -- human-readable recipe: netlist edits + analyses + submit
  status TEXT,                    -- planned | submitted | running | done | failed | skipped_cached | imported
  source_path TEXT,               -- imported only: the external result directory
  submitted_at TEXT, finished_at TEXT, cpu_seconds REAL, peak_mem_mb REAL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS consumes (
  run_id TEXT, port TEXT, block TEXT, param TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS ix_runs_analysis ON runs(analysis);
CREATE UNIQUE INDEX IF NOT EXISTS ux_consumes ON consumes(run_id, port, block, param);
CREATE INDEX IF NOT EXISTS ix_consumes_param ON consumes(port, block, param);
"""

# A number that stands alone -- not a digit buried inside an identifier such as VDD0P8_A.
_NUM = re.compile(r"(?<![A-Za-z0-9_.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![A-Za-z0-9_.])")


def _utcnow() -> str:
    """UTC timestamp, second resolution, sortable as text."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_num(value) -> str:
    """Canonical text for a number: 25.0 -> '25', 5e-4 -> '0.0005'."""
    f = float(value)
    if f == int(f) and abs(f) < 1e16:
        return str(int(f))
    return f"{f:.12g}"


def canon_load_key(value) -> str:
    """Canonical text for a load cell key.

    A load key is either a label ("on_a", "off_a"), a bare current ("5e-4"), or a small
    composite ("VDD0P8_A=5e-4").  Any standalone number inside it is re-formatted, so
    `5e-4`, `0.0005` and `0.00050` all produce the same key -- and therefore the same
    `run_id`.  Identifiers are left alone: the digits in `VDD0P8_A` are not numbers.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _fmt_num(value)
    text = str(value).strip()
    return _NUM.sub(lambda m: _fmt_num(m.group(0)), text)


def make_run_id(*, netlist_sha: str, process: str, temp_c: float, vset: int,
                load_key: str, analysis: str, stimulus: str) -> str:
    """12-hex content hash of the *simulation*, not of the request around it.

    THIS IS THE RESUME MECHANISM (CONTRACTS.md section 3: "same netlist and corner submitted
    again is `skipped_cached`").  The hash covers exactly the seven fields that decide what
    the simulator actually computes: the netlist content, the corner cell
    (process / temp_c / vset / load_key), the analysis and the one excited source.

    Deliberately NOT hashed:
      * `reads`  -- a re-plan that harvests MORE observables out of the same AC sweep must
                    collide with the run already done, otherwise contract 3's AC superposition
                    ("one injection, read every port") would re-simulate for free outputs.
      * `recipe` -- cosmetic text; re-wording it must not orphan finished results.
      * `engine`, `job_id`, timing, `status` -- who ran it and how it went, not what it is.

    `temp_c` is rounded to 3 decimals and `load_key` goes through `canon_load_key()`, so
    25 vs 25.0 and 5e-4 vs 0.0005 hash identically.
    """
    key = {
        "netlist_sha": str(netlist_sha or ""),
        "process": str(process or ""),
        "temp_c": round(float(temp_c), 3) + 0.0,   # +0.0 folds -0.0 onto 0.0
        "vset": int(vset),
        "load_key": canon_load_key(load_key),
        "analysis": str(analysis or ""),
        "stimulus": str(stimulus or ""),
    }
    return jsonio.sha(key, 12)


@dataclass
class Run:
    """One row of `runs`.  Field order follows the contract's column order."""

    run_id: str
    process: str
    temp_c: float
    vset: int
    load_key: str            # "" when the run has no load axis
    analysis: str
    stimulus: str            # the ONE source that is excited, e.g. "IL_VDD0P8_A"
    reads: list[str]         # ["ac_zout.VDD0P8_A", "ac_psrr.IB_PTAT", ...] -- contract-2 names
    netlist_sha: str = ""
    netlist_path: str = ""
    psf_path: str = ""
    engine: str = ""
    job_id: str = ""
    recipe: str = ""         # see `Recipe`
    status: str = "planned"
    source_path: str = ""    # set only for status "imported"
    submitted_at: str = ""
    finished_at: str = ""
    cpu_seconds: float = 0.0
    peak_mem_mb: float = 0.0
    error: str = ""

    def __post_init__(self) -> None:
        # Coerce so a row that came from somewhere else cannot leak None into the web API.
        for name in ("run_id", "process", "load_key", "analysis", "stimulus", "netlist_sha",
                     "netlist_path", "psf_path", "engine", "job_id", "recipe", "status",
                     "source_path", "submitted_at", "finished_at", "error"):
            value = getattr(self, name)
            setattr(self, name, "" if value is None else str(value))
        # SQLite has no NaN: a run that SWEEPS temperature stores NULL and must come
        # back as NaN, not crash. NaN means 'this run is not at one temperature'.
        self.temp_c = float('nan') if self.temp_c is None else float(self.temp_c)
        self.vset = int(self.vset)
        if isinstance(self.reads, str):
            self.reads = [self.reads]
        self.reads = [str(r) for r in (self.reads or [])]
        self.cpu_seconds = float(self.cpu_seconds or 0.0)
        self.peak_mem_mb = float(self.peak_mem_mb or 0.0)

    # -- identity -------------------------------------------------------------
    def content_id(self) -> str:
        """The `run_id` this run's content implies (see `make_run_id`)."""
        return make_run_id(netlist_sha=self.netlist_sha, process=self.process,
                           temp_c=self.temp_c, vset=self.vset, load_key=self.load_key,
                           analysis=self.analysis, stimulus=self.stimulus)

    # -- helpers used by the screens -----------------------------------------
    def ports(self) -> list[str]:
        """Ports this run reads, in first-seen order ('ac_zout.VDD0P8_A' -> 'VDD0P8_A')."""
        seen: list[str] = []
        for r in self.reads:
            port = r.rsplit(".", 1)[-1] if "." in r else ""
            if port and port not in seen:
                seen.append(port)
        return seen

    def touches_port(self, port: str) -> bool:
        return port in self.ports()

    def cell_text(self) -> str:
        """The corner cell in the user's words: 'tt, 25 C, VSET 3, load on_a'."""
        temp = ("T swept" if self.temp_c != self.temp_c    # NaN: the run sweeps temperature
                else f"{_fmt_num(self.temp_c)} C")
        bits = [self.process or "?", temp, f"VSET {self.vset}"]
        bits.append(f"load {self.load_key}" if self.load_key else "no load axis")
        return ", ".join(bits)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict) -> "Run":
        d = dict(row)
        try:
            reads = json.loads(d.get("reads") or "[]")
        except json.JSONDecodeError:
            reads = []
        if not isinstance(reads, list):
            reads = [str(reads)]
        d["reads"] = reads
        return cls(**{k: d.get(k) for k in RUN_COLUMNS})


@dataclass
class Recipe:
    """Human-readable "what exactly did we do to the netlist" text.

    CONTRACTS.md section 3: `~` edited in place (original value in a trailing comment),
    `+` added, `-` stripped.  Every producer (planner, runner, importer) builds its recipe
    through this class so the Plan and Run screens can show one format.

    `text()` is sectioned so it survives a round trip through `parse()`; the submit command
    is ALWAYS the last line, which is what the box operator copies.
    """

    edits: list[str] = field(default_factory=list)      # pre-formatted, see edit()/add()/strip()
    analyses: list[str] = field(default_factory=list)   # the analysis statements written in
    saves: list[str] = field(default_factory=list)
    submit: str = ""                                    # for spectre_ssh: the literal remote tcsh command

    _SECTIONS = ("edits", "analyses", "saves", "submit")

    @staticmethod
    def edit(line: str, was: str) -> str:
        """`~ <line>        // was <was>` -- an in-place edit, original value kept."""
        return f"~ {line}        // was {was}"

    @staticmethod
    def add(line: str) -> str:
        return f"+ {line}"

    @staticmethod
    def strip(line: str) -> str:
        return f"- {line}"

    def text(self) -> str:
        out: list[str] = []
        for name in ("edits", "analyses", "saves"):
            lines = [str(x) for x in getattr(self, name)]
            if lines:
                out.append(f"[{name}]")
                out.extend(lines)
        if self.submit:
            out.append("[submit]")
            out.append(self.submit)
        return "\n".join(out)

    @classmethod
    def parse(cls, text: str) -> "Recipe":
        """Inverse of `text()`.  Lines before any header are treated as edits."""
        r = cls()
        bucket = "edits"
        submit: list[str] = []
        for raw in (text or "").splitlines():
            line = raw.rstrip("\r")
            if not line.strip():
                continue
            head = line.strip()
            if head.startswith("[") and head.endswith("]") and head[1:-1] in cls._SECTIONS:
                bucket = head[1:-1]
                continue
            if bucket == "submit":
                submit.append(line)
            else:
                getattr(r, bucket).append(line)
        r.submit = "\n".join(submit)
        return r


class Ledger:
    """The per-project run ledger.  Open it, or use it as a context manager."""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web shell runs long jobs on a background thread and
        # hands the same Ledger to them; callers serialize their own writes.
        self._db = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # DELETE, not WAL -- see the module docstring (NFS).  Also converts an old WAL ledger.
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    @classmethod
    def for_project(cls, project: str) -> "Ledger":
        """`$PMUKIT_DATA/<project>/runs.sqlite`, creating the project tree if needed."""
        return cls(paths.ensure_project(project) / "runs.sqlite")

    @property
    def connection(self) -> sqlite3.Connection:
        """Escape hatch for raw queries (digest, tests).  Do not write through it."""
        return self._db

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writing --------------------------------------------------------------
    def upsert(self, run: Run) -> str:
        """Insert, or update a run that already exists.  Returns the run_id.

        Re-planning must never clobber a finished run: when the stored status is one of
        done/failed/imported and the incoming status is "planned", the stored row keeps its
        status, timing (submitted_at / finished_at / cpu_seconds / peak_mem_mb), psf_path,
        error, engine, job_id and source_path -- the outcome belongs to the simulation, not
        to the plan.  What IS refreshed is the plan's own view of the run: `reads`, `recipe`
        and `netlist_path` (the plan may now harvest more observables from the same sweep,
        or the netlist may have moved).  `reads` is replaced, not merged: if a re-plan no
        longer wants an observable, the run genuinely no longer feeds it.
        """
        with self._db:
            return self._apply(run)[0]

    def plan_many(self, runs: Iterable[Run]) -> dict:
        """Bulk upsert in one transaction.

        Returns {"new": n, "cached": n, "updated": n}, where "cached" counts incoming
        *planned* runs whose stored status is already done/imported -- that is the resume
        count the Plan screen shows as "already have results, will not re-simulate".
        """
        tally = {"new": 0, "cached": 0, "updated": 0}
        with self._db:
            for run in runs:
                tally[self._apply(run)[1]] += 1
        return tally

    def _apply(self, run: Run) -> tuple[str, str]:
        """Do one upsert inside the caller's transaction.  Returns (run_id, kind)."""
        if run.status not in STATUSES:
            raise PmuError(
                what=f"Cannot store run with status {run.status!r}.",
                why="CONTRACTS.md section 3 fixes the status set; an unknown value would make the "
                    "Run screen and the NOT RUN report disagree about what happened.",
                do=[f"Use one of: {', '.join(STATUSES)}."],
                where=f"{self.path}: runs.status")
        if run.analysis not in ANALYSES:
            raise PmuError(
                what=f"Cannot store run with analysis {run.analysis!r}.",
                why="CONTRACTS.md section 3 enumerates the analyses the planner may emit; a typo "
                    "here would silently drop the run out of the cost account and the dataset.",
                do=[f"Use one of: {', '.join(ANALYSES)}."],
                where=f"{self.path}: runs.analysis")

        run_id = run.run_id or run.content_id()
        cur = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        stored = cur.fetchone()
        values = run.to_dict()
        values["run_id"] = run_id
        values["reads"] = jsonio.canon(run.reads)

        if stored is None:
            cols = ", ".join(RUN_COLUMNS)
            marks = ", ".join("?" for _ in RUN_COLUMNS)
            self._db.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})",
                             [values[c] for c in RUN_COLUMNS])
            return run_id, "new"

        kind = "updated"
        if stored["status"] in PROTECTED and run.status == "planned":
            # keep the outcome, refresh the plan's view
            for col in ("status", "psf_path", "engine", "job_id", "source_path",
                        "submitted_at", "finished_at", "cpu_seconds", "peak_mem_mb", "error"):
                values[col] = stored[col]
            if stored["status"] in ("done", "imported"):
                kind = "cached"
        sets = ", ".join(f"{c} = ?" for c in RUN_COLUMNS if c != "run_id")
        self._db.execute(f"UPDATE runs SET {sets} WHERE run_id = ?",
                         [values[c] for c in RUN_COLUMNS if c != "run_id"] + [run_id])
        return run_id, kind

    def set_status(self, run_id: str, status: str, *, error: str = "", job_id: str | None = None,
                   psf_path: str | None = None, cpu_seconds: float | None = None,
                   peak_mem_mb: float | None = None, finished: bool = False,
                   submitted: bool = False) -> None:
        """Move one run along its lifecycle.

        `error` is always written (pass "" to clear it on a retry); the optional keywords are
        left untouched when None.  `submitted=True` / `finished=True` stamp submitted_at /
        finished_at from the current UTC time.
        """
        if status not in STATUSES:
            raise PmuError(
                what=f"Unknown run status {status!r}.",
                why="CONTRACTS.md section 3 fixes the status set; the Run screen filters and the "
                    "NOT RUN report are written against exactly those values.",
                do=[f"Use one of: {', '.join(STATUSES)}.",
                    "If a new lifecycle state is really needed, change CONTRACTS.md first."],
                where=f"{self.path}: runs.status")
        cols = ["status = ?", "error = ?"]
        args: list = [status, error or ""]
        if job_id is not None:
            cols.append("job_id = ?"); args.append(job_id)
        if psf_path is not None:
            cols.append("psf_path = ?"); args.append(psf_path)
        if cpu_seconds is not None:
            cols.append("cpu_seconds = ?"); args.append(float(cpu_seconds))
        if peak_mem_mb is not None:
            cols.append("peak_mem_mb = ?"); args.append(float(peak_mem_mb))
        if submitted:
            cols.append("submitted_at = ?"); args.append(_utcnow())
        if finished:
            cols.append("finished_at = ?"); args.append(_utcnow())
        args.append(run_id)
        with self._db:
            cur = self._db.execute(f"UPDATE runs SET {', '.join(cols)} WHERE run_id = ?", args)
            if cur.rowcount == 0:
                raise PmuError(
                    what=f"No run {run_id!r} in the ledger.",
                    why="set_status() updates an existing row; this run_id was never planned or "
                        "imported, so there is nothing to move.",
                    do=["Plan the run first (pmukit plan), then submit it.",
                        "Check the id against `pmukit status` / GET /api/p/<project>/ledger."],
                    where=str(self.path))

    # -- reading --------------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return Run.from_row(row) if row else None

    def all(self, *, status: str | Sequence[str] | None = None,
            analysis: str | Sequence[str] | None = None,
            process: str | Sequence[str] | None = None,
            port: str | None = None, limit: int | None = None, offset: int = 0) -> list[Run]:
        """Ledger rows in insertion (plan) order.

        `port` filters on the reads list: a run "touches" a port when any read is
        '<observable>.<port>', so the Model screen can ask "what fed VDD0P8_A?".
        `limit`/`offset` are applied after every filter, including `port`.
        """
        where, args = [], []
        for col, val in (("status", status), ("analysis", analysis), ("process", process)):
            clause, a = _in_clause(col, val)
            if clause:
                where.append(clause); args.extend(a)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid"
        runs = [Run.from_row(r) for r in self._db.execute(sql, args)]
        if port:
            runs = [r for r in runs if r.touches_port(port)]
        if offset:
            runs = runs[offset:]
        if limit is not None:
            runs = runs[:limit]
        return runs

    def counts_by_status(self) -> dict[str, int]:
        """Every contract status, zero-filled, plus anything unexpected found in the file."""
        out = {s: 0 for s in STATUSES}
        for row in self._db.execute("SELECT status, COUNT(*) AS n FROM runs GROUP BY status"):
            out[row["status"]] = out.get(row["status"], 0) + row["n"]
        return out

    def not_run(self) -> list[Run]:
        """Contract 0c: what the report must list as NOT RUN.

        Everything that did not produce results -- planned, submitted, running, failed.
        `skipped_cached` counts as run: the results are already in the dataset.
        """
        marks = ", ".join("?" for _ in HAVE_RESULTS)
        rows = self._db.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({marks}) ORDER BY rowid", HAVE_RESULTS)
        return [Run.from_row(r) for r in rows]

    def to_rows(self, runs: Iterable[Run] | None = None) -> list[dict]:
        """JSON-safe dicts for the web API (every value is str / int / float / list)."""
        runs = self.all() if runs is None else runs
        return [r.to_dict() for r in runs]

    # -- consumes: which parameter ate which run ------------------------------
    def add_consumes(self, run_id: str, entries: Iterable[tuple[str, str, str]]) -> None:
        """Record (port, block, param) triples that this run feeds.  Idempotent."""
        rows = [(run_id, str(p), str(b), str(prm)) for p, b, prm in entries]
        if not rows:
            return
        if self.get(run_id) is None:
            raise PmuError(
                what=f"Cannot attach consumers to unknown run {run_id!r}.",
                why="`consumes` is the Plan screen's reverse lookup for 'why does this run exist?'; "
                    "a triple pointing at no run would show up as an orphan reason.",
                do=["Upsert the run first, then add its consumers."],
                where=str(self.path))
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO consumes (run_id, port, block, param) VALUES (?, ?, ?, ?)",
                rows)

    def consumers(self, run_id: str) -> list[tuple[str, str, str]]:
        rows = self._db.execute(
            "SELECT port, block, param FROM consumes WHERE run_id = ? ORDER BY port, block, param",
            (run_id,))
        return [(r["port"], r["block"], r["param"]) for r in rows]

    def runs_for_param(self, port: str, block: str, param: str | None = None) -> list[str]:
        """Run ids feeding one parameter, or a whole block when `param` is None."""
        sql = "SELECT DISTINCT run_id FROM consumes WHERE port = ? AND block = ?"
        args = [port, block]
        if param is not None:
            sql += " AND param = ?"
            args.append(param)
        return [r["run_id"] for r in self._db.execute(sql + " ORDER BY run_id", args)]

    def why(self, run_id: str) -> str:
        """One paragraph, used verbatim by the Plan screen's Why panel and `pmukit status`."""
        run = self.get(run_id)
        if run is None:
            raise PmuError(
                what=f"No run {run_id!r} in the ledger.",
                why="why() explains a stored run; this id is not in the table.",
                do=["Check the id against `pmukit status` / GET /api/p/<project>/ledger."],
                where=str(self.path))
        who = self.consumers(run_id)
        tail = f"(analysis {run.analysis}, stimulus {run.stimulus or 'none'})"
        if not who:
            reads = ", ".join(run.reads) or "nothing"
            return (f"Run {run_id} has no recorded consumer: it reads {reads} at {run.cell_text()} "
                    f"{tail}, but no fitted parameter has claimed it yet.")
        names = [f"{p}.{b}.{prm}" for p, b, prm in who]
        ports = {p for p, _, _ in who}
        wanted = [r for r in run.reads if r.rsplit(".", 1)[-1] in ports] or run.reads
        return (f"Run {run_id} exists because {', '.join(names)} "
                f"{'needs' if len(names) == 1 else 'need'} {', '.join(wanted) or 'its results'} "
                f"at {run.cell_text()} {tail}.")

    # -- cost accounting ------------------------------------------------------
    def cost_by_analysis(self) -> dict[str, dict]:
        """{analysis: {"runs": n, "cpu_seconds": s, "done": n}} -- contract 3's cost account.

        "runs" counts every planned row, "cpu_seconds" sums what was actually burned (rows
        that never ran contribute 0.0) and "done" counts the rows that produced results
        (done or imported).
        """
        out: dict[str, dict] = {}
        for row in self._db.execute(
                "SELECT analysis, COUNT(*) AS n, "
                "       COALESCE(SUM(cpu_seconds), 0.0) AS cpu, "
                "       SUM(CASE WHEN status IN ('done', 'imported') THEN 1 ELSE 0 END) AS ok "
                "FROM runs GROUP BY analysis ORDER BY analysis"):
            out[row["analysis"]] = {"runs": row["n"], "cpu_seconds": float(row["cpu"]),
                                    "done": int(row["ok"] or 0)}
        return out

    def total_cpu_hours(self) -> float:
        row = self._db.execute("SELECT COALESCE(SUM(cpu_seconds), 0.0) AS s FROM runs").fetchone()
        return float(row["s"]) / 3600.0


def _in_clause(col: str, value) -> tuple[str, list]:
    """Build 'col = ?' or 'col IN (?, ?)' for a str or a sequence of str."""
    if value is None:
        return "", []
    if isinstance(value, str):
        return f"{col} = ?", [value]
    vals = list(value)
    if not vals:
        return "", []
    marks = ", ".join("?" for _ in vals)
    return f"{col} IN ({marks})", vals
