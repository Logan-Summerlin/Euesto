# Euesto documentation

Euesto is a local-first Windows chatbot. The desktop talks to an authenticated gateway; the gateway talks to a network-disabled executor; executor changes remain in ephemeral staging until the desktop publication broker approves and applies them.

## Recommended reading order

1. [Architecture](ARCHITECTURE.md) — system boundaries and dependency direction.
2. [Contributing](CONTRIBUTING.md) — setup, workflow, and review rules.
3. [Testing](TESTING.md) — test taxonomy, markers, and required checks.
4. [Tools](TOOLS.md), [Limits](LIMITS.md), [Publication](PUBLICATION.md), and [Egress](EGRESS.md) — contracts and security-sensitive behavior.
5. [Troubleshooting](TROUBLESHOOTING.md) — operational failures and recovery.

## Where to make a change

| Concern | Owner | Notes |
|---|---|---|
| Qt Quick presentation | `qml/` | Views and composition only; call the bridge rather than implementing policy. |
| Desktop state and adapters | `src/` | Controllers, persistence, gateway client, runtime, approvals, and publication coordination. `qml_backend.py` is the thin QML adapter; behavior belongs in the matching service under `src/desktop/` (runtime, settings, conversations, generation, staging/publication). |
| Process/bootstrap wiring | `app.py` | Application startup and dependency wiring only. |
| Provider and agent behavior | `server/` | Gateway, OpenRouter, budgets, journals, sessions, and agent loops. |
| Workspace tools and staging | `executor/` | Bounded, network-disabled filesystem execution; never publication. |
| Opt-in package egress | `egress/`, `docker/compose.egress.yaml` | Allowlisted CONNECT proxy for the install-capable profile; never loaded by default. |
| Protocol and registries | `shared/` | Framework-neutral structures; no desktop or gateway orchestration. |
| Tests | `tests/` | Follow the taxonomy in `TESTING.md`; security and container tests must remain credential-free. |

## Plans

Every living plan and the roadmap live in this directory, one authoritative copy each. Status is recorded per item inside each document; when a plan is fully implemented or superseded it moves to `archived-doc/` (non-normative) rather than being kept alongside a newer copy.

| Document | Scope |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Completed, active, planned, deferred, and non-goal status for the product. |
| [HARNESS_FIX_PLAN.md](HARNESS_FIX_PLAN.md) | Verified coding-harness fixes by tier (P0 correctness through P3 roadmap-scope), with acceptance criteria. |
| [HARNESS_QOL_PLAN.md](HARNESS_QOL_PLAN.md) | Coding-harness quality-of-life improvements (edits, patches, status, investigation budget, validation). |
| [HARNESS_VALIDATION_PLAN.md](HARNESS_VALIDATION_PLAN.md) | Making every documented validation check runnable locally and reproducible in CI. |
| [ORGANIZATION_PLAN.md](ORGANIZATION_PLAN.md) | Repository organization, naming, and the desktop-bridge decomposition. |

Documentation file names are predictable: `UPPER_SNAKE_CASE.md` in `docs/`, lowercase-hyphenated names in `archived-doc/`, and no spaces or embedded dates anywhere (dates belong in commit history or `CHANGELOG.md`). The repository root keeps only `README.md`, `AGENTS.md`, and `CHANGELOG.md`. `tests/structural/test_documentation_layout.py` enforces this.

## Local-only state

`.local-chat-snapshot.json` and `.local-chat-checkpoints/` are runtime artifacts. They are ignored and must not be committed. The historical `archived-doc/` directory is non-normative.

## Checks

```text
pytest
pytest -m "slow and not docker"
pytest -m docker
ruff check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```
