# Changelog

## Unreleased — CI validation hardening

Implements all four phases of [docs/CI_VALIDATION_REMEDIATION_PLAN.md](docs/CI_VALIDATION_REMEDIATION_PLAN.md). The public tool surface, approval tiers, and the executor/runner sandbox are unchanged: this is CI, the validation harness, and the test suite only.

**A hang names itself (Phase 1)**

- Every tier has its own deadline (`preflight` 60 s, `collect` 180 s, `fast` 900 s, `slow` 1200 s, `ruff`/`compile`/`qml` 300 s, `qml-smoke` 300 s, `docker` 2700 s) below its workflow step ceiling, so the harness's `timed out after <n>s` report fires before GitHub kills the step.
- A validation command now runs in its own session on POSIX and is killed as a process group (`SIGTERM`, a bounded grace, `SIGKILL`), so the runner servers and `bash -lc` grandchildren a test starts cannot outlive the deadline and keep the step's output open. Windows keeps `taskkill /F /T`.
- A `[progress]` line prints every 30 s while a command runs, and `scripts/validation-step.sh` sends its heartbeat to the console as well as `validation-logs/<name>.log`, so a live-but-slow step is visibly alive.
- `Validate local gateway` wraps every tier in that script with its own `timeout-minutes`, runs `validate.py collect` first, uploads `validation-logs-<job>` with `if: always()`, and runs as four independent jobs (`fast`, `slow`, `qml`, `containers`).

**Workflow fixes (Phase 2)**

- `push` is limited to `main` and tags, `workflow_dispatch` is kept, and both workflows cancel superseded runs through a `concurrency` group.
- The container security job no longer needs the validation job, so a hang in one tier cannot cancel the non-root, egress, mount, and resource checks.
- `validate.py preflight --require <tiers>` exits non-zero when a tier's tools are unavailable; both workflows use it as a gate.
- The duplicated `compileall` in the QML step is gone, and the Compose hardening assertions moved out of workflow YAML into `tests/docker/test_runner_split.py` (the workflow renders the configuration and uploads it).
- The release job gets a `runtime-images` ceiling, a builder created before the first push, and shared `type=gha` build caches; `build-windows` keeps 28 minutes of room below its 92 minutes of step ceilings.
- The vulnerability scan moved to a scheduled workflow that scans the digest-pinned references from the newest release manifest, with no Docker socket and no network dependency in the release path.

**Suite fixes (Phase 3)**

- The Bash tool's runner-backed tests are `slow` and no longer start a socket server in the fast tier; their server gets its own session, muted output, and a group-kill teardown.
- Unbounded `await event.wait()` rendezvous are bounded with `asyncio.wait_for`, and `tests/structural/test_async_bounds.py` fails on a new one or on an unaudited `asyncio.sleep` of a second or more.
- `tests/conftest.py` fails a test that leaves a child process or a QThread running (after reaping it), and `tests/ui/conftest.py` fails a test that leaves a `RuntimeService` un-shut-down.
- Validation children run with a disposable `HOME`/`XDG_*` (`USERPROFILE`/`LOCALAPPDATA`/`APPDATA` on Windows) and the null keyring backend, so a tier cannot write real state or reach a host credential store.
- The docker tier builds its four runner targets through one shared cache and keeps every in-test ceiling below the tier deadline.
- The docker tier finally receives its fixtures: the harness forwards only the fixture variables and the build-cache path, and it fails by name when the fixtures are missing instead of skipping every container check as a pass.

**Prevention (Phase 4)**

- `tests/structural/test_workflow_syntax.py` parses every workflow and checks the part of GitHub's schema that decides whether a workflow is listed and dispatchable: valid YAML, known keys, complete jobs and steps, a name, a `workflow_dispatch` trigger, and a `push` filter that covers the default branch.
- The validation workflow's `push` filter now names both `main` and `master`: a filter that misses the repository's default branch silently stops the workflow after every merge.
- `tests/structural/test_workflow_budgets.py` enforces the workflow invariants: a step ceiling and the logging wrapper on every validator step, a collect step before the pytest tiers, an unconditional `validation-logs` upload, a concurrency group, the job-below-step-sum budget, the harness-to-ceiling margin, docker per-test ceilings, no security assertions in YAML, and one owner for the compile file list.

**Follow-up: the scan failed on an older manifest schema**

The first run of `Scan runtime images` failed in "Scan the digest-pinned images" with `KeyError: 'egress_proxy'`. The step read the manifest keys it expected to exist, but it scans the manifest of the newest *successful* `Build Windows release`, which is older than this checkout's schema (schema 2 shipped before the egress proxy image existed), so the key was simply absent and no image was scanned. The reference list now comes from `scripts/release_image_refs.py`: it reads the image keys from `schema_version` (schemas 1–3), scans an older release for the images it does declare, refuses a known schema that has lost a key it declares, and refuses a schema newer than it knows by name instead of skipping the new image. `tests/structural/test_release_image_refs.py` covers each schema and keeps the workflow from reading manifest keys inline again.

**Follow-up: a parametrized payload filled the fast and slow logs**

Two tests parametrized over their own fixtures — every parser transcript in `test_output_reduction.py` and every request body in `test_runner_protocol.py` — generated node ids the size of the payload: 676 characters per transcript, 2,616 for the 200-variable envelope, and 4,000,001 for the oversized request. pytest repeats a node id in every `-v` line, in each failure header, and in `validate.py collect`, so those tiers logged parameters rather than results, and one case alone could bury the report. Both parametrizations now pass explicit, bounded `ids=`; `tests/conftest.py` refuses any collected id over 200 characters (the longest legitimate one is 161) with a message that names the offender and the fix, and `tests/structural/test_test_id_bounds.py` covers that refusal.

## Unreleased — Agentic harness Phases 3–5

Implements Phases 3–5 of [docs/AGENTIC_HARNESS_PLAN.md](docs/AGENTIC_HARNESS_PLAN.md). The public surface grows to thirteen model-facing tools: `update_plan` (Plan and Agent), `web_search` and `fetch_url` (Agent, off by default per workspace). The `accept_edits` approval tier is replaced by `sandbox` with no alias. The security boundary is unchanged for the executor and runner; a container gains network only through an approved setup run or a `registries` session, and the workspace policy file is read from the host only.

**Long runs (Phase 3)**

- Agent turns stream; tool calls are assembled from the deltas, with an idle timeout and a per-turn ceiling instead of a fixed request timeout. Live text is shown while a turn runs and is not saved as the answer.
- `update_plan` keeps a per-session checklist (up to 20 steps) shown above the composer and carried into handoffs and wind-downs.
- At 85% of a budget the agent is asked to wrap up; a run that exhausts its budget (or stops making progress) gets a tool-free summary and pauses resumably. *Continue with another budget* resumes it.
- Loop resilience: corrective results for bad arguments and unknown tools, a change-approach note after repeated failures, one re-prompt for an empty reply, and a wind-down after four turns without progress.
- Declared checks (`[checks]` in the workspace policy) run in the runner from the review pane, tied to the staged snapshot. Auto publication waits for the checks named in `verify_before_publish`.
- `investigate_repository` is available in Plan too, and its nested loop can review a staged diff with `status`.

**Approvals in proportion to risk (Phase 4)**

- The Sandbox tier runs `write`, `edit`, `apply_patch`, and `bash` without prompts while the runner has no network. It is the default for new Agent sessions after a one-time explanation; every publication is still reviewed.
- Saved command rules match a single simple command by executable and argument prefix; compound commands never match an allow or ask rule. A rules screen manages them.
- Publication review flags host-executable, dependency, policy, binary, and large-deletion changes. Auto stops for confirmation at a flagged batch.
- The review pane publishes a selected subset of staged files; unselected changes stay staged.
- Secret-like files are refused in staging. High-confidence secrets are redacted from every tool result before the provider sees it, and publication warns about secrets it would write.
- `.euesto/policy.toml` in the host workspace declares the runner image, network mode, setup command, checks, extra secret patterns, and never-published paths. It takes effect only after confirmation; invalid files fail closed.
- Approval prompts show highlighted Bash commands with their purpose, directory, timeout, and network status, and URL prompts show the full URL.

**Controlled connectivity (Phase 5)**

- Dependencies install in an approved, one-shot setup container attached only to the allowlisted registry proxy. Isolated setup sees only the manifests and lockfiles; the agent's runner stays offline afterwards.
- The release manifest is schema 3 and pins the egress proxy image by digest; the proxy's audit log is viewable in Settings.
- A session can opt into `registries` network mode for its runner when the workspace policy allows it. Bash asks before every command in that mode.
- `web_search` and `fetch_url` give the model documentation access through the gateway, never through the executor or runner. `fetch_url` fetches allowlisted documentation hosts and asks for any other URL.
- `LOCAL_CHAT_EXECUTOR_EGRESS` is removed.

**Release validation (Windows)**

- The release job runs `pytest --collect-only` and then one step per validation tier, each `shell: bash` with its own `timeout-minutes`. Every step runs through `scripts/validation-step.sh`, which writes to `validation-logs/<name>.log`, appends a heartbeat every 20 s, prints the log when the command ends, and exits with its status; the artifact is uploaded even when the job fails.
- `scripts/qml_smoke.py` loads `qml/Main.qml` against the real bridge (temporary database, null keyring, no Docker), fails on a QML `ReferenceError`/`TypeError` instead of ignoring it, and is bounded by faulthandler, a 30 s quit timer, and the new `validate.py qml-smoke` tier (300 s). It leaves through `os._exit` so Qt teardown cannot wedge it.
- pytest names each test (`-v`), interrupts a hung test after 180 s with the thread method, and dumps its stack after 90 s. The validator gives a command no stdin and no console window, kills its whole process tree on a deadline, reaps it with a bounded wait, stops leaking `QT_QPA_PLATFORM` to non-Qt children, and passes Windows the OS variables plus `PYTHONUNBUFFERED`.
- Qt tests take their host isolation from `tests/ui/conftest.py`: the null keyring backend for every module, no Docker discovery, no staged-session listing, and no automatic runtime.

## Agentic harness Phases 0–2

Implements Phases 0–2 of [docs/AGENTIC_HARNESS_PLAN.md](docs/AGENTIC_HARNESS_PLAN.md). The ten-tool contract and the manifest, approval, and hash-validation protocol are unchanged. The security boundary is narrower: commands no longer run where the source mount is.

**Agent loop (Phase 0)**

- Each tool result is bounded on its own, sized by budget profile, instead of a cumulative 512,000-byte allowance that starved long runs; shortened results emit `tool.result_bounded`.
- A gateway-owned, versioned static prompt per mode, written for low-cost models; saved workspace instructions now reach the model.
- Provider errors are retried up to three times with backoff; a run that still fails pauses as resumable (`provider_error`) instead of failing.
- Saved Bash rules are scoped to their tokenized command. A migration disables broad legacy rules and shows a one-time notice. The Auto-mode clean-staging check works over HTTP, `read` honors the configured limit, and the dev lock matches the gateway image's Starlette and uvicorn.
- Closing the app or changing workspace with staged work offers *Review and publish*, *Discard*, *Keep for later*, or *Cancel*; staged work is never destroyed silently.
- A symbolic link created in staging is listed as an unpublishable link instead of wedging `status`, `write`, `bash`, and publication.
- Narration between tool calls appears in the transcript; a live meter shows cost, cache hit rate, context against the working set, tool calls, and time; *Steer* (Ctrl+Enter) sends guidance to the running agent before its next turn.
- For Plan and Agent, the model picker lists only tool-capable models, shows input, cached-input, and output prices, and warns about small windows and missing cached prices. The offline defaults are tool- and cache-capable.

**Efficiency for low-cost models (Phase 1)**

- Per-turn usage and cache accounting with miss causes and a `cache.unexpected_miss` alarm; run summaries show the cache hit rate.
- A cache-stable layout: static prompt, tool schemas, a once-per-session context (instructions, bounded `AGENTS.md`, skills, workspace snapshot), append-only history, and run notes. OpenRouter `session_id` for sticky routing, with cache breakpoints only for providers that need them.
- Compaction is a rare, planned event: a model-written handoff at a high-water mark, then append-only again down from a low-water mark.
- Lean plain-text results: `read` defaults to 32,000 bytes with an outline of the unshown remainder; mutations return short numbered snippets; the gateway fills in `expected_sha256` and replaces unchanged re-reads with a one-line note.
- Full command output is kept as a log in `.euesto-output/` (searchable through `bash`, never published), and test, lint, and compiler output is reduced to totals and first failures (pytest, unittest, Jest, Vitest, Go, Cargo, tsc, ESLint, Ruff, mypy, gcc/clang).
- Parallel tool calls, reasoning effort, a 16,000-token output cap with a corrective note on length truncation, and a structural test that bounds the static prefix.

**The workbench (Phase 2)**

- Bash runs in a separate runner container that has no network, no source mount, no secrets, no state volume, and no executor credential. The executor becomes the control plane: file tools, staging, checkpoints, baselines, and manifests, with 1 CPU, 1 GB, and 128 processes.
- Four digest-pinned runner images (`runner-base`, `runner-python`, `runner-node`, `runner-polyglot`), auto-detected from project files or chosen in Settings, published with SBOMs, provenance, and a vulnerability report. `/work` allows exec.
- Staging is persistent and disposable per workspace (`euesto-{work,state,cache}-<key>` volumes kept across restarts). Resuming rebases onto host edits; files changed on both sides are held as conflicts and resolved per file. Kept work can be published from a later session, and Settings lists staged sessions with their size and age.
- Sandbox git: a credential-free, hook-free clone with configurable history depth (200 commits by default), so `git diff euesto/base` shows exactly the agent's changes.
- Runner CPUs, memory, and process limits are settings within ceilings that fail closed.

**Upgrade notes**

- Staging that existed only in a running executor's memory-backed `/work` is not migrated; publish or discard it before upgrading.
- The egress overlay now attaches the runner, not the executor.
- Saved Bash rules created before this release are disabled; save them again from an approval prompt.

## Unreleased — Second simplification pass

A further KISS/YAGNI pass over every layer, the tests, and the documentation. The ten-tool contract, model-facing prompts and result shapes, the publication protocol, and the security boundary are unchanged.

**Fixes**

- Gateway errors on streamed requests (chat streams, agent events) are reported again; the client decoded the unread body and raised `httpx.ResponseNotRead` instead.
- "Review staged changes" no longer always reports 0 changes; it reports whether staging differs from the workspace baseline and the staged snapshot's size.
- A resumed agent run sends the model one, refreshed, `LOCAL EXECUTOR CONTEXT` message instead of the saved one plus a new one.
- Skills that need `status` or `investigate_repository` are rejected in Plan mode, matching the Plan tool set; the executor-accepted `read` `offset` and `find`/`ls` `cursor` arguments are now declared in the tool schemas.
- The run status line, transcript metadata, and usage dialog word token and cost totals the same way (`1,234 tokens · 5 cached · $0.001200`).

**Removed** (each was unreachable or only ever sent its default)

- Per-request agent budget fields (`max_iterations`, `max_tool_calls`, `max_wall_seconds`, `max_cost`); budgets come only from the selected profile. A run paused before this change cannot be resumed after it; start it again.
- Model aliases (no UI could create one); existing databases keep an empty `model_aliases` table.
- `WorkspaceBroker.undo`, the permission-rule `last_used_at`/`rule_used` path, thirteen event types nothing emitted, the gateway inspection's duplicate `total_known`, and bridge members QML never used (`approvalPolicy`, `usage`, `saveModelAlias`, `checkGateway`, `resumableRuns`).

**Simplification**

- **Shared:** one executor tool set (`EXECUTOR_TOOLS`) and an explicit `READ_TOOLS` replace single-member sets and an alias; one relative-path normalizer; `GatewayStatus.serves_workspace` is the single readiness check for both the runtime worker and the desktop.
- **Executor:** one `checkpointed` context manager gives `write`, `edit`, `apply_patch`, and `bash` the same checkpoint-and-rollback path (bash also rolls back and kills its process group on any unexpected failure); staging and checkpoints share one hashed-copy loop and one hidden-path rule; `search_text` is merged into `grep`.
- **Gateway:** `server/agent/runtime.py` keeps the parent loop; the nested investigation loop moves to `server/agent/investigation.py` and the shared tool-call parsing and result bounding to `server/agent/tool_calls.py`. Tool schemas are built from small helpers.
- **Desktop:** one request helper in the gateway client, a shared stream-worker base, one storage update path, a single parents-first pass for JSON import (previously quadratic on deep trees), one credential check for every run entry point, one request-options parser, and shared Docker lookup/spawn helpers in the runtime manager.
- **Tests:** every flat `tests/*.py` file moved into `tests/unit/<domain>/` (adding `shared/`) or `tests/structural/`, enforced by a structural test; duplicate baseline and completion tests merged; one `tests/helpers.executor_config` replaces about twenty per-file factories. New regression tests cover each fix above and malformed JSON imports.
- **Formatting:** the repository is `ruff format`-formatted, the `ruff` validation tier also runs `ruff format --check .`, and the E701/E702 lint ignores are dropped.
- **Documentation:** the harness validation plan is archived as `archived-doc/harness-validation-plan.md` with its open items in `docs/ROADMAP.md`; `docs/TESTING.md`, `docs/CONTRIBUTING.md`, and `AGENTS.md` describe the test layout and formatter.

## Unreleased — Repository investigation reliability and model selection

- `investigate_repository` now always returns a summary unless the provider fails before any evidence is gathered. Fixed: tool calls left unanswered when the nested loop stopped mid-turn (providers rejected the synthesis request); the nested wall-time limit aborting the loop with no summary instead of reserving time for synthesis; a paid final answer being discarded when it pushed usage over the nested cost cap; nested results sharing, and being starved by, the parent run's 512,000-byte result ledger; unbounded nested context; empty final answers accepted as summaries; and no retry for transient provider errors. Mid-conversation synthesis instructions are sent as `user` messages, reasoning is sent back with tool calls, and provider error text is included in failures. Results report `stop_reason`.
- The Settings investigation-model control works again: the Save button called an out-of-scope `currentText` (a QML ReferenceError), so the setting never changed from MiMo-V2.5. The picker also accepts any typed OpenRouter model ID, keeps its selection across model-list reloads, and the legacy DeepSeek default is migrated only once, so choosing it later sticks.

## Unreleased — Code simplification

A KISS/YAGNI pass that keeps behavior, the QML bridge surface, HTTP/protocol shapes, and tool contracts unchanged except for the fixes below. The completed plan is archived as `archived-doc/simplification-plan.md`; the findings it left for a product or security decision are listed in `docs/ROADMAP.md`.

**Fixes**

- Holding a model in the model picker to favourite it no longer raises `AttributeError`; `SettingsService` called `Storage` methods that did not exist.
- `scripts/validate.py` runs again when invoked as a script. Its `__main__` guard had been lost, so every CI `validate.py <tier>` step exited 0 without running anything. **CI now really runs these tiers.**
- The investigation model's forced final-summary turn is no longer offered tools. An empty `allowed_tools` set now sends `tool_choice: "none"`.
- The composer hint now names the actual steer shortcut, Ctrl+Enter; Shift+Enter always inserts a newline.
- Malformed JSON posted to the approval endpoint returns 422 instead of an internal error.
- Staged rewrites keep file permission modes. `write`, `edit`, and `apply_patch` replaced files through a `0600` temporary file, so every rewrite of an existing file reported a permission change in `status` and `workspace_status`, and publication made the host file owner-only on POSIX hosts. A replaced file now keeps its mode and a new file gets the default (`0666` minus the umask).
- The fast CI tier passes on GitHub runners, whose login profile appends `/snap/bin` to `PATH`; the bash environment test now requires the base `PATH` entries rather than an exact suffix.
- The Windows release job's validation tiers pass now that they really run. Preflight version probes no longer read pipes a killed probe's child can hold open (the hang aborted the whole test run); the validation environment keeps `USERPROFILE` and the other Windows OS variables so `Path.home()` works; on Windows, tests use `.pytest-tmp/` because the workspace broker refuses workspaces under `AppData`, where the system temp directory lives; checkpoint restore only passes `follow_symlinks=False` to `chmod` where the platform supports it; tests that hash or slice fixture text write exact bytes rather than platform newlines; and tests that need the executor's Linux container (its `/bin/bash`, POSIX permission bits, or POSIX `st_ctime`) carry a `posix` marker and are skipped on Windows (the executor only runs in that container, and Linux CI still runs them).

**Simplification**

- **Desktop:**
  - `app.py` only wires the application. Its `DesktopBridge` subclass (investigation-model defaults, model filtering, validated saving, deferred permission reloads) is folded into `SettingsService`/`DesktopBridge`, and `src/investigation_models.py` is merged into `src/desktop/preferences.py`.
  - Removed the unused worker slots from the bridge (`onRunStarted`, `onAgentEvent`, `onStream*`, `onWorkerFinished`).
  - Replaced the module-global publication client with an explicit one.
  - Merged the two identical staging workers.
  - Status updates go through `set_status` only.
  - Consolidated runtime-state updates and the message-metadata fields.
  - Deduplicated QML dialog and model-option code.
- **Gateway:**
  - One tool-call parser in the agent runtime, and the investigation loop is split into small helpers.
  - One workspace guard and shared run bookkeeping in `GatewayService`.
  - One Starlette handler for `GatewayServiceError`.
  - One request helper in `ExecutorClient`.
  - The OpenRouter title header and provider routing are shared.
  - The agent context compactor returns only the messages.
- **Executor:**
  - Removed the unused `seed=False` path and the `.local-chat-snapshot.json` file it read.
  - Removed the test-only checkpoint inspection and the unused checkpoint options; `CheckpointError` is folded into `ExecutorToolError`.
  - One `sha256_file` and one atomic write replace their copies.
  - Pagination cursors and listing lines are shared in `executor/tools/listing.py`.
  - The text search now lives in `grep.py`.
  - `ExecutorService.execute` dispatches through a table that passes each tool only its effective limits.
  - `max_checkpoint_bytes` is keyword-required on the mutation tools (the stale 2,000,000,000 default is gone), `bash` reads its caps from `ExecutorConfig.HARD_CEILINGS`, and the `coding` profile is derived from the config field defaults.
  - One helper each for the `expected_sha256` conflict check, the regular non-hard-linked file check, and streaming UTF-8 validation (`executor/utf8.py`). `read` now reports hard-linked targets with the same message as the mutation tools.
  - `ExecutorToolError.retryable` is removed; nothing read it.
- **Removed dead code:**
  - The unused bash event stream (`/v1/tools/{id}/events`) and its client method.
  - `scripts/capture_screenshot.py` and `src/commands.py`.
  - Unused package re-exports and `__version__` strings.
  - Legacy `run_command`/`patch` display branches.
  - Assorted unreferenced helpers, each checked across Python, QML, tests, scripts, workflows, and the build spec.
  - The superseded `scripts/install.ps1` and `scripts/uninstall-shortcuts.ps1`, the icon/mockup/protocol-check scripts, and `assets/screenshot.png`.
- **Dependencies:** Pillow and pytest-asyncio are no longer development dependencies.
- **Documentation:** the completed harness fix, harness QoL, organization, and simplification plans are archived in `archived-doc/`, with their open items in `docs/ROADMAP.md`. `docs/HARNESS_VALIDATION_PLAN.md` records per-phase status. `docs/TESTING.md` holds the only list of check commands, and stale container, testing, and architecture text is corrected.
- **Lint:** ruff no longer ignores unused imports and variables (`F401`, `F841`), import order (`I001`), or the `UP` modernization rules.

**Tests**

- New coverage:
  - the fixes above;
  - the folded bridge behavior (investigation models are listed, filterable, and validated);
  - agent worker failure handling;
  - a Unix-socket round trip for `ExecutorClient`;
  - agent context compaction;
  - bash cancellation bookkeeping.
- Replaced source-text assertions on `server/service.py`, `server/openrouter/agent.py`, `app.py`, and `src/workers.py` with behavioral tests. The `"pi-compatible"` profile constants that existed only for those checks are removed.
- Tool vocabulary and mode rules are asserted once, in `tests/test_tooling_contract.py`, instead of in seven files. Duplicated executor-config, bash, approval, and investigation cases are merged into their domain files.
- `tests/ui/test_privacy_and_transcript.py` no longer reads source text. Its checks are behavioral desktop-service tests, the existing QML rendering tests, or named structural checks in `tests/structural/test_qml_structure.py`.

## Unreleased — Harness P2-5/P2-6 and `apply_patch`

- Renamed the multi-file mutation tool `patch` to `apply_patch` everywhere (registry, schema, executor dispatch and module, permissions, approval display, docs, tests); its malformed-request error code is now `apply_patch.malformed`. `patch` is a removed name with no alias. The `max_patch_operations`/`max_patch_bytes` limit names are unchanged.
- `bash` cancellation now always rolls back, even with `rollback_on_failure: false`, and reports `rollback_reason: "cancelled"`; commands get `/dev/null` stdin unless `stdin` is supplied, so they never inherit a terminal from the executor.
- Restored the bash-tool unit coverage lost in the test reorganization (plus `rollback_on_failure` opt-out coverage) in `tests/unit/executor/test_bash.py`, and the QML transcript rendering tests as `tests/ui/test_transcript_qml.py` (slow tier).
- Consolidated planning documents: `docs/HARNESS_FIX_PLAN.md` and `docs/ROADMAP.md` (moved from the root), a plan index in `docs/README.md`, the reconciled root QoL plan archived as `archived-doc/improvement-plan.md`, and the implemented bash-rollback note removed. `tests/structural/test_documentation_layout.py` enforces the layout and naming.
- Documented hard-linked file limitations and workarounds in `docs/TROUBLESHOOTING.md`.

## Unreleased — Harness P1 gaps

- New read-only Agent tool `status`: every staged change against the publication baseline (created/modified/deleted/permission changes, hashes, sizes, modes), bounded unified diffs, pagination, and the number of publication batches needed. No Git involved.
- New mutation tool `apply_patch`: ordered `write`/`edit`/`delete` operations across files under one checkpoint, all-or-nothing, with per-operation results and failure diagnostics (`max_patch_operations`, `max_patch_bytes`).
- `edit` retries an unmatched multi-line `old_str` once in the file's other line-ending convention (LF ↔ CRLF) and reports typed, bounded diagnostics (`edit.no_match`, `edit.too_many_matches`, `edit.too_few_matches`, `staging.conflict`, `edit.malformed_context`). Failed results can now carry diagnostic `data`.
- The shrink guard is advisory (`shrink_warning: true`) for exact edits and for writes confirmed by a matching `expected_sha256`; unconfirmed whole-file shrinks are still refused.
- Changesets above one broker batch (500 files / 32,000,000 bytes) publish as ordered, separately approved batches; each batch is all-or-nothing on the host, progress is recorded in a durable ledger, and a failed batch can be retried to resume. The staging baseline hand-off now sends a content-free receipt.
- Binary and non-UTF-8 files publish byte-exact through `PublishOperation.content_base64`.
- `visible_files()` reuses digests for unchanged files (stat signature plus a racy-change window), checkpoints no longer re-hash the object store, and write/edit/apply_patch status reuses the checkpoint's walk, so a warm single-file mutation no longer hashes the whole tree.
- Consecutive read-only tool calls in one turn (`read`, `grep`, `find`, `ls`, `status`) run concurrently; mutations stay serialized in order.
- Opt-in allowlisted egress prototype (`docker/compose.egress.yaml`, `egress/`): package installs through an audited CONNECT proxy on an internal-only network; the default profile keeps `network_mode: none`.

## Unreleased — Harness P0 fixes

- Raised the protocol tool-argument cap from 512 KiB to 17,000,000 bytes, derived from the largest argument-carrying hard ceiling plus a fixed envelope and measured as unescaped UTF-8 JSON, so `write` content and `bash` stdin reach their documented limits in every profile.
- `investigate_repository` now resets its four-call allowance at every parent model turn and caps nested wall time at 300 seconds.
- `grep` honors the configured `max_search_seconds`; `find` and `ls` gained the same time budget and report `truncation_reason: "time_budget"`.
- `ls` (and `grep`) now default to the documented 500 results when `max_results` is omitted.
- Added `max_grep_output_bytes` so grep's output clip no longer shares the per-file scan budget.
- A plain `env/` directory is no longer excluded from staging, tools, or publication; `.venv`/`venv` remain excluded.
- `read` now treats metadata/dependency/cache paths hidden from `find`/`grep`/`ls` as missing in both modes.

## Unreleased — Documentation refresh

- Updated all authoritative documentation for the eight-tool API, including `investigate_repository`.
- Added architecture sections for investigation delegation, budget profiles, journaling, skills, and path-safety rules.
- Documented executor tool details (read clamps, shrink guards, bash environment controls) in `docs/TOOLS.md`.
- Documented agent budget profiles and broker publication bounds in `docs/LIMITS.md` and `docs/PUBLICATION.md`.
- Added investigation failure modes to `docs/TROUBLESHOOTING.md`.
- Created `ARCHIVED DOC/` and moved the fully implemented `subagent-tool-plan.md` there.
- Added a concise repository map with per-folder descriptions to `AGENTS.md`.

## Unreleased — Phase 7

- Rebuilt repository documentation around separate architecture, tools, limits, publication, contributor, and troubleshooting guides.
- Documented the current seven-tool API, Plan/Agent permissions, effective resource limits, staging, checkpointing, publication, recovery, and security boundary.
- Reworked the project plan into a status-oriented roadmap rather than a hybrid architecture/history document.
- Added documentation contract tests for the public tool vocabulary and authoritative limit references.
- Updated the README to focus on product behavior, quick start, privacy/data flow, modes, recovery, and developer navigation.
