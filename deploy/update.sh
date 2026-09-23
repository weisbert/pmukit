#!/bin/bash
# from LDO_modeling/deploy/update.sh @ d2c5b80
#
# pmukit INCREMENTAL update: refresh the source of an existing install, reuse its venv+wheels.
# Normally you do not call this directly -- `bash apply` on an incremental package does it for you.
#
#     bash update.sh [PACKAGE_DIR]        # default: the directory this script sits in
#
# Guarantees:
#   * $PMUKIT_DATA is NEVER touched (that is where all real work lives);
#   * the venv is NEVER rebuilt -- if requirements.txt moved since the deployed full package,
#     this refuses and tells you to install a full package instead;
#   * launchers are reinstalled ATOMICALLY (temp + mv), so a running launcher is never truncated.
#
# THIS FILE MUST BE LF.  CRLF -> `set -euo pipefail\r` -> "invalid option name".
set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")" && pwd)}"
SRC="$(cd "$SRC" && pwd)"
PREFIX="${PMUKIT_PREFIX:-$HOME/pmukit}"
# the data dir the install was made with, as its launcher records it (else the old default)
_BAKED="$(sed -n 's/^PMUKIT_DATA="\${PMUKIT_DATA:-\(.*\)}"$/\1/p' "$PREFIX/bin/pmukit" 2>/dev/null | head -1 || true)"
DATA="${PMUKIT_DATA:-${_BAKED:-$HOME/pmukit_data}}"

die() {
    echo ""                          >&2
    echo "What : $1"                 >&2
    echo "Why  : $2"                 >&2
    echo "Do   : $3"                 >&2
    [ $# -ge 4 ] && [ -n "$4" ] && echo "     : $4" >&2
    echo "Where: ${5:-$SRC}"         >&2
    exit 1
}
jval() {  # $1 = key, $2 = json file -- first "key": "value" match
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$2" 2>/dev/null | head -1
}

echo "== pmukit update =="
echo "   package : $SRC"
echo "   prefix  : $PREFIX"
echo "   data    : $DATA   (untouched)"

[ -x "$PREFIX/.venv/bin/python" ] || die \
    "There is no pmukit install at $PREFIX." \
    "An incremental update refreshes an existing venv; none was found." \
    "Install a FULL package first:  cd <full-package> && bash apply" "" "$PREFIX/.venv"
[ -f "$SRC/MANIFEST.json" ] || die \
    "This directory is not a pmukit package." \
    "update.sh needs MANIFEST.json to know which files changed and which were deleted." \
    "Run it from inside the unpacked incremental package." "" "$SRC/MANIFEST.json"

NEW_REQ="$(jval requirements_hash "$SRC/MANIFEST.json")"
OLD_REQ="$(jval requirements_hash "$PREFIX/MANIFEST.deployed.json")"
VER="$(cat "$SRC/VERSION" 2>/dev/null || echo unknown)"
echo "   version : $VER"
echo "   req-hash: bundle ${NEW_REQ:0:12}  deployed ${OLD_REQ:0:12}"
if [ -n "$NEW_REQ" ] && [ -n "$OLD_REQ" ] && [ "$NEW_REQ" != "$OLD_REQ" ]; then
    die "The dependency set changed since this box was installed." \
        "An incremental package ships no wheels, so the venv here would stay on the old numpy/scipy." \
        "Build and install a FULL package:  python deploy/package.py --out dist/pkg   (on the desk)" \
        "" "$PREFIX/MANIFEST.deployed.json"
fi

# ------------------------------------------------------------------ 1. verify before touching --
echo "[1/4] verifying MANIFEST.json + SHA256SUMS ..."
"$PREFIX/.venv/bin/python" - "$SRC" <<'PYEOF'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
man = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()
bad = []
for rel, meta in man.get("files", {}).items():
    p = root / rel.replace("\\", "/")
    if not p.is_file():
        bad.append(("MISSING", rel))
    elif sha(p) != meta["sha256"]:
        bad.append(("CORRUPT", rel))
sums = root / "SHA256SUMS"
if sums.is_file():
    raw = sums.read_bytes()
    if b"\r" in raw:
        bad.append(("CRLF", "SHA256SUMS"))
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            want, rel = line.split("  ", 1)
            p = root / rel
            if not p.is_file():
                bad.append(("MISSING", rel))
            elif sha(p) != want:
                bad.append(("CORRUPT", rel))
bad = list(dict.fromkeys(bad))     # MANIFEST and SHA256SUMS overlap: report each file once
if bad:
    for kind, rel in bad[:20]:
        print(f"      {kind}  {rel}")
    print(f"\nWhat : The update package failed its integrity check ({len(bad)} file(s)).")
    print("Why  : A shipped file is missing or its sha256 does not match the packager's record.")
    print("Do   : Re-copy the package from the desk and run the update again.")
    print(f"Where: {bad[0][1]}")
    sys.exit(1)
print(f"      integrity OK ({len(man.get('files', {}))} files)")
PYEOF

# ------------------------------------------------------------- 2. overlay changed + delete old --
echo "[2/4] overlaying changed files onto $PREFIX/app ..."
if [ -d "$SRC/app" ]; then
    ( cd "$SRC/app" && tar cf - . ) | ( cd "$PREFIX/app" && tar xf - )
fi
DEL="$("$PREFIX/.venv/bin/python" - "$SRC" <<'PYEOF'
import json, pathlib, sys
man = json.loads((pathlib.Path(sys.argv[1]) / "MANIFEST.json").read_text(encoding="utf-8"))
for rel in man.get("deleted", []):
    if rel.startswith("app/") and ".." not in rel:
        print(rel[len("app/"):])
PYEOF
)"
n_del=0
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    rm -f "$PREFIX/app/$rel"
    n_del=$((n_del + 1))
done <<< "$DEL"
echo "      overlaid; removed $n_del deleted file(s)"
# drop stale bytecode so a deleted module cannot be resurrected from a .pyc
find "$PREFIX/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
for s in "$PREFIX"/app/deploy/*.sh "$PREFIX/app/deploy/apply"; do
    [ -f "$s" ] && sed -i 's/\r$//' "$s" || true
done

# --------------------------------------------------------- 3. launchers, installed ATOMICALLY --
# temp + mv: an in-place cp would truncate the launcher that may be RUNNING right now.
echo "[3/4] reinstalling launchers ..."
mkdir -p "$PREFIX/bin"
install_launcher() {
    tmp="$PREFIX/bin/.$1.tmp.$$"
    cat > "$tmp"
    sed -i 's/\r$//' "$tmp"
    chmod 755 "$tmp"
    mv -f "$tmp" "$PREFIX/bin/$1"
}
install_launcher pmukit <<EOF
#!/bin/sh
PREFIX="\${PMUKIT_PREFIX:-$PREFIX}"
# The data dir chosen at install time: a shell that never sourced env.csh must not quietly fall
# back to ~/pmukit_data (another site.json, no account, runs landing in \$HOME).
PMUKIT_DATA="\${PMUKIT_DATA:-$DATA}"
PYTHONPATH="\$PREFIX/app\${PYTHONPATH:+:\$PYTHONPATH}"
export PMUKIT_DATA PYTHONPATH
exec "\$PREFIX/.venv/bin/python" -m pmukit "\$@"
EOF
install_launcher pmukit-ui <<EOF
#!/bin/sh
exec "\${PMUKIT_PREFIX:-$PREFIX}/bin/pmukit" ui "\$@"
EOF

# ------------------------------------------------------------------------ 4. post-update check --
echo "[4/4] post-update check ..."
"$PREFIX/.venv/bin/python" "$PREFIX/app/deploy/postinstall_check.py" --prefix "$PREFIX" || {
    echo "*** update applied, but the browser-path check did not fully pass (see above)."
}
cp "$SRC/MANIFEST.json" "$PREFIX/MANIFEST.deployed.json"

echo ""
echo "OK. $PREFIX/app refreshed to $VER; venv and $DATA untouched."
