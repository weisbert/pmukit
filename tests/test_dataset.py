"""Contract 2: the dataset must be typed, per-port, NaN-honest and crash-safe.

Synthetic names only: rails VDD0P8_A / VDD0P8_B, biases IB_PTAT / IB_POLY, project demo_pmu.
"""
import json
import pathlib

import numpy as np
import pytest

from pmukit.dataset import Dataset, cell_key, parse_cell_key
from pmukit.errors import PmuError

FREQ = np.logspace(1, 10, 37)          # 10 Hz .. 10 GHz
VPIN = np.linspace(0.0, 0.8, 9)
TIME = np.linspace(0.0, 2e-6, 21)

DIMS = {
    "process": ["tt", "ss", "ff"],
    "temp_c": [-40, 25, 125],
    "vset": [3],
    "load_a": {"VDD0P8_A": [2e-6, 1e-4, 5e-4, 1e-3],
               "VDD0P8_B": [5e-6, 2e-4]},
}


def make(tmp_path, dims=None, project="demo_pmu"):
    return Dataset.create(tmp_path / "dataset", project=project,
                          config_sha="c0ffee123456", dims=dims or DIMS)


def declare_four(ds):
    """The four dim shapes of contract 2."""
    ds.declare("ac_zout.VDD0P8_A",
               dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", unit="ohm", coord=FREQ, coord_name="freq_ac")
    ds.declare("noise_v.VDD0P8_A",
               dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="float64", unit="V^2/Hz", coord=FREQ, coord_name="freq_ac")
    ds.declare("dc_iv.IB_PTAT",
               dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", unit="A", coord=VPIN)
    ds.declare("tran_load_on.VDD0P8_A",
               dims=("process", "temp_c", "vset", "time_s"),
               dtype="float64", unit="V", coord=TIME)


CELL_A = {"process": "tt", "temp_c": 25, "vset": 3, "load_a": 5e-4}
CELL_NOLOAD = {"process": "tt", "temp_c": 25, "vset": 3}


# --------------------------------------------------------------------------- round trip


def test_roundtrip_all_four_shapes(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    z = (1.0 + 0.5j) * np.arange(FREQ.size) + 0.25j
    n = 1e-17 / (1.0 + FREQ / 1e3)
    iv = 1e-6 * VPIN**2
    tr = 0.8 - 0.05 * np.exp(-TIME / 1e-7)
    ds.put("ac_zout.VDD0P8_A", CELL_A, z)
    ds.put("noise_v.VDD0P8_A", CELL_A, n)
    ds.put("dc_iv.IB_PTAT", CELL_NOLOAD, iv)
    ds.put("tran_load_on.VDD0P8_A", CELL_NOLOAD, tr)
    ds.close()

    again = Dataset.open(tmp_path / "dataset")
    assert again.project == "demo_pmu"
    assert again.config_sha == "c0ffee123456"
    assert again.variables() == ["ac_zout.VDD0P8_A", "dc_iv.IB_PTAT",
                                 "noise_v.VDD0P8_A", "tran_load_on.VDD0P8_A"]
    np.testing.assert_array_equal(again.get("ac_zout.VDD0P8_A", CELL_A), z)
    np.testing.assert_array_equal(again.get("noise_v.VDD0P8_A", CELL_A), n)
    np.testing.assert_array_equal(again.get("dc_iv.IB_PTAT", CELL_NOLOAD), iv)
    np.testing.assert_array_equal(again.get("tran_load_on.VDD0P8_A", CELL_NOLOAD), tr)
    np.testing.assert_array_equal(again.coord("ac_zout.VDD0P8_A"), FREQ)
    np.testing.assert_array_equal(again.coord("dc_iv.IB_PTAT"), VPIN)
    # full array keeps the declared hyper-rectangle; only the written cell is non-NaN
    full = again.get("ac_zout.VDD0P8_A")
    assert full.shape == (3, 3, 1, 4, FREQ.size)
    assert full.dtype == np.complex128
    assert np.isnan(full[1, 1, 0, 2]).all()          # ss/25C/vset3/5e-4 never run
    again.close()


def test_index_json_matches_contract_shape(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.put("ac_zout.VDD0P8_A", CELL_A, np.ones(FREQ.size, dtype=complex))
    ds.close()
    raw = json.loads((tmp_path / "dataset" / "index.json").read_text(encoding="utf-8"))
    assert set(raw) == {"project", "config_sha", "created", "dims", "variables", "missing"}
    assert raw["dims"]["load_a"]["VDD0P8_B"] == [5e-6, 2e-4]
    assert raw["dims"]["freq_hz"] == "per-variable coordinate"
    rec = raw["variables"]["ac_zout.VDD0P8_A"]
    assert rec["dims"] == ["process", "temp_c", "vset", "load_a", "freq_hz"]
    assert rec["dtype"] == "complex128" and rec["file"] == "ac_zout.VDD0P8_A.npy"
    assert rec["coord"] == "freq_ac.npy"
    assert (tmp_path / "dataset" / "freq_ac.npy").is_file()
    # written through: no temp files survive a mutation
    assert not list((tmp_path / "dataset").glob("*.tmp"))


def test_declared_cells_are_nan(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    z = ds.get("ac_zout.VDD0P8_A", CELL_A)
    assert np.isnan(z.real).all() and np.isnan(z.imag).all()
    assert np.isnan(ds.get("dc_iv.IB_PTAT", CELL_NOLOAD)).all()


def test_returned_arrays_are_read_only(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(ValueError):
        ds.get("dc_iv.IB_PTAT", CELL_NOLOAD)[0] = 1.0


# --------------------------------------------------------------------------- per-port loads


def test_per_port_load_grids(tmp_path):
    ds = make(tmp_path)
    ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", coord=FREQ, coord_name="freq_ac")
    ds.declare("ac_zout.VDD0P8_B", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", coord=FREQ, coord_name="freq_ac")
    assert ds.get("ac_zout.VDD0P8_A").shape == (3, 3, 1, 4, FREQ.size)
    assert ds.get("ac_zout.VDD0P8_B").shape == (3, 3, 1, 2, FREQ.size)
    assert ds.axis("load_a", "VDD0P8_A") == [2e-6, 1e-4, 5e-4, 1e-3]
    assert ds.axis("load_a", "VDD0P8_B") == [5e-6, 2e-4]

    # each variable indexes its OWN grid: 5e-4 exists on A, not on B
    ds.put("ac_zout.VDD0P8_A", CELL_A, np.ones(FREQ.size, dtype=complex))
    with pytest.raises(PmuError) as e:
        ds.put("ac_zout.VDD0P8_B", CELL_A, np.ones(FREQ.size, dtype=complex))
    assert "load_a" in str(e.value) and "5e-06" in str(e.value)
    b_cell = dict(CELL_A, load_a=2e-4)
    ds.put("ac_zout.VDD0P8_B", b_cell, 2 * np.ones(FREQ.size, dtype=complex))
    assert ds.get("ac_zout.VDD0P8_B", b_cell)[0] == 2
    ds.close()


def test_load_axis_needs_a_port(tmp_path):
    ds = make(tmp_path)
    with pytest.raises(PmuError) as e:
        ds.axis("load_a")
    assert "port" in str(e.value)
    with pytest.raises(PmuError) as e:
        ds.declare("ac_zout.VDD0P8_C", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
                   dtype="complex128", coord=FREQ)
    assert "VDD0P8_C" in str(e.value)


# --------------------------------------------------------------------------- put validation


def test_put_off_axis_value_names_key_and_lists_axis(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError) as e:
        ds.put("ac_zout.VDD0P8_A", dict(CELL_A, temp_c=85), np.ones(FREQ.size, dtype=complex))
    msg = str(e.value)
    assert "temp_c" in msg and "85" in msg
    assert "-40" in msg and "25" in msg and "125" in msg          # the axis is listed

    with pytest.raises(PmuError) as e:
        ds.put("ac_zout.VDD0P8_A", dict(CELL_A, process="sf"), np.ones(FREQ.size, dtype=complex))
    assert "process" in str(e.value) and "'tt'" in str(e.value)


def test_put_wrong_length(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError) as e:
        ds.put("ac_zout.VDD0P8_A", CELL_A, np.ones(FREQ.size - 1, dtype=complex))
    assert f"{FREQ.size}" in str(e.value) and "freq_hz" in str(e.value)
    with pytest.raises(PmuError):
        ds.put("ac_zout.VDD0P8_A", CELL_A, 1.0 + 0j)              # scalar into a swept variable


def test_put_missing_and_extra_cell_keys(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError) as e:
        ds.put("ac_zout.VDD0P8_A", CELL_NOLOAD, np.ones(FREQ.size, dtype=complex))
    assert "load_a" in str(e.value)
    with pytest.raises(PmuError) as e:
        ds.put("dc_iv.IB_PTAT", CELL_A, np.ones(VPIN.size))
    assert "load_a" in str(e.value)


def test_put_complex_into_real_variable(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError) as e:
        ds.put("noise_v.VDD0P8_A", CELL_A, np.ones(FREQ.size, dtype=complex))
    assert "complex" in str(e.value)


def test_put_scalar_variable(tmp_path):
    ds = make(tmp_path)
    ds.declare("dc_dropout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a"),
               dtype="float64", unit="V")
    ds.put("dc_dropout.VDD0P8_A", CELL_A, 0.115)
    assert float(ds.get("dc_dropout.VDD0P8_A", CELL_A)) == 0.115
    assert ds.coord("dc_dropout.VDD0P8_A") is None
    with pytest.raises(PmuError):
        ds.put("dc_dropout.VDD0P8_A", CELL_A, np.ones(3))


def test_float_axis_tolerance(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    iv = 1e-6 * VPIN
    ds.put("dc_iv.IB_PTAT", {"process": "tt", "temp_c": 25.0, "vset": 3}, iv)   # 25.0 -> axis 25
    np.testing.assert_array_equal(ds.get("dc_iv.IB_PTAT", CELL_NOLOAD), iv)
    # within rtol 1e-9 still matches, well outside it does not
    assert ds.has("dc_iv.IB_PTAT", {"process": "tt", "temp_c": 25 + 1e-9, "vset": 3})
    with pytest.raises(PmuError):
        ds.has("dc_iv.IB_PTAT", {"process": "tt", "temp_c": 25.1, "vset": 3})
    # a float load also finds an int-free float grid, and vset 3.0 finds 3
    ds.put("ac_zout.VDD0P8_A", {"process": "tt", "temp_c": 25.0, "vset": 3.0, "load_a": 0.0005},
           np.ones(FREQ.size, dtype=complex))
    assert ds.has("ac_zout.VDD0P8_A", CELL_A)


# --------------------------------------------------------------------------- missing / coverage


def test_mark_missing_and_coverage(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.put("dc_iv.IB_PTAT", CELL_NOLOAD, 1e-6 * VPIN)
    ds.mark_missing("dc_iv.IB_PTAT", {"process": "ss", "temp_c": 125, "vset": 3},
                    "run failed: spectre exited 1")

    broke = {"process": "ss", "temp_c": 125, "vset": 3}
    assert np.isnan(ds.get("dc_iv.IB_PTAT", broke)).all()          # still NaN
    assert ds.has("dc_iv.IB_PTAT", CELL_NOLOAD) is True            # filled
    assert ds.has("dc_iv.IB_PTAT", broke) is False                 # ran and broke
    assert ds.has("dc_iv.IB_PTAT", {"process": "ff", "temp_c": -40, "vset": 3}) is False

    rows = ds.missing()
    assert rows == [["dc_iv.IB_PTAT", "ss", 125, 3, "run failed: spectre exited 1"]]
    assert ds.missing("ac_zout.VDD0P8_A") == []

    cov = ds.coverage("dc_iv.IB_PTAT")
    assert cov == {"declared": 9, "filled": 1, "missing": 1, "never_run": 7}
    assert sum(cov[k] for k in ("filled", "missing", "never_run")) == cov["declared"]
    empty = ds.coverage("ac_zout.VDD0P8_A")
    assert empty == {"declared": 36, "filled": 0, "missing": 0, "never_run": 36}

    ds.close()
    again = Dataset.open(tmp_path / "dataset")
    assert again.missing() == rows
    assert again.coverage("dc_iv.IB_PTAT") == cov
    again.close()


def test_mark_missing_needs_a_reason_and_a_real_cell(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError):
        ds.mark_missing("dc_iv.IB_PTAT", CELL_NOLOAD, "   ")
    with pytest.raises(PmuError) as e:
        ds.mark_missing("dc_iv.IB_PTAT", {"process": "zz", "temp_c": 25, "vset": 3}, "nope")
    assert "process" in str(e.value)
    ds.mark_missing("dc_iv.IB_PTAT", CELL_NOLOAD, "first")
    ds.mark_missing("dc_iv.IB_PTAT", CELL_NOLOAD, "second")       # same cell, reason replaced
    assert ds.missing() == [["dc_iv.IB_PTAT", "tt", 25, 3, "second"]]


def test_a_successful_retry_clears_the_missing_row(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.mark_missing("dc_iv.IB_PTAT", CELL_NOLOAD, "run failed: spectre exited 1")
    assert ds.has("dc_iv.IB_PTAT", CELL_NOLOAD) is False
    ds.put("dc_iv.IB_PTAT", CELL_NOLOAD, 1e-6 * VPIN)             # the retry worked
    assert ds.missing() == []
    assert ds.has("dc_iv.IB_PTAT", CELL_NOLOAD) is True
    assert ds.coverage("dc_iv.IB_PTAT") == {"declared": 9, "filled": 1, "missing": 0,
                                            "never_run": 8}
    ds.close()
    assert Dataset.open(tmp_path / "dataset").missing() == []


def test_mark_missing_overrides_data_already_there(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.put("dc_iv.IB_PTAT", CELL_NOLOAD, 1e-6 * VPIN)
    ds.mark_missing("dc_iv.IB_PTAT", CELL_NOLOAD, "bias never settled, curve is junk")
    assert ds.has("dc_iv.IB_PTAT", CELL_NOLOAD) is False
    assert ds.coverage("dc_iv.IB_PTAT") == {"declared": 9, "filled": 0, "missing": 1,
                                            "never_run": 8}


def test_missing_rows_carry_load_a_when_the_variable_has_it(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.mark_missing("ac_zout.VDD0P8_A", CELL_A, "ac diverged")
    assert ds.missing() == [["ac_zout.VDD0P8_A", "tt", 25, 3, 5e-4, "ac diverged"]]


def test_unknown_variable_is_named(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    with pytest.raises(PmuError) as e:
        ds.get("ac_psrr.VDD0P8_A", CELL_A)
    assert "ac_psrr.VDD0P8_A" in str(e.value)


# --------------------------------------------------------------------------- sha


def test_sha_stable_across_close_open_and_sensitive_to_one_cell(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.put("noise_v.VDD0P8_A", CELL_A, np.ones(FREQ.size))
    first = ds.sha()
    assert len(first) == 12 and int(first, 16) >= 0
    ds.close()

    again = Dataset.open(tmp_path / "dataset")
    assert again.sha() == first

    changed = np.ones(FREQ.size)
    changed[5] = 1.5
    again.put("noise_v.VDD0P8_A", CELL_A, changed)
    second = again.sha()
    assert second != first

    again.mark_missing("noise_v.VDD0P8_A", dict(CELL_A, process="ss"), "run failed")
    assert again.sha() != second
    again.close()


# --------------------------------------------------------------------------- cell keys


@pytest.mark.parametrize("cell,key", [
    ({"process": "tt", "temp_c": 25, "vset": 3, "load_a": 5e-4}, "tt/25C/vset3/5.0e-04A"),
    ({"process": "ss", "temp_c": -40.0, "vset": 3}, "ss/-40C/vset3"),
    ({"process": "ff", "temp_c": 125, "vset": 0, "load_a": 2e-6}, "ff/125C/vset0/2.0e-06A"),
    ({"process": "tt"}, "tt"),
])
def test_cell_key_roundtrip(cell, key):
    assert cell_key(cell) == key
    back = parse_cell_key(key)
    assert set(back) == set(cell)
    for k, v in cell.items():
        if k == "process":
            assert back[k] == v
        else:
            assert back[k] == pytest.approx(float(v))
    assert cell_key(back) == key


def test_cell_key_rejects_unknown_key():
    with pytest.raises(PmuError) as e:
        cell_key({"process": "tt", "corner": "tt"})
    assert "corner" in str(e.value)


# --------------------------------------------------------------------------- declare


def test_declare_twice_identical_is_a_noop(tmp_path):
    ds = make(tmp_path)
    ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", unit="ohm", coord=FREQ, coord_name="freq_ac")
    ds.put("ac_zout.VDD0P8_A", CELL_A, np.ones(FREQ.size, dtype=complex))
    before = ds.sha()
    ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", unit="ohm", coord=FREQ, coord_name="freq_ac")
    assert ds.sha() == before                                     # data survived
    assert ds.get("ac_zout.VDD0P8_A", CELL_A)[0] == 1


def test_declare_twice_different_signature_raises(tmp_path):
    ds = make(tmp_path)
    kw = dict(dims=("process", "temp_c", "vset", "load_a", "freq_hz"), coord=FREQ,
              coord_name="freq_ac")
    ds.declare("ac_zout.VDD0P8_A", dtype="complex128", unit="ohm", **kw)
    with pytest.raises(PmuError) as e:
        ds.declare("ac_zout.VDD0P8_A", dtype="float64", unit="ohm", **kw)
    assert "dtype" in str(e.value)
    with pytest.raises(PmuError):
        ds.declare("ac_zout.VDD0P8_A", dtype="complex128", unit="V", **kw)
    with pytest.raises(PmuError) as e:
        ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "freq_hz"),
                   dtype="complex128", unit="ohm", coord=FREQ, coord_name="freq_ac")
    assert "dims" in str(e.value)
    with pytest.raises(PmuError) as e:                            # same name, other grid
        ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
                   dtype="complex128", unit="ohm", coord=FREQ[:-1], coord_name="freq_ac")
    assert "freq" in str(e.value)


def test_declare_rejects_bad_shapes(tmp_path):
    ds = make(tmp_path)
    with pytest.raises(PmuError) as e:                            # wrong dim order
        ds.declare("ac_zout.VDD0P8_A", dims=("temp_c", "process", "vset", "freq_hz"),
                   dtype="complex128", coord=FREQ)
    assert "order" in str(e.value)
    with pytest.raises(PmuError) as e:                            # coordinate not last
        ds.declare("ac_zout.VDD0P8_A", dims=("process", "freq_hz", "temp_c"),
                   dtype="complex128", coord=FREQ)
    assert "freq_hz" in str(e.value) or "temp_c" in str(e.value)
    with pytest.raises(PmuError) as e:                            # no NaN in an int array
        ds.declare("dc_iv.IB_POLY", dims=("process", "temp_c", "vset", "vpin_v"),
                   dtype="int64", coord=VPIN)
    assert "NaN" in str(e.value)
    with pytest.raises(PmuError) as e:                            # coordinate without values
        ds.declare("dc_iv.IB_POLY", dims=("process", "temp_c", "vset", "vpin_v"), dtype="float64")
    assert "coord" in str(e.value)
    with pytest.raises(PmuError) as e:                            # values without a coordinate dim
        ds.declare("dc_dropout.VDD0P8_A", dims=("process", "temp_c", "vset"), dtype="float64",
                   coord=VPIN)
    assert "coord" in str(e.value)
    with pytest.raises(PmuError) as e:                            # not observable.port
        ds.declare("ac_zout", dims=("process", "temp_c", "vset"), dtype="float64")
    assert "observable.port" in str(e.value)
    assert ds.variables() == []


def test_shared_coordinate_file_must_match(tmp_path):
    ds = make(tmp_path)
    ds.declare("ac_zout.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", coord=FREQ, coord_name="freq_ac")
    with pytest.raises(PmuError) as e:
        ds.declare("ac_psrr.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
                   dtype="complex128", coord=FREQ * 2, coord_name="freq_ac")
    assert "freq_ac.npy" in str(e.value)
    # default coord_name keeps every variable independent
    ds.declare("ac_psrr.VDD0P8_A", dims=("process", "temp_c", "vset", "load_a", "freq_hz"),
               dtype="complex128", coord=FREQ * 2)
    np.testing.assert_array_equal(ds.coord("ac_psrr.VDD0P8_A"), FREQ * 2)
    np.testing.assert_array_equal(ds.coord("ac_zout.VDD0P8_A"), FREQ)


# --------------------------------------------------------------------------- create / open


def test_create_refuses_to_overwrite(tmp_path):
    make(tmp_path)
    with pytest.raises(PmuError) as e:
        make(tmp_path)
    assert "already exists" in str(e.value)


def test_create_validates_dims(tmp_path):
    with pytest.raises(PmuError) as e:
        Dataset.create(tmp_path / "d1", project="demo_pmu", config_sha="x",
                       dims={"process": [], "temp_c": [25]})
    assert "process" in str(e.value)
    with pytest.raises(PmuError) as e:
        Dataset.create(tmp_path / "d2", project="demo_pmu", config_sha="x",
                       dims={"process": ["tt"], "temp": [25]})
    assert "temp" in str(e.value)
    with pytest.raises(PmuError) as e:
        Dataset.create(tmp_path / "d3", project="demo_pmu", config_sha="x",
                       dims={"process": ["tt"], "load_a": [1e-4]})
    assert "port" in str(e.value)


def test_open_missing_or_malformed_index(tmp_path):
    with pytest.raises(PmuError) as e:
        Dataset.open(tmp_path / "nothing")
    assert "index.json" in str(e.value)

    d = tmp_path / "broken"
    d.mkdir()
    (d / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PmuError):
        Dataset.open(d)

    (d / "index.json").write_text('{"project": "demo_pmu"}', encoding="utf-8")
    with pytest.raises(PmuError) as e:
        Dataset.open(d)
    assert "config_sha" in str(e.value)

    ds = make(tmp_path)
    declare_four(ds)
    ds.close()
    idx = pathlib.Path(tmp_path / "dataset" / "index.json")
    raw = json.loads(idx.read_text(encoding="utf-8"))
    raw["missing"] = [["dc_iv.IB_PTAT", "tt", 25, "reason but no vset"]]
    idx.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PmuError) as e:
        Dataset.open(tmp_path / "dataset")
    assert "missing" in str(e.value)


def test_interrupted_write_leaves_the_last_good_state(tmp_path):
    """Crash safety: every mutation lands through <name>.tmp + os.replace, so a killed process
    leaves either the old file or the new one -- and a stray .tmp is ignored."""
    ds = make(tmp_path)
    declare_four(ds)
    ds.put("noise_v.VDD0P8_A", CELL_A, np.ones(FREQ.size))
    good = ds.sha()
    ds.close()

    d = tmp_path / "dataset"
    (d / "index.json.tmp").write_text('{"half wri', encoding="utf-8")       # killed mid-write
    (d / "noise_v.VDD0P8_A.npy.tmp").write_bytes(b"\x93NUMPY truncated")

    again = Dataset.open(d)
    np.testing.assert_array_equal(again.get("noise_v.VDD0P8_A", CELL_A), np.ones(FREQ.size))
    assert again.sha() == good                                   # .tmp leftovers are not the data
    again.put("noise_v.VDD0P8_A", dict(CELL_A, process="ss"), 2 * np.ones(FREQ.size))
    assert json.loads((d / "index.json").read_text(encoding="utf-8"))["variables"]
    again.close()


def test_closed_dataset_refuses_work(tmp_path):
    ds = make(tmp_path)
    declare_four(ds)
    ds.close()
    ds.close()                                                     # idempotent
    with pytest.raises(PmuError) as e:
        ds.get("dc_iv.IB_PTAT", CELL_NOLOAD)
    assert "closed" in str(e.value)


def test_context_manager_and_summary(tmp_path):
    with make(tmp_path) as ds:
        declare_four(ds)
        ds.put("noise_v.VDD0P8_A", CELL_A, np.ones(FREQ.size))
        ds.mark_missing("noise_v.VDD0P8_A", dict(CELL_A, process="ff"), "psf truncated")
        s = ds.summary()
    assert s["project"] == "demo_pmu"
    assert s["dataset_sha"] == json.loads(json.dumps(s))["dataset_sha"]      # JSON-safe
    assert s["variables"]["noise_v.VDD0P8_A"]["shape"] == [3, 3, 1, 4, FREQ.size]
    assert s["variables"]["noise_v.VDD0P8_A"]["coverage"] == {
        "declared": 36, "filled": 1, "missing": 1, "never_run": 34}
    assert s["totals"]["declared"] == 36 + 36 + 9 + 9
    assert s["totals"]["filled"] == 1
    assert s["missing"][0][0] == "noise_v.VDD0P8_A"
    assert s["dims"]["load_a"]["VDD0P8_A"] == [2e-6, 1e-4, 5e-4, 1e-3]


def test_big_variable_is_memory_mapped_and_still_writable(tmp_path):
    """> 8 MB: read through a memory map, copy on write, and the file still swaps on Windows."""
    big = np.linspace(0.0, 1.0, 1_200_000)                         # 9.6 MB per corner row
    ds = Dataset.create(tmp_path / "dataset", project="demo_pmu", config_sha="x",
                        dims={"process": ["tt"], "temp_c": [25], "vset": [3]})
    ds.declare("tran_load_on.VDD0P8_A", dims=("process", "temp_c", "vset", "time_s"),
               dtype="float64", coord=big)
    ds.close()

    again = Dataset.open(tmp_path / "dataset")
    assert (tmp_path / "dataset" / "tran_load_on.VDD0P8_A.npy").stat().st_size > (8 << 20)
    arr = again.get("tran_load_on.VDD0P8_A")
    assert isinstance(arr, np.memmap)                              # mapped, not read into RAM
    del arr
    wave = 0.8 - 0.05 * np.exp(-big)
    again.put("tran_load_on.VDD0P8_A", CELL_NOLOAD, wave)
    np.testing.assert_array_equal(again.get("tran_load_on.VDD0P8_A", CELL_NOLOAD), wave)
    again.close()

    third = Dataset.open(tmp_path / "dataset")
    np.testing.assert_array_equal(third.get("tran_load_on.VDD0P8_A", CELL_NOLOAD), wave)
    third.close()


def test_works_under_pmukit_data(tmp_path, monkeypatch):
    """Nothing is hardcoded: $PMUKIT_DATA may point anywhere."""
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "root"))
    from pmukit import paths
    proj = paths.ensure_project("demo_pmu")
    assert proj == tmp_path / "root" / "demo_pmu"
    ds = Dataset.create(proj / "dataset", project="demo_pmu", config_sha="x", dims=DIMS)
    ds.declare("dc_iv.IB_POLY", dims=("process", "temp_c", "vset", "vpin_v"),
               dtype="float64", coord=VPIN)
    ds.put("dc_iv.IB_POLY", CELL_NOLOAD, 3e-6 * VPIN)
    ds.close()
    assert (proj / "dataset" / "index.json").is_file()
    np.testing.assert_array_equal(
        Dataset.open(proj / "dataset").get("dc_iv.IB_POLY", CELL_NOLOAD), 3e-6 * VPIN)
