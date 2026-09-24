"""The web shell: one `http.server`, one page, no CDN, no framework, no Qt.

    python -m pmukit.server                 # 127.0.0.1:8765, real projects under $PMUKIT_DATA
    python -m pmukit.server --demo --open   # synthetic PMU_DEMO data, opens a browser

Design rules this file obeys
----------------------------
* **Loopback by default.** The box has no authentication and no network; `--host` is an
  explicit opt-in for the VNC-desktop-vs-node case.
* **The page only talks through the route table** in `docs/OVERNIGHT_BRIEF.md`. Every route in
  that table is implemented here with exactly that path and method.
* **Every failure is the four-part error** -- `PmuError.to_dict()`, never a bare string.
* **Long work goes on a background thread** and answers `{"job": id}`; progress is polled from
  `GET /api/jobs/<id>` or from the ledger. Closing the page does not stop the work.
* **Drawing a curve never launches a simulator.** `GET /api/p/<n>/model/curve` returns the
  measurement and the model on the *same* frequency points, the model side from the fitter's
  analytic `predict()`.
* **Modules that are still landing** (`runner`, `importer`, `fit`, `emit`, `verify`) are imported
  *inside* the handler. A missing one answers `501` with a four-part error naming it, so the
  server always starts and the page always says what is missing instead of hanging.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import http.server
import json
import math
import os
import pathlib
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid

from . import helptext, jsonio, paths
from .errors import PmuError
from .state import SCREEN_INDEX, UiState, state_dir_projects

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_TRIES = 20
WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
PAGE = WEB_DIR / "index.html"

PROJECT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
STAMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUNID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SSH_HOST = os.environ.get("PMUKIT_SSH_HOST", "ewave-vm")
SSH_PROBE_TIMEOUT = 15.0
LOCAL_PROBE_TIMEOUT = 6.0
MACHINE_DEADLINE = 20.0

MAX_BODY = 64 << 20          # a netlist pasted into the page; PSF never comes through here


# ============================================================================== helpers
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(what: str, why: str, do, where: str = "") -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=where)


class NotLanded(Exception):
    """A module of the pipeline is not present yet. Answered as 501, never as a crash."""

    def __init__(self, module: str, what_for: str, detail: str = "") -> None:
        self.module = module
        self.error = _err(
            what=f"{what_for} is not available yet: this build has no `{module}`.",
            why=(f"`{module}` is part of the pipeline and is imported only when a route needs it; "
                 f"importing it failed{': ' + detail if detail else ''}. The server keeps running "
                 f"so every other screen stays usable."),
            do=[f"Install or pull the build that ships `{module}`",
                "Everything before this step (config, plan, ledger) is already saved and will be "
                "picked up as soon as the module is there"],
            where=module)
        Exception.__init__(self, self.error.what)


def _lazy(module: str, what_for: str):
    """Import a pipeline module on demand, or raise NotLanded (-> 501)."""
    import importlib
    try:
        return importlib.import_module(module)
    except Exception as exc:                       # ImportError, but also a broken half-landed file
        raise NotLanded(module, what_for, f"{type(exc).__name__}: {exc}") from None


def _attr(mod, name: str, what_for: str):
    fn = getattr(mod, name, None)
    if fn is None:
        raise NotLanded(f"{mod.__name__}.{name}", what_for, "the module is there, the entry point is not")
    return fn


def _clean(obj):
    """Make anything JSON-safe: NaN/Inf -> null, numpy -> list/float, Path/set -> str/list.

    JSON has no NaN and `JSON.parse` refuses a bare one -- and the ledger really does store NaN
    (the temperature-sweep run has no single temperature). Silently poisoning the page with a
    parse error would be the worst kind of failure, so it is normalized here, once.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_clean(v) for v in obj)
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    if hasattr(obj, "tolist"):                     # numpy array / scalar, without importing numpy
        return _clean(obj.tolist())
    if hasattr(obj, "to_dict"):
        return _clean(obj.to_dict())
    if isinstance(obj, complex):
        return {"re": _clean(obj.real), "im": _clean(obj.imag)}
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _eng(value, unit: str = "") -> str:
    """500e-6 -> '500 u'. The page shows currents the way a designer writes them."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if x == 0 or not math.isfinite(x):
        return f"0 {unit}".strip()
    sign = "-" if x < 0 else ""
    x = abs(x)
    for exp, suf in ((12, "T"), (9, "G"), (6, "M"), (3, "k"), (0, ""), (-3, "m"),
                     (-6, "u"), (-9, "n"), (-12, "p"), (-15, "f")):
        if x >= 10.0 ** exp or exp == -15:
            v = x / (10.0 ** exp)
            txt = f"{v:.4g}"
            return f"{sign}{txt} {suf}{unit}".strip()
    return f"{sign}{x:g} {unit}".strip()


# ============================================================================== jobs
class Job:
    """One background task. The page polls it; closing the page does not stop it."""

    def __init__(self, kind: str, project: str, title: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.project = project
        self.title = title
        self.status = "queued"            # queued | running | done | partial | failed
        self.progress = 0.0
        self.message = "waiting for a worker thread"
        self.events: list[dict] = []
        self.result = None
        self.error = None                 # the four-part dict
        self.not_landed = ""
        self.started_at = _now()
        self.finished_at = ""
        self._lock = threading.Lock()

    def say(self, message: str, progress: float | None = None) -> None:
        with self._lock:
            self.message = str(message)
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))
            self.events.append({"at": _now(), "text": str(message),
                                "progress": round(self.progress, 4)})
            del self.events[:-400]

    def to_dict(self, since: int = 0) -> dict:
        with self._lock:
            return _clean({
                "job": self.id, "kind": self.kind, "project": self.project, "title": self.title,
                "status": self.status, "progress": round(self.progress, 4),
                "message": self.message, "started_at": self.started_at,
                "finished_at": self.finished_at, "result": self.result, "error": self.error,
                "not_landed": self.not_landed,
                "events": self.events[since:], "n_events": len(self.events),
            })


class Jobs:
    """A tiny thread pool of one thread per job. There are never many."""

    def __init__(self, cap: int = 64) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.cap = cap

    def submit(self, kind: str, project: str, title: str, fn) -> Job:
        job = Job(kind, project, title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self.cap:
                done = self._order.pop(0)
                self._jobs.pop(done, None)

        def run() -> None:
            job.status = "running"
            job.say("started", 0.02)
            try:
                job.result = _clean(fn(job))
                if job.status == "running":
                    job.status = "partial" if job.error else "done"
                job.progress = 1.0
                job.say("finished" if job.status == "done" else "finished with reservations", 1.0)
            except NotLanded as nl:
                job.status = "failed"
                job.error = nl.error.to_dict()["error"]
                job.not_landed = nl.module
                job.say(nl.error.what, 1.0)
            except PmuError as pe:
                job.status = "failed"
                job.error = pe.to_dict()["error"]
                job.say(pe.what, 1.0)
            except Exception as exc:                                   # pragma: no cover - defence
                job.status = "failed"
                job.error = _err(f"{kind} crashed: {type(exc).__name__}: {exc}",
                                 "An unexpected exception escaped the worker; the traceback is in "
                                 "the job events.",
                                 ["Re-run the step", "Copy the traceback below to the desk"],
                                 f"job {job.id}").to_dict()["error"]
                job.say(traceback.format_exc()[-4000:], 1.0)
            finally:
                job.finished_at = _now()

        threading.Thread(target=run, name=f"pmukit-{kind}-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise _err(f"no job {job_id!r}.",
                       "Jobs live in the server process only; this id is unknown, or the server "
                       "was restarted since it was created.",
                       ["Start the step again -- nothing is lost, the ledger and the config are "
                        "on disk", "Reload the page"],
                       f"GET /api/jobs/{job_id}")
        return job

    def recent(self, project: str = "", limit: int = 20) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))
        out = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None or (project and job.project != project):
                continue
            d = job.to_dict()
            d.pop("events", None)
            out.append(d)
            if len(out) >= limit:
                break
        return out


JOBS = Jobs()


# ============================================================================== machine probes
def _run_cmd(argv, timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g} s"
    except FileNotFoundError:
        return 127, "", f"{argv[0]} is not on PATH"
    except OSError as exc:                                             # pragma: no cover - fs/env
        return 126, "", str(exc)


def _probe_site():
    from .site import SiteConfig
    try:
        return SiteConfig.load()
    except PmuError:
        return SiteConfig()


#: Engines that start no simulator: a probe of a simulator, queue, PDK or licence answers
#: "not needed" for them instead of reporting a machine it will never use as broken.
NO_SIM_ENGINES = {"fake": "synthesizes results analytically", "dry_run": "writes the decks, "
                  "runs nothing"}


def _not_needed(name: str, site, detail: str) -> dict:
    return {"name": name, "ok": True, "needed": False, "detail": detail, "ms": 0,
            "how": f"site engine {site.engine}", "reason": None}


def probe_engine(site=None) -> dict:
    """The simulator the SITE says to use.  On the box (donau_alps) that is the ALPS wrapper the
    environment points at -- or Spectre, if the site picked it; on the desk (spectre_ssh) it is
    Spectre on the VM, whose Cadence environment lives only in `~/.cshrc`, so the remote command
    must be a tcsh that sources it.  `fake` and `dry_run` start no simulator: nothing to probe."""
    site = site if site is not None else _probe_site()
    if site.engine in NO_SIM_ENGINES:
        return _not_needed("engine", site, f"{site.engine}: {NO_SIM_ENGINES[site.engine]} -- "
                                           f"no simulator is started")
    if site.engine == "donau_alps":
        return _probe_local_simulator(site)
    host = str(getattr(site, "ssh_host", "") or SSH_HOST)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
           'tcsh -c "source ~/.cshrc; which spectre"']
    t0 = time.time()
    rc, out, errtext = _run_cmd(cmd, SSH_PROBE_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    if rc == 0 and out and "not found" not in out.lower():
        return {"name": "engine", "ok": True,
                "detail": f"{out.splitlines()[-1].strip()}  (on {host})",
                "ms": ms, "how": " ".join(cmd), "reason": None}
    why = errtext or out or f"exit code {rc}"
    return {"name": "engine", "ok": False, "detail": "", "ms": ms, "how": " ".join(cmd),
            "reason": _err(f"no simulator answered on {host}.",
                           f"`ssh {host} 'tcsh -c \"source ~/.cshrc; which spectre\"'` said: "
                           f"{why}. The Cadence environment is only in ~/.cshrc, so a plain bash "
                           f"login finds nothing.",
                           [f"Check that {host} is up and that BatchMode ssh works",
                            "Or set the site engine to `fake` / `dry_run` to plan without "
                            "simulating"],
                           "pmukit/site.py: engine").to_dict()["error"]}


def _probe_local_simulator(site) -> dict:
    from . import sitenv
    from .backends.donau_alps import alps_exe
    t0 = time.time()
    sim = sitenv.simulator(site)
    if sim.value == "alps":
        root = sitenv.alps_root()
        exe = alps_exe(root.value) if root.ok else ""
        ok = bool(exe) and pathlib.Path(exe).is_file()
        ms = int((time.time() - t0) * 1000)
        if ok:
            return {"name": "engine", "ok": True, "detail": f"alps {exe}  (from {root.source})",
                    "ms": ms, "how": root.source, "reason": None}
        why = (f"{exe} does not exist (root from {root.source})" if exe else
               "none of PMUKIT_ALPS_ROOT, ALPS_ROOT, ALPS_HOME is set and `alps` is not on PATH")
        return {"name": "engine", "ok": False, "detail": exe, "ms": ms, "how": "$ALPS_ROOT",
                "reason": _err("the ALPS wrapper was not found.", why + ".",
                               ["Source the site's ALPS setup before `pmukit ui` (it exports "
                                "ALPS_ROOT)",
                                "Or setenv PMUKIT_ALPS_ROOT <the directory holding bin/alps>"],
                               "environment: ALPS_ROOT").to_dict()["error"]}
    exe = shutil.which("spectre") or ""
    ms = int((time.time() - t0) * 1000)
    if exe:
        return {"name": "engine", "ok": True, "detail": f"spectre {exe}", "ms": ms,
                "how": "which spectre", "reason": None}
    return {"name": "engine", "ok": False, "detail": "", "ms": ms, "how": "which spectre",
            "reason": _err("the site simulator is Spectre, but `spectre` is not on PATH.",
                           "Donau runs the payload with this shell's environment (-x all).",
                           ["Source the Cadence setup before `pmukit ui`",
                            "Or switch back to ALPS: pmukit site --simulator alps"],
                           "site simulator").to_dict()["error"]}


def probe_queue(site=None) -> dict:
    """Is the Donau scheduler answering, and does it know the site's queue?

    `dqueue` (queue list, ~ LSF bqueues) is the probe: it has to reach the scheduler to print
    anything, and its listing says whether `site.queue` exists.  NOT `dsub --version` -- Donau's
    parser has no such flag ("invalid option or param. Unexpected argument \"version\""), which
    made a healthy box report the queue as down.  `dversion` is the fallback when `dqueue` is
    not on PATH.  Neither submits anything.  Only the donau_alps engine submits to a queue.
    """
    if site is not None and site.engine in NO_SIM_ENGINES:
        return _not_needed("queue", site, f"not needed: {site.engine} submits nothing")
    if site is not None and site.engine == "spectre_ssh":
        return _not_needed("queue", site, f"not needed: spectre_ssh runs straight on "
                                          f"{site.ssh_host}, no batch queue")
    from .site import SiteConfig
    t0 = time.time()
    if not shutil.which("dsub"):
        ms = int((time.time() - t0) * 1000)
        return {"name": "queue", "ok": False, "detail": "", "ms": ms, "how": "which dsub",
                "reason": _err("there is no batch queue on this machine.",
                               "`dsub` is not on PATH; this is a desk, not a submit host.",
                               ["Runs go straight to the engine instead of a queue -- that is "
                                "normal on the desk",
                                "On the box, log in to a submit host"],
                               "PATH").to_dict()["error"]}
    try:
        want = (site if site is not None else SiteConfig.load()).queue.strip()
    except PmuError:
        want = ""
    tool = shutil.which("dqueue") or shutil.which("dversion")
    how = pathlib.Path(tool).name if tool else "dqueue"
    if not tool:
        ms = int((time.time() - t0) * 1000)
        return {"name": "queue", "ok": True, "ms": ms, "how": "which dsub", "reason": None,
                "detail": "dsub is on PATH (no dqueue/dversion to ask the scheduler)"}
    rc, out, errtext = _run_cmd([tool], LOCAL_PROBE_TIMEOUT)
    ms = int((time.time() - t0) * 1000)
    if rc != 0:
        return {"name": "queue", "ok": False, "detail": "", "ms": ms, "how": how,
                "reason": _err("the Donau scheduler did not answer.",
                               f"`{how}` exited {rc}: {errtext or out or 'no output'}.",
                               ["Retry when the scheduler is back -- the ledger resumes exactly "
                                "where it stopped",
                                "Or copy the netlists and submit them by hand"],
                               how).to_dict()["error"]}
    first = next((ln.strip() for ln in (out or "").splitlines() if ln.strip()), how)
    if how == "dqueue" and want:
        listed = re.search(rf"(^|\s){re.escape(want)}(\s|$)", out or "", re.MULTILINE)
        # dqueue's layout has not been seen on the box yet, so a miss is a NOTE, not a failure:
        # a false "queue down" is exactly the alarm this probe just stopped raising.
        detail = (f"Donau answers; queue '{want}' listed" if listed else
                  f"Donau answers; '{want}' not found in the dqueue listing -- check `dqueue`")
        return {"name": "queue", "ok": True, "detail": detail, "ms": ms, "how": how,
                "reason": None}
    return {"name": "queue", "ok": True, "detail": first[:120], "ms": ms, "how": how,
            "reason": None}


def probe_pdk(site=None) -> dict:
    if site is not None and site.engine in NO_SIM_ENGINES:
        return _not_needed("pdk", site, f"not needed: {site.engine} simulates nothing")
    t0 = time.time()
    from . import sitenv
    raw = sitenv.pdk_root().value
    ms = int((time.time() - t0) * 1000)
    if raw and pathlib.Path(raw).expanduser().is_dir():
        return {"name": "pdk", "ok": True, "detail": raw, "ms": ms, "how": "$PDK", "reason": None}
    if site is not None and site.engine == "spectre_ssh":
        # the VM resolves the model includes itself; here the PDK only lets the planner
        # double-check that a corner's section exists
        return _not_needed("pdk", site, f"not set here -- {site.ssh_host} resolves the model "
                                        f"includes; set $PDK only to check section names locally")
    return {"name": "pdk", "ok": False, "detail": raw, "ms": ms, "how": "$PDK",
            "reason": _err("the PDK directory is not set.",
                           f"$PDK is {raw!r}, which is not a directory; the corner rewriter needs "
                           f"it to resolve the include lines of your netlist.",
                           ["Set $PDK to the model directory before starting `pmukit ui`",
                            "A netlist whose includes are absolute paths works without it"],
                           "environment: PDK").to_dict()["error"]}


def probe_license(site=None) -> dict:
    from . import sitenv
    t0 = time.time()
    site = site if site is not None else _probe_site()
    if site.engine in NO_SIM_ENGINES:
        return _not_needed("license", site, f"not needed: {site.engine} checks out no license")
    if site.engine == "spectre_ssh":
        return _not_needed("license", site, f"not needed here: Spectre checks out its license "
                                            f"on {site.ssh_host}, from its ~/.cshrc")
    lic =sitenv.license_(sim=sitenv.simulator(site).value)     # Empyrean for ALPS, CDS for Spectre
    if lic.ok:
        return {"name": "license", "ok": True, "detail": f"{lic.source[1:]}={lic.value}",
                "ms": int((time.time() - t0) * 1000), "how": lic.source, "reason": None}
    return {"name": "license", "ok": False, "detail": "",
            "ms": int((time.time() - t0) * 1000), "how": "$CDS_LIC_FILE / $LM_LICENSE_FILE",
            "reason": _err("no license server is configured in this environment.",
                           "None of CDS_LIC_FILE, LM_LICENSE_FILE or EMPYREAN_LICENSE_FILE is "
                           "set, so a simulation would fail at check-out rather than at start.",
                           ["Source the site environment (~/.cshrc on the VM) before `pmukit ui`",
                            "Planning, fitting and delivering need no license"],
                           "environment: license").to_dict()["error"]}


def machine(deadline: float = MACHINE_DEADLINE) -> dict:
    """Probe engine / queue / PDK / license, each with its own timeout, all in parallel.

    A probe that fails carries a four-part reason, never a bare false; a probe that overruns the
    deadline is reported as overrunning rather than allowed to hang the page.
    """
    probes = {"engine": probe_engine, "queue": probe_queue, "pdk": probe_pdk,
              "license": probe_license}
    out: dict[str, dict] = {}
    threads = []
    # Every probe follows the SITE engine: fake / dry_run need no simulator, queue, PDK or
    # licence, spectre_ssh asks the VM over ssh, donau_alps reads this box's ALPS / Donau facts.
    site = _probe_site()

    def go(name, fn):
        try:
            out[name] = fn(site)
        except Exception as exc:                                       # pragma: no cover - defence
            out[name] = {"name": name, "ok": False, "detail": "", "ms": 0, "how": fn.__name__,
                         "reason": _err(f"the {name} probe crashed: {type(exc).__name__}: {exc}",
                                        "The probe raised instead of answering.",
                                        ["Reload Home to probe again"],
                                        f"pmukit.server.{fn.__name__}").to_dict()["error"]}

    for name, fn in probes.items():
        t = threading.Thread(target=go, args=(name, fn), daemon=True)
        t.start()
        threads.append((name, t))
    end = time.time() + deadline
    for name, t in threads:
        t.join(max(0.0, end - time.time()))
    for name, t in threads:
        if name not in out:
            out[name] = {"name": name, "ok": False, "detail": "", "ms": int(deadline * 1000),
                         "how": "probe", "reason": _err(
                             f"the {name} probe is still running after {deadline:g} s.",
                             "The page never waits for a probe: whatever is behind it is not "
                             "answering within its own timeout.",
                             ["Reload Home to probe again",
                              "Work that does not need it (plan, fit, deliver) is unaffected"],
                             f"pmukit.server.machine(deadline={deadline:g})").to_dict()["error"]}
    ready = all(out[n]["ok"] for n in ("engine",))
    return {"checked_at": _now(), "ready": ready, "probes": [out[n] for n in probes],
            "data_root": str(paths.data_root()),
            "pmukit": _version(), "python": sys.version.split()[0],
            "host": socket.gethostname(), "engine": site.engine}


def _version() -> str:
    from . import __version__
    return __version__


# ============================================================================== command echo
def cli_echo(screen: str, st, project: str = "") -> str:
    """The `$ pmukit ...` strip: what the user just did, as a command they can paste.

    `st` is whatever the screen knows about itself (a small dict). Nothing here reads the disk:
    the strip must be instant and must never be a second source of truth.
    """
    st = st if isinstance(st, dict) else {}
    p = project or str(st.get("project") or "<project>")
    scr = (screen or "").strip().lower()

    def join(seq, sep=","):
        return sep.join(str(x) for x in seq if str(x) != "")

    if scr == "home":
        sel = st.get("selected")
        return f"pmukit list && pmukit open {sel}" if sel else "pmukit list"
    if scr == "new":
        if not st.get("netlist"):
            return f"pmukit new {p} --netlist <input.scs>"
        bits = [f"pmukit new {p}", f"--netlist {st['netlist']}"]
        if st.get("pmu_inst"):
            bits.append(f"--pmu-inst {st['pmu_inst']}")
        if st.get("corners"):
            bits.append("--corners " + join(st["corners"]))
        if st.get("temps"):
            bits.append("--temps " + join(st["temps"]))
        if st.get("vset"):
            bits.append("--vset " + join(st["vset"]))
        for rail, ld in sorted((st.get("loads") or {}).items()):
            on = _eng(ld.get("on_a"), "A").replace(" ", "")
            off = _eng(ld.get("off_a"), "A").replace(" ", "")
            sw = "/switch" if ld.get("switches") else ""
            bits.append(f"--load {rail}={on}/{off}{sw}")
        if st.get("stubs"):
            bits.append("--stub " + join(st["stubs"]))
        if st.get("ignored"):
            bits.append("--ignore " + join(st["ignored"]))
        if st.get("fmax"):
            bits.append("--fmax " + _eng(st["fmax"], "Hz").replace(" ", ""))
        return " ".join(bits)
    if scr == "plan":
        off = [g for g, on in sorted((st.get("ticks") or {}).items()) if not on]
        cmd = f"pmukit plan {p}"
        if off:
            cmd += " --skip " + join(off)
        return cmd + f" && pmukit run {p}" + (f" --engine {st['engine']}" if st.get("engine") else "")
    if scr == "run":
        if st.get("run") and st.get("action"):
            return f"pmukit run {p} --{st['action']} {st['run']}"
        only = st.get("filter")
        return f"pmukit status {p} --watch" + (f" --only {only}" if only and only != "all" else "")
    if scr == "model":
        bits = [f"pmukit report {p}"]
        if st.get("port"):
            bits.append(f"--port {st['port']}")
        if st.get("cell"):
            bits.append(f"--cell {st['cell']}")
        if st.get("block"):
            bits.append(f"--block {st['block']}")
        return " ".join(bits)
    if scr == "deliver":
        if st.get("file") == "report.md":
            return f"pmukit report {p}"
        cmd = f"pmukit deliver {p}"
        if st.get("file"):
            cmd += f" && pmukit show {p} {st['file']}"
        return cmd
    if scr == "digest":
        bits = [f"pmukit digest {p}"]
        if st.get("budget"):
            bits.append(f"--budget {int(st['budget'])}")
        if st.get("blocks"):
            bits.append("--blocks " + join(st["blocks"]))
        return " ".join(bits) + f" > digest_{p}.txt"
    if scr == "states":
        return "pmukit help states"
    if scr == "settings":
        bits = ["pmukit site"]
        for key, flag in (("engine", "--engine"), ("simulator", "--simulator"),
                          ("queue", "--queue"), ("cpus", "--cpus"), ("account", "--account")):
            if st.get(key) not in (None, ""):
                bits.append(f"{flag} {st[key]}")
        return " ".join(bits)
    return f"pmukit help {scr or 'home'}"


# ============================================================================== project facade
_PLAN_CACHE: dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


class Project:
    """Everything a route needs about one project, assembled from the landed contract modules.

    Nothing is stored here that already lives on disk: the config, the netlist, the ledger, the
    dataset and the deliverables are the truth. This is only the glue that the routes share.
    """

    def __init__(self, name: str, root=None) -> None:
        if not PROJECT_RE.match(name or ""):
            raise _err(f"{name!r} is not a usable project name.",
                       "A project is a directory under $PMUKIT_DATA, so the name has to be a safe "
                       "directory name: letters, digits, _ . - and no separators.",
                       ["Use a name like `demo_pmu`"], "POST /api/projects")
        self.name = name
        self._root = root
        self.dir = (pathlib.Path(root) if root is not None else paths.data_root()) / name

    # ---- paths
    @property
    def config_path(self) -> pathlib.Path:
        return self.dir / "config.json"

    @property
    def derived_path(self) -> pathlib.Path:
        return self.dir / "derived.json"

    @property
    def netlist_dir(self) -> pathlib.Path:
        return self.dir / "netlists"

    @property
    def dataset_path(self) -> pathlib.Path:
        return self.dir / "dataset"

    @property
    def fit_path(self) -> pathlib.Path:
        return self.dir / "fit.json"

    @property
    def verify_path(self) -> pathlib.Path:
        return self.dir / "verify.json"

    def ensure(self) -> "Project":
        for sub in ("", "netlists", "deliver", "digest", "logs", "dataset"):   # runs: runs_dir()
            (self.dir / sub if sub else self.dir).mkdir(parents=True, exist_ok=True)
        return self

    # ---- ui state
    def state(self) -> UiState:
        return UiState.load(self.name, self._root)

    # ---- config
    def config(self):
        from .config import ProjectConfig
        return ProjectConfig.load(self.config_path)

    def config_or_none(self):
        try:
            return self.config()
        except PmuError:
            return None

    def save_config(self, cfg, note: str = "") -> None:
        st = self.state()
        hist = st.history()
        if hist.current() is None:
            existing = self.config_or_none()
            if existing is not None:
                hist.push(existing, "before this change")
        cfg.save(self.config_path)
        hist.push(cfg, note)
        st.record_config_change(note)
        st.save()

    # ---- netlist + pins
    def netlist_path(self) -> pathlib.Path:
        st = self.state()
        if st.netlist and pathlib.Path(st.netlist).is_file():
            return pathlib.Path(st.netlist)
        cfg = self.config_or_none()
        if cfg is not None:
            p = pathlib.Path(cfg.netlist)
            if not p.is_absolute():
                cand = self.dir / cfg.netlist
                if cand.is_file():
                    return cand
            if p.is_file():
                return p
        local = self.netlist_dir / "input.scs"
        if local.is_file():
            return local
        raise _err(f"{self.name} has no netlist yet.",
                   "Every screen below New reads the pin roles out of one Spectre netlist; none "
                   "has been loaded for this project.",
                   ["Drop an input.scs on the New screen",
                    "Or type its path on this machine in the box on the New screen"],
                   str(self.netlist_dir / "input.scs"))

    @property
    def source_meta_path(self) -> pathlib.Path:
        return self.netlist_dir / "source.json"

    def netlist_source(self) -> dict | None:
        """Where the project's netlist copy came from: {path, typed, name, via, sha, bytes,
        loaded_at, changes}. None for a project loaded before this was recorded."""
        try:
            d = jsonio.read(self.source_meta_path)
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def save_netlist_source(self, meta: dict) -> None:
        jsonio.write(self.source_meta_path, meta)

    def netlist(self):
        from .netlist import Netlist
        nl = Netlist.from_file(self.netlist_path())
        nl.origin = str((self.netlist_source() or {}).get("path") or "")
        return nl

    def pins(self, nl=None):
        nl = nl if nl is not None else self.netlist()
        cfg = self.config_or_none()
        inst = cfg.pmu_inst if cfg is not None else guess_pmu_inst(nl)
        ports = dict(cfg.ports) if cfg is not None else {}
        if nl.find_instance(inst) is None:
            raise _inst_gone(nl, inst)
        return nl.scan(inst, ports=ports or None)

    # ---- derived + plan
    def site(self, engine: str = "", account: str = ""):
        """The install's site config, with optional per-submit engine / account overrides.

        The engine is an install property, not a project one -- but the Plan screen has to be
        able to say "this once, with the fake backend" without editing site.json under the user.
        """
        from .site import ENGINES, SiteConfig
        try:
            cfg = SiteConfig.load(site_path(self._root))
        except PmuError:
            cfg = SiteConfig()
        if account:
            cfg.project_account = str(account).strip()
        if engine:
            if engine not in ENGINES:
                raise _err(f"{engine!r} is not a backend this build knows.",
                           "The runner has one interface and these backends behind it.",
                           [f"Use one of: {', '.join(ENGINES)}"], "POST submit {engine}")
            cfg.engine = engine
        return cfg

    def derived(self):
        from .config import derive
        cfg = self.config()
        try:
            pins = self.pins()
        except PmuError:
            pins = None
        d = derive(cfg, pins, self.site())
        try:
            d.save(self.derived_path)
        except OSError:                                                # pragma: no cover - fs
            pass
        return d

    def plan(self, *, apply_ticks: bool = True):
        from .plan import compile_plan, measured_cost
        cfg = self.config()
        der = self.derived()
        key = (cfg.sha(), der.sha(), jsonio.sha_file(self.netlist_path(), 12))
        with _CACHE_LOCK:
            hit = _PLAN_CACHE.get(self.name)
        if hit and hit[0] == key:
            plan = hit[1]
        else:
            nl = self.netlist()
            pins = self.pins(nl)
            try:
                with self.ledger() as led:
                    cost = measured_cost(led)
            except Exception:                                          # pragma: no cover - no db
                cost = None
            plan = compile_plan(cfg, der, nl, pins, site=self.site(),
                                **({"cost": cost} if cost else {}))
            with _CACHE_LOCK:
                _PLAN_CACHE[self.name] = (key, plan)
        if apply_ticks:
            ticks = self.state().plan_ticks
            for g in plan.groups:
                g.enabled = bool(ticks.get(g.id, True))
        return plan

    # ---- ledger + dataset
    def ledger(self):
        from .ledger import Ledger
        self.ensure()
        return Ledger(self.dir / "runs.sqlite")

    def dataset(self, create: bool = False):
        from .dataset import Dataset
        if (self.dataset_path / "index.json").is_file():
            return Dataset.open(self.dataset_path)
        if not create:
            raise _err(f"{self.name} has no dataset yet.",
                       "The dataset is written by the runner as results come back; nothing has "
                       "been run or imported for this project.",
                       ["Submit the plan on the Plan screen",
                        "Or import existing ADE result directories on the New screen"],
                       str(self.dataset_path))
        # The dimensions come from the PLAN, not from a second guess here: importer.open_or_create
        # is the same call the runner makes, so a dataset created from the web shell and one
        # created by `pmukit run` are byte-identical.
        importer = _lazy("pmukit.importer", "Creating the dataset")
        return _attr(importer, "open_or_create", "Creating the dataset")(
            self.dataset_path, self.plan(apply_ticks=False),
            project=self.name, config_sha=self.config().sha())

    # ---- fitted parameters
    def fit_result(self) -> dict | None:
        if not self.fit_path.is_file():
            return None
        try:
            return jsonio.read(self.fit_path)
        except (OSError, ValueError):
            return None

    def verify_result(self) -> dict | None:
        if not self.verify_path.is_file():
            return None
        try:
            return jsonio.read(self.verify_path)
        except (OSError, ValueError):
            return None

    def verify_stale(self) -> bool:
        """True when fit.json was rewritten AFTER verify.json: its grades judge a fit that no
        longer exists, and showing them as the verdict on the new one would be a lie."""
        try:
            return self.fit_path.stat().st_mtime > self.verify_path.stat().st_mtime
        except OSError:
            return False

    def verify_current(self) -> tuple[dict, str]:
        """(verify.json, "") -- or ({}, why) when there is none or it predates the fit."""
        ver = self.verify_result()
        if not ver:
            return {}, ""
        if self.verify_stale():
            return {}, ("the fit was re-run after the last verify, so its grades judge a model "
                        "that no longer exists; re-run verify to grade this one")
        return ver, ""


#: Spectre primitives: a top-level instance of one is a bench source, probe or passive, never
#: the PMU, so it is not offered as a candidate.
_PRIMITIVES = frozenset({
    "isource", "vsource", "iprobe", "resistor", "capacitor", "inductor", "mutual_inductor",
    "vcvs", "vccs", "ccvs", "cccs", "pvcvs", "pvccs", "pccvs", "pcccs", "bsource", "diode",
    "switch", "relay", "port", "tline", "nport", "delay", "winding", "core"})


def pmu_candidates(nl) -> list[dict]:
    """Top-level instances that could be the PMU, best first -- the ranking `guess_pmu_inst` uses.

    A master defined as a subckt (in the deck, or in a plain include it can read) ranks above
    one that is not; then the most pins; then file order. The first is marked `guess` only when
    its subckt is defined -- an undefined master is offered, never picked.
    """
    out = []
    for order, (name, nodes, master, _rest) in enumerate(nl.instances(0)):
        if master in _PRIMITIVES:
            continue
        out.append({"name": name, "master": master, "pins": len(nodes),
                    "defined": nl.subckt_home(master) is not None, "order": order})
    out.sort(key=lambda c: (not c["defined"], -c["pins"], c["order"]))
    for i, c in enumerate(out):
        del c["order"]
        c["guess"] = i == 0 and c["defined"]
    return out


def guess_pmu_inst(nl) -> str:
    """The instance with the most pins whose master is a subckt defined in the file.

    The convention sources (IL_/VB_/VS_/VEN_) are two-node primitives, so the PMU instance is
    unambiguous in every deck that follows the convention. When it is not, the user picks from
    the candidates the error carries -- we report, we do not guess twice.
    """
    cands = pmu_candidates(nl)
    if cands and cands[0]["guess"]:
        return cands[0]["name"]
    raise PmuError(
        what="could not tell which instance is the PMU.",
        why="The PMU is the top-level instance whose master is a subckt defined in the netlist "
            "(or in an include pmukit can read); this deck has "
            + (f"{len(cands)} other top-level instance(s), none of them defined."
               if cands else "no top-level instance of a subckt at all."),
        do=["Pick the PMU instance from the list here" if cands else
            "Check that the testbench instantiates the PMU at the top level",
            "Or re-export the netlist with the PMU subckt in it"],
        where=f"{nl.path or 'netlist'}: top-level instances",
        extra={"candidates": cands})


def _inst_gone(nl, inst: str) -> PmuError:
    """The configured PMU instance is not in this netlist: offer the ones that are."""
    cands = pmu_candidates(nl)
    return PmuError(
        what=f"there is no top-level instance named {inst!r} in the netlist.",
        why="The PMU instance is named in the project config (or was just picked); the netlist "
            "read last has no top-level instance by that name -- renamed in the bench, or "
            "another deck.",
        do=["Pick the PMU instance from the list here" if cands else
            "Check that the testbench instantiates the PMU at the top level",
            f"Or rename the instance back to {inst} in the bench and re-read the netlist"],
        where=f"{nl.path or 'netlist'}: top-level instances",
        extra={"candidates": cands})


# ============================================================================== demo data
DEMO_PROJECT = "demo_pmu"
DEMO_PORTS = ["VDD0P8_A", "VDD0P8_B", "IB_PTAT", "IB_POLY"]
DEMO_CORNERS = ["tt", "ss", "ff"]
DEMO_TEMPS = [-40, 25, 125]
DEMO_GRADE = {"VDD0P8_B|ss|125": "not_run", "VDD0P8_B|ss|25": "yellow",
              "VDD0P8_B|ff|-40": "yellow", "IB_POLY|ff|-40": "yellow",
              "VDD0P8_A|ss|125": "yellow"}


def _demo_pins() -> dict:
    rows = [
        ("VDDA_1V0", "VDDA_1V0", "supply", "VS_VDDA_1V0", 1.0, "ignore"),
        ("VDD0P8_A", "VDD0P8_A", "rail", "IL_VDD0P8_A", 5e-4, "model"),
        ("VDD0P8_B", "VDD0P8_B", "rail", "IL_VDD0P8_B", 2e-3, "model"),
        ("VDD0P8_C", "VDD0P8_C", "rail", "IL_VDD0P8_C", 1e-4, "stub"),
        ("IB_PTAT", "ib_ptat", "bias", "VB_IB_PTAT", 0.4, "model"),
        ("IB_POLY", "ib_poly", "bias", "VB_IB_POLY", 0.4, "model"),
        ("EN", "en", "en", "VEN_EN", 1.0, "model"),
        ("TESTMODE", "testmode", "none", None, None, "ignore"),
        ("VSS_A", "VSS_A", "none", None, None, "ignore"),
        ("VSS_B", "VSS_B", "none", None, None, "ignore"),
        ("AGND", "AGND", "none", None, None, "ignore"),
    ]
    pins = []
    for i, (name, net, role, src, dc, fate) in enumerate(rows):
        ground = name in ("VSS_A", "VSS_B", "AGND")
        pins.append({"name": name, "net": net, "index": i, "role": role, "src": src,
                     "src_master": ("isource" if role == "rail" else
                                    "vsource" if src else None),
                     "dc": dc, "gnd": ("VSS_A" if name == "VDD0P8_A" else
                                       "VSS_B" if name == "VDD0P8_B" else
                                       "AGND" if role in ("bias", "en") else None),
                     "gnd_from": "wiring" if not ground else "", "is_ground": ground,
                     "fate": "ignore" if ground else fate,
                     "reason": ("no IL_/VB_/VS_/VEN_ source drives this pin"
                                if name == "TESTMODE" else "")})
    return {"pmu_inst": "PMU_TOP", "pmu_master": "pmu_demo", "pins": pins,
            "candidates": _demo_netlist_info()["candidates"],
            "sections": {"pdk/toplevel.scs": "tt", "pdk/rc.scs": "typ"},
            "params": {"VSET": "3"},
            "analyses": ["dcOp dc", "ac1 ac start=1 stop=1G dec=10"],
            "notes": ["three grounds read from the wiring, not from a prefix"],
            "summary": {"rails": 2, "biases": 2, "stubs": 1, "grounds": 3,
                        "unclassified": ["TESTMODE"]},
            "netlist": {"path": "tb/input.scs", "sha": "9c1e4bb7", "bytes": 41984}}


def _demo_netlist_info() -> dict:
    return {"source": {"path": "/work/pmu_tb/spectre/schematic/netlist/input.scs",
                       "typed": "$WORK_ROOT/pmu_tb/spectre/schematic/netlist/input.scs",
                       "name": "input.scs", "via": "path", "sha": "9c1e4bb7", "bytes": 41984,
                       "loaded_at": "2026-09-15T14:02:11Z", "on_disk": "same",
                       "changes": {"first": False, "unchanged": True, "pmu_inst": "PMU_TOP",
                                   "text": "input.scs is unchanged since the last read "
                                           "(byte-identical); every answer is kept"}},
            "copy": {"path": "tb/input.scs", "sha": "9c1e4bb7", "bytes": 41984},
            "pmu_inst": "PMU_TOP", "cwd": "/work",
            "candidates": [{"name": "PMU_TOP", "master": "pmu_demo", "pins": 11,
                            "defined": True, "guess": True},
                           {"name": "XREF", "master": "bgr_ref", "pins": 3,
                            "defined": True, "guess": False}]}


def _demo_groups() -> list[dict]:
    raw = [
        ("dc_load:IL_VDD0P8_A", "DC load sweep -- VDD0P8_A", "dc_load", 18, 3.1,
         "rail voltage vs load: load regulation, dropout and the current limit",
         ["VDD0P8_A"], ["dc_load"]),
        ("dc_load:IL_VDD0P8_B", "DC load sweep -- VDD0P8_B", "dc_load", 18, 3.1,
         "same for rail B", ["VDD0P8_B"], ["dc_load"]),
        ("dc_temp", "DC temperature sweep", "dc_temp", 3, 19.4,
         "one sweep per corner makes temperature continuous inside the .va",
         DEMO_PORTS, ["dc_temp"]),
        ("dc_iv:VB_IB_PTAT", "Bias I-V -- IB_PTAT", "dc_iv", 9, 1.6,
         "bias current vs pin voltage: the PTAT slope and the compliance knee",
         ["IB_PTAT"], ["dc_iv"]),
        ("dc_iv:VB_IB_POLY", "Bias I-V -- IB_POLY", "dc_iv", 9, 1.6,
         "same for the flat bias", ["IB_POLY"], ["dc_iv"]),
        ("ac:IL_VDD0P8_A", "AC -- inject IL_VDD0P8_A", "ac", 36, 19.4,
         "output impedance of rail A: your ripple current times Zout is rail ripple",
         ["VDD0P8_A"], ["ac_zout"]),
        ("ac:IL_VDD0P8_B", "AC -- inject IL_VDD0P8_B", "ac", 36, 19.4,
         "output impedance of rail B", ["VDD0P8_B"], ["ac_zout"]),
        ("ac:VS_VDDA_1V0", "AC -- inject VS_VDDA_1V0", "ac", 36, 1.8,
         "one supply injection is read at every output at once (AC superposition)",
         DEMO_PORTS, ["ac_psrr", "ac_yout"]),
        ("noise:VDD0P8_A", "Noise -- VDD0P8_A", "noise", 36, 20.2,
         "rail voltage noise; supply pushing turns it into phase noise",
         ["VDD0P8_A"], ["noise_v"]),
        ("noise:VDD0P8_B", "Noise -- VDD0P8_B", "noise", 36, 20.2,
         "same for rail B", ["VDD0P8_B"], ["noise_v"]),
        ("noise:IB_PTAT", "Noise -- IB_PTAT", "noise", 9, 5.1,
         "bias current noise up-converts into VCO phase noise",
         ["IB_PTAT"], ["noise_i"]),
        ("noise:IB_POLY", "Noise -- IB_POLY", "noise", 9, 5.1,
         "same for the flat bias", ["IB_POLY"], ["noise_i"]),
        ("tran_load:IL_VDD0P8_A", "Load-EN transient -- VDD0P8_A", "tran_load", 18, 30.4,
         "your block switching on: how deep the rail dips and how it overshoots",
         ["VDD0P8_A"], ["tran_load"]),
        ("tran_load:IL_VDD0P8_B", "Load-EN transient -- VDD0P8_B", "tran_load", 18, 30.4,
         "same for rail B", ["VDD0P8_B"], ["tran_load"]),
        ("tran_en:VEN_EN", "EN power-up transient", "tran_en", 9, 8.1,
         "rails and biases come up with the measured rise time",
         DEMO_PORTS, ["tran_en"]),
    ]
    return [{"id": i, "title": t, "analysis": a, "runs": n, "cpu_seconds": h * 3600,
             "cpu_hours": h, "why": w, "ports": p, "observables": o, "enabled": True,
             "cached": 1 if a == "dc_load" else 0}
            for i, t, a, n, h, w, p, o in raw]


def _demo_ledger_rows() -> list[dict]:
    rows = [
        ("7c3e91a04bd2", "tt", 25.0, "noise", "VDD0P8_A", "done", 252.0, ""),
        ("b19f0c72e4a8", "ss", 125.0, "ac", "VS_VDDA_1V0", "running", 0.0, ""),
        ("e4d27a5c1f90", "ss", 125.0, "tran_load", "IL_VDD0P8_B", "failed", 760.0,
         "timestep too small near the IL_VDD0P8_B edge"),
        ("02aa8e6b7d31", "ff", -40.0, "noise", "VB_IB_POLY", "running", 0.0, ""),
        ("5f6c1d9e2ab7", "tt", 25.0, "dc_load", "IL_VDD0P8_B", "skipped_cached", 0.0, ""),
        ("a8e05b3c9d14", "ss", 25.0, "ac", "IL_VDD0P8_A", "done", 188.0, ""),
        ("c73b2f8e6a05", "ff", 125.0, "tran_en", "VEN_EN", "planned", 0.0, ""),
        ("d1e94a7f0c26", "tt", 125.0, "noise", "VDD0P8_B", "done", 351.0, ""),
        ("39f8c6b1e2d7", "ss", -40.0, "dc_iv", "VB_IB_PTAT", "done", 22.0, ""),
        ("6b2d0e9a4c83", "ff", 25.0, "tran_load", "IL_VDD0P8_A", "planned", 0.0, ""),
        ("f0a7c4d2b8e1", "ss", 125.0, "tran_load", "IL_VDD0P8_B", "failed", 758.0,
         "timestep too small near the IL_VDD0P8_B edge"),
        ("8e1b5f3a7d09", "tt", -40.0, "dc_temp", "", "done", 107.0, ""),
    ]
    out = []
    for rid, proc, temp, an, stim, status, cpu, error in rows:
        out.append({"run_id": rid, "process": proc, "temp_c": temp, "vset": 3,
                    "load_key": "L2", "analysis": an, "stimulus": stim,
                    "reads": [f"{an}.{DEMO_PORTS[0]}"], "status": status,
                    "cpu_seconds": cpu, "error": error, "engine": "spectre_ssh",
                    "job_id": "", "netlist_sha": "9c1e4bb7",
                    "netlist_path": f"runs/{proc}_{int(temp)}c_v3/{rid}/input.scs",
                    "psf_path": f"runs/{proc}_{int(temp)}c_v3/{rid}/psf"
                                if status in ("done", "skipped_cached") else "",
                    "recipe": _demo_recipe(rid, proc, temp, an, stim),
                    "submitted_at": "2026-09-15T13:02:00Z",
                    "finished_at": "2026-09-15T13:06:12Z" if status == "done" else "",
                    "peak_mem_mb": 1400.0, "source_path": "",
                    "cell_text": f"{proc} / {temp:g} C / vset 3 / L2"})
    return out


def _demo_recipe(rid: str, proc: str, temp: float, an: str, stim: str) -> str:
    return "\n".join([
        f"# {an}   cell {proc} / {temp:g} C / VSET 3   stimulus {stim or '-'}",
        f'~ include "pdk/toplevel.scs" section={proc}        // was section=tt',
        f'~ include "pdk/rc.scs" section={proc}              // was section=typ',
        "~ parameters VSET=3                                // was VSET=3",
        f"+ options temp={temp:g}",
        "- dcOp dc                                          // your analyses stripped",
        f"+ {an}z {an.split('_')[0]} " + ("start=10 stop=20G dec=20" if an == "ac"
                                          else "start=10 stop=100M dec=20" if an == "noise"
                                          else "stop=10u step=2n" if an.startswith("tran")
                                          else "param=temp start=-40 stop=125 step=5"),
        "+ save VDD0P8_A VDD0P8_B VB_IB_PTAT:p VB_IB_POLY:p",
        f"# ssh ewave-vm 'tcsh -c \"source ~/.cshrc; cd ~/pmukit_work/{rid}; "
        f"spectre -64 input.scs -format psfascii -raw psf\"'",
    ])


def _demo_curve(port: str, block: str, cell: str) -> dict:
    """The same analytic shapes the design prototype drew -- rails: |Zout|, biases: noise PSD."""
    rail = port.startswith("VDD")
    marg = DEMO_GRADE.get(f"{port}|{cell.split('/')[0]}|{cell.split('/')[1].rstrip('c')}") == "yellow"
    fs = [10.0 ** (1.0 + i / 20.0) for i in range(181)]

    def z(f, f0, q, a, lf):
        w = f / f0
        loop = 388.0 * a / math.sqrt((1 - w * w) ** 2 + (w / q) ** 2)
        low = 23.0 * lf / math.sqrt(1 + (f / 4e4) ** 2)
        cap = 1.0 / (2 * math.pi * f * 1e-9)
        return math.sqrt((1.0 / (1.0 / (loop + low) + 1.0 / cap)) ** 2 + 0.16)

    def zph(f, f0, q):
        return math.degrees(math.atan2(-(f / f0) / q, 1 - (f / f0) ** 2))

    def n(f, w, p):
        return 1e-24 * w * (1 + 9e3 / f) + 1e-22 * p / (1 + (f / 2e6) ** 2)

    if rail:
        gt = [z(f, 1.78e6, 2.6, 1.0, 1.0) for f in fs]
        md = [z(f, 1.56e6 if marg else 1.75e6, 2.1 if marg else 2.5,
                0.8 if marg else 0.98, 1.02) for f in fs]
        gp = [zph(f, 1.78e6, 2.6) for f in fs]
        mp = [zph(f, 1.56e6 if marg else 1.75e6, 2.1 if marg else 2.5) for f in fs]
        unit, label = "ohm", "|Zout|"
    else:
        gt = [n(f, 1.0, 1.0) for f in fs]
        md = [n(f, 1.35 if marg else 1.05, 0.7 if marg else 0.95) for f in fs]
        gp = mp = [0.0] * len(fs)
        unit, label = "A^2/Hz", "current noise PSD"
    return {"port": port, "block": block, "cell": cell, "x": fs, "x_label": "frequency [Hz]",
            "x_log": True, "unit": unit, "label": label, "complex": bool(rail),
            "gt": {"mag": gt, "phase_deg": gp}, "model": {"mag": md, "phase_deg": mp},
            "points": len(fs), "source": "demo (analytic)"}


def _demo_blocks(port: str, grade: str) -> list[dict]:
    if port.startswith("VDD"):
        return [
            {"name": "dc", "metric": "vout error / dropout", "value": "0.8 mV / 2 %",
             "limit": "5 mV / 10 %", "grade": "green"},
            {"name": "zout", "metric": "|Z| RMS / peak freq",
             "value": "1.9 dB / 12 %" if grade == "yellow" else "0.31 dB / 3 %",
             "limit": "2 dB / 15 %", "grade": "yellow" if grade == "yellow" else "green"},
            {"name": "psrr", "metric": "|H| RMS / phase", "value": "0.9 dB / 4 deg",
             "limit": "2 dB / 15 deg", "grade": "green"},
            {"name": "noise", "metric": "PSD log-RMS", "value": "0.4 dB", "limit": "1.5 dB",
             "grade": "green"},
            {"name": "load_en", "metric": "dip / overshoot",
             "value": "3.8 % / not run" if grade == "not_run" else "3.8 % / 6.1 %",
             "limit": "10 % / 10 %", "grade": "not_run" if grade == "not_run" else "green"},
        ]
    return [
        {"name": "idc", "metric": "I error / PTAT slope", "value": "0.6 % / 1.1 %",
         "limit": "2 % / 5 %", "grade": "green"},
        {"name": "yout", "metric": "gds / Cout", "value": "3 % / 5 %", "limit": "10 % / 20 %",
         "grade": "green"},
        {"name": "noise", "metric": "PSD log-RMS",
         "value": "1.3 dB" if grade == "yellow" else "0.5 dB", "limit": "1.5 dB",
         "grade": "yellow" if grade == "yellow" else "green"},
        {"name": "psrr", "metric": "|H| RMS", "value": "1.0 dB", "limit": "2 dB",
         "grade": "green"},
    ]


DEMO_FILES = [
    ("PMU_demo_pmu.scs", "Spectre library", 1240,
     "library with sections tt / ss / ff, each including its .va"),
    ("PMU_demo_pmu_tt.va", "Verilog-A", 38912,
     "tt corner, temperature continuous, vset and load-EN switches as instance params"),
    ("PMU_demo_pmu_ss.va", "Verilog-A", 38880, "ss corner"),
    ("PMU_demo_pmu_ff.va", "Verilog-A", 38848, "ff corner"),
    ("envelope.json", "validity", 910,
     "load / temp / freq / corner / VSET ranges, and which large-signal terms are on"),
    ("report.md", "report", 14336,
     "trust summary, per-cell grades, HB health check, not-run list"),
    ("provenance.json", "provenance", 640,
     "config sha, dataset sha, pmukit version, testbench state at characterization"),
]

DEMO_FILE_BODY = {
    "PMU_demo_pmu.scs": """// pmukit 0.1.0 | demo_pmu | 2026-09-15T14:02 | config 5d8ca1 | dataset a91fc3
library PMU_demo_pmu
  section tt
    ahdl_include "PMU_demo_pmu_tt.va"
  endsection tt
  section ss
    ahdl_include "PMU_demo_pmu_ss.va"
  endsection ss
  section ff
    ahdl_include "PMU_demo_pmu_ff.va"
  endsection ff
endlibrary PMU_demo_pmu
""",
    "envelope.json": """{
  "load_a": {"VDD0P8_A": [2e-06, 0.001], "VDD0P8_B": [2e-05, 0.004]},
  "temp_c": [-40, 125],
  "freq_max_hz": 2e10,
  "corners": ["tt", "ss", "ff"],
  "vset_codes": [3],
  "ls_default_on": ["VDD0P8_A", "VDD0P8_B"],
  "ports": ["EN", "IB_PTAT", "VDD0P8_A", "VDD0P8_B"],
  "notes": ["EN power-up ramp: usable, not signed off"]
}
""",
    "report.md": """# demo_pmu -- can I trust this model in my simulation?

Valid for: load_A 2 u - 1 mA, load_B 20 u - 4 mA, -40 - 125 C, <= 20 GHz, tt/ss/ff, VSET 3.
Usable, not sign-off: EN power-up ramp.
Not run: tran_load.VDD0P8_B at ss / 125 C (failed twice).
HB health: first-step residual 7.7e-3, every large-signal term checked one at a time.

| port | tt -40 | tt 25 | tt 125 | ss -40 | ss 25 | ss 125 | ff -40 | ff 25 | ff 125 |
|---|---|---|---|---|---|---|---|---|---|
| VDD0P8_A | green | green | green | green | green | yellow | green | green | green |
| VDD0P8_B | green | green | green | green | yellow | not_run | yellow | green | green |
""",
    "provenance.json": """{
  "pmukit": "0.1.0",
  "config_sha": "5d8ca1",
  "dataset_sha": "a91fc3",
  "runs_consumed": 280,
  "tb_state_note": "RX mode, register 0x12 = 0x03",
  "created": "2026-09-15T14:02:11Z"
}
""",
}
DEMO_FILE_BODY["PMU_demo_pmu_tt.va"] = """// pmukit 0.1.0 | demo_pmu | corner tt | 2026-09-15T14:02
// config 5d8ca1 | dataset a91fc3 | TB state: RX mode, reg 0x12=0x03
// valid: load_A 2u..1m  load_B 20u..4m  temp -40..125  f<=20G  vset 3
// large-signal: load_en_A ON (HB check 7.7e-3)  load_en_B OFF (default)  en_ramp usable-only
`include "disciplines.vams"
module PMU_demo_pmu(VDDA_1V0, VDD0P8_A, VDD0P8_B, VDD0P8_C, IB_PTAT, IB_POLY, EN, TESTMODE,
                    VSS_A, VSS_B, AGND);
  inout VDDA_1V0, VDD0P8_A, VDD0P8_B, VDD0P8_C, IB_PTAT, IB_POLY, EN, TESTMODE;
  inout VSS_A, VSS_B, AGND;
  parameter integer vset = 3;
  parameter integer load_en_A = 1, load_en_B = 0;
  // rail A: dc table(T), zout ladder, psrr gm-C biquad, noise, load_en (opt-in)
  ...
"""
DEMO_FILE_BODY["PMU_demo_pmu_ss.va"] = DEMO_FILE_BODY["PMU_demo_pmu_tt.va"].replace(
    "corner tt", "corner ss").replace("7.7e-3", "9.1e-3")
DEMO_FILE_BODY["PMU_demo_pmu_ff.va"] = DEMO_FILE_BODY["PMU_demo_pmu_tt.va"].replace(
    "corner tt", "corner ff").replace("7.7e-3", "6.4e-3")

DEMO_DIGEST_BLOCKS = [
    ("D0", "provenance + config", 1100, 0, True),
    ("D1", "ledger summary", 2800, 1, True),
    ("D2", "fitted parameters, all cells (lossless)", 6400, 2, True),
    ("D6", "failed-run logs + netlist diff", 4600, 3, True),
    ("D3", "grades + trust summary", 900, 4, True),
    ("D4bias", "curves: bias idc(T), I-V, current noise", 7300, 5, True),
    ("D4rail", "curves: Zout + PSRR + rail noise, selected cell", 9400, 6, True),
    ("D5", "transients: load-EN, selected cell", 7200, 7, False),
]


def _demo_project_rows() -> list[dict]:
    return [
        {"name": "demo_pmu", "dut": "PMU_DEMO", "screen": "model", "step": 4,
         "cells": "9 (tt/ss/ff x 3 T)", "deliverables": 2, "touched": "today 14:02",
         "status": "ok", "note": "fit done, 1 cell yellow, 1 not run"},
        {"name": "ldo_v3_miller", "dut": "LDO_V3_MILLER (synthetic)", "screen": "deliver",
         "step": 5, "cells": "9", "deliverables": 4, "touched": "yesterday", "status": "ok",
         "note": "regression fixture, all green"},
        {"name": "capless_try", "dut": "LDO_V2_CAPLESS (synthetic)", "screen": "new", "step": 1,
         "cells": "-", "deliverables": 0, "touched": "3 days ago", "status": "mute",
         "note": "netlist loaded, plan not built"},
    ]


def _demo_machine() -> dict:
    return {"checked_at": _now(), "ready": True, "data_root": "~/pmukit_data (demo)",
            "pmukit": _version(), "python": sys.version.split()[0], "host": "demo",
            "probes": [
                {"name": "engine", "ok": True, "detail": "spectre 18.1.0.077 (demo)", "ms": 340,
                 "how": "ssh ewave-vm tcsh -c 'source ~/.cshrc; which spectre'", "reason": None},
                {"name": "queue", "ok": True, "detail": "rf_short, 12 slots free (demo)",
                 "ms": 90, "how": "dqueue", "reason": None},
                {"name": "pdk", "ok": True, "detail": "$PDK -> .../models (demo)", "ms": 2,
                 "how": "$PDK", "reason": None},
                {"name": "license", "ok": False, "detail": "", "ms": 1,
                 "how": "$CDS_LIC_FILE / $LM_LICENSE_FILE",
                 "reason": {"what": "no license server is configured in this environment.",
                            "why": "None of CDS_LIC_FILE, LM_LICENSE_FILE or "
                                   "EMPYREAN_LICENSE_FILE is set, so a simulation would fail at "
                                   "check-out rather than at start.",
                            "do": ["Source the site environment before `pmukit ui`",
                                   "Planning, fitting and delivering need no license"],
                            "where": "environment: license"}},
            ]}


# ============================================================================== route helpers
def _read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > MAX_BODY:
        raise _err(f"the request body is {length} bytes, more than the {MAX_BODY} byte limit.",
                   "The page never uploads simulation results; only a netlist or a form comes "
                   "through here.",
                   ["Send the netlist by path instead of by value: "
                    "{\"path\": \"/full/path/input.scs\"}"],
                   "request body")
    raw = handler.rfile.read(length)
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _err("the request body is not valid JSON.",
                   f"json.loads failed: {exc}.",
                   ["This is a bug in the page -- reload it",
                    "If you are calling the API by hand, send Content-Type: application/json"],
                   "request body") from None
    if not isinstance(obj, dict):
        raise _err("the request body is not a JSON object.",
                   "Every route in the table takes an object, never a bare list or number.",
                   ["Wrap the value: {\"value\": ...}"], "request body")
    return obj


def _one(query: dict, key: str, default=None):
    v = query.get(key)
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def _safe_name(value: str, pattern: re.Pattern, kind: str, where: str) -> str:
    """Reject anything that could walk out of its directory. The value is already URL-decoded."""
    bad = (not isinstance(value, str) or not value or value in (".", "..")
           or "/" in value or "\\" in value or ".." in value or "\x00" in value
           or value.startswith("~") or not pattern.match(value))
    if bad:
        raise _err(f"refusing {value!r} as a {kind}.",
                   "It would leave the directory it is supposed to name: only a bare name made "
                   "of letters, digits, dot, dash and underscore is accepted.",
                   [f"Pick a {kind} from the list this screen shows"],
                   where)
    return value


_UNSET_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _resolve_user_path(typed: str, where: str) -> pathlib.Path:
    """A path as a tcsh user types it -> an absolute path to an existing netlist file.

    `~` and `$VAR` / `${VAR}` are expanded (against the SERVER's environment -- it is the server
    that reads the file); quotes pasted along with the path are dropped; a relative path is
    resolved against the server's cwd, and the error says so, with both paths spelled out. A
    directory is accepted when it holds an `input.scs` (ADE's netlist directory).
    """
    raw = str(typed).strip().strip("'\"").strip()
    expanded = os.path.expandvars(os.path.expanduser(raw))
    left = [m.group(1) for m in _UNSET_VAR.finditer(expanded) if m.group(1) not in os.environ]
    if left:
        raise _err(f"${left[0]} is not set in the pmukit server's environment.",
                   f"{raw!r} names an environment variable; the server expands it with its own "
                   f"environment (the shell `pmukit ui` was started from), where it is not set.",
                   [f"Type the path without ${left[0]}", f"Or setenv {left[0]} ... in the shell "
                    "and restart `pmukit ui`"], where)
    p = pathlib.Path(expanded)
    if not p.is_absolute():
        p = pathlib.Path.cwd() / p
    p = pathlib.Path(os.path.normpath(str(p)))
    if p.is_dir() and (p / "input.scs").is_file():
        p = p / "input.scs"
    if not p.is_file():
        rel = "" if pathlib.Path(expanded).is_absolute() else (
            f" A relative path is read from the server's working directory, {pathlib.Path.cwd()}.")
        raise _err(f"there is no netlist at {p}.",
                   f"{raw!r} does not point at a readable file on the machine the pmukit server "
                   f"runs on" + (" (it is a directory without an input.scs)" if p.is_dir() else "")
                   + "." + rel,
                   ["Type the full path, e.g. $WORK_ROOT/sim/pmu_tb/spectre/schematic/netlist/"
                    "input.scs", "Or drop the file on the New screen instead"],
                   str(p))
    return p


def site_path(root=None):
    """site.json under the server's data root when one was given, else $PMUKIT_DATA's."""
    return (pathlib.Path(root) / "site.json") if root is not None else None


def _site_cpus(val, where: str) -> int:
    """cpus from a JSON body: an integer, or the text of one (a form field sends text)."""
    if isinstance(val, str) and val.strip().isdigit():
        val = int(val.strip())
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        raise _err(f"site cpus is not a positive integer ({val!r}).",
                   "cpus becomes the -mt / queue slot count on every submitted job.",
                   ['Send {"cpus": 8} -- a whole number, 1 or more'], where)
    return val


def _site_names(val, where: str) -> list:
    """One account name or a list of them."""
    if val is None:
        return []
    items = val if isinstance(val, list) else [val]
    if not all(isinstance(x, str) and x.strip() for x in items):
        raise _err(f"remove_account is not an account name or a list of them ({val!r}).",
                   "An account is named by its Donau -A name.",
                   ['Send {"remove_account": "<name>"}'], where)
    return [x.strip() for x in items]


def _site_accounts(val, where: str) -> list:
    """add_account as {"name", "note"}, as "name=note" (the CLI spelling), or a list of either."""
    if val is None:
        return []
    out = []
    for x in (val if isinstance(val, list) else [val]):
        if isinstance(x, str):
            name, _, note = x.partition("=")
        elif isinstance(x, dict) and isinstance(x.get("note", ""), str):
            name, note = x.get("name"), x.get("note", "")
        else:
            name, note = None, ""
        if not isinstance(name, str) or not name.strip():
            raise _err("an account to add has no name.",
                       "The account list is what the Plan screen offers for dsub -A; an entry "
                       "needs the Donau account name, the note is optional.",
                       ['Send {"add_account": {"name": "<account>", "note": "sims up to 1TB"}}'],
                       where)
        if any(c.isspace() for c in name.strip()):
            raise _err(f"account name {name.strip()!r} contains a space.",
                       "It is passed to dsub -A as one word; a space would split it.",
                       ["Type the Donau account name exactly as the site gave it",
                        "Put any description in the note instead"], where)
        out.append((name.strip(), note.strip()))
    return out


# ============================================================================== the handler
class Api:
    """Route implementations. Kept out of the HTTP class so they are easy to call from tests."""

    def __init__(self, *, demo: bool = False, root=None, project: str | None = None) -> None:
        self.demo = bool(demo)
        self.root = root
        #: `pmukit open <project>`: the project the page opens on when its URL names none.
        self.initial_project = project if project and PROJECT_RE.match(str(project)) else ""

    # ---------------------------------------------------------------- site (install-wide)
    def site_get(self) -> dict:
        """What the Plan footer and the Settings screen show: the engine in effect, the account
        list and the pick, what the environment overrides, and what the box's environment
        provides (read-only -- `pmukit site` prints the same table)."""
        from . import sitenv
        from .site import ENGINE_NOTES, ENGINES, SIMULATORS, SiteConfig
        engines = [{"name": e, "note": ENGINE_NOTES.get(e, "")} for e in ENGINES]
        if self.demo:
            return {"engine": "donau_alps", "simulator": "alps", "simulator_source": "default",
                    "queue": "short", "cpus": 8, "ssh_host": "ewave-vm",
                    "accounts": [{"name": "ug_demo.smallClass", "note": "sims up to 512GB"},
                                 {"name": "ug_demo.bigClass", "note": "sims up to 2TB"}],
                    "account": "ug_demo.smallClass", "account_source": "site config",
                    "stored": {"engine": "donau_alps", "simulator": "alps", "queue": "short",
                               "cpus": 8, "project_account": "ug_demo.smallClass"},
                    "overrides": {}, "engines": engines, "simulators": list(SIMULATORS),
                    "environment": [], "path": "(demo, nothing is written)", "demo": True}
        path = site_path(self.root)
        cfg = SiteConfig.load(path)
        stored = SiteConfig.load(path, env=False)
        acc = sitenv.account(cfg)
        sim = sitenv.simulator(cfg)
        overrides = SiteConfig.env_overrides()
        if sim.source.startswith("$"):
            overrides["simulator"] = sim.source
        if acc.source.startswith("$"):
            overrides["account"] = acc.source
        env = [{"name": f.name, "value": f.value, "source": f.source}
               for f in sitenv.facts(cfg) if f.name not in ("simulator", "account")]
        return {"engine": cfg.engine, "simulator": sim.value, "simulator_source": sim.source,
                "queue": cfg.queue, "cpus": cfg.cpus, "ssh_host": cfg.ssh_host,
                "accounts": list(cfg.accounts), "account": acc.value,
                "account_source": acc.source,
                "stored": {"engine": stored.engine, "simulator": stored.simulator,
                           "queue": stored.queue, "cpus": stored.cpus,
                           "project_account": stored.project_account},
                "overrides": overrides, "engines": engines, "simulators": list(SIMULATORS),
                "environment": env, "path": str(path or SiteConfig.default_path())}

    #: What PUT /api/site accepts -- the `pmukit site` flags, spelled as JSON keys.
    SITE_KEYS = ("engine", "simulator", "queue", "cpus", "ssh_host", "remote_workdir",
                 "spectre_cmd", "add_account", "remove_account", "account")

    def site_put(self, body: dict) -> dict:
        """Change the site config the way `pmukit site` does, and save it to site.json.

        {"engine", "simulator", "queue", "cpus", "ssh_host", ...} set a value;
        {"add_account": {"name", "note"}} (or "name=note", or a list of either) adds or re-notes
        a Donau account; {"remove_account": "name"} (or a list) drops one; {"account": "name"}
        makes it the one runs are charged to (joining the list if new).  Applied in that order,
        validated as a whole, and written only when every part is valid.  The change is made on
        top of what is STORED, so an environment override is never written into the file.
        """
        from .site import SiteConfig
        where = "PUT /api/site"
        body = body if isinstance(body, dict) else {}
        unknown = sorted(set(body) - set(self.SITE_KEYS))
        if unknown:
            raise _err(f"PUT /api/site does not take {unknown}.",
                       "The site config is closed: an unknown key is a typo that would be "
                       "silently ignored.",
                       [f"Send only these keys: {', '.join(self.SITE_KEYS)}"], where)
        if not any(k in body for k in self.SITE_KEYS):
            raise _err("nothing to change was given.",
                       "PUT /api/site changes the site config: the engine, the simulator, the "
                       "queue, the CPU count or the Donau account list.",
                       ['Send e.g. {"account": "<one of the listed accounts>"}',
                        'Or {"engine": "donau_alps"}'], where)
        if "account" in body and not str(body.get("account") or "").strip():
            raise _err("no account was given.",
                       "PUT /api/site selects the Donau account runs are charged to.",
                       ['Send {"account": "<one of the listed accounts>"}'], where)
        path = site_path(self.root)
        cfg = SiteConfig() if self.demo else SiteConfig.load(path, env=False)
        for name in ("engine", "simulator", "queue", "ssh_host", "remote_workdir", "spectre_cmd"):
            if name in body:
                val = body[name]
                if not isinstance(val, str):
                    raise _err(f"site {name} is not a string ({val!r}).",
                               f"{name} is stored as text in site.json.",
                               [f'Send {{"{name}": "<text>"}}'], where)
                setattr(cfg, name, val.strip())
        if "cpus" in body:
            cfg.cpus = _site_cpus(body["cpus"], where)
        for name, note in _site_accounts(body.get("add_account"), where):
            cfg.add_account(name, note)
        for name in _site_names(body.get("remove_account"), where):
            cfg.remove_account(name)
        if "account" in body:
            cfg.select_account(str(body["account"]))
        if self.demo:
            cfg.validate(where)                      # the same refusals, nothing written
            return self.site_get()
        cfg.save(path)
        return self.site_get()

    # ---------------------------------------------------------------- Home
    def projects(self) -> dict:
        if self.demo:
            return {"projects": _demo_project_rows(), "data_root": "~/pmukit_data (demo)",
                    "demo": True, "initial_project": self.initial_project}
        rows = []
        for entry in state_dir_projects(self.root):
            if not PROJECT_RE.match(entry["name"]):
                continue
            pr = Project(entry["name"], self.root)
            try:
                st = pr.state()
            except PmuError as exc:
                # One unreadable folder must not blank out Home: list it with the reason.
                rows.append({"name": entry["name"], "dut": "-", "screen": "home", "step": 0,
                             "cells": "-", "deliverables": 0, "touched": "-", "status": "bad",
                             "note": exc.what})
                continue
            cfg = pr.config_or_none()
            n_deliv = 0
            deliver_dir = pr.dir / "deliver"
            if deliver_dir.is_dir():
                n_deliv = sum(1 for d in deliver_dir.iterdir() if d.is_dir())
            cells = "-"
            if cfg is not None:
                cells = (f"{len(cfg.corner_names())} x {len(cfg.temps_c)} T "
                         f"x {len(cfg.vset_codes)} vset")
            rows.append({"name": entry["name"], "dut": (cfg.pmu_inst if cfg else "-"),
                         "screen": st.screen, "step": SCREEN_INDEX.get(st.screen, 1),
                         "cells": cells, "deliverables": n_deliv,
                         "touched": st.updated_at or "-",
                         "status": "ok" if cfg is not None else "mute",
                         "note": (st.recent[0]["text"] if st.recent else
                                  ("config saved" if cfg is not None else
                                   "netlist loaded, plan not built" if st.netlist else
                                   "empty project"))})
        return {"projects": rows, "data_root": str(paths.data_root()), "demo": False,
                "initial_project": self.initial_project}

    def new_project(self, body: dict) -> dict:
        if self.demo:
            raise _err("--demo cannot create projects.",
                       "Demo mode serves a fixed synthetic PMU and never touches $PMUKIT_DATA, "
                       "so there is nothing to create.",
                       ["Restart without --demo to work on real projects"], "POST /api/projects")
        name = str(body.get("name") or "").strip()
        pr = Project(name, self.root)
        if pr.dir.exists() and any(pr.dir.iterdir()) and not body.get("reopen"):
            raise _err(f"project {name!r} already exists.",
                       f"{pr.dir} is not empty; creating it again would sit on top of an existing "
                       f"ledger and dataset.",
                       [f"Open {name} from Home instead",
                        "Or pick another name"], str(pr.dir))
        pr.ensure()
        st = pr.state()
        st.go("new").note(f"project {name} created", "home")
        st.save()
        return {"project": name, "path": str(pr.dir), "screen": st.screen, "created": True}

    def deliverables_diff(self, a: str, b: str) -> dict:
        if self.demo:
            return {"a": a, "b": b, "diff": {
                "provenance": {"dataset_sha": ["a91fc3", "c07d12"]},
                "envelope": {"freq_max_hz": [1e10, 2e10]},
                "files": {"added": [], "removed": [], "changed": ["PMU_demo_pmu_ss.va",
                                                                  "report.md"]},
                "grades": {"VDD0P8_B|ss|noise": ["yellow", "green"]}}}
        da = self._deliverable_at(a)
        db = self._deliverable_at(b)
        diff = da.diff(db)
        return {"a": a, "b": b,
                "diff": {"provenance": {k: list(v) for k, v in diff["provenance"].items()},
                         "envelope": {k: list(v) for k, v in diff["envelope"].items()},
                         "files": diff["files"],
                         "grades": {"|".join(k): list(v) for k, v in diff["grades"].items()}}}

    def _deliverable_at(self, ref: str):
        from .deliverable import Deliverable
        if "/" not in (ref or ""):
            raise _err(f"{ref!r} does not name a deliverable.",
                       "A deliverable is identified by project and stamp: `<project>/<stamp>`.",
                       ["Pick both sides from the list on Home"],
                       "GET /api/deliverables/diff?a=&b=")
        proj, _, stamp = ref.partition("/")
        proj = _safe_name(proj, PROJECT_RE, "project name", "GET /api/deliverables/diff")
        stamp = _safe_name(stamp, STAMP_RE, "deliverable stamp", "GET /api/deliverables/diff")
        return Deliverable.open(Project(proj, self.root).dir / "deliver" / stamp)

    # ---------------------------------------------------------------- New
    def pins(self, project: str) -> dict:
        if self.demo:
            return _demo_pins()
        pr = Project(project, self.root)
        nl = pr.netlist()
        table = pr.pins(nl)
        d = table.to_dict()
        pins = []
        for name, entry in d.items():
            row = dict(entry)
            row["name"] = name
            pins.append(row)
        pins.sort(key=lambda r: r.get("index", 0))
        rails = [p for p in pins if p["role"] == "rail" and p["fate"] == "model"]
        biases = [p for p in pins if p["role"] == "bias" and p["fate"] == "model"]
        path = pr.netlist_path()
        return {"pmu_inst": table.pmu_inst, "pmu_master": table.pmu_master, "pins": pins,
                "candidates": pmu_candidates(nl),
                "sections": table.sections, "params": table.params,
                "analyses": table.analyses, "notes": table.notes,
                "summary": {"rails": len(rails), "biases": len(biases),
                            "stubs": len([p for p in pins if p["fate"] == "stub"]),
                            "grounds": len([p for p in pins if p["is_ground"]]),
                            "unclassified": [p["name"] for p in pins
                                             if p["role"] == "none" and not p["is_ground"]
                                             and p["fate"] != "ignore"]},
                "netlist": {"path": str(path), "sha": jsonio.sha_file(path, 12),
                            "bytes": path.stat().st_size}}

    def netlist_info(self, project: str) -> dict:
        """The New screen's Netlist row, readable even when the pins are not: where the file
        came from (so it can be re-read in one click), whether that file changed on disk since,
        the copy the project works on, the PMU candidates and what changed at the last read."""
        if self.demo:
            return _demo_netlist_info()
        pr = Project(project, self.root)
        src = pr.netlist_source()
        if src and src.get("path"):
            sp = pathlib.Path(src["path"])
            if not sp.is_file():
                src["on_disk"] = "missing"
            else:
                try:
                    data = sp.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
                    same = jsonio.sha_bytes(data.encode("utf-8"), 12) == src.get("sha")
                    src["on_disk"] = "same" if same else "changed"
                except OSError:
                    src["on_disk"] = "missing"
        cfg = pr.config_or_none()
        try:
            path = pr.netlist_path()
        except PmuError:
            path = None
        copy, cands = None, []
        if path is not None:
            copy = {"path": str(path), "sha": jsonio.sha_file(path, 12),
                    "bytes": path.stat().st_size}
            try:
                cands = pmu_candidates(pr.netlist())
            except PmuError:
                cands = []
        return {"source": src, "copy": copy, "pmu_inst": cfg.pmu_inst if cfg is not None else "",
                "candidates": cands, "cwd": str(pathlib.Path.cwd())}

    def load_netlist(self, project: str, body: dict) -> dict:
        """Copy the netlist into the project and parse it. Runs as a job (parse is the slow part
        on a real deck, and the New screen wants a loading state with a real backing).

        Three ways in: `text` (dropped or chosen in the browser, with its `name`), `path` on this
        machine (`~` and `$VAR` expanded), or `reread` -- the path it was last loaded from, so a
        bench fixed in Virtuoso and exported to the same place is one click. A re-read keeps
        every config answer that still applies, drops those of vanished pins, and says so.
        """
        where = f"POST /api/p/{project}/netlist"
        pr = Project(project, self.root).ensure()
        text = body.get("text")
        src = body.get("path")
        pmu_inst = str(body.get("pmu_inst") or "").strip()
        target = pr.netlist_dir / "input.scs"
        prev = pr.netlist_source() or {}
        if body.get("reread"):
            if not prev.get("path"):
                raise _err("there is no path to re-read the netlist from.",
                           "This project's netlist was " +
                           ("dropped into the browser, and a browser never tells the server "
                            "where a file came from." if prev.get("via") == "upload" else
                            "loaded before pmukit recorded where netlists come from."),
                           ["Type its path on this machine in the box on the New screen",
                            "Or drop the file again"], where)
            src, text = prev["path"], None
        if isinstance(text, str) and text.strip():
            new_text = text.replace("\r\n", "\n")
            name = re.split(r"[\\/]", str(body.get("name") or "input.scs"))[-1] or "input.scs"
            meta = {"path": "", "typed": "", "name": name, "via": "upload"}
        elif isinstance(src, str) and src.strip():
            sp = _resolve_user_path(src, where)
            try:
                new_text = sp.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            except OSError as exc:
                raise _err(f"could not read {sp}.", f"The server cannot open it: {exc}.",
                           ["Check the file's permissions", "Or drop the file on the New screen"],
                           str(sp)) from None
            meta = {"path": str(sp), "typed": str(src).strip(), "name": sp.name, "via": "path"}
        else:
            raise _err("no netlist was given.",
                       "POST /api/p/<project>/netlist needs either the file's text or a path to "
                       "it on this machine.",
                       ["Drop the file on the New screen",
                        "Or send {\"path\": \"/full/path/input.scs\"}"],
                       "POST /api/p/<project>/netlist")
        data = new_text.encode("utf-8")
        meta.update(sha=jsonio.sha_bytes(data, 12), bytes=len(data), loaded_at=_now())
        copy_sha = jsonio.sha_file(target, 12) if target.is_file() else ""
        before = prev.get("sha") or copy_sha
        unchanged = bool(copy_sha) and before == meta["sha"]
        # Roles assigned on this screen are written into the COPY; a changed source replaces it.
        edited = bool(copy_sha and prev.get("sha") and copy_sha != prev["sha"])

        def work(job):
            from .netlist import Netlist
            cfg = pr.config_or_none()
            old = None
            if cfg is not None and copy_sha and not unchanged:
                job.say("reading the pins of the netlist loaded before", 0.1)
                try:
                    old = pr.pins()
                except PmuError:
                    old = None            # the previous copy did not scan either
            if not unchanged:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_text, encoding="utf-8", newline="\n")
            # Recorded before the scan: a deck the scan refuses can still be re-read once fixed.
            pr.save_netlist_source(dict(meta, changes=None))
            job.say(f"reading {meta['name']}", 0.3)
            nl = Netlist.from_file(target)
            nl.origin = meta["path"]
            inst = pmu_inst or (cfg.pmu_inst if cfg is not None else guess_pmu_inst(nl))
            if nl.find_instance(inst) is None:
                raise _inst_gone(nl, inst)
            job.say(f"resolving the pins of {inst}", 0.6)
            table = nl.scan(inst, ports=None)
            if cfg is None:
                pr.save_config(_seed_config(project, target, inst, table), "seeded from the netlist")
                changes = {"first": True, "unchanged": False, "pmu_inst": inst,
                           "text": f"read {meta['name']}: {inst}, {len(table.pins)} pins"}
            elif unchanged and inst == cfg.pmu_inst:
                changes = {"first": False, "unchanged": True, "pmu_inst": inst,
                           "text": f"{meta['name']} is unchanged since the last read "
                                   f"(byte-identical); every answer is kept"}
            else:
                new_cfg, changes = _carry_over(cfg, table, old, inst)
                if edited:
                    changes["text"] += ("; the role sources pmukit had written into the previous "
                                        "copy are replaced by this file")
                pr.save_config(new_cfg, "netlist re-read: " + changes["text"])
            meta["changes"] = changes
            pr.save_netlist_source(meta)
            st = pr.state()
            st.netlist = str(target)
            st.go("new").note(f"netlist read: {changes['text']}", "new")
            st.save()
            with _CACHE_LOCK:
                _PLAN_CACHE.pop(project, None)
            job.say("done", 1.0)
            out = self.pins(project)
            out["changes"] = changes
            return out

        return {"job": JOBS.submit("parse", project, f"read {meta['name']}", work).id,
                "source": meta}

    def set_instance(self, project: str, body: dict) -> dict:
        """The PMU instance picker: re-scan with another instance.

        The pins are a different set, so ports and loads are re-seeded for it; every other
        answer (corners, temperatures, codes, the code variable, fmax, the state note) is kept,
        and the change goes through the config history so Ctrl-Z brings the old instance back.
        """
        where = f"PUT /api/p/{project}/netlist/instance"
        if self.demo:
            raise _err("--demo cannot switch the PMU instance.",
                       "Demo mode serves a fixed synthetic PMU and never touches $PMUKIT_DATA.",
                       ["Restart without --demo to work on real projects"], where)
        inst = str(body.get("pmu_inst") or "").strip()
        if not inst:
            raise _err("no PMU instance was given.",
                       "The picker sends the name of a top-level instance of the netlist.",
                       ["Pick one in the PMU instance list on the New screen"], where)
        pr = Project(project, self.root)
        nl = pr.netlist()
        if nl.find_instance(inst) is None:
            raise _inst_gone(nl, inst)
        table = nl.scan(inst, ports=None)
        cfg = pr.config_or_none()
        if cfg is None:
            new_cfg = _seed_config(project, pr.netlist_path(), inst, table)
            changes = {"first": True, "unchanged": False, "pmu_inst": inst,
                       "text": f"PMU instance {inst}: {len(table.pins)} pins"}
        elif cfg.pmu_inst == inst:
            return {"pmu_inst": inst, "pins": self.pins(project),
                    "changes": {"first": False, "unchanged": True, "pmu_inst": inst,
                                "text": f"{inst} already is the PMU instance"}}
        else:
            new_cfg, changes = _carry_over(cfg, table, None, inst)
        pr.save_config(new_cfg, f"PMU instance -> {inst}")
        meta = pr.netlist_source()
        if meta is not None:
            meta["changes"] = changes
            pr.save_netlist_source(meta)
        st = pr.state()
        st.netlist = str(pr.netlist_path())
        st.note(changes["text"], "new")
        st.save()
        with _CACHE_LOCK:
            _PLAN_CACHE.pop(project, None)
        return {"pmu_inst": inst, "changes": changes, "pins": self.pins(project)}

    def set_pin(self, project: str, pin: str, body: dict) -> dict:
        """The Model column and the right-click 'set role'.

        `fate` writes the ports map of the config (undoable). `role` is different: a role comes
        from the netlist, so assigning one WRITES the convention source into the netlist -- the
        netlist stays the single source of truth for roles.
        """
        pr = Project(project, self.root)
        cfg = pr.config()
        changed = []
        role = body.get("role")
        if role:
            path = pr.netlist_path()
            nl = pr.netlist()
            table = nl.scan(cfg.pmu_inst, ports=dict(cfg.ports))
            if pin not in table.pins:
                raise _err(f"{pin!r} is not a pin of {cfg.pmu_inst}.",
                           "Roles are assigned to pins of the PMU instance named in the config.",
                           [f"Pick one of: {', '.join(sorted(table.pins))}"], str(path))
            dc = body.get("dc")
            if dc is None:
                dc = 0.0 if role in ("rail", "bias") else 1.0
            name = nl.insert_role_source(table.pins[pin], str(role), dc=float(dc))
            nl.write(path)
            with _CACHE_LOCK:
                _PLAN_CACHE.pop(project, None)
            changed.append(f"role {role} (wrote {name} into the netlist)")
        fate = body.get("fate")
        if fate:
            ports = dict(cfg.ports)
            ports[pin] = str(fate)
            cfg.ports = ports
            if str(fate) != "model":
                cfg.my_load = {k: v for k, v in cfg.my_load.items() if k != pin}
            cfg.validate()
            pr.save_config(cfg, f"{pin} -> {fate}")
            changed.append(f"fate {fate}")
        if not changed:
            raise _err(f"nothing to change on pin {pin!r}.",
                       "A pin update carries `fate` (model / stub / ignore) and/or `role` "
                       "(rail / bias / supply / en).",
                       ["Send {\"fate\": \"stub\"} or {\"role\": \"rail\", \"dc\": 5e-4}"],
                       f"PUT /api/p/{project}/pins/{pin}")
        st = pr.state()
        st.note(f"pin {pin}: {', '.join(changed)}", "new")
        st.save()
        return {"pin": pin, "changed": changed, "pins": self.pins(project)}

    def get_config(self, project: str) -> dict:
        if self.demo:
            return {"exists": True, "config": _demo_config(), "sha": "5d8ca1",
                    "answers": {}, "undoable": "", "history": []}
        pr = Project(project, self.root)
        st = pr.state()
        cfg = pr.config_or_none()
        return {"exists": cfg is not None,
                "config": cfg.to_dict() if cfg is not None else None,
                "sha": cfg.sha() if cfg is not None else "",
                "answers": st.answers, "undoable": st.undoable(),
                "history": st.history().entries()[-10:]}

    def put_config(self, project: str, body: dict) -> dict:
        from .config import ProjectConfig
        pr = Project(project, self.root).ensure()
        raw = body.get("config") if isinstance(body.get("config"), dict) else body
        raw = dict(raw)
        raw.setdefault("project", project)
        cfg = ProjectConfig.from_dict(raw, where=str(pr.config_path))
        pr.save_config(cfg, str(body.get("note") or "edited on the New screen"))
        with _CACHE_LOCK:
            _PLAN_CACHE.pop(project, None)
        st = pr.state()
        if isinstance(body.get("answers"), dict):
            st.answers = body["answers"]
        st.note("configuration saved", "new")
        st.save()
        return {"config": cfg.to_dict(), "sha": cfg.sha(), "undoable": st.undoable()}

    def derived(self, project: str) -> dict:
        if self.demo:
            return {"derived": {"note": "demo mode serves a fixed derived config"},
                    "sha": "d3m0"}
        pr = Project(project, self.root)
        d = pr.derived()
        return {"derived": d.to_dict(), "sha": d.sha()}

    def undo_config(self, project: str) -> dict:
        pr = Project(project, self.root)
        st = pr.state()
        kind, payload = st.undo()
        if kind == "config":
            payload.save(pr.config_path)
            st.note("undo: configuration restored", "new")
            out = {"kind": kind, "config": payload.to_dict(), "sha": payload.sha()}
        else:
            st.note("undo: plan ticks restored", "plan")
            out = {"kind": kind, "plan_ticks": payload}
        st.save()
        with _CACHE_LOCK:
            _PLAN_CACHE.pop(project, None)
        out["undoable"] = st.undoable()
        return out

    def measure_load(self, project: str) -> dict:
        """Read what the netlist already says about each rail's load, and say where it came from.

        The ON current is a measurement -- the dc of the IL_ source. The OFF current is NOT in
        the netlist, so it is offered as a suggestion and labelled as one; pmukit does not invent
        a number and then present it as measured.
        """
        if self.demo:
            return {"rails": {"VDD0P8_A": {"on_a": 5e-4, "on_from": "IL_VDD0P8_A dc=500u",
                                           "off_a_suggested": 2e-6,
                                           "off_note": "not in the netlist -- you set it"},
                              "VDD0P8_B": {"on_a": 2e-3, "on_from": "IL_VDD0P8_B dc=2m",
                                           "off_a_suggested": 8e-6,
                                           "off_note": "not in the netlist -- you set it"}},
                    "biases": {"IB_PTAT": {"compliance_v": 0.4, "from": "VB_IB_PTAT dc=0.4"},
                               "IB_POLY": {"compliance_v": 0.4, "from": "VB_IB_POLY dc=0.4"}}}
        pr = Project(project, self.root)
        table = pr.pins()
        cfg = pr.config_or_none()
        modeled = set(cfg.modeled_ports()) if cfg is not None else None
        rails, biases = {}, {}
        for name, pin in table.pins.items():
            if modeled is not None and name not in modeled:
                continue
            if pin.role == "rail" and pin.dc is not None:
                on = abs(float(pin.dc))
                rails[name] = {"on_a": on, "on_from": f"{pin.src} dc={_eng(pin.dc, 'A')}",
                               "off_a_suggested": max(on / 250.0, 1e-9),
                               "off_note": "not in the netlist -- you set it"}
            elif pin.role == "bias" and pin.dc is not None:
                biases[name] = {"compliance_v": float(pin.dc),
                                "from": f"{pin.src} dc={_eng(pin.dc, 'V')}"}
        if not rails and not biases:
            raise _err("no rail or bias carries a dc value in this netlist.",
                       "The load numbers are read from the dc of the IL_ sources; none of them "
                       "has one.",
                       ["Give each IL_ source a typical load, e.g. "
                        "`IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u`",
                        "Or type the numbers into the three questions by hand"],
                       str(pr.netlist_path()))
        return {"rails": rails, "biases": biases}

    def import_external(self, project: str, body: dict) -> dict:
        dirs = body.get("dirs") or []
        if not isinstance(dirs, list) or not dirs:
            raise _err("no result directories were given.",
                       "The importer matches existing ADE result directories (each with its own "
                       "input.scs and psf) against the plan; it needs at least one.",
                       ["Send {\"dirs\": [\"/path/to/psf/parent\", ...]}"],
                       f"POST /api/p/{project}/import")
        pr = Project(project, self.root)

        def work(job):
            importer = _lazy("pmukit.importer", "Importing existing results")
            fn = _attr(importer, "import_external", "Importing existing results")
            job.say(f"matching {len(dirs)} directories against the plan", 0.2)
            plan = pr.plan()
            with pr.ledger() as led:
                ds = pr.dataset(create=True)
                report = fn([str(d) for d in dirs], plan, led, ds,
                            vset_param=pr.config().vset_param)
            job.say("done", 1.0)
            return {"report": report}

        return {"job": JOBS.submit("import", project, "import external results", work).id}

    # ---------------------------------------------------------------- Plan
    def plan(self, project: str) -> dict:
        if self.demo:
            groups = _demo_groups()
            runs = sum(g["runs"] for g in groups if g["enabled"])
            return {"project": project, "config_sha": "5d8ca1", "derived_sha": "b7e2",
                    "groups": groups, "ticks": {},
                    "cost": {"runs": runs, "cpu_hours": round(
                        sum(g["cpu_hours"] for g in groups if g["enabled"]), 1),
                        "by_analysis": {}},
                    "cells": 9, "cached": 10,
                    "states": [{"key": f"L{i}", "label": lbl, "currents": {}}
                               for i, lbl in enumerate(["a=2u b=20u", "a=100u b=400u",
                                                        "a=500u b=2m", "a=1m b=4m"])],
                    "notes": []}
        pr = Project(project, self.root)
        plan = pr.plan()
        rows = plan.to_rows()
        cached = 0
        try:
            with pr.ledger() as led:
                have = {r.run_id for r in led.all() if r.status in ("done", "imported",
                                                                   "skipped_cached")}
            for row, g in zip(rows, plan.groups):
                row["cached"] = sum(1 for r in g.runs if r.run_id in have)
                cached += row["cached"]
                row["cpu_hours"] = round(row.get("cpu_seconds", 0.0) / 3600.0, 2)
        except PmuError:                                               # pragma: no cover - no db
            for row in rows:
                row["cached"] = 0
                row["cpu_hours"] = round(row.get("cpu_seconds", 0.0) / 3600.0, 2)
        cfg = pr.config()
        cells = len(cfg.corner_names()) * len(cfg.temps_c) * len(cfg.vset_codes)
        return {"project": project, "config_sha": plan.config_sha,
                "derived_sha": plan.derived_sha, "groups": rows,
                "ticks": pr.state().plan_ticks, "cost": plan.cost_summary(),
                "cells": cells, "cached": cached,
                "states": [{"key": s.key, "label": s.label, "currents": s.currents}
                           for s in plan.states],
                "notes": plan.notes}

    def set_plan_groups(self, project: str, body: dict) -> dict:
        pr = Project(project, self.root)
        ticks = body.get("ticks")
        if not isinstance(ticks, dict):
            gid, on = body.get("id"), body.get("on")
            if not gid:
                raise _err("no plan groups were given.",
                           "PUT /plan/groups takes either the whole tick map or one {id, on}.",
                           ["Send {\"ticks\": {\"ac:IL_VDD0P8_A\": false}}",
                            "Or {\"id\": \"ac:IL_VDD0P8_A\", \"on\": false}"],
                           f"PUT /api/p/{project}/plan/groups")
            ticks = dict(pr.state().plan_ticks)
            ticks[str(gid)] = bool(on)
        st = pr.state()
        st.set_ticks(ticks, "plan ticks")
        st.note("plan selection changed", "plan")
        st.save()
        out = self.plan(project)
        out["consequences"] = self.consequences(project)["consequences"]
        out["undoable"] = st.undoable()
        return out

    def consequences(self, project: str) -> dict:
        if self.demo:
            return {"consequences": []}
        plan = Project(project, self.root).plan()
        return {"consequences": plan.consequences()}

    def plan_runs(self, project: str, group: str) -> dict:
        if self.demo:
            rows = []
            for c in DEMO_CORNERS:
                for t in DEMO_TEMPS:
                    rid = jsonio.sha([group, c, t], 12)
                    rows.append({"run_id": rid, "process": c, "temp_c": float(t), "vset": 3,
                                 "load_key": "L2", "load_label": "a=500u b=2m",
                                 "analysis": group.split(":")[0], "stimulus": group.split(":")[-1],
                                 "reads": [], "why": "demo", "cost_s": 300.0})
            return {"group": group, "runs": rows}
        pr = Project(project, self.root)
        plan = pr.plan(apply_ticks=False)
        g = plan.group(group)
        labels = {s.key: s.label for s in plan.states}
        rows = []
        for r in g.runs:
            run = r.run
            rows.append({"run_id": run.run_id, "process": run.process, "temp_c": run.temp_c,
                         "vset": run.vset, "load_key": run.load_key,
                         "load_label": labels.get(run.load_key, run.load_key),
                         "analysis": run.analysis, "stimulus": run.stimulus,
                         "reads": run.reads, "why": r.why(), "cost_s": r.cost_s,
                         "cell_text": run.cell_text()})
        return {"group": group, "title": g.title, "why": g.why, "runs": rows}

    def recipe(self, project: str, run_id: str) -> dict:
        if self.demo:
            row = next((r for r in _demo_ledger_rows() if r["run_id"] == run_id),
                       _demo_ledger_rows()[0])
            return {"run_id": row["run_id"], "recipe": row["recipe"],
                    "lines": _recipe_lines(row["recipe"]), "cell": row["cell_text"],
                    "analysis": row["analysis"]}
        pr = Project(project, self.root)
        text = ""
        cell = analysis = ""
        with pr.ledger() as led:
            run = led.get(run_id)
        if run is not None:
            text, cell, analysis = run.recipe, run.cell_text(), run.analysis
        if not text:
            for r in pr.plan(apply_ticks=False).runs(enabled_only=False):
                if r.run_id == run_id:
                    text = r.run.recipe
                    cell, analysis = r.run.cell_text(), r.run.analysis
                    break
        if not text:
            raise _err(f"no recipe for run {run_id!r}.",
                       "A recipe is written when the run is planned; this id is in neither the "
                       "ledger nor the current plan.",
                       ["Rebuild the plan on the Plan screen",
                        "Check the id against the ledger"],
                       f"GET /api/p/{project}/runs/{run_id}/recipe")
        return {"run_id": run_id, "recipe": text, "lines": _recipe_lines(text),
                "cell": cell, "analysis": analysis}

    def submit(self, project: str, body: dict) -> dict:
        pr = Project(project, self.root)
        commit_only = bool(body.get("commit_only"))
        engine = str(body.get("engine") or "")
        account = str(body.get("account") or "")

        def work(job):
            job.say("writing the enabled runs into the ledger", 0.05)
            plan = pr.plan()
            with pr.ledger() as led:
                counts = plan.commit(led)
            st = pr.state()
            st.go("run").note(f"submitted {counts.get('new', 0)} new runs "
                              f"({counts.get('cached', 0)} already had results)", "plan")
            st.save()
            out = {"committed": counts}
            if commit_only:
                job.say("committed (not submitted: commit_only)", 1.0)
                return out
            try:
                runner_mod = _lazy("pmukit.runner", "Submitting runs")
                Runner = _attr(runner_mod, "Runner", "Submitting runs")
            except NotLanded as nl:
                job.status = "partial"
                job.error = nl.error.to_dict()["error"]
                job.not_landed = nl.module
                job.say("plan committed; no runner to submit with", 1.0)
                return out
            job.say("handing the plan to the runner", 0.1)
            total = max(1, counts.get("new", 0) + counts.get("updated", 0))
            seen = {"n": 0}

            def on_event(kind, run_id="", detail=""):
                """runner.EventFn is (kind, run_id, detail) -- one line per state change."""
                seen["n"] += 1
                text = " ".join(str(x) for x in (kind, str(run_id)[:12], detail) if str(x))
                job.say(text[:300], min(0.98, 0.1 + 0.88 * seen["n"] / total))

            site = pr.site(engine, account)
            job.say(f"backend {site.engine}"
                    + (f", account {site.project_account}" if site.engine == "donau_alps" else ""), 0.12)
            with pr.ledger() as led:
                runner = Runner(pr.name, plan, led, site,
                                root=(pathlib.Path(self.root) / pr.name / "runs"
                                      if self.root is not None else None))
                result = runner.run_all(on_event=on_event)
            out["runner"] = _clean(result)
            out["engine"] = site.engine
            job.say("queue drained", 1.0)
            return out

        return {"job": JOBS.submit("run", project, "submit the plan", work).id}

    # ---------------------------------------------------------------- Run
    def ledger(self, project: str, status: str = "") -> dict:
        if self.demo:
            rows = _demo_ledger_rows()
            if status and status != "all":
                rows = [r for r in rows if r["status"] == status]
            counts = {}
            for r in _demo_ledger_rows():
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            counts.update({"done": 191, "running": 8, "planned": 71, "failed": 2,
                           "skipped_cached": 10})
            return {"rows": rows, "counts": counts, "total": 282, "cpu_hours": 163.2,
                    "not_run": 2}
        pr = Project(project, self.root)
        with pr.ledger() as led:
            runs = led.all(status=(status or None) if status not in ("", "all") else None)
            rows = led.to_rows(runs)
            counts = led.counts_by_status()
            cpu = led.total_cpu_hours()
            not_run = len(led.not_run())
            total = sum(counts.values())
        for row, run in zip(rows, runs):
            row["cell_text"] = run.cell_text()
        return {"rows": rows, "counts": counts, "total": total,
                "cpu_hours": round(cpu, 2), "not_run": not_run}

    def run_detail(self, project: str, run_id: str) -> dict:
        if self.demo:
            row = next((r for r in _demo_ledger_rows() if r["run_id"] == run_id), None)
            if row is None:
                raise _err(f"no run {run_id!r} in the demo ledger.",
                           "Demo mode serves a fixed set of twelve runs.",
                           ["Pick a row from the ledger table"], "demo")
            return {"run": row, "consumes": [["VDD0P8_A", "zout", "Rout"]],
                    "log": _demo_log(row), "why": "demo"}
        pr = Project(project, self.root)
        with pr.ledger() as led:
            run = led.get(run_id)
            if run is None:
                raise _err(f"no run {run_id!r} in the ledger.",
                           "The ledger holds one row per planned or finished simulation; this id "
                           "is not one of them.",
                           ["Pick a row from the ledger table", "Rebuild the plan"],
                           str(pr.dir / "runs.sqlite"))
            consumes = led.consumers(run_id)
            why = led.why(run_id)
            row = run.to_dict()
        row["cell_text"] = run.cell_text()
        lines, _next, _done = _read_log(pr, run, 0, 400)
        return {"run": row, "consumes": [list(c) for c in consumes], "why": why,
                "log": "\n".join(lines)}

    def run_log(self, project: str, run_id: str, offset: int, limit: int) -> dict:
        if self.demo:
            row = next((r for r in _demo_ledger_rows() if r["run_id"] == run_id),
                       _demo_ledger_rows()[0])
            lines = _demo_log(row).split("\n")
            return {"run_id": run_id, "lines": lines[offset:offset + limit],
                    "next_offset": min(len(lines), offset + limit), "done": True,
                    "total": len(lines)}
        pr = Project(project, self.root)
        with pr.ledger() as led:
            run = led.get(run_id)
        if run is None:
            raise _err(f"no run {run_id!r} in the ledger.",
                       "The log route reads the log of a run the ledger knows about.",
                       ["Pick a row from the ledger table"], str(pr.dir / "runs.sqlite"))
        lines, nxt, done = _read_log(pr, run, offset, limit)
        return {"run_id": run_id, "lines": lines, "next_offset": nxt, "done": done,
                "status": run.status}

    def run_action(self, project: str, run_id: str, action: str) -> dict:
        """retry / skip / kill.

        CONTRACTS fixes the status set, so "skip" is not a new status: the run goes back to
        `planned` carrying the reason, which is exactly what `Ledger.not_run()` reports as NOT RUN.
        """
        if self.demo:
            return {"run_id": run_id, "action": action, "status":
                    {"retry": "planned", "skip": "planned", "kill": "failed"}[action],
                    "demo": True}
        pr = Project(project, self.root)
        with pr.ledger() as led:
            run = led.get(run_id)
            if run is None:
                raise _err(f"no run {run_id!r} in the ledger.",
                           "Only a run the ledger knows about can be retried, skipped or killed.",
                           ["Pick a row from the ledger table"], str(pr.dir / "runs.sqlite"))
            if action == "retry":
                led.set_status(run_id, "planned", error="")
                new = "planned"
            elif action == "skip":
                led.set_status(run_id, "planned",
                               error="skipped by the user -- will be reported NOT RUN")
                new = "planned"
            elif action == "kill":
                if run.status not in ("running", "submitted"):
                    raise _err(f"run {run_id} is {run.status}, not running.",
                               "Kill stops work that is in flight; a finished or planned run has "
                               "nothing to stop.",
                               ["Use Retry to run it again",
                                "Use Skip to mark it NOT RUN"], str(pr.dir / "runs.sqlite"))
                led.set_status(run_id, "failed", error="killed from the Run screen",
                               finished=True)
                new = "failed"
            else:
                raise _err(f"unknown run action {action!r}.",
                           "A submitted run is never undone; it is retried, skipped or killed.",
                           ["Use one of: retry, skip, kill"],
                           f"POST /api/p/{project}/runs/{run_id}/<action>")
        st = pr.state()
        st.note(f"run {run_id[:8]} {action}", "run")
        st.save()
        return {"run_id": run_id, "action": action, "status": new}

    def fit(self, project: str, body: dict) -> dict:
        pr = Project(project, self.root)
        tiers = tuple(body.get("tiers") or ("hb", "ls", "en"))

        def work(job):
            fit_mod = _lazy("pmukit.fit", "Fitting the model")
            fn = _attr(fit_mod, "fit_project", "Fitting the model")
            job.say("opening the dataset", 0.05)
            ds = pr.dataset()
            der = pr.derived()
            fitted = {"n": 0}

            def on_event(ev):
                fitted["n"] += 1

            def on_progress(done, total, port, cell):
                # one line per (port, cell) step: the bar moves with the real work instead of
                # sitting at one number for the minutes a full fit takes
                job.say(f"fitting {port} at {_cell_label(cell)} -- step {done + 1} of {total}, "
                        f"{fitted['n']} blocks so far",
                        0.08 + 0.87 * (done / max(1, total)))

            job.say("fitting every block, corner by corner", 0.08)
            result = fn(ds, der, tiers=tiers, on_event=on_event, on_progress=on_progress)
            job.say(f"fitted {fitted['n']} blocks; writing fit.json", 0.97)
            payload = _clean(result.to_dict() if hasattr(result, "to_dict") else result)
            jsonio.write(pr.fit_path, payload)
            st = pr.state()
            st.go("model").note("fit finished", "run")
            st.save()
            job.say("done", 1.0)
            return {"fit": str(pr.fit_path), "summary": _fit_summary(payload)}

        return {"job": JOBS.submit("fit", project, "fit the model", work).id}

    # ---------------------------------------------------------------- Model
    def model_summary(self, project: str) -> dict:
        """The four tiles: what the model is good for, what is held back, what was never run,
        and whether it survives HB. Every number here is read back from the fit and the derived
        config -- the shell invents none of them."""
        if self.demo:
            return {"fitted": True, "graded_by": "demo", "valid": {
                        "load VDD0P8_A": "2 u - 1 mA", "load VDD0P8_B": "20 u - 4 mA",
                        "temp": "-40 - 125 C (continuous)", "freq": "<= 20 GHz",
                        "corners": "tt, ss, ff", "VSET": "3"},
                    "usable_not_signoff": [
                        {"item": "EN power-up ramp",
                         "note": "rails and biases rise with the measured time; use the real LDO "
                                 "for startup sign-off"}],
                    "not_run": [
                        {"item": "tran_load.VDD0P8_B at ss / 125 C",
                         "note": "failed twice; load_en.B at that cell uses the ss / 25 C fit"}],
                    "hb": {"status": "pass", "ran": True, "ok": True,
                           "detail": "first-step residual 7.7e-3",
                           "note": "driven HB, every large-signal term toggled one at a time; "
                                   "none exceeds 10x the all-off residual"},
                    "dataset": "a91fc3", "runs_consumed": 280}
        pr = Project(project, self.root)
        fit = pr.fit_result()
        ver, stale = pr.verify_current()
        if fit is None:
            not_run, done, total = [], 0, 0
            try:
                with pr.ledger() as led:
                    counts = led.counts_by_status()
                    not_run = [{"item": (r.cell_text() + " " + r.analysis), "note": r.error or ""}
                               for r in led.not_run()[:20]]
                    done = (counts.get("done", 0) + counts.get("imported", 0)
                            + counts.get("skipped_cached", 0))
                    total = sum(counts.values())
            except PmuError:                                           # pragma: no cover - no db
                pass
            return {"fitted": False, "done": done, "total": total, "not_run": not_run,
                    "why": "no fit yet -- the Model screen is empty until `fit` has run",
                    "valid": {}, "usable_not_signoff": [], "hb": None}

        blocks = _fit_blocks(fit)
        usable, missing = [], []
        for bf in blocks:
            tier = _tier_of(fit, bf)
            if bf.get("missing"):
                missing.append({"item": "%s.%s at %s" % (bf["port"], bf["block"],
                                                         _cell_label(bf.get("cell"))),
                                "note": (bf.get("notes") or [""])[0]})
            elif tier == "en":
                usable.append({"item": "%s.%s" % (bf["port"], bf["block"]),
                               "note": "usable, not sign-off: it rises with the measured time; "
                                       "use the real PMU for startup sign-off"})
            elif tier == "ls":
                usable.append({"item": "%s.%s" % (bf["port"], bf["block"]),
                               "note": "large-signal term, opt-in: off by default in the emitted "
                                       "model, enabled per instance"})
        seen, uniq = set(), []
        for u in usable:
            if u["item"] in seen:
                continue
            seen.add(u["item"])
            uniq.append(u)
        try:
            valid = _envelope_text(ver.get("envelope") or {}) or _valid_from_derived(pr.derived())
        except PmuError:                                               # pragma: no cover - no cfg
            valid = {}
        runs_consumed = 0
        try:
            with pr.ledger() as led:
                runs_consumed = sum(1 for r in led.all()
                                    if r.status in ("done", "imported", "skipped_cached"))
        except PmuError:                                               # pragma: no cover - no db
            pass
        grid = _grade_grid(fit, ver, stale)
        return {"fitted": True, "valid": valid,
                "graded_by": grid["graded_by"], "verify_stale": bool(stale),
                "usable_not_signoff": uniq, "not_run": missing[:30],
                # verify_project writes the HB report under `hb_check`; reading "hb" returned
                # None forever, so the tile said "not checked" after every check.
                "hb": _hb_summary(ver.get("hb_check")), "dataset": fit.get("dataset_sha", ""),
                "spec_sha": fit.get("spec_sha", ""), "runs_consumed": runs_consumed,
                "blocks": len(blocks)}

    def model_grades(self, project: str) -> dict:
        """The grid: the worst block per port and cell.

        When `verify` has run these are its green / yellow / red. When it has not, the grid still
        shows the truth it has -- `not_run` for a block whose data was never measured, `fitted`
        for one that was -- and says so in `graded_by`. An ungraded block is never coloured green.
        """
        if self.demo:
            cells, rows = [], []
            for c in DEMO_CORNERS:
                for tp in DEMO_TEMPS:
                    cells.append({"corner": c, "temp_c": tp, "label": "%s %g" % (c, tp)})
            for port in DEMO_PORTS:
                grid = []
                for c in DEMO_CORNERS:
                    for tp in DEMO_TEMPS:
                        grid.append({"corner": c, "temp_c": tp,
                                     "grade": DEMO_GRADE.get("%s|%s|%g" % (port, c, tp), "green")})
                rows.append({"port": port, "cells": grid})
            return {"cells": cells, "rows": rows, "grades": [], "fitted": True,
                    "graded_by": "demo"}
        pr = Project(project, self.root)
        fit = pr.fit_result()
        ver, stale = pr.verify_current()
        if fit is None:
            return {"cells": [], "rows": [], "grades": [], "fitted": False, "graded_by": "",
                    "why": "nothing is fitted yet"}
        grid = _grade_grid(fit, ver, stale)
        return {"cells": grid["cells"], "rows": grid["rows"],
                "grades": ver.get("grades") or [], "fitted": True,
                "graded_by": grid["graded_by"], "verify_stale": bool(stale),
                "ungraded": grid["ungraded"], "why": grid["why"]}

    def model_cell(self, project: str, port: str, corner: str, temp: str) -> dict:
        if self.demo:
            grade = DEMO_GRADE.get("%s|%s|%s" % (port, corner, temp), "green")
            blocks = _demo_blocks(port, grade)
            for b in blocks:
                b["cell_key"] = "%s/%sC" % (corner, temp)
            return {"port": port, "corner": corner, "temp_c": temp, "grade": grade,
                    "blocks": blocks, "graded_by": "demo",
                    "runs": [r["run_id"] for r in _demo_ledger_rows()[:4]]}
        pr = Project(project, self.root)
        fit = pr.fit_result()
        ver, stale = pr.verify_current()
        if fit is None:
            raise _err("%s has no fitted model yet." % project,
                       "The per-cell block table is read from the fit; nothing has been fitted.",
                       ["Run the fit from the Run screen"], str(pr.fit_path))
        vgrades = _verify_index(ver)
        port_type = str((fit.get("ports") or {}).get(port) or "rail")
        blocks, worst = [], "green"
        for bf in _fit_blocks(fit):
            if bf["port"] != port:
                continue
            cell = bf.get("cell") or {}
            bcorner = str(cell.get("process") or "")
            if corner and bcorner and bcorner != corner:
                continue
            btemp = _numstr(cell.get("temp_c"))
            if temp not in ("", None) and btemp and btemp != _numstr(temp):
                continue
            # The SAME join as the grid, so a cell's badge and its rows cannot disagree.
            gv = _verify_lookup(vgrades, port, corner or bcorner, _numstr(temp), bf["block"])
            grade = str(gv.get("grade")) if gv else ("not_run" if bf.get("missing") else "fitted")
            # verify grades a block per CORNER (the worst of its cells). The row's own verdict,
            # against the same limit table, says WHICH load / VSET / temperature is the one.
            own = _row_grade(bf, port_type) if gv else ""
            blocks.append({"name": bf["block"], "metric": bf.get("metric", ""),
                           "value": ("not run" if bf.get("missing")
                                     else _numstr(bf.get("score"), 3)),
                           "score": bf.get("score"),
                           "limit": _limit_text(bf.get("metric", "")),
                           "grade": grade, "row_grade": own or grade,
                           "detail": (gv or {}).get("detail", ""),
                           "missing": bool(bf.get("missing")),
                           "n_points": bf.get("n_points", 0),
                           # A block is fitted on the axes ITS parameters vary over, so one
                           # cell of the grid can hold several rows of the same block -- one
                           # per load and per VSET. Both are named so the rows tell apart.
                           "load_a": cell.get("load_a"),
                           "load": ("" if cell.get("load_a") is None
                                    else _eng(cell.get("load_a"), "A")),
                           "vset": cell.get("vset"),
                           "temp": ("" if cell.get("temp_c") is not None else
                                    ("sweep" if bf["block"] in ("dc", "idc") else "")),
                           "cell_key": _cell_key_of(cell),
                           "notes": bf.get("notes") or [],
                           "identifiability": bf.get("identifiability") or {}})
            if _GRADE_RANK.get(grade, 0) > _GRADE_RANK.get(worst, 0):
                worst = grade
        runs = []
        try:
            with pr.ledger() as led:
                runs = [r.run_id for r in led.all(port=port, process=corner or None, limit=40)]
        except PmuError:                                               # pragma: no cover - no db
            pass
        return {"port": port, "corner": corner, "temp_c": temp,
                "grade": worst if blocks else "not_run", "blocks": blocks, "runs": runs,
                "graded_by": ("verify" if vgrades and not any(
                    b["grade"] == "fitted" for b in blocks) else
                    "partial" if vgrades else "fit"),
                "verify_stale": bool(stale),
                "why": "" if blocks else "no fitted block for this port at this cell"}

    def model_curve(self, project: str, port: str, cell: str, block: str) -> dict:
        """The measurement and the model on the SAME points.

        The model side is the fitter's analytic `predict()`. No simulator is started here, ever:
        that is what makes the Model screen instant, and what makes the drawn curve identical to
        the one the emitted .va will produce.
        """
        if self.demo:
            return _demo_curve(port, block, cell or "ss/25c")
        pr = Project(project, self.root)
        fit = pr.fit_result()
        if fit is None:
            raise _err("%s has no fitted model yet." % project,
                       "The model curve is computed from the fitted parameters; nothing has been "
                       "fitted for this project.",
                       ["Run the fit from the Run screen (or `pmukit fit %s`)" % project],
                       str(pr.fit_path))
        fit_mod = _lazy("pmukit.fit", "Drawing the model curve")
        port_type = str((fit.get("ports") or {}).get(port) or "")
        modname = FIT_MODULE.get((port_type, block))
        if modname is None:
            raise _err("there is no %r block on a %s port." % (block, port_type or "?"),
                       "The model spec fixes which blocks a port type has; a curve can only be "
                       "drawn for one of them.",
                       ["Pick one of: " + ", ".join(sorted(b for (pt, b) in FIT_MODULE
                                                           if pt == port_type)) or "(none)"],
                       "GET /api/p/%s/model/curve?block=" % project)
        predict = getattr(getattr(fit_mod, modname, None), "predict", None)
        if predict is None:
            raise NotLanded("pmukit.fit.%s.predict" % modname, "Drawing the model curve",
                            "the fitter has no analytic predict() for this block")
        want = _parse_cell(cell)
        bf = _pick_fit(fit, port, block, want)
        if bf.get("missing"):
            raise _err("%s.%s was never measured at %s." % (port, block,
                                                            _cell_label(bf.get("cell"))),
                       "The fit records this block as NOT RUN: "
                       + (bf.get("notes") or ["the data is not in the dataset"])[0],
                       ["Retry the run behind it on the Run screen",
                        "Or accept it: the report and every .va header name it"],
                       str(pr.fit_path))
        obs = _observable_for(block, port_type)
        if obs is None:
            raise _err("the curve view does not draw %r." % block,
                       "It draws the blocks that have one measured curve against one axis: the "
                       "spectra (zout, psrr, noise, yout) and the temperature laws (dc, idc). A "
                       "transient block needs the load event as context and lives in the report.",
                       ["Pick a spectral block on this cell",
                        "Open report.md for the transient numbers"],
                       "GET /api/p/%s/model/curve?block=%s" % (project, block))
        var = "%s.%s" % (obs, port)
        ds = pr.dataset()
        full = dict(bf.get("cell") or {})
        for k, v in want.items():
            full.setdefault(k, v)
        x = ds.coord(var)
        if x is None:
            raise _err("%s has no coordinate in the dataset." % var,
                       "A curve needs the axis it was measured against; this variable was stored "
                       "without one.",
                       ["Re-import or re-run the measurement behind this block"],
                       str(pr.dataset_path))
        gt = ds.get(var, full)
        spectral = obs.startswith(("ac_", "noise_"))
        kwargs = {"f": x} if spectral else {"T": x}
        if block in ("psrr", "noise") and port_type == "rail":
            zbf = _pick_fit(fit, port, "zout", full, required=False)
            if zbf is None or zbf.get("missing"):
                raise _err("%s.%s cannot be drawn without its zout fit." % (port, block),
                           "The rail PSRR and noise models are shaped by the same output "
                           "impedance, so predict() takes the zout parameters with them.",
                           ["Fit zout for this cell first -- it is part of the same fit run"],
                           str(pr.fit_path))
            kwargs["zout"] = zbf.get("params") or {}
        model = predict(bf.get("params") or {}, **kwargs)
        unit, label = _curve_units(obs, port_type)
        return {"port": port, "block": block, "cell": _cell_key_of(full),
                "cell_label": _cell_label(full),
                "x": _clean(x), "x_label": ("frequency [Hz]" if spectral else "temperature [C]"),
                # a temperature law is a few percent around one value: linear on both axes
                "x_log": bool(spectral), "y_log": bool(spectral), "unit": unit, "label": label,
                "complex": bool(obs.startswith("ac_")),
                "gt": _split_complex(gt), "model": _split_complex(model),
                "points": int(len(x)), "source": var,
                "score": bf.get("score"), "metric": bf.get("metric", "")}

    def verify(self, project: str, body: dict) -> dict:
        pr = Project(project, self.root)

        def work(job):
            ver = _lazy("pmukit.verify", "The HB health check")
            fn = _attr(ver, "verify_project", "The HB health check")
            job.say("grading every block against its limit", 0.2)
            fit = pr.fit_result()
            if fit is None:
                raise _err(f"{project} has no fitted model to verify.",
                           "verify grades a fit; there is none.",
                           ["Run the fit first"], str(pr.fit_path))
            result = fn(pr.name, fit, pr.dataset(), pr.derived())
            payload = _clean(result.to_dict() if hasattr(result, "to_dict") else result)
            jsonio.write(pr.verify_path, payload)
            st = pr.state()
            st.note("verify finished", "model")
            st.save()
            job.say("done", 1.0)
            return {"verify": str(pr.verify_path), "grades": payload.get("grades", [])[:200],
                    # verify_project's key is `hb_check`; reading "hb" silently returned None.
                    "hb": payload.get("hb_check"),
                    "rollup": payload.get("rollup"), "worst": payload.get("worst")}

        return {"job": JOBS.submit("verify", project, "verify the model", work).id}

    # ---------------------------------------------------------------- Deliver
    def deliver(self, project: str, body: dict) -> dict:
        pr = Project(project, self.root)

        def work(job):
            emit = _lazy("pmukit.emit", "Writing the deliverable")
            fn = _attr(emit, "deliver", "Writing the deliverable")
            fit = pr.fit_result()
            if fit is None:
                raise _err(f"{project} has no fitted model to deliver.",
                           "The deliverable is the emitted model: there is nothing to emit until "
                           "the fit has run.",
                           ["Run the fit from the Run screen"], str(pr.fit_path))
            # The same call as `pmukit deliver`: the emitter fits the dataset itself and takes
            # verify.json's grades, so the two deliverables cannot differ.
            job.say("emitting one .va per corner, report.md and envelope.json", 0.3)
            kw = _attr(emit, "verify_inputs", "Writing the deliverable")(pr.verify_result())
            out = fn(pr.name, root=(pathlib.Path(self.root) if self.root is not None
                                    else paths.data_root()), derived=pr.derived(), **kw)
            st = pr.state()
            st.go("deliver").note("deliverable written", "model")
            st.save()
            return {"deliverable": {"path": str(out), "stamp": pathlib.Path(out).name}}

        return {"job": JOBS.submit("deliver", project, "write the deliverable", work).id}

    def deliverables(self, project: str) -> dict:
        if self.demo:
            return {"deliverables": [{
                "stamp": "20260915-140211", "path": "~/pmukit_data/demo_pmu/deliver/20260915-140211",
                "created": "2026-09-15T14:02:11Z",
                "files": [{"name": n, "kind": k, "bytes": b, "desc": d,
                           "sha": jsonio.sha([n, b], 8)} for n, k, b, d in DEMO_FILES],
                "envelope": json.loads(DEMO_FILE_BODY["envelope.json"]),
                "provenance": json.loads(DEMO_FILE_BODY["provenance.json"])}]}
        from .deliverable import Deliverable
        out = []
        for d in Deliverable.list(project, self.root):
            files = []
            for name in d.files():
                p = d.path / name
                files.append({"name": name, "kind": _file_kind(name),
                              "bytes": p.stat().st_size, "sha": jsonio.sha_file(p, 12),
                              "desc": _file_desc(name)})
            out.append({"stamp": d.stamp, "path": str(d.path), "files": files,
                        "envelope": _clean(d.envelope.to_json()),
                        "provenance": _clean(d.provenance.to_json()),
                        "created": _clean(d.provenance.to_json()).get("created", "")})
        return {"deliverables": out}

    def deliverable_file(self, project: str, stamp: str, name: str) -> dict:
        stamp = _safe_name(stamp, STAMP_RE, "deliverable stamp",
                           f"GET /api/p/{project}/deliverables/<stamp>/files/<name>")
        name = _safe_name(name, FILENAME_RE, "file name",
                          f"GET /api/p/{project}/deliverables/{stamp}/files/<name>")
        if self.demo:
            body = DEMO_FILE_BODY.get(name)
            if body is None:
                raise _err(f"{name!r} is not a file of this deliverable.",
                           "Demo mode serves the seven files of one synthetic deliverable.",
                           [f"Pick one of: {', '.join(n for n, _k, _b, _d in DEMO_FILES)}"],
                           "demo")
            return {"stamp": stamp, "name": name, "text": body, "bytes": len(body)}
        from .deliverable import Deliverable
        d = Deliverable.open(Project(project, self.root).dir / "deliver" / stamp)
        text = d.read_file(name)
        return {"stamp": stamp, "name": name, "text": text, "bytes": len(text.encode("utf-8"))}

    # ---------------------------------------------------------------- Digest
    def digest_blocks(self, project: str) -> dict:
        if self.demo:
            blocks = [{"id": i, "title": t, "bytes": b, "priority": p,
                       "included_by_default": d} for i, t, b, p, d in DEMO_DIGEST_BLOCKS]
            return {"blocks": blocks, "budgets": [32000, 64000, 128000],
                    "estimate": {"bytes": sum(x["bytes"] for x in blocks if
                                              x["included_by_default"]),
                                 "parts": 1, "dropped": []}}
        from . import digest as dg
        payload = self._digest_payload(project)
        blocks = dg.blocks_available(payload)
        est = dg.estimate(payload, None, dg.DEFAULT_BUDGET)
        return {"blocks": blocks, "budgets": list(dg.BUDGETS), "estimate": est}

    def digest_export(self, project: str, body: dict) -> dict:
        from . import digest as dg
        budget = int(body.get("budget") or dg.DEFAULT_BUDGET)
        blocks = body.get("blocks")
        if self.demo:
            chosen = [b for b in DEMO_DIGEST_BLOCKS
                      if (blocks is None and b[4]) or (blocks and b[0] in blocks)]
            kept, acc = [], 0
            for b in sorted(chosen, key=lambda x: x[3]):
                if acc + b[2] <= budget:
                    acc += b[2]
                    kept.append(b[0])
            dropped = [b[0] for b in chosen if b[0] not in kept]
            text = "\n".join(
                [f"[pmukit-digest v1] project={project} created={_now()} budget={budget} parts=1"]
                + [f"[{b[0]} {b[1]}]" for b in chosen if b[0] in kept]
                + [f"[D9 trailer] kept {len(kept)}/{len(chosen)} blocks, {acc} bytes"
                   + (f", DROPPED (budget): {', '.join(dropped)}" if dropped else
                      ", nothing dropped")])
            return {"parts": [text], "text": text, "bytes": len(text),
                    "dropped": dropped, "budget": budget}
        payload = self._digest_payload(project)
        parts = dg.export(payload, budget=budget, blocks=blocks, project=project)
        est = dg.estimate(payload, blocks, budget)
        st = Project(project, self.root).state()
        st.note(f"digest exported ({len(parts)} part(s), {est['bytes']} bytes)", "digest")
        st.save()
        return {"parts": parts, "text": "\n".join(parts), "bytes": est["bytes"],
                "dropped": est["dropped"], "budget": budget}

    def digest_import(self, body: dict) -> dict:
        from . import digest as dg
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _err("nothing was pasted.",
                       "digest import reassembles the plain-text parts the box printed; the body "
                       "carried no text.",
                       ["Paste the digest text into the box on this screen"],
                       "POST /api/digest/import")
        payload = dg.parse(text)
        meta = payload.get("meta") or {}
        return {"meta": _clean(meta),
                "summary": {"ledger_rows": len(payload.get("ledger") or []),
                            "grades": len(payload.get("grades") or []),
                            "curves": sorted((payload.get("curves") or {}).keys()),
                            "transients": sorted((payload.get("transients") or {}).keys()),
                            "params": bool(payload.get("params")),
                            "dropped": payload.get("dropped") or []},
                "provenance": _clean(payload.get("provenance") or {})}

    def _digest_payload(self, project: str) -> dict:
        """Assemble the contract-5 payload from whatever this project actually has.

        Missing pieces are simply absent -- the digest names what it dropped, and a block with no
        data is not a block. Nothing here invents numbers.
        """
        pr = Project(project, self.root)
        cfg = pr.config_or_none()
        fit = pr.fit_result() or {}
        ver = pr.verify_result() or {}
        payload: dict = {"meta": {"project": project, "created": _now()},
                         "provenance": {"pmukit_version": _version(),
                                        "config_sha": cfg.sha() if cfg else "",
                                        "dataset_sha": fit.get("dataset_sha", ""),
                                        "spec_sha": fit.get("spec_sha", ""),
                                        "tb_state_note": cfg.state_note if cfg else ""},
                         "ledger": [], "params": fit.get("fits") or {},
                         "grades": ver.get("grades") or [],
                         "trust": ver.get("trust") or {},
                         "curves": {}, "transients": {}, "faillog": []}
        try:
            with pr.ledger() as led:
                for run in led.all():
                    payload["ledger"].append({
                        "run_id": run.run_id, "status": run.status,
                        "cell": run.cell_text().replace(" ", ""),
                        "analysis": f"{run.analysis} {run.stimulus}".strip(),
                        "cpu_s": run.cpu_seconds, "error": run.error})
                    if run.status == "failed":
                        lines, _n, _d = _read_log(pr, run, 0, 40)
                        payload["faillog"].append({
                            "run_id": run.run_id,
                            "netlist_edits": [ln for ln in (run.recipe or "").split("\n")
                                              if ln[:1] in "~+-"],
                            "log_tail": lines})
        except PmuError:                                               # pragma: no cover - no db
            pass
        return payload

    # ---------------------------------------------------------------- global
    def help(self, screen: str) -> dict:
        return helptext.as_dict(screen)

    def cli(self, screen: str, state_json: str, project: str) -> dict:
        st = {}
        if state_json:
            try:
                st = json.loads(state_json)
            except json.JSONDecodeError:
                st = {}
        return {"screen": screen, "cli": cli_echo(screen, st, project)}

    def job(self, job_id: str, since: int = 0) -> dict:
        return JOBS.get(job_id).to_dict(since)


# ============================================================================== small helpers
def _seed_config(project: str, netlist: pathlib.Path, inst: str, table):
    """A first config straight from the netlist, so the New screen has something to show.

    Every value here is read out of the deck, not invented: the corner is the section the
    include lines already carry, the loads are the dc of the IL_ sources. The three questions
    are what the user then corrects.
    """
    from .config import ProjectConfig
    ports, loads = {}, {}
    for name, pin in table.pins.items():
        if pin.is_ground or pin.role == "none":
            # A ground is read from the wiring, and a pin with no convention source has no role
            # to model. Neither is a decision the user has to make on arrival.
            ports[name] = "ignore"
        else:
            # rail / bias are the outputs; supply and enable are the STIMULI -- the PSRR
            # injection and the power-up ramp are measured through them, so they start modeled.
            # Turning one off here would silently delete a whole group from the plan.
            ports[name] = "model"
        if pin.role == "rail" and pin.dc:
            on = abs(float(pin.dc))
            loads[name] = {"on_a": on, "off_a": max(on / 250.0, 1e-9), "switches": True}
    corners = sorted({s for s in table.sections.values() if s}) or ["tt"]
    vset = table.params.get("VSET")
    try:
        codes = [int(float(vset))] if vset is not None else [0]
    except (TypeError, ValueError):
        codes = [0]
    return ProjectConfig.from_dict({
        "project": project, "netlist": str(netlist), "pmu_inst": inst,
        "corners": corners[:1], "temps_c": [-40.0, 25.0, 125.0], "vset_codes": codes,
        "ports": ports, "my_load": loads, "care_up_to_hz": 1e10,
        "state_note": ""}, where=str(netlist))


def _carry_over(cfg, table, old, inst: str):
    """(config, changes) after a netlist re-read or a PMU instance switch.

    Corners, temperatures, codes, the code variable, fmax and the state note are the user's and
    always kept. Per pin, the Model answer, the load and the stub level are kept while the pin
    still exists with the same role. A pin that is new, or whose role changed (an IL_ source
    added in the bench), is seeded the way a first read seeds it; a pin that vanished takes its
    answers with it. A switch to an instance with other pins re-seeds them all. Every one of
    those is listed in `changes`, never done quietly.
    """
    from .config import ProjectConfig
    seed = _seed_config(cfg.project, pathlib.Path(cfg.netlist), inst, table)
    switched = inst != cfg.pmu_inst
    carry = not switched or set(table.pins) == set(cfg.ports)
    compare = old is not None and not switched
    before = list(old.pins) if compare else list(cfg.ports)
    old_role = {n: p.role for n, p in old.pins.items()} if compare else {}
    ports, loads, stub, roles = {}, {}, {}, []
    for name, pin in table.pins.items():
        was = old_role.get(name)
        if was is not None and was != pin.role:
            roles.append({"pin": name, "from": was, "to": pin.role})
        if carry and name in cfg.ports and (was is None or was == pin.role):
            ports[name] = cfg.ports[name]
            if name in cfg.my_load:
                loads[name] = cfg.my_load[name]
            if name in cfg.stub_dc:
                stub[name] = cfg.stub_dc[name]
        else:
            ports[name] = seed.ports[name]
            if name in seed.my_load:
                loads[name] = seed.my_load[name]
    added = [n for n in table.pins if n not in before]
    removed = [n for n in before if n not in table.pins]
    dropped = {"ports": [n for n in cfg.ports if n not in ports],
               "my_load": [n for n in cfg.my_load if n not in loads],
               "stub_dc": [n for n in cfg.stub_dc if n not in stub]}
    d = cfg.to_dict()
    d.update(pmu_inst=inst, ports=ports, my_load={k: v.to_dict() for k, v in loads.items()})
    d.pop("stub_dc", None)
    if stub:
        d["stub_dc"] = stub
    new = ProjectConfig.from_dict(d, where=getattr(cfg, "source_path", "") or "project config")
    if switched:
        text = (f"PMU instance {cfg.pmu_inst} -> {inst}: {len(table.pins)} pins; "
                + ("same pin names, so the pin answers carry over" if carry else
                   "Model column and loads re-seeded for it")
                + "; corners, temperatures, codes and fmax kept")
    else:
        parts = [f"{inst} still found"]
        if added:
            parts.append(f"{len(added)} pin(s) added ({', '.join(added)})")
        if removed:
            parts.append(f"{len(removed)} pin(s) gone ({', '.join(removed)}): their answers "
                         "were dropped")
        if roles:
            parts.append("role changed: " + ", ".join(f"{r['pin']} {r['from']} -> {r['to']}"
                                                      for r in roles) + " (re-seeded)")
        if len(parts) == 1:
            parts.append("same pins, every answer kept")
        text = "; ".join(parts)
    return new, {"first": False, "unchanged": False, "pmu_inst": inst, "prev_inst": cfg.pmu_inst,
                 "inst_switched": switched, "pins_added": added, "pins_removed": removed,
                 "roles_changed": roles, "dropped": dropped, "text": text}


def _demo_config() -> dict:
    return {"project": "demo_pmu", "netlist": "tb/input.scs", "pmu_inst": "PMU_TOP",
            "corners": ["tt", "ss", "ff"], "temps_c": [-40.0, 25.0, 125.0], "vset_codes": [3],
            "ports": {"VDDA_1V0": "model", "VDD0P8_A": "model", "VDD0P8_B": "model",
                      "VDD0P8_C": "stub", "IB_PTAT": "model", "IB_POLY": "model",
                      "EN": "model", "TESTMODE": "ignore"},
            "my_load": {"VDD0P8_A": {"on_a": 5e-4, "off_a": 2e-6, "switches": True},
                        "VDD0P8_B": {"on_a": 2e-3, "off_a": 2e-5, "switches": True}},
            "care_up_to_hz": 2e10, "state_note": "RX mode, register 0x12 = 0x03"}


def _recipe_lines(text: str) -> list[dict]:
    out = []
    for line in (text or "").split("\n"):
        mark = line[:1] if line[:1] in "~+-#" else " "
        out.append({"mark": mark, "text": line})
    return out


def _demo_log(row: dict) -> str:
    status = row["status"]
    if status == "failed":
        return ("spectre 18.1.0.077  -64  -format psfascii\n"
                "tran: tstop=10u  step=2n  (load-EN off)\n"
                "  t=1.9998e-06  step reduced 128x at VDD0P8_B (dV/dt)\n"
                "  t=2.0011e-06  ERROR: timestep too small (1.2e-19)\n"
                "  convergence failure near the IL_VDD0P8_B edge\n"
                f"job FAILED  rc=1  peak mem {row['peak_mem_mb']:.0f} MB")
    if status == "running":
        return ("spectre 18.1.0.077  -64  -format psfascii\n"
                "ac: 10 Hz -> 20 GHz  20 pts/dec\n"
                "  dc op converged in 214 iterations\n"
                "  ac sweep 41% ... 63% ... 78%")
    if status == "skipped_cached":
        return "identical run_id already in the ledger -> reused, nothing simulated"
    if status == "planned":
        return "not submitted yet"
    return ("spectre 18.1.0.077  -64  -format psfascii\n"
            "  dc op converged in 190 iterations\n"
            "  sweep complete\n"
            f"job DONE  rc=0  cpu {row['cpu_seconds']:.0f} s")


def _log_candidates(pr: Project, run) -> list[pathlib.Path]:
    # runner.py lays out <paths.runs_dir>/<run_id>/{input.scs,spectre.log | raw.log,raw/}; on the
    # box runs_dir is under $WORK_ROOT, apart from the project's data
    wd = paths.runs_dir(pr.name) / run.run_id
    out = [wd / "spectre.log", wd / "raw.log", pr.dir / "runs" / run.run_id / "spectre.log",
           pr.dir / "logs" / f"{run.run_id}.log"]
    if run.netlist_path:
        p = pathlib.Path(run.netlist_path)
        if not p.is_absolute():
            p = pr.dir / run.netlist_path
        out += [p.with_suffix(".log"), p.parent / "spectre.log", p.parent / "logFile"]
    if run.psf_path:
        p = pathlib.Path(run.psf_path)
        if not p.is_absolute():
            p = pr.dir / run.psf_path
        out += [p / "logFile", p.parent / "spectre.log"]
    return out


def _read_log(pr: Project, run, offset: int, limit: int) -> tuple[list[str], int, bool]:
    for cand in _log_candidates(pr, run):
        try:
            if cand.is_file():
                lines = cand.read_text(encoding="utf-8", errors="replace").split("\n")
                chunk = lines[offset:offset + limit]
                nxt = min(len(lines), offset + limit)
                done = run.status in ("done", "failed", "imported", "skipped_cached") \
                    and nxt >= len(lines)
                return chunk, nxt, done
        except OSError:                                                # pragma: no cover - fs
            continue
    msg = {"planned": "not submitted yet -- no log",
           "submitted": "waiting for a slot -- the engine has not written a log yet",
           "running": "running -- the log has not been copied back yet",
           "skipped_cached": "identical run_id already had results -> nothing was simulated"}
    text = msg.get(run.status, f"no log file found for {run.run_id}")
    if run.error:
        text += f"\nerror: {run.error}"
    lines = text.split("\n")
    return lines[offset:offset + limit], len(lines), True


def _file_kind(name: str) -> str:
    return {".scs": "Spectre library", ".va": "Verilog-A", ".md": "report",
            ".json": "metadata", ".txt": "text"}.get(pathlib.PurePath(name).suffix, "file")


def _file_desc(name: str) -> str:
    if name == "envelope.json":
        return "load / temp / freq / corner / VSET ranges, and which large-signal terms are on"
    if name == "provenance.json":
        return "config sha, dataset sha, pmukit version, testbench state at characterization"
    if name == "report.md":
        return "trust summary, per-cell grades, HB health check, not-run list"
    if name.endswith(".scs"):
        return "library with one section per corner, each including its .va"
    if name.endswith(".va"):
        return "one corner, temperature continuous, vset and load-EN as instance parameters"
    return ""


def _envelope_text(env: dict) -> dict:
    """envelope.json (pmukit.deliverable.Envelope.to_json) as the Valid-range box's lines.

    The keys are the Envelope's own -- `freq_max_hz`, `vset_codes`. Reading `freq_hz_max` /
    `vset` (the demo's old spelling) silently dropped both lines the moment verify ran."""
    out = {}
    for rail, rng in sorted((env.get("load_a") or {}).items()):
        try:
            out[f"load {rail}"] = _range_text(rng[0], rng[1], "A")
        except (TypeError, IndexError):
            continue
    t = env.get("temp_c")
    if isinstance(t, (list, tuple)) and len(t) == 2:
        out["temp"] = _temp_text(t[0], t[1])
    if env.get("freq_max_hz"):
        out["freq"] = f"<= {_eng(env['freq_max_hz'], 'Hz')}"
    if env.get("corners"):
        out["corners"] = ", ".join(str(c) for c in env["corners"])
    if env.get("vset_codes"):
        out["VSET"] = ", ".join(str(v) for v in env["vset_codes"])
    return out


def _range_text(lo, hi, unit: str) -> str:
    try:
        if float(lo) == float(hi):
            return f"{_eng(lo, unit)} only"
    except (TypeError, ValueError):
        pass
    return f"{_eng(lo, unit)} - {_eng(hi, unit)}"


def _temp_text(lo, hi) -> str:
    """One measured temperature is not a range; saying "25 - 25 C (continuous)" invites a
    user to trust the model at 125 C."""
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return f"{lo} - {hi} C"
    if lo == hi:
        return f"{lo:g} C only"
    return f"{lo:g} - {hi:g} C (continuous)"


# ------------------------------------------------------------------ grades on the Model screen
_GRADE_RANK = {"green": 0, "fitted": 1, "yellow": 2, "not_run": 3, "red": 4}


def _verify_index(ver: dict) -> dict:
    """verify.json's grades keyed (port, corner, temp, block). verify grades per CORNER, so
    `temp` is "" for every row it writes today; a temperature-specific row keeps its own key."""
    out = {}
    for g in ((ver or {}).get("grades") or []):
        if not isinstance(g, dict):
            continue
        key = (str(g.get("port")), str(g.get("corner") or g.get("process") or ""),
               _numstr(g.get("temp_c")), str(g.get("block")))
        prev = out.get(key)
        if prev is None or _GRADE_RANK.get(str(g.get("grade")), 0) > \
                _GRADE_RANK.get(str(prev.get("grade")), 0):
            out[key] = g
    return out


def _verify_lookup(index: dict, port: str, corner: str, temp: str, block: str):
    """The verify grade that judges one (port, corner, temperature, block).

    A temperature-specific grade wins; otherwise the per-corner grade applies to EVERY
    temperature of that corner. (Keying the lookup by the cell's temperature alone never found
    a per-corner grade, so after verify the grid still said FIT everywhere.)  A column with no
    corner at all takes the worst grade the block has on any corner."""
    g = index.get((port, corner, temp, block)) if temp else None
    if g is None:
        g = index.get((port, corner, "", block))
    if g is None and not corner:
        cands = [v for (p, _c, _t, b), v in index.items() if p == port and b == block]
        if cands:
            g = max(cands, key=lambda v: _GRADE_RANK.get(str(v.get("grade")), 0))
    return g


def _row_grade(bf: dict, port_type: str) -> str:
    """One fitted block's own verdict against verify's limit table ('' if it cannot say)."""
    try:
        from .fit._base import BlockFit
        from .verify.grades import grade_block
        grade, _detail = grade_block(BlockFit.from_dict(bf), port_type=port_type or "rail")
        return str(grade)
    except Exception:                                                  # noqa: BLE001 - decoration
        return ""


def _limit_text(metric: str) -> str:
    """The green / yellow bounds verify applies to this metric, e.g. '<= 0.5 / 1 dB'."""
    try:
        from .verify.grades import limit_for
        lim, _exact = limit_for(metric)
    except Exception:                                                  # noqa: BLE001 - decoration
        return "-"
    if lim is None:
        return "-"
    return f"<= {_numstr(lim.green, 3)} / {_numstr(lim.yellow, 3)} {lim.unit}".strip()


def _grade_grid(fit: dict, ver: dict, stale: str = "") -> dict:
    """The grade grid: the worst block per port and (corner, temperature) cell.

    When `verify` has run its green / yellow / red are joined onto EVERY temperature cell of
    their corner (`_verify_lookup`). A block verify did not grade still shows `fitted`, and the
    grid stays `partial` -- provisional -- while any such block is on screen: the banner goes
    away only when every cell shown is judged by verify's limits.
    """
    vgrades = _verify_index(ver)
    blocks = _fit_blocks(fit)
    # The grid's columns are the (corner, temperature) cells that were actually measured.
    # A block fitted on FEWER axes than that is not a column of its own: `dc` is fitted once
    # per corner against the whole temperature sweep, and an emitter constant has no cell at
    # all. Such a block covers every column it is compatible with, which is what the user
    # means by "is this corner good".
    cells, order = {}, []
    for bf in blocks:
        cell = bf.get("cell") or {}
        corner, temp = str(cell.get("process") or ""), _numstr(cell.get("temp_c"))
        if not corner or not temp:
            continue
        ck = (corner, temp)
        if ck not in cells:
            cells[ck] = {"corner": corner, "temp_c": cell.get("temp_c"),
                         "label": (corner + " " + temp).strip()}
            order.append(ck)
    if not order:                           # nothing carries a temperature: one column per corner
        for bf in blocks:
            corner = str((bf.get("cell") or {}).get("process") or "")
            ck = (corner, "")
            if corner and ck not in cells:
                cells[ck] = {"corner": corner, "temp_c": None, "label": corner}
                order.append(ck)
    if not order:
        ck = ("", "")
        cells[ck] = {"corner": "", "temp_c": None, "label": "all cells"}
        order.append(ck)
    ports, ungraded, seen_pb = {}, [], set()
    for bf in blocks:
        cell = bf.get("cell") or {}
        corner, temp = str(cell.get("process") or ""), _numstr(cell.get("temp_c"))
        covers = [k for k in order
                  if (not corner or k[0] == corner) and (not temp or k[1] == temp)]
        slot = ports.setdefault(bf["port"], {})
        seen_pb.add((bf["port"], bf["block"]))
        for ck in covers:
            gv = _verify_lookup(vgrades, bf["port"], ck[0], ck[1], bf["block"])
            if gv is not None:
                grade = str(gv.get("grade"))
            else:
                grade = "not_run" if bf.get("missing") else "fitted"
                if vgrades and grade == "fitted":
                    label = "%s.%s" % (bf["port"], bf["block"])
                    if label not in ungraded:
                        ungraded.append(label)
            prev = slot.get(ck)
            if prev is None or _GRADE_RANK.get(grade, 0) > _GRADE_RANK.get(prev, 0):
                slot[ck] = grade
    # A block verify graded that has no fitted record at all (a group ticked off before the
    # fit) is `not_run` in verify.json; it still belongs on its corner's cells.
    for (p, c, t, b), g in vgrades.items():
        if (p, b) in seen_pb or p not in ports:
            continue
        for ck in order:
            if (not c or ck[0] == c) and (not t or ck[1] == t):
                grade = str(g.get("grade"))
                prev = ports[p].get(ck)
                if prev is None or _GRADE_RANK.get(grade, 0) > _GRADE_RANK.get(prev, 0):
                    ports[p][ck] = grade
    order.sort(key=lambda k: (k[0], float(k[1]) if k[1] else 1e9))
    rows = [{"port": port,
             "cells": [{"corner": cells[k]["corner"], "temp_c": cells[k]["temp_c"],
                        "grade": ports[port].get(k, "not_run")} for k in order]}
            for port in sorted(ports)]
    if not vgrades:
        graded_by = "fit"
        why = ("provisional: the pass/fail limits come from `verify`. Until it runs, a cell says "
               "only whether every block it covers was fitted at all.")
        if stale:
            why = "provisional: " + stale + "."
    elif ungraded:
        # Even a cell that shows yellow may hide an unjudged block that would be red.
        graded_by = "partial"
        why = ("provisional in part: verify has no grade for %s, so every cell those cover is "
               "not fully judged (FIT, or at best the colour of the blocks that were). Re-run "
               "verify to grade them." % (", ".join(ungraded[:6])
                                          + (" and %d more" % (len(ungraded) - 6)
                                             if len(ungraded) > 6 else "")))
    else:
        graded_by, why = "verify", ""
    return {"cells": [cells[k] for k in order], "rows": rows, "graded_by": graded_by,
            "why": why, "ungraded": ungraded}


def _hb_summary(hbr) -> dict | None:
    """verify.json's `hb_check` as the HB tile: {status, ran, ok, detail, note, ...}.

    None only when verify has not run at all. A check that ran on no simulator is NOT a pass:
    it comes back `ran: False` with the reason, and every large-signal term stays off."""
    if not isinstance(hbr, dict) or not hbr:
        return None
    status = str(hbr.get("status") or "not_run")
    terms = [t for t in (hbr.get("terms") or []) if isinstance(t, dict)]
    notes = [str(n) for n in (hbr.get("notes") or [])]
    on = [str(p) for p in (hbr.get("ls_default_on") or [])]
    engine = str(hbr.get("engine") or "")
    if status == "not_run":
        detail = "not run" + (f" (engine {engine})" if engine else "")
        note = notes[0] if notes else "the HB health check did not run"
    elif not terms:
        detail = "passed: no large-signal term to check"
        note = notes[0] if notes else ""
    else:
        failing = [str(t.get("term")) for t in terms if not t.get("pass")]
        base = (hbr.get("baseline") or {}).get("first_step")
        head = (f"{len(terms) - len(failing)} of {len(terms)} large-signal terms pass"
                if failing else f"all {len(terms)} large-signal terms pass")
        detail = head + (f"; all-off first-step residual {_numstr(base, 3)}"
                         if isinstance(base, (int, float)) and math.isfinite(base) else "")
        note = ("on by default: " + (", ".join(on) or "none")
                + ("; opt-in only: " + ", ".join(failing) if failing else "")
                + ". Each term toggled one at a time in a driven HB on " + (engine or "?") + ".")
    return {"status": status, "ran": status != "not_run", "ok": status == "pass",
            "detail": detail, "note": note, "engine": engine, "ls_default_on": on,
            "corner": hbr.get("corner", "")}


def _fit_summary(payload: dict) -> dict:
    blocks = _fit_blocks(payload)
    return {"ports": sorted({b.get("port", "") for b in blocks}),
            "blocks": sorted({b.get("block", "") for b in blocks}),
            "fitted": sum(1 for b in blocks if not b.get("missing")),
            "not_run": sum(1 for b in blocks if b.get("missing")),
            "dataset_sha": payload.get("dataset_sha", "")}


def _parse_cell(cell: str) -> dict:
    """A cell key back into the contract-2 cell dict.

    The canonical spelling is `dataset.cell_key()` -- "tt/25C/vset3/5.0e-04A" -- and that is
    what every payload hands the page, so that is tried first. The loose fallback accepts what a
    person types into the URL ("ss/125c", "tt/25/v3") without turning an unrecognised word into
    a second `process` that silently overwrites the first.
    """
    text = str(cell or "").strip()
    if not text:
        return {}
    try:
        from .dataset import parse_cell_key
        parsed = parse_cell_key(text)
        if parsed:
            return {k: v for k, v in parsed.items() if v is not None}
    except Exception:
        pass
    out: dict = {}
    for piece in text.split("/"):
        piece = piece.strip()
        if not piece:
            continue
        low = piece.lower()
        if low.endswith("c") and _isnum(piece[:-1]):
            out["temp_c"] = float(piece[:-1])
        elif low.startswith("vset") and _isnum(piece[4:]):
            out["vset"] = int(float(piece[4:]))
        elif low.startswith("v") and _isnum(piece[1:]):
            out["vset"] = int(float(piece[1:]))
        elif low.endswith("a") and _isnum(piece[:-1]):
            out["load_a"] = float(piece[:-1])
        elif piece.upper().startswith("L") and piece[1:].isdigit():
            out["load_key"] = piece
        elif _isnum(piece):
            out["temp_c"] = float(piece)
        elif "process" not in out:
            out["process"] = piece
    return out


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


#: which fitter module answers for which (port type, block) -- mirrors pmukit.fit._MODULES.
FIT_MODULE = {("rail", "dc"): "dc", ("rail", "zout"): "zout", ("rail", "psrr"): "psrr",
              ("rail", "noise"): "noise", ("rail", "load_en"): "load_en",
              ("bias", "idc"): "bias", ("bias", "yout"): "bias", ("bias", "noise"): "bias",
              ("bias", "psrr"): "bias", ("en", "ramp"): "en"}

#: the one measured curve each block is drawn against. A block absent here has no single
#: curve-versus-one-axis view (the transients carry their load event with them).
CURVE_OBSERVABLE = {("rail", "zout"): "ac_zout", ("rail", "psrr"): "ac_psrr",
                    ("rail", "noise"): "noise_v", ("rail", "dc"): "dc_temp",
                    ("bias", "yout"): "ac_yout", ("bias", "noise"): "noise_i",
                    ("bias", "psrr"): "ac_psrr", ("bias", "idc"): "dc_temp"}

CURVE_UNITS = {"ac_zout": ("ohm", "|Zout|"), "ac_psrr": ("dB", "PSRR"),
               "ac_yout": ("S", "|Yout|"), "noise_v": ("V^2/Hz", "output noise PSD"),
               "noise_i": ("A^2/Hz", "current noise PSD"),
               "dc_temp": ("A or V", "value versus temperature"),
               "dc_load": ("V", "rail voltage versus load"),
               "dc_iv": ("A", "bias current versus pin voltage")}


def _observable_for(block: str, port_type: str):
    return CURVE_OBSERVABLE.get((port_type, block))


def _curve_units(obs: str, port_type: str = "") -> tuple:
    if obs == "dc_temp":            # rail `dc` is the output voltage, bias `idc` the current
        if port_type == "rail":
            return ("V", "output voltage versus temperature")
        if port_type == "bias":
            return ("A", "bias current versus temperature")
    return CURVE_UNITS.get(obs, ("", obs))


def _fit_blocks(fit: dict) -> list:
    """Every BlockFit of a saved FitResult, as plain dicts, in a stable order."""
    fits = fit.get("fits") if isinstance(fit, dict) else None
    if not isinstance(fits, dict):
        return []
    out = []
    for key in sorted(fits):
        bf = fits[key]
        if not isinstance(bf, dict):
            continue
        bf = dict(bf)
        bf.setdefault("port", str(key).split("/")[0])
        bf.setdefault("block", (str(key).split("/") + ["", ""])[1])
        bf["key"] = key
        out.append(bf)
    return out


def _tier_of(fit: dict, bf: dict) -> str:
    """Which delivery tier a fitted block belongs to (hb / ls / en), via the model spec."""
    port_type = str((fit.get("ports") or {}).get(bf.get("port")) or "")
    if not port_type:
        return ""
    try:
        from . import spec
        return spec.tier_of(str(bf.get("block")), port_type)
    except Exception:                                                  # pragma: no cover - defence
        return ""


def _numstr(value, digits: int = 6) -> str:
    """A number as the same string on both sides of a comparison; '' when there is none."""
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(f):
        return ""
    return ("%.*g" % (digits, f))


def _cell_key_of(cell) -> str:
    """The contract-2 cell key, so the page can hand exactly this cell back on the next call."""
    try:
        from .dataset import cell_key
        return cell_key(dict(cell or {}))
    except Exception:                                                  # pragma: no cover - defence
        return "/".join(f"{k}={v}" for k, v in sorted((cell or {}).items()))


def _cell_label(cell) -> str:
    cell = dict(cell or {})
    bits = []
    if cell.get("process"):
        bits.append(str(cell["process"]))
    if cell.get("temp_c") is not None:
        bits.append(_numstr(cell["temp_c"]) + " C")
    if cell.get("vset") is not None:
        bits.append("vset " + str(cell["vset"]))
    if cell.get("load_a") is not None:
        bits.append("load " + _eng(cell["load_a"], "A"))
    return ", ".join(bits) or "every cell"


def _pick_fit(fit: dict, port: str, block: str, want: dict, *, required: bool = True):
    """The fitted block whose cell best matches what the screen asked for.

    A block is fitted on the axes ITS parameters vary over, so its cell is a *reduction* of the
    project cell: `zout` carries a load, `dc` does not. Matching therefore scores agreement on
    the keys both sides have, instead of demanding an exact key.
    """
    best, best_score = None, -1
    for bf in _fit_blocks(fit):
        if bf.get("port") != port or bf.get("block") != block:
            continue
        cell = bf.get("cell") or {}
        score, ok = 0, True
        for k, v in (want or {}).items():
            if k not in cell:
                continue
            if _numstr(cell[k]) == _numstr(v) or str(cell[k]) == str(v):
                score += 1
            else:
                ok = False
                break
        if not ok:
            continue
        if score > best_score:
            best, best_score = bf, score
    if best is None and required:
        have = sorted({_cell_label(b.get("cell")) for b in _fit_blocks(fit)
                       if b.get("port") == port and b.get("block") == block})
        raise _err(f"no fitted {block} for {port} at {_cell_label(want)}.",
                   "The fit record carries no block at that cell: either the block was never "
                   "fitted there, or the fit is older than the configuration.",
                   ["Re-run the fit",
                    "Cells that do exist for this block: " + (", ".join(have) or "none")],
                   "fit.json")
    return best


def _valid_from_derived(der) -> dict:
    """The validity envelope as the characterization plan defines it, before `verify` writes
    its own. Every entry is a measured range, not a promise."""
    out = {}
    for rail, info in sorted((der.loads or {}).items()):
        pts = [float(a) for a in (info.get("points_a") or [])]
        if pts:
            out[f"load {rail}"] = _range_text(min(pts), max(pts), "A")
    temps = [float(t) for t in ((der.temps_c or {}).get("points") or [])]
    sweep = der.dc_temp_sweep or {}
    if temps:
        out["temp"] = _temp_text(min(temps), max(temps))
    elif sweep.get("start_c") is not None:
        out["temp"] = _temp_text(sweep["start_c"], sweep["stop_c"])
    freq = der.freq or {}
    if freq.get("stop_hz"):
        out["freq"] = "<= " + _eng(freq["stop_hz"], "Hz")
    corners = (der.process or {}).get("corners") or []
    if corners:
        out["corners"] = ", ".join(str(c) for c in corners)
    codes = (der.vset or {}).get("codes") or []
    if codes:
        out["VSET"] = ", ".join(str(v) for v in codes)
    return out


def _split_complex(arr) -> dict:
    """Any array -> {"mag": [...], "phase_deg": [...]} so one chart code path draws everything."""
    if arr is None:
        return {"mag": [], "phase_deg": []}
    values = arr.tolist() if hasattr(arr, "tolist") else list(arr)
    mag, phase = [], []
    for v in values:
        if isinstance(v, complex):
            m = abs(v)
            mag.append(m if math.isfinite(m) else None)
            phase.append(math.degrees(math.atan2(v.imag, v.real)))
        else:
            try:
                f = float(v)
            except (TypeError, ValueError):
                f = float("nan")
            mag.append(f if math.isfinite(f) else None)
            phase.append(0.0)
    return {"mag": mag, "phase_deg": phase}


# ============================================================================== HTTP plumbing
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pmukit/" + _version()
    protocol_version = "HTTP/1.1"

    # -- infrastructure
    def log_message(self, fmt, *args):                                 # pragma: no cover - noise
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(_clean(obj), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, err: PmuError, code: int = 400) -> None:
        self._json(err.to_dict(), code)

    def _guard_host(self) -> bool:
        """Refuse a request whose Host is not the loopback we bound to (DNS rebinding).

        Only applied when bound to loopback: an explicit --host is the user saying 'let another
        machine's browser in'.
        """
        if self.server.host not in ("127.0.0.1", "localhost", "::1"):
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "")

    # -- verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._guard_host():
                self._error(_err("this server only answers requests addressed to localhost.",
                                 "It is bound to the loopback interface with no authentication; "
                                 "a request carrying another Host header is refused so a web page "
                                 "elsewhere cannot drive it.",
                                 ["Open http://127.0.0.1:%d/ in the browser on this machine"
                                  % self.server.server_address[1],
                                  "Start with --host <addr> to serve another machine on purpose"],
                                 "Host header"), 403)
                return
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            handler, args = _match(method, path)
            if handler is None:
                if method == "GET" and not path.startswith("/api/"):
                    self._serve_page()
                    return
                self._error(_err(f"no route {method} {path}.",
                                 "The page talks to the server only through the routes in the "
                                 "interface table; this is not one of them.",
                                 ["Reload the page -- it may be older than the server",
                                  "See docs/OVERNIGHT_BRIEF.md for the route table"],
                                 f"{method} {path}"), 404)
                return
            body = _read_json_body(self) if method in ("POST", "PUT") else {}
            result = handler(self, args, query, body)
            if result is not None:
                self._json(result)
        except NotLanded as nl:
            self._error(nl.error, 501)
        except PmuError as pe:
            self._error(pe, 400)
        except BrokenPipeError:                                        # pragma: no cover - client
            pass
        except Exception as exc:                                       # pragma: no cover - defence
            tb = traceback.format_exc()
            if self.server.verbose:
                sys.stderr.write(tb)
            self._error(_err(f"the server hit an unexpected {type(exc).__name__}: {exc}",
                             "An exception escaped a route handler; this is a bug in pmukit, not "
                             "in your data.",
                             ["Reload the page and try again",
                              "Copy the traceback from the server console to the desk"],
                             f"{method} {self.path}"), 500)

    def _serve_page(self) -> None:
        if not PAGE.is_file():                                         # pragma: no cover - build
            self._error(_err("the web page is missing from this install.",
                             f"{PAGE} does not exist; the package was built without its data "
                             f"files.",
                             ["Reinstall pmukit",
                              "Or run from a source checkout where pmukit/web/index.html exists"],
                             str(PAGE)), 500)
            return
        body = PAGE.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    # -- streaming log (text/plain, flushed as it grows)
    def _stream_log(self, project: str, run_id: str, follow: float) -> None:
        api = self.server.api
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        offset = 0
        end = time.time() + max(0.0, follow)
        try:
            while True:
                data = api.run_log(project, run_id, offset, 500)
                lines = data.get("lines") or []
                if lines:
                    chunk = ("\n".join(lines) + "\n").encode("utf-8")
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                    offset = data.get("next_offset", offset + len(lines))
                if data.get("done") or time.time() >= end:
                    break
                time.sleep(0.4)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):                # pragma: no cover - client
            pass


# ------------------------------------------------------------------ the route table
def _api(fn):
    """Adapter: a route body that just returns JSON."""
    def wrapped(handler, args, query, body):
        return fn(handler.server.api, handler, args, query, body)
    return wrapped


ROUTES: list = []


def route(method: str, pattern: str):
    rx = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, rx, _api(fn)))
        return fn
    return deco


def _match(method: str, path: str):
    for m, rx, fn in ROUTES:
        if m != method:
            continue
        hit = rx.match(path)
        if hit:
            return fn, hit.groupdict()
    return None, {}


# ---- Home
@route("GET", r"/api/projects")
def _r_projects(api, h, a, q, b):
    return api.projects()


@route("POST", r"/api/projects")
def _r_new_project(api, h, a, q, b):
    return api.new_project(b)


@route("GET", r"/api/site")
def _r_site(api, h, a, q, b):
    return api.site_get()


@route("PUT", r"/api/site")
def _r_site_put(api, h, a, q, b):
    return api.site_put(b)


@route("GET", r"/api/machine")
def _r_machine(api, h, a, q, b):
    return _demo_machine() if api.demo else machine()


@route("GET", r"/api/deliverables/diff")
def _r_diff(api, h, a, q, b):
    return api.deliverables_diff(_one(q, "a", ""), _one(q, "b", ""))


# ---- New
@route("POST", r"/api/p/<project>/netlist")
def _r_netlist(api, h, a, q, b):
    return api.load_netlist(a["project"], b)


@route("GET", r"/api/p/<project>/netlist")
def _r_netlist_info(api, h, a, q, b):
    return api.netlist_info(a["project"])


@route("PUT", r"/api/p/<project>/netlist/instance")
def _r_netlist_instance(api, h, a, q, b):
    return api.set_instance(a["project"], b)


@route("POST", r"/api/p/<project>/import")
def _r_import(api, h, a, q, b):
    return api.import_external(a["project"], b)


@route("GET", r"/api/p/<project>/pins")
def _r_pins(api, h, a, q, b):
    return api.pins(a["project"])


@route("PUT", r"/api/p/<project>/pins/<pin>")
def _r_pin(api, h, a, q, b):
    return api.set_pin(a["project"], a["pin"], b)


@route("GET", r"/api/p/<project>/config")
def _r_get_config(api, h, a, q, b):
    return api.get_config(a["project"])


@route("PUT", r"/api/p/<project>/config")
def _r_put_config(api, h, a, q, b):
    return api.put_config(a["project"], b)


@route("GET", r"/api/p/<project>/config/derived")
def _r_derived(api, h, a, q, b):
    return api.derived(a["project"])


@route("POST", r"/api/p/<project>/config/undo")
def _r_undo(api, h, a, q, b):
    return api.undo_config(a["project"])


@route("POST", r"/api/p/<project>/measure-load")
def _r_measure(api, h, a, q, b):
    return api.measure_load(a["project"])


# ---- Plan
@route("GET", r"/api/p/<project>/plan")
def _r_plan(api, h, a, q, b):
    return api.plan(a["project"])


@route("PUT", r"/api/p/<project>/plan/groups")
def _r_plan_groups(api, h, a, q, b):
    return api.set_plan_groups(a["project"], b)


@route("GET", r"/api/p/<project>/plan/consequences")
def _r_consequences(api, h, a, q, b):
    return api.consequences(a["project"])


@route("GET", r"/api/p/<project>/plan/runs")
def _r_plan_runs(api, h, a, q, b):
    group = _one(q, "group", "")
    if not group:
        raise _err("no plan group was named.",
                   "This route lists the runs of one group; without a group there is nothing to "
                   "list.",
                   ["Add ?group=<id>, e.g. ?group=ac:IL_VDD0P8_A"],
                   "GET /api/p/<project>/plan/runs?group=")
    return api.plan_runs(a["project"], group)


@route("GET", r"/api/p/<project>/runs/<run>/recipe")
def _r_recipe(api, h, a, q, b):
    return api.recipe(a["project"], _safe_name(a["run"], RUNID_RE, "run id", "recipe"))


@route("POST", r"/api/p/<project>/submit")
def _r_submit(api, h, a, q, b):
    return api.submit(a["project"], b)


# ---- Run
@route("GET", r"/api/p/<project>/ledger")
def _r_ledger(api, h, a, q, b):
    return api.ledger(a["project"], _one(q, "status", ""))


@route("GET", r"/api/p/<project>/runs/<run>")
def _r_run(api, h, a, q, b):
    return api.run_detail(a["project"], _safe_name(a["run"], RUNID_RE, "run id", "run detail"))


@route("GET", r"/api/p/<project>/runs/<run>/log")
def _r_run_log(api, h, a, q, b):
    run_id = _safe_name(a["run"], RUNID_RE, "run id", "run log")
    follow = _one(q, "follow")
    if follow and str(follow) not in ("0", "false"):
        h._stream_log(a["project"], run_id, min(60.0, float(follow) if _isnum(str(follow))
                                                else 20.0))
        return None
    return api.run_log(a["project"], run_id, int(_one(q, "offset", 0) or 0),
                       min(5000, int(_one(q, "limit", 500) or 500)))


@route("POST", r"/api/p/<project>/runs/<run>/<action>")
def _r_run_action(api, h, a, q, b):
    return api.run_action(a["project"], _safe_name(a["run"], RUNID_RE, "run id", "run action"),
                          a["action"])


@route("POST", r"/api/p/<project>/fit")
def _r_fit(api, h, a, q, b):
    return api.fit(a["project"], b)


# ---- Model
@route("GET", r"/api/p/<project>/model/summary")
def _r_model_summary(api, h, a, q, b):
    return api.model_summary(a["project"])


@route("GET", r"/api/p/<project>/model/grades")
def _r_model_grades(api, h, a, q, b):
    return api.model_grades(a["project"])


@route("GET", r"/api/p/<project>/model/cell")
def _r_model_cell(api, h, a, q, b):
    return api.model_cell(a["project"], _one(q, "port", ""), _one(q, "corner", ""),
                          _one(q, "temp", ""))


@route("GET", r"/api/p/<project>/model/curve")
def _r_model_curve(api, h, a, q, b):
    return api.model_curve(a["project"], _one(q, "port", ""), _one(q, "cell", ""),
                           _one(q, "block", "zout"))


@route("POST", r"/api/p/<project>/verify")
def _r_verify(api, h, a, q, b):
    return api.verify(a["project"], b)


# ---- Deliver
@route("POST", r"/api/p/<project>/deliver")
def _r_deliver(api, h, a, q, b):
    return api.deliver(a["project"], b)


@route("GET", r"/api/p/<project>/deliverables")
def _r_deliverables(api, h, a, q, b):
    return api.deliverables(a["project"])


@route("GET", r"/api/p/<project>/deliverables/<stamp>/files/<name>")
def _r_deliverable_file(api, h, a, q, b):
    return api.deliverable_file(a["project"], a["stamp"], a["name"])


# ---- Digest
@route("GET", r"/api/p/<project>/digest/blocks")
def _r_digest_blocks(api, h, a, q, b):
    return api.digest_blocks(a["project"])


@route("POST", r"/api/p/<project>/digest")
def _r_digest(api, h, a, q, b):
    return api.digest_export(a["project"], b)


@route("POST", r"/api/digest/import")
def _r_digest_import(api, h, a, q, b):
    return api.digest_import(b)


# ---- global
@route("GET", r"/api/help/<screen>")
def _r_help(api, h, a, q, b):
    return api.help(a["screen"])


@route("GET", r"/api/cli")
def _r_cli(api, h, a, q, b):
    return api.cli(_one(q, "screen", "home"), _one(q, "state", ""), _one(q, "project", ""))


@route("GET", r"/api/jobs/<job>")
def _r_job(api, h, a, q, b):
    return api.job(a["job"], int(_one(q, "since", 0) or 0))


@route("GET", r"/api/jobs")
def _r_jobs(api, h, a, q, b):
    return {"jobs": JOBS.recent(_one(q, "project", ""))}


@route("GET", r"/api/state/<project>")
def _r_state(api, h, a, q, b):
    """Not in the table; the page's own bookmark so a reload lands on the same screen."""
    if api.demo:
        return {"project": a["project"], "screen": "home", "exists": True, "recent": [],
                "plan_ticks": {}, "answers": {}, "demo": True}
    st = Project(a["project"], api.root).state()
    return {"project": st.project, "screen": st.screen, "exists": st.exists,
            "recent": st.recent, "plan_ticks": st.plan_ticks, "answers": st.answers,
            "netlist": st.netlist, "undoable": st.undoable(), "last_job": st.last_job}


@route("PUT", r"/api/state/<project>")
def _r_put_state(api, h, a, q, b):
    if api.demo:
        return {"ok": True, "demo": True}
    st = Project(a["project"], api.root).state()
    if b.get("screen"):
        st.go(str(b["screen"]))
    if isinstance(b.get("answers"), dict):
        st.answers = b["answers"]
    if b.get("last_job"):
        st.last_job = str(b["last_job"])
    if b.get("note"):
        st.note(str(b["note"]))
    st.save()
    return {"project": st.project, "screen": st.screen}


# ============================================================================== server
class PmuServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    # POSIX: reuse the address so a restart does not trip over TIME_WAIT.
    # Windows: SO_REUSEADDR there means "steal a port someone else is already listening on", so
    # it is left OFF -- otherwise a second `pmukit ui` would silently hijack the first one's port
    # instead of auto-incrementing to a free one.
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, handler, *, api: Api, verbose: bool = False) -> None:
        super().__init__(addr, handler)
        self.api = api
        self.host = addr[0]
        self.verbose = verbose


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, demo: bool = False,
                root=None, verbose: bool = False, tries: int = PORT_TRIES,
                project: str | None = None) -> PmuServer:
    """Bind, auto-incrementing the port while it is busy. Loopback unless `host` says otherwise.
    `project` is the one the page opens on when its URL names none (`pmukit open`)."""
    api = Api(demo=demo, root=root, project=project)
    last = None
    for i in range(max(1, tries)):
        try:
            return PmuServer((host, port + i), _Handler, api=api, verbose=verbose)
        except OSError as exc:
            last = exc
            continue
    raise _err(f"could not bind {host}:{port}..{port + tries - 1}.",
               f"Every port in the range is in use ({last}).",
               [f"Stop the other pmukit server, or pass --port <free port>"],
               f"{host}:{port}")


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, demo: bool = False,
          open_browser: bool = False, root=None, verbose: bool = False,
          project: str | None = None) -> None:
    srv = make_server(host, port, demo=demo, root=root, verbose=verbose, project=project)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{srv.server_address[1]}/"
    if project:
        url += f"?project={urllib.parse.quote(str(project))}"
    # flush: on the box this is read off a tcsh terminal, and it is often piped into tee or a
    # log. A URL that only appears when the process exits is a URL nobody can open.
    print(f"pmukit {_version()} -- {url}", flush=True)
    print(f"  data:  {'(demo, nothing is read or written)' if demo else paths.data_root()}",
          flush=True)
    print(f"  bound: {srv.server_address[0]}:{srv.server_address[1]}"
          f"{'  (loopback only)' if host == DEFAULT_HOST else ''}", flush=True)
    print("  stop:  Ctrl-C", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                                              # pragma: no cover - env
            print("  (could not open a browser; paste the URL above)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()


def main(argv=None, *, host=None, port=None, demo=None, open_browser=None, project=None,
         data=None, verbose=None) -> int:
    """Entry point for both callers.

    `python -m pmukit.server --demo` parses argv; `pmukit ui` (pmukit.cli) calls it with
    keywords. Any keyword given wins over the parsed value, so the CLI never has to build an
    argv list just to pass four flags through.
    """
    ap = argparse.ArgumentParser(prog="pmukit ui", description="the pmukit web shell")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="interface to bind (default 127.0.0.1: loopback only)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port (default {DEFAULT_PORT}; the next free one is used when busy)")
    ap.add_argument("--demo", action="store_true",
                    help="serve a synthetic PMU; $PMUKIT_DATA is never touched")
    ap.add_argument("--open", action="store_true", help="open a browser on the URL")
    ap.add_argument("--data", default=None, help="override $PMUKIT_DATA for this process")
    ap.add_argument("--project", default=None,
                    help="open the page on this project instead of the most recent one")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    kw_call = any(v is not None for v in (host, port, demo, open_browser, project, data, verbose))
    args = ap.parse_args([] if (argv is None and kw_call) else argv)
    if host is not None:
        args.host = host
    if port is not None:
        args.port = int(port)
    if demo is not None:
        args.demo = bool(demo)
    if open_browser is not None:
        args.open = bool(open_browser)
    if project is not None:
        args.project = project
    if data is not None:
        args.data = data
    if verbose is not None:
        args.verbose = bool(verbose)
    if args.data:
        os.environ["PMUKIT_DATA"] = args.data
    serve(args.host, args.port, demo=args.demo, open_browser=args.open,
          verbose=args.verbose, project=args.project)
    return 0


if __name__ == "__main__":                                             # pragma: no cover - entry
    raise SystemExit(main())
