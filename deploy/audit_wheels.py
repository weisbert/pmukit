#!/usr/bin/env python3
# from LDO_modeling/deploy/audit_wheels.py @ d2c5b80
"""glibc wheel-tag auditor -- the air-gap install's #1 safety gate.

The box is RHEL8-class but the wheel baseline we ship against is **glibc 2.17**
(manylinux2014 / manylinux_2_17), because that is the oldest libc in the fleet and a
2.17 wheel runs everywhere newer.  On a Windows desk `pip download` will happily fetch
`manylinux_2_28` / `_2_31` / `_2_34` wheels, which then die on the box with

    ImportError: /lib64/libc.so.6: version `GLIBC_2.28' not found

This script inspects every wheel in a directory and REJECTS:

  * any wheel whose minimum glibc across its platform tags is newer than the target;
  * any wheel built for the wrong CPython / ABI (cp310, cp312, ...);
  * any `none-any` wheel that secretly carries a compiled extension (.so/.pyd/.dll);
  * musllinux and bare `linux_*` wheels (wrong libc / not portable).

    python deploy/audit_wheels.py wheels/                 # table + exit 1 on any reject
    python deploy/audit_wheels.py wheels/ --explain       # also print the tag each wheel matched
    python deploy/audit_wheels.py wheels/ --deep          # ALSO scan the wheel's .so bytes for
                                                          # GLIBC_2.NN version references
`--deep` is the auditwheel-free static check used by dryrun_manylinux2014.sh when Docker is
not available on the desk: ELF version references live as literal strings in `.dynstr`, so a
byte scan for `GLIBC_2.<n>` recovers the real requirement without any Linux tooling.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from pmukit.errors import PmuError  # noqa: E402

# named manylinux profiles -> (glibc_major, glibc_minor)
NAMED = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_MX = re.compile(r"^manylinux_(\d+)_(\d+)_(.+)$")                 # manylinux_2_28_x86_64
_MX_NAMED = re.compile(r"^(manylinux1|manylinux2010|manylinux2014)_(.+)$")
_MX_TYPO = re.compile(r"^manylinux(\d+)_(\d+)_(.+)$")             # manylinux2_28_x86_64 (seen in the wild)
_MUSL = re.compile(r"^musllinux_(\d+)_(\d+)_(.+)$")
_GLIBC_REF = re.compile(rb"GLIBC_(\d+)\.(\d+)")
_EXT_SUFFIX = (".so", ".pyd", ".dll", ".dylib")
INF = (999, 999)

#: python/abi tags that a cp311 target can install
OK_PY_TAGS = {"cp311", "py3", "py311", "py2.py3"}
OK_ABI_TAGS = {"cp311", "abi3", "none"}


def _platform_glibc(tag: str, arch: str):
    """One platform sub-tag -> ((glibc_major, glibc_minor), ok_arch).

    Returns the glibc needed to run this sub-tag, or INF if it cannot run on a glibc target.
    """
    if tag == "any":
        return (0, 0), True                              # pure python
    m = _MX.match(tag)
    if m:
        return (int(m.group(1)), int(m.group(2))), (m.group(3) == arch)
    m = _MX_NAMED.match(tag)
    if m:
        return NAMED[m.group(1)], (m.group(2) == arch)
    m = _MX_TYPO.match(tag)                              # manylinux2_28_x86_64 -> glibc 2.28
    if m:
        return (int(m.group(1)), int(m.group(2))), (m.group(3) == arch)
    if _MUSL.match(tag):
        return INF, False                                # musl libc, not glibc
    if tag.startswith("linux_"):
        return INF, False                                # bare linux: built locally, not portable
    return INF, False                                    # win_*, macosx_*, unknown


def _split_wheel(path) -> dict:
    """{name, version, py, abi, plat} from a wheel FILENAME (PEP 427)."""
    stem = pathlib.Path(path).name
    stem = stem[:-4] if stem.endswith(".whl") else stem
    parts = stem.split("-")
    if len(parts) < 5:
        return dict(name=stem, version="", py="", abi="", plat="")
    return dict(name=parts[0], version=parts[1], py=parts[-3], abi=parts[-2], plat=parts[-1])


def _has_compiled_extension(path) -> list:
    """Members of the wheel that are compiled extensions (a 'none-any' wheel must have none)."""
    try:
        with zipfile.ZipFile(path) as z:
            return [n for n in z.namelist()
                    if not n.endswith("/") and n.lower().endswith(_EXT_SUFFIX)]
    except (OSError, zipfile.BadZipFile):
        return []


def deep_glibc(path):
    """Max GLIBC_x.y referenced by any ELF object inside the wheel, or None if there is none.

    Pure-stdlib stand-in for auditwheel: glibc symbol-version references are literal strings
    in the object's `.dynstr`, so scanning the member bytes finds them.
    """
    worst = None
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                low = n.lower()
                if not (low.endswith(".so") or ".so." in low):
                    continue
                blob = z.read(n)
                if not blob.startswith(b"\x7fELF"):
                    continue
                for mj, mn in _GLIBC_REF.findall(blob):
                    v = (int(mj), int(mn))
                    worst = v if worst is None else max(worst, v)
    except (OSError, zipfile.BadZipFile):
        return None
    return worst


def audit_wheel(path, arch: str = "x86_64", deep: bool = False) -> dict:
    """Audit ONE wheel. Returns a row dict; `min_glibc=None` means no usable glibc tag."""
    info = _split_wheel(path)
    subtags = info["plat"].split(".") if info["plat"] else []
    best, matched = None, ""
    for t in subtags:
        g, ok_arch = _platform_glibc(t, arch)
        if g == (0, 0):                                  # pure python 'any'
            best, matched = (0, 0), t
            break
        if ok_arch and g != INF and (best is None or g < best):
            best, matched = g, t
    row = dict(name=pathlib.Path(path).name, tags=subtags, min_glibc=best, matched_tag=matched,
               py=info["py"], abi=info["abi"], deep_glibc=None, stowaways=[])
    if best == (0, 0):
        row["stowaways"] = _has_compiled_extension(path)
    if deep:
        row["deep_glibc"] = deep_glibc(path)
    return row


def _verdict(row, max_glibc, arch):
    """-> (ok: bool, verdict: str). Order matters: report the FIRST disqualifying reason."""
    py, abi = row["py"], row["abi"]
    if py and py not in OK_PY_TAGS:
        return False, f"REJECT (python tag {py!r}, need one of {sorted(OK_PY_TAGS)})"
    if abi and abi not in OK_ABI_TAGS:
        return False, f"REJECT (abi tag {abi!r}, need one of {sorted(OK_ABI_TAGS)})"
    g = row["min_glibc"]
    if g is None:
        return False, f"REJECT (no glibc/{arch} platform tag: {'.'.join(row['tags']) or '?'})"
    if g > max_glibc:
        return False, (f"REJECT (needs glibc {g[0]}.{g[1]} > {max_glibc[0]}.{max_glibc[1]}"
                       f" -- tag {row['matched_tag']})")
    if g == (0, 0) and row["stowaways"]:
        return False, ("REJECT (none-any wheel carries a compiled extension: "
                       + ", ".join(row["stowaways"][:3]) + ")")
    d = row["deep_glibc"]
    if d is not None and d > max_glibc:
        return False, (f"REJECT (ELF inside needs GLIBC_{d[0]}.{d[1]} > "
                       f"{max_glibc[0]}.{max_glibc[1]} -- tag lied)")
    if g == (0, 0):
        return True, "PASS (pure-python)"
    extra = f", ELF max GLIBC_{d[0]}.{d[1]}" if d is not None else ""
    return True, f"PASS (glibc {g[0]}.{g[1]}{extra})"


def audit_dir(wheels_dir, max_glibc=(2, 17), arch: str = "x86_64", deep: bool = False):
    """-> (rows, violations). Each row carries name/tags/verdict/ok."""
    d = pathlib.Path(wheels_dir)
    rows, violations = [], []
    for w in sorted(d.glob("*.whl")):
        r = audit_wheel(w, arch=arch, deep=deep)
        ok, verdict = _verdict(r, max_glibc, arch)
        r["ok"], r["verdict"] = ok, verdict
        rows.append(r)
        if not ok:
            violations.append(r)
    return rows, violations


def print_table(rows, violations, max_glibc, arch, explain=False, out=None):
    out = out or sys.stdout
    if not rows:
        return
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        print(f"  {r['name']:<{width}}  {r['verdict']}", file=out)
        if explain and r["ok"]:
            print(f"  {'':<{width}}    matched tag: {r['matched_tag'] or 'any'}"
                  f"   (py={r['py'] or '?'} abi={r['abi'] or '?'})", file=out)
    n, bad = len(rows), len(violations)
    print(f"\n  {n - bad}/{n} PASS   target glibc {max_glibc[0]}.{max_glibc[1]} / {arch}", file=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit wheels for glibc-2.17 (manylinux2014) compat")
    ap.add_argument("wheels_dir")
    ap.add_argument("--max-glibc", default="2.17")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--explain", action="store_true",
                    help="print the platform tag each accepted wheel matched")
    ap.add_argument("--deep", action="store_true",
                    help="also scan the wheel's ELF members for GLIBC_x.y references")
    a = ap.parse_args(argv)

    d = pathlib.Path(a.wheels_dir)
    if not d.is_dir():
        raise PmuError(
            what=f"Wheel directory {a.wheels_dir!r} does not exist.",
            why="The auditor walks a directory of .whl files; the path given is not a directory.",
            do=["Build a package first: python deploy/package.py --out dist/pkg",
                "Then audit its wheels: python deploy/audit_wheels.py dist/pkg/wheels"],
            where=str(d))
    mj, mn = (int(x) for x in a.max_glibc.split("."))
    rows, viol = audit_dir(d, max_glibc=(mj, mn), arch=a.arch, deep=a.deep)
    if not rows:
        raise PmuError(
            what=f"No wheels found in {d}.",
            why="The directory exists but contains no *.whl file, so there is nothing to audit.",
            do=["Run the packager without --dry-run so it downloads wheels, or point --wheel-cache"
                " at a directory that already has them."],
            where=str(d))
    print_table(rows, viol, (mj, mn), a.arch, explain=a.explain)
    if viol:
        print("\nAUDIT FAIL -> downpin these (last version with a manylinux_2_17 / manylinux2014"
              " wheel) in requirements.txt and re-run the packager:")
        for r in viol:
            print(f"   - {r['name']}   {r['verdict']}")
        return 1
    print("AUDIT PASS -> every wheel installs on the box.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PmuError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
