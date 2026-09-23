#!/bin/bash
# pmukit ONE-STEP installer for the box.  Carried next to the tarball, NOT inside it.
#
#     <any folder you made, e.g. <workarea>/pmukit>/
#         pkg.tar.gz  pkg.tar.gz.sha256  pmukit_install.sh      <- the three uploaded files
#
#     cd <that folder>
#     bash pmukit_install.sh                  # newest *.tar.gz here (full OR incremental)
#     bash pmukit_install.sh pkg_i.tar.gz     # or name one
#
# EVERYTHING lands inside the folder this script sits in -- nothing in $HOME, nothing in /tmp:
#
#     install/   the tool: app/, .venv/ (numpy + scipy live here), bin/, wheels/
#     data/      $PMUKIT_DATA: projects, runs, delivered models, site.json
#     tmp/       TMPDIR for the install (emptied at the end)
#     env.csh    `source` it in tcsh to use pmukit       env.sh: same for bash
#     install.log  the full output of the last run -- paste it back if something fails
#
# The only thing used from outside the folder is the system python3.11 the venv is built from
# (override: setenv PMUKIT_PYTHON /path/to/python3.11).
#
# Re-running is safe: a newer FULL tarball replaces app/ and reuses the venv; an INCREMENTAL
# tarball is handed to update.sh.  data/ and an existing env.csh are never touched.
#
# THIS FILE MUST BE LF.  CRLF -> `set -euo pipefail\r` -> "invalid option name".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="$ROOT/install.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

die() {        # four-part error, same shape as pmukit.errors.PmuError
    echo ""
    echo "What : $1"
    echo "Why  : $2"
    echo "Do   : $3"
    echo "Where: ${4:-$ROOT}"
    echo ""
    echo "(full output saved in $LOG)"
    exit 1
}

STAGE=""
cleanup() {
    [ -n "$STAGE" ] && rm -rf "$STAGE" || true
    [ -d "$ROOT/tmp" ] && find "$ROOT/tmp" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
}
trap cleanup EXIT

echo "== pmukit install into $ROOT =="

# ------------------------------------------------------------------------- 1. which tarball --
TAR="${1:-}"
if [ -z "$TAR" ]; then
    TAR="$(ls -1t "$ROOT"/*.tar.gz 2>/dev/null | head -1 || true)"
    [ -n "$TAR" ] || die "No *.tar.gz next to this script." \
        "pmukit_install.sh installs the package tarball that sits in the same folder." \
        "Upload pkg.tar.gz + pkg.tar.gz.sha256 into $ROOT and re-run: bash pmukit_install.sh"
    n="$(ls -1 "$ROOT"/*.tar.gz | wc -l)"
    [ "$n" -gt 1 ] && echo "   ($n tarballs here; taking the NEWEST. Name one to override: bash pmukit_install.sh <file>.tar.gz)"
fi
case "$TAR" in /*) ;; *) TAR="$PWD/$TAR" ;; esac
[ -f "$TAR" ] || die "Tarball '$TAR' not found." "The path given does not exist." \
    "Check the name:  ls $ROOT/*.tar.gz" "$TAR"
echo "   tarball : $TAR"

# --------------------------------------------------------------- 2. python 3.11, before anything --
PYBIN="${PMUKIT_PYTHON:-python3.11}"
command -v "$PYBIN" >/dev/null 2>&1 || die "No python3.11 on PATH." \
    "The package's numpy/scipy wheels are cp311; the venv must be built from a 3.11 interpreter." \
    "setenv PMUKIT_PYTHON /path/to/python3.11   then re-run: bash pmukit_install.sh" "PATH=$PATH"
"$PYBIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]==(3,11) else 1)' \
    || die "$PYBIN is not Python 3.11 ($("$PYBIN" -V 2>&1))." \
           "The shipped wheels are tagged cp311, so pip refuses them on any other version." \
           "setenv PMUKIT_PYTHON /path/to/python3.11   then re-run: bash pmukit_install.sh"
export PMUKIT_PYTHON="$(command -v "$PYBIN")"
echo "   python  : $PMUKIT_PYTHON  ($("$PYBIN" -V 2>&1))"

# ------------------------------------------------------------------------ 3. tarball sha256 --
SUM="$TAR.sha256"
[ -f "$SUM" ] || die "No $(basename "$SUM") next to the tarball." \
    "Without the sidecar there is no way to tell a complete upload from a truncated one." \
    "Upload $(basename "$SUM") (it is built next to the tarball) into $ROOT and re-run." "$SUM"
echo "[a] sha256 of the tarball ..."
sed 's/\r$//' "$SUM" | (cd "$(dirname "$TAR")" && sha256sum -c -) \
    || die "The tarball is corrupt or incomplete." \
           "Its sha256 does not match $(basename "$SUM")." \
           "Upload the tarball again (binary mode) and re-run." "$TAR"

# ---------------------------------------------------------- 4. pin every location to $ROOT --
mkdir -p "$ROOT/install" "$ROOT/data" "$ROOT/tmp"
export PMUKIT_PREFIX="$ROOT/install"
export PMUKIT_DATA="$ROOT/data"
export TMPDIR="$ROOT/tmp"                   # webprobe / ensurepip temp files
export PIP_NO_CACHE_DIR=1                   # no ~/.cache/pip
export PIP_DISABLE_PIP_VERSION_CHECK=1      # no pip self-check state in ~
export XDG_CACHE_HOME="$ROOT/tmp/cache"     # anything else that would reach for ~/.cache
export PMUKIT_INSTALLER=1                   # apply: skip its ~/.cshrc advice, env.csh replaces it

had_home_prefix=0; [ -e "$HOME/pmukit" ] && had_home_prefix=1
had_home_data=0;   [ -e "$HOME/pmukit_data" ] && had_home_data=1

# ------------------------------------------------------------------ 5. unpack + hand to apply --
STAGE="$ROOT/tmp/stage.$$"
rm -rf "$STAGE"; mkdir -p "$STAGE"
echo "[b] unpacking into $STAGE ..."
tar xzf "$TAR" -C "$STAGE"
[ -f "$STAGE/apply" ] && [ -f "$STAGE/MANIFEST.json" ] || die \
    "This tarball is not a pmukit package." \
    "It has no apply + MANIFEST.json at its top level." \
    "Build it on the desk with: python deploy/package.py --out dist/pkg --tar" "$TAR"
echo "   version : $(cat "$STAGE/VERSION" 2>/dev/null || echo unknown)"
echo "[c] bash apply ..."
echo ""
sed -i 's/\r$//' "$STAGE/apply" "$STAGE/update.sh" 2>/dev/null || true
bash "$STAGE/apply"
echo ""

# ------------------------------------------------------------------------------ 6. env files --
if [ ! -f "$ROOT/env.csh" ]; then
    cat > "$ROOT/env.csh" <<EOF
# pmukit environment (tcsh). Written once by pmukit_install.sh; re-installs leave it alone,
# so add your own lines below (e.g. setenv PMUKIT_ALPS_ROOT ...).
#     source $ROOT/env.csh
setenv PMUKIT_PREFIX $ROOT/install
setenv PMUKIT_DATA   $ROOT/data
setenv PATH          $ROOT/install/bin:\$PATH
rehash
EOF
    echo "[d] wrote $ROOT/env.csh"
else
    echo "[d] kept existing $ROOT/env.csh"
fi
if [ ! -f "$ROOT/env.sh" ]; then
    cat > "$ROOT/env.sh" <<EOF
# pmukit environment (bash):  source $ROOT/env.sh
export PMUKIT_PREFIX=$ROOT/install
export PMUKIT_DATA=$ROOT/data
export PATH=$ROOT/install/bin:\$PATH
EOF
fi

# ---------------------------------------------------------- 7. did anything escape the folder --
leak=""
[ "$had_home_prefix" = 0 ] && [ -e "$HOME/pmukit" ] && leak="$leak $HOME/pmukit"
[ "$had_home_data" = 0 ] && [ -e "$HOME/pmukit_data" ] && leak="$leak $HOME/pmukit_data"
SITE="$("$ROOT/install/.venv/bin/python" -c 'import numpy,os;print(os.path.dirname(os.path.dirname(numpy.__file__)))' 2>/dev/null || echo '?')"
case "$SITE" in "$ROOT"/*) ;; *) leak="$leak numpy@$SITE" ;; esac

echo ""
echo "================================================================================"
echo " pmukit installed: $(cat "$ROOT/install/INSTALL.json" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
echo "   tool        $ROOT/install"
echo "   numpy/scipy $SITE"
echo "   data        $ROOT/data"
if [ -n "$leak" ]; then
    echo "   !! created OUTSIDE the folder:$leak"
else
    echo "   nothing written outside $ROOT"
fi
echo ""
echo " Next (tcsh):"
echo "   source $ROOT/env.csh"
echo "   pmukit --help"
echo "   pmukit site --engine donau_alps --queue short --cpus 8 --account <your Donau account>"
echo "   pmukit ui                      # paste the printed http://127.0.0.1:... into Firefox"
echo "================================================================================"
