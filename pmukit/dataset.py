"""Contract 2: the characterization dataset -- typed, dimensioned, NaN-honest.

A dataset is a DIRECTORY (`$PMUKIT_DATA/<project>/dataset/`): one `index.json` declaring the axes
and the variables, plus one `.npy` per variable and one per sweep coordinate.  It replaces the old
string-keyed npz (`tr_<net>_<corner>_<temp>`) with a store that knows its own dimensions.  It never
enters git.

Dimension order is fixed for every variable::

    process, temp_c, vset, [load_a], [sweep coordinate]

* `load_a` is PER PORT: `dims["load_a"]` is `{port: [currents]}` because rails carry different load
  grids.  A variable takes the grid of the port in its OWN name (`ac_zout.VDD0P8_A` -> `VDD0P8_A`).
* the trailing sweep coordinate (`freq_hz`, `time_s`, `vpin_v`, `temp_sweep_c`) is PER VARIABLE: it
  lives in its own `.npy` (the record's `coord` field) and `dims` only carries the marker string
  "per-variable coordinate".

NaN means "no data".  Every cell is in exactly one of three states, and the fitter must be able to
tell them apart:

    filled      some non-NaN data was written
    missing     registered through `mark_missing` -- it ran and it broke, with a reason
    never_run   declared, still all-NaN, nobody said why

Durability: every mutation (`declare`, `put`, `mark_missing`) writes through -- first the affected
`.npy`, then `index.json`, each through a temp file + `os.replace`.  A crash therefore always leaves
a readable dataset: at worst the last cell is lost, never half-written, and `index.json` never
claims data that is not on disk because cell presence is read from the array, not from the index.
"""
from __future__ import annotations

import copy
import gc
import json
import os
import pathlib
import re
from datetime import datetime
from typing import Any

import numpy as np

from . import jsonio
from .errors import PmuError

__all__ = ["Dataset", "cell_key", "parse_cell_key", "CELL_DIMS", "PER_VARIABLE"]

#: the cell axes, in the one order every variable must use
CELL_DIMS = ("process", "temp_c", "vset", "load_a")
#: what `dims[<coordinate>]` says in index.json
PER_VARIABLE = "per-variable coordinate"
#: index file name inside the dataset directory
INDEX_NAME = "index.json"
#: arrays bigger than this are memory-mapped on read (copied on write)
MMAP_BYTES = 8 << 20
#: float axis matching: relative only, so 25.0 finds the axis entry 25
RTOL = 1e-9

_VAR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_+-]*\.[A-Za-z0-9_][A-Za-z0-9_+-]*$")
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_TEMP_RE = re.compile(rf"^({_NUM})C$")
_VSET_RE = re.compile(rf"^vset({_NUM})$")
_LOAD_RE = re.compile(rf"^({_NUM})A$")


# --------------------------------------------------------------------------- helpers


def _py(value: Any) -> Any:
    """numpy scalar -> plain python, so it can go into index.json."""
    return value.item() if isinstance(value, np.generic) else value


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)


def _fmt(values) -> str:
    return ", ".join(repr(_py(v)) for v in values)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_text(path: pathlib.Path, text: str) -> None:
    """Write `text` (LF) through a temp file + os.replace, fsynced before the swap."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    _replace(tmp, path)


def _save_npy(path: pathlib.Path, arr: np.ndarray) -> None:
    """Write one `.npy` atomically, never pickling."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.save(fh, arr, allow_pickle=False)
        fh.flush()
        os.fsync(fh.fileno())
    _replace(tmp, path)


def _replace(tmp: pathlib.Path, path: pathlib.Path) -> None:
    """os.replace, with one gc pass in case a stale memory map still holds the target (Windows)."""
    try:
        os.replace(tmp, path)
        return
    except OSError:
        gc.collect()
    try:
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PmuError(
            what=f"Could not update {path.name}.",
            why=f"the operating system refused to replace it ({exc.__class__.__name__}: {exc}); on "
                "Windows this happens when an array returned by get() is still memory-mapped",
            do=["drop references to arrays returned by get() before writing, then retry",
                "close other programs holding the dataset directory open"],
            where=str(path),
        ) from exc


def _axis_index(axis: list, value: Any, *, dim: str, var: str, where: str) -> int:
    """Position of `value` on `axis`: exact for strings/ints, np.isclose (rtol 1e-9) for floats."""
    for i, a in enumerate(axis):
        if isinstance(a, str) or isinstance(value, str):
            if a == value:
                return i
            continue
        try:
            if a == value or np.isclose(float(a), float(value), rtol=RTOL, atol=0.0):
                return i
        except (TypeError, ValueError):
            continue
    raise PmuError(
        what=f"{var}: {dim}={_py(value)!r} is not on the {dim} axis.",
        why="a cell can only name a value the dataset was created with; the axes are fixed at "
            "create() time so the stored hyper-rectangle stays rectangular",
        do=[f"use one of: {_fmt(axis)}",
            f"or re-create the dataset with {dim} extended to include {_py(value)!r}"],
        where=where,
    )


def _split_dims(var: str, dims: tuple[str, ...], where: str) -> tuple[tuple[str, ...], str]:
    """(cell dims, sweep coordinate name) -- validates the fixed order."""
    if not dims:
        raise PmuError(
            what=f"{var}: dims is empty.",
            why="every variable needs at least one dimension; the fixed order is "
                f"{', '.join(CELL_DIMS)}, [sweep coordinate]",
            do=["declare it with e.g. dims=('process', 'temp_c', 'vset', 'freq_hz')"],
            where=where,
        )
    cell: list[str] = []
    coord_dim = ""
    for i, d in enumerate(dims):
        if d in CELL_DIMS:
            if coord_dim:
                raise PmuError(
                    what=f"{var}: dim {d!r} comes after the sweep coordinate {coord_dim!r}.",
                    why="the sweep coordinate is always last -- it is the only per-variable axis",
                    do=[f"order the dims as {', '.join(CELL_DIMS)}, [sweep coordinate]"],
                    where=where,
                )
            cell.append(d)
        elif i != len(dims) - 1:
            raise PmuError(
                what=f"{var}: unknown dim {d!r}.",
                why=f"only {', '.join(CELL_DIMS)} are cell axes; any other name is read as the "
                    "sweep coordinate and must be the LAST dim",
                do=[f"fix the spelling, or move {d!r} to the end if it is the sweep coordinate"],
                where=where,
            )
        else:
            coord_dim = d
    if len(set(cell)) != len(cell):
        raise PmuError(
            what=f"{var}: duplicate dim in {list(dims)}.",
            why="each axis may appear once",
            do=["remove the repeated name"],
            where=where,
        )
    order = [CELL_DIMS.index(d) for d in cell]
    if order != sorted(order):
        raise PmuError(
            what=f"{var}: dims {list(dims)} are out of order.",
            why=f"the dimension order is fixed at {', '.join(CELL_DIMS)}, [sweep coordinate] so "
                "every variable indexes the same way",
            do=[f"reorder to {', '.join(d for d in CELL_DIMS if d in cell)}"
                + (f", {coord_dim}" if coord_dim else "")],
            where=where,
        )
    return tuple(cell), coord_dim


def _axis_values(name: str, raw: Any, where: str) -> list:
    """Validate and normalise one axis from `dims`."""
    if not isinstance(raw, (list, tuple)) or len(raw) == 0:
        raise PmuError(
            what=f"Axis {name!r} is not a non-empty list.",
            why="an axis with no points would allocate a zero-size array, and nothing could be "
                "written to it",
            do=[f"pass {name} as a list with at least one value"],
            where=where,
        )
    vals = [_py(v) for v in raw]
    if name == "process":
        bad = [v for v in vals if not isinstance(v, str) or not v]
        if bad:
            raise PmuError(
                what=f"process axis has non-string entries: {_fmt(bad)}.",
                why="process corners are Spectre section names, always text",
                do=["pass e.g. ['tt', 'ss', 'ff']"],
                where=where,
            )
    elif name == "vset":
        out = []
        for v in vals:
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if not isinstance(v, int) or isinstance(v, bool):
                raise PmuError(
                    what=f"vset axis entry {v!r} is not an integer code.",
                    why="VSET is a register code the tool writes into `parameters VSET=`",
                    do=["pass e.g. [3] or [0, 1, 2, 3]"],
                    where=where,
                )
            out.append(v)
        vals = out
    else:  # temp_c, load_a
        bad = [v for v in vals if not _is_num(v)]
        if bad:
            raise PmuError(
                what=f"{name} axis has non-numeric entries: {_fmt(bad)}.",
                why=f"{name} is a physical quantity and must be a number",
                do=[f"pass {name} as a list of numbers"],
                where=where,
            )
        vals = [float(v) if name == "load_a" else v for v in vals]
    if len(set(map(repr, vals))) != len(vals):
        raise PmuError(
            what=f"Axis {name!r} repeats a value: {_fmt(vals)}.",
            why="a repeated axis point would make two different cells share one slot",
            do=["remove the duplicate"],
            where=where,
        )
    return vals


def _validate_dims(dims: Any, where: str) -> dict:
    """Normalise the `dims` block of index.json (also used to re-validate on open)."""
    if not isinstance(dims, dict) or not dims:
        raise PmuError(
            what="dims must be a non-empty dict.",
            why="the dataset cannot allocate anything without its axes",
            do=["pass e.g. {'process': ['tt'], 'temp_c': [25], 'vset': [3], "
                "'load_a': {'VDD0P8_A': [5e-4]}}"],
            where=where,
        )
    out: dict[str, Any] = {}
    for name, axis in dims.items():
        name = str(name)
        if name == "load_a":
            if not isinstance(axis, dict) or not axis:
                raise PmuError(
                    what="dims['load_a'] must be a non-empty {port: [currents]} dict.",
                    why="load grids are per port -- rails carry different currents, so one shared "
                        "list would silently mis-index the other rail",
                    do=["pass e.g. {'VDD0P8_A': [2e-6, 1e-4, 5e-4], 'VDD0P8_B': [1e-5, 1e-4]}"],
                    where=where,
                )
            out["load_a"] = {str(p): _axis_values("load_a", g, where) for p, g in axis.items()}
        elif isinstance(axis, str):
            if axis != PER_VARIABLE:
                raise PmuError(
                    what=f"dims[{name!r}] is the string {axis!r}.",
                    why=f"a string axis only means {PER_VARIABLE!r} -- the coordinate itself lives "
                        "in the variable's own .npy",
                    do=[f"pass a list of values, or the marker {PER_VARIABLE!r}"],
                    where=where,
                )
            out[name] = PER_VARIABLE
        elif name in CELL_DIMS:
            out[name] = _axis_values(name, axis, where)
        else:
            raise PmuError(
                what=f"dims[{name!r}] is a list, but {name!r} is not a cell axis.",
                why=f"only {', '.join(CELL_DIMS)} carry shared value lists; every other axis is a "
                    "per-variable sweep coordinate",
                do=[f"fix the spelling of {name!r}", f"or declare it as {PER_VARIABLE!r}"],
                where=where,
            )
    return out


def _check_var_name(var: str, where: str) -> None:
    if not _VAR_RE.match(var):
        raise PmuError(
            what=f"Variable name {var!r} is not of the form 'observable.port'.",
            why="contract 2 names every variable after its observable and its port (the port is "
                "how a load grid is found), and the name is also the .npy file name",
            do=["use e.g. 'ac_zout.VDD0P8_A' or 'dc_iv.IB_PTAT'"],
            where=where,
        )


def _ro(arr: np.ndarray) -> np.ndarray:
    """Read-only view: the dataset owns its buffers, callers must copy before modifying.

    The view keeps the array's class, so a memory-mapped variable stays a `np.memmap` and is not
    pulled into RAM by a read.
    """
    view = arr.view() if isinstance(arr, np.ndarray) else np.asarray(arr).view()
    view.flags.writeable = False
    return view


# --------------------------------------------------------------------------- cell keys


def cell_key(cell: dict) -> str:
    """Stable, human- and ledger-readable key for one cell, e.g. ``tt/25C/vset3/5.0e-04A``.

    Only the dims present are rendered, always in the fixed order.  Used for ledger `load_key`,
    log lines and digest text -- indexing inside the dataset uses the axis positions, not this.
    """
    if not isinstance(cell, dict):
        raise PmuError(
            what=f"cell must be a dict, got {type(cell).__name__}.",
            why="a cell names one point per axis, e.g. {'process': 'tt', 'temp_c': 25}",
            do=["pass a dict of axis -> value"],
            where="pmukit.dataset.cell_key",
        )
    extra = [k for k in cell if k not in CELL_DIMS]
    if extra:
        raise PmuError(
            what=f"cell has unknown key(s): {_fmt(extra)}.",
            why=f"a cell may only name {', '.join(CELL_DIMS)}",
            do=["drop the extra key, or fix its spelling"],
            where="pmukit.dataset.cell_key",
        )
    parts = []
    for d in CELL_DIMS:
        if d not in cell:
            continue
        v = _py(cell[d])
        if d == "process":
            s = str(v)
            if "/" in s:
                raise PmuError(
                    what=f"process name {s!r} contains '/'.",
                    why="'/' separates the segments of a cell key, so the key could not be parsed "
                        "back",
                    do=["rename the corner without a slash"],
                    where="pmukit.dataset.cell_key",
                )
            parts.append(s)
        elif d == "temp_c":
            parts.append(f"{float(v):g}C")
        elif d == "vset":
            parts.append(f"vset{float(v):g}")
        else:
            parts.append(f"{float(v):.1e}A")
    return "/".join(parts)


def parse_cell_key(key: str) -> dict:
    """Inverse of `cell_key`. Temperatures and loads come back as floats, vset as an int."""
    cell: dict[str, Any] = {}
    for seg in str(key).split("/"):
        if not seg:
            continue
        m = _VSET_RE.match(seg)
        if m:
            f = float(m.group(1))
            cell["vset"] = int(f) if f.is_integer() else f
            continue
        m = _TEMP_RE.match(seg)
        if m:
            cell["temp_c"] = float(m.group(1))
            continue
        m = _LOAD_RE.match(seg)
        if m:
            cell["load_a"] = float(m.group(1))
            continue
        if "process" in cell:
            raise PmuError(
                what=f"Cannot parse cell-key segment {seg!r}.",
                why="a segment is the process name, or <temp>C, or vset<code>, or <current>A",
                do=[f"check the key {key!r} against cell_key() output, e.g. 'tt/25C/vset3/5.0e-04A'"],
                where="pmukit.dataset.parse_cell_key",
            )
        cell["process"] = seg
    return cell


# --------------------------------------------------------------------------- the dataset


class Dataset:
    """A directory of `.npy` arrays plus `index.json` -- contract 2."""

    def __init__(self, path, index: dict) -> None:
        """Use `Dataset.create` / `Dataset.open`; this takes an already-validated index."""
        self.path = pathlib.Path(path)
        self._index = index
        self._cache: dict[str, np.ndarray] = {}
        self._coords: dict[str, np.ndarray] = {}
        self._missing: dict[tuple[str, tuple[int, ...]], str] = {}
        self._closed = False
        self._rebuild_missing()

    # ------------------------------------------------------------------ construction

    @classmethod
    def create(cls, path, *, project: str, config_sha: str, dims: dict) -> "Dataset":
        """Create an empty dataset directory. Refuses to overwrite an existing one."""
        p = pathlib.Path(path)
        idx = p / INDEX_NAME
        if idx.exists():
            raise PmuError(
                what=f"A dataset already exists at {p}.",
                why="create() never overwrites -- the existing index.json and .npy files are "
                    "characterization results that cost simulator time",
                do=["Dataset.open(path) to keep filling it",
                    "delete the directory first if you really want to start over"],
                where=str(idx),
            )
        clean = _validate_dims(dims, str(idx))
        index = {
            "project": str(project),
            "config_sha": str(config_sha),
            "created": _now(),
            "dims": clean,
            "variables": {},
            "missing": [],
        }
        p.mkdir(parents=True, exist_ok=True)
        ds = cls(p, index)
        ds._write_index()
        return ds

    @classmethod
    def open(cls, path) -> "Dataset":
        """Open an existing dataset. PmuError when index.json is missing or malformed."""
        p = pathlib.Path(path)
        idx = p / INDEX_NAME
        if not idx.is_file():
            raise PmuError(
                what=f"No dataset at {p}.",
                why=f"{INDEX_NAME} is missing -- a dataset is a directory of .npy files described "
                    "by that index",
                do=["run the characterization first (Plan -> Run fills the dataset)",
                    "Dataset.create(path, ...) to start an empty one"],
                where=str(idx),
            )
        try:
            index = jsonio.read(idx)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise PmuError(
                what=f"{INDEX_NAME} could not be read.",
                why=f"{exc.__class__.__name__}: {exc}",
                do=["restore the file from the last good run, or re-create the dataset"],
                where=str(idx),
            ) from exc
        if not isinstance(index, dict):
            raise PmuError(
                what=f"{INDEX_NAME} is not a JSON object.",
                why=f"the top level parsed as {type(index).__name__}, contract 2 wants an object "
                    "with project/config_sha/created/dims/variables/missing",
                do=["restore the file from the last good run, or re-create the dataset"],
                where=str(idx),
            )
        for key, kind in (("project", str), ("config_sha", str), ("dims", dict),
                          ("variables", dict)):
            if not isinstance(index.get(key), kind):
                raise PmuError(
                    what=f"{INDEX_NAME} has no valid {key!r}.",
                    why=f"contract 2 requires {key!r} to be a {kind.__name__}; found "
                        f"{type(index.get(key)).__name__}",
                    do=["restore the file from the last good run, or re-create the dataset"],
                    where=str(idx),
                )
        index.setdefault("created", "")
        index.setdefault("missing", [])
        if not isinstance(index["missing"], list):
            raise PmuError(
                what=f"{INDEX_NAME} has a malformed 'missing' block.",
                why=f"it must be a list of [variable, ...cell values..., reason] rows; found "
                    f"{type(index['missing']).__name__}",
                do=["restore the file from the last good run, or re-create the dataset"],
                where=str(idx),
            )
        index["dims"] = _validate_dims(index["dims"], str(idx))
        for var, rec in index["variables"].items():
            if not isinstance(rec, dict) or "dims" not in rec or "dtype" not in rec:
                raise PmuError(
                    what=f"{INDEX_NAME}: variable {var!r} has no dims/dtype.",
                    why="every variable record carries dims, dtype and file",
                    do=["restore the file from the last good run, or re-create the dataset"],
                    where=str(idx),
                )
            rec.setdefault("file", f"{var}.npy")
            rec.setdefault("unit", "")
        return cls(p, index)

    def close(self) -> None:
        """Flush index.json and drop cached arrays. Idempotent."""
        if self._closed:
            return
        self._write_index()
        self._cache.clear()
        self._coords.clear()
        gc.collect()          # let any memory map go before the directory is reused
        self._closed = True

    def __enter__(self) -> "Dataset":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<Dataset {self._index.get('project')!r} at {self.path} "
                f"vars={len(self._index['variables'])}>")

    # ------------------------------------------------------------------ properties

    @property
    def project(self) -> str:
        return self._index["project"]

    @property
    def config_sha(self) -> str:
        """The config the characterization was planned from. Comparing it is the caller's call."""
        return self._index["config_sha"]

    @property
    def created(self) -> str:
        return self._index.get("created", "")

    # ------------------------------------------------------------------ declaration

    def declare(self, var: str, *, dims: tuple[str, ...], dtype: str, unit: str = "",
                coord: np.ndarray | None = None, coord_name: str = "") -> None:
        """Allocate the full hyper-rectangle for `var`, filled with NaN.

        `dims` is the fixed order (`process, temp_c, vset, [load_a], [sweep coordinate]`); the
        sweep coordinate, when there is one, is the last name and its values are `coord`.
        `coord_name` is the base name of the shared coordinate file (default: `<var>.<dim>`);
        two variables may share one file only if their coordinate arrays are identical.

        Re-declaring with the identical signature is a no-op; a different signature is a PmuError.
        """
        self._check_open()
        var = str(var)
        where = str(self.path / INDEX_NAME)
        _check_var_name(var, where)
        dims = tuple(str(d) for d in dims)
        cell_dims, coord_dim = _split_dims(var, dims, where)

        try:
            dt = np.dtype(dtype)
        except TypeError as exc:
            raise PmuError(
                what=f"{var}: dtype {dtype!r} is not a numpy dtype.",
                why=f"numpy rejected it ({exc})",
                do=["use 'float64' for magnitudes, 'complex128' for AC transfers"],
                where=where,
            ) from exc
        if dt.kind not in "fc":
            raise PmuError(
                what=f"{var}: dtype {dt.name!r} cannot hold NaN.",
                why="NaN is how this store says 'no data'; integer and boolean arrays have no NaN, "
                    "so a never-run cell would read back as a real measurement",
                do=["use 'float64' (or 'complex128' for AC transfers)"],
                where=where,
            )

        shape = [len(self._axis_for(var, d)) for d in cell_dims]
        carr = None
        if coord_dim:
            if coord is None:
                raise PmuError(
                    what=f"{var}: dims end in {coord_dim!r} but no coord was given.",
                    why="the sweep coordinate is per variable, so its values must be stored with it",
                    do=[f"pass coord=<1-D array of {coord_dim} values>"],
                    where=where,
                )
            carr = np.asarray(coord, dtype=float)
            if carr.ndim != 1 or carr.size == 0:
                raise PmuError(
                    what=f"{var}: coord must be a non-empty 1-D array, got shape {carr.shape}.",
                    why="the coordinate is the trailing axis of the variable",
                    do=["pass e.g. np.logspace(1, 10, 181)"],
                    where=where,
                )
            shape.append(int(carr.size))
        elif coord is not None:
            raise PmuError(
                what=f"{var}: a coord was given but dims {list(dims)} end in a cell axis.",
                why="only a variable whose last dim is a sweep coordinate stores one",
                do=[f"append the coordinate name to dims (e.g. dims={list(dims) + ['freq_hz']})",
                    "or drop coord= for a scalar-per-cell variable"],
                where=where,
            )

        cname = str(coord_name) if coord_name else (f"{var}.{coord_dim}" if coord_dim else "")
        rec: dict[str, Any] = {"dims": list(dims), "dtype": dt.name, "unit": str(unit),
                               "file": f"{var}.npy"}
        if coord_dim:
            rec["coord"] = f"{cname}.npy"

        old = self._index["variables"].get(var)
        if old is not None:
            diff = [k for k in ("dims", "dtype", "unit", "file", "coord")
                    if old.get(k, "") != rec.get(k, "")]
            if diff:
                raise PmuError(
                    what=f"{var} is already declared with a different signature ({', '.join(diff)}).",
                    why=f"stored {({k: old.get(k) for k in diff})}, asked for "
                        f"{({k: rec.get(k) for k in diff})}; re-shaping a variable would throw away "
                        "cells that are already characterized",
                    do=["declare it with the stored signature",
                        "or start a new dataset if the plan really changed"],
                    where=where,
                )
            if carr is not None and not np.array_equal(self.coord(var), carr):
                raise PmuError(
                    what=f"{var} is already declared with a different {coord_dim} axis.",
                    why=f"stored {self.coord(var).size} points, asked for {carr.size}; the sweep "
                        "coordinate is part of the variable's shape",
                    do=["pass the stored coordinate", "or start a new dataset"],
                    where=where,
                )
            return                                   # idempotent

        if carr is not None:
            cpath = self.path / rec["coord"]
            if cpath.is_file():
                shared = np.load(cpath, allow_pickle=False)
                if not np.array_equal(shared, carr):
                    raise PmuError(
                        what=f"Coordinate file {rec['coord']} already holds a different axis.",
                        why=f"it has {shared.size} points, {var} asks for {carr.size}; a shared "
                            "coordinate file must be identical for every variable that uses it",
                        do=[f"pass coord_name='{cname}_{var.replace('.', '_')}' to keep them apart",
                            "or pass the coordinate that is already stored"],
                        where=str(cpath),
                    )
            else:
                _save_npy(cpath, carr)
            self._coords[rec["coord"]] = carr

        fill = np.nan + 1j * np.nan if dt.kind == "c" else np.nan
        arr = np.full(tuple(shape), fill, dtype=dt)
        _save_npy(self.path / rec["file"], arr)
        self._cache[var] = arr
        self._index["variables"][var] = rec
        if coord_dim:
            self._index["dims"][coord_dim] = PER_VARIABLE
        self._write_index()

    # ------------------------------------------------------------------ write / read

    def put(self, var: str, cell: dict, data: np.ndarray) -> None:
        """Write one cell: the 1-D sweep, or a scalar for a variable with no sweep coordinate.

        A successful put clears any `mark_missing` registration for that cell -- a retry that
        worked is no longer "ran and broke".
        """
        rec = self._record(var)
        cell_dims, coord_dim = self._dims_of(var)
        idx, _ = self._resolve(var, cell, cell_dims)
        arr = self._writable(var)
        where = str(self.path / rec["file"])
        d = np.asarray(data)
        if d.dtype.kind == "c" and arr.dtype.kind == "f":
            raise PmuError(
                what=f"{var}: complex data written into a {arr.dtype.name} variable.",
                why="the variable was declared real, so the imaginary part would be dropped silently",
                do=[f"declare {var} as complex128", "or write the magnitude/real part explicitly"],
                where=where,
            )
        if coord_dim:
            n = int(arr.shape[-1])
            if d.ndim != 1 or d.size != n:
                raise PmuError(
                    what=f"{var} {cell_key(cell)}: data has {d.size} point(s), the {coord_dim} "
                         f"axis has {n}.",
                    why="one cell is exactly one sweep over the variable's own coordinate",
                    do=[f"pass a 1-D array of {n} values",
                        "check that the simulation swept the planned points"],
                    where=where,
                )
        else:
            if d.size != 1:
                raise PmuError(
                    what=f"{var} {cell_key(cell)}: expected a scalar, got shape {d.shape}.",
                    why=f"{var} has no sweep coordinate -- one cell holds one number",
                    do=["pass a scalar", "or declare the variable with a sweep coordinate"],
                    where=where,
                )
            d = d.reshape(())
        arr[idx] = d
        _save_npy(self.path / rec["file"], arr)
        if self._missing.pop((var, idx), None) is not None:
            self._index["missing"] = self._missing_rows()
        self._write_index()

    def get(self, var: str, cell: dict | None = None) -> np.ndarray:
        """The whole array (cell=None) or one cell's sweep. Read-only view -- copy to modify."""
        self._record(var)
        arr = self._array(var)
        if cell is None:
            return _ro(arr)
        cell_dims, _ = self._dims_of(var)
        idx, _ = self._resolve(var, cell, cell_dims)
        out = arr[idx]
        return _ro(out if isinstance(out, np.ndarray) else np.asarray(out))

    def coord(self, var: str) -> np.ndarray | None:
        """The variable's sweep coordinate, or None when it has no trailing axis."""
        rec = self._record(var)
        name = rec.get("coord")
        if not name:
            return None
        if name not in self._coords:
            p = self.path / name
            if not p.is_file():
                raise PmuError(
                    what=f"{var}: coordinate file {name} is missing.",
                    why="index.json declares it but the .npy is not in the dataset directory",
                    do=["re-run the characterization for this variable",
                        "or restore the dataset directory from the last good run"],
                    where=str(p),
                )
            self._coords[name] = np.load(p, allow_pickle=False)
        return _ro(self._coords[name])

    def has(self, var: str, cell: dict) -> bool:
        """True when the cell holds data: not all-NaN and not registered missing."""
        cell_dims, _ = self._dims_of(var)
        idx, _ = self._resolve(var, cell, cell_dims)
        if (var, idx) in self._missing:
            return False
        sl = np.asarray(self._array(var)[idx])
        return not bool(np.all(np.isnan(sl)))

    # ------------------------------------------------------------------ missing bookkeeping

    def mark_missing(self, var: str, cell: dict, reason: str) -> None:
        """Register 'it ran and it broke' for one cell. The data stays NaN; the reason is kept."""
        cell_dims, _ = self._dims_of(var)
        idx, _ = self._resolve(var, cell, cell_dims)
        reason = str(reason).strip()
        if not reason:
            raise PmuError(
                what=f"{var} {cell_key(cell)}: mark_missing needs a reason.",
                why="the reason is what tells the fitter and the report 'ran and broke' apart from "
                    "'never run'",
                do=["pass e.g. 'run failed: spectre exited 1 (see logs/<run_id>.log)'"],
                where=str(self.path / INDEX_NAME),
            )
        self._missing[(var, idx)] = reason
        self._index["missing"] = self._missing_rows()
        self._write_index()

    def missing(self, var: str | None = None) -> list[list]:
        """Contract-2 rows: [var, process, temp_c, vset, (load_a,) reason]."""
        self._check_open()
        rows = self._missing_rows()
        return [list(r) for r in rows if var is None or r[0] == var]

    def coverage(self, var: str) -> dict:
        """{'declared', 'filled', 'missing', 'never_run'} over the cells of one variable.

        The three states partition the declared cells.  A cell registered missing counts as
        missing even if it also holds data (`mark_missing` after a bad run wins); a later `put`
        clears the registration and the cell counts as filled again.
        """
        cell_dims, coord_dim = self._dims_of(var)
        arr = self._array(var)
        ncell = int(np.prod(arr.shape[:len(cell_dims)])) if cell_dims else 1
        nan = np.isnan(arr)
        filled = np.asarray(~np.all(nan, axis=-1) if coord_dim else ~nan)
        miss = np.zeros(arr.shape[:len(cell_dims)], dtype=bool)
        for (v, idx) in self._missing:
            if v == var:
                miss[idx] = True
        n_miss = int(np.count_nonzero(miss))
        n_filled = int(np.count_nonzero(filled & ~miss))
        return {"declared": ncell, "filled": n_filled, "missing": n_miss,
                "never_run": ncell - n_filled - n_miss}

    # ------------------------------------------------------------------ whole dataset

    def variables(self) -> list[str]:
        self._check_open()
        return sorted(self._index["variables"])

    def dims(self) -> dict:
        self._check_open()
        return copy.deepcopy(self._index["dims"])

    def axis(self, name: str, port: str | None = None) -> list:
        """One axis. `load_a` needs the port, because every rail has its own load grid."""
        self._check_open()
        where = str(self.path / INDEX_NAME)
        if name == "load_a":
            grids = self._index["dims"].get("load_a")
            if not isinstance(grids, dict):
                raise PmuError(
                    what="This dataset declares no load_a axis.",
                    why="dims['load_a'] is absent, so no variable can depend on load",
                    do=["re-create the dataset with load_a={'<port>': [...]} if loads matter"],
                    where=where,
                )
            if port is None:
                raise PmuError(
                    what="axis('load_a') needs a port.",
                    why="load grids are per port -- rails carry different currents",
                    do=[f"pass port=<one of: {', '.join(sorted(grids))}>"],
                    where=where,
                )
            if port not in grids:
                raise PmuError(
                    what=f"No load grid for port {port!r}.",
                    why="dims['load_a'] only carries the ports the plan characterizes under load",
                    do=[f"use one of: {', '.join(sorted(grids))}"],
                    where=where,
                )
            return list(grids[port])
        ax = self._index["dims"].get(name)
        if not isinstance(ax, list):
            raise PmuError(
                what=f"This dataset has no {name!r} axis.",
                why=f"dims[{name!r}] is {ax!r}; per-variable coordinates are read with coord(var)",
                do=[f"use one of: {', '.join(k for k, v in self._index['dims'].items() if isinstance(v, list))}",
                    "or coord(var) for a sweep coordinate"],
                where=where,
            )
        return list(ax)

    def sha(self) -> str:
        """12-hex dataset_sha over index.json plus every referenced .npy -- order-independent."""
        self._check_open()
        files: dict[str, str] = {}
        for rec in self._index["variables"].values():
            for key in ("file", "coord"):
                name = rec.get(key)
                if not name:
                    continue
                p = self.path / name
                if name not in files and p.is_file():
                    files[name] = jsonio.sha_file(p)
        return jsonio.sha({"index": jsonio.canon(self._index), "files": files}, 12)

    def summary(self) -> dict:
        """Per-variable coverage + provenance, for the Model screen and the digest. JSON-safe."""
        self._check_open()
        variables = {}
        totals = {"declared": 0, "filled": 0, "missing": 0, "never_run": 0}
        for var in self.variables():
            rec = self._index["variables"][var]
            cov = self.coverage(var)
            for k in totals:
                totals[k] += cov[k]
            variables[var] = {
                "dims": list(rec["dims"]),
                "dtype": rec["dtype"],
                "unit": rec.get("unit", ""),
                "shape": [int(n) for n in self._array(var).shape],
                "coverage": cov,
            }
        return {
            "project": self.project,
            "config_sha": self.config_sha,
            "created": self.created,
            "dataset_sha": self.sha(),
            "dims": self.dims(),
            "variables": variables,
            "totals": totals,
            "missing": self.missing(),
        }

    # ------------------------------------------------------------------ internals

    def _check_open(self) -> None:
        if self._closed:
            raise PmuError(
                what="This dataset is closed.",
                why="close() dropped its cached arrays; the handle is not reusable",
                do=[f"re-open it: Dataset.open({str(self.path)!r})"],
                where=str(self.path),
            )

    def _record(self, var: str) -> dict:
        self._check_open()
        rec = self._index["variables"].get(var)
        if rec is None:
            known = ", ".join(self.variables()) or "(none declared yet)"
            raise PmuError(
                what=f"This dataset has no variable {var!r}.",
                why="only declared variables have storage; declare() allocates the hyper-rectangle",
                do=[f"declare it first: declare({var!r}, dims=(...), dtype='float64')",
                    f"declared so far: {known}"],
                where=str(self.path / INDEX_NAME),
            )
        return rec

    def _dims_of(self, var: str) -> tuple[tuple[str, ...], str]:
        rec = self._record(var)
        return _split_dims(var, tuple(rec["dims"]), str(self.path / INDEX_NAME))

    def _axis_for(self, var: str, dim: str) -> list:
        if dim == "load_a":
            port = var.split(".", 1)[1] if "." in var else ""
            return self.axis("load_a", port)
        return self.axis(dim)

    def _resolve(self, var: str, cell: dict, cell_dims: tuple[str, ...]
                 ) -> tuple[tuple[int, ...], dict]:
        """(index tuple, cell with canonical axis values). Names the offending key on any mismatch."""
        where = str(self.path / INDEX_NAME)
        if not isinstance(cell, dict):
            raise PmuError(
                what=f"{var}: cell must be a dict, got {type(cell).__name__}.",
                why="a cell names one value per axis of the variable",
                do=[f"pass {{{', '.join(repr(d) + ': ...' for d in cell_dims)}}}"],
                where=where,
            )
        extra = [k for k in cell if k not in cell_dims]
        absent = [d for d in cell_dims if d not in cell]
        if extra or absent:
            parts = []
            if absent:
                parts.append("missing " + ", ".join(repr(k) for k in absent))
            if extra:
                parts.append("unexpected " + ", ".join(repr(k) for k in extra))
            raise PmuError(
                what=f"{var}: cell keys do not match its dims ({'; '.join(parts)}).",
                why=f"{var} is stored over exactly {', '.join(cell_dims) or '(no cell axes)'}",
                do=[f"pass a cell with keys: {', '.join(cell_dims) or '(none)'}"],
                where=where,
            )
        idx, canon = [], {}
        for d in cell_dims:
            ax = self._axis_for(var, d)
            i = _axis_index(ax, cell[d], dim=d, var=var, where=where)
            idx.append(i)
            canon[d] = ax[i]
        return tuple(idx), canon

    def _array(self, var: str) -> np.ndarray:
        if var in self._cache:
            return self._cache[var]
        rec = self._record(var)
        p = self.path / rec["file"]
        if not p.is_file():
            raise PmuError(
                what=f"{var}: data file {rec['file']} is missing.",
                why="index.json declares the variable but its .npy is not in the dataset directory",
                do=["re-declare the variable and re-run its cells",
                    "or restore the dataset directory from the last good run"],
                where=str(p),
            )
        mmap = "r" if p.stat().st_size > MMAP_BYTES else None
        arr = np.load(p, mmap_mode=mmap, allow_pickle=False)
        self._cache[var] = arr
        return arr

    def _writable(self, var: str) -> np.ndarray:
        """The cached array, copied out of its memory map if need be (contract: copy on write)."""
        arr = self._array(var)
        if isinstance(arr, np.memmap) or not arr.flags.writeable:
            arr = np.array(arr)
            self._cache[var] = arr
            gc.collect()      # release the old map so os.replace can swap the file on Windows
        return arr

    def _missing_rows(self) -> list[list]:
        rows = []
        for (var, idx), reason in self._missing.items():
            cell_dims, _ = self._dims_of(var)
            row = [var]
            for d, i in zip(cell_dims, idx):
                row.append(_py(self._axis_for(var, d)[i]))
            row.append(reason)
            rows.append(row)
        rows.sort(key=jsonio.canon)
        return rows

    def _rebuild_missing(self) -> None:
        where = str(self.path / INDEX_NAME)
        out: dict[tuple[str, tuple[int, ...]], str] = {}
        for row in self._index.get("missing") or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise PmuError(
                    what=f"{INDEX_NAME}: malformed 'missing' row {row!r}.",
                    why="a row is [variable, ...one value per cell axis..., reason]",
                    do=["restore the file from the last good run, or re-create the dataset"],
                    where=where,
                )
            var, reason, values = str(row[0]), str(row[-1]), list(row[1:-1])
            cell_dims, _ = self._dims_of(var)
            if len(values) != len(cell_dims):
                raise PmuError(
                    what=f"{INDEX_NAME}: 'missing' row for {var} has {len(values)} cell value(s), "
                         f"the variable has {len(cell_dims)} cell axis/axes.",
                    why="the row must name one value per cell axis, in dim order",
                    do=[f"a row for {var} looks like "
                        f"[{var!r}, {', '.join('<' + d + '>' for d in cell_dims)}, '<reason>']"],
                    where=where,
                )
            idx, _canon = self._resolve(var, dict(zip(cell_dims, values)), cell_dims)
            out[(var, idx)] = reason
        self._missing = out

    def _write_index(self) -> None:
        text = json.dumps(self._index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _atomic_text(self.path / INDEX_NAME, text)
