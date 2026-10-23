#!/bin/sh
# The executor's container entrypoint: fail closed on a missing mount or credential, then hand
# the control plane to uvicorn on the gateway-facing socket.
#
# This file is LF on purpose. It is executed directly from the image, so a CRLF copy makes the
# kernel look for an interpreter named `sh` followed by a carriage return, the container exits
# before uvicorn creates the socket, and `docker compose up --wait` fails for every test that
# needs the whole agent profile (tests/structural/test_line_endings.py, docs/TESTING.md).
set -eu
umask 007

# Every precondition names itself: a bare `test` exits silently, and `docker compose up --wait`
# then reports only that the profile never became healthy (docker/README.container.md).
fail() {
  echo "executor: $*" >&2
  exit 1
}

test -d /source && test -r /source \
  || fail "the read-only /source workspace mount is missing or unreadable"
test -w /work || fail "/work is missing or not writable (the staging volume)"
test -w /state || fail "/state is missing or not writable (the state volume)"
test -d /run/runner || fail "the runner socket directory /run/runner is not mounted"
executor_token_file=${LOCAL_CHAT_EXECUTOR_TOKEN_FILE:-/run/secrets/executor_token}
# Compose mounts a `file:` secret as a bind mount, so the container sees the host file's owner
# and mode: a credential only its creator can read is unreadable here, because the executor runs
# as UID 10002 (the same reason the container-tier fixtures are world-readable).
test -r "$executor_token_file" \
  || fail "the executor credential $executor_token_file is unreadable by this container user"
test "${LOCAL_CHAT_WORKSPACE_ID:-UNSELECTED}" != "UNSELECTED" \
  || fail "LOCAL_CHAT_WORKSPACE_ID is unset; the executor refuses to guess a workspace"
rm -f /run/ipc/executor.sock
exec python -m uvicorn executor.app:create_app --factory --uds /run/ipc/executor.sock --no-access-log --workers 1
