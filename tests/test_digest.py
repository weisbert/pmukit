"""Contract 5: the digest -- the air-gap return path. Plain text, priority budget, no silent cut."""
from __future__ import annotations

import json
import math
import re

import numpy as np
import pytest

from pmukit import digest
from pmukit.digest import decimate_preserving_extremes
from pmukit.errors import PmuError


# --------------------------------------------------------------------------- payload fixture
def _zout(n=181):          # 20 points/decade, the density the measurement plan uses
    f = np.logspace(1, 10, n)
    gt = 0.02 + 1.0 / (1.0 + (f / 1.7e6) ** 2) ** 0.5
    return f, gt


def _tran(n=4001):
    t = np.linspace(0.0, 1e-5, n)
    y = 0.8 - 0.05 * np.exp(-(t - 2e-6) / 3e-7) * (t > 2e-6) \
        + 0.03 * np.exp(-(t - 4e-6) / 5e-7) * (t > 4e-6)
    return t, y


def make_payload(*, curves=True, transients=True, big=False) -> dict:
    f, gt = _zout()
    t, y = _tran()
    params = {
        "VDD0P8_A": {"tt": {"zout": {"Ra": 0.021234567890123, "La": 1.2345678901234e-07,
                                     "Rpl": 48.312345678901, "poles": [-1.1e6, -3.7e8]},
                            "psrr": {"k": -0.0123456789, "note": "signed 2nd-order section"},
                            "noise": {"white": 1.234e-17, "kf": 4e-29}}},
        "IB_PTAT": {"tt": {"idc": {"slope_a_per_c": 3.21e-9, "i0": 1.212e-06}}},
    }
    if big:
        params["filler"] = {f"c{i}": {"a": float(i) + 0.5, "b": [float(i)] * 8}
                            for i in range(400)}
    payload = {
        "meta": {"project": "demo_pmu", "created": "2026-09-15T14:31:02Z"},
        "provenance": {"config_sha": "5d8ca1", "dataset_sha": "a91fc3", "spec_sha": "77c003",
                       "pmukit_version": "0.1.0",
                       "tb_state_note": "RX mode, register 0x12 = 0x03"},
        "ledger": [
            {"run_id": "7c3e91a04bd2", "status": "done", "cell": "tt/25c/3",
             "analysis": "noise VDD0P8_A 500u", "cpu_s": 252.0},
            {"run_id": "e4d27a5c1f90", "status": "failed", "cell": "ss/125c/3",
             "analysis": "tran load-EN VDD0P8_B off", "cpu_s": 760.0,
             "error": "timestep too small near the IL_VDD0P8_B edge"},
        ],
        "params": params,
        "trust": {"valid_range": "load 2u-1m, -40..125 C, <= 20 GHz",
                  "hb_check": "pass, first-step residual 7.7e-3"},
        "grades": [
            {"port": "VDD0P8_A", "cell": "tt/25c", "block": "zout", "grade": "green",
             "score": 0.42, "detail": "matches the reference"},
            {"port": "VDD0P8_B", "cell": "ss/125c", "block": "noise", "grade": "red",
             "score": 12.5, "detail": "misses the flicker corner"},
        ],
        "faillog": [
            {"run_id": "e4d27a5c1f90",
             "netlist_edits": ["~ IL_VDD0P8_B (VDD0P8_B 0) isource type=pwl wave=[0 2m ...]",
                               "+ tr1 tran stop=10u step=2n"],
             "log_tail": [f"line {i}" for i in range(80)]},
        ],
        "curves": {}, "transients": {},
    }
    if curves:
        payload["curves"] = {
            "zout.VDD0P8_A": {"kind": "zout", "port": "VDD0P8_A", "cell": "ss/25c",
                              "x": f.tolist(), "gt": gt.tolist(),
                              "model": (gt * 1.01).tolist()},
            "idc.IB_PTAT": {"kind": "idc", "port": "IB_PTAT", "cell": "tt", "x_label": "T[C]",
                            "x": np.linspace(-40, 125, 34).tolist(),
                            "gt": np.linspace(1.2e-6, 1.6e-6, 34).tolist(),
                            "model": np.linspace(1.21e-6, 1.59e-6, 34).tolist()},
        }
    if transients:
        payload["transients"] = {
            "load_on.VDD0P8_A": {"kind": "tran_load_on", "port": "VDD0P8_A", "cell": "ss/25c",
                                 "t": t.tolist(), "gt": y.tolist(),
                                 "model": (y + 1e-4).tolist(), "n": 200},
        }
    return payload


def _headers(text: str) -> list[str]:
    return re.findall(r"^\[(D\d+ [^\]]*)\]$", text, re.M)


# --------------------------------------------------------------------------- round trip
def test_export_parse_round_trip_keeps_d2_bit_identical():
    payload = make_payload()
    parts = digest.export(payload, budget=128_000, part_size=128_000)
    assert len(parts) == 1
    back = digest.parse(parts)

    assert back["meta"]["project"] == "demo_pmu"
    assert back["meta"]["created"] == "2026-09-15T14:31:02Z"
    assert back["meta"]["version"] == "v1"
    assert back["dropped"] == []

    # D2 is the lossless block: identical structure AND identical float bits.
    assert back["params"] == payload["params"]
    a = json.dumps(payload["params"], sort_keys=True)
    b = json.dumps(back["params"], sort_keys=True)
    assert a == b
    assert back["params"]["VDD0P8_A"]["tt"]["zout"]["Ra"] == 0.021234567890123
    assert repr(back["params"]["VDD0P8_A"]["tt"]["zout"]["La"]) == repr(1.2345678901234e-07)

    # D0 / D1 / D3 / D6
    assert back["provenance"]["config_sha"] == "5d8ca1"
    assert back["provenance"]["tb_state_note"] == "RX mode, register 0x12 = 0x03"
    assert [r["run_id"] for r in back["ledger"]] == ["7c3e91a04bd2", "e4d27a5c1f90"]
    assert back["ledger"][1]["status"] == "failed" and back["ledger"][1]["cpu_s"] == 760.0
    assert "timestep too small" in back["ledger"][1]["error"]
    assert back["grades"][1] == {"port": "VDD0P8_B", "cell": "ss/125c", "block": "noise",
                                 "grade": "red", "score": 12.5,
                                 "detail": "misses the flicker corner"}
    assert back["trust"]["hb_check"].startswith("pass")
    assert back["faillog"][0]["run_id"] == "e4d27a5c1f90"
    assert len(back["faillog"][0]["log_tail"]) == digest.FAILLOG_TAIL      # tail only
    assert back["faillog"][0]["log_tail"][-1] == "line 79"
    assert len(back["faillog"][0]["netlist_edits"]) == 2


def test_curves_travel_on_one_resampled_grid_with_gt_and_model():
    payload = make_payload()
    back = digest.parse(digest.export(payload, budget=128_000, part_size=128_000))
    z = back["curves"]["zout.VDD0P8_A"]
    assert z["sub"] == "rail" and z["kind"] == "zout" and z["cell"] == "ss/25c"
    # 10 Hz .. 10 GHz is 9 decades at 12 points/decade
    assert len(z["x"]) == 12 * 9 + 1
    assert len(z["gt"]) == len(z["x"]) == len(z["model"])
    per_decade = (len(z["x"]) - 1) / math.log10(z["x"][-1] / z["x"][0])
    assert abs(per_decade - 12) < 1e-6
    assert z["model"][5] == pytest.approx(z["gt"][5] * 1.01, rel=1e-3)

    # a temperature sweep is not log-able and is never upsampled
    idc = back["curves"]["idc.IB_PTAT"]
    assert idc["sub"] == "bias" and idc["x_label"] == "T[C]"
    assert len(idc["x"]) == 34 and idc["x"][0] == pytest.approx(-40.0)


def test_transients_keep_the_extremes_exactly_through_a_round_trip():
    payload = make_payload()
    t, y = payload["transients"]["load_on.VDD0P8_A"]["t"], \
        payload["transients"]["load_on.VDD0P8_A"]["gt"]
    dip, over = float(min(y)), float(max(y))
    back = digest.parse(digest.export(payload, budget=128_000, part_size=128_000))
    tr = back["transients"]["load_on.VDD0P8_A"]
    assert len(tr["t"]) <= 201 < len(t)
    assert dip in tr["gt"] and over in tr["gt"]
    assert tr["gt"][-1] == y[-1]                    # settled value, verbatim
    assert tr["t"][0] == t[0]


# --------------------------------------------------------------------------- budget
def test_over_budget_drops_lowest_priority_and_names_them_in_the_trailer():
    payload = make_payload(big=True)
    full = digest.export(payload, budget=128_000, part_size=128_000)[0]
    assert "dropped (budget): none" in full

    parts = digest.export(payload, budget=32_000, part_size=32_000)
    text = "".join(parts)
    trailer = text.split("[D9 trailer]", 1)[1]
    m = re.search(r"dropped \(budget\): (.*)", trailer)
    dropped = [x.strip() for x in m.group(1).split(",")]
    assert dropped and dropped != ["none"]
    # the transients and the rail curves go before the ledger or the params
    assert "D5" in dropped
    assert "D0" not in dropped and "D1" not in dropped

    kept_headers = _headers(text)
    for name in dropped:
        head = dict(digest._HEAD)[name]
        assert f"{head[0]} {head[1]}" not in kept_headers

    # nothing silently truncated: every block that IS present is complete and parses
    back = digest.parse(parts)
    assert back["dropped"] == dropped
    assert back["params"] == payload["params"]      # a kept block is never partial
    assert len(text.encode("ascii")) <= 32_000 * len(parts)


def test_blocks_available_and_estimate_agree_with_export():
    payload = make_payload(big=True)
    avail = digest.blocks_available(payload)
    ids = [b["id"] for b in avail]
    assert ids == ["D0", "D1", "D2", "D3", "D4bias", "D4rail", "D5", "D6"]
    assert all(b["bytes"] > 0 and b["included_by_default"] for b in avail)
    # CONTRACTS.md section 5: provenance > ledger > params > failed logs > bias > rail > transients
    # (grades sit just after params -- the contract's list does not name them).
    by_priority = [b["id"] for b in sorted(avail, key=lambda b: b["priority"])]
    assert by_priority == ["D0", "D1", "D2", "D3", "D6", "D4bias", "D4rail", "D5"]

    est = digest.estimate(payload, None, 32_000)
    parts = digest.export(payload, budget=32_000, part_size=digest.DEFAULT_PART_SIZE)
    assert est["parts"] == len(parts)
    assert est["bytes"] == sum(len(p.split("\n", 1)[1]) for p in parts)
    assert est["dropped"] == digest.parse(parts)["dropped"]

    small = digest.estimate(payload, ("D0", "D1"), 128_000)
    assert small["dropped"] == [] and small["parts"] == 1


def test_unknown_block_id_is_refused():
    with pytest.raises(PmuError) as e:
        digest.export(make_payload(), blocks=("D0", "D7"))
    assert "D7" in e.value.what and e.value.do


# --------------------------------------------------------------------------- parts
def test_multipart_splits_parses_shuffled_and_names_a_missing_part():
    payload = make_payload(big=True)
    parts = digest.export(payload, budget=128_000, part_size=32_000)
    assert len(parts) > 1
    for i, part in enumerate(parts, 1):
        assert part.startswith("[pmukit-digest v1]")
        assert f"part {i}/{len(parts)}" in part.split("\n", 1)[0]
        assert len(part.encode("ascii")) <= 32_000

    shuffled = list(reversed(parts))
    assert digest.parse(shuffled)["params"] == payload["params"]
    assert digest.parse("\n".join(parts))["params"] == payload["params"]   # one big paste

    withheld = parts[:1] + parts[2:]
    with pytest.raises(PmuError) as e:
        digest.parse(withheld)
    assert e.value.what and e.value.why and e.value.do and e.value.where
    assert "part 2" in e.value.what and str(len(parts)) in e.value.what

    with pytest.raises(PmuError) as e:
        digest.parse(parts + parts[:1])
    assert "twice" in e.value.what


def test_relay_damage_that_must_still_parse():
    """What the air gap actually does to a paste: CRLF, a lost final newline, stray blank lines."""
    payload = make_payload(big=True)
    parts = digest.export(payload, budget=128_000, part_size=32_000)
    assert len(parts) > 1

    crlf = [p.replace("\n", "\r\n") for p in parts]
    assert digest.parse(crlf)["params"] == payload["params"]

    chopped = parts[:-1] + [parts[-1].rstrip("\n")]
    assert digest.parse(chopped)["params"] == payload["params"]

    assert digest.parse("\n\n" + "\n".join(parts))["params"] == payload["params"]


def test_version_mismatch_is_refused():
    part = digest.export(make_payload(), budget=128_000, part_size=128_000)[0]
    bad = part.replace("[pmukit-digest v1]", "[pmukit-digest v9]", 1)
    with pytest.raises(PmuError) as e:
        digest.parse(bad)
    assert "v9" in e.value.what and "v1" in e.value.why


def test_corrupted_body_fails_the_sha():
    part = digest.export(make_payload(), budget=128_000, part_size=128_000)[0]
    bad = part.replace("5d8ca1", "5d8ca2", 1)
    assert bad != part
    with pytest.raises(PmuError) as e:
        digest.parse(bad)
    assert "sha256" in e.value.what and "edited" in e.value.why
    assert e.value.do and e.value.where

    with pytest.raises(PmuError):
        digest.parse(part.split("[D9 trailer]", 1)[0])       # truncated: no trailer

    with pytest.raises(PmuError) as e:
        digest.parse("hello, this is not a digest")
    assert "not a pmukit digest" in e.value.what


# --------------------------------------------------------------------------- helper
def test_decimate_preserving_extremes_keeps_dip_and_overshoot():
    t, y = _tran()
    dip_i, over_i = int(np.argmin(y)), int(np.argmax(y))
    t2, y2 = decimate_preserving_extremes(t, y, 200)

    assert len(t2) <= 200 and len(t2) == len(y2)
    assert t[dip_i] in t2 and t[over_i] in t2
    # the argmin / argmax SAMPLES survive verbatim, not an interpolated neighbour
    assert y2[list(t2).index(t[dip_i])] == y[dip_i]
    assert y2[list(t2).index(t[over_i])] == y[over_i]
    assert y2.min() == y.min() and y2.max() == y.max()
    assert t2[0] == t[0] and t2[-1] == t[-1] and y2[-1] == y[-1]

    # shorter than the target: returned untouched
    t3, y3 = decimate_preserving_extremes(t[:10], y[:10], 200)
    assert np.array_equal(t3, t[:10]) and np.array_equal(y3, y[:10])

    with pytest.raises(PmuError):
        decimate_preserving_extremes(t[:5], y[:6], 3)


# --------------------------------------------------------------------------- entry points
def test_failure_bundle_carries_d0_d1_d6_and_nothing_else():
    text = "".join(digest.failure_bundle(make_payload()))
    heads = [h.split()[0] for h in _headers(text)]
    assert heads == ["D0", "D1", "D6", "D9"]
    back = digest.parse(digest.failure_bundle(make_payload()))
    assert back["params"] == {} and back["curves"] == {} and back["transients"] == {}
    assert back["faillog"] and back["ledger"] and back["provenance"]


def test_d1_reads_a_contract3_ledger_row_without_an_adapter():
    """`Run.to_dict()` uses process/temp_c/vset/load_key and `cpu_seconds`, not cell/cpu_s."""
    payload = dict(make_payload(curves=False, transients=False))
    payload["ledger"] = [
        {"run_id": "a8e05b3c9d14", "process": "ss", "temp_c": 125.0, "vset": 3,
         "load_key": "on_a", "analysis": "ac", "status": "done", "cpu_seconds": 188.0},
        {"run_id": "b1c2d3e4f506", "process": "tt", "temp_c": float("nan"), "vset": 3,
         "load_key": "", "analysis": "dc_temp", "status": "failed", "cpu_seconds": 3.5,
         "error": "license checkout timed out"},
    ]
    back = digest.parse(digest.export(payload, budget=128_000, part_size=128_000))
    assert [r["cell"] for r in back["ledger"]] == ["ss/125c/v3/on_a", "tt/Tsweep/v3"]
    assert back["ledger"][0]["cpu_s"] == 188.0
    assert back["ledger"][1]["error"] == "license checkout timed out"


def test_desk_bundle_can_narrow_to_one_cell():
    payload = make_payload()
    text = "".join(digest.desk_bundle(payload, cells=["ss/25c"], budget=128_000))
    assert "zout.VDD0P8_A" in text and "idc.IB_PTAT" not in text
    assert "load_on.VDD0P8_A" in text
    text_all = "".join(digest.desk_bundle(payload, budget=128_000))
    assert "idc.IB_PTAT" in text_all


def test_empty_payload_still_produces_a_verifiable_digest():
    parts = digest.export({})
    back = digest.parse(parts)
    assert back["provenance"] == {} and back["ledger"] == []
    assert back["meta"]["parts"] == 1


# --------------------------------------------------------------------------- encoding
def test_every_rendered_line_is_ascii_lf_and_untrailed():
    payload = make_payload(big=True)
    payload["ledger"][0]["analysis"] = "ac · inject VDD0P8_A · 100 µ"
    payload["provenance"]["tb_state_note"] = "RX → ±20 °C"
    for parts in (digest.export(payload, budget=128_000, part_size=32_000),
                  digest.failure_bundle(payload)):
        text = "".join(parts)
        text.encode("ascii")                       # raises on any non-ASCII byte
        assert "\r" not in text
        for line in text.split("\n"):
            assert line == line.rstrip(), repr(line)
    back = digest.parse(digest.export(payload, budget=128_000, part_size=32_000))
    assert back["ledger"][0]["analysis"] == "ac . inject VDD0P8_A . 100 u"
    assert back["provenance"]["tb_state_note"] == "RX -> ?20 degC"


def test_block_headers_are_the_only_lines_at_column_zero():
    text = "".join(digest.export(make_payload(big=True), budget=128_000, part_size=128_000))
    for line in text.split("\n"):
        if line and not line.startswith(" "):
            assert re.match(r"^\[(pmukit-digest v1\]|D\d+ )", line), repr(line)
