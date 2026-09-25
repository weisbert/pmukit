"""The New screen on a bench the size of a real one: the deck is read and scanned once.

A real ADE export is a few MB with a ~100-pin PMU, and one scan of it is ~1 s. A Model tick used
to re-read and re-scan the deck (twice), so the screen went blank for 1-3 s per click. Pinned:

* GET pins, PUT pin, the bulk PUT and the Netlist row never scan an unchanged working copy
  again, and are fast (wall-clock guards on a synthetic 4 MB bench, `PMUKIT_SKIP_TIMING=1`
  turns those off);
* what the cache serves IS the scan: the same table `Netlist.scan(inst, ports=cfg.ports)` gives;
* the cache lets go when the deck changes -- a re-read, a role written into it, a rewrite of the
  copy behind the server's back (even one of the same size, inside one timestamp tick) -- and
  the instance is part of its key;
* a PUT answers with the table, the summary and the config, so the page never re-asks;
* "model all rails / all biases / none" is ONE request and ONE Ctrl-Z;
* concurrent requests scan a deck once, and concurrent ticks all land.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from pmukit import netlist as netlist_mod
from pmukit import server
from pmukit.errors import PmuError
from pmukit.netlist import Netlist
from tests.bigbench import pin_names, write_bench
from tests.test_new_screen import REFGEN, api, bench, edit, history, load_ok, wait  # noqa: F401

timing = pytest.mark.skipif(os.environ.get("PMUKIT_SKIP_TIMING") == "1",
                            reason="PMUKIT_SKIP_TIMING=1: wall-clock guards are off here")
#: warm GET pins / PUT pin on the 4 MB bench: ~5 ms and ~40 ms here, ~1.1 s each before.
WARM_BOUND_S = 0.3


class Count:
    """Counts the expensive walks of a Netlist: a scan and the PMU candidate ranking."""

    def __init__(self, monkeypatch):
        self.scans = 0
        self.cands = 0
        real_scan, real_cands = Netlist.scan, server.pmu_candidates

        def scan(nl, *a, **kw):
            self.scans += 1
            return real_scan(nl, *a, **kw)

        def cands(nl):
            self.cands += 1
            return real_cands(nl)
        monkeypatch.setattr(netlist_mod.Netlist, "scan", scan)
        monkeypatch.setattr(server, "pmu_candidates", cands)


def _big_project(root, name="big"):
    a = server.Api(root=root / "data")
    a.new_project({"name": name})
    path = write_bench(root / f"bench_{name}")
    job = wait(a.load_netlist(name, {"path": str(path)}))
    assert job.status == "done", job.error
    return a, path


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    root = tmp_path_factory.mktemp("big")
    a, path = _big_project(root)
    return a, path


def _row(payload, name):
    return next(p for p in payload["pins"] if p["name"] == name)


# --------------------------------------------------------------------------- the big bench
def test_the_bench_is_the_size_of_a_real_one(big):
    a, path = big
    assert path.stat().st_size > 3_500_000
    pins = a.pins("big")
    assert len(pins["pins"]) == 99
    roles = pins["summary"]["roles"]
    assert roles == {"supply": 3, "rail": 4, "bias": 20, "en": 2, "ground": 2, "none": 68}


def test_the_cached_table_is_the_scan(big):
    """The cache stamps the config's fates on a copy of the base scan -- it must be exactly what
    a fresh `scan(inst, ports=...)` of the copy gives, pin for pin."""
    a, path = big
    a.set_pin("big", "VRAIL2", {"fate": "stub"})
    served = a.pins("big")
    pr = server.Project("big", a.root)
    cfg = pr.config()
    fresh = Netlist.from_file(pr.netlist_path())
    fresh.origin = str(path)
    want = fresh.scan(cfg.pmu_inst, ports=dict(cfg.ports))
    got = {p["name"]: {k: v for k, v in p.items() if k != "name"} for p in served["pins"]}
    assert got == want.to_dict()
    assert served["notes"] == want.notes and served["params"] == want.params
    assert served["sections"] == want.sections and served["analyses"] == want.analyses
    assert served["candidates"] == server.pmu_candidates(fresh)
    assert _row(served, "VRAIL2")["fate"] == "stub"
    a.set_pin("big", "VRAIL2", {"fate": "model"})


def test_nothing_on_the_new_screen_rescans_an_unchanged_deck(big, monkeypatch):
    a, _path = big
    a.pins("big")                                     # warm
    n = Count(monkeypatch)
    a.pins("big")
    a.netlist_info("big")
    out = a.set_pin("big", "VRAIL1", {"fate": "stub"})
    assert _row(out["pins"], "VRAIL1")["fate"] == "stub"
    a.set_pins("big", {"fates": {"VRAIL1": "model", "IBIAS3": "stub"}})
    a.set_pins("big", {"fates": {"IBIAS3": "model"}})
    a.derived("big")
    assert (n.scans, n.cands) == (0, 0), "an unchanged working copy was scanned again"


@pytest.mark.timing
@timing
def test_warm_pins_and_a_tick_are_fast(big):
    a, _path = big
    a.pins("big")

    def worst(fn, n=3):
        ts = []
        for _ in range(n):
            t = time.perf_counter()
            fn()
            ts.append(time.perf_counter() - t)
        return max(ts)

    get = worst(lambda: a.pins("big"))
    info = worst(lambda: a.netlist_info("big"))
    put = worst(lambda: (a.set_pin("big", "VRAIL0", {"fate": "stub"}),
                         a.set_pin("big", "VRAIL0", {"fate": "model"})), 2) / 2
    bulk = worst(lambda: (a.set_pins("big", {"fates": {f"IBIAS{i}": "stub" for i in range(20)}}),
                          a.set_pins("big", {"fates": {f"IBIAS{i}": "model"
                                                       for i in range(20)}})), 2) / 2
    for what, t in (("GET pins", get), ("GET netlist", info), ("PUT pin", put),
                    ("PUT pins (bulk)", bulk)):
        assert t < WARM_BOUND_S, f"{what} took {t * 1000:.0f} ms on the 4 MB bench"


def test_concurrent_requests_scan_the_deck_once(tmp_path, monkeypatch):
    a, _path = _big_project(tmp_path, "conc")
    server._scan_forget(server.Project("conc", a.root).dir)
    n = Count(monkeypatch)
    out, errs = [], []

    def get():
        try:
            out.append(a.pins("conc"))
        except Exception as exc:                      # pragma: no cover - reported below
            errs.append(exc)
    threads = [threading.Thread(target=get) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs and len(out) == 6
    assert n.scans == 1 and n.cands == 1
    assert all(o["pins"] == out[0]["pins"] for o in out)

    # ticks arriving together: every one of them lands (no lost read-modify-write)
    names = pin_names()
    todo = names["rail"] + names["bias"][:8]
    threads = [threading.Thread(target=a.set_pin, args=("conc", p, {"fate": "stub"}))
               for p in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ports = a.get_config("conc")["config"]["ports"]
    assert [p for p in todo if ports[p] != "stub"] == []


# --------------------------------------------------------------------------- the PUT answers
def test_a_tick_answers_with_the_table_the_summary_and_the_config(api, bench):
    first = load_ok(api, {"path": str(bench / "input.scs")})
    assert first["summary"]["rails"] == 3
    out = api.set_pin("p", "VDD0P8_B", {"fate": "stub"})
    assert out["pin"] == "VDD0P8_B" and out["changed"] == ["fate stub"]
    assert _row(out["pins"], "VDD0P8_B")["fate"] == "stub"
    assert out["pins"]["summary"]["rails"] == 2 and out["pins"]["summary"]["stubs"] >= 1
    assert out["config"]["config"]["ports"]["VDD0P8_B"] == "stub"
    assert "VDD0P8_B" not in out["config"]["config"]["my_load"]
    assert out["undoable"] == "config" and out["config"]["undoable"] == "config"
    assert out["pins"] == api.pins("p")                 # the same table a GET gives


def test_a_rail_ticked_back_gets_its_load_back(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    before = api.get_config("p")["config"]["my_load"]["VDD0P8_A"]
    api.set_pin("p", "VDD0P8_A", {"fate": "stub"})
    out = api.set_pin("p", "VDD0P8_A", {"fate": "model"})
    assert out["config"]["config"]["my_load"]["VDD0P8_A"] == before


def test_a_tick_on_a_pin_that_is_not_there_is_refused(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    with pytest.raises(PmuError) as ei:
        api.set_pin("p", "NOPE", {"fate": "stub"})
    assert "NOPE" in ei.value.what and ei.value.do


# --------------------------------------------------------------------------- bulk
def test_model_none_is_one_request_and_one_undo(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    was = dict(api.get_config("p")["config"]["ports"])
    n_hist = len(history(api))
    rb = [p["name"] for p in api.pins("p")["pins"] if p["role"] in ("rail", "bias")]
    out = api.set_pins("p", {"fates": {p: "stub" for p in rb}, "note": "model none"})
    assert sorted(out["changed"]) == sorted(p for p in rb if was[p] != "stub")
    assert all(_row(out["pins"], p)["fate"] == "stub" for p in rb)
    assert out["pins"]["summary"]["rails"] == 0 and out["pins"]["summary"]["biases"] == 0
    assert out["config"]["config"]["my_load"] == {}
    assert len(history(api)) == n_hist + 1                    # ONE history entry
    back = api.undo_config("p")                               # ONE Ctrl-Z
    assert back["config"]["ports"] == was
    assert all(_row(api.pins("p"), p)["fate"] == was[p] for p in rb)


def test_bulk_with_a_pin_that_is_not_there_writes_nothing(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    n_hist = len(history(api))
    with pytest.raises(PmuError) as ei:
        api.set_pins("p", {"fates": {"VDD0P8_A": "stub", "GHOST": "stub"}})
    assert "GHOST" in ei.value.what
    assert len(history(api)) == n_hist
    assert api.get_config("p")["config"]["ports"]["VDD0P8_A"] == "model"
    with pytest.raises(PmuError):
        api.set_pins("p", {"fates": {}})


def test_bulk_that_changes_nothing_adds_no_undo_step(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    n_hist = len(history(api))
    out = api.set_pins("p", {"fates": {"VDD0P8_A": "model"}})
    assert out["changed"] == [] and len(history(api)) == n_hist


# --------------------------------------------------------------------------- invalidation
def test_a_reread_of_a_changed_bench_is_served_fresh(api, bench, monkeypatch):
    load_ok(api, {"path": str(bench / "input.scs")})
    assert _row(api.pins("p"), "TESTMODE")["role"] == "none"
    edit(bench / "input.scs", "VEN_EN ", "VB_TESTMODE (TESTMODE 0) vsource dc=0.3\nVEN_EN ")
    load_ok(api, {"reread": True})
    n = Count(monkeypatch)
    assert _row(api.pins("p"), "TESTMODE")["role"] == "bias"
    assert n.scans == 0, "the re-read's own scan is the copy's: it is not scanned again"


def test_a_role_written_into_the_deck_is_served_fresh(api, bench):
    load_ok(api, {"path": str(bench / "input.scs")})
    api.pins("p")
    out = api.set_pin("p", "TESTMODE", {"role": "bias", "dc": 0.3})
    assert _row(out["pins"], "TESTMODE")["role"] == "bias"
    assert _row(api.pins("p"), "TESTMODE")["src"] == "VB_TESTMODE"


def test_the_instance_is_part_of_the_key(api, bench, monkeypatch):
    edit(bench / "input.scs", "simOpts options", REFGEN + "simOpts options")
    load_ok(api, {"path": str(bench / "input.scs")})
    assert api.pins("p")["pmu_inst"] == "PMU_TOP"
    out = api.set_instance("p", {"pmu_inst": "XREF"})
    assert out["pins"]["pmu_inst"] == "XREF"
    assert {p["name"] for p in api.pins("p")["pins"]} == {"VREF", "VIN", "GND"}
    n = Count(monkeypatch)
    api.undo_config("p")                                      # back to PMU_TOP: a hit
    assert api.pins("p")["pmu_inst"] == "PMU_TOP"
    assert n.scans == 0


def test_a_same_size_rewrite_of_the_copy_is_seen(api, bench):
    """Someone rewrites pmukit's copy behind the server (same size, inside one timestamp tick):
    the stat alone cannot tell, so a recent file is checked by content."""
    load_ok(api, {"path": str(bench / "input.scs")})
    assert _row(api.pins("p"), "VDD0P8_C")["role"] == "rail"
    copy = server.Project("p", api.root).netlist_path()
    st = os.stat(copy)
    text = copy.read_text(encoding="utf-8")
    new = text.replace("IL_VDD0P8_C (", "XL_VDD0P8_C (")
    assert len(new) == len(text) and new != text
    copy.write_text(new, encoding="utf-8", newline="\n")
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))       # the very same stat
    assert _row(api.pins("p"), "VDD0P8_C")["role"] == "none"


def test_two_projects_are_cached_apart(tmp_path, bench):
    a = server.Api(root=tmp_path / "data2")
    for name in ("one", "two"):
        a.new_project({"name": name})
    job = wait(a.load_netlist("one", {"path": str(bench / "input.scs")}))
    assert job.status == "done", job.error
    other = tmp_path / "other"
    write_bench(other, n_leaves=4, dev_per_leaf=5, n_blocks=2)
    job = wait(a.load_netlist("two", {"path": str(other / "input.scs")}))
    assert job.status == "done", job.error
    assert a.pins("one")["pmu_inst"] == "PMU_TOP"
    assert a.pins("two")["pmu_inst"] == "I_PMU"
    a.set_pin("one", "VDD0P8_A", {"fate": "stub"})
    assert _row(a.pins("one"), "VDD0P8_A")["fate"] == "stub"
    assert _row(a.pins("two"), "VRAIL0")["fate"] == "model"
