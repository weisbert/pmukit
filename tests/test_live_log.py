"""The live output log a right-click opens on a run: what the engine is writing, as it writes it.

* it reads the log the engine is GROWING -- the simulator log in the run directory, else the
  freshest log-like file there or in `raw/` (ALPS names its own) -- from a byte offset, so each
  answer carries only what is new;
* the file it reads is named in every answer; when another one takes over (the engine's own log
  appears), the window starts that one from the top;
* with no file yet the answer says why in the user's words, and a Donau job falls back to
  `dpeek`; the page opens it in a window of its own (?livelog=<run id>).
"""
from __future__ import annotations

import pathlib

import pytest

from pmukit import paths, server
from pmukit.ledger import Ledger, Run

RID = "abcdef012345"
PAGE = (pathlib.Path(server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def api(tmp_path, monkeypatch):
    for v in ("PMUKIT_SIM_ROOT", "WORK_ROOT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    a = server.Api(root=tmp_path)
    a.new_project({"name": "p"})
    led = Ledger(tmp_path / "p" / "runs.sqlite")
    led.plan_many([Run(run_id=RID, process="tt", temp_c=25.0, vset=3, load_key="",
                       analysis="dc_load", stimulus="IL_A", reads=["dc_load.A"])])
    led.upsert(Run(run_id=RID, process="tt", temp_c=25.0, vset=3, load_key="",
                   analysis="dc_load", stimulus="IL_A", reads=["dc_load.A"], status="running",
                   job_id="4711"))
    led.close()
    return a


def test_it_follows_the_log_the_engine_is_growing(api, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda _n: None)
    wd = paths.runs_dir("p") / RID
    (wd / "raw").mkdir(parents=True)
    log = wd / "raw" / "input.log"
    log.write_bytes(b"ALPS start\n")                    # the box's logs are LF
    d = api.live_log("p", RID)
    assert d["text"] == "ALPS start\n" and d["src"] == str(log) and d["reset"]
    assert d["status"] == "running" and d["job_id"] == "4711" and not d["done"]
    with open(log, "ab") as fh:
        fh.write(b"dc point 1\n")
    d2 = api.live_log("p", RID, d["offset"], d["src"])
    assert d2["text"] == "dc point 1\n" and not d2["reset"]
    # nothing new: an empty answer, same place
    d3 = api.live_log("p", RID, d2["offset"], d2["src"])
    assert d3["text"] == "" and d3["offset"] == d2["offset"]
    # the simulator's own log appears: the window starts it from the top
    (wd / "spectre.log").write_bytes(b"spectre-style log\n")
    d4 = api.live_log("p", RID, d3["offset"], d3["src"])
    assert d4["reset"] and d4["src"] == str(wd / "spectre.log")
    assert d4["text"] == "spectre-style log\n"


def test_a_long_log_comes_in_pieces(api, monkeypatch):
    monkeypatch.setattr(server, "LIVE_CHUNK", 10)
    wd = paths.runs_dir("p") / RID
    wd.mkdir(parents=True)
    (wd / "spectre.log").write_text("0123456789abcdefghij", encoding="utf-8")
    d = api.live_log("p", RID)
    assert d["text"] == "0123456789" and d["more"]
    d = api.live_log("p", RID, d["offset"], d["src"])
    assert d["text"] == "abcdefghij" and not d["more"]


def test_no_log_yet_says_why_and_a_donau_job_falls_back_to_dpeek(api, monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda _n: None)
    d = api.live_log("p", RID)
    assert d["replace"] and "no log file in the run directory yet" in d["text"]
    calls = []

    class Done:
        stdout, stderr = "line from the node\n", ""
    monkeypatch.setattr(server.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(server.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or Done())
    d = api.live_log("p", RID)
    assert calls == [["dpeek", "4711"]]
    assert d["src"] == "dpeek 4711" and d["replace"] and "line from the node" in d["text"]


def test_the_page_opens_it_from_the_run_menu_in_a_window_of_its_own():
    assert 'label:"Live output log (new window)"' in PAGE
    assert "openLiveLog(c.row.run_id)" in PAGE
    assert 'if (q.get("livelog")){ liveLogWindow(' in PAGE
    assert '"/live?offset="' in PAGE


def test_a_dry_run_note_is_not_history(tmp_path):
    led = Ledger(tmp_path / "runs.sqlite")
    base = dict(process="tt", temp_c=25.0, vset=3, load_key="", analysis="dc_load",
                stimulus="IL_A", reads=["dc_load.A"])
    led.plan_many([Run(run_id="aaaaaaaaaaa1", **base), Run(run_id="aaaaaaaaaaa2", **base)])
    led.upsert(Run(run_id="aaaaaaaaaaa1", status="planned",
                   error="dry run: nothing submitted; deck at x", **base))
    led.upsert(Run(run_id="aaaaaaaaaaa2", status="planned",
                   error="DEGRADED from spectre_ssh (no route): nothing submitted", **base))
    assert led.drop_unsubmitted([]) == 2
