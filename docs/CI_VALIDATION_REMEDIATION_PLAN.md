# CI and test-suite remediation plan

**Status:** implemented. All four phases landed together with the tests and documents they change (see [§12](#12-implementation-record)); the confirming Linux and Docker runs in [§7](#7-how-to-confirm-which-mechanism-is-biting-before-changing-code) are the one part that still has to be recorded from CI, because the platform they need was not available where the fixes were written.

**Scope:** the two GitHub Actions workflows (`Validate local gateway`, `Build Windows release`), the validation harness (`scripts/validate.py`, `scripts/validation-step.sh`), and the pytest suite they drive.

**Symptom being explained:** after a large repository refactor, the Linux `Validate local gateway` workflow repeatedly stalls and then fails. The reported case is the **Fast tests** step "getting stuck and not finishing" — the step produces no further output and the job is eventually cancelled with a generic maximum-execution-time message rather than a named failure.

**One-sentence diagnosis:** the Linux validation job is the only job in the repository with **no per-step ceiling, no per-tier deadline that can win the race, and no diagnostic capture**, so a tier that wedges for any reason is indistinguishable from a tier that is merely slow, and both end as an anonymous job cancellation at 25 minutes.

---

## 1. What the failure actually looks like

The repository has two independent validation paths, and only one of them was hardened:

| Property | `release.yml` → `build-windows` | `test-containers.yml` → `validation` |
|---|---|---|
| Per-tier `timeout-minutes` | yes (5/15/20/5/5/5/5) | **none** |
| `pytest --collect-only` before tiers | yes | **none** |
| Output through `scripts/validation-step.sh` | yes | **none** |
| Heartbeat during a stall | yes (20 s) | **none** |
| `validation-logs` artifact with `if: always()` | yes | **none** |
| Job ceiling | 60 min | **25 min** |
| Tier deadline can fire before the job dies | yes for `ruff`/`compile`/`qml`/`qml-smoke`, no for `fast`/`slow` | **no, for every tier** |

`docs/TESTING.md` ("Required checks") describes the left column in detail — one step per tier, a collect step, a heartbeat, a log artifact — and treats it as *the* CI behaviour. The Linux job, which is the primary gate on pull requests, implements none of it. That single asymmetry explains why an occasional hang is fatal and undiagnosable there and survivable in the release pipeline.

---

## 2. Root causes

Each cause below is independently sufficient to produce "stuck, then cancelled". They compound: the first makes the second invisible, and the third decides whether you see it at all.

### RC-1 — Deadline inversion: no tier deadline in the Linux job can ever fire (certain)

`scripts/validate.py` gives every command a hard deadline:

- `COMMAND_TIMEOUT_SECONDS = 1200` (20 min) for `fast`, `slow`, `ruff`, `compile`, `qml`
- `DOCKER_TIMEOUT_SECONDS = 3600` (60 min) for `docker`
- `QML_SMOKE_TIMEOUT_SECONDS = 300` (5 min) for `qml-smoke`

The Linux `validation` job has `timeout-minutes: 25` for **all** of `fast`, `slow`, `ruff`, `compile`, `qml`, and `qml-smoke`, plus checkout, Python setup, `apt-get install libegl1`, and `python scripts/bootstrap.py`. The sum of the documented per-command deadlines is over an hour inside a 25-minute ceiling, so:

- `fast` alone may legitimately consume 20 of the 25 minutes;
- after `fast`, `slow` starts with a 20-minute budget the job cannot honour;
- whichever tier is running at minute 25 is killed by GitHub, and the *harness's* careful `timed out after 1200s` report — the thing `test_a_hung_command_fails_its_tier_by_name_with_its_deadline` in `tests/structural/test_validation_harness.py` asserts — never happens.

That is precisely the reported shape: the *Fast tests* step is the one that runs at the boundary, and the job dies unnamed. The same inversion exists in `release.yml`, where the `fast` step allows 15 minutes against a 20-minute harness deadline: the step ceiling always wins, so the harness's timeout message is unreachable there too.

`docs/TESTING.md` claims "Each command has a hard deadline … so a hang is reported rather than left to stall the release job." At the job level on Linux, that claim is currently false.

### RC-2 — A timeout on Linux does not kill the command tree, and does not release the step's output (certain)

`_run_command` in `scripts/validate.py` starts the child with:

```python
process = subprocess.Popen(
    command,
    cwd=cwd,
    env=env,
    stdin=subprocess.DEVNULL,
    creationflags=CREATE_NO_WINDOW,
)
```

Three omissions matter:

1. **No `start_new_session=True`.** The child shares the step's process group, so there is no group handle to signal.
2. **`stdout`/`stderr` are not redirected.** They are inherited, i.e. the step's console stream.
3. **`_kill_tree` is Windows-only in practice.** It runs `taskkill /F /T`, then unconditionally falls through to `process.kill()`, which signals *only the direct child*.

The suite is full of processes that outlive their parent: `tests/unit/executor/test_bash.py` spawns a `python -m runner` socket server per test (with `stdin=DEVNULL` but **inherited stdout/stderr**), the runner spawns `bash -lc` grandchildren, and the docker tier spawns containers. When the deadline fires on Linux, those descendants survive, keep the pipe to the step's stdout open, and GitHub's runner keeps the step in progress while it waits for the stream to close — **even though pytest is already dead**. This is a second, independent route to "stuck, not finishing", and it is the reason a killed tier can hang the job for the full 25 minutes after the actual work stopped.

`docs/TESTING.md` documents `taskkill /T` explicitly, so the Windows branch is intentional; the POSIX branch is simply missing.

### RC-3 — The pytest deadline cannot interrupt the async tests that dominate the tier (confirmed mechanism; the specific wedged test needs one run)

`pyproject.toml` sets `--timeout=180 --timeout-method=thread`. The thread method raises an asynchronous exception in the main thread, and CPython delivers it only when that thread executes bytecode. Every gateway and executor test drives `asyncio.run(...)`; a task parked on an `asyncio.Event` that is never set sits in `epoll_wait`, so the exception is never delivered and **the 180 s deadline never fires**. `faulthandler_timeout = 90` will print the stack, which is useful — but printing a stack does not end the run. The tier then blocks until RC-1 kills the job.

The suite contains unbounded rendezvous points that would behave exactly this way if a refactor stopped setting one event:

| File | Unbounded wait |
|---|---|
| `tests/unit/gateway/test_gateway.py` | `await cancel_event.wait()` (:44), `await started.wait()` (:150), `await finished.wait()` (:152) |
| `tests/unit/executor/test_bash.py` | `await started.wait()` (:102) |
| `tests/unit/gateway/test_streaming_turns.py` | `await asyncio.sleep(10)` (:155) and fixed-duration polling (:123, :182) |
| `tests/unit/gateway/test_redaction_and_web.py` | `await asyncio.sleep(1)` (:310) |
| `tests/unit/gateway/test_parallel_tool_calls.py` | `await asyncio.sleep(DELAY)` (:39) |

`docs/TESTING.md` ("Async test policy") forbids exactly this ("Do not coordinate with `asyncio.sleep`, fixed-duration polling loops, or arbitrary timeout increases"), but nothing enforces it, and `.wait()` with no bound is not covered by any check at all. Note that `asyncio.sleep(10)` is bounded — it wastes ten seconds but cannot hang; a bare `.wait()` on an event nobody sets is unbounded. The dangerous ones are the three in `test_gateway.py` and the one in `test_bash.py`.

RC-3 is *the* mechanism that converts a refactor bug (an event no longer set) into an unbounded hang instead of a red test. It is also the reason the per-command deadline in `validate.py` is a backstop and not a fix: it fires at 20 minutes with no idea which test was stuck.

### RC-4 — The fast tier stopped being fast (certain)

Two refactor-era changes moved heavy machinery into the tier that runs on every push:

1. **Every Bash test now runs twice.** `tests/unit/executor/test_bash.py` declares
   `@pytest.fixture(autouse=True, params=["in-process", "runner"])`, so all ~40 Bash tests execute once in-process and once against a **real `python -m runner` Unix-socket server** started per test, with a 10 s startup poll, `stdin=DEVNULL`, inherited stdout, and cleanup that is `terminate()` + `wait(timeout=10)` with no process-group kill and no output redirection. Neither parameter set is marked `slow`, so all of it is in `fast`.

2. **UI modules that build the real desktop stack are not marked `slow`.** Only `test_transcript_qml.py`, `test_investigation_model_qml.py`, and `test_leave_dialog_qml.py` carry `pytestmark = pytest.mark.slow`. `tests/ui/test_desktop_bridge_surface.py`, `tests/ui/test_desktop_approval_policy.py`, and `tests/ui/test_privacy_and_transcript.py` do not, and they construct `DesktopBridge` → `RuntimeService` → `RuntimeManager`, which creates `QThread` workers, `QTimer` health checks, and gateway clients.

Consequence: the tier that is supposed to give feedback in minutes now starts dozens of sockets and threads, and its failure modes include process leaks and exit-time joins that have nothing to do with the code under test.

### RC-5 — Nothing verifies that a test left no work behind (certain)

`tests/ui/conftest.py` isolates the host (offscreen Qt, software backend, null keyring, no Docker discovery, no automatic runtime) but its autouse fixture ends with a bare `yield` — there is **no teardown assertion**. `tests/conftest.py` has no process, thread, or socket guard at all.

A `QThread` still running, or a `python -m runner` server that was never reaped, either aborts the interpreter or is joined during interpreter finalisation — *after* every test has reported "passed". That presents as "the tests finished and the step never ends", which is the third route to the reported symptom and the one most easily mistaken for an infrastructure problem.

### RC-6 — Nothing tests the workflows, so the hardening never propagated (certain)

`tests/structural/test_validation_harness.py` pins the harness (tier names, timeouts, environment, kill behaviour) and is genuinely good. But **there is no structural test over `.github/workflows/*.yml`**. `docs/TESTING.md` describes per-step timeouts, a collect step, and a log artifact as required behaviour, and no check fails when a workflow omits them. `release.yml` got the hardening; `test-containers.yml` silently did not, and nothing noticed. This is the durable cause of "repeated problems after a large refactor": the invariants live in prose.

### RC-7 — The heartbeat is invisible while a hang is happening (certain, smaller)

`scripts/validation-step.sh` writes heartbeats into `validation-logs/<name>.log` and `cat`s the file only **after** the command returns:

```bash
( while sleep 20; do echo "[heartbeat] ..."; done ) >> "$log" 2>&1 &
...
"$@" >> "$log" 2>&1
status=$?
...
cat "$log"
```

For a *hung* step, the console therefore shows nothing at all until the step ends or is killed — the heartbeat proves liveness only to a file that nobody is watching. Writing to `tee -a` (or `tail -f` in a sidecar) would make the live log show `[heartbeat] fast still running after 300s` while it is stuck, which is what an operator actually needs.

---

## 3. Other workflow defects

Found while auditing both workflows. None of these is the reported hang on its own, but several cause *different* intermittent failures, and two of them make the reported one worse.

| # | Defect | Evidence | Effect |
|---|---|---|---|
| W1 | `preflight` cannot fail | `validate.py:main` — `code = 0 if args.tier == "preflight" else execute(...)`; it returns the report, never a failing status | Both workflows use `preflight` as a gate, so a missing Docker daemon, PySide6, or `pyside6-qmllint` passes silently and fails later inside an unrelated tier |
| W2 | No `concurrency:` in either workflow, and `on: pull_request:` **plus** unfiltered `on: push:` | `test-containers.yml` header | Every push to a PR branch starts two full runs (×2 jobs); nothing is cancelled; the 75-minute container job queues behind the validation gate — indistinguishable from "stuck" |
| W3 | The security job is gated on the flaky job | `containers:` has `needs: validation` | One hang in `fast` cancels every non-root, egress, mount, and resource check |
| W4 | `build-windows` step ceilings sum to exactly its job ceiling | 5+15+20+5+5+5+5 = 60 = `timeout-minutes: 60`, before bootstrap, PyInstaller, `choco install innosetup`, archiving, and an 8 s packaged-app smoke test | Guaranteed cancellation whenever any pre-tier step runs long |
| W5 | `runtime-images` has no `timeout-minutes` | `release.yml` | Falls back to the 360-minute default |
| W6 | `docker/setup-buildx-action@v3` runs **after** three `build-push-action` steps that already pushed | `release.yml` | Works only because `build-push-action` creates its own builder; an ordering landmine, and SBOM/provenance apply to runner images only |
| W7 | Vulnerability scan pulls an image and mounts `docker.sock` in-job | `aquasec/trivy:0.67.2` + `-v /var/run/docker.sock:...` | Adds a network dependency and a daemon mount to the release path; fails for reasons unrelated to the release |
| W8 | `qml-smoke` step ceiling equals the harness deadline | `timeout-minutes: 5` vs `QML_SMOKE_TIMEOUT_SECONDS = 300` | The harness needs its 30 s reap window; GitHub always kills first, so the tier cannot report |
| W9 | Duplicated compile work | `validate.py compile` **and** `python -m compileall …` inside the "QML lint and compile" step | Doubles the work and drifts the file list in two places |
| W10 | Security assertions live in YAML | The `Compose policy rendering` step asserts `network_mode: none`, the exact runner volume set, `read_only`, `no-new-privileges`, and no `docker.sock` | The same invariants are already asserted in `tests/docker/test_runner_split.py`; any new compose service breaks the job with an `AssertionError` instead of a reviewed security finding. Violates "one authoritative copy" |
| W11 | Artifact uploads for files a failed step never created | `path: compose.yaml`, `validation-preflight.json`, `docker-preflight.json` | Warning noise on failure; also writes `compose.yaml` into the repository root |
| W12 | `AGENTS.md` tier list is stale | It lists `fast, slow, docker, ruff, compile, qml, all`; `validate.py` and `docs/TESTING.md` also have `qml-smoke`, and `tests/structural/test_validation_harness.py` pins it | Contributors following `AGENTS.md` omit a real tier |

---

## 4. Other test-suite defects

| # | Defect | Evidence | Effect |
|---|---|---|---|
| T1 | Docker tier cannot self-report | `tests/docker/test_runner_split.py:303` `@pytest.mark.timeout(3600)` on a test parametrized over **four** runner targets, each building an image with `timeout=3600` (:319); plus `timeout(1200)` (:179) and `timeout(1500)` (:240); `tests/docker/test_egress_profile.py:74` (900), `:157` (1200) | Per-test budgets sum to hours against a 3600 s tier deadline and a 75-minute job. Worst case is ~4 hours of builds inside a 60-minute harness budget |
| T2 | Slow tier is dominated by one 50,000-file benchmark | `tests/unit/executor/test_visible_files_cache.py:163` `@pytest.mark.timeout(600)`, plus `posix` and `slow` | Half of the slow tier's 1200 s budget goes to one test; on a cold Windows runner the remainder is tight |
| T3 | Environments are not hermetic | `validate.py:_safe_env` exports only `PATH`, `PYTHONPATH`, `PYTHONIOENCODING`, `PYTHONUNBUFFERED` (plus Qt for Qt tiers, plus `WINDOWS_ENV` on Windows) — no `HOME`/`XDG_DATA_HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`, and on Windows the **real** `USERPROFILE`/`LOCALAPPDATA` are passed through | On Linux `Path.home()` falls back to the passwd entry, so `src/settings.app_data_dir()` writes **real** state under the runner's home; tiers are not isolated from each other or the host; a future containerised runner with no passwd entry fails with `RuntimeError` inside `fast` |
| T4 | No async bound guard | Nothing checks that a test's `await` is bounded | A refactor that drops an event turns a failure into an unbounded hang (RC-3) |
| T5 | No leak guard | `tests/conftest.py` (whole file), `tests/ui/conftest.py` (bare `yield`) | QThread/process/socket leaks become exit-time hangs (RC-5) |
| T6 | Harness deadlines are pinned by name in a test | `test_a_hung_command_fails_its_tier_by_name_with_its_deadline` asserts `seen == [validate.COMMAND_TIMEOUT_SECONDS]` for `fast` | Any deadline change in this plan **must** update this test in the same change; otherwise the fix looks like a regression |
| T7 | Three copies of the compose hardening invariants | `test_container_policy.py`, `test_runner_split.py`, and workflow YAML | Drift and false reds |

---

## 5. Target deadline model

The rule to enforce, in one line: **the innermost deadline must always fire first, and every layer must have one.**

```text
test-level (pytest --timeout 180)  <  tier deadline (validate.py)  <  step ceiling (workflow)  <  job ceiling
```

Concrete targets. `harness ≤ step ceiling − 60 s` leaves the harness its kill-and-reap window; `Σ step ceilings ≤ job ceiling − 10 min` leaves room for checkout, setup, bootstrap, and uploads.

| Tier | Harness deadline | GH step ceiling | Notes |
|---|---|---|---|
| `preflight` | 60 s | 2 min | Must be able to fail (W1) |
| `collect` (`--collect-only`) | 180 s | 5 min | Names an import-time hang in seconds |
| `fast` | 900 s | 20 min | After RC-4 is fixed, expect 3–5 min |
| `slow` | 1200 s | 25 min | T2 must leave headroom |
| `ruff` / `compile` / `qml` | 300 s each | 10 min each | |
| `qml-smoke` | 300 s | 10 min | Was 5 (W8) |
| `docker` | 2700 s | 60 min | Only after T1 is fixed |

Job structure. **Option A (recommended):** four independent jobs — `fast` (collect + fast + ruff + compile: 45 min of ceilings → 60), `slow` (25 → 45), `qml` (`qml` + `qml-smoke`: 20 → 30), `containers` (60 → 90, no `needs`). This satisfies the invariant by construction, cuts wall-clock by running tiers in parallel, and stops one hang from cancelling the security tier. It renames the required status checks, so branch protection must be updated in the same change. **Option B (minimal):** keep one job, raise its ceiling to 110 min, add the seven step ceilings (90 min total). Preserves check names; keeps the pipeline serial and slow.

---

## 6. Remediation plan

Ordered so each phase makes the next failure *diagnosable* before it makes the suite *fast*, and so the last phase makes regression impossible. Every phase lands with the tests it touches, per `AGENTS.md` ("Change discipline").

### Phase 1 — Make a hang name itself (highest value, no behaviour change)

| Item | Change | Status |
|---|---|---|
| 1.1 | `validate.py::_run_command`: add `start_new_session=True` (POSIX) so the child owns a process group | Implemented |
| 1.2 | `validate.py::_kill_tree`: on POSIX, `os.killpg(os.getpgid(pid), SIGTERM)`, bounded wait, then `SIGKILL`; keep `taskkill /T` on Windows | Implemented |
| 1.3 | `validate.py::_run_command`: print a progress line every 30 s while `process.wait()` runs, so the log shows liveness | Implemented |
| 1.4 | `validation-step.sh`: send heartbeats to the console **and** the log (`tee -a`), so a live hang is visibly alive (RC-7) | Implemented |
| 1.5 | `test-containers.yml`: wrap every tier in `bash scripts/validation-step.sh <name> …` and give each a `timeout-minutes` from §5 | Implemented |
| 1.6 | `test-containers.yml`: add a `pytest --collect-only` step first | Implemented |
| 1.7 | `test-containers.yml`: upload `validation-logs` with `if: always()`, `if-no-files-found: ignore` | Implemented |
| 1.8 | `test-containers.yml`: raise the job ceiling per §5 (Option A or B); until then, at minimum raise it above the sum of step ceilings | Implemented |

Acceptance: a deliberately wedged tier (see §7.5) produces a named `timed out after <n>s` check entry, a log artifact containing the last test and the heartbeat trail, and **no surviving child processes** after the step.

### Phase 2 — Correct the workflow defects

| Item | Change | Status |
|---|---|---|
| 2.1 | `concurrency: {group: validation-${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true}`; restrict `push:` to `main` plus tags; keep `workflow_dispatch` (W2) | Implemented |
| 2.2 | Drop `needs: validation` from `containers` (W3) | Implemented |
| 2.3 | `preflight` gains `--require` (or returns non-zero when a requested tier's tools are unavailable) and both jobs use it (W1) | Implemented |
| 2.4 | Delete the duplicated `python -m compileall` from the QML step (W9) | Implemented |
| 2.5 | `release.yml`: `timeout-minutes` on `runtime-images` (90); move `setup-buildx` before the first build; `cache-from/to: type=gha` (W5, W6) | Implemented |
| 2.6 | Move the trivy scan out of the release job into a scheduled workflow pinned by digest (W7) | Implemented |
| 2.7 | Move the compose assertions out of the workflow into `tests/docker/test_runner_split.py`, and have the workflow only render and upload the config (W10) | Implemented |
| 2.8 | `if-no-files-found: ignore` on conditional artifacts; stop writing `compose.yaml` into the repository root (W11) | Implemented |
| 2.9 | Reconcile the tier list in `AGENTS.md` with `validate.py` / `docs/TESTING.md` (`qml-smoke`) and correct the deadline paragraph in `docs/TESTING.md` to the §5 table (W12) | Implemented |

### Phase 3 — Fix the suite

| Item | Change | Status |
|---|---|---|
| 3.1 | `tests/unit/executor/test_bash.py`: keep `in-process` in `fast`; mark the `runner` parameter `pytest.mark.slow`; redirect the server's `stdout`/`stderr` to `DEVNULL`; add `start_new_session=True`; replace cleanup with `killpg` → bounded wait → `SIGKILL` (RC-4) | Implemented |
| 3.2 | Bound every rendezvous: `await asyncio.wait_for(started.wait(), 5)` in `test_gateway.py` (:44, :150, :152) and `test_bash.py` (:102), asserting the event's cause, not merely its arrival (RC-3) | Implemented |
| 3.3 | Add `tests/structural/test_async_bounds.py`: fail on a bare `.wait()` or an `asyncio.sleep` with a duration ≥ 1 s in `tests/`, unless the test carries `pytest.mark.timeout` with a reason (T4) | Implemented |
| 3.4 | Add an autouse leak guard to `tests/conftest.py`: snapshot child PIDs and live `QThread`s before, assert the set has not grown after; convert `tests/ui/conftest.py`'s bare `yield` into an assertion that `RuntimeService` was shut down (RC-5, T5) | Implemented |
| 3.5 | Make `_safe_env` hermetic: per-run temp `HOME`/`XDG_*` (POSIX) and `USERPROFILE`/`LOCALAPPDATA`/`APPDATA` (Windows), plus `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` (T3) | Implemented |
| 3.6 | Docker tier: parametrize the four runner targets as a matrix with shared buildx cache; reduce in-test ceilings (3600 → 1200) below the tier deadline (T1) | Implemented |
| 3.7 | Update `test_a_hung_command_fails_its_tier_by_name_with_its_deadline` for the new per-tier deadlines (T6) | Implemented |
| 3.8 | Prune the duplicated compose assertions to one place (T7) | Implemented |

### Phase 4 — Prevent recurrence

`tests/structural/test_workflow_budgets.py`, written as a text scan (the dev lock has no PyYAML; the existing structural tests already scan files as text). Assert, for every file in `.github/workflows/`:

1. every step that invokes `scripts/validate.py` has a `timeout-minutes`;
2. every tier step runs through `scripts/validation-step.sh`;
3. a `pytest --collect-only` step precedes the tier steps;
4. `validation-logs` is uploaded with `if: always()`;
5. `concurrency.cancel-in-progress: true` is present;
6. each job's `timeout-minutes` ≥ the sum of its steps' `timeout-minutes` + 600 s;
7. each `COMMAND_TIMEOUTS` entry maps to a step ceiling satisfying `harness ≤ ceiling − 60 s`;
8. no `pytest.mark.timeout` in `tests/docker/` exceeds the docker tier deadline;
9. neither workflow contains security assertions (they belong in the docker tests).

This is the item that ends the pattern: as long as the invariants are prose, a refactor can drop them silently, which is exactly what happened.

---

## 7. How to confirm which mechanism is biting before changing code

Do this once, on Linux, with the harness's own environment, and record the result in this document. The workflow defects in §3 stand regardless; this only identifies *which* of RC-1…RC-5 is currently producing the stall.

1. **Baseline the tier and watch it end.**
   `python scripts/bootstrap.py && time python scripts/validate.py fast`
   Compare the total to the 20-minute deadline. A total under ~6 min after 3.1 means RC-4 was a major contributor.
2. **Name the wedged test.** Run the tier directly with a visible stack dump:
   `python -X faulthandler -m pytest -m "not slow and not docker" -v --timeout=180 -p no:cacheprovider > /tmp/fast.log 2>&1`, then tail the file. A dump ~90 s after the last started test names RC-3 and the test. A run that never reaches a test is an import-time problem (use step 4). A run that prints `==== N passed ====` and never returns is RC-5.
3. **Prove the threshold.** `python -m pytest --collect-only -q` under a 5-minute bound isolates collection.
4. **Prove the orphan behaviour (RC-2).** Start `python scripts/validate.py fast` in the background, kill the *parent* after a few seconds, then `ps -ef | grep -E 'python -m runner|/bin/bash -lc'`. Survivors confirm the POSIX kill gap and the pinned stdout.
5. **Prove the fix.** After Phase 1 + 3.2, deliberately break one event (make `SlowProvider`'s `cancel_event` never fire in `tests/unit/gateway/test_gateway.py`) and confirm the run **fails within 180 s naming that test** instead of stalling, and that `ps` shows no surviving `python -m runner`.

---

## 8. Verification after each phase

```bash
python scripts/bootstrap.py
python scripts/validate.py preflight --json /tmp/pre.json
python scripts/validate.py fast
python scripts/validate.py slow
python scripts/validate.py ruff
python scripts/validate.py compile
python scripts/validate.py qml
python scripts/validate.py qml-smoke
# Linux with Docker:
source scripts/docker-fixtures.sh && setup_fixtures && python scripts/validate.py docker
```

Plus, in CI, one run that intentionally wedges a tier and demonstrates the named timeout, the surviving `validation-logs` artifact, and a clean process table.

---

## 9. Risks and non-goals

- **Do not raise job ceilings as the fix.** Raising `validation` from 25 to 110 minutes without step ceilings and log capture only makes a hang more expensive. Ceilings are the outermost layer; they are not the diagnosis.
- **Do not mark the slow tests `slow` to hide them.** The Bash duplication (3.1) is a real cost worth keeping for the `runner` path — in the slow tier, where it cannot starve the feedback loop.
- **Do not add retries.** A retry on a hang converts a reproducible defect into an intermittent one and burns twice the budget.
- **Do not weaken the security assertions** while moving them out of YAML (2.7): delete a copy only once the authoritative test asserts the same invariants.
- **Non-goal:** changing the public tool surface, approval tiers, or any executor/runner sandbox property. This plan touches CI, the harness, and tests only.
- **Migration note:** Option A in §5 renames required status checks; update branch protection in the same change or merges will block on checks that no longer exist.

---

## 10. Ranked summary

| Rank | Cause | Certainty | Fix |
|---|---|---|---|
| 1 | The Linux job has no step ceilings, no log capture, and a 25-minute ceiling below the sum of tier deadlines (RC-1, RC-7) | certain | Phase 1 |
| 2 | `pytest-timeout`'s thread method cannot interrupt `epoll_wait`, and the suite has unbounded `Event.wait()`s (RC-3) | certain mechanism; test needs §7.2 | Phase 3 (3.2, 3.3) |
| 3 | A POSIX timeout kills only the direct child and inherits stdout, so orphans keep the step open (RC-2) | certain | Phase 1 (1.1–1.3) |
| 4 | The fast tier now starts real runner servers and QThreads, with no leak guard (RC-4, RC-5) | certain | Phase 3 (3.1, 3.4) |
| 5 | No structural test guards the workflows, so the hardening never reached `test-containers.yml` (RC-6) | certain | Phase 4 |
| 6 | Docker-tier per-test budgets exceed every enclosing deadline (T1) | certain | Phase 3 (3.6) |
| 7 | `preflight` cannot fail, duplicate work, no concurrency, `needs:` coupling, release-job budget arithmetic (W1–W12) | certain | Phase 2 |

---

## 11. Evidence index

| Claim | Source |
|---|---|
| `validation` job: `timeout-minutes: 25`, no step ceilings, no collect step, no log artifact | [test-containers.yml](../.github/workflows/test-containers.yml) |
| `containers` job: `needs: validation`, `timeout-minutes: 75` | [test-containers.yml](../.github/workflows/test-containers.yml) |
| Compose hardening asserted in YAML; `compose.yaml` written to the repository root | [test-containers.yml](../.github/workflows/test-containers.yml) |
| `build-windows` step ceilings sum to 60 = job ceiling; `validation-step.sh`, collect step, `validation-logs` present only here | [release.yml](../.github/workflows/release.yml) |
| `runtime-images` has no ceiling; `setup-buildx` after three pushes; trivy pulls an image and mounts `docker.sock` | [release.yml](../.github/workflows/release.yml) |
| `COMMAND_TIMEOUT_SECONDS`/`DOCKER_TIMEOUT_SECONDS`/`QML_SMOKE_TIMEOUT_SECONDS`; `_run_command` without `start_new_session` or output redirection; `_kill_tree` POSIX gap; `preflight` returns 0 | [validate.py](../scripts/validate.py) |
| Heartbeat written only to the log file; log printed only after the command ends | [validation-step.sh](../scripts/validation-step.sh) |
| 180 s thread timeout, `faulthandler_timeout = 90`, no cache provider | [pyproject.toml](../pyproject.toml) |
| Tier list, per-command deadlines, "each command has a hard deadline", release-job step layout, async test policy | [TESTING.md](TESTING.md) |
| Harness deadlines and tier names pinned by test; `fast` deadline asserted by name | [test_validation_harness.py](../tests/structural/test_validation_harness.py) |
| Doubled Bash backend starting a real socket server per test, inherited stdout | [test_bash.py](../tests/unit/executor/test_bash.py) |
| Unbounded `Event.wait()` rendezvous | [test_gateway.py](../tests/unit/gateway/test_gateway.py) |
| Four docker builds at `timeout(3600)`; 900/1200/1500 s per-test budgets | [test_runner_split.py](../tests/docker/test_runner_split.py) |
| 600 s benchmark in the slow tier | [test_visible_files_cache.py](../tests/unit/executor/test_visible_files_cache.py) |
| No suite-wide leak guard; host isolation without teardown assertion | [conftest.py](../tests/conftest.py), [tests/ui/conftest.py](../tests/ui/conftest.py) |
| Documented `docs/` naming and index rules followed by this file | [test_documentation_layout.py](../tests/structural/test_documentation_layout.py) |

## 12. Implementation record

The phases below are implemented in this change. Statuses in §6 read `Implemented`; each item
landed with the test that pins it, per `AGENTS.md` ("Change discipline").

**Phase 1 — a hang names itself**

- `scripts/validate.py` gives each command its own session on POSIX (`start_new_session`) and
  kills the process group (`SIGTERM`, a bounded grace, then `SIGKILL`) instead of only the direct
  child; `taskkill /F /T` still covers Windows. A progress line prints every 30 s while a command
  runs, so "still working" and "stuck" no longer look the same in a log.
- `scripts/validation-step.sh` sends the heartbeat through `tee -a`, so it reaches the console
  while the step is still running rather than a file nobody is watching.
- `Validate local gateway` wraps every tier in that wrapper, gives each step its own
  `timeout-minutes`, runs `pytest --collect-only` first, uploads `validation-logs-<job>` with
  `if: always()`, and runs as four independent jobs (`fast`, `slow`, `qml`, `containers`) so no
  tier's ceiling is below the sum of the harness deadlines.

**Phase 2 — workflow defects**

- `push` is restricted to `main` and tags; `workflow_dispatch` is kept; both workflows have a
  `concurrency` group with `cancel-in-progress: true`.
- `containers` no longer needs `validation`, so a hang in one tier cannot cancel the security
  tier.
- `preflight --require <tiers>` makes preflight a gate that can fail; both workflows use it.
- The duplicated `compileall` in the QML step is gone; the `compile` tier owns the file list.
- `release.yml`: `runtime-images` has a 90-minute ceiling, `setup-buildx` runs before the first
  push, every image build shares a `type=gha` cache, and `build-windows` keeps 28 minutes of room
  below its 92 minutes of step ceilings.
- The vulnerability scan moved to the scheduled `.github/workflows/vulnerability-scan.yml`,
  which scans the digest-pinned references from the newest release manifest, needs no Docker
  socket, and cannot block a release.
- The Compose assertions moved out of YAML into `tests/docker/test_runner_split.py`; the workflow
  only renders the configuration into `$RUNNER_TEMP` and uploads it. Conditional artifacts use
  `if-no-files-found: ignore`.
- The tier list in `AGENTS.md` matches `validate.py` (`collect`, `qml-smoke`) and the deadline
  paragraph in `docs/TESTING.md` is the §5 table.

**Phase 3 — the suite**

- `tests/unit/executor/test_bash.py`: the `runner` backend is `slow`, its server gets
  `start_new_session`, `stdout`/`stderr` to `DEVNULL`, and a group-kill teardown.
- Every rendezvous is bounded with `asyncio.wait_for` (`tests/unit/gateway/test_gateway.py`,
  `tests/unit/executor/test_bash.py`), and the slow provider asserts *why* it was cancelled.
- `tests/structural/test_async_bounds.py` fails on a bare `await ….wait()` or an unaudited
  `asyncio.sleep` of a second or more.
- `tests/conftest.py` gained process and `QThread` leak guards; `tests/ui/conftest.py` fails a
  test that leaves a `RuntimeService` un-shut-down.
- `_safe_env` is hermetic: a disposable `HOME`/`XDG_*` (and `USERPROFILE`/`LOCALAPPDATA`/
  `APPDATA` on Windows) plus the null keyring backend.
- The docker tier builds its four runner targets through one shared build cache and keeps every
  in-test ceiling below the 2700 s tier deadline.
- The duplicates the audit found are pruned: the compose hardening lives once (rendered, in
  `test_runner_split.py`) and the harness deadlines are asserted per tier.

One further defect surfaced while moving the compose assertions: `_safe_env` filtered the
`LOCAL_CHAT_*` fixture variables out of the pytest child, so every fixture-dependent container
check skipped itself and the docker tier reported a pass while running none of them. The harness
now forwards the fixture variables and the build-cache path (and nothing else from the host
environment), and the docker tier fails by name when the fixtures are missing.

**Phase 4 — prevention**

- `tests/structural/test_workflow_budgets.py` asserts all nine workflow invariants from §6, plus
  that the compile file list has one owner.

**Follow-up: workflows that never appeared**

Both workflows were reported missing from the Actions tab. The files themselves are valid YAML
and valid workflows (`tests/structural/test_workflow_syntax.py` now proves it on every run), but
two defects made them invisible or inert:

- `test-containers.yml` filtered `push` to `main` alone. The repository records no default-branch
  name, so on a repository whose default branch is `master` the workflow never ran after a merge,
  with no error anywhere. The filter now lists `main` and `master`.
- GitHub lists a workflow, and enables its *Run workflow* button, only once the file exists on the
  default branch. `vulnerability-scan.yml` is new and `test-containers.yml` gained four new job
  names, so both have to land on the default branch before they appear; until then a branch can
  only exercise them through `pull_request`. This is a GitHub rule, not a repository setting, and
  it is now documented in `docs/TESTING.md` ("Workflow visibility").

**Still to do in CI**

- Record the §7 confirmation runs (tier baseline, the forced wedge, the orphan check) once a
  Linux runner with Docker is available, and update §7 with the observed result.
- Update branch protection: §5 Option A was implemented, so the required status checks are now
  the four job names `fast`, `slow`, `qml`, and `containers` rather than `validation` and
  `containers`.
