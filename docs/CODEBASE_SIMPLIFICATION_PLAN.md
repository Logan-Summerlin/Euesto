# Codebase simplification plan

**Status:** proposed; not started. A KISS/YAGNI pass over code, tests, and documentation that preserves behavior. Update each phase in place as work lands, and move this file to `archived-doc/codebase-simplification-plan.md` once it is complete (see [README.md](README.md#plans-and-roadmap)).

## Context

Euesto has about 33k lines of application code (Python and QML), 22k lines of tests and 7.6k lines of docs. An earlier pass ([the code-simplification plan](../archived-doc/simplification-plan.md)) removed the obvious dead code. Since then, harness Phases 0–5 have added a lot, and the weight has moved into a few places:

- **Oversized modules:** `server/agent/runtime.py` (2,527 lines, 17 run-keyed dicts), `src/runtime_manager.py` (1,324), `qml/Main.qml` (1,637).
- **Duplicated logic** in every layer.
- **Test scaffolding** copied between test files.
- **Implemented plans** still sitting in `docs/` as if they were living documents.

The goal is a smaller, more consistent and easier-to-maintain codebase that does exactly what it does today. Two things are allowed to change: the confirmed bugs in Phase 1 get fixed, and internal Python/QML seams move.

**Decisions:**
- **Deep, phased refactor.** Remove dead code and duplication, split the oversized modules, and consolidate the tests, in small commits that each pass validation.
- **No CLAUDE.md.** `AGENTS.md` is the repository's CLAUDE.md. Update it in place and don't add a second file.
- **Archive only.** Move the implemented plans to `archived-doc/`. Leave the existing CHANGELOG text as it is, apart from fixing links that the moves break, and add one new entry for this pass.
- **Keep compatibility for user data**, remove compatibility for callers. These stay:
  - SQLite migrations, including v0.1 to the message tree;
  - the stored `minimal` thinking level read as `low`;
  - the migration of the legacy investigation model;
  - the `$ARGUMENTS` placeholder in prompt templates;
  - import of export schemas v1 and v2;
  - the gateway's `LEGACY_CONTEXT_PREFIXES` for stored sessions;
  - checkpoint manifests that have no creation time;
  - the journal's legacy bash-rule migration and its notices;
  - the "Local OpenRouter Chat" keyring, export and window names.

**Invariants that must not move:**
- the thirteen-tool contract;
- HTTP and protocol shapes, including the diagnostic keys in `/v1/status`;
- the security boundary;
- publication behaviour;
- the bridge contract, except for the one signal change listed in 4.5, with its test updated.

## Ground rules (every step)

- Before deleting or moving a name, grep it across `*.py`, `*.qml`, `.github/`, `docker/`, `build/chatbot.spec`, `installer/` and `docs/`.
- **Watch these monkeypatch and private-member targets:**
  - In `server.agent.runtime`: `agent_turn` (patched about 15 times), `TURN_GROWTH_RESERVE_TOKENS`, `STREAM_*` and `RETRY_BASE_DELAY`.
  - `server.agent.investigation.agent_turn`.
  - `src.runtime_manager.locate_docker` (`tests/ui/conftest.py:39`).
  - `src.workspace_broker.MAX_PUBLISH_BYTES`, patched by name as a string.
  - Private members tests reach into: `_investigate_repository`, `_api_keys`, `_unchanged_read`, `_live` and `_investigation_calls`.
- `tests/ui/test_desktop_bridge_surface.py` pins the bridge. `tests/structural/test_qml_structure.py`, `QML_FILES` in `scripts/validate.py:25` and `docs/TESTING.md` list the QML files.
- **One commit per numbered step.** Before each commit, run `python scripts/validate.py fast`, `ruff` and `compile`. Add `qml` and `qml-smoke` when the step touches desktop or QML code, and `slow` for gateway-loop, bash or runner changes. Report results verbatim. Push at the end of each phase. The command list is in [TESTING.md](TESTING.md#required-checks).
- **Behaviour changes** happen only in Phase 1, and each one comes with a regression test named for the visible behaviour.

---

## Phase 0: Baseline

1. Install the toolchain (`python scripts/bootstrap.py`, or `pip install -r requirements-dev.lock`). Run `validate.py preflight`, then `fast`, `slow`, `ruff`, `compile`, `qml` and `qml-smoke`, and record the baseline pass/fail and test counts. If there is no Docker daemon, the docker tier is reported as unavailable, not as passed.

## Phase 1: Confirmed bugs (with regression tests)

1. **The model prompt shows a dict where it should show the bash timeout.** `server/agent/session_context.py:161-162` formats `limits["max_command_seconds"]`. `executor/config.py:281` `limits_status()` returns a dict per limit (`docs/LIMITS.md:150` documents that shape). Read `["effective"]` in session_context. Fix the fake at `tests/unit/gateway/test_harness_loop.py:34` to use the real shape, and add a test that the prompt line contains the number.
2. **The compile tier skips `runner/`.** Add `runner` to `scripts/validate.py` (the compile command, about line 432) and to the compileall line in `docs/TESTING.md`.
3. **A rejected nested `bash` call loses its error message.** `server/agent/tool_calls.py:52-53` blanks `output` for any bash result. Blank it only when `result.ok`.
4. **A short wall-time remainder fails the run instead of winding it down.** `_clamp_bash_timeout` (`runtime.py:2501`) raises `RuntimeError`, which the `(TypeError, ValueError)` handler at about line 1491 doesn't catch. Raise the error type that handler expects, or route it to wind-down, and add a test.
5. Make `remove_staged_session` (`src/runtime_manager.py:566`) write atomically through the same temp-file and `os.replace` helper as `record_staged_session`.

## Phase 2: Dead code (zero behaviour change)

Delete each item after re-grepping it. Items referenced only from tests are deleted together with those test lines.

**Gateway and shared:**
- `SCHEMAS_BY_NAME` (`openrouter/agent.py:242`)
- `MISS_CAUSES` (`usage.py:20`)
- `STOP_CAUSES` (`shared/investigation.py:35`)
- `SECRET_KINDS` (`shared/secret_scan.py:62`)
- `RunState.tool_shape`
- the `"waiting"` run state (`journal/store.py:189`)
- the unused `keep_recent` parameter (`context.py:17`)
- `agent_payload(stream=…)`
- the `RunBudget.add_usage` `input_tokens`/`output_tokens` aliases (fix the fake at `test_agent_completion.py:66`)
- the "older result shape" fallbacks that only serve test fakes (`runtime.py:1758-1767`, `_consumed_bytes` at 2309; update the fakes to the real `read` result)
- `tool_calls.parse_arguments`, once item I1 in 3.1 lands
- `STAGED_EDIT_TOOLS` (only tests and CONTRIBUTING use it)

**Executor, runner and scripts:**
- `discard_staging` (`staging.py:244`)
- `BashRunner._write_stdin` (`bash.py:401`)
- `PYTHON_EXTENSIONS` and `reduce_output`'s `command` parameter (`reduction.py`)
- the never-passed `restore_checkpoint` limits
- `mode_changed`, inlined into `permission_changed`
- the duplicate `except` in `state.py:85`
- the in-function import in `sandbox_git.py:419`
- the `_OutputBuffer` alias
- the redundant `except (FileNotFoundError, OSError)` in `validate.py:625`
- `requirements.txt` and `requirements-dev.txt`, if a re-grep confirms nothing reads them (bootstrap, CI and Docker all use the lock or `requirements-gateway.txt`)

**Desktop:**
- the `refresh=False` path of `fetch_models`
- `review_staging`'s `cursor` parameter
- `GatewayStreamEvent.done`
- `HealthState.STARTING`
- the empty-token check in `connection.py:41`
- transcript row fields QML never reads (`kind`, `parentMessageId`, `model`, `legacy`, `eventType`, `rollback`; tests switch to `role`/`key`)
- `RunMeter.to_dict` keys QML never reads
- the one-line pass-throughs in `storage.py:719-725` and `markdown_renderer.py:38`

## Phase 3: Remove duplication within each layer

### 3.1 Gateway and shared (`server/`, `shared/`)

- **`service.py`:**
  - one `_executor_call(code, fn)` helper replaces the 7 copies of the try/raise `GatewayServiceError(…, status=409)` pattern;
  - one `_require_idle_staging()` replaces 3 copies;
  - `_save_snapshot`/`_save_session` become inline callables, like `plan_saver`.
- **`app.py`:** one `_body(request, code, allowed)` helper raises `GatewayServiceError(status=422)` and replaces about 12 copied try/`_json` blocks. Merge the three `_models*` functions. Error codes and shapes stay the same.
- **Declared checks:** one parser, in `service.declared_checks`, used by runtime. Drop the bare-string branch and the duplicate `DEFAULT_CHECK_TIMEOUT_SECONDS`, and use `shared.policy.DEFAULT_CHECK_SECONDS`.
- **Permission rules:** one `shared/permissions.rule_from_request(...)` used by both `runtime._run_rule_for_request` and `JournalStore.rule_for_request`.
- **Inside `runtime.py`:**
  - `_pause(...)` replaces the 4 hand-built `run.paused` payloads;
  - one tool-free summary-turn helper serves wind-down and compaction, along with one "current plan" note;
  - one `_ask(...)` serves the three `approval.required` → `approvals.wait` paths;
  - one `session_key(request, run_id)`;
  - one read key, shared by `_read_dedup_key` and `_unchanged_read`;
  - `_coverage_percent` reuses `results._coverage_share`;
  - one leading-system prefix scan, shared with `openrouter/agent.with_cache_breakpoints`;
  - drop the second parse and validation of reads in `_execute_parallel_group`.
- **Investigation:**
  - (I1) pass the already-validated arguments dict into `RepositoryInvestigation.run` and delete `_parse_investigation_arguments`;
  - (I2) nested calls use `parse_call_arguments` plus `validate_arguments`;
  - (I4) one shared provider-retry helper serves `runtime._model_turn` and `investigation._turn`, with one set of retry constants;
  - (I6) journal redactions directly and drop `pending_redactions`.
  - The nested model keeps seeing JSON results; unifying the result format is out of scope because it changes what the model sees.
- **Schema literals** import their gateway constants: `MAX_PLAN_STEPS` and friends from `plan.py`, `MAX_INSPECTED_PATHS`/`MAX_INSPECTED_PATH_CHARS`, `MAX_SEARCH_RESULTS`, `MAX_URL_CHARS`. The values don't change. `plan._parse` keeps only the checks the schema can't express: id length, duplicate ids and whitespace folding.
- **`shared/tools.py`:** `INVESTIGATION_TOOLS`, `PLAN_EXECUTOR_TOOLS` and `PARALLEL_SAFE_TOOLS` become one definition, aliased only where the names carry meaning. Compute the plan-mode tool union once and reuse it in `service.py:118`.
- **`shared/requests.py`:**
  - `to_dict`/`from_dict` derive fields with `dataclasses.fields(cls)`, as `tools.py` does;
  - `REASONING_EFFORT_FALLBACKS` becomes a plain one-step map;
  - `NETWORK_MODES` comes from `shared.policy`;
  - the message limits become shared constants.
- **OpenRouter:**
  - one helper builds the auth and `X-Title` headers, used by `agent.py`, `client.py`, `web.py` and `catalog.py`;
  - merge the two `bounded_excerpt` variants.
  - The two SSE paths stay separate: their error semantics differ on purpose (chat raises non-retryable, the agent path retryable).
- **`journal/store.py`:**
  - `_json_object()` replaces the 6 copies of decode-and-check-dict;
  - `is_terminal` reads `runs.state` instead of parsing up to 20k events, after first confirming nothing appends an event after a terminal one;
  - fold `nonterminal_runs` into its single caller.
- **Run-keyed state:** move the 17 run-keyed dicts onto `RunState`, which deletes the hand-written pops, the `finally` cleanup and `_fallback_state`.

### 3.2 Executor, runner, egress

- **Source walk:** one `iter_source_files(source)` generator serves `staging.seed_staging` and `drift.host_files`, together with one set of no-follow open flags. That gives the link, secret, exclusion and hard-link rules a single home.
- **Proxy validation:** one `_PROXY_URL` regex and proxy-environment builder in `runner/` (the runner imports nothing from the project), used by `executor/egress.py`.
- **Process-group kill:** one SIGTERM → wait → SIGKILL helper in `runner/process.py`, used by `executor/tools/bash.py`, `runner/server.py` and `runner/process.py`.
- **Limits:** limit arguments are keyword-required and supplied by `ExecutorConfig`. Remove the second clamps in `bash.py`, `listing.py`, `grep.py`, `config.py:268` and the copied defaults in `checkpoints.py:29`.
- `OUTPUT_DIRECTORY` is defined once. Add `OutputBuffer.from_wire()` so `_remote_outcome` stops reaching into the buffer's internals. `/v1/status` computes `workspace_changes` once per call instead of walking staging twice.

### 3.3 Desktop (`src/`, `app.py`)

- **Worker tracking:** a `track(owner, attr, worker, after=None)` helper in `src/workers.py` replaces 13 finish-and-deleteLater handlers. `stop_worker` and `stop_setup` share one implementation.
- **Workers collapse into `CallWorker`:** `StagingWorker`, `CatalogWorker` and `GatewayHealthWorker` become `CallWorker`. `src/connection.py` then shrinks to `HealthState` and `HealthResult`, which move into `src/desktop/runtime.py`.
- **Staging service:** `StagingPublicationService` sends discard, inspect and review through `_start_action`, which removes two worker slots and their handlers.
- **Bridge type:** delete `src/desktop/host.py` (a 22-signal Protocol) and use `TYPE_CHECKING` imports of `DesktopBridge`.
- **One home per fact:**
  - one built-in slash-command table, holding name, description and handler;
  - one `_update_workspace_config(mutator, sync=)` replaces 6 read-modify-write copies (`sync` stays off for policy setup bookkeeping);
  - one `SettingsService.context_budget(model)` replaces 3 compaction-budget computations;
  - one `_entry(ModelOption)` builds model entries (copy `year` and the description across so the picker text stays the same);
  - `RUNNER_TARGETS` derives from `shared.policy.RUNNER_IMAGES`, `NETWORK_MODES` replaces the string literals, `SERVER_TOOLS` derives from `fields(ServerToolOptions)`, and the gateway-URL default lives in one place.
- **`runtime_manager.py`:**
  - a `ComposeSession.run(...)` method and a `_collect_audit(...)` helper replace the duplicated compose and audit code;
  - the compose path is defined once;
  - one `_is_key()`;
  - canonicalise the workspace once, in `desktop/runtime.py`.
- **Smaller fixes:**
  - `stop_auto()` also emits the state change and is used everywhere;
  - `add_user_turn` returns a "retitled" flag instead of the messages being scanned twice;
  - `_runtime_settings` converts QML camelCase once at the boundary (saved snake_case JSON still loads).
- **Single-shot `chunk` signal:** the text moves onto `complete`/`failed` and the `chunk` signal goes away, along with `stream_chunks`, `on_stream_chunk` and the fallback run-id path that never fires. Text is still saved on failure.

## Phase 4: Reorganisation

1. **Split `server/agent/runtime.py`** along the seams the per-run objects from 3.1 create. Target about 1,000 lines left in `runtime.py`, with no logic changes.
   - `compaction.py`: the `ContextMeter` per-run object, `HighWater`, `ContextFloorTooLarge`, the prompt estimate and `_maybe_compact`.
   - `file_ledger.py`: `SeenFile`, coverage, the unchanged-read notes and the expected-hash fill.
   - `turns.py`: `_DeltaJournal`, `_model_turn` and the retry/stream constants, shared with investigation.
   - `gateway_tools.py`: `update_plan`, the web tools, URL approval and `investigate_repository`.
   - The three ask paths move into the existing `approvals.py`.
   - Monkeypatch targets in tests move with the code (for example `server.agent.turns.agent_turn`).
2. **Split `src/runtime_manager.py`** into a `src/runtime/` package:
   - `images.py`: `RuntimeImages`, `RuntimeSettings`, `detect_runner_target`, `workspace_key`;
   - `docker.py`: `locate_*`, `run_docker`, `compose_base_args`, spawn and redact;
   - `sessions.py`: staged sessions;
   - `setup.py`: `SetupWorker`, mounts, egress overlays and the audit;
   - `manager.py`: `RuntimeWorker` and `RuntimeManager`.

   Update the imports and the `locate_docker` patch target, plus `build/chatbot.spec` if it needs hidden imports.
3. **Split `src/desktop/staging_publication.py`** into `publication.py` (the batch chain and the confirm/publish cycle) and `staging.py` (review, discard, rebase, checks and the leave guard). The pure formatting helpers at lines 752–811 move next to `approval_display.publication_summary`.
4. **Fix layering:**
   - The desktop should not import `server/`. Move skill discovery and parsing (`Skill`, `parse_skill`, `discover_skills`, `MAX_REFERENCE_BYTES`) out of `server/extensions/skills.py`: into `src/extensions.py` if the gateway only needs `render_skill_context`, otherwise into `shared/skills.py`.
   - `shared/investigation.py` is only used by the server; move it to `server/agent/`.
   - `shared/publication_risk.py` is only used by the desktop; move it to `src/desktop/`.
   - Fix `local_path`, which lives in `conversations.py` but is imported by `runtime.py`.
5. **Clean up `qml/Main.qml`:**
   - add inline `SectionLabel`/`HelpText` components, a `warningColor` property, and `idle`/`stagingIdle`/`setupReady` properties;
   - use `toLocaleString()` in place of the regex thousands-separator copies;
   - add a `decide()` function to the approval dialog;
   - move `SettingsDialog`, `ReviewDialog`, `ModelDialog` and `ApprovalDialog` into their own `qml/*.qml` files, keeping every `objectName`;
   - in the bridge, `fileExported`/`fileImported` go through `infoRequested`; update `test_desktop_bridge_surface.py`;
   - update `QML_FILES`, `test_qml_structure.py` and the qmllint line in `TESTING.md`.

## Phase 5: Scripts, CI, Docker

- **`scripts/validate.py`:**
  - derive `COMMAND_TIMEOUTS` from `TIER_TIMEOUTS`;
  - derive the argparse `choices` from the tiers;
  - turn `commands()` into a table;
  - reuse `ALL_TIERS` for `all` and fix its comment.
  - Version probing stays, because it feeds the JSON report.
- **Workflow tests:** one shared workflow-parser module, `tests/structural/workflow_yaml.py`, used by both `test_workflow_syntax.py` and `test_workflow_budgets.py`. No new dependency.
- **Deliberately not changed, recorded in ROADMAP:**
  - `release.yml` restructuring: the publish path only runs on tag builds and can't be checked here;
  - Compose YAML anchors and the runner Dockerfile Node dedupe: they need Docker to render and check. Do them only if `docker compose config` and a build are available in the session.

## Phase 6: Test suite

- **Shared scaffolding:**
  - `tests/unit/gateway/agent_fakes.py` holds `ScriptedExecutor`, `Provider`, `AllowApprovals`, `call`/`raw_call`/`tools_turn`/`final_turn`, `make_request`, `run` and `of_kind()`. Test modules stop importing each other: `test_long_runs`, `test_streaming_turns` and `test_redaction_and_web` currently import `test_harness_loop`, and `test_tool_feedback` redefines all of it.
  - `tests/helpers.py` gains `executor_service(tmp_path, files=None, **cfg)`, `run_tool(...)`, `TOKEN` (one definition), `Recorder`/`FakeHost` for the desktop, and publication `_manifest`/`FakeBroker`.
  - A new `tests/docker/conftest.py` holds `_available` and `_run`.
  - `RENDEZVOUS_TIMEOUT_SECONDS` is defined once.
- **Remove redundancy:**
  - fold the unique asserts in `executor/test_regressions.py` (DOS names, unique paths, reparse points, ls secret filtering) into the paths and edge-case tests, then delete the file;
  - delete `test_redaction_and_web.py:347`, which duplicates `test_tool_contract.py:127`;
  - parametrize find/ls pagination and the investigation-budget lookalikes (:168/:248, :401/:439, :703/:725/:851);
  - rewrite the first nine `test_investigation_budget` tests with that file's own helpers;
  - fold `test_investigation_model.py` into `test_investigation_contract.py`;
  - collect the compaction tests from `harness_loop`, `long_runs` and `tool_feedback` into `test_compaction.py`.
- **Prune brittle structural pins:**
  - `test_qml_structure.py`: drop styling-literal pins (widths, colours, counts) and pins already covered behaviourally (the leave dialog, reasoning levels);
  - `test_documentation_contract.py`: drop the hard-coded tool set (use the registry), the overlapping index check, the `.gitignore` and wording pins, and the 4× "500 results" assertions;
  - `test_workflow_syntax.py`: merge the trigger tests into one table-driven test;
  - `test_release_image_refs.py`: parametrize the digest tests;
  - drop the `__main__` blocks;
  - `test_validation_harness`: run preflight in-process with a stub runner instead of a subprocess;
  - `tests/docker/test_container_policy.py` only reads files: move its unique checks to `tests/structural/` and drop the two that duplicate `test_runner_split`.
- **Layout and naming by domain:**
  - `test_harness_loop.py` → `test_agent_loop.py`;
  - the gateway half of `executor/test_budget_resources.py` → `gateway/`;
  - the runner tests (`test_runner_protocol`, and the `runner/setup.py` cases from `desktop/test_setup_phase.py`) → a new `tests/unit/runner/` (add `runner` to the `TESTING.md` domain list);
  - the snapshot cases in `test_output_artifacts` → the executor snapshot tests;
  - rename test functions named after versions or history (`…_v04_…`, `test_v05_…`, `…_tripled`, `…legacy_tool_limit`, `…two_x…`, `…old_run_allowance…`);
  - remove plan and phase references ("plan 0.6", "Phase 3") from the 26 test docstrings; they go stale once those plans are archived.
- Test counts may fall because of merges, but every behaviour asserted today stays asserted. Track this against the Phase 0 baseline.

## Phase 7: Documentation

- **Archive the implemented plans** to `archived-doc/` under lowercase-hyphenated names, each with an "Archived — implemented" banner:
  - `HARNESS_TOOL_FEEDBACK_PLAN` → `harness-tool-feedback-plan.md`;
  - `CONTEXT_COMPACTION_LIMITS_PLAN` → `context-compaction-limits-plan.md`;
  - `MODEL_CONTROLS_PLAN` → `model-controls-plan.md` (all three phases are in the tree);
  - `AGENTIC_HARNESS_PLAN` → `agentic-harness-plan.md`. Its proposed Phases 6–7 (project memory, missions, background processes, parallel attempts, first-run wizard, diagnostics page, workspace switching, model presets, spending caps, naming) move into `docs/ROADMAP.md` as short open items that link to the archived sections, following the existing "carried from archived plans" pattern.
- After the moves, update the `docs/README.md` index, the `archived-doc/README.md` table, the `ROADMAP.md` links, and the plan links in `CHANGELOG.md`; touch only the links in the CHANGELOG.
- **AGENTS.md** (the CLAUDE.md equivalent): update the repository map and the change-discipline file list to the new modules (`server/agent/{compaction,file_ledger,turns,gateway_tools}.py`, `src/runtime/`, the QML dialog files, the moved shared modules). Keep it invariant-only and no longer.
- **Other docs:**
  - `ARCHITECTURE.md`: the new module ownership, and the desktop never importing `server/`;
  - `CONTRIBUTING.md`: replace the restated tool-sync list with a link to the AGENTS.md change discipline, and fix the stale `tool_calls.py` and `STAGED_EDIT_TOOLS` references;
  - `TESTING.md`: the domain list, the compile line, the qmllint file list and the helper modules;
  - `docker/README.container.md`, if paths change.
- **ROADMAP "reported, not changed":**
  - unused `/v1/status` keys (a protocol and diagnostics decision);
  - unification of the nested investigation result format;
  - the `release.yml` and Compose dedupe;
  - silently swallowed `GatewayError`s in desktop config sync and notices;
  - inconsistent error prefixes and worker signal names;
  - the FTS index rebuilt on every startup.
- When every phase is done, move this plan to `archived-doc/codebase-simplification-plan.md` with an archive banner, and update both indexes.
- Add a `CHANGELOG.md` entry, "Unreleased — codebase simplification": the bug fixes, the module moves, and net line counts per area (as the previous pass did).

## Verification

- Per commit: `python scripts/validate.py fast`, `ruff` and `compile`, plus `qml` and `qml-smoke` for desktop or QML work and `slow` for loop, bash or runner work.
- End of each phase, then at the end: `python scripts/validate.py all`. The docker tier runs if a daemon is available; otherwise it is reported as unavailable, and `tests/docker` is checked to collect (`validate.py collect`).
- **Structural greps:**
  - nothing in `src/` or `app.py` imports `server`;
  - no references remain to deleted or moved names;
  - `test_documentation_layout.py` and `test_documentation_contract.py` pass (links, index, tool and limit values).
- **Spot behaviour checks:**
  - the prompt-rendering test shows `Commands time out after at most 900s.`;
  - `scripts/qml_smoke.py` loads the split QML;
  - the bridge-surface test passes with its one intended change.
- **Final summary for the user:** net lines per area, against the Phase 0 baseline counts.

## Out of scope (kept deliberately)

- User-data compatibility (see Context).
- HTTP and status payload shapes.
- The SSE error semantics of chat versus agent.
- The publication broker and `workspace_broker.py` logic.
- Redaction, the approval tiers and path containment.
- `reduction.py`'s parsers.
- Release manifest schema support.
- Product renaming.
- New dependencies.
