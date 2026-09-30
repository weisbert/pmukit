"""The output-code check: does "other codes change the DC output only" hold for THIS rail?

The LDO output code is fixed on the chip.  A designer moves it in simulation only for the V of
PVT -- the whole rail up or down, to see what the block downstream does -- so pmukit measures the
small-signal blocks (Zout, PSRR, noise) at the NOMINAL code only and the other codes at DC only
(`spec._RAIL_AC`).  Across a code range the feedback ratio moves about 20 %, which moves Zout and
PSRR about 2 dB; the one exception is nonlinear -- at the highest codes the pass device runs out
of headroom, the loop loses gain, and Zout / PSRR collapse.  That is not assumed away; it is
measured here, and REPORTED, never fitted:

  * the plan's `code_check` group re-runs each rail's Zout injection and the supply injection at
    the lowest and the highest code, per corner, at the coldest and the hottest temperature, at
    the nominal load state (`plan._code_check`);
  * the runner stores those in their own dataset (`importer.CODE_CHECK_DIR`, per code), which the
    fitter never opens;
  * this module compares MEASURED against MEASURED: the extreme code's curve against the nominal
    code's own curve at the same corner, temperature and load, over the care band, per rail --
    the largest |difference| in dB for |Zout| and |PSRR| and the frequency where it sits.

Grade (thresholds `grades.CODE_CHECK_OK_DB` / `CODE_CHECK_MARGINAL_DB`, next to the block limits):
ok <= 2 dB, marginal <= 6 dB, bad above.  It lands in report.md ("Output code: other codes"), in
grades.json (`code_check`) and as one line on the Model screen.  Delivery is never blocked by it.
"""
from __future__ import annotations

import math
import pathlib

import numpy as np

from .. import paths
from ..errors import PmuError
from .grades import CODE_CHECK_GRADES, CODE_CHECK_MARGINAL_DB, CODE_CHECK_OK_DB

__all__ = ["compare", "for_project", "grade_db", "summary_line", "CHECKED"]

#: (dataset observable, what the report calls it)
CHECKED = (("ac_zout", "Zout"), ("ac_psrr", "PSRR"))

_ADVICE = {
    "bad": "headroom? characterize that code as nominal if the design uses it",
    "marginal": "more than the feedback ratio explains; look at the headroom at that code",
}


def grade_db(db: float) -> str:
    """ok / marginal / bad for one largest |difference| in dB."""
    if db <= CODE_CHECK_OK_DB:
        return "ok"
    if db <= CODE_CHECK_MARGINAL_DB:
        return "marginal"
    return "bad"


def _worst(grades) -> str:
    rank = {g: i for i, g in enumerate(CODE_CHECK_GRADES)}
    have = [g for g in grades if g in rank]
    return max(have, key=rank.get) if have else ""


def _curve(ds, var: str, corner, temp, code, load):
    """(freq, values) of one stored cell, or None when it was never filled.

    The cell is built from the variable's OWN declared dims: the main dataset keeps Zout / PSRR
    without a vset axis (an older one may still carry it -- then the nominal code is named)."""
    if ds is None or var not in ds.variables():
        return None
    want = {"process": corner, "temp_c": temp, "vset": code, "load_a": load}
    cell = {}
    for d in ds.var_dims(var):
        if d not in want:
            continue                                    # the trailing sweep coordinate
        if want[d] is None:
            return None
        cell[d] = want[d]
    try:
        if not ds.has(var, cell):
            return None
        return (np.asarray(ds.coord(var), dtype=float), np.asarray(ds.get(var, cell)))
    except PmuError:                                    # a value that is not on an axis
        return None


def _db(v) -> np.ndarray:
    a = np.abs(np.asarray(v))
    with np.errstate(divide="ignore", invalid="ignore"):
        return 20.0 * np.log10(np.where(a > 0, a, np.nan))


def _delta(ref, chk, care_hz: float):
    """(largest |dB difference|, its frequency, signed difference) over f <= care_hz, or None."""
    f, r = ref
    fc, c = chk
    mr, mc = _db(r), _db(c)
    if fc.size != f.size or not np.allclose(fc, f, rtol=1e-6, atol=0.0):
        ok = np.isfinite(mc) & (fc > 0)
        if np.count_nonzero(ok) < 2:
            return None
        mc = np.interp(np.log(f), np.log(fc[ok]), mc[ok], left=np.nan, right=np.nan)
    band = (f <= care_hz * (1.0 + 1e-9)) & np.isfinite(mr) & np.isfinite(mc)
    if not band.any():
        return None
    d = np.where(band, mc - mr, 0.0)
    i = int(np.argmax(np.abs(d)))
    return float(abs(d[i])), float(f[i]), float(d[i])


def _where(port, code, corner, temp) -> str:
    return f"{port} at code {code}, {corner} {temp:g}C"


def compare(main_ds, check_ds, derived) -> dict:
    """The check, from the two datasets.  Pure: nothing is written, no simulator is touched.

    Returns ``{"nominal", "codes", "temps_c", "care_up_to_hz", "rows", "grade", "summary"}``.
    One row per (rail, corner, temperature, code): `zout_db` / `zout_hz` / `zout_sign`, the same
    for `psrr` (None where a curve is absent), its `grade` -- ok / marginal / bad from what could
    be compared, "not_run" when nothing could -- and
    one `text` sentence.  `codes` is empty when only one code is configured -- there is nothing
    to check and the report says nothing about it."""
    from ..plan import check_codes, check_temps, load_states, nominal_state
    v = derived.vset or {}
    all_codes = [int(c) for c in (v.get("codes") or [])]
    nominal = all_codes[0] if all_codes else None
    codes = check_codes(derived)
    out = {"nominal": nominal, "codes": codes, "all_codes": all_codes,
           "temps_c": check_temps(derived) if codes else [],
           "care_up_to_hz": float((derived.freq or {}).get("stop_hz", 0.0) or 0.0),
           "limits_db": {"ok": CODE_CHECK_OK_DB, "marginal": CODE_CHECK_MARGINAL_DB},
           "rows": [], "grade": "", "summary": ""}
    if not codes:
        return out
    care = out["care_up_to_hz"] or math.inf
    states = load_states(derived)
    nom = nominal_state(states, derived)
    corners = list((derived.process or {}).get("corners") or []) or ["tt"]
    for port in sorted(derived.rails or {}):
        load = nom.of(port)
        for corner in corners:
            for temp in out["temps_c"]:
                for code in codes:
                    row = {"port": port, "corner": corner, "temp_c": temp, "code": code}
                    got = []
                    for obs, label in CHECKED:
                        key = label.lower()
                        var = f"{obs}.{port}"
                        ref = _curve(main_ds, var, corner, temp, nominal, load)
                        chk = _curve(check_ds, var, corner, temp, code, load)
                        dl = _delta(ref, chk, care) if ref is not None and chk is not None else None
                        if dl is None:
                            row[f"{key}_db"] = row[f"{key}_hz"] = row[f"{key}_sign"] = None
                            continue
                        row[f"{key}_db"], row[f"{key}_hz"] = round(dl[0], 3), dl[1]
                        row[f"{key}_sign"] = 1 if dl[2] >= 0 else -1
                        got.append((dl[0], label, dl[1], dl[2]))
                    row["grade"], row["text"] = _row_verdict(row, got)
                    out["rows"].append(row)
    graded = [r["grade"] for r in out["rows"] if r["grade"] != "not_run"]
    out["grade"] = _worst(graded) or ("not_run" if out["rows"] else "")
    out["summary"] = summary_line(out)
    return out


def _row_verdict(row: dict, got: list) -> tuple[str, str]:
    from ..deliverable import eng
    where = _where(row["port"], row["code"], row["corner"], row["temp_c"])
    missing = [label for _obs, label in CHECKED if row.get(f"{label.lower()}_db") is None]
    if not got:
        return "not_run", (f"{where}: not checked -- no measurement at this code (the output-code "
                           "check runs have not produced results)")
    db, label, f_hz, signed = max(got)
    grade = grade_db(db)
    if grade == "ok":
        text = (f"{where}: within {CODE_CHECK_OK_DB:g} dB ("
                + ", ".join(f"{lb} {d:.1f} dB" for d, lb, _f, _s in sorted(got, key=lambda g: g[1]))
                + ")")
    else:
        # two significant figures: a sweep point reads 1.995 MHz, a person says 2 MHz
        text = (f"{where}: {label} {db:.1f} dB {'worse' if signed > 0 else 'better'} at "
                f"{eng(float(f'{f_hz:.2g}'), 'Hz')} -- {_ADVICE[grade]}")
    if missing:
        text += f" ({', '.join(missing)} not checked: no measurement)"
    return grade, text


def summary_line(result: dict) -> str:
    """One sentence for the Model screen: the verdict and the worst row, or why there is none."""
    codes = result.get("codes") or []
    if not codes:
        return ""
    nominal = result.get("nominal")
    rows = result.get("rows") or []
    others = [c for c in (result.get("all_codes") or []) if c != nominal]
    head = (f"small-signal from code {nominal}; code{'s' if len(others) > 1 else ''} "
            f"{', '.join(str(c) for c in others)} change{'' if len(others) > 1 else 's'} "
            "the DC output only")
    graded = [r for r in rows if r.get("grade") in CODE_CHECK_GRADES]
    if not graded:
        return head + " -- the Zout/PSRR check at codes " + ", ".join(map(str, codes)) + \
            " has not run yet"
    worst = max(graded, key=lambda r: (CODE_CHECK_GRADES.index(r["grade"]),
                                       max(r.get("zout_db") or 0.0, r.get("psrr_db") or 0.0)))
    if worst["grade"] == "ok":
        top = max(max(r.get("zout_db") or 0.0, r.get("psrr_db") or 0.0) for r in graded)
        return (head + f" -- check at codes {', '.join(map(str, codes))}: ok "
                f"(largest Zout/PSRR difference {top:.1f} dB)")
    return head + f" -- check {worst['grade']}: {worst['text']}"


def for_project(project: str, *, root=None, derived=None) -> dict:
    """The check for one project, read from `<project>/dataset` and `<project>/codecheck`.

    `root` is the data root (`$PMUKIT_DATA`), as `emit.deliver` takes it.  A missing dataset is not
    an error: every row then says "not checked"."""
    from ..config import DerivedConfig
    from ..dataset import Dataset
    from ..importer import CODE_CHECK_DIR
    d = (pathlib.Path(root) if root is not None else paths.data_root()) / project
    if derived is None:
        derived = DerivedConfig.load(d / "derived.json")
    elif not isinstance(derived, DerivedConfig):
        derived = DerivedConfig.from_dict(derived)
    # Opened read-only and never close()d: close() rewrites index.json, and a reader must not
    # touch the file whose time says whether fit.json is older than the data.
    opened = []
    for sub in ("dataset", CODE_CHECK_DIR):
        try:
            opened.append(Dataset.open(d / sub) if (d / sub / "index.json").is_file() else None)
        except PmuError:
            opened.append(None)
    return compare(opened[0], opened[1], derived)
