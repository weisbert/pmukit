#!/usr/bin/env python3
"""webprobe -- can the red-zone box drive a tool through a browser?

Pure stdlib (python3.6+), no wheels, no internet, no Qt. Run on the box:

    python3 deploy/webprobe.py                 # binds 127.0.0.1:8765
    python3 deploy/webprobe.py --host 0.0.0.0  # browser on ANOTHER host (VNC desktop vs node)

then open  http://localhost:8765  in the box's Firefox. Every button is one thing a real
web GUI would need; if all four go green, a web front-end is viable in the red zone.
"""
import argparse
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# only these commands can be run from the page (no free-form shell)
COMMANDS = {
    "hostname": ["hostname"],
    "python": [sys.executable, "--version"],
    "which_dsub": ["sh", "-c", "which dsub || echo 'dsub NOT on PATH'"],
    "alps_dir": ["sh", "-c", "ls -d /software/empyrean/alps/* 2>/dev/null || echo 'no alps dir'"],
    "stream10": ["sh", "-c", "for i in 1 2 3 4 5 6 7 8 9 10; do echo tick $i; sleep 1; done"],
}

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>webprobe</title>
<style>
 body{font-family:sans-serif;max-width:760px;margin:24px auto;padding:0 16px;background:#fafafa}
 h1{font-size:20px} section{border:1px solid #ddd;border-radius:6px;padding:12px;margin:12px 0;background:#fff}
 pre{background:#111;color:#0f0;padding:8px;min-height:2em;white-space:pre-wrap;font-size:12px}
 button{margin:2px 4px 2px 0;padding:6px 10px} .ok{color:#080} .bad{color:#c00}
</style></head><body>
<h1>webprobe: browser &rarr; python server &rarr; box</h1>
<section><b>1. ping (server alive, who am I)</b><br>
 <button onclick="ping()">ping</button><pre id="p1"></pre></section>
<section><b>2. run a command, see its output</b><br>
 <button onclick="run('hostname')">hostname</button>
 <button onclick="run('python')">python --version</button>
 <button onclick="run('which_dsub')">which dsub</button>
 <button onclick="run('alps_dir')">ls alps</button><pre id="p2"></pre></section>
<section><b>3. streaming: a 10 s job, lines must appear one per second (not all at the end)</b><br>
 <button onclick="run('stream10')">stream 10 ticks</button><pre id="p3"></pre></section>
<section><b>4. files: download a generated .va, upload text back</b><br>
 <a href="/download/probe.va" download>download probe.va</a> &nbsp;
 <button onclick="upload()">upload this textarea</button><br>
 <textarea id="up" rows="3" cols="60">// paste anything, click upload</textarea><pre id="p4"></pre></section>
<script>
function ping(){fetch('/ping').then(r=>r.json()).then(j=>{p1.textContent=JSON.stringify(j,null,1)})
 .catch(e=>{p1.textContent='FAIL '+e})}
function run(name){const out=name==='stream10'?p3:p2;out.textContent='';const t0=Date.now();
 fetch('/run/'+name).then(r=>{const rd=r.body.getReader();const dec=new TextDecoder();
  function pump(){return rd.read().then(({done,value})=>{if(done){out.textContent+='\n[done '+((Date.now()-t0)/1000).toFixed(1)+' s]';return}
   out.textContent+=dec.decode(value);return pump()})}return pump()}).catch(e=>{out.textContent='FAIL '+e})}
function upload(){fetch('/upload',{method:'POST',body:up.value}).then(r=>r.json())
 .then(j=>{p4.textContent=JSON.stringify(j)}).catch(e=>{p4.textContent='FAIL '+e})}
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/ping":
            return self._send(200, {
                "host": socket.gethostname(), "os": platform.platform(),
                "python": sys.version.split()[0], "cwd": os.getcwd(), "user": os.environ.get("USER"),
                "dsub_on_path": bool(shutil.which("dsub")), "time": time.strftime("%F %T")})
        if self.path.startswith("/run/"):
            name = self.path[5:]
            if name not in COMMANDS:
                return self._send(404, {"error": "unknown command"})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            p = subprocess.Popen(COMMANDS[name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in iter(p.stdout.readline, b""):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                self.wfile.flush()
            p.wait()
            tail = ("[exit %d]\n" % p.returncode).encode()
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(tail), tail))
            return
        if self.path == "/download/probe.va":
            va = ("// generated by webprobe on %s at %s\nmodule probe(p,n); inout p,n; electrical p,n;\n"
                  "analog I(p,n) <+ V(p,n)/1k;\nendmodule\n" % (socket.gethostname(), time.strftime("%F %T")))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=probe.va")
            self.send_header("Content-Length", str(len(va)))
            self.end_headers()
            self.wfile.write(va.encode())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/upload":
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            fd, path = tempfile.mkstemp(prefix="webprobe_", suffix=".txt")
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            return self._send(200, {"saved": path, "bytes": n})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%T"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = http.server.ThreadingHTTPServer((a.host, a.port), H)
    print("webprobe listening on http://%s:%d  (Ctrl-C to stop)" % (a.host, a.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
