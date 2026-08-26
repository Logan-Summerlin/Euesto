# Testing

Euesto tests public contracts at four levels:

- **Unit** tests exercise one module or operation with temporary workspaces and fakes.
- **Contract** tests protect documented protocols, schemas, tool names, modes, and limits.
- **Integration** tests exercise gateway, executor, staging, and publication boundaries.
- **Security** tests protect containment, link/device handling, privilege, network, and publication rules.
- **Structural** tests are reserved for static configuration, packaging, documentation, and visual QML invariants that cannot be exercised through a public runtime API. They must be named and reviewed as structural checks.

## Tiers and markers

The repository-owned validator is the canonical entry point. It uses the active Python interpreter, reports unavailable tools explicitly, and never forwards provider credentials.

```text
python scripts/validate.py preflight
python scripts/validate.py fast
python scripts/validate.py slow
python scripts/validate.py qml
python scripts/validate.py docker
python scripts/validate.py all
```

The default `pytest` command collects all tests; use the validator (or the explicit expressions below) for a selected tier. Tests are marked only when they genuinely need extra time or a container runtime; ordinary temporary-directory and subprocess tests are fast tests.

```text
pytest                         # fast: not slow and not docker
pytest -m "slow and not docker" # slow tests
pytest -m docker               # container/configuration tests
pytest -m "not docker"        # all non-container tests
pytest --co                   # inspect collection
```

Every test must be discoverable through `testpaths`; CI must use marker expressions, never curated file lists or test-count allowlists. New tests should be placed by domain under `tests/` (unit, integration, security, ui, or docker) as the suite is reorganized.

## Async test policy

Async tests use explicit synchronization. Prefer injected `asyncio.Event` objects, fake providers that signal lifecycle points, terminal journal events, and deterministic adapters. Do not coordinate with `asyncio.sleep`, fixed-duration polling loops, or arbitrary timeout increases. A real process such as `sleep 30` is acceptable when it is the subject of a cancellation or timeout test, but the test must synchronize on an observable start hook.

Tests must not require provider credentials or network access. Use fake providers, local ASGI transports, and temporary files.

## Regression and review policy

Every bug fix adds exactly one regression test named for the externally observable behavior that was broken. If an existing test already covers the bug, update that test rather than adding a duplicate. Stabilize tests locally before the final commit; intermediate `test:`/`fix:` commits are not merged. Review verifies this rule by judgment; CI verifies execution and correctness rather than commit messages or superficial test counts.

## Required checks

```text
pytest
pytest -m "slow and not docker"
pytest -m docker
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

The Docker tier additionally runs the Compose hardening, non-root, mount, resource, and blocked-egress checks in the container workflow.
