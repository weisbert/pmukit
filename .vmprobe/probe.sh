#!/bin/bash
# VM reachability prober: logs one line per attempt, stops on first success.
LOG=/c/code/pmukit/.vmprobe/vm_probe.log
for i in $(seq 1 40); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -o BatchMode=yes -o ConnectTimeout=8 ewave-vm 'tcsh -c "source ~/.cshrc; which spectre"' 2>&1)
  RC=$?
  if [ $RC -eq 0 ] && echo "$OUT" | grep -q spectre; then
    echo "$TS UP rc=$RC $OUT" >> "$LOG"
    echo "VM_UP" > /c/code/pmukit/.vmprobe/STATE
    exit 0
  fi
  echo "$TS DOWN rc=$RC $(echo $OUT | head -c 120)" >> "$LOG"
  echo "VM_DOWN" > /c/code/pmukit/.vmprobe/STATE
  sleep 600
done
