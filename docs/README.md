# Euesto documentation

Euesto is a local-first Windows chatbot. The desktop talks to an authenticated gateway; the gateway talks to a network-disabled executor, which runs commands in a separate network-disabled runner; changes remain in persistent, disposable staging until the desktop publication broker approves and applies them.

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
| Workspace tools and staging | `executor/` | Bounded, network-disabled control plane: file tools, staging, checkpoints, baselines, and manifests; never publication. |
| Command execution | `runner/`, `docker/Dockerfile.runner` | Standard-library command server and the toolchain images Bash runs in. |
| Opt-in package egress | `egress/`, `docker/compose.egress.yaml` | Allowlisted CONNECT proxy for the install-capable profile; never loaded by default. |
| Protocol and registries | `shared/` | Framework-neutral structures; no desktop or gateway orchestration. |
| Tests | `tests/` | Follow the taxonomy in `TESTING.md`; security and container tests must remain credential-free. |

## Plans

Every living plan and the roadmap live in this directory, one authoritative copy each. Status is recorded per item inside each document; when a plan is fully implemented or superseded it moves to `archived-doc/` (non-normative) rather than being kept alongside a newer copy.

| Document | Scope |
|---|---|
| [ROADMAP.md](ROADMAP.md) | Completed, active, planned, deferred, and non-goal status for the product, including the open items carried from archived plans. |
| [AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md) | Phased plan to widen agent capability inside the Docker sandbox while narrowing exits. Phases 0–2 are implemented (loop fixes, token and cache efficiency for low-cost models, a separate runner workbench, durable staging); Phases 3–7 (long-run support, risk-proportional approvals, controlled connectivity, missions, and setup) are proposed. |

Documentation file names are predictable: `UPPER_SNAKE_CASE.md` in `docs/`, lowercase-hyphenated names in `archived-doc/`, and no spaces or embedded dates anywhere (dates belong in commit history or `CHANGELOG.md`). The repository root keeps only `README.md`, `AGENTS.md`, and `CHANGELOG.md`. `tests/structural/test_documentation_layout.py` enforces this.

The completed harness fix, harness quality-of-life, harness validation, organization, and code-simplification plans are archived in `archived-doc/` (indexed in its `README.md`).

## Local-only state

`.local-chat-checkpoints/` is a runtime artifact. It is ignored and must not be committed. The historical `archived-doc/` directory is non-normative.

## Checks

The required checks are listed once, in [TESTING.md](TESTING.md#required-checks).
