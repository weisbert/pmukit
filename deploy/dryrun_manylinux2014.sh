#!/bin/bash
# from LDO_modeling/deploy/dryrun_manylinux2014.sh @ d2c5b80
#
# Rehearse the air-gap install BEFORE it reaches the box: prove every wheel in a package is
# installable and importable under a glibc-2.17 tag set, with the network switched off.
#
#     bash deploy/dryrun_manylinux2014.sh dist/pkg          # a package directory
#     IMAGE=quay.io/pypa/manylinux2014_x86_64 bash deploy/dryrun_manylinux2014.sh dist/pkg
#
# TWO paths, and the script says which one it took:
#
#   A. DOCKER available  -- the real thing. Runs the package's own `apply` inside
#      quay.io/pypa/manylinux2014_x86_64 with `--network none`, then imports numpy+scipy.
#      This is a genuine glibc-2.17 execution test.
#
#   B. NO DOCKER (the usual case on the Windows desk) -- a STATIC check, and it says so out
#      loud. It runs `audit_wheels.py --deep --explain`, which (i) re-checks every platform /
#      ABI tag against the 2.17 baseline and (ii) scans every ELF object inside each wheel for
#      `GLIBC_x.y` version references, catching a wheel whose FILENAME claims 2.17 while its
#      .so actually needs 2.28. What B cannot prove: that pip's resolver is happy on the box
#      and that the extensions dynamically link. Those wait for the box.
set -euo pipefail

PKG="${1:?usage: dryrun_manylinux2014.sh <package-dir-or-tarball>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-quay.io/pypa/manylinux2014_x86_64}"
PY="${PMUKIT_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

if [ -d "$PKG" ]; then
    PKG_ABS="$(cd "$PKG" && pwd)"
else
    PKG_ABS="$(cd "$(dirname "$PKG")" && pwd)/$(basename "$PKG")"
fi
[ -e "$PKG_ABS" ] || { echo "ERROR: no such package: $PKG"; exit 1; }

if command -v docker >/dev/null 2>&1; then
    echo "== PATH A: REAL dry-run on $IMAGE, network DISABLED =="
    docker run --rm --network none -v "$PKG_ABS":/pkg:ro "$IMAGE" bash -euxc '
      export PATH=/opt/python/cp311-cp311/bin:$PATH
      command -v python3.11 >/dev/null 2>&1 \
        || ln -sf /opt/python/cp311-cp311/bin/python3.11 /usr/local/bin/python3.11
      python3.11 --version
      ldd --version | head -1                       # expect glibc 2.17
      cp -r /pkg /tmp/pkg && cd /tmp/pkg
      export HOME=/tmp/boxhome PMUKIT_PREFIX=/tmp/boxhome/pmukit PMUKIT_DATA=/tmp/boxhome/pmukit_data
      mkdir -p "$HOME"
      bash apply
      /tmp/boxhome/pmukit/.venv/bin/python -c "import numpy, scipy; print(\"imports OK\", numpy.__version__, scipy.__version__)"
    '
    echo ""
    echo "DRY-RUN PASSED (docker): offline --no-index install + native-wheel import on glibc 2.17."
    exit 0
fi

echo "== PATH B: docker is NOT available on this machine -> STATIC check only =="
echo "   This does NOT execute anything on glibc 2.17. It verifies, without auditwheel:"
echo "     * every wheel's platform/ABI tag against the 2.17 baseline, and"
echo "     * every ELF member's GLIBC_x.y version references (catches a lying filename)."
echo "   Still unproven until the box: pip's resolve on the real interpreter, and dynamic linking."
echo ""
WHEELS="$PKG_ABS/wheels"
[ -d "$WHEELS" ] || { echo "ERROR: $WHEELS not found (is this a --full package?)"; exit 1; }
"$PY" "$HERE/audit_wheels.py" "$WHEELS" --deep --explain
echo ""
echo "STATIC CHECK PASSED. Install docker and re-run for the real glibc-2.17 rehearsal:"
echo "    bash deploy/dryrun_manylinux2014.sh $PKG"
