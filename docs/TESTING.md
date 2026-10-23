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
python scripts/validate.py qml-smoke
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

`tests/ui/conftest.py` is the one place that isolates the Qt tests from the host: it sets the offscreen platform, the software Quick backend, and the null keyring backend (so no test reaches the Windows credential store), stubs `locate_docker` to `None` and the staged-session listing to an empty list, and refuses to start an automatic runtime. A UI test that reaches Docker, the credential store, or the app-controlled runtime blocks in a native call instead of failing, so keep new isolation there rather than in individual modules.

## Test timeouts

`pyproject.toml` gives every test 180 s with `--timeout-method=thread` (a thread timeout also interrupts a wait inside an extension module, which a signal would never reach) and prints the hung test's stack after 90 s through `faulthandler`. `-v` names the running test in the log, and `-p no:cacheprovider` keeps a tier independent of files an earlier run left in the checkout. A test that legitimately needs longer must say why and use `pytest.mark.timeout`; do not raise the suite-wide deadline.

Tests are placed by domain: `tests/unit/<domain>/` (`desktop`, `egress`, `executor`, `gateway`, `publication`, `shared`), `tests/ui/` for Qt tests, `tests/docker/` for container tests, and `tests/structural/` for structural and documentation checks. No test lives directly in `tests/`, and test file names are unique across folders (the folders are not packages); `tests/structural/test_documentation_layout.py` enforces both.

## Async test policy

Async tests use explicit synchronization. Prefer injected `asyncio.Event` objects, fake providers that signal lifecycle points, terminal journal events, and deterministic adapters. Do not coordinate with `asyncio.sleep`, fixed-duration polling loops, or arbitrary timeout increases. A real process such as `sleep 30` is acceptable when it is the subject of a cancellation or timeout test, but the test must synchronize on an observable start hook.

Tests must not require provider credentials or network access. Use fake providers, local ASGI transports, and temporary files.

## Regression and review policy

Every bug fix adds exactly one regression test named for the externally observable behavior that was broken. If an existing test already covers the bug, update that test rather than adding a duplicate. Stabilize tests locally before the final commit; intermediate `test:`/`fix:` commits are not merged. Review verifies this rule by judgment; CI verifies execution and correctness rather than commit messages or superficial test counts.

## Required checks

This is the one authoritative list of check commands; other documents link here. Install the locked toolchain first (`python scripts/bootstrap.py`, or `python -m pip install -r requirements-dev.lock` in a Python 3.12 virtual environment). `python scripts/validate.py all` runs every check below; the named tiers are `fast`, `slow`, `docker`, `ruff` (which also runs the format check), `compile`, `qml`, and `qml-smoke`:

```text
pytest -m "not slow and not docker"
pytest -m "slow and not docker"
pytest -m docker
ruff check . && ruff format --check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
python scripts/validate.py qml-smoke
```

Run the checks that apply to a change. The Docker tier needs Linux with Docker and the fixtures from `scripts/docker-fixtures.sh`; in CI it additionally runs the Compose hardening, non-root, mount, resource, and blocked-egress checks. `python scripts/validate.py preflight` reports missing tools as unavailable rather than failed. Each command has a hard deadline (20 minutes; 60 for `docker`, 5 for `qml-smoke`, which only has to start Qt and load `qml/Main.qml`): a command that exceeds it has its whole process tree killed (`taskkill /T` on Windows), is reaped with a bounded wait, and its tier fails with `timed out after <n>s`, so a hang is reported rather than left to stall the release job. Commands get no stdin and no console window, and only the `qml` and `qml-smoke` tiers receive `QT_QPA_PLATFORM`, `QT_QUICK_BACKEND`, and `QT_QUICK_CONTROLS_STYLE`; a Windows child also gets the OS variables (`SYSTEMROOT`, `APPDATA`, `ProgramFiles`, `VIRTUAL_ENV`, and the rest) it cannot invent.

The release job splits the checks into one step per tier (`collect`, `fast`, `slow`, `ruff`, `compile`, `qml`, `qml-smoke`), each with `shell: bash` and its own `timeout-minutes` (5 for `collect`, `ruff`, `compile`, `qml`, and `qml-smoke`, 15 for `fast`, 20 for `slow`) inside the job's 60-minute ceiling. A `pytest --collect-only` step runs first, so an import-time hang is named in seconds. Every step runs through `scripts/validation-step.sh`, which writes the command's output to `validation-logs/<name>.log`, appends a heartbeat every 20 s, prints the log when the command ends, and exits with the command's status; the workflow uploads the directory as the `validation-logs` artifact with `if: always()`. A stalled step therefore still leaves the tier name, the last test, and the heartbeat behind.

On Windows (the release job), tests marked `posix` are skipped: they need the executor's Linux container, either its `/bin/bash`, POSIX permission bits (`chmod` only toggles read-only on Windows), or POSIX `st_ctime` (creation time on Windows). Mark any new test with the same dependency the same way.
