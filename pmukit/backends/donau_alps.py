# from LDO_modeling/cadence/cluster/donau.py @ d2c5b80
# from LDO_modeling/cadence/cluster/alps_cli.py @ d2c5b80
# from LDO_modeling/cadence/cluster/run_corner.py @ d2c5b80
"""The red-zone engine: submit into the Donau queue, run ALPS on the compute node.

Donau is the site scheduler (an LSF-bsub-flavoured fork): `dsub` submits, `djob` reports, `dkill`
cancels, `dpeek` tails.  ALPS (Empyrean) is the engine ADE's "Use ALPS" checkbox selects; it reads
the same Spectre-syntax netlist and writes classic PSF.

Every flag below is a scar (TOOL_FACTS "ALPS / Donau"), and the validated line is::

    dsub -A <account> -q <queue> -R "cpu=8;mem=8000" -x all -EP <netdir> -J \\
         <alps_root>/bin/alps input.scs -format ps -o raw -I <pdk>/alps \\
         -ahdllibdir <ahd> -mt 8 -ade

  * `<alps_root>/bin/alps` is the bash **WRAPPER**, never the raw binary -- the raw binary dies
    with `libsvadv.so: cannot open shared object file` on the compute node because only the
    wrapper sets LD_LIBRARY_PATH.
  * `-format ps` is classic PSF (the hidden flag; ADE's psfxl is downgraded to ps for ALPS).
    **Never psfxl** -- `pmukit.binpsf` reads classic PSF.
  * `-ade` gives ADE-style output names plus the 0-byte `.simDone` completion sentinel that
    `poll()` watches.
  * `-mt N` MUST equal Donau `cpu=N`, or the node is oversubscribed.  It is taken from
    `site.cpus`, and the resource string is built from the same number so they cannot drift.
  * `-x all` propagates the submit shell's environment (FlexLM `LM_LICENSE_FILE`) to the node --
    without it even the wrapper cannot license.
  * `-I` is a **DIRECTORY** (`$PDK_ROOT` -> `-I $PDK_ROOT/alps`).  Passing the `toplevel.scs`
    FILE produces `-I .../toplevel.scs/alps` and every model silently fails to resolve.

Site facts that are NOT in `site.json` come from the environment, because they are properties of
the box's install tree and must never be committed to this repo.  `pmukit.sitenv` resolves them,
reading the box's OWN variables first-class (`pmukit site` shows which one each came from):

    ALPS install root  $PMUKIT_ALPS_ROOT, else $ALPS_ROOT, else $ALPS_HOME minus /tools/alps,
                       else `which alps`                                            (required)
    PDK model root     $PMUKIT_PDK_ROOT, else $PDK, else $PDK_HOME
                       (optional; omit if the deck's includes are self-contained)
    Donau account      $PMUKIT_DONAU_ACCOUNT, else site.json project_account
    simulator          $PMUKIT_SIMULATOR / $PMUKIT_CLUSTER_ENGINE, else site.json simulator,
                       else 'alps'  (Spectre licenses are scarce at the site)
    PMUKIT_AHDLLIBDIR  a pre-compiled AHDL/VA model DB (optional; omitted -> auto-compile)
    PMUKIT_DONAU_MEM   the mem= MB in `-R` (default 8000)

**This backend has never been executed.**  There is no Donau on the desk, so it is exercised in
dry-run only (the composed command is asserted against the shape above).  Its first real run is on
the box.
"""
from __future__ import annotations

import json as _json
import os
import pathlib
import re
import shlex
import shutil
import subprocess

from .. import sitenv
from ..errors import PmuError

__all__ = ["DonauAlpsBackend", "build_dsub_cmd", "build_sim_cmd", "map_state", "parse_job_id",
           "SIMDONE", "engine_model_tree"]

SIMDONE = ".simDone"          # the 0-byte sentinel `-ade` drops in the -o dir when the run ends
INPUT_SCS = "input.scs"
PSF_DIRNAME = "raw"

#: Donau / LSF state words -> the four states this backend reports upward.  Both spellings are
#: recognised, case-insensitively, so a fork's wording cannot silently fall through.
_STATE_MAP = {
    "pending": "pending", "pend": "pending", "queued": "pending", "waiting": "pending",
    "configuring": "pending", "submitted": "pending", "suspended": "pending", "psusp": "pending",
    "running": "running", "run": "running", "started": "running", "active": "running",
    "done": "done", "succeeded": "done", "success": "done", "completed": "done",
    "complete": "done", "finished": "done", "exit_ok": "done",
    "failed": "failed", "fail": "failed", "exit": "failed", "error": "failed",
    "killed": "failed", "cancelled": "failed", "canceled": "failed", "aborted": "failed",
    "timeout": "failed", "exited": "failed",
}

#: `JOBID 37238970`, `"jobId":"37322154"`, `"jobId": 372`, `"id": 372`.
_JOBID_RE = re.compile(r'(?:job[\s_]*id|"id)["\s]*[:=]?\s*"?(\d+)', re.IGNORECASE)


# --------------------------------------------------------------------------- command building
def alps_exe(alps_root: str) -> str:
    """`<root>/bin/alps` -- the bash WRAPPER, whichever spelling of the root was configured."""
    w = str(alps_root).rstrip("/")
    if w.endswith("/bin/alps"):
        return w
    if w.endswith("/bin"):
        return w + "/alps"
    return w + "/bin/alps"


def engine_model_tree(model_dir: str, engine: str) -> str:
    """The `-I` include SEARCH DIRECTORY for the engine's PDK subtree.

    `-I` is a directory (where `include "toplevel.scs"` resolves), never a file.  In order:
    a `.scs` FILE -> its containing directory (the common footgun: pasting the model file makes
    `.../toplevel.scs/alps`); a path whose leaf already IS the engine name -> as-is; otherwise
    the model ROOT -> append `/<engine>`.
    """
    d = str(model_dir).rstrip("/")
    if d.lower().endswith(".scs"):
        return d.rsplit("/", 1)[0] if "/" in d else "."
    if d.rsplit("/", 1)[-1] == engine:
        return d
    return f"{d}/{engine}"


def build_sim_cmd(engine: str, input_scs: str, out_psf: str, *, alps_root: str = "",
                  model_dir: str = "", ahdllibdir: str = "", mt: int = 8,
                  ade: bool = True) -> list[str]:
    """The engine invocation Donau wraps, as an argv list."""
    if engine not in ("alps", "spectre"):
        raise PmuError(
            what=f"Unknown cluster engine {engine!r}.",
            why="The Donau payload is either ALPS (the validated path) or Cadence Spectre.",
            do=["Set PMUKIT_CLUSTER_ENGINE to 'alps' or 'spectre'."],
            where="pmukit/backends/donau_alps.py")
    inc = ["-I", engine_model_tree(model_dir, engine)] if model_dir else []
    ahdl = ["-ahdllibdir", str(ahdllibdir)] if ahdllibdir else []
    if engine == "alps":
        if not alps_root:
            raise PmuError(
                what="No ALPS install root: none of PMUKIT_ALPS_ROOT, ALPS_ROOT, ALPS_HOME is set.",
                why="ALPS must be launched through its bash WRAPPER (<root>/bin/alps): the raw "
                    "binary cannot find libsvadv.so on a compute node because only the wrapper "
                    "sets LD_LIBRARY_PATH.",
                do=["Source the site's ALPS setup (it exports ALPS_ROOT), or set "
                    "PMUKIT_ALPS_ROOT to the ALPS install root (the directory that contains "
                    "bin/alps).",
                    "`which alps` on the box prints the wrapper -- its parent's parent is the "
                    "root."],
                where="environment: PMUKIT_ALPS_ROOT")
        cmd = [alps_exe(alps_root), str(input_scs),
               "-format", "ps",            # classic PSF, the hidden flag; NEVER psfxl
               "-o", str(out_psf),
               *inc, *ahdl,
               "-mt", str(int(mt))]        # MUST equal Donau cpu=N
        if ade:
            cmd.append("-ade")             # ADE output names + the .simDone sentinel
        return cmd
    # Spectre fallback: flags INFERRED from ADE, not yet confirmed against a real box run.
    return ["spectre", str(input_scs), "-format", "psfascii", "-raw", str(out_psf),
            *inc, *ahdl, f"+mt={int(mt)}", "+aps"]


def build_dsub_cmd(payload: list, netlistdir, *, account: str, queue: str, resource: str,
                   x_all: bool = True, block: bool = False, json: bool = True) -> list[str]:
    """`dsub ... <payload>` as an argv list.

    `-EP <netlistdir>` is the task working directory on the node, so the payload's relative
    `-o raw` resolves there.  `-J` asks for JSON so the JOBID is parseable.  `-I` (attach and
    block) is off by default: the runner polls instead, so a dropped session cannot kill a job.
    """
    if not isinstance(payload, (list, tuple)):
        raise PmuError(
            what="The Donau payload must be an argv list, not a string.",
            why="dsub is exec'd, not shelled; a string would be passed as one argument.",
            do=["Pass build_sim_cmd(...) straight through."],
            where="pmukit/backends/donau_alps.py")
    cmd = ["dsub", "-A", str(account), "-q", str(queue), "-R", str(resource)]
    if x_all:
        cmd += ["-x", "all"]               # carry FlexLM + EDA env to the node
    cmd += ["-EP", str(netlistdir)]
    if json:
        cmd += ["-J"]
    if block:
        cmd += ["-I"]
    return cmd + [str(x) for x in payload]


def map_state(raw) -> str | None:
    """A raw `djob` reply -> pending | running | done | failed, or None when it says nothing."""
    if raw is None:
        return None
    text = str(raw).lower()
    m = re.search(r"\b(?:state|status|stat)\s*[:=]?\s*([a-z_]+)", text)
    if m and m.group(1) in _STATE_MAP:
        return _STATE_MAP[m.group(1)]
    # An exit CODE is not the LSF state word EXIT: `Exit: 0` means clean completion.  This must
    # win over the bare-token scan below, which would read "exit" as failed and report a
    # successful run as a failure.
    me = re.search(r"\bexit(?:ed)?\b\s*(?:code|status|:|=)?\s*(\d+)\b", text)
    if me:
        return "done" if int(me.group(1)) == 0 else "failed"
    for tok, mapped in _STATE_MAP.items():
        if re.search(rf"\b{re.escape(tok)}\b", text):
            return mapped
    return None


def parse_job_id(stdout: str) -> str | None:
    """The numeric JOBID out of dsub stdout: the JSON envelope first, then the streamed form."""
    text = stdout or ""
    try:
        obj = _json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        for src in (data, obj):
            for k in ("jobId", "jobid", "JOBID", "job_id", "id"):
                v = src.get(k)
                if v is not None and re.fullmatch(r"\d+", str(v).strip()):
                    return str(v).strip()
    m = _JOBID_RE.search(text)
    return m.group(1) if m else None


class _Subprocess:
    """The real command executor.  Only ever fires on the box; tests inject a fake."""

    def __call__(self, argv, timeout=None):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- the backend
class DonauAlpsBackend:
    """Submit one deck per run into Donau; ALPS runs it; the PSF lands on the shared filesystem.

    NOT YET RUN FOR REAL: there is no Donau on the desk.  `dry_run=True` composes and returns the
    command without executing anything, which is how it is exercised here.
    """

    name = "donau_alps"

    def __init__(self, site, *, runner=None, dry_run: bool = False,
                 timeout_s: float | None = None, env=None):
        self.site = site
        self.timeout_s = timeout_s
        self.dry_run = bool(dry_run)
        self._run = runner or _Subprocess()
        env = os.environ if env is None else env
        self.engine = sitenv.simulator(site, env).value
        # `which alps` is a fallback for the live box only; an injected env means a test.
        self.alps_root = sitenv.alps_root(env, which=shutil.which if env is os.environ else None).value
        self.pdk_root = sitenv.pdk_root(env).value
        self.ahdllibdir = str(env.get("PMUKIT_AHDLLIBDIR", ""))
        self.account = sitenv.account(site, env).value
        self.mem_mb = int(env.get("PMUKIT_DONAU_MEM", "8000") or 8000)
        self.queue = str(getattr(site, "queue", "") or "")
        self.cpus = int(getattr(site, "cpus", 8) or 8)
        self._state: dict[str, str] = {}

    # ------------------------------------------------------------------ command
    @property
    def resource(self) -> str:
        """`cpu=N;mem=M`.  N is the SAME number that becomes `-mt N`, so they cannot drift."""
        return f"cpu={self.cpus};mem={self.mem_mb}"

    def dsub_command(self, job) -> list[str]:
        """The full argv this run would be submitted with.  Pure text: nothing is executed."""
        self._require_config()
        # The node is always Linux, so the task directory goes over as a POSIX path even when
        # the command was composed on Windows.
        wd = pathlib.PurePosixPath(pathlib.Path(job.workdir).as_posix())
        payload = build_sim_cmd(self.engine, INPUT_SCS, PSF_DIRNAME,
                                alps_root=self.alps_root, model_dir=self.pdk_root,
                                ahdllibdir=self.ahdllibdir, mt=self.cpus,
                                ade=(self.engine == "alps"))
        return build_dsub_cmd(payload, wd, account=self.account, queue=self.queue,
                              resource=self.resource, x_all=True, block=False, json=True)

    def _require_config(self) -> None:
        if not self.account.strip():
            raise PmuError(
                what="No Donau account is configured.",
                why="`dsub -A <account>` names the resource class the job is charged to; there is "
                    "no default and the submit is rejected without it.",
                do=["Set PMUKIT_DONAU_ACCOUNT, or `project_account` in site.json.",
                    "The account is a site fact and is deliberately NOT committed to this repo."],
                where="environment: PMUKIT_DONAU_ACCOUNT / site.json: project_account")
        if not self.queue.strip():
            raise PmuError(
                what="No Donau queue is configured.",
                why="`dsub -q <queue>` picks the work queue; there is no default queue.",
                do=["Set `queue` in site.json (the site's short queue is the usual one)."],
                where="site.json: queue")

    # ------------------------------------------------------------------ interface
    def available(self) -> tuple[bool, str]:
        if self.dry_run:
            return True, "dry-run: the dsub command is composed, nothing is submitted"
        missing = [t for t in ("dsub", "djob", "dkill") if shutil.which(t) is None]
        if missing:
            return False, (f"the Donau client is not on PATH ({', '.join(missing)}) -- this "
                           "engine only exists on the box")
        if not self.alps_root and self.engine == "alps":
            return False, "no ALPS install root (PMUKIT_ALPS_ROOT / ALPS_ROOT / ALPS_HOME)"
        if self.engine == "alps" and not os.path.isfile(alps_exe(self.alps_root)):
            return False, f"the ALPS wrapper {alps_exe(self.alps_root)} does not exist"
        if not self.account.strip():
            return False, "no Donau account (PMUKIT_DONAU_ACCOUNT / site.project_account)"
        if not self.queue.strip():
            return False, "no Donau queue (site.json: queue)"
        return True, (f"dsub -A {self.account} -q {self.queue} -R \"{self.resource}\", "
                      f"engine {self.engine}")

    def submit(self, job) -> str:
        cmd = self.dsub_command(job)
        job.detail = shlex.join(str(x) for x in cmd)
        if self.dry_run:
            job.state = "skipped"
            job.job_id = ""
            return ""
        res = self._run(cmd, timeout=self.timeout_s)
        if getattr(res, "returncode", 1) != 0:
            raise PmuError(
                what="The Donau submit failed.",
                why=f"`dsub` exited {res.returncode}: "
                    f"{(getattr(res, 'stderr', '') or getattr(res, 'stdout', '')).strip()[:400]}",
                do=["Check the account / queue / resource string against the site's own docs.",
                    f"Reproduce by hand: {job.detail}"],
                where=f"dsub for run {job.run.run_id}")
        job_id = parse_job_id(getattr(res, "stdout", "") or "")
        if job_id is None:
            raise PmuError(
                what="Donau accepted the job but pmukit could not read its JOBID.",
                why="`dsub -J` answers with a JSON envelope carrying data.jobId; this reply had "
                    f"neither that nor a streamed `JOBID <n>`: "
                    f"{(getattr(res, 'stdout', '') or '')[:300]!r}",
                do=["Check the job with `djob` by hand and retry.",
                    "If this dsub build reports the id differently, extend parse_job_id()."],
                where=f"dsub for run {job.run.run_id}")
        job.job_id = job_id
        job.state = "running"
        self._state[job_id] = "pending"
        return job_id

    def poll(self, job) -> str:
        if self.dry_run:
            return "skipped"
        res = self._run(["djob", str(job.job_id)], timeout=self.timeout_s)
        raw = (getattr(res, "stdout", "") or "") + "\n" + (getattr(res, "stderr", "") or "")
        state = map_state(raw) or self._state.get(job.job_id) or "pending"
        self._state[job.job_id] = state
        job.detail = raw.strip().splitlines()[-1][:200] if raw.strip() else ""
        if state == "done":
            return "done"
        if state == "failed":
            job.detail = self._peek(job) or job.detail
            return "failed"
        return "running"                   # pending and running are both "not finished yet"

    def fetch(self, job) -> pathlib.Path:
        """No transfer: the box's queue writes onto the shared filesystem the run dir is on.

        The HARD gate is a non-empty PSF directory -- a job that says `done` and wrote nothing hit
        a netlist or model error that only the ALPS log explains.  The `-ade` `.simDone` sentinel
        is a SOFT signal: some ALPS builds do not drop it, so its absence on a job that DID write
        output is a note, not a failure.
        """
        wd = pathlib.Path(job.workdir)
        job.log_path = next((p for p in (wd / "spectre.log", wd / "alps.log", wd / "logFile")
                             if p.is_file()), None) or _newest_log(wd, wd / PSF_DIRNAME)
        psf = wd / PSF_DIRNAME
        if self.dry_run:
            return psf
        if not psf.is_dir() or not any(psf.iterdir()):
            job.state = "failed"
            job.detail = (f"job {job.job_id} reported done but {psf} is empty -- the simulation "
                          "wrote no output; read the ALPS log in the run directory."
                          + ("\n" + self._peek(job) if self._peek(job) else ""))
            return psf
        if self.engine == "alps" and not (psf / SIMDONE).exists():
            job.detail = (f"note: no {SIMDONE} sentinel in {psf}, but output was written -- "
                          "accepting (not every ALPS build drops it)")
        return psf

    def kill(self, job) -> None:
        if self.dry_run or not job.job_id:
            return
        try:
            self._run(["dkill", str(job.job_id)], timeout=self.timeout_s)
        except Exception:                              # noqa: BLE001 -- kill is best effort
            return

    def _peek(self, job) -> str:
        """Best-effort `dpeek` tail: the scheduler's view of why a job died."""
        if self.dry_run or not job.job_id:
            return ""
        try:
            res = self._run(["dpeek", str(job.job_id)], timeout=self.timeout_s)
        except Exception:                              # noqa: BLE001 -- diagnostic only
            return ""
        text = (getattr(res, "stdout", "") or getattr(res, "stderr", "") or "")
        return "\n".join(text.splitlines()[-40:])


def _newest_log(*dirs) -> pathlib.Path | None:
    """The freshest ALPS/Spectre log across `dirs`.

    When a Donau job fails, `dpeek` only says "job <id> FAILED" -- the SCHEDULER's view.  The real
    reason (a parse error, a missing model, a non-convergent operating point) is in the engine's
    own log, which ALPS writes in the run cwd and sometimes in the `-o` directory.
    """
    cands = []
    for d in dirs:
        p = pathlib.Path(d)
        if p.is_dir():
            for pat in ("*.log", "*.warn", "*.out", "logFile", "CDS.log"):
                cands.extend(p.glob(pat))
    if not cands:
        return None
    try:
        return max(cands, key=lambda f: f.stat().st_mtime)
    except OSError:
        return cands[0]
