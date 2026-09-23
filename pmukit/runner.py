"""The runner: take a compiled plan, make the simulations happen, land the results.

One interface, four backends (`pmukit.backends`).  The Runner owns everything that is the same
whichever engine runs the job -- the run directory, the resume cache, the ledger bookkeeping, the
import into the dataset -- and the backend owns only "get this deck simulated and bring the
results back".

Resume is not a separate mechanism: `Run.run_id` is a content hash of the netlist plus the corner
cell plus the analysis (CONTRACTS.md section 3), so re-planning an unchanged run collides with the
finished one and is reported `skipped_cached`.

The run directory, one per run, under `$PMUKIT_DATA/<project>/runs/<run_id>/`::

    input.scs     the netlist variant this run simulates
    recipe.txt    the human-readable recipe (netlist edits + analyses + the submit command)
    raw/          the PSF the simulator wrote (fetched back for a remote engine)
    spectre.log   the simulator log (fetched back too; its tail is the `error` on a failure)

Lifecycle, per run:  planned -> submitted -> running -> done | failed.  A run whose ledger status
already carries results is never re-submitted while `resume=True`.

`done` is where a run pmukit ITSELF simulated ends, whatever engine produced it; the engine is
recorded in the `engine` column, so a `fake` result is always identifiable as synthetic.  The
status `imported` is reserved for CONTRACTS.md section 3's other branch -- results the USER
already had, read out of an external result directory, always with `source_path` set (see
`importer.import_external`).  Keeping the two apart is what lets the Plan screen's "already
covered" count, `cost_by_analysis()["done"]` and the report all stay true.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import pathlib
import re
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from . import paths
from .errors import PmuError
from .ledger import HAVE_RESULTS, Ledger, Run

__all__ = ["Job", "Backend", "Runner", "parse_spectre_log", "BACKEND_STATES"]

#: What `Backend.poll()` may return.  "skipped" is the dry-run engine's honest answer: nothing
#: was submitted, so there is neither a result nor a failure.
BACKEND_STATES = ("running", "done", "failed", "skipped")

NETLIST_NAME = "input.scs"
RECIPE_NAME = "recipe.txt"
LOG_NAME = "spectre.log"
PSF_DIRNAME = "raw"

_TIME_UNIT = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "sec": 1.0, "min": 60.0, "m": 60.0, "h": 3600.0}
_MEM_UNIT = {"bytes": 1.0 / (1 << 20), "kbytes": 1.0 / 1024.0, "mbytes": 1.0, "gbytes": 1024.0}

_CPU_TOTAL = re.compile(r"Time used:\s*CPU\s*=\s*([0-9.eE+-]+)\s*([A-Za-z]+)")
_CPU_ACC = re.compile(r"Time accumulated:\s*CPU\s*=\s*([0-9.eE+-]+)\s*([A-Za-z]+)")
_PEAK_MEM = re.compile(r"Peak (?:resident )?memory used\s*=\s*([0-9.eE+-]+)\s*([A-Za-z]+)")


def parse_spectre_log(text: str) -> tuple[float, float]:
    """``(cpu_seconds, peak_mem_mb)`` from a Spectre log; ``(0.0, 0.0)`` when it says neither.

    Spectre's closing audit reads `Time used: CPU = 211 ms, elapsed = 211 ms.` and
    `Peak memory used = 101 Mbytes.`; per-analysis lines read `Time accumulated: CPU = ...` and
    `Peak resident memory used = ...`.  The aggregate wins when present, otherwise the largest
    accumulated value -- both are cumulative, so the maximum is the run's total.
    """
    cpu = 0.0
    hits = _CPU_TOTAL.findall(text or "")
    if not hits:
        hits = _CPU_ACC.findall(text or "")
    for value, unit in hits:
        try:
            cpu = max(cpu, float(value) * _TIME_UNIT.get(unit.lower(), 1.0))
        except ValueError:
            continue
    mem = 0.0
    for value, unit in _PEAK_MEM.findall(text or ""):
        try:
            mem = max(mem, float(value) * _MEM_UNIT.get(unit.lower(), 1.0))
        except ValueError:
            continue
    return cpu, mem


def log_tail(path, lines: int = 40, cap: int = 4000) -> str:
    """The last `lines` lines of a log, capped to `cap` characters -- the ledger `error` text."""
    p = pathlib.Path(path) if path else None
    if p is None or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return tail[-cap:]


# --------------------------------------------------------------------------- the job
@dataclass
class Job:
    """One simulation handed to a backend.

    `workdir` already holds `input.scs` and `recipe.txt` when the backend sees it; the backend
    fills `job_id` and, after `fetch()`, `log_path`.  `state` and `detail` are the backend's
    scratch space -- a backend whose `submit()` blocks records its outcome there and `poll()`
    simply reports it.
    """

    run: Run
    workdir: pathlib.Path
    netlist_text: str
    site: object                      # SiteConfig; duck-typed so tests can pass a stub
    job_id: str = ""
    log_path: pathlib.Path | None = None
    state: str = ""                   # backend scratch: the last known BACKEND_STATE
    detail: str = ""                  # backend scratch: ONE readable line about that state
    console: str = ""                 # backend scratch: the engine's console tail, if any

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def psf_dir(self) -> pathlib.Path:
        return pathlib.Path(self.workdir) / PSF_DIRNAME

    @property
    def netlist_path(self) -> pathlib.Path:
        return pathlib.Path(self.workdir) / NETLIST_NAME


@runtime_checkable
class Backend(Protocol):
    """What every engine must provide.  Nothing else in pmukit knows which engine ran a job."""

    name: str

    def available(self) -> tuple[bool, str]:
        """``(usable?, one line saying why / which version)``.  NEVER raises: the New screen and
        `pmukit doctor` call it to decide what to offer."""

    def submit(self, job: "Job") -> str:
        """Start the job; return the engine's job id.  May block (spectre_ssh does) or not."""

    def poll(self, job: "Job") -> str:
        """One of `BACKEND_STATES`."""

    def fetch(self, job: "Job") -> pathlib.Path:
        """Bring the results local; return the PSF directory.  Called for failures too, because
        that is how the log gets home."""

    def kill(self, job: "Job") -> None:
        """Cancel a running job.  Best effort; never raises for a job that already finished."""


# --------------------------------------------------------------------------- the runner
EventFn = Callable[[str, str, str], None]


class Runner:
    """Submit a plan's runs, poll them to completion, import the results, keep the ledger true."""

    def __init__(self, project, plan, ledger: Ledger, site, *,
                 dataset=None, root=None, backend=None, jobs: int | None = None, aux=()):
        """
        `project`  a ProjectConfig (or its name) -- only the name and config sha are used.
        `plan`     the compiled Plan; its enabled runs are what `run_all` submits.
        `ledger`   the project's run ledger (contract 3).
        `site`     the SiteConfig; `site.engine` picks the backend unless `backend` is given.
        `dataset`  a Dataset to import into.  None (the default) opens or creates the project's
                   own dataset from the plan's axes; pass `False` to run WITHOUT importing.
        `root`     where run directories live (default `$PMUKIT_DATA/<project>/runs`).
        `backend`  an explicit Backend instance, for tests and for `--engine` overrides.
        `jobs`     thread-pool width (the `--jobs` knob).  Default 1: `spectre_ssh` runs one deck
                   at a time so a shared VM is not oversubscribed, and 1 keeps the log order
                   readable.  The fake and dry-run engines ignore it in practice.
        `aux`      files or directories copied into EVERY run directory before submission.  A
                   testbench whose `include` lines are relative (a PDK checked in next to the
                   deck) does not resolve once the deck is copied somewhere else; listing the
                   directory here makes the run directory self-contained, which is also what
                   makes it shippable to another machine.  An ADE export with absolute include
                   paths needs none.
        """
        self.project = getattr(project, "project", None) or str(project)
        self.config_sha = getattr(project, "sha", lambda: "")() if hasattr(project, "sha") else ""
        # The PMU instance name, so the importer can read pin names out of the deck the way the
        # New screen does.  Absent, the importer falls back (and says which fallback it used).
        self.pmu_inst = str(getattr(project, "pmu_inst", "") or "")
        self.plan = plan
        self.ledger = ledger
        self.site = site
        self.jobs = max(1, int(jobs or 1))
        self._jobs_given = jobs is not None
        self.aux = [pathlib.Path(a) for a in (aux or ())]
        self.root = (pathlib.Path(root) if root is not None
                     else paths.ensure_project(self.project) / "runs")
        self.root.mkdir(parents=True, exist_ok=True)
        self.backend = backend if backend is not None else self._make_backend()
        self.timeout_s: float | None = None

        self._jobs: dict[str, Job] = {}
        #: run_id -> the importer's per-run report, so the caller can see what was stored.
        self.reports: dict[str, dict] = {}
        self._planned = {p.run_id: p for p in plan.runs(enabled_only=False)}
        self._skipped: set[str] = set()
        # ONE lock over every shared mutation.  The ledger is a single sqlite connection whose
        # docstring says callers serialize their own writes, and a Dataset owns numpy buffers plus
        # one index.json -- neither survives two worker threads writing at once.  The simulation
        # itself, which is the slow part, stays outside it.
        self._lock = threading.RLock()

        self._dataset = None if dataset is False else dataset
        self._own_dataset = dataset is None
        self._no_dataset = dataset is False

    # ------------------------------------------------------------------ construction helpers
    def _make_backend(self):
        from .backends import make_backend
        return make_backend(getattr(self.site, "engine", "dry_run"), self.site)

    # ------------------------------------------------------------------ serialized mutations
    def _upsert(self, run: Run) -> str:
        with self._lock:
            return self.ledger.upsert(run)

    def _consumes(self, run_id: str, feeds) -> None:
        with self._lock:
            self.ledger.add_consumes(run_id, feeds)

    def _status(self, run_id: str, status: str, **kw) -> None:
        with self._lock:
            self.ledger.set_status(run_id, status, **kw)

    def _get(self, run_id: str):
        with self._lock:
            return self.ledger.get(run_id)

    def dataset(self):
        """The dataset results are imported into; opened or created on first use.

        Created from the PLAN's own axes (`importer.dims_from_plan`), so the hyper-rectangle is
        exactly the cells the plan will fill -- the dataset never claims an axis nobody ran.
        """
        if self._no_dataset:
            return None
        with self._lock:
            return self._dataset_locked()

    def _dataset_locked(self):
        if self._dataset is None:
            from . import importer
            self._dataset = importer.open_or_create(
                paths.project_dir(self.project) / "dataset", self.plan,
                project=self.project, config_sha=self.config_sha or self.plan.config_sha)
        return self._dataset

    # ------------------------------------------------------------------ run directories
    def workdir(self, run_id: str) -> pathlib.Path:
        return self.root / run_id

    def _prepare(self, planned) -> Job:
        """Write `input.scs` + `recipe.txt` for one planned run and wrap it in a Job."""
        run = planned.run
        wd = self.workdir(run.run_id)
        wd.mkdir(parents=True, exist_ok=True)
        for src in self.aux:
            if not src.exists():
                raise PmuError(
                    what=f"The auxiliary path {src} does not exist.",
                    why="It was listed as a file the run directory needs (typically the PDK tree "
                        "a relative `include` line points at), and a run copied without it "
                        "cannot resolve its models.",
                    do=["Fix the path, or make the netlist's include lines absolute."],
                    where=str(src))
            dst = wd / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copyfile(src, dst)
        (wd / NETLIST_NAME).write_text(planned.netlist_text, encoding="utf-8", newline="\n")
        (wd / RECIPE_NAME).write_text((run.recipe or "").rstrip("\n") + "\n",
                                      encoding="utf-8", newline="\n")
        job = Job(run=run, workdir=wd, netlist_text=planned.netlist_text, site=self.site)
        with self._lock:
            self._jobs[run.run_id] = job
        return job

    # ------------------------------------------------------------------ the main loop
    def run_all(self, *, resume: bool = True, on_event: EventFn | None = None,
                timeout_s: float | None = None, jobs: int | None = None) -> dict:
        """Submit every enabled planned run, poll to completion, import, update the ledger.

        `resume=True` skips runs whose ledger status already carries results (done / imported /
        skipped_cached) -- that is the hash cache, reported as `skipped_cached`.
        `on_event(kind, run_id, detail)` streams progress to the web shell's job poller; kinds
        are engine / start / submitted / running / done / failed / skipped_cached / dry_run /
        stored / note / finished.  ("stored" reports cells written into the dataset; it is NOT
        the ledger status `imported`, which belongs to external results only.)
        """
        emit = on_event or (lambda kind, run_id, detail: None)
        # A backend may carry its own defaults: Donau jobs get the short queue's wallclock as a
        # deadline (a job stuck PENDING must not block the queue forever) and run several at once.
        self.timeout_s = timeout_s or getattr(self.backend, "default_job_timeout_s", None)
        if timeout_s is not None and hasattr(self.backend, "timeout_s"):
            self.backend.timeout_s = timeout_s        # an explicit deadline also bounds each call
        width = max(1, int(jobs or (self.jobs if self._jobs_given else 0)
                           or getattr(self.backend, "default_jobs", 0) or 1))

        ok, why = self.backend.available()
        if not ok:
            emit("engine", "", f"{self.backend.name} is not usable: {why}")
        planned = [p for p in self.plan.runs(enabled_only=True) if p.run_id not in self._skipped]
        summary = {"engine": self.backend.name, "engine_note": why,
                   "planned": len(planned), "done": 0, "failed": 0, "skipped_cached": 0,
                   "dry_run": 0, "stored": 0, "runs": [], "started": time.time()}

        todo = []
        for p in planned:
            stored = self._get(p.run_id)
            if resume and stored is not None and stored.status in HAVE_RESULTS:
                self._status(p.run_id, "skipped_cached", error="")
                summary["skipped_cached"] += 1
                summary["runs"].append({"run_id": p.run_id, "status": "skipped_cached"})
                emit("skipped_cached", p.run_id,
                     f"already has results ({stored.status}); the content hash matched, so "
                     "nothing was re-simulated")
                continue
            todo.append(p)

        if width == 1 or len(todo) <= 1:
            for p in todo:
                self._tally(summary, self.run_one(p, on_event=emit))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=width) as pool:
                futures = {pool.submit(self.run_one, p, on_event=emit): p for p in todo}
                for fut in concurrent.futures.as_completed(futures):
                    self._tally(summary, fut.result())

        summary["elapsed_s"] = round(time.time() - summary.pop("started"), 3)
        summary["cpu_seconds"] = round(
            sum(r.get("cpu_seconds", 0.0) for r in summary["runs"]), 3)
        emit("finished", "", f"{summary['done']} done, {summary['failed']} failed, "
                             f"{summary['skipped_cached']} cached, {summary['dry_run']} dry-run, "
                             f"{summary['stored']} dataset cell(s) stored")
        return summary

    def _tally(self, summary: dict, run: Run) -> None:
        """Fold one finished run into the summary.  Called from the pool's completion loop, which
        is single-threaded, so `summary` needs no lock of its own."""
        stored = len((self.reports.get(run.run_id) or {}).get("filled", []))
        row = {"run_id": run.run_id, "status": run.status, "cpu_seconds": run.cpu_seconds,
               "peak_mem_mb": run.peak_mem_mb, "engine": run.engine, "job_id": run.job_id,
               "stored": stored}
        summary["runs"].append(row)
        summary["stored"] += stored
        if run.status in ("done", "imported"):
            summary["done"] += 1
        elif run.status == "failed":
            summary["failed"] += 1
        elif run.status == "skipped_cached":
            summary["skipped_cached"] += 1
        else:
            summary["dry_run"] += 1

    # ------------------------------------------------------------------ one run
    def run_one(self, planned, *, on_event: EventFn | None = None) -> Run:
        """Prepare, submit, poll, fetch, import and record ONE planned run."""
        emit = on_event or (lambda kind, run_id, detail: None)
        job = self._prepare(planned)
        run = job.run
        run_id = run.run_id
        run.engine = self.backend.name
        run.netlist_path = str(job.netlist_path)

        self._upsert(dataclasses.replace(run, status="planned"))
        if planned.feeds:
            self._consumes(run_id, planned.feeds)

        emit("start", run_id, f"{run.analysis} at {run.cell_text()} on {self.backend.name}")
        try:
            job.job_id = self.backend.submit(job) or ""
        except PmuError as exc:
            return self._fail(job, str(exc.what), detail=str(exc), emit=emit)
        run.job_id = job.job_id
        self._status(run_id, "submitted", error="", job_id=job.job_id, submitted=True)
        emit("submitted", run_id, f"job {job.job_id or '(none)'} on {self.backend.name}")

        state = self._await(job, emit)
        try:
            psf_dir = self.backend.fetch(job)
        except PmuError as exc:
            if state != "skipped":
                return self._fail(job, str(exc.what), detail=str(exc), emit=emit)
            psf_dir = job.psf_dir
        if state == "done" and getattr(job, "state", "") == "failed":
            # fetch() found the scheduler's "done" hollow (e.g. an empty PSF dir). Recording it
            # as done would make resume skip it forever and leave its cells silently missing.
            state = "failed"

        log = job.log_path or (job.workdir / LOG_NAME)
        cpu, mem = parse_spectre_log(
            log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "")

        if state == "skipped":
            # dry_run: nothing was submitted, so there is no result and no failure.  The ledger
            # status stays `planned` on purpose -- `skipped_cached` would claim results that do
            # not exist and would hide the run from the report's NOT RUN list.
            self._status(run_id, "planned", error=job.detail or "dry run: no simulator was "
                         "invoked; the netlist and recipe were written",
                         job_id=job.job_id, finished=True)
            emit("dry_run", run_id, job.detail or "netlist and recipe written, nothing submitted")
            return self._get(run_id) or run
        if state == "failed":
            tail = log_tail(log) or job.console
            return self._fail(job, job.detail or "the engine reported the job failed",
                              detail=tail, emit=emit, cpu=cpu, mem=mem)

        self._status(run_id, "done", error="", job_id=job.job_id,
                     psf_path=str(psf_dir), cpu_seconds=cpu, peak_mem_mb=mem, finished=True)
        emit("done", run_id, f"cpu {cpu:.3f} s, peak {mem:.0f} MB, psf {psf_dir}")

        if self._no_dataset:
            return self._get(run_id) or run
        from . import importer
        with self._lock:                       # the dataset owns buffers and one index.json
            ds = self._dataset_locked()
            report = importer.import_run(self.ledger.get(run_id) or run, psf_dir, ds,
                                         plan=self.plan, pmu_inst=self.pmu_inst)
            self.reports[run_id] = report
        # The ledger status stays `done`: pmukit ran this itself.  `imported` means the results
        # came from somewhere else and carries `source_path`; conflating them would let a
        # synthesized run be counted as "already covered by the user's own data".
        if report["filled"]:
            emit("stored", run_id, f"{len(report['filled'])} dataset cell(s): "
                                   f"{', '.join(report['filled'])}")
        else:
            emit("stored", run_id,
                 "no cell could be derived: " + ("; ".join(report["missing"]) or "(nothing)"))
        for note in report["notes"]:
            emit("note", run_id, note)
        return self._get(run_id) or run

    def _await(self, job: Job, emit: EventFn) -> str:
        """Poll one job to a terminal state.  Reports each transition exactly once."""
        last = ""
        deadline = None if not self.timeout_s else time.time() + float(self.timeout_s)
        while True:
            state = self.backend.poll(job)
            if state not in BACKEND_STATES:
                raise PmuError(
                    what=f"Backend {self.backend.name} reported the unknown state {state!r}.",
                    why="A backend may only answer with one of "
                        f"{', '.join(BACKEND_STATES)}; the runner's lifecycle is written "
                        "against exactly those.",
                    do=[f"Fix {self.backend.name}.poll() to return one of them."],
                    where=f"pmukit/backends/{self.backend.name}.py")
            if state != last:
                if state == "running":
                    self._status(job.run_id, "running", error="", job_id=job.job_id)
                    emit("running", job.run_id, job.detail or "the engine is simulating")
                last = state
            if state in ("done", "failed", "skipped"):
                return state
            if deadline is not None and time.time() > deadline:
                self.backend.kill(job)
                job.detail = (f"timed out after {self.timeout_s:.0f} s "
                              f"(the job was killed on {self.backend.name})")
                return "failed"
            time.sleep(float(getattr(self.backend, "poll_interval_s", 0.5)))

    def _fail(self, job: Job, what: str, *, detail: str, emit: EventFn,
              cpu: float = 0.0, mem: float = 0.0) -> Run:
        text = (what + ("\n" + detail if detail and detail != what else "")).strip()
        self._status(job.run_id, "failed", error=text[-4000:], job_id=job.job_id,
                     cpu_seconds=cpu, peak_mem_mb=mem, finished=True)
        emit("failed", job.run_id, what)
        if not self._no_dataset:
            from . import importer
            with self._lock:
                importer.mark_run_missing(job.run, self._dataset_locked(),
                                          f"run failed: {what}", plan=self.plan,
                                          netlist_text=job.netlist_text, pmu_inst=self.pmu_inst)
        return self._get(job.run_id) or job.run

    # ------------------------------------------------------------------ operator controls
    def retry(self, run_id: str) -> Run:
        """Re-run one run, clearing its stored failure first.  The results, if any, are re-imported."""
        planned = self._planned.get(run_id)
        if planned is None:
            raise PmuError(
                what=f"No planned run {run_id!r} to retry.",
                why="retry() re-submits a run of THIS plan; the id is not in it (a re-plan may "
                    "have changed the netlist, which changes the run id).",
                do=["Re-compile the plan and retry the new id.",
                    "`pmukit status` lists the ids this plan owns."],
                where=f"runner for {self.project}")
        self._skipped.discard(run_id)
        if self._get(run_id) is not None:
            self._status(run_id, "planned", error="")
        return self.run_one(planned)

    def skip(self, run_id: str) -> None:
        """Take one run out of this session's queue, with the reason recorded.

        The ledger status stays `planned`: nothing ran, so the run must keep showing up in the
        report's NOT RUN list.  It is the `error` column that says a human skipped it.
        """
        if run_id not in self._planned:
            raise PmuError(
                what=f"No planned run {run_id!r} to skip.",
                why="skip() removes a run of THIS plan from the queue; the id is not in it.",
                do=["Check the id against `pmukit status` / the Plan screen."],
                where=f"runner for {self.project}")
        self._skipped.add(run_id)
        if self._get(run_id) is not None:
            self._status(run_id, "planned",
                         error="skipped by the operator; nothing was submitted")

    def kill(self, run_id: str) -> None:
        """Cancel a running job.  Silent for a job that already finished."""
        job = self._jobs.get(run_id)
        if job is None:
            raise PmuError(
                what=f"No live job for run {run_id!r}.",
                why="kill() cancels a job this Runner submitted; nothing was submitted for this "
                    "id in this session.",
                do=["Kill it through the engine directly (dkill <job id> on the cluster).",
                    "Check the id against `pmukit status`."],
                where=f"runner for {self.project}")
        self.backend.kill(job)
        self._status(run_id, "failed", error="killed by the operator",
                     job_id=job.job_id, finished=True)
