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
python scripts/validate.py docker
python scripts/validate.py ruff
python scripts/validate.py compile
python scripts/validate.py qml
python scripts/validate.py all
```

A bare `pytest` has no marker filter: it collects every test, including the `slow` and `docker` ones. Use the validator (or the explicit expressions below) to run one tier. Tests are marked only when they genuinely need extra time or a container runtime; ordinary temporary-directory and subprocess tests are fast tests.

```text
pytest -m "not slow and not docker"  # fast tier (validate.py fast)
pytest -m "slow and not docker"      # slow tier (validate.py slow)
pytest -m docker                     # container/configuration tier (validate.py docker)
pytest -m "not docker"               # all non-container tests
pytest --co                          # inspect collection
```

Qt Quick rendering tests (`tests/ui/test_transcript_qml.py`) are `slow`: they load QML through an offscreen `QQmlApplicationEngine` and skip when PySide6/Qt Quick cannot load (on Linux, install `libegl1`). Every test must be discoverable through `testpaths`; CI must use marker expressions, never curated file lists or test-count allowlists.

New tests are placed by domain: `tests/unit/<domain>/` (`desktop`, `egress`, `executor`, `gateway`, `publication`), `tests/ui/` for Qt tests, `tests/docker/` for container tests, and `tests/structural/` for structural checks. The remaining flat `tests/*.py` files move into these folders as they are touched; `integration/` and `security/` folders are created when their first test moves.

## Async test policy

Async tests use explicit synchronization. Prefer injected `asyncio.Event` objects, fake providers that signal lifecycle points, terminal journal events, and deterministic adapters. Do not coordinate with `asyncio.sleep`, fixed-duration polling loops, or arbitrary timeout increases. A real process such as `sleep 30` is acceptable when it is the subject of a cancellation or timeout test, but the test must synchronize on an observable start hook.

Tests must not require provider credentials or network access. Use fake providers, local ASGI transports, and temporary files.

## Regression and review policy

Every bug fix adds exactly one regression test named for the externally observable behavior that was broken. If an existing test already covers the bug, update that test rather than adding a duplicate. Stabilize tests locally before the final commit; intermediate `test:`/`fix:` commits are not merged. Review verifies this rule by judgment; CI verifies execution and correctness rather than commit messages or superficial test counts.

## Required checks

This is the one authoritative list of check commands; other documents link here. Install the locked toolchain first (`python scripts/bootstrap.py`, or `python -m pip install -r requirements-dev.lock` in a Python 3.12 virtual environment). `python scripts/validate.py all` runs every check below except the QML smoke test, and each of the first six lines is a named tier (`fast`, `slow`, `docker`, `ruff`, `compile`, `qml`):

```text
pytest -m "not slow and not docker"
pytest -m "slow and not docker"
pytest -m docker
ruff check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
python scripts/qml_smoke.py
```

Run the checks that apply to a change. The Docker tier needs Linux with Docker and the fixtures from `scripts/docker-fixtures.sh`; in CI it additionally runs the Compose hardening, non-root, mount, resource, and blocked-egress checks. `python scripts/validate.py preflight` reports missing tools as unavailable rather than failed.
