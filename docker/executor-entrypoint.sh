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
test -d /source
test -r /source
test -w /work
test -w /state
test -d /run/runner
executor_token_file=${LOCAL_CHAT_EXECUTOR_TOKEN_FILE:-/run/secrets/executor_token}
test -r "$executor_token_file"
test "${LOCAL_CHAT_WORKSPACE_ID:-UNSELECTED}" != "UNSELECTED"
rm -f /run/ipc/executor.sock
exec python -m uvicorn executor.app:create_app --factory --uds /run/ipc/executor.sock --no-access-log --workers 1
