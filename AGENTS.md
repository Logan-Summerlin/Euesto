# AGENTS.md

Read this file before editing. It is the current operating contract for contributors and coding agents.

## Mission

Euesto is a local-first Windows chatbot with a network-disabled executor. Prefer a small, auditable core over features that weaken security, privacy, cost control, or maintainability.

## System boundary

```text
Desktop -> authenticated gateway -> Unix-socket executor -> ephemeral staging -> reviewed publication
```

- Desktop owns UI, history, approvals, and host publication.
- Gateway owns provider calls, agent loops, budgets, sessions, and events.
- Executor owns bounded workspace access and staging. It has no network and never writes the host workspace.
- `shared/` owns framework-neutral protocol and tool schemas.
- The gateway has no workspace mount. The executor uses `network_mode: none`.
- Only the desktop broker may publish an approved manifest.

## Agent modes

| Mode | Tools |
|---|---|
| Plan | `read`, `grep`, `find`, `ls` |
| Agent | `read`, `write`, `edit`, `bash`, `grep`, `find`, `ls` |

There is one model-facing tool API. Do not add aliases, compatibility profiles, hidden legacy tools, or alternate vocabularies.

## Executor architecture

```text
agent-facing seven-tool API
        -> safe executor primitives
        -> paths / staging / sandbox
        -> checkpoints
        -> desktop publish layer
```

The public tools are deliberately thin. Reusable mutation infrastructure belongs in executor internals, not in agent-facing compatibility wrappers.

## Filesystem security

- Normalize and contain every path beneath the selected workspace.
- Reject symlinks, hard-linked mutation targets, reparse points, device paths, aliases, UNC/device paths, and secret-like paths as applicable.
- Enforce file, output, result, and total staging limits.
- Preserve UTF-8-only text semantics.
- Validate expected hashes when supplied; optional hashes must not become mandatory compatibility shims.
- Create checkpoints before mutations and restore them when a mutation fails.
- Preserve staging and publish-manifest semantics; executor never publishes directly.

## Bash security

`bash` is the only command tool and runs `/bin/bash -lc` non-interactively inside the isolated executor.

- Run as the non-root executor user.
- Use a fixed minimal environment; reject unsafe environment overrides.
- No interactive TTY.
- Bound command length, stdin, stdout/stderr, event history, timeout, and process count through the executor/container limits.
- Start a new process group and terminate the complete group on cancellation or timeout.
- Roll back staged filesystem mutations when a command fails, is cancelled, or times out.
- Keep `network_mode: none`, dropped capabilities, `no-new-privileges`, read-only root, and container CPU/memory/PID limits.

## Transactions

`write`, `edit`, and `bash` checkpoint staging before mutation. Failed mutations roll back. Successful changes remain staged until publication. Checkpoint and staging infrastructure is internal and must not be exposed as model tools.

## Permissions

Mode restrictions are enforced in code, not prompts. Read-only tools may run without approval; mutation tools require approval unless the active session policy permits them. Saved Bash rules must remain scoped to the command prefix and workspace path where applicable.

## Tool schemas

Keep `server/openrouter/agent.py`, `shared/tools.py`, and executor dispatch synchronized. The model-facing schema must describe the actual executor arguments. `write` and `edit` may be used without a hash; a supplied hash is an optimistic concurrency check.

## Documentation and cleanup

Delete dead or superseded code. Do not preserve compatibility helpers merely because old tests or documentation referenced them. When a public tool is removed, remove its executor module, wrappers, aliases, schemas, tests, and stale documentation in the same change.

## Checks

Run the applicable checks after changes:

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests must not require a live provider key. Container and security tests should verify non-root execution, blocked egress, mounts, resource limits, traversal/link rejection, staging recovery, and exact tool-mode boundaries.
