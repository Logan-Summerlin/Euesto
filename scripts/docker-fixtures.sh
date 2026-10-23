#!/usr/bin/env bash
# Shared, disposable fixtures for the container validation tier.
#
# This file is LF on purpose: the workflow sources it under `bash -e`, and a CRLF copy fails at
# `set: pipefail<CR>: invalid option name` before any fixture exists, so the container tier then
# runs without the throwaway directories and credentials it needs (tests/structural/
# test_line_endings.py, docs/TESTING.md).
set -euo pipefail

setup_fixtures() {
  : "${LOCAL_CHAT_CI_ROOT:=${TMPDIR:-/tmp}/euesto-validation}"
  export LOCAL_CHAT_CI_ROOT
  export LOCAL_CHAT_SECRETS_DIR="$LOCAL_CHAT_CI_ROOT/secrets"
  export LOCAL_CHAT_WORKSPACE="$LOCAL_CHAT_CI_ROOT/workspace"
  mkdir -p "$LOCAL_CHAT_SECRETS_DIR" "$LOCAL_CHAT_WORKSPACE"
  printf '%s' 'safe fixture' > "$LOCAL_CHAT_WORKSPACE/fixture.txt"
  printf '%s' 'ci-token-with-at-least-thirty-two-bytes-0001' > "$LOCAL_CHAT_SECRETS_DIR/gateway_token.txt"
  printf '%s' 'ci-executor-token-with-at-least-thirty-two-bytes-0001' > "$LOCAL_CHAT_SECRETS_DIR/executor_token.txt"
  chmod 700 "$LOCAL_CHAT_SECRETS_DIR" "$LOCAL_CHAT_CI_ROOT"
  # Compose mounts a `file:` secret as a bind mount, so a container sees this file's host owner
  # and mode rather than a container-owned copy. These fixtures belong to the CI user, while the
  # gateway runs as UID 10001 and the executor as UID 10002, so a 0600 credential is unreadable
  # to both: the executor's entrypoint exits at its credential check, the gateway refuses a
  # token it never read, and `docker compose up --wait` fails for every check that starts the
  # whole agent profile (only the runner-only checks pass). The values are throwaway
  # placeholders and the directory stays 0700, so read access for the container identities is
  # the point of the fixture, not a leak.
  chmod 0644 "$LOCAL_CHAT_SECRETS_DIR"/*.txt

  echo "LOCAL_CHAT_CI_ROOT=$LOCAL_CHAT_CI_ROOT"
  echo "LOCAL_CHAT_SECRETS_DIR=$LOCAL_CHAT_SECRETS_DIR"
  echo "LOCAL_CHAT_WORKSPACE=$LOCAL_CHAT_WORKSPACE"
}

cleanup_docker_fixtures() {
  local ci_root="${LOCAL_CHAT_CI_ROOT:-}"
  if [[ -z "$ci_root" && -n "${LOCAL_CHAT_SECRETS_DIR:-}" ]]; then
    ci_root="${LOCAL_CHAT_SECRETS_DIR%/secrets}"
  fi
  if [[ -z "$ci_root" ]]; then
    ci_root="${TMPDIR:-/tmp}/euesto-validation"
  fi
  docker compose --file docker/compose.yaml --file docker/compose.egress.yaml --file docker/compose.setup.yaml --profile agent down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$ci_root"
}

# Preserve executable-script behavior while allowing the workflow to source this
# file and call setup_fixtures explicitly without triggering cleanup at shell exit.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  setup_fixtures
  trap cleanup_docker_fixtures EXIT INT TERM
fi
