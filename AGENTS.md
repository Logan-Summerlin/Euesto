# AGENTS.md

Read this file before editing. It contains durable invariants only; detailed behavior belongs in `docs/`.

## Mission

Euesto is a local-first Windows chatbot with a network-disabled executor. Prefer a small, auditable core over features that weaken security, privacy, cost control, or maintainability.

## System boundary

```text
Desktop -> authenticated gateway -> Unix-socket executor -> ephemeral staging -> reviewed publication
```

- Desktop owns UI, history, approvals, runtime management, and host publication.
- Gateway owns provider calls, agent loops, budgets, sessions, journals, skills, and events; it has no workspace mount.
- Executor owns bounded workspace access and staging; it has no network and never publishes to the host.
- `shared/` owns framework-neutral protocol structures.

## Public tools

The model-facing local tools are `read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, `status`, and `investigate_repository`.

- Plan: `read`, `grep`, `find`, `ls` only.
- Agent: all ten; mutations (`write`, `edit`, `apply_patch`, `bash`) remain in staging; `status` is read-only staged-change review.
- Consecutive read-only calls in one turn may run concurrently; mutations stay serialized in call order.
- `investigate_repository` is Agent-only, read-only, capped at four calls per turn, and restricted to the Plan tool set inside its nested loop.
- Do not add aliases, legacy compatibility tools, hidden capabilities, or alternate public vocabularies.

See `docs/TOOLS.md` for the contract and `docs/LIMITS.md` for limits.

## Security invariants

- Normalize and contain paths beneath the selected workspace.
- Preserve link/device/reparse-point protections and UTF-8 text semantics.
- Keep executor non-root, network-disabled, capability-restricted, and without host publication authority. The only exception is the opt-in allowlisted-egress overlay (`docs/EGRESS.md`): an internal-only network to a CONNECT proxy that allows named registries; the default profile keeps `network_mode: none`.
- Keep the source mount read-only and mutations in ephemeral staging.
- Checkpoint mutations and roll them back on failure, cancellation, or timeout; Bash may explicitly retain partial progress on a non-zero exit only when `rollback_on_failure: false` is requested, while timeouts and cancellation always roll back.
- Keep Bash non-interactive and bounded (fixed base environment; user env is filtered and bounded).
- Require approved, path-bounded, hash-validated publication through the desktop broker; larger changesets publish as separately approved, all-or-nothing batches, and binary files travel byte-exact as `content_base64`.

## Change discipline

When changing a public tool, update `shared/tools.py`, `server/openrouter/agent.py`, executor dispatch/permissions, relevant tests, and the corresponding `docs/TOOLS.md`/`docs/LIMITS.md` sections together. Do not preserve stale compatibility code merely for old callers.

When changing publication or security behavior, update the authoritative documentation and regression/security tests in the same change.

Keep one authoritative copy of each document. Planning documents and the roadmap live in `docs/` with `UPPER_SNAKE_CASE.md` names (no spaces or dates) and are indexed in `docs/README.md`; superseded plans move to `archived-doc/` instead of staying beside a newer copy. The root holds only `README.md`, `AGENTS.md`, and `CHANGELOG.md` as documentation.

## Checks

The canonical validation entry point is `python scripts/validate.py`; use `preflight` to report missing dependencies explicitly and the named tiers (`fast`, `slow`, `qml`, `docker`, `all`) for execution. Run the applicable checks after changes:

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests must not require provider credentials. Container/security checks must continue to verify non-root execution, blocked egress, mounts, resource limits, traversal/link rejection, staging recovery, and exact tool-mode boundaries.

## Scoped investigation

Read-only repository investigation delegation is in scope through `investigate_repository`. It uses the parent executor session and budget, has no mutation, command, or publication authority, and is limited to four calls per turn. General multi-agent orchestration, independent sessions, and concurrent staging remain out of scope.

## Repository map

Concise guide to each top-level folder:

| Path | Contents |
|---|---|
| `.github/` | CI workflows: container security checks and release packaging. |
| `archived-doc/` | Superseded or fully implemented historical documents. Not normative. |
| `assets/` | Application icon used by the desktop app, packaging, and installer. |
| `build/` | PyInstaller spec and version metadata for Windows packaging. |
| `docker/` | Gateway/executor images, Compose topology, secrets wiring, and the container operator guide (`README.container.md`). |
| `docs/` | Authoritative architecture, tools, limits, publication, contributor, and troubleshooting references, plus the roadmap and every living plan (indexed in `docs/README.md`). |
| `egress/` | Opt-in allowlisted HTTPS egress proxy (standard library only) for the install-capable executor profile. |
| `executor/` | The sandboxed tool service: dispatch, path safety, staging, checkpoints, resource limits. |
| `installer/` | Inno Setup script for the Windows installer. |
| `qml/` | Qt Quick UI (main window, sidebar, transcript, composer). |
| `scripts/` | Developer helpers: locked bootstrap, the validation harness, dev up/down, QML smoke check, and Docker test fixtures. |
| `server/` | Loopback gateway: HTTP API, auth, agent runtime, budgets, journal store, OpenRouter client, skills/capabilities extensions. |
| `shared/` | Framework-neutral protocol: tool registry, requests/responses, permissions, events, publish manifests. |
| `src/` | Desktop application code: thin QML adapter (`qml_backend.py`) over the services in `src/desktop/`, controllers, storage/migrations, gateway client, runtime manager, publication broker. |
| `tests/` | Unit, integration, contract, regression, and security tests (no provider credentials required). |

Root files: `app.py` (desktop entry point), `AGENTS.md` (durable invariants), `README.md` (product overview), `CHANGELOG.md` (release summary), `pyproject.toml` / `requirements*.txt` (Python configuration).
