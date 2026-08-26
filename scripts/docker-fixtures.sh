#!/usr/bin/env bash
# Shared, disposable fixtures for the container validation tier.
set -euo pipefail

: "${LOCAL_CHAT_CI_ROOT:=${TMPDIR:-/tmp}/euesto-validation}"
export LOCAL_CHAT_CI_ROOT
export LOCAL_CHAT_SECRETS_DIR="$LOCAL_CHAT_CI_ROOT/secrets"
export LOCAL_CHAT_WORKSPACE="$LOCAL_CHAT_CI_ROOT/workspace"
mkdir -p "$LOCAL_CHAT_SECRETS_DIR" "$LOCAL_CHAT_WORKSPACE"
printf '%s' 'safe fixture' > "$LOCAL_CHAT_WORKSPACE/fixture.txt"
printf '%s' 'ci-token-with-at-least-thirty-two-bytes-0001' > "$LOCAL_CHAT_SECRETS_DIR/gateway_token.txt"
printf '%s' 'ci-executor-token-with-at-least-thirty-two-bytes-0001' > "$LOCAL_CHAT_SECRETS_DIR/executor_token.txt"
chmod 700 "$LOCAL_CHAT_SECRETS_DIR" "$LOCAL_CHAT_CI_ROOT"
chmod 600 "$LOCAL_CHAT_SECRETS_DIR"/*.txt

cleanup_docker_fixtures() {
  docker compose --file docker/compose.yaml --profile agent down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$LOCAL_CHAT_CI_ROOT"
}
trap cleanup_docker_fixtures EXIT INT TERM

echo "LOCAL_CHAT_CI_ROOT=$LOCAL_CHAT_CI_ROOT"
echo "LOCAL_CHAT_SECRETS_DIR=$LOCAL_CHAT_SECRETS_DIR"
echo "LOCAL_CHAT_WORKSPACE=$LOCAL_CHAT_WORKSPACE"
