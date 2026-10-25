# Testing

This document owns the test tiers, the validator, the required checks, and the CI workflow rules. Other documents link here rather than restating them.

## Test levels

- **Unit** tests exercise one module or operation with temporary workspaces and fakes. They live in `tests/unit/<domain>/` (`desktop`, `egress`, `executor`, `gateway`, `publication`, `shared`).
- **Contract** tests pin documented protocols, schemas, tool names, modes, and limits.
- **Integration** tests cross the gateway, executor, staging, and publication boundaries.
- **Security** tests cover containment, link and device handling, privilege, network, and publication rules.
- **Structural** tests check static configuration, packaging, documentation, workflows, and QML invariants. They live in `tests/structural/`.
- **UI** tests (`tests/ui/`) load QML offscreen. **Docker** tests (`tests/docker/`) need a container runtime.

No test may need provider credentials or network access. Use fake providers, local ASGI transports, and temporary files. Every test must be discoverable through `testpaths`. Test file names are unique across folders, and no test lives directly in `tests/`.

## Running checks

`scripts/validate.py` is the canonical entry point. It uses the active interpreter, reports unavailable tools explicitly, and never forwards provider credentials to a child.

```text
python scripts/validate.py preflight [--require fast,docker] [--json report.json]
python scripts/validate.py collect   # diagnostic: pytest --collect-only, names import-time hangs
python scripts/validate.py fast      # not slow and not docker
python scripts/validate.py slow      # slow and not docker
python scripts/validate.py docker    # docker marker
python scripts/validate.py ruff      # ruff check and ruff format --check
python scripts/validate.py compile
python scripts/validate.py qml
python scripts/validate.py qml-smoke
python scripts/validate.py all       # every tier except collect
```

A bare `pytest` collects everything, including `slow` and `docker` tests. Use the validator, or a marker expression, to run one tier.

Each tier runs in a disposable environment: `HOME` and the XDG directories point at an empty per-run tree, and `PYTHON_KEYRING_BACKEND` is the null backend. Only the container fixtures (`LOCAL_CHAT_CI_ROOT`, `LOCAL_CHAT_SECRETS_DIR`, `LOCAL_CHAT_WORKSPACE`, the workspace id and key, the runner resource settings, `EUSTO_BUILDX_CACHE_DIR`) are forwarded from the host. Only the `qml` and `qml-smoke` tiers receive the Qt platform variables.

Preflight reports a tool as available when it is on `PATH`, even if its version probe is slow. Only a probe that exits non-zero is reported as failed. `--require` exits non-zero when a required tier's tool is unavailable; the workflows use this as their gate.

## Required checks

This is the one authoritative list of check commands; other documents link here. Install the locked toolchain first (`python scripts/bootstrap.py`, or `python -m pip install -r requirements-dev.lock` in a Python 3.12 virtual environment). `python scripts/validate.py all` runs every check below:

```text
pytest -m "not slow and not docker"
pytest -m "slow and not docker"
pytest -m docker
ruff check . && ruff format --check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
python scripts/validate.py qml-smoke
```

Run the checks that apply to a change, and report results verbatim. The Docker tier needs Linux with Docker and the fixtures from `scripts/docker-fixtures.sh`; it fails by name when they are missing rather than skipping. It is the one authoritative place for the rendered Compose hardening invariants (network mode, read-only root, `no-new-privileges`, capability and mount sets, no Docker socket).

## Deadlines

The innermost deadline always fires first:

```text
test (pytest --timeout 180 s) < tier deadline (validate.py) < step ceiling (workflow) < job ceiling
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

A command past its deadline has its whole process tree killed (`SIGTERM` then `SIGKILL` on POSIX, `taskkill /F /T` on Windows) and fails with `timed out after <n>s`. Commands get no stdin and print a progress line every 30 s. Tests run under a 180 s thread timeout, and `faulthandler` prints the stack of a test still running after 90 s. Do not raise the suite-wide timeout; a test that needs longer uses `pytest.mark.timeout` and says why.

## Workflows

- `Validate local gateway` (`.github/workflows/test-containers.yml`) runs one job per group: `fast` (collect, fast, ruff, compile), `slow` (collect, slow), `qml` (qml, qml-smoke), and `containers` (docker). A hang in one group cannot cancel the others. Branch protection must require these four job names.
- `Build Windows release` (`.github/workflows/release.yml`) runs the same tiers inside `build-windows`. It publishes only on a `v*` tag or a manual dispatch.
- Every validation step runs through `scripts/validation-step.sh`, which writes a log under `validation-logs/`, sends heartbeats, and uploads the logs as an artifact even on failure.
- GitHub lists a workflow only once its file is on the default branch. The build workflows also declare `pull_request` and branch `push` filters, and neither writes on those events.
- `tests/structural/test_workflow_syntax.py` and `test_workflow_budgets.py` enforce valid YAML and workflow keys, a `name`, the deadline arithmetic, the logging wrapper, and the absence of security assertions in YAML. Keep them passing before pushing.

## Policies

**Async tests** use explicit synchronization: injected `asyncio.Event`s, fake providers that signal lifecycle points, and terminal journal events. Do not coordinate with `asyncio.sleep` or fixed polling. Every rendezvous is bounded with `asyncio.wait_for(event.wait(), RENDEZVOUS_TIMEOUT_SECONDS)`. `tests/structural/test_async_bounds.py` rejects an unbounded wait.

**Bash tests** run twice: in-process (fast) and against a real `python -m runner` server (slow). Test node ids must stay under 200 characters, so put large payloads in parameters and give cases explicit `ids=`. `tests/structural/test_test_id_bounds.py` enforces this.

**Line endings:** shell scripts must be LF. `.gitattributes` enforces it for `*.sh` and `*.bash`, and `tests/structural/test_line_endings.py` rejects a carriage return in any POSIX shell script. A CRLF entrypoint fails the container at start.

**Platform markers:** on Windows, tests marked `posix` are skipped. They need a Linux container path, POSIX permission bits, `dir_fd`/`O_NOFOLLOW`, or process-group kills. Keep cross-platform assertions in their own test.

**Leak policy:** a test must end what it starts. `tests/conftest.py` tracks child processes and application `QThread`s, and fails a test that leaves one running. Start servers and threads in fixtures, stop them in teardown under a bounded wait, and signal the whole process group. `tests/ui/conftest.py` isolates the Qt tests from the host (offscreen platform, null keyring, stubbed Docker and runtime).

**Regression tests:** every bug fix adds one regression test named for the externally visible behavior. If an existing test covers the bug, update it instead of adding a duplicate.

**Documentation tests:** `tests/structural/test_documentation_contract.py` ties `docs/TOOLS.md`, `docs/LIMITS.md`, `README.md`, and `AGENTS.md` to the code's tool registry and limits. `test_documentation_layout.py` enforces file names, the index, and link resolution. Changing a documented value means updating the code, the doc, and the test together.
