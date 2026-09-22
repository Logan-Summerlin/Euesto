# Tools

This is the authoritative human-readable reference for the ten model-facing tools. The source of truth for dispatch is `shared/tools.py`; the model-facing JSON schemas live in `server/openrouter/agent.py`; executor limits and ceilings live in `executor/config.py`. Changes to one must be checked against the others.

## Common contract

Every executor request contains `request_id`, `run_id`, `tool`, `mode`, and an object-valued `arguments` field. `mode` is `plan` or `agent`. Tool requests are rejected when the tool is unknown, when Plan requests a mutation, or when arguments exceed the 17,000,000-byte protocol cap (`MAX_TOOL_ARGUMENT_BYTES` in `shared/tools.py`). The cap is measured as unescaped UTF-8 JSON, so newline- or control-character-heavy payloads do not lose capacity to escaping, and it is derived from the largest argument-carrying hard ceiling (`edit` result and `patch` content, 16,000,000 bytes) plus a 1,000,000-byte envelope, so it is never the binding constraint below a documented per-tool limit (see `docs/LIMITS.md`).

Every result contains `request_id`, `ok`, `output`, `data`, `error_code`, `truncated`, `elapsed_seconds`, `returned`, `total_known`, `limit`, and `next_cursor`. Optional counts are non-negative integers. Errors are classified and returned rather than exposing arbitrary exception details to the model. A failed result may carry bounded, JSON-serializable diagnostics in `data` (for example why an exact edit did not match, or which `patch` operation failed).

All tools operate on relative POSIX paths that are normalized and contained beneath the workspace root. Absolute, drive, UNC, traversal (`..`), Windows-alias, reserved-DOS-name, non-canonical-Unicode, and secret-like paths are rejected, as are symlinks and hard-linked files. Only UTF-8 text is readable and writable; binary content is refused.

Metadata, dependency, and cache directories (`.git`, `.hg`, `.svn`, `.venv`, `venv`, `.tox`, `.nox`, `node_modules`, `__pycache__`, and tool caches) plus executor `.local-chat-*` metadata are excluded from staging and are invisible to every read-oriented tool: `find`, `grep`, and `ls` omit them and `read` reports them as missing, in both Plan and Agent mode. A directory named `env/` is ordinary source and is staged, searched, and published like any other directory; name virtual environments `.venv` or `venv` to keep them out of staging.

## `read`

- **Purpose:** inspect UTF-8 text.
- **Arguments:** `path` (required); optional `start_line`, `end_line`, `offset`, and `max_bytes`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Defaults:** each call returns at most 64,000 bytes unless `max_bytes` raises it; the tool clamps any request to 256,000 bytes per call regardless of the configured profile limit.
- **Hard maximum:** 8,000,000 bytes at the protocol/config layer; the 256,000-byte per-call tool ceiling applies on top.
- **Semantics:** Plan reads the read-only source snapshot; Agent reads staged workspace content. Excluded metadata/dependency/cache paths fail as missing files, exactly as they are absent from `find`/`grep`/`ls`. Line ranges and byte offsets cannot be combined. An end-line past the end of the file is clipped to the final available line and reported as `range_clipped`; invalid start lines and offsets remain rejected, and byte offsets must land on UTF-8 character boundaries.
- **Truncation/cursors:** results report `truncated`, `byte_offset`, `next_offset`, and `next_start_line`; continue with the next line range or offset instead of assuming a whole file fits in one response.
- **Errors:** invalid path, invalid range, invalid offset, non-UTF-8 or binary content, missing file, and resource-limit failures are classified.

## `write`

- **Purpose:** create or replace one UTF-8 text file.
- **Arguments:** `path`, `content` required; optional `expected_sha256`, `create_parents`.
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged.
- **Default:** 1,000,000-byte file content limit in `coding`.
- **Hard maximum:** 8,000,000 bytes.
- **Hash:** `expected_sha256` is optional. When supplied it is an optimistic concurrency check; it is not a mandatory compatibility requirement. A mismatch fails with `staging.conflict` and reports the expected and actual hashes in `data`.
- **Shrink guard:** replacing an existing file (≥200 bytes and ≥20 lines) with less than half its bytes *and* lines is rejected (`staging.shrink_warning`) unless the write is confirmed by a matching `expected_sha256`, which proves the current content was reviewed. A confirmed large shrink is applied and reported with `shrink_warning: true` and `shrink_details`.
- **Source/staging:** writes target the writable staging tree, never the read-only source mount. Parent directories are created only with `create_parents`.
- **Failure:** checkpoint restoration occurs before an unsuccessful mutation is returned.

## `edit`

- **Purpose:** replace an exact string in one UTF-8 text file.
- **Arguments:** `path`, `old_str`, `new_str` required; optional `expected_occurrences` (1–1000) and `expected_sha256`.
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged.
- **Defaults:** 2,000,000-byte target and result limits in `coding`.
- **Hard maximums:** 16,000,000 bytes for target and result.
- **Hash:** optional optimistic concurrency check.
- **Semantics:** replacement streams through a temporary file and is swapped in atomically; the actual occurrence count must equal `expected_occurrences` (default 1) or the edit fails. Because an exact match with a verified count already proves the change deliberate, a large proportional reduction is applied and reported as `shrink_warning: true` with `shrink_details` instead of being rejected.
- **Newline policy:** `old_str` is first matched byte-for-byte. Only if it matches nothing and contains a line break is it retried once with its line breaks translated to the other convention (LF ↔ CRLF); `new_str` is translated the same way so the file keeps its own convention, and the result reports `line_ending_adjustment` (`lf_to_crlf`, `crlf_to_lf`, or null). The occurrence count must still match exactly; nothing fuzzy is ever applied.
- **Diagnostics:** failures are typed: `edit.no_match` (zero matches), `edit.too_many_matches`, `edit.too_few_matches`, `staging.conflict` (hash mismatch), and `edit.malformed_context` (empty/NUL `old_str`, invalid `expected_occurrences`). For targets up to 1,000,000 bytes, `data` includes the file's and `old_str`'s line-ending styles, up to 10 matching line numbers, and for zero matches the closest line with a ≤240-character escaped `context_preview` and hints (for example "matches only if whitespace is ignored").
- **Localized edits:** exact replacement avoids rewriting unrelated files; target/result limits make larger files incrementally inspectable and locally editable.
- **Failure:** checkpoint restoration occurs on failed mutation.

## `patch`

- **Purpose:** apply one logical change across several UTF-8 text files atomically.
- **Arguments:** `operations` (required, 1–`max_patch_operations`): an ordered list of objects with `operation` and `path` plus the fields of that operation — `write` (`content`, optional `expected_sha256`, `create_parents`), `edit` (`old_str`, `new_str`, optional `expected_occurrences`, `expected_sha256`), or `delete` (optional `expected_sha256`).
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged. A path-scoped approval rule matches a patch only when every operation path is inside its scope; "allow for this run"/saved rules scope to the deepest directory the paths share.
- **Defaults:** 100 operations and 2,000,000 bytes of combined `content`/`old_str`/`new_str` in `coding`; each operation also obeys the `write`/`edit` limits.
- **Hard maximums:** 500 operations and 16,000,000 combined bytes.
- **Semantics:** one checkpoint is taken, then operations run in order against the state left by the previous one (so several edits to one file compose). `write` and `edit` behave exactly as the standalone tools, including hash checks, the shrink guard, the newline policy, and diagnostics; `delete` removes one regular, non-hard-linked file. It is additive: `write` and `edit` remain the tools for single-file changes.
- **Atomicity:** if any operation fails, raises, or is interrupted, the checkpoint is restored and no operation remains applied. The failure's `data` names `failed_operation`, its `path`, `applied_before_failure`, `rolled_back: true`, and the underlying `cause_code`/`cause` diagnostics.
- **Result:** `operations` (per-operation path, kind, hashes, bounded diff, occurrences, and any shrink warning), `paths`, `operation_counts`, `shrink_warnings`, and the post-mutation workspace status. Per-operation diffs share a 64,000-byte budget.

## `bash`

- **Purpose:** run a non-interactive Bash command in the staged workspace.
- **Arguments:** `command` required; optional `working_directory`, `timeout_seconds` (1–900), `env`, and `stdin` (maximum 8,000,000 characters at schema level).
- **Modes:** Agent only.
- **Permission:** mutation-capable; checkpointed and staged.
- **Defaults:** 300 seconds, 1,000,000 bytes of command text, stdin, and output in `coding`.
- **Hard maximums:** 900 seconds; 1,000,000 command bytes; 8,000,000 stdin/output bytes.
- **Execution:** `/bin/bash -lc`, non-interactive, no network, restricted environment, process-group cleanup.
- **Environment:** a fixed base environment (`PATH`, `HOME`, locale, UTF-8 Python flags) is always applied. User-supplied `env` is limited to 64 variables with POSIX-identifier names and ≤16,384-byte values; `PATH`, `HOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, and `BASH_ENV*` are refused.
- **Output:** stdout/stderr is bounded; oversized output is retained as a bounded head/tail preview with a truncation marker. Command event cursors are exposed separately through the executor event endpoint.
- **Failure:** timed-out and cancelled commands always roll staged filesystem changes back to their checkpoint. Non-zero-exit commands roll back by default; set `rollback_on_failure: false` when retaining partial progress is intentional. Results separate the process `exit_code` from checkpoint outcome with `rolled_back` and `rollback_reason` (`nonzero_exit` or `none`).

## `grep`

- **Purpose:** search file contents.
- **Arguments:** required `query`; optional `path`, `regex`, `case_sensitive`, `include_glob`, `exclude_glob`, `max_results` (1–5000), `context_lines` (0–5), `include_metadata`, and `cursor`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results, a 64 MiB per-file scan budget, a 1,000,000-byte output budget, and a 30-second time budget in `coding`.
- **Hard maximums:** 5,000 results, 256 MiB scan budget, 4,000,000 output bytes, and 300 seconds.
- **Budgets:** the scan budget (`max_grep_scan_bytes`) only skips candidate files larger than it (reported as `files_skipped_too_large`); the output budget (`max_grep_output_bytes`) independently clips the combined output (reported as `output_truncated`); the time budget (`max_search_seconds`, overridable with `LOCAL_CHAT_MAX_SEARCH_SECONDS`) stops the scan and returns partial results with `truncation_reason: "time_budget"`.
- **Semantics:** queries are literal text by default and regular expressions with `regex`; matches report path, line number, and a 500-character line excerpt, with optional context lines and metadata. Secret-like paths and `.local-chat-*` directories are skipped.
- **Truncation/cursors:** bounded result sets report truncation/limits and may return `next_cursor`. Search scope should be constrained with `path` and globs when appropriate; the scan budget is a resource limit, not a claim that every byte of an arbitrarily large corpus is always scanned.

## `find`

- **Purpose:** recursively discover files and directories.
- **Arguments:** optional `path`, `glob`, `max_depth` (0–20, default 10), `max_results` (1–2000), and `details`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results when `max_results` is omitted and a 30-second time budget in `coding`.
- **Hard maximum:** 2,000 results and 300 seconds; traversal is independently bounded by depth/path rules.
- **Time budget:** the walk shares `max_search_seconds` with `grep`. When it expires the partial listing is returned with `truncated: true` and `truncation_reason: "time_budget"` and no cursor; narrow `path`, `glob`, or `max_depth` instead of retrying.
- **Truncation:** a result-count cutoff reports `truncation_reason: "result_limit"` and a `next_cursor`.

## `ls`

- **Purpose:** list immediate directory contents.
- **Arguments:** optional `path`, `max_results` (1–2000), and `details`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results when `max_results` is omitted and a 30-second time budget in `coding`.
- **Hard maximum:** 2,000 results and 300 seconds.
- **Time budget:** the listing shares `max_search_seconds` with `grep`. When it expires the entries seen so far are returned with `truncated: true` and `truncation_reason: "time_budget"` and no cursor, because unvisited entries may sort before them.
- **Truncation:** a result-count cutoff reports `truncation_reason: "result_limit"` and a `next_cursor`.
- **Semantics:** immediate listing only; it does not recursively enumerate the whole tree.

## `status`

- **Purpose:** review everything staged for publication, independent of Git (`.git` is never staged).
- **Arguments:** optional `paths` (1–50 relative files or directories to restrict the report to), `include_diffs` (default false), `max_results` (1–500, default 100), and `cursor`.
- **Modes:** Agent only.
- **Permission:** read-only; never prompts.
- **Semantics:** compares the staged workspace against the publication baseline (the snapshot the next manifest is validated against), so it shows exactly what would be published: created, modified, deleted, and permission-changed files with base/staged hashes, sizes, and modes. Available for empty and non-empty staging. Secret-like paths, dependency/cache directories, and executor `.local-chat-*` metadata (snapshot and checkpoints) are never reported or diffed.
- **Publication metadata:** `publication_batches` is how many sequential, separately approved publication batches the pending changes need (`null` if a single file exceeds one batch), with `publication_batch_limits`.
- **Diffs:** with `include_diffs`, unified diffs against the baseline content (resolved by hash from the checkpoint object store or the read-only source) are returned for the current page, bounded to 64,000 bytes and 800 lines in aggregate; files over 1,000,000 bytes, binary files, mode-only changes, and unavailable baselines are reported by kind without text.
- **Truncation/cursors:** `returned`, `total_known`, `truncated`, and `next_cursor` page through large change sets.

## `investigate_repository`

- **Purpose:** delegate a bounded repository investigation to a cheaper model.
- **Arguments:** `query` required; there are no separate path or hint arguments. Put the complete investigation request in `query`.
- **Request guidance:** include relevant symptoms, error messages, suspected components or files, hypotheses, desired scope, and any other context that can help the investigator focus its search. Do not encode path hints separately; describe them naturally in the request. The investigation model decides whether to use `read`, `grep`, `find`, or `ls` and how to scope those tools.
- **Modes:** Agent only.
- **Permission:** read-only; it cannot mutate, execute commands, checkpoint, or publish, and it never requires an approval prompt.
- **Model:** uses the investigation model configured in Settings (default `xiaomi/mimo-v2.5`); the primary model cannot select or override it.
- **Budget:** each call receives at most 50% of the parent run's remaining cost (calls fail closed below a $0.01 floor) and inherits bounded iteration, tool-call (36/36 caps), and wall-time limits from the parent's remaining budgets. Wall time is the parent's remaining wall time clamped to 10–300 seconds, so one investigation can never consume most of a long parent run. Up to four calls are accepted per turn (the allowance resets at the start of every parent model turn), and a failed call still counts toward that turn's cap.
- **Tools:** the nested investigation loop is restricted to `read`, `grep`, `find`, and `ls` through the parent's executor session, so it observes current staged state. Non-Plan tool calls inside the loop are rejected in code.
- **Synthesis:** the harness reserves the final iteration and tool-call slot to force a summary instead of further exploration.
- **Result:** returns `summary`, `files_examined`, and `truncated`; nested `subagent.*` events remain in the journal for replay/audit. On failure the parent is told to fall back to direct tool use.

## Modes and permissions

| Tool | Plan | Agent | Writes staging? |
|---|---|---|---|
| `read` | yes | yes | no |
| `write` | no | yes | yes |
| `edit` | no | yes | yes |
| `patch` | no | yes | yes |
| `bash` | no | yes | potentially |
| `grep` | yes | yes | no |
| `find` | yes | yes | no |
| `ls` | yes | yes | no |
| `status` | no | yes | no |
| `investigate_repository` | no | yes | no |

Read-only tools run without approval prompts in both prompt and Auto sessions; mutation and command tools require approval under the `prompt` policy and are auto-allowed under `auto`. Plan-mode mutation denial is enforced twice: once in `shared/tools.py` request validation and again by `executor/permissions.py`.

The public ten-tool model-facing API includes the scoped read-only `investigate_repository` tool.

## Concurrency within a turn

When one model turn issues several calls, consecutive independent read-only calls (`read`, `grep`, `find`, `ls`, `status`) run concurrently (at most 8 at a time) and the executor serves them off its event loop. Mutations (`write`, `edit`, `patch`, `bash`) and `investigate_repository` run one at a time in their original order, so each keeps its own checkpoint and a read issued after a write observes it. Results are always returned to the model in the original call order. Check `shared/tools.py` and `server/openrouter/agent.py` when modifying schemas or dispatch.


## Permission matching

Permission scopes use case-insensitive canonical relative paths with both slash styles accepted. Invalid, absolute, drive, UNC, and traversal paths do not match a rule and remain subject to executor validation. When multiple non-restrictive rules match, the longest normalized path prefix wins; restrictive decisions retain the ordering `DENY > ASK > ALLOW`.

Approval prompts are bounded by the active agent run's remaining wall-clock budget. An unanswered prompt produces an `approval.timeout` event and fails the run rather than waiting indefinitely.
