#!/usr/bin/env python3
# from LDO_modeling/deploy/package.py @ d2c5b80
"""Build the OFFLINE air-gap package for the box (RHEL8 / tcsh / no network).

Run on the Windows desk (which has network); carry the result to the box and run `bash apply`.
The package is a plain directory tree -- no installer magic, nothing to unpack blind:

    <out>/
      apply                 <- the one command the user runs on the box
      update.sh             <- incremental refresh of an existing install
      VERSION               <- one line: 0.1.0+g<sha>
      MANIFEST.json         <- POSIX keys -> {sha256, size}; mode/wheels/deleted/req hash
      SHA256SUMS            <- LF; `sha256sum -c SHA256SUMS` from this directory
      requirements.lock     <- exact pins resolved from the downloaded wheels (full only)
      wheels/*.whl          <- cp311 / x86_64 / manylinux2014 (glibc 2.17)   (full only)
      app/                  <- the tool source (pmukit/, tools/, deploy/, web/, metadata)

Modes
    --full                          source + wheels (first install, or when deps moved)
    --incremental <prev-package>    only the files whose sha256 changed, plus a delete list
    --dry-run                       do EVERYTHING except the network download (wheels come
                                    from the local cache) -- so the acceptance runs offline

    python deploy/package.py --out dist/pkg
    python deploy/package.py --out dist/pkg --dry-run
    python deploy/package.py --out dist/pkg_i --incremental dist/pkg
    python deploy/package.py --out dist/pkg --tar          # also emit <out>.tar.gz + .sha256

Never shipped: .venv, data/, work*/, runs/, deliver/, dist/, tests/ (fixtures may hold real
measurements), .pmukit-denylist (customer identifiers), __pycache__, and anything git ignores.
Runtime deps are stdlib + numpy + scipy ONLY -- there is no Qt in this pipeline.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from pmukit.errors import PmuError  # noqa: E402
import audit_wheels                 # noqa: E402

# --- the red target -------------------------------------------------------------------------
PY_TAG, ABI, ARCH = "311", "cp311", "x86_64"
TARGET_GLIBC = (2, 17)
PLATFORMS = ["manylinux2014_x86_64", "manylinux_2_17_x86_64"]

# --- what crosses the air gap ---------------------------------------------------------------
APP_DIRS = ["pmukit", "tools", "deploy", "web"]          # 'web' only if it exists
APP_FILES = ["README.md", "requirements.txt", "pyproject.toml"]
#: excluded if ANY path segment matches (shutil.ignore_patterns semantics)
SKIP_PARTS = ("__pycache__", "*.pyc", "*.pyo", ".git", ".venv", "dist", "build",
              "data", "runs", "deliver", "work", "work_*", "tests", ".pytest_cache",
              ".pmukit-denylist", "*.npz", "*.psf", "*.psfbin", "*.raw", "*.log")
#: the package root files an installer needs next to app/
INSTALLERS = ["apply", "update.sh"]


# ============================================================================ hashing / git ==
def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_text(path: pathlib.Path, text: str) -> None:
    """LF, always. A CRLF in `apply` gives `set -euo pipefail\\r` -> 'invalid option name',
    and a CRLF anywhere makes `sha256sum -c` fail on the box."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True).stdout


def _git_sha(root=ROOT) -> str:
    try:
        sha = _git(root, "rev-parse", "--short", "HEAD").strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + "-dirty" if _git_dirty(root) else sha


def _git_dirty(root=ROOT) -> bool:
    try:
        return _git(root, "status", "--porcelain").strip() != ""
    except (OSError, subprocess.CalledProcessError):
        return False


def _git_ignored(root, rels):
    """Subset of `rels` (POSIX, repo-relative) that git ignores. Empty set if git is absent."""
    if not rels:
        return set()
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin"],
                           input="\n".join(rels), capture_output=True, text=True)
    except OSError:
        return set()
    if r.returncode not in (0, 1):                       # 1 = "nothing ignored", not an error
        return set()
    return {ln.strip().replace("\\", "/") for ln in r.stdout.splitlines() if ln.strip()}


def _version(root=ROOT) -> str:
    """`0.1.0` from pyproject, or '0.0.0' if it cannot be read (never fail the build on this)."""
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


# ================================================================================= staging ===
def _skipped(rel: str) -> bool:
    parts = pathlib.PurePosixPath(rel).parts
    return any(fnmatch.fnmatch(part, pat) for part in parts for pat in SKIP_PARTS)


def source_files(root=ROOT):
    """Repo-relative POSIX paths of everything that belongs in app/.

    Filesystem walk (NOT `git ls-files`): the deploy pipeline must be packageable before it is
    committed.  Hygiene comes from SKIP_PARTS plus a `git check-ignore` pass, so data/, runs/,
    work*/, .venv and .pmukit-denylist can never ride along even if someone adds a new one.
    """
    root = pathlib.Path(root)
    found = []
    for d in APP_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if not _skipped(rel):
                found.append(rel)
    for f in APP_FILES:
        if (root / f).is_file() and not _skipped(f):
            found.append(f)
    ignored = _git_ignored(root, found)
    return sorted(set(found) - ignored)


def stage_app(out: pathlib.Path, root=ROOT, only=None):
    """Copy the source into <out>/app. `only` limits the copy to those repo-relative paths."""
    root = pathlib.Path(root)
    app = out / "app"
    app.mkdir(parents=True, exist_ok=True)
    rels = source_files(root) if only is None else list(only)
    if not rels and only is None:       # an incremental with nothing changed is legal; an empty
                                        # FULL package is not
        raise PmuError(
            what="Nothing to package: no source files were found.",
            why=f"None of {APP_DIRS + APP_FILES} exist under {root}, or everything was excluded.",
            do=[f"Run the packager from a pmukit checkout (expected {root / 'pmukit'} to exist)."],
            where=str(root))
    for rel in rels:
        src, dst = root / rel, app / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return app, rels


def stage_summary(out: pathlib.Path, warn_mb=10):
    """Size tripwire on app/ ONLY -- wheels are legitimately ~55 MB, source is not."""
    app = out / "app"
    files = [p for p in app.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    n_all = sum(1 for p in out.rglob("*") if p.is_file())
    all_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    print(f"      app/: {len(files)} files, {total / 1e6:.2f} MB"
          f"   |   package: {n_all} files, {all_mb:.1f} MB")
    for p in sorted(files, key=lambda q: q.stat().st_size, reverse=True)[:5]:
        print(f"        {p.stat().st_size / 1e6:7.3f} MB  {p.relative_to(app).as_posix()}")
    if total > warn_mb * 1e6:
        print(f"*** WARN: staged app/ is {total / 1e6:.1f} MB (> {warn_mb} MB). pmukit source is"
              " well under 5 MB; a big tree usually means raw waveforms or a data dir leaked in."
              " Check the list above. ***")
    return total


# ================================================================================== wheels ===
def wheel_cache(explicit=None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    env = os.environ.get("PMUKIT_WHEEL_CACHE")
    if env:
        return pathlib.Path(env).expanduser().resolve()
    return ROOT / "dist" / "wheelcache"


def download_wheels(dest: pathlib.Path, req: pathlib.Path):
    """Cross-download the box's wheels: cp311 / x86_64 / manylinux2014 (glibc 2.17)."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-r", str(req), "--dest", str(dest),
           "--only-binary=:all:", "--python-version", PY_TAG, "--implementation", "cp",
           "--abi", ABI]
    for p in PLATFORMS:
        cmd += ["--platform", p]
    print("      $ python -m " + " ".join(cmd[2:]))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise PmuError(
            what="Cross-downloading the Linux wheels failed.",
            why=("`pip download --platform manylinux2014_x86_64` could not resolve or fetch every"
                 " requirement (no network, a proxy, or a pin with no manylinux_2_17 wheel)."),
            do=["Re-run with --dry-run to build the package from the local wheel cache.",
                "If a pin has no glibc-2.17 wheel, downpin it in requirements.txt and retry."],
            where=str(req))
    return dest


def copy_cached_wheels(dest: pathlib.Path, cache: pathlib.Path):
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    if cache.is_dir():
        for w in sorted(cache.glob("*.whl")):
            shutil.copy2(w, dest / w.name)
            n += 1
    if n == 0:
        print(f"*** WARN: --dry-run and the wheel cache {cache} is empty -> the package has NO"
              " wheels. Run once WITHOUT --dry-run (on a networked desk) to fill the cache. ***")
    else:
        print(f"      copied {n} cached wheel(s) from {cache}")
    return dest


def refresh_cache(wheels: pathlib.Path, cache: pathlib.Path):
    cache.mkdir(parents=True, exist_ok=True)
    for w in sorted(wheels.glob("*.whl")):
        shutil.copy2(w, cache / w.name)


def freeze_lock(wheels: pathlib.Path) -> str:
    """requirements.lock from the exact wheels present (name==version, sorted)."""
    pins = {}
    for w in sorted(pathlib.Path(wheels).glob("*.whl")):
        parts = w.name[:-4].split("-")
        if len(parts) >= 2:
            pins[parts[0].replace("_", "-").lower()] = parts[1]
    return "".join(f"{n}=={v}\n" for n, v in sorted(pins.items()))


def audit(wheels: pathlib.Path, explain=False):
    rows, viol = audit_wheels.audit_dir(wheels, max_glibc=TARGET_GLIBC, arch=ARCH)
    audit_wheels.print_table(rows, viol, TARGET_GLIBC, ARCH, explain=explain)
    if viol:
        names = ", ".join(r["name"] for r in viol)
        raise PmuError(
            what=f"{len(viol)} wheel(s) would not load on the box.",
            why=("The box's baseline is glibc 2.17 (manylinux2014); these wheels need a newer"
                 " glibc or the wrong CPython ABI, so `import` dies with 'GLIBC_2.x not found'."),
            do=["Downpin the offending package in requirements.txt to its last manylinux_2_17"
                " release, then re-run the packager.",
                "Inspect with: python deploy/audit_wheels.py <pkg>/wheels --explain"],
            where=names)
    return rows


# ================================================================================ manifest ===
def _iter_package_files(out: pathlib.Path, exclude=("MANIFEST.json", "SHA256SUMS")):
    for p in sorted(out.rglob("*")):
        if p.is_file():
            rel = p.relative_to(out).as_posix()          # POSIX keys: a WindowsPath str has
            if rel not in exclude:                       # backslashes -> every file reads
                yield rel, p                             # "missing" on Linux


def write_manifest(out: pathlib.Path, **extra) -> dict:
    files = {rel: {"sha256": _sha256(p), "size": p.stat().st_size}
             for rel, p in _iter_package_files(out)}
    # `mode` et al. FIRST, the (long) file table last: apply greps "mode" out of this file with
    # sed, and a leading file table would give a path key the chance to match first.
    manifest = dict(schema="pmukit/package/1", **extra)
    manifest["files"] = files
    _write_text(out / "MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    return manifest


def write_sha256sums(out: pathlib.Path) -> int:
    """`sha256sum -c SHA256SUMS`, run from the package root, verifies every shipped file."""
    lines = [f"{_sha256(p)}  {rel}" for rel, p in _iter_package_files(out, exclude=("SHA256SUMS",))]
    _write_text(out / "SHA256SUMS", "\n".join(lines) + "\n")
    return len(lines)


def load_manifest(where) -> tuple:
    p = pathlib.Path(where)
    if p.is_dir():
        p = p / "MANIFEST.json"
    if not p.is_file():
        raise PmuError(
            what=f"No MANIFEST.json at {where!r}.",
            why="--incremental needs the previous package (or its MANIFEST.json) to diff against.",
            do=["Pass the directory a previous `package.py --out ...` wrote, e.g."
                " --incremental dist/pkg",
                "If there is no previous package, build a --full one first."],
            where=str(p))
    return json.loads(p.read_text(encoding="utf-8")), p


# =================================================================================== build ===
def _prepare_out(out: pathlib.Path):
    if out.exists():
        if (out / ".git").exists() or (out / "pyproject.toml").exists():
            raise PmuError(
                what=f"Refusing to use {out} as the package output directory.",
                why="It looks like a source checkout (it has .git / pyproject.toml) and the"
                    " packager wipes its output directory before building.",
                do=["Pass an empty or scratch path, e.g. --out dist/pkg."],
                where=str(out))
        shutil.rmtree(out)
    out.mkdir(parents=True)


def _copy_installers(out: pathlib.Path):
    """apply + update.sh live at the package root AND inside app/deploy/ (single source)."""
    for name in INSTALLERS:
        src = HERE / name
        if not src.is_file():
            raise PmuError(
                what=f"deploy/{name} is missing.",
                why="The installer scripts are part of the package; without them the box has no"
                    " way to install what you built.",
                do=[f"Restore {src} from git."],
                where=str(src))
        text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
        _write_text(out / name, text)                    # re-written LF, whatever the desk did


def build(out: pathlib.Path, mode="full", prev=None, dry_run=False, cache=None,
          explain=False, make_tar=False, root=ROOT):
    root = pathlib.Path(root)
    _prepare_out(out)
    built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sha, ver = _git_sha(root), _version(root)
    req = root / "requirements.txt"
    req_input_hash = _sha256(req) if req.is_file() else ""

    deleted, req_lock_hash = [], ""
    if mode == "incremental":
        last, last_path = load_manifest(prev)
        req_lock_hash = last.get("requirements_hash", "")
        if last.get("requirements_input_hash") and last["requirements_input_hash"] != req_input_hash:
            raise PmuError(
                what="requirements.txt changed since the package you are diffing against.",
                why="An incremental package ships NO wheels, so the box's venv would keep the old"
                    " dependency set while the code expects the new one.",
                do=["Build a --full package instead (it re-downloads and re-audits the wheels)."],
                where=str(last_path))
        prev_files = last.get("files", {})
        all_rels = source_files(root)
        changed = []
        for rel in all_rels:
            key = f"app/{rel}"
            old = prev_files.get(key)
            if old is None or old.get("sha256") != _sha256(root / rel):
                changed.append(rel)
        have = {f"app/{r}" for r in all_rels}
        deleted = sorted(k for k in prev_files if k.startswith("app/") and k not in have)
        print(f"[1/5] staging CHANGED source ({len(changed)} of {len(all_rels)} files,"
              f" {len(deleted)} deleted) ...")
        stage_app(out, root, only=changed)
        wheels_named = []
    else:
        print("[1/5] staging source ...")
        stage_app(out, root)
        print("[2/5] wheels for cp311 / x86_64 / manylinux2014 (glibc 2.17) ...")
        wdir = out / "wheels"
        cachedir = wheel_cache(cache)
        if dry_run:
            print(f"      --dry-run: NO network; using cache {cachedir}")
            copy_cached_wheels(wdir, cachedir)
        else:
            download_wheels(wdir, req)
            refresh_cache(wdir, cachedir)
        print("[3/5] AUDIT wheels (reject anything above glibc 2.17) ...")
        if any(wdir.glob("*.whl")):
            audit(wdir, explain=explain)
        else:
            print("      (no wheels present -- audit skipped; see the WARN above)")
        lock = freeze_lock(wdir)
        _write_text(out / "requirements.lock", lock)
        req_lock_hash = _sha256_text(lock)
        wheels_named = sorted(w.name for w in wdir.glob("*.whl"))

    print("[4/5] installers + VERSION ...")
    _copy_installers(out)
    _write_text(out / "VERSION", f"{ver}+g{sha}\n")

    print("[5/5] MANIFEST.json + SHA256SUMS ...")
    manifest = write_manifest(
        out, mode=mode, version=ver, git_sha=sha, built_utc=built,
        python=f"cp{PY_TAG}", arch=ARCH, target_glibc="%d.%d" % TARGET_GLIBC,
        requirements_input_hash=req_input_hash, requirements_hash=req_lock_hash,
        wheels=wheels_named, deleted=deleted)
    n = write_sha256sums(out)
    stage_summary(out)

    tar = None
    if make_tar:
        tar = out.parent / (out.name + ".tar.gz")     # never with_suffix(): 'pkg.v2' -> 'pkg.tar.gz'
        with tarfile.open(tar, "w:gz") as t:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    t.add(p, arcname=p.relative_to(out).as_posix())
        _write_text(tar.parent / (tar.name + ".sha256"), f"{_sha256(tar)}  {tar.name}\n")
        # the one-step installer travels NEXT TO the tarball (it has to exist before the unpack)
        installer = tar.parent / "pmukit_install.sh"
        _write_text(installer, (root / "deploy" / "pmukit_install.sh").read_text(encoding="utf-8"))

    print(f"\nDONE -> {out}   mode={mode}  version={ver}+g{sha}")
    print(f"       {len(manifest['files'])} files in MANIFEST, {n} lines in SHA256SUMS,"
          f" {len(wheels_named)} wheel(s)")
    if deleted:
        print(f"       delete list: {len(deleted)} path(s) the box will remove")
    if tar:
        print(f"       tarball     : {tar}  ({tar.stat().st_size / 1e6:.1f} MB) + .sha256")
        print(f"\nUpload these 3 files into one folder on the box (e.g. <workarea>/pmukit):")
        print(f"    {tar.name}   {tar.name}.sha256   pmukit_install.sh")
        print("then, in that folder:   bash pmukit_install.sh")
        return manifest
    print("\nOn the box:   cd <package>  &&  bash apply")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "pkg"), help="package directory to build")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="source + wheels (default)")
    g.add_argument("--incremental", metavar="PREV_PACKAGE",
                   help="ship only files whose sha256 changed vs PREV_PACKAGE, plus a delete list")
    ap.add_argument("--dry-run", action="store_true",
                    help="everything except the network download (wheels come from the cache)")
    ap.add_argument("--wheel-cache", default=None,
                    help="wheel cache dir (default $PMUKIT_WHEEL_CACHE else dist/wheelcache)")
    ap.add_argument("--explain", action="store_true", help="verbose wheel audit table")
    ap.add_argument("--tar", action="store_true", help="also emit <out>.tar.gz + .sha256 sidecar")
    a = ap.parse_args(argv)
    build(pathlib.Path(a.out).resolve(),
          mode="incremental" if a.incremental else "full", prev=a.incremental,
          dry_run=a.dry_run, cache=a.wheel_cache, explain=a.explain, make_tar=a.tar)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PmuError as e:
        print("\n" + str(e), file=sys.stderr)
        raise SystemExit(1)
