# Code Simplification Plan

> **Archived — implemented; not normative.** Formerly `docs/SIMPLIFICATION_PLAN.md`. Every step is done (see Status). The findings under "Reported, deliberately not changed" are carried in [`docs/ROADMAP.md`](../docs/ROADMAP.md).

A KISS/YAGNI pass over the repository: remove verified dead code, merge duplicated behavior, and cut concepts. Nothing else may change: behavior, the QML bridge surface, HTTP/protocol shapes, and tool contracts stay as they are, apart from the listed bug fixes. Work happens on the `Code-Simplification` branch in small commits, and each commit passes `pytest` (non-docker), `ruff check .`, `compileall`, and `pyside6-qmllint`.

**Before removing anything:**
- Re-grep the name across `*.py`, `*.qml`, workflows, Dockerfiles, `build/chatbot.spec`, `installer/*.iss`, and docs.
- Watch for dynamic references. Examples:
  - `tests/unit/publication/test_binary_publication.py` monkeypatches `src.workspace_broker.MAX_PUBLISH_BYTES` by string.
  - `tests/test_executor_tool_surface.py` used `globals()` lookups.
  - `tests/ui/test_desktop_bridge_surface.py` pins every bridge property, signal, and slot.

## Status

**Complete.** Steps 1–8 are done and CI is green. The checks were run locally: non-docker `pytest`, `pytest -m "slow and not docker"`, `ruff`, compileall, qmllint, `scripts/qml_smoke.py`, and `python scripts/validate.py preflight`. The container tier ran in CI. The findings below still need a decision and are carried in `docs/ROADMAP.md`.

Net line changes against `020a4b4`, excluding this document and `CHANGELOG.md`:

| Area | Added | Removed | Net |
|---|---|---|---|
| Code (Python, QML, scripts, configuration) | 1,107 | 1,909 | −802 |
| Tests | 846 | 916 | −70 |
| Documentation | 140 | 93 | +47 |

The documentation growth is mostly the open items carried into `docs/ROADMAP.md` and the archive banners. The binary `assets/screenshot.png` is also deleted.

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
5. **Executor:**
   - The unused `seed=False` path and snapshot file are removed.
   - The test-only checkpoint inspection and the unused checkpoint options are removed.
   - `CheckpointError` is replaced by `ExecutorToolError`.
   - One `sha256_file` and one atomic write remain.
   - Pagination and listing helpers are shared in `executor/tools/listing.py`.
   - `search_text.py` is folded into `grep.py`.
   - `ExecutorService.execute` dispatches through a table, one entry per tool with only that operation's effective limits, and `workspace_status` is unpacked.
   - `max_checkpoint_bytes` is keyword-required on `write`, `edit`, `apply_patch`, and `bash`; the stale `2_000_000_000` default is gone.
   - `bash` takes its command, stdin, and time caps from `ExecutorConfig.HARD_CEILINGS`, and the `coding` profile is derived from the field defaults.
   - One helper each: `mutations.check_expected_sha256`, `paths.require_regular_file`, and `executor/utf8.py` (streaming UTF-8 validation for `read`, `write`, and `edit`).
   - `status` uses `WorkspaceChange.permission_changed`, and `ExecutorToolError.retryable` is gone.
6. **Scripts and dependencies:**
   - `scripts/install.ps1`, `scripts/uninstall-shortcuts.ps1`, `scripts/render_mockup.py`, `scripts/generate_icon.py`, `scripts/protocol-check.py`, and `assets/screenshot.png` are deleted; `assets/app.ico` stays.
   - Pillow and pytest-asyncio are removed from the requirements, the lock, `validate.py`, and `bootstrap.py`; the two async tests use `asyncio.run`.
7. **Documentation:**
   - `HARNESS_FIX_PLAN.md`, `HARNESS_QOL_PLAN.md`, and `ORGANIZATION_PLAN.md` are archived in `archived-doc/` with banners; their open items (copy-on-write staging, the P3 bullets, product naming, flat-test relocation) are in `docs/ROADMAP.md`.
   - `HARNESS_VALIDATION_PLAN.md` has per-phase status lines and an open-items list.
   - `docs/TESTING.md` holds the only list of check commands; AGENTS.md, the root README, `docs/README.md`, `CONTRIBUTING.md`, and `docker/README.container.md` link to it, and every compileall line includes `egress`.
   - Stale text is fixed in `TESTING.md`, `docker/README.container.md`, `ARCHITECTURE.md`, the AGENTS.md repository map, and `docs/README.md`; `.local-chat-snapshot.json` is gone from the docs, `.gitignore`, and the tests; install instructions use `requirements-dev.lock`.
8. **Tests:**
   - Tool vocabulary and mode rules live once, in `tests/test_tooling_contract.py`; `test_organization_structure.py` is folded into `test_documentation_contract.py`.
   - Executor-config cases are merged into `test_executor_config.py`, bash limit/rollback cases into `tests/unit/executor/test_bash.py`, `test_qol_phases.py` into `tests/unit/gateway/test_approval_policy.py`, and the two small investigation files into `test_investigation_tool_contract.py`.
   - `tests/ui/test_privacy_and_transcript.py` no longer reads source text: its Python checks are behavioral tests in `tests/unit/desktop/test_desktop_services.py`, transcript layout is covered by `tests/ui/test_transcript_qml.py`, and the QML visual and packaging invariants are named structural checks in `tests/structural/test_qml_structure.py`.
9. **CI fix:** `test_bash_preserves_restricted_environment` failed on GitHub runners, whose login profile appends `/snap/bin` to `PATH`. It now requires the base `PATH` entries rather than an exact suffix. This was the first failure CI surfaced once the `validate.py` fix made the tiers really run.


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
- **Staged rewrites drop file modes.** `write`, `edit`, and `apply_patch` replace a file through a `mkstemp` temporary file, so the result has mode `0600`. Every rewrite of an existing `0644` file therefore reports a permission change in `status` and `workspace_status`. The staged mode is also applied on the host at publication, which matters on POSIX hosts. Preserving the previous mode (and the umask default for new files) is a small fix, but it changes publication behavior, so it was reported instead of folded into this pass. `tests/test_tooling_contract.py` deliberately does not pin the permission count.
