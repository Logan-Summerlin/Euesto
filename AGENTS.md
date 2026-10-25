# AGENTS.md

Durable invariants for anyone changing this repository. Detailed behavior lives in `docs/`; this file does not repeat it. Read it before editing.

## Mission

Euesto is a local-first Windows chatbot with a contained OpenRouter agent. Prefer a small, auditable core. Do not add features that weaken security, privacy, cost control, or maintainability.

## System boundary

```text
Desktop -> loopback gateway -> Unix-socket executor -> runner (Bash) -> staging -> reviewed publication (desktop broker)
```

- **Desktop** (`app.py`, `src/`, `qml/`): UI, history, approvals, runtime management, and the only host publication path.
- **Gateway** (`server/`): provider calls, agent loop, budgets, sessions, journal, skills. No workspace mount.
- **Executor** (`executor/`): control plane for file tools, staging, checkpoints, baselines, and manifests. No network, runs no commands, never publishes.
- **Runner** (`runner/`): runs Bash against staging only. No network, source mount, state volume, secrets, or executor credential. Never publishes.
- **`shared/`**: framework-neutral protocol. Must not import `src/` or `server/`.
- **`egress/`**: opt-in allowlisted CONNECT proxy (`docs/EGRESS.md`).

## Public tools

The model-facing surface is exactly thirteen tools:

- Executor (nine): `read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, `status`.
- Gateway (four): `investigate_repository`, `update_plan`, `web_search`, `fetch_url`.

- **Plan** mode: `read`, `grep`, `find`, `ls`, `investigate_repository`, `update_plan` only; read-only.
- **Agent** mode: all thirteen; `web_search` and `fetch_url` only when the workspace opts in. Mutations (`write`, `edit`, `apply_patch`, `bash`) affect staging only.
- Consecutive read-only executor calls in one turn may run concurrently; mutations and gateway tools run serialized in call order.
- `investigate_repository` is read-only, capped at four calls per turn, and restricted to `read`, `grep`, `find`, `ls`, `status` inside its nested loop.
- `update_plan` is gateway-local and never prompts. The web tools never reach the executor or runner; `fetch_url` asks per URL off its documentation allowlist.
- Approval tiers: `prompt`, `sandbox`, `auto`. Bash asks under every tier when registry access is on, unless a saved rule matches.
- Do not add aliases, legacy compatibility tools, hidden capabilities, or alternate vocabularies. Do not keep compatibility code for old callers.

Reference: `docs/TOOLS.md` (contract), `docs/LIMITS.md` (limits).

## Security invariants

- Normalize and contain every path beneath the selected workspace. Preserve link, device, and reparse-point rejection, and UTF-8 text semantics.
- Executor and runner stay non-root, network-disabled, capability-restricted, and without host publication authority. The only exception is the allowlisted-egress overlay (`docs/EGRESS.md`): the approved one-shot setup container, and the runner in a session that opted into the `registries` network mode. The executor never joins that network.
- The source mount is read-only and mounted only in the executor. Mutations go to persistent, disposable per-workspace staging, removed only by an explicit discard. Never destroy staged work silently.
- Checkpoint every mutation and roll back on failure, cancellation, or timeout. Bash may keep partial progress on a non-zero exit only when `rollback_on_failure: false` is requested; timeouts and cancellation always roll back.
- Bash is non-interactive and bounded: fixed base environment, filtered and bounded user environment.
- Publication requires approval, path bounds, and hash validation through the desktop broker. Larger changesets publish as separately approved, all-or-nothing batches. Binary files travel byte-exact as `content_base64`.
- Read the workspace policy (`.euesto/policy.toml`) from the host only. Apply it only after the user confirms exactly what was reviewed, and fail closed when it is invalid. Auto never publishes a host-executable, dependency, or policy change without explicit confirmation.
- Redact high-confidence secrets from every tool result before it enters provider-bound history.

## Change discipline

- Changing a public tool means updating together: `shared/tools.py`, `server/openrouter/agent.py`, executor dispatch and permissions, the relevant tests, and the matching sections of `docs/TOOLS.md` and `docs/LIMITS.md`.
- Changing publication or security behavior means updating the authoritative doc (`docs/PUBLICATION.md` or `docs/ARCHITECTURE.md`) and the regression or security tests in the same change.
- Each fact has one authoritative home. Link to it; do not restate it in a second file.
- Documents: `README.md`, `AGENTS.md`, and `CHANGELOG.md` at the root only. Living plans and the roadmap live in `docs/` as `UPPER_SNAKE_CASE.md`, indexed in `docs/README.md`. Superseded or completed plans move to `archived-doc/`. No dates in document names.

## Checks

- Canonical entry point: `python scripts/validate.py <tier>`. The one authoritative list of commands is in `docs/TESTING.md` ("Required checks").
- Python is `ruff format`-formatted; the `ruff` tier checks it.
- Tests live in `tests/unit/<domain>/`, `tests/ui/`, `tests/docker/`, or `tests/structural/`, never in `tests/` directly. Test file names are unique across folders.
- Unit tests must not need provider credentials or network. Container and security tests must keep verifying non-root execution, blocked egress, mounts, resource limits, traversal and link rejection, staging recovery, and exact tool-mode boundaries.
- Run the checks that apply to your change, then report results verbatim. Never claim a check passed without running it.

## Scope

- Read-only investigation delegation through `investigate_repository` is in scope. It has no mutation, command, or publication authority.
- Independent sub-agents, multi-agent orchestration, and concurrent staging are out of scope.

## Repository map

| Path | Contents |
|---|---|
| `app.py` | Desktop entry point. Wires the application; holds no domain policy. |
| `src/` | Desktop adapter (`qml_backend.py`), `src/desktop/` services, storage and migrations, gateway client, runtime manager, publication broker. |
| `qml/` | Qt Quick UI. No security or persistence policy. |
| `server/` | Gateway: HTTP API, auth, agent loop (`server/agent/`), OpenRouter client, journal, skills and extensions. |
| `executor/` | Control plane: tool implementations, path safety, staging, checkpoints, drift rebase, sandbox git, limits. |
| `runner/` | Standard-library Bash command server for the runner container. |
| `shared/` | Protocol: tool registry, requests and responses, permissions, policy, events, secret scanning. |
| `egress/` | Allowlisted HTTPS egress proxy (standard library only). |
| `docker/` | Images, Compose files, entrypoint, and `README.container.md` (operator guide). |
| `scripts/` | `validate.py` (checks), bootstrap, dev up/down, QML smoke, Docker fixtures. |
| `tests/` | Unit, UI, Docker, and structural tests. |
| `docs/` | Authoritative references, roadmap, and living plans (see `docs/README.md`). |
| `archived-doc/` | Completed or superseded documents. Not normative. |
| `.github/` | CI workflows: container and validation tiers, Windows release, image vulnerability scan. |
| `build/`, `installer/`, `assets/` | PyInstaller spec and version metadata, Inno Setup script, application icon. |
