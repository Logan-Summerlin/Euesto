# Code Simplification Plan

A KISS/YAGNI pass over the repository: remove verified dead code, merge duplicated behavior, and cut concepts. Nothing else may change: behavior, the QML bridge surface, HTTP/protocol shapes, and tool contracts stay as they are, apart from the listed bug fixes. Work happens on the `Code-Simplification` branch in small commits, and each commit passes `pytest` (non-docker), `ruff check .`, `compileall`, and `pyside6-qmllint`.

**Before removing anything:**
- Re-grep the name across `*.py`, `*.qml`, workflows, Dockerfiles, `build/chatbot.spec`, `installer/*.iss`, and docs.
- Watch for dynamic references. Examples:
  - `tests/unit/publication/test_binary_publication.py` monkeypatches `src.workspace_broker.MAX_PUBLISH_BYTES` by string.
  - `tests/test_executor_tool_surface.py` used `globals()` lookups.
  - `tests/ui/test_desktop_bridge_surface.py` pins every bridge property, signal, and slot.

## Status

Steps 1–5 are complete, except the Step 5 items listed under "Remaining". Steps 6–8 are not started.

### Done

1. **Bug fixes (with regression tests):**
   - The model-favorite toggle called `Storage` methods that did not exist.
   - `scripts/validate.py` had lost its `__main__` guard, so every CI `validate.py <tier>` step passed without running anything. **CI now really runs these tiers.**
   - `agent_turn` treated `allowed_tools=set()` as "unset". The forced-synthesis turn now sends `tool_choice: "none"`.
2. **Lint:** `ruff` no longer ignores `F401`, `F841`, `I001`, or the `UP` rules.
3. **Desktop:**
   - The `app.py` bridge subclass is folded into `SettingsService` / `DesktopBridge`, and `src/investigation_models.py` is removed.
   - The unused `on*` worker slots are removed.
   - Verified-dead helpers are removed.
   - The module-global publication client is gone.
   - `StagingDiscardWorker` and `StagingInspectWorker` are merged into one staging worker.
   - Runtime-state updates are consolidated.
   - Status updates go through `set_status` only.
   - Message-metadata fields are shared.
   - QML dialog code is deduplicated.
   - The steer hint is fixed (Ctrl+Enter).
4. **Gateway/shared:**
   - `runtime.py` has one tool-call parser, and the investigation loop is split into helpers.
   - `GatewayService` has one workspace guard and shared run bookkeeping.
   - `GatewayServiceError` is handled by a single Starlette exception handler.
   - There is one `ExecutorClient._request` helper.
   - The unused bash event stream and executor route are removed, along with unused re-exports and options.
   - Source-text tests are replaced by behavioral tests.
5. **Executor (partial):**
   - The unused `seed=False` path and snapshot file are removed.
   - The test-only checkpoint inspection and the unused checkpoint options are removed.
   - `CheckpointError` is replaced by `ExecutorToolError`.
   - One `sha256_file` and one atomic write remain.
   - Pagination and listing helpers are shared in `executor/tools/listing.py`.
   - `search_text.py` is folded into `grep.py`.

## Remaining

### Step 5 — executor (finish)

- **`executor/app.py`:**
  - Replace the ten-branch `if/elif` in `ExecutorService.execute` with a dispatch table.
  - Unpack the semicolon-packed `workspace_status`.
  - Keep the error codes that `tests/unit/executor/test_error_codes.py` checks by AST.
- **Stale defaults:** `max_checkpoint_bytes=2_000_000_000` in `write`, `edit`, `apply_patch`, and `bash` should become keyword-required, since callers always pass it. Update the fakes in `tests/test_executor_dispatch_limits.py`.
- **`bash.py` limits:** take `MAX_STDIN_BYTES`, `MAX_COMMAND_BYTES`, and `MAX_COMMAND_SECONDS` from `config.HARD_CEILINGS` instead of repeating the numbers.
- **`config.py` profiles:** derive the `_profiles()` "base" dict from the `ExecutorConfig` field defaults.
- **One helper each:**
  - the `expected_sha256` conflict check (`write.py`, `edit.py`, `apply_patch._delete`);
  - the regular, non-hard-linked file check;
  - streaming UTF-8 validation (`read.py`, `write.py`, `edit.py`).
- **Other small items:**
  - `status.py` should use `WorkspaceChange.permission_changed`.
  - Drop `ExecutorToolError.retryable`; `executor/app.py` discards it.

### Step 6 — scripts and dependencies

- **Delete the orphaned files:**
  - `scripts/install.ps1` and `scripts/uninstall-shortcuts.ps1` (superseded by the Inno Setup installer).
  - `scripts/render_mockup.py`, `scripts/generate_icon.py`, and `scripts/protocol-check.py`.
  - `assets/screenshot.png`.
  - Keep `assets/app.ico`.
- **Remove Pillow** from:
  - `requirements-dev.txt` and `requirements-dev.lock`;
  - `scripts/validate.py` (`PYTHON_PACKAGES`);
  - `scripts/bootstrap.py`.
- **Drop `pytest-asyncio`:** convert its two tests in `tests/test_qol_phases.py` to `asyncio.run`, then remove it from the requirements, the lock, `validate.py`, and `bootstrap.py`.

### Step 7 — documentation

- **Archive completed plans.** Move `HARNESS_FIX_PLAN.md`, `HARNESS_QOL_PLAN.md`, and `ORGANIZATION_PLAN.md` to `archived-doc/` with lowercase-hyphen names.
  - Carry their open items into `ROADMAP.md`: the P3 bullets, copy-on-write staging, product naming, and flat-test relocation.
  - Update the `docs/README.md` plan table, `archived-doc/README.md`, and `tests/structural/test_documentation_layout.py` (which requires `HARNESS_FIX_PLAN.md`).
- **Validation plan.** Add per-phase status lines to `HARNESS_VALIDATION_PLAN.md`:
  - Phases 1–3 and 6 are done.
  - Open: Qt preflight, the validation image, collection/marker checks, JUnit upload, and a structural test that workflows invoke the validator.
- **One copy of the check commands.** Keep the canonical list in `docs/TESTING.md`. AGENTS.md, the root README, `docs/README.md`, `CONTRIBUTING.md`, and `docker/README.container.md` should link to it. Every compileall line must include `egress`.
- **Fix stale text:**
  - `TESTING.md`: the default-collection claim and folders that do not exist.
  - `docker/README.container.md`: the `env` exclusion, "shells are rejected", 2 GB vs 2.5 GB, and "v1.1".
  - `ARCHITECTURE.md`: publication bounds are per batch.
  - AGENTS.md repo map: it still lists the screenshot and the scripts.
  - `docs/README.md` "Local-only state": `.local-chat-snapshot.json` is no longer written. Drop it there and from `.gitignore` and `tests/test_organization_structure.py`.
  - Install instructions should use `requirements-dev.lock`.
- **Keep required strings.** Keep every string `tests/test_documentation_contract.py` requires, or update that test in the same commit.

### Step 8 — test consolidation

- **Tool vocabulary and mode rules** are asserted in seven files. Merge them into `tests/test_tooling_contract.py`:
  - `test_tooling_regressions.py`, `test_narrow_tooling.py`, `test_executor_tool_surface.py`, and `test_executor_dispatch_limits.py`;
  - fold `test_organization_structure.py` into `test_documentation_contract.py`.
- **Merge duplicated cases:**
  - executor-config cases into `test_executor_config.py`;
  - bash limit/rollback cases into `tests/unit/executor/test_bash.py`;
  - `test_qol_phases.py` into `tests/unit/gateway/test_approval_policy.py`;
  - `test_investigation_tool.py` and `test_subagent_tool.py` into `test_investigation_tool_contract.py`.
- **Remaining source-text assertions:** convert those in `tests/ui/test_privacy_and_transcript.py` to behavioral checks where practical.

### Finish

- Run the full checks: non-docker `pytest`, `pytest -m "slow and not docker"`, `ruff`, compileall, qmllint, `scripts/qml_smoke.py`, and `python scripts/validate.py preflight`.
- Push the branch.
- Summarize the net line changes for code, tests, and docs against `020a4b4`.

## Reported, deliberately not changed

These need a product or security decision:

- **Saved bash "always allow" rules are too broad.** `JournalStore.rule_for_request` reads `executable` and `arguments` arguments that no tool has. As a result, one saved bash rule allows every bash command in that workspace and mode. The per-run rule tokenizes `command` correctly. Unifying them tightens what existing saved rules allow.
- **`rule_used` is never called.** `AgentRuntime` stores the `rule_used` callback but never invokes it, so `PermissionRule.last_used_at` is never set.
- **The staging check is unreachable over HTTP.** In `server/service.py`, the auto-policy `staging.not_clean` preflight only runs when `investigation_model_id` is empty. `AgentRunRequest.from_dict` always fills that field, so the check never runs for HTTP requests.
- **Undeclared tool arguments.** `find` and `ls` accept `cursor`, and `read` accepts `offset`, but the model-facing schemas do not declare them.
- **`read` ignores larger configured limits.** It clamps to 256,000 bytes regardless of the configured `max_read_bytes`.
- **Starlette versions drift.** `requirements-dev.lock` pins `starlette==0.45.3` and `uvicorn==0.34.0`. The gateway image pins 0.52.1 for both.
- **Old product name.** "Local OpenRouter Chat" remains in the keyring service name, the export format marker, and the window title. Renaming it is a product decision, and the keyring name protects stored secrets.
- **Usage formatters differ.** The three usage formatters produce different UI strings, so they were left separate.
