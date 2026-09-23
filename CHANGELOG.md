# Changelog

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
- The Windows release job's validation tiers pass now that they really run. Preflight version probes no longer read pipes a killed probe's child can hold open (the hang aborted the whole test run); the validation environment keeps `USERPROFILE` and the other Windows OS variables so `Path.home()` works; on Windows, tests use `.pytest-tmp/` because the workspace broker refuses workspaces under `AppData`, where the system temp directory lives; checkpoint restore only passes `follow_symlinks=False` to `chmod` where the platform supports it; tests that hash or slice fixture text write exact bytes rather than platform newlines; and tests that run the executor's `/bin/bash` carry a `posix` marker and are skipped on Windows (the executor only runs in its Linux container, and Linux CI still runs them).

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
