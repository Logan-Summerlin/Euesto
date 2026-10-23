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
python scripts/validate.py collect
python scripts/validate.py fast
python scripts/validate.py slow
python scripts/validate.py docker
python scripts/validate.py ruff
python scripts/validate.py compile
python scripts/validate.py qml
python scripts/validate.py qml-smoke
python scripts/validate.py all
```

`collect` is a diagnostic tier, not a check: it imports every test module (`pytest --collect-only`) so an import-time hang is named in seconds, and `all` does not run it.

A bare `pytest` has no marker filter: it collects every test, including the `slow` and `docker` ones. Use the validator (or the explicit expressions below) to run one tier. Tests are marked only when they genuinely need extra time or a container runtime; ordinary temporary-directory and subprocess tests are fast tests.

```text
pytest -m "not slow and not docker"  # fast tier (validate.py fast)
pytest -m "slow and not docker"      # slow tier (validate.py slow)
pytest -m docker                     # container/configuration tier (validate.py docker)
pytest -m "not docker"               # all non-container tests
pytest --co                          # inspect collection
```

Qt Quick rendering tests (`tests/ui/test_transcript_qml.py`) are `slow`: they load QML through an offscreen `QQmlApplicationEngine` and skip when PySide6/Qt Quick cannot load (on Linux, install `libegl1`). Every test must be discoverable through `testpaths`; CI must use marker expressions, never curated file lists or test-count allowlists.

The Bash tool's tests run twice: once in-process, which stays in the fast tier, and once against a real `python -m runner` command server, which is `slow`. Starting a socket server and `bash -lc` grandchildren per test is the cost that once made the fast tier start dozens of processes; keeping it in the slow tier preserves the coverage without starving the feedback loop.

`tests/ui/conftest.py` is the one place that isolates the Qt tests from the host: it sets the offscreen platform, the software Quick backend, and the null keyring backend (so no test reaches the Windows credential store), stubs `locate_docker` to `None` and the staged-session listing to an empty list, refuses to start an automatic runtime, and fails a test that leaves a started `RuntimeService` un-shut-down. A UI test that reaches Docker, the credential store, or the app-controlled runtime blocks in a native call instead of failing, so keep new isolation there rather than in individual modules.

## Test ids

A collected node id is echoed by every `-v` progress line, by each failure header, and by
`validate.py collect`, so it must stay short: `tests/conftest.py` refuses a collected id longer
than 200 characters and names the parametrization to fix. Keep the payload in the parameters and
give the case an explicit `ids=`, as `tests/unit/executor/test_runner_protocol.py`,
`tests/unit/executor/test_output_reduction.py`, and `tests/unit/gateway/test_redaction_and_web.py`
do. A parametrization over a captured transcript or a request body otherwise writes that whole
payload into the tier's log, so the fast and slow logs show parameters instead of the failure, and
one oversized case (a four-megabyte request body) is enough to bury the report and to repeat it in
`--collect-only`. `tests/structural/test_test_id_bounds.py` covers the refusal.

## Test timeouts

`pyproject.toml` gives every test 180 s with `--timeout-method=thread` (a thread timeout also interrupts a wait inside an extension module, which a signal would never reach) and prints the hung test's stack after 90 s through `faulthandler`. `-v` names the running test in the log, and `-p no:cacheprovider` keeps a tier independent of files an earlier run left in the checkout. A test that legitimately needs longer must say why and use `pytest.mark.timeout`; do not raise the suite-wide deadline. No ceiling in `tests/docker/` may reach the docker tier's own deadline; `tests/structural/test_workflow_budgets.py` checks it.

Tests are placed by domain: `tests/unit/<domain>/` (`desktop`, `egress`, `executor`, `gateway`, `publication`, `shared`), `tests/ui/` for Qt tests, `tests/docker/` for container tests, and `tests/structural/` for structural and documentation checks. No test lives directly in `tests/`, and test file names are unique across folders (the folders are not packages); `tests/structural/test_documentation_layout.py` enforces both.

## Async test policy

Async tests use explicit synchronization. Prefer injected `asyncio.Event` objects, fake providers that signal lifecycle points, terminal journal events, and deterministic adapters. Do not coordinate with `asyncio.sleep`, fixed-duration polling loops, or arbitrary timeout increases. A real process such as `sleep 30` is acceptable when it is the subject of a cancellation or timeout test, but the test must synchronize on an observable start hook.

Every rendezvous is bounded. `pytest-timeout`'s thread method raises inside the test's thread, and CPython delivers that only while the thread runs bytecode: a task parked in `epoll_wait` on an event nobody sets never reaches the deadline, so an unbounded `await event.wait()` turns a broken refactor into a hung tier instead of a red test. Use `await asyncio.wait_for(event.wait(), RENDEZVOUS_TIMEOUT_SECONDS)` and assert what the event means, not merely that it fired. `tests/structural/test_async_bounds.py` fails on a bare `await ….wait()` and on an `asyncio.sleep` of a second or more that is not in its audited list of sleeps-are-the-subject tests; adding one requires a review of that list.

Tests must not require provider credentials or network access. Use fake providers, local ASGI transports, and temporary files.

`python scripts/validate.py <tier>` runs each tier in a disposable environment: `HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and `XDG_CACHE_HOME` (and, on Windows, `USERPROFILE`, `LOCALAPPDATA`, and `APPDATA`) point at one empty tree per run, so no tier writes real state under the runner's home, and `PYTHON_KEYRING_BACKEND` is the null backend, so no test can reach a host credential store. Only the container-tier fixtures (`LOCAL_CHAT_CI_ROOT`, `LOCAL_CHAT_SECRETS_DIR`, `LOCAL_CHAT_WORKSPACE`, the workspace id and key, the runner resource settings, and `EUSTO_BUILDX_CACHE_DIR`) are forwarded from the host, because the docker tier reads them; everything else, credentials included, stays out of a validation child.

## Leak policy

A test must end what it starts. `tests/conftest.py` tracks every child process a test launches and every `QThread` the application's own subclasses start, waits briefly for a just-finished one, and fails the test (after reaping the leak) if anything is still running. A leaked child keeps the step's output open and a live `QThread` is joined during interpreter finalisation, *after* every test has reported "passed"; both present as "the tests finished and the step never ends". Start a server or a thread in a fixture and stop it in the fixture's teardown, under a bounded wait, and signal the whole process group when a server has descendants of its own.

## Regression and review policy

Every bug fix adds exactly one regression test named for the externally observable behavior that was broken. If an existing test already covers the bug, update that test rather than adding a duplicate. Stabilize tests locally before the final commit; intermediate `test:`/`fix:` commits are not merged. Review verifies this rule by judgment; CI verifies execution and correctness rather than commit messages or superficial test counts.

## Required checks

This is the one authoritative list of check commands; other documents link here. Install the locked toolchain first (`python scripts/bootstrap.py`, or `python -m pip install -r requirements-dev.lock` in a Python 3.12 virtual environment). `python scripts/validate.py all` runs every check below; the named tiers are `collect` (diagnostic), `fast`, `slow`, `docker`, `ruff` (which also runs the format check), `compile`, `qml`, and `qml-smoke`:

```text
pytest -m "not slow and not docker"
pytest -m "slow and not docker"
pytest -m docker
ruff check . && ruff format --check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
python scripts/validate.py qml-smoke
```

Run the checks that apply to a change. The Docker tier needs Linux with Docker and the fixtures from `scripts/docker-fixtures.sh` (it fails by name when they are missing rather than skipping every check, and the fixture variables are the only host environment it receives); in CI it additionally runs the non-root, mount, resource, and blocked-egress checks, and it is the one authoritative place for the rendered Compose hardening invariants (network mode, read-only root, no-new-privileges, capability and mount sets, no Docker socket) — the workflow only renders the configuration and uploads it as `container-compose-report`. `python scripts/validate.py preflight` reports missing tools as unavailable rather than failed; `python scripts/validate.py preflight --require fast,docker` exits non-zero when a tool that tier needs is unavailable, which is what the workflows use as their gate.

### Deadlines

The innermost deadline always fires first, and every layer has one:

```text
test-level (pytest --timeout 180 s)  <  tier deadline (validate.py)  <  step ceiling (workflow)  <  job ceiling
```

| Tier | Harness deadline | Workflow step ceiling |
|---|---|---|
| `preflight` | 60 s | 2 min |
| `collect` | 180 s | 5 min |
| `fast` | 900 s | 20 min |
| `slow` | 1200 s | 25 min |
| `ruff` / `compile` / `qml` | 300 s each | 10 min each |
| `qml-smoke` | 300 s | 10 min |
| `docker` | 2700 s | 60 min |

A command that exceeds its deadline has its whole process tree killed and is reaped with a bounded wait, and its tier fails with `timed out after <n>s`. On POSIX the command gets its own session (`start_new_session`) and is killed as a group with `SIGTERM`, then `SIGKILL`; on Windows `taskkill /F /T` does the same. Without the group kill the runner servers and `bash -lc` grandchildren a test starts survive the deadline and keep the step's output open, so the step stays "in progress" after pytest is gone. Commands get no stdin and no console window, print a `[progress]` line every 30 s so a live-but-slow command is visibly alive, and only the `qml` and `qml-smoke` tiers receive `QT_QPA_PLATFORM`, `QT_QUICK_BACKEND`, and `QT_QUICK_CONTROLS_STYLE`; a Windows child also gets the OS variables (`SYSTEMROOT`, `APPDATA`, `ProgramFiles`, `VIRTUAL_ENV`, and the rest) it cannot invent.

Every step that runs the validator also has a `timeout-minutes`, and each step ceiling leaves the harness at least a minute to kill, reap, and report. Each job's ceiling leaves ten minutes below the sum of its step ceilings for checkout, bootstrap, and artifact uploads.

### Workflows

`Validate local gateway` (`.github/workflows/test-containers.yml`) runs one job per tier group — `fast` (collect, fast, ruff, compile), `slow` (collect, slow), `qml` (qml, qml-smoke), and `containers` (the docker tier) — so a hang in one tier cannot cancel the others, and the security tier does not wait behind the validation gate. Those four job names are the required status checks; branch protection must list them. `Build Windows release` runs the same tiers as one step each inside `build-windows`. Both wrappers behave the same way:

- a `pytest --collect-only` step (`validate.py collect`) runs before the tiers in every job that runs pytest, so an import-time hang is named in seconds;
- every step runs through `scripts/validation-step.sh`, which writes the command's output to `validation-logs/<name>.log`, sends a heartbeat every 20 s to the console *and* the log, prints the log when the command ends, and exits with the command's status;
- the workflow uploads the directory as a `validation-logs-<job>` artifact with `if: always()`, and conditional artifacts use `if-no-files-found: ignore`.

A stalled step therefore still leaves the tier name, the last test, the `[progress]` line, and the heartbeat trail behind. `tests/structural/test_workflow_budgets.py` enforces all of it — step ceilings, the logging wrapper, the collect step, the log artifact, the concurrency group, the job/step budget arithmetic, the harness-to-ceiling margin, the docker per-test ceilings, and the absence of security assertions in YAML — so a workflow cannot silently lose the hardening again.

### Workflow visibility

GitHub only lists a workflow in the Actions tab, and only offers it to `workflow_dispatch`, once the workflow file exists on the repository's **default branch**. A new workflow file therefore looks "missing" until it is merged; on a branch it can only be exercised by its `pull_request` trigger. Two further requirements fail *silently* — the workflow disappears from the list instead of failing — and `tests/structural/test_workflow_syntax.py` proves both before a push:

- the file must be valid YAML and a valid workflow (known keys only, `runs-on` on every job, exactly one of `uses`/`run` per step, integer ceilings, `needs` naming real jobs), and it must have a `name` so the Actions tab can list it;
- a `push` filter must name the default branch. This repository does not record which branch that is, so the filter lists both `main` and `master`; a filter that matches neither stops validation after every merge with no error anywhere.

Every workflow also carries `workflow_dispatch`, so a maintainer can always start one by hand once it is on the default branch.

On Windows (the release job), tests marked `posix` are skipped: they need the executor's Linux container, either its `/bin/bash`, POSIX permission bits (`chmod` only toggles read-only on Windows), or POSIX `st_ctime` (creation time on Windows). Mark any new test with the same dependency the same way.
