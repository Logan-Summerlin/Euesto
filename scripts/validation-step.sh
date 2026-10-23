#!/usr/bin/env bash
# Run one validation command with a heartbeat, keeping every line in validation-logs/<name>.log.
#
# A hung command must still say what it was doing. The heartbeat proves the step was alive; the
# last line before it names the tier, test, or command that stopped making progress. Heartbeats
# go to the console *and* the log (`tee -a`), because a heartbeat that only reaches a file
# proves liveness to nobody while an operator is watching the step do nothing.
#
# The command's own output goes to the file rather than a pipe, so a grandchild that outlives
# the tier's kill (scripts/validate.py) cannot keep the step open; the file is printed as soon
# as the command ends and the workflow uploads the whole directory with `if: always()`.
#
# This file is LF on purpose: it runs under bash on Linux runners, and a CRLF copy fails there
# with `$'\r': command not found`.
#
# Usage: validation-step.sh <name> <command...>
set -uo pipefail

name="${1:?usage: validation-step.sh <name> <command...>}"
shift

mkdir -p validation-logs
log="validation-logs/${name}.log"
: > "$log"

started=$(date +%s)
echo "[start] ${name} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$log"

(
  while sleep 20; do
    printf '[heartbeat] %s still running after %ss\n' \
      "$name" "$(( $(date +%s) - started ))" | tee -a "$log"
  done
) &
heartbeat=$!
trap 'kill "$heartbeat" 2>/dev/null' EXIT

"$@" >> "$log" 2>&1
status=$?

trap - EXIT
kill "$heartbeat" 2>/dev/null
wait "$heartbeat" 2>/dev/null
echo "[done] ${name} exited ${status} after $(( $(date +%s) - started ))s" >> "$log"
cat "$log"
exit "$status"
