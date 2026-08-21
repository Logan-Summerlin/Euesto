# Tools

This is the authoritative human-readable reference for the eight model-facing tools. The source of truth for dispatch is `shared/tools.py`; the model-facing JSON schemas live in `server/openrouter/agent.py`; executor limits and ceilings live in `executor/config.py`. Changes to one must be checked against the others.

## Common contract

Every executor request contains `request_id`, `run_id`, `tool`, `mode`, and an object-valued `arguments` field. `mode` is `plan` or `agent`. Tool requests are rejected when the tool is unknown, when Plan requests a mutation, or when serialized arguments exceed 512 KiB.

Every result contains `request_id`, `ok`, `output`, `data`, `error_code`, `truncated`, `elapsed_seconds`, `returned`, `total_known`, `limit`, and `next_cursor`. Optional counts are non-negative integers. Errors are classified and returned rather than exposing arbitrary exception details to the model.

All tools operate on relative POSIX paths that are normalized and contained beneath the workspace root. Absolute, drive, UNC, traversal (`..`), Windows-alias, reserved-DOS-name, non-canonical-Unicode, and secret-like paths are rejected, as are symlinks and hard-linked files. Only UTF-8 text is readable and writable; binary content is refused.

## `read`

- **Purpose:** inspect UTF-8 text.
- **Arguments:** `path` (required); optional `start_line`, `end_line`, `offset`, and `max_bytes`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Defaults:** each call returns at most 64,000 bytes unless `max_bytes` raises it; the tool clamps any request to 256,000 bytes per call regardless of the configured profile limit.
- **Hard maximum:** 8,000,000 bytes at the protocol/config layer; the 256,000-byte per-call tool ceiling applies on top.
- **Semantics:** Plan reads the read-only source snapshot; Agent reads staged workspace content. Line ranges and byte offsets cannot be combined. Reads past the end of the file are rejected rather than returning an empty result, and byte offsets must land on UTF-8 character boundaries.
- **Truncation/cursors:** results report `truncated`, `byte_offset`, `next_offset`, and `next_start_line`; continue with the next line range or offset instead of assuming a whole file fits in one response.
- **Errors:** invalid path, invalid range, invalid offset, non-UTF-8 or binary content, missing file, and resource-limit failures are classified.

## `write`

- **Purpose:** create or replace one UTF-8 text file.
- **Arguments:** `path`, `content` required; optional `expected_sha256`, `create_parents`.
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged.
- **Default:** 1,000,000-byte file content limit in `coding`.
- **Hard maximum:** 8,000,000 bytes.
- **Hash:** `expected_sha256` is optional. When supplied it is an optimistic concurrency check; it is not a mandatory compatibility requirement.
- **Shrink guard:** replacing an existing file (≥200 bytes and ≥20 lines) with less than half its bytes *and* lines is rejected so a whole-file clobber cannot pass silently; retry deliberately after reviewing.
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
- **Semantics:** replacement streams through a temporary file and is swapped in atomically; the actual occurrence count must equal `expected_occurrences` (default 1) or the edit fails. The same shrink guard as `write` applies to large proportional reductions.
- **Localized edits:** exact replacement avoids rewriting unrelated files; target/result limits make larger files incrementally inspectable and locally editable.
- **Failure:** checkpoint restoration occurs on failed mutation.

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
- **Failure:** timed-out, cancelled, and non-zero-exit commands roll staged filesystem changes back to their checkpoint (`rolled_back` is reported).

## `grep`

- **Purpose:** search file contents.
- **Arguments:** required `query`; optional `path`, `regex`, `case_sensitive`, `include_glob`, `exclude_glob`, `max_results` (1–5000), `context_lines` (0–5), `include_metadata`, and `cursor`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results and a 64 MiB scan budget in `coding`.
- **Hard maximums:** 5,000 results and 256 MiB scan budget.
- **Semantics:** queries are literal text by default and regular expressions with `regex`; matches report path, line number, and a 500-character line excerpt, with optional context lines and metadata. Secret-like paths and `.local-chat-*` directories are skipped.
- **Truncation/cursors:** bounded result sets report truncation/limits and may return `next_cursor`. Search scope should be constrained with `path` and globs when appropriate; the scan budget is a resource limit, not a claim that every byte of an arbitrarily large corpus is always scanned.

## `find`

- **Purpose:** recursively discover files and directories.
- **Arguments:** optional `path`, `glob`, `max_depth` (0–20, default 10), `max_results` (1–2000), and `details`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results in `coding`.
- **Hard maximum:** 2,000 results; traversal is independently bounded by depth/path rules.
- **Truncation:** bounded listings report limits/truncation where applicable.

## `ls`

- **Purpose:** list immediate directory contents.
- **Arguments:** optional `path`, `max_results` (1–2000), and `details`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results in `coding`.
- **Hard maximum:** 2,000 results.
- **Semantics:** immediate listing only; it does not recursively enumerate the whole tree.

## `investigate_repository`

- **Purpose:** delegate a bounded repository investigation to a cheaper model.
- **Arguments:** `query` required; there are no separate path or hint arguments. Put the complete investigation request in `query`.
- **Request guidance:** include relevant symptoms, error messages, suspected components or files, hypotheses, desired scope, and any other context that can help the investigator focus its search. Do not encode path hints separately; describe them naturally in the request. The investigation model decides whether to use `read`, `grep`, `find`, or `ls` and how to scope those tools.
- **Modes:** Agent only.
- **Permission:** read-only; it cannot mutate, execute commands, checkpoint, or publish, and it never requires an approval prompt.
- **Model:** uses the investigation model configured in Settings (default `xiaomi/mimo-v2.5`); the primary model cannot select or override it.
- **Budget:** each call receives at most 50% of the parent run's remaining cost (calls fail closed below a $0.01 floor) and inherits bounded iteration, tool-call (36/36 caps), and wall-time limits from the parent's remaining budgets; at most two calls are accepted per turn, and a failed call still counts toward the cap.
- **Tools:** the nested investigation loop is restricted to `read`, `grep`, `find`, and `ls` through the parent's executor session, so it observes current staged state. Non-Plan tool calls inside the loop are rejected in code.
- **Synthesis:** the harness reserves the final iteration and tool-call slot to force a summary instead of further exploration.
- **Result:** returns `summary`, `files_examined`, and `truncated`; nested `subagent.*` events remain in the journal for replay/audit. On failure the parent is told to fall back to direct tool use.

## Modes and permissions

| Tool | Plan | Agent | Writes staging? |
|---|---|---|---|
| `read` | yes | yes | no |
| `write` | no | yes | yes |
| `edit` | no | yes | yes |
| `bash` | no | yes | potentially |
| `grep` | yes | yes | no |
| `find` | yes | yes | no |
| `ls` | yes | yes | no |
| `investigate_repository` | no | yes | no |

Read-only tools run without approval prompts in both prompt and Auto sessions; mutation and command tools require approval under the `prompt` policy and are auto-allowed under `auto`. Plan-mode mutation denial is enforced twice: once in `shared/tools.py` request validation and again by `executor/permissions.py`.

The public eight-tool model-facing API includes the scoped read-only `investigate_repository` tool. Check `shared/tools.py` and `server/openrouter/agent.py` when modifying schemas or dispatch.
