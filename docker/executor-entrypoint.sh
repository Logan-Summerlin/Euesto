#!/bin/sh
set -eu
umask 007
test -d /source
test -r /source
test -w /work
executor_token_file=${LOCAL_CHAT_EXECUTOR_TOKEN_FILE:-/run/secrets/executor_token}
test -r "$executor_token_file"
test "${LOCAL_CHAT_WORKSPACE_ID:-UNSELECTED}" != "UNSELECTED"
rm -f /run/ipc/executor.sock
exec python -m uvicorn executor.app:create_app --factory --uds /run/ipc/executor.sock --no-access-log --workers 1
