#!/usr/bin/env bash
# vmrun.sh -- run a Spectre deck on the simulation VM and bring the results back.
#
#   tools/vmrun.sh <local_run_dir> <tag> [extra spectre args...]
#
# What it does
#   1. tars <local_run_dir> and unpacks it into ~/pmukit_work/<tag>/ on $PMUKIT_VM
#      (the remote dir is wiped first, so a run is always reproducible from the local dir)
#   2. runs, on the VM:
#        tcsh -c "source ~/.cshrc; cd ~/pmukit_work/<tag>; \
#                 spectre -64 input.scs -format psfascii -raw raw +log spectre.log -E <extra>"
#      The Cadence environment lives ONLY in ~/.cshrc, so the remote command MUST be a
#      tcsh login-ish shell that sources it.  A plain `ssh vm spectre` will not find it.
#   3. copies raw/ and spectre.log back into <local_run_dir>
#   4. exits non-zero with the tail of spectre.log if the run failed
#
# This script itself runs on the desk under bash (Git Bash on Windows works: it only needs
# ssh/scp/tar).  Only the REMOTE command is tcsh.  No rsync required (Git Bash has none).
#
# Env knobs:
#   PMUKIT_VM        ssh destination            (default: ewave-vm)
#   PMUKIT_VM_ROOT   remote scratch root        (default: ~/pmukit_work)
#   PMUKIT_DECK      deck file name             (default: input.scs)
#   PMUKIT_SSH_OPTS  extra ssh options          (default: -o BatchMode=yes)
set -eu

usage() {
    sed -n '2,25p' "$0" >&2
    exit 2
}

[ $# -ge 2 ] || usage

LOCAL_DIR=$1
TAG=$2
shift 2
EXTRA=$*

VM=${PMUKIT_VM:-ewave-vm}
VM_ROOT=${PMUKIT_VM_ROOT:-'~/pmukit_work'}
DECK=${PMUKIT_DECK:-input.scs}
SSH_OPTS=${PMUKIT_SSH_OPTS:--o BatchMode=yes}

[ -d "$LOCAL_DIR" ] || { echo "vmrun: no such directory: $LOCAL_DIR" >&2; exit 2; }
[ -f "$LOCAL_DIR/$DECK" ] || { echo "vmrun: no $DECK in $LOCAL_DIR" >&2; exit 2; }

# The tag names a directory, so keep it boring.
case $TAG in
    *[!A-Za-z0-9_.-]*) echo "vmrun: tag must be [A-Za-z0-9_.-]+ (got '$TAG')" >&2; exit 2 ;;
esac

REMOTE_DIR="$VM_ROOT/$TAG"

# ---- 1. ship the deck -------------------------------------------------------------------
# shellcheck disable=SC2086
ssh $SSH_OPTS "$VM" "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR"
# tar-pipe: one round trip, keeps the directory layout, no rsync on the desk.
# shellcheck disable=SC2086
tar -C "$LOCAL_DIR" -cf - . | ssh $SSH_OPTS "$VM" "cd $REMOTE_DIR && tar -xf -"

# ---- 2. run spectre under tcsh so ~/.cshrc supplies the Cadence env ----------------------
RUN="cd $REMOTE_DIR; spectre -64 $DECK -format psfascii -raw raw +log spectre.log -E $EXTRA"
set +e
# shellcheck disable=SC2086
ssh $SSH_OPTS "$VM" "tcsh -c 'source ~/.cshrc; $RUN'"
RC=$?
set -e

# ---- 3. bring the results home ----------------------------------------------------------
# shellcheck disable=SC2086
ssh $SSH_OPTS "$VM" "cd $REMOTE_DIR && tar -cf - spectre.log raw 2>/dev/null" \
    | tar -C "$LOCAL_DIR" -xf - 2>/dev/null || true

# ---- 4. verdict -------------------------------------------------------------------------
LOG="$LOCAL_DIR/spectre.log"
FATAL=0
if [ -f "$LOG" ] && grep -qi 'fatal error' "$LOG"; then FATAL=1; fi
# A run that produced no raw/ never really ran (bad transfer, wrong path, killed job).
if [ ! -d "$LOCAL_DIR/raw" ]; then FATAL=1; fi

if [ "$RC" -ne 0 ] || [ "$FATAL" -ne 0 ]; then
    echo "vmrun: spectre FAILED (rc=$RC) for tag '$TAG'" >&2
    if [ -f "$LOG" ]; then
        echo "---- tail of $LOG ----" >&2
        tail -n 40 "$LOG" >&2
    else
        echo "(no spectre.log came back -- check ssh to $VM)" >&2
    fi
    exit 1
fi

echo "vmrun: ok  tag=$TAG  ->  $LOCAL_DIR/raw"
