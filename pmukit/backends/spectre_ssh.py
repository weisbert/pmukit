"""The desk engine: run Spectre on a Linux host over ssh.

The whole backend is three ssh calls -- push, run, pull -- and one hard-won rule:

    **The Cadence environment lives ONLY in `~/.cshrc` on the run host.**  A plain
    `ssh host spectre ...` finds no `spectre`, no license and no models.  Every remote command is
    therefore `tcsh -c "source ~/.cshrc; ..."`.

The other two rules come from the same scar file (TOOL_FACTS "Spectre / Verilog-A"):

    * always `spectre -64` -- otherwise ahdlcmi compiles Verilog-A `-m32` and dies on
      `gnu/stubs-32.h`;
    * `-format psfascii` -- what `pmukit.psf` parses on this path (binary PSF is the cluster's).

Transport is a tar stream piped through ssh, not rsync and not scp: Git Bash has no rsync, and
scp splits a Windows `C:/...` path on the colon.  `tarfile` is in the standard library, so the
only external program this backend needs is `ssh` itself.  The download direction comes back
base64-encoded because a login banner on the remote's stdout would otherwise corrupt the tar.

Degradation: when ssh cannot reach the host, this backend hands the job to `dry_run` rather than
failing the sweep -- and says DEGRADED on every single run, in `available()`, in each job's detail
line and in the ledger `error`.  It is never silent.
"""
from __future__ import annotations

import base64
import io
import pathlib
import re
import shutil
import subprocess
import tarfile

from ..errors import PmuError
from .dry_run import DryRunBackend

__all__ = ["SpectreSSHBackend", "remote_command"]

#: Only these characters may appear in a remote path segment we build.  The remote command is a
#: shell string (it has to be: `~` and `source` need a shell), so anything exotic is refused
#: rather than quoted-and-hoped.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_~./+-]+$")
_SAFE_TAG = re.compile(r"^[A-Za-z0-9_.-]+$")
_B64_LINE = re.compile(r"^[A-Za-z0-9+/=]*$")

SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new")

#: Result artefacts: never uploaded (a retry must start clean), always downloaded.
RESULTS = ("spectre.log", "raw")


def remote_command(remote_dir: str, spectre_cmd: str = "spectre", deck: str = "input.scs") -> str:
    """The literal command that runs on the host -- the one line to paste when debugging.

    `tcsh -c "source ~/.cshrc; ..."` is not a style choice: the Cadence env is only in .cshrc.
    """
    return (f'tcsh -c "source ~/.cshrc; cd {remote_dir}; '
            f'{spectre_cmd} -64 {deck} -format psfascii -raw raw +log spectre.log -E"')


class SpectreSSHBackend:
    """Push the run directory to a Linux host, run Spectre there, bring the results home."""

    name = "spectre_ssh"

    def __init__(self, site, *, ssh: str = "ssh", allow_degrade: bool = True,
                 timeout_s: float | None = None):
        self.site = site
        self.ssh = ssh
        self.timeout_s = timeout_s
        self.allow_degrade = bool(allow_degrade)
        self.host = str(getattr(site, "ssh_host", "") or "")
        self.workroot = str(getattr(site, "remote_workdir", "~/pmukit_work") or "~/pmukit_work")
        self.spectre_cmd = str(getattr(site, "spectre_cmd", "spectre") or "spectre")
        self._probe: tuple[bool, str] | None = None
        self._degraded: DryRunBackend | None = None

    # ------------------------------------------------------------------ ssh plumbing
    def _run(self, remote: str, *, stdin: bytes | None = None,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        argv = [self.ssh, *SSH_OPTS, self.host, remote]
        try:
            return subprocess.run(argv, input=stdin, capture_output=True,
                                  timeout=timeout or self.timeout_s or 3600)
        except FileNotFoundError as exc:
            raise PmuError(
                what=f"The ssh client {self.ssh!r} is not on PATH.",
                why="This backend copies the run directory to the simulation host and runs "
                    "Spectre there; it needs an ssh client to do anything at all.",
                do=["Install OpenSSH (Windows: 'Add a feature' -> OpenSSH Client), or run from "
                    "Git Bash.",
                    "Or set engine to dry_run / fake to work without a simulator."],
                where="pmukit/backends/spectre_ssh.py") from exc
        except subprocess.TimeoutExpired as exc:
            raise PmuError(
                what=f"The remote command on {self.host} timed out.",
                why=f"ssh did not return within {timeout or self.timeout_s or 3600:.0f} s while "
                    f"running: {remote}",
                do=[f"Check the host is reachable: ssh {self.host} true",
                    "Raise the timeout (Runner(timeout_s=...)), or kill the stuck job on the host."],
                where=f"{self.host}") from exc

    def remote_dir(self, job) -> str:
        """`<remote_workdir>/<run_id>` -- one directory per run, wiped before every upload."""
        tag = str(job.run.run_id)
        if not _SAFE_TAG.match(tag):
            raise PmuError(
                what=f"Run id {tag!r} is not a safe remote directory name.",
                why="The run id becomes a directory on the remote host inside a shell command; "
                    "only letters, digits, '_', '.' and '-' are accepted rather than quoted.",
                do=["This is a pmukit bug -- run ids are content hashes and should be hex."],
                where="pmukit/backends/spectre_ssh.py")
        if not _SAFE_PATH.match(self.workroot):
            raise PmuError(
                what=f"site.remote_workdir {self.workroot!r} is not a safe remote path.",
                why="It is pasted into a remote shell command (a leading '~' has to be expanded "
                    "by that shell), so spaces and shell metacharacters are refused.",
                do=["Use a plain path such as ~/pmukit_work or /scratch/<user>/pmukit."],
                where="site.json: remote_workdir")
        return f"{self.workroot}/{tag}"

    # ------------------------------------------------------------------ interface
    def available(self) -> tuple[bool, str]:
        """Probe ssh + the Cadence env.  Never raises; the answer is cached for the session."""
        if self._probe is not None:
            return self._probe
        if not self.host.strip():
            self._probe = (False, "site.ssh_host is empty -- there is no host to run on")
            return self._probe
        if shutil.which(self.ssh) is None:
            self._probe = (False, f"no ssh client on PATH ({self.ssh!r})")
            return self._probe
        probe = 'tcsh -c "source ~/.cshrc; which spectre; spectre -W"'
        try:
            p = self._run(probe, timeout=30)
        except PmuError as exc:
            self._probe = (False, exc.what)
            return self._probe
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        if p.returncode != 0:
            first = (err.strip() or out.strip() or "no output").splitlines()[0][:200]
            self._probe = (False, f"ssh {self.host}: {first}")
            return self._probe
        both = out + "\n" + err            # `spectre -W` prints its banner on stderr
        path = next((l.strip() for l in both.splitlines() if "/" in l and "spectre" in l), "")
        ver = re.search(r"([0-9]+\.[0-9]+(?:\.[0-9]+)*)", both)
        self._probe = (True, f"spectre {ver.group(1) if ver else '?'} at {path or '?'} "
                             f"on {self.host}")
        return self._probe

    def submit(self, job) -> str:
        """Upload the run directory and run Spectre.  This call BLOCKS for the simulation.

        Blocking is deliberate: a plain ssh host has no queue, so there is nothing to poll.
        Parallelism comes from the runner's thread pool (`--jobs`), which keeps one deck per
        worker and never oversubscribes a shared VM by accident.
        """
        ok, why = self.available()
        if not ok:
            if not self.allow_degrade:
                raise PmuError(
                    what=f"Cannot reach the simulation host {self.host!r}.",
                    why=why,
                    do=[f"Check the host: ssh {self.host} true",
                        "Set engine to dry_run to write the decks without simulating.",
                        "Set engine to fake to exercise the pipeline with analytic results."],
                    where=f"site.json: ssh_host={self.host}")
            self._degraded = self._degraded or DryRunBackend(
                self.site, degraded_from=self.name, reason=why)
            return self._degraded.submit(job)

        d = self.remote_dir(job)
        self._push(job, d)
        cmd = remote_command(d, self.spectre_cmd)
        p = self._run(cmd)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        job.job_id = f"{self.host}:{job.run.run_id}"
        job.detail = cmd
        if p.returncode != 0:
            job.state = "failed"
            job.detail = f"spectre exited {p.returncode} on {self.host}"
            # keep the remote console for the log tail, but out of the headline
            job.console = (err.strip() or out.strip())[-2000:]
        else:
            job.state = "done"
            job.console = ""
        return job.job_id

    def poll(self, job) -> str:
        if self._degraded is not None:
            return self._degraded.poll(job)
        return job.state or "running"

    def fetch(self, job) -> pathlib.Path:
        """Bring `spectre.log` and `raw/` home, then decide whether the run really succeeded.

        The log is fetched even for a failed run -- its tail is the ledger's `error`, and without
        it the user is told only "it failed".
        """
        if self._degraded is not None:
            return self._degraded.fetch(job)
        d = self.remote_dir(job)
        wd = pathlib.Path(job.workdir)
        p = self._run(f"cd {d} && tar -czf - {' '.join(RESULTS)} 2>/dev/null | base64",
                      timeout=self.timeout_s or 1800)
        payload = (p.stdout or b"").decode("ascii", "ignore")
        blob = "".join(l for l in payload.splitlines() if _B64_LINE.match(l))
        if blob:
            try:
                data = base64.b64decode(blob, validate=False)
                _extract(io.BytesIO(data), wd)
            except (ValueError, tarfile.TarError, OSError) as exc:
                raise PmuError(
                    what=f"The results from {self.host} could not be unpacked.",
                    why=f"The base64 tar stream from {d} did not decode ({exc}); a login banner "
                        "on the remote stdout or a truncated transfer does this.",
                    do=[f"Check by hand: ssh {self.host} 'ls -l {d}/raw'",
                        "Silence the remote login banner for non-interactive sessions "
                        "(~/.hushlogin), then retry."],
                    where=f"{self.host}:{d}") from exc
        job.log_path = wd / "spectre.log"
        log = job.log_path.read_text(encoding="utf-8", errors="replace") \
            if job.log_path.is_file() else ""
        psf_dir = wd / "raw"
        # A "success" that wrote no PSF never really ran: a bad transfer, a wrong path, a killed
        # job.  Spectre also reports some failures only in the log, with rc 0.
        first = _first_error(log)
        if job.state == "failed" and first:
            # The scheduler-level message ("exited 2") is never the reason; the reason is the
            # first ERROR line the simulator printed.  Lead with that.
            job.detail = f"{job.detail}: {first}"
        if job.state != "failed":
            if "fatal error" in log.lower():
                job.state = "failed"
                job.detail = (f"spectre reported a fatal error on {self.host}"
                              + (f": {first}" if first else " (see spectre.log)"))
            elif not psf_dir.is_dir() or not any(psf_dir.iterdir()):
                job.state = "failed"
                job.detail = (f"spectre exited 0 on {self.host} but wrote no PSF into {d}/raw -- "
                              "the analyses produced no output")
        return psf_dir

    def kill(self, job) -> None:
        """Best effort: the remote spectre is a child of the blocking ssh, so killing that ssh
        normally ends it; this also sweeps a process that outlived its connection."""
        if self._degraded is not None or not self.host:
            return
        tag = str(job.run.run_id)
        try:
            self._run(f"pkill -f 'pmukit_work/{tag}' >/dev/null 2>&1 || true", timeout=30)
        except PmuError:
            return

    # ------------------------------------------------------------------ transport
    def _push(self, job, remote_dir: str) -> None:
        """Wipe the remote directory and re-upload the run inputs as one gzipped tar stream.

        Result artefacts are excluded so a retry can never read a previous run's PSF and call it
        this run's answer.
        """
        wd = pathlib.Path(job.workdir)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for p in sorted(wd.rglob("*")):
                rel = p.relative_to(wd)
                if rel.parts and rel.parts[0] in RESULTS:
                    continue
                if p.is_file():
                    tf.add(p, arcname=rel.as_posix())
        p = self._run(f"rm -rf {remote_dir} && mkdir -p {remote_dir} && cd {remote_dir} "
                      f"&& tar -xzf -", stdin=buf.getvalue(), timeout=self.timeout_s or 600)
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            raise PmuError(
                what=f"Could not upload the run directory to {self.host}.",
                why=f"`rm -rf {remote_dir} && mkdir -p ... && tar -xzf -` exited "
                    f"{p.returncode}: {err[:400] or 'no stderr'}",
                do=[f"Check write access: ssh {self.host} 'mkdir -p {remote_dir}'",
                    "Check the remote has GNU tar on PATH for a non-login shell."],
                where=f"{self.host}:{remote_dir}")


_ERROR_LINE = re.compile(r"^\s*(?:ERROR|Fatal error|Error found)\b.*$", re.M | re.I)


def _first_error(log: str) -> str:
    """The first line of the simulator log that actually says what went wrong.

    Spectre exits 2 and prints `spectre terminated prematurely due to fatal error` -- which says
    nothing.  The line that matters is the `ERROR: "input.scs" 47: ...` above it, and that is the
    one the ledger headline should carry.
    """
    m = _ERROR_LINE.search(log or "")
    return " ".join(m.group(0).split())[:300] if m else ""


def _extract(fileobj, dest: pathlib.Path) -> None:
    """Extract a tar stream under `dest`, refusing absolute paths and `..` segments.

    Everything here came off another machine; a member that escaped the run directory would
    write into the user's tree.  The check is explicit rather than relying on a tarfile filter,
    which changed default between Python versions.
    """
    dest = pathlib.Path(dest).resolve()
    with tarfile.open(fileobj=fileobj, mode="r:gz") as tf:
        members = []
        for m in tf.getmembers():
            name = m.name.replace("\\", "/").lstrip("./")
            if not name or name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
                continue
            if m.issym() or m.islnk() or m.isdev():
                continue
            m.name = name
            members.append(m)
        tf.extractall(dest, members=members)
