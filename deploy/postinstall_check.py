#!/usr/bin/env python3
"""Post-install verification: can this box actually drive pmukit through a browser?

`bash apply` runs this at the end.  It drives the SAME four checks as `tools/webprobe.py`
(the probe the user clicks in Firefox), but headlessly: it starts webprobe's HTTP server on
127.0.0.1 with an ephemeral port, hits it with urllib, and prints four PASS/FAIL lines.

    python deploy/postinstall_check.py                 # four checks + numpy/scipy import
    python deploy/postinstall_check.py --host 0.0.0.0  # bind check for a remote-desktop box

Pure stdlib, no Qt, no network beyond loopback.  Exits non-zero if any check fails, so the
installer can say "installed, but the browser path is not clear yet" instead of claiming
success it did not verify.
"""
from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

APP = pathlib.Path(__file__).resolve().parent.parent      # .../app  (or the repo root)
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

TIMEOUT = 20.0


def _load_webprobe():
    """tools/webprobe.py, wherever this tree is rooted."""
    try:
        return importlib.import_module("tools.webprobe")
    except ImportError:
        sys.path.insert(0, str(APP / "tools"))
        return importlib.import_module("webprobe")


def _serve(mod, host):
    import http.server
    mod.H.log_message = lambda self, fmt, *a: None       # the installer prints the verdict, not
    srv = http.server.ThreadingHTTPServer((host, 0), mod.H)   # an access log
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _get(url, timeout=TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def check_ping(base):
    """1. the server answers and can describe the box."""
    code, body = _get(base + "/ping")
    j = json.loads(body.decode())
    return code == 200 and "host" in j and "python" in j, f"host={j.get('host')} py={j.get('python')}"


def check_command(base):
    """2. the page can run a command on the box and read its output."""
    code, body = _get(base + "/run/python")
    text = body.decode(errors="replace")
    return code == 200 and "[exit 0]" in text, text.strip().splitlines()[0][:60] if text.strip() else "(no output)"


def check_stream(base, mod):
    """3. a long job streams -- the first line must arrive well before the job ends."""
    # the interpreter, not `sh -c`: this check must also run on the Windows desk, where the box's
    # shell built-ins are not on PATH.
    mod.COMMANDS["_pmukit_stream"] = [
        sys.executable, "-c",
        "import time\nfor i in range(3):\n    print('tick', i + 1, flush=True)\n    time.sleep(1)"]
    t0 = time.time()
    first = None
    with urllib.request.urlopen(base + "/run/_pmukit_stream", timeout=TIMEOUT) as r:
        while True:
            chunk = r.read(1)
            if not chunk:
                break
            if first is None:
                first = time.time() - t0
    total = time.time() - t0
    ok = first is not None and total > 1.0 and first < total - 0.6
    return ok, f"first byte {first:.2f}s of {total:.2f}s" if first is not None else "no bytes"


def check_files(base):
    """4. download a generated file and upload text back."""
    code, body = _get(base + "/download/probe.va")
    ok_down = code == 200 and b"endmodule" in body
    payload = b"pmukit postinstall probe\n"
    req = urllib.request.Request(base + "/upload", data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        status, j = r.status, json.loads(r.read().decode())
    ok_up = status == 200 and j.get("bytes") == len(payload)
    return ok_down and ok_up, f"download {len(body)} B, upload -> {j.get('saved', '?')}"


def check_imports():
    """0. the venv actually has the runtime stack (this is what the wheels were for)."""
    import numpy
    import scipy
    # The run ledger is SQLite; a site Python built without _sqlite3 would install fine and
    # then fail at the first `pmukit plan --submit`.
    import sqlite3
    return True, (f"numpy {numpy.__version__}, scipy {scipy.__version__}, "
                  f"sqlite {sqlite3.sqlite_version}")


def check_entry_point():
    """5. `python -m pmukit` runs -- exactly what $PREFIX/bin/pmukit execs."""
    import os
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    r = subprocess.run([sys.executable, "-m", "pmukit", "--help"],
                       capture_output=True, text=True, env=env, timeout=TIMEOUT)
    if r.returncode == 0:
        return True, (r.stdout or r.stderr).strip().splitlines()[0][:60] if (r.stdout or r.stderr) else "ok"
    tail = (r.stderr or r.stdout).strip().splitlines()
    note = tail[-1][:120] if tail else f"exit {r.returncode}"
    if "__main__" in note:
        note += "   <- pmukit/__main__.py is missing (the CLI milestone owns it)"
    return False, note


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--prefix", default="", help="install prefix, printed for context only")
    a = ap.parse_args(argv)

    if a.prefix:
        print(f"      prefix {a.prefix}")
    results = []

    try:
        ok, note = check_imports()
    except Exception as e:                                   # noqa: BLE001 - report, never crash
        ok, note = False, f"{type(e).__name__}: {e}"
    results.append(("0. runtime stack (numpy + scipy + sqlite3)", ok, note))

    try:
        ok, note = check_entry_point()
    except Exception as e:                                   # noqa: BLE001 - report, never crash
        ok, note = False, f"{type(e).__name__}: {e}"
    results.append(("5. CLI entry point (python -m pmukit)", ok, note))

    mod = _load_webprobe()
    srv, port = _serve(mod, a.host)
    base = f"http://{a.host if a.host != '0.0.0.0' else '127.0.0.1'}:{port}"
    try:
        for label, fn in (("1. server alive (ping)", check_ping),
                          ("2. run a command, read its output", check_command),
                          ("3. streaming (lines arrive as they happen)",
                           lambda b: check_stream(b, mod)),
                          ("4. files (download a .va, upload text back)", check_files)):
            try:
                ok, note = fn(base)
            except (urllib.error.URLError, OSError, ValueError, socket.timeout) as e:
                ok, note = False, f"{type(e).__name__}: {e}"
            results.append((label, ok, note))
    finally:
        srv.shutdown()
        srv.server_close()

    results.sort(key=lambda r: r[0])                         # 0..5, whatever order they ran in
    width = max(len(r[0]) for r in results)
    for label, ok, note in results:
        print(f"      {'PASS' if ok else 'FAIL'}  {label:<{width}}  {note}")
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"      {n_ok}/{len(results)} checks passed"
          f"   (the same four buttons as: python tools/webprobe.py, opened in Firefox)")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
