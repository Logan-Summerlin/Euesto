# AGENTS.md

Read this file before editing. It contains durable invariants only; detailed behavior belongs in `docs/`.

## Mission

Euesto is a local-first Windows chatbot with a network-disabled executor. Prefer a small, auditable core over features that weaken security, privacy, cost control, or maintainability.

## System boundary

```text
Desktop -> authenticated gateway -> Unix-socket executor -> ephemeral staging -> reviewed publication
```

- Desktop owns UI, history, approvals, runtime management, and host publication.
- Gateway owns provider calls, agent loops, budgets, sessions, and events; it has no workspace mount.
- Executor owns bounded workspace access and staging; it has no network and never publishes to the host.
- `shared/` owns framework-neutral protocol structures.

## Public tools

The only model-facing local tools are `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls`.

- Plan: `read`, `grep`, `find`, `ls` only.
- Agent: all seven; mutations remain in staging.
- Do not add aliases, legacy compatibility tools, hidden capabilities, or alternate public vocabularies.

See `docs/TOOLS.md` for the contract and `docs/LIMITS.md` for limits.

## Security invariants

- Normalize and contain paths beneath the selected workspace.
- Preserve link/device/reparse-point protections and UTF-8 text semantics.
- Keep executor non-root, network-disabled, capability-restricted, and without host publication authority.
- Keep the source mount read-only and mutations in ephemeral staging.
- Checkpoint mutations and roll them back on failure, cancellation, or timeout.
- Keep Bash non-interactive and bounded.
- Require approved, path-bounded, hash-validated publication through the desktop broker.

## Change discipline

When changing a public tool, update `shared/tools.py`, `server/openrouter/agent.py`, executor dispatch/permissions, relevant tests, and the corresponding `docs/TOOLS.md`/`docs/LIMITS.md` sections together. Do not preserve stale compatibility code merely for old callers.

When changing publication or security behavior, update the authoritative documentation and regression/security tests in the same change.

## Checks

Run the applicable checks after changes:

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests must not require provider credentials. Container/security checks must continue to verify non-root execution, blocked egress, mounts, resource limits, traversal/link rejection, staging recovery, and exact tool-mode boundaries.
