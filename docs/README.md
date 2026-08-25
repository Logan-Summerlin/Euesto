# Euesto documentation

Euesto is a local-first Windows chatbot. The desktop talks to an authenticated gateway; the gateway talks to a network-disabled executor; executor changes remain in ephemeral staging until the desktop publication broker approves and applies them.

## Recommended reading order

1. [Architecture](ARCHITECTURE.md) — system boundaries and dependency direction.
2. [Contributing](CONTRIBUTING.md) — setup, workflow, and review rules.
3. [Testing](TESTING.md) — test taxonomy, markers, and required checks.
4. [Tools](TOOLS.md), [Limits](LIMITS.md), and [Publication](PUBLICATION.md) — contracts and security-sensitive behavior.
5. [Troubleshooting](TROUBLESHOOTING.md) — operational failures and recovery.

## Where to make a change

| Concern | Owner | Notes |
|---|---|---|
| Qt Quick presentation | `qml/` | Views and composition only; call the bridge rather than implementing policy. |
| Desktop state and adapters | `src/` | Controllers, persistence, gateway client, runtime, approvals, and publication coordination. `qml_backend.py` is the QML adapter. |
| Process/bootstrap wiring | `app.py` | Application startup and dependency wiring only. |
| Provider and agent behavior | `server/` | Gateway, OpenRouter, budgets, journals, sessions, and agent loops. |
| Workspace tools and staging | `executor/` | Bounded, network-disabled filesystem execution; never publication. |
| Protocol and registries | `shared/` | Framework-neutral structures; no desktop or gateway orchestration. |
| Tests | `tests/` | Follow the taxonomy in `TESTING.md`; security and container tests must remain credential-free. |

## Local-only state

`.local-chat-snapshot.json` and `.local-chat-checkpoints/` are runtime artifacts. They are ignored and must not be committed. The historical `archived-doc/` directory is non-normative.

## Checks

```text
pytest
pytest -m "slow and not docker"
pytest -m docker
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```
