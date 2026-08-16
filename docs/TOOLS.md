# Tools

This is the authoritative human-readable reference for the seven model-facing executor tools. The source of truth for dispatch is `shared/tools.py`; the model-facing JSON schemas live in `server/openrouter/agent.py`; executor limits and ceilings live in `executor/config.py`. Changes to one must be checked against the others.

## Common contract

Every executor request contains `request_id`, `run_id`, `tool`, `mode`, and an object-valued `arguments` field. `mode` is `plan` or `agent`. Tool requests are rejected when the tool is unknown, when Plan requests a mutation, or when serialized arguments exceed 512 KiB.

Every result contains `request_id`, `ok`, `output`, `data`, `error_code`, `truncated`, `elapsed_seconds`, `returned`, `total_known`, `limit`, and `next_cursor`. Optional counts are non-negative integers. Errors are classified and returned rather than exposing arbitrary exception details to the model.

## `read`

- **Purpose:** inspect UTF-8 text.
- **Arguments:** `path` (required); optional `start_line`, `end_line`, and `max_bytes`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** executor-configured read limit (1,000,000 bytes in the `coding` profile).
- **Hard maximum:** 8,000,000 bytes.
- **Semantics:** Plan reads the read-only source snapshot; Agent reads staged workspace content.
- **Truncation/cursors:** the result may report truncation; incremental reads should use line ranges or byte/range arguments supported by the implementation rather than assuming a whole file fits in one response.
- **Errors:** invalid path, invalid range, non-UTF-8 content, missing file, and resource-limit failures are classified.

## `write`

- **Purpose:** create or replace one UTF-8 text file.
- **Arguments:** `path`, `content` required; optional `expected_sha256`, `create_parents`.
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged.
- **Default:** 1,000,000-byte file content limit in `coding`.
- **Hard maximum:** 8,000,000 bytes.
- **Hash:** `expected_sha256` is optional. When supplied it is an optimistic concurrency check; it is not a mandatory compatibility requirement.
- **Source/staging:** writes target the writable staging tree, never the read-only source mount.
- **Failure:** checkpoint restoration occurs before an unsuccessful mutation is returned.

## `edit`

- **Purpose:** replace an exact string in one UTF-8 text file.
- **Arguments:** `path`, `old_str`, `new_str` required; optional `expected_occurrences` (1–1000) and `expected_sha256`.
- **Modes:** Agent only.
- **Permission:** mutation; checkpointed and staged.
- **Defaults:** 2,000,000-byte target and result limits in `coding`.
- **Hard maximums:** 16,000,000 bytes for target and result.
- **Hash:** optional optimistic concurrency check.
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
- **Output:** stdout/stderr is bounded and may be truncated; command event cursors are exposed separately through the executor event endpoint.
- **Failure:** failed, cancelled, or timed-out commands roll staged filesystem changes back.

## `grep`

- **Purpose:** search file contents.
- **Arguments:** required `query`; optional `path`, `regex`, `case_sensitive`, `include_glob`, `exclude_glob`, `max_results` (1–5000), `context_lines` (0–5), `include_metadata`, and `cursor`.
- **Modes:** Plan and Agent.
- **Permission:** read-only.
- **Default:** 500 results and a 64 MiB scan budget in `coding`.
- **Hard maximums:** 5,000 results and 256 MiB scan budget.
- **Truncation/cursors:** bounded result sets report truncation/limits and may return `next_cursor`. Search scope should be constrained with `path` and globs when appropriate; the scan budget is a resource limit, not a claim that every byte of an arbitrarily large corpus is always scanned.

## `find`

- **Purpose:** recursively discover files and directories.
- **Arguments:** optional `path`, `glob`, `max_depth` (0–20), `max_results` (1–2000), and `details`.
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

The public seven-tool API is unchanged by this documentation. Check `shared/tools.py` and `server/openrouter/agent.py` when modifying schemas or dispatch.
