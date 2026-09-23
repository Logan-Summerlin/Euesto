# Resource Limits

Limits below describe the current `coding` executor profile. `executor/config.py` is authoritative. A request cannot exceed the configured profile value or its hard ceiling, whichever is lower. Container/runtime capacity can impose an additional outer limit.

| Area | Coding default | Hard maximum | Notes |
|---|---:|---:|---|
| `read` bytes | 1,000,000 | 8,000,000 | Exact byte values: 1,000,000 / 8,000,000. Requested value is clamped to effective limit. The `read` tool additionally clamps every call to 256,000 bytes (64,000 default) regardless of profile. |
| `write` bytes | 1,000,000 | 8,000,000 | Exact byte values: 1,000,000 / 8,000,000. UTF-8 text content. |
| `edit` target | 2,000,000 | 16,000,000 | Exact byte values: 2,000,000 / 16,000,000. Target file size. |
| `edit` result | 2,000,000 | 16,000,000 | Exact byte values: 2,000,000 / 16,000,000. Resulting file size. |
| `apply_patch` operations | 100 | 500 | Exact counts: 100 / 500 (`max_patch_operations`). Operations per atomic patch. |
| `apply_patch` content | 2,000,000 bytes | 16,000,000 bytes | Exact byte values: 2,000,000 / 16,000,000 (`max_patch_bytes`). Combined `content`/`old_str`/`new_str` across all operations; each operation also obeys the `write`/`edit` limits. |
| Bash command | 1,000,000 bytes | 1,000,000 bytes | Exact byte values: 1,000,000 / 1,000,000. Hard ceiling equals default. |
| Bash stdin | 1,000,000 bytes | 8,000,000 bytes | Exact byte values: 1,000,000 / 8,000,000. Schema also bounds stdin to 8,000,000 characters. |
| Bash output | 1,000,000 bytes | 8,000,000 bytes | Exact byte values: 1,000,000 / 8,000,000. Output is bounded/truncatable. |
| Bash timeout | 300 s | 900 s | Exact seconds: 300 / 900. Non-interactive process group. |
| `grep` results | 500 | 5,000 | Exact counts: 500 / 5,000. Separate scan-byte budget. |
| `grep` scan | 64 MiB (64,000,000 bytes) | 256 MiB (256,000,000 bytes) | Exact decimal byte values: 64,000,000 / 256,000,000. Per-file size above which a candidate file is skipped (`files_skipped_too_large`). |
| `grep` output | 1,000,000 bytes | 4,000,000 bytes | Exact byte values: 1,000,000 / 4,000,000 (`max_grep_output_bytes`). Clips the combined output independently of the scan budget. |
| Search time (`grep`, `find`, `ls`) | 30 s | 300 s | Exact seconds: 30 / 300 (`max_search_seconds`, `LOCAL_CHAT_MAX_SEARCH_SECONDS`). Partial results report `truncation_reason: "time_budget"`. |
| `find` results | 500 | 2,000 | Exact counts: 500 / 2,000. Also the default when `max_results` is omitted. Depth also capped at 20 by schema. |
| `ls` results | 500 | 2,000 | Exact counts: 500 / 2,000. Also the default when `max_results` is omitted. Immediate directory only. |
| `status` results | 100 | 500 | Per-page change entries; `cursor` continues. Diffs share 64,000 bytes / 800 lines per call and skip files over 1,000,000 bytes. |
| Parallel read-only calls | 8 | 8 | Consecutive `read`/`grep`/`find`/`ls`/`status` calls in one turn run concurrently, at most 8 at a time (`MAX_PARALLEL_TOOL_CALLS`). |
| Tool arguments (protocol) | 17,000,000 bytes | 17,000,000 bytes | `MAX_TOOL_ARGUMENT_BYTES` in `shared/tools.py`, measured as unescaped UTF-8 JSON. Derived as the largest argument-carrying hard ceiling (16,000,000 bytes: the `edit` result, which also bounds `old_str` + `new_str` together, and `apply_patch` content; `write` needs 8,000,000 and `bash` command + stdin + env about 10,050,000) plus a 1,000,000-byte envelope, so it never binds below a per-tool limit in any profile. |
| Staged files | 300,000 | 1,000,000 | Exact counts: 300,000 / 1,000,000. Shared staging resource. |
| Staging bytes | 2.5 GB (2,500,000,000 bytes) | 4 GB (4,000,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 4,000,000,000. Must fit the work-volume resource model. |
| Checkpoint bytes | 2.5 GB (2,500,000,000 bytes) | 3.5 GB (3,500,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 3,500,000,000. Shares work-volume capacity with staging/temp headroom. |
| Work capacity | 8 GB (8,000,000,000 bytes) | 8 GB (8,000,000,000 bytes) | Exact configured/ceiling value: 8,000,000,000 bytes. Actual container capacity must be greater than configured capacity and required headroom. |
| Required temp headroom | 1 GB (1,000,000,000 bytes) | 1 GB (1,000,000,000 bytes) | Fixed resource-model constant; staging + checkpoint + headroom must fit strictly below work capacity. |

## Publication batches

The desktop broker publishes at most 500 operations and 32,000,000 bytes of staged content per manifest (`PUBLISH_BATCH_MAX_OPERATIONS` / `PUBLISH_BATCH_MAX_BYTES` in `shared/tools.py`). A larger changeset is published as ordered batches of up to 2,000 (`MAX_PUBLISH_BATCHES`), each separately approved and hash-validated; a single file larger than one batch cannot be published. Binary content counts its decoded bytes. See `docs/PUBLICATION.md`.

## Agent run budgets

Separate from executor limits, each Agent run is bounded by a budget profile (`server/agent/budgets.py`):

| Profile | Iterations | Tool calls | Wall time | Cost |
|---|---:|---:|---:|---:|
| `coding` (default) | 600 | 900 | 1,800 s | $2.00 |
| `extended-coding` | 1,200 | 1,800 | 3,600 s | $4.00 |
| `large-coding` | 1,800 | 2,700 | 5,400 s | $8.00 |

A profile that exceeds twice the standard `coding` value on tool calls, wall time, or cost requires explicit user approval before the session runs. Investigation delegation adds its own caps of up to four calls per turn and 36 iterations / 36 tool calls / 300 seconds of wall time per nested loop, debited against the parent run's remaining budget.

## Profiles

The `small` profile reduces file/output/search limits. `coding` is the default. `large-workspace` raises selected limits while remaining below hard ceilings. Environment variables named `LOCAL_CHAT_<LIMIT>` can override profile values, but `ExecutorConfig` rejects values above hard ceilings or resource-model capacity.

## Which limit wins?

1. The tool schema rejects structurally invalid requests before execution.
2. The protocol rejects arguments above the 17,000,000-byte `MAX_TOOL_ARGUMENT_BYTES` cap. It sits above every per-tool argument ceiling, so in practice step 3 decides.
3. Executor `effective_limit()` applies the requested value, configured profile value, and hard ceiling; the minimum wins.
4. Tool-specific filesystem/path/security rules can reject an operation independently of byte/count limits.
5. Staging/checkpoint and work-volume resource limits apply to mutations and may reject an otherwise valid operation.
6. Docker CPU, memory, PID, disk, and network isolation are outer containment controls; they never expand application limits.

The effective limits reported by `/v1/status` are intended to make the active profile and source of each configured value inspectable.

## Investigation delegation

Investigation delegation accepts up to 50 `inspected_paths` per call and returns at most 50 structured findings. It is bounded to four calls per turn (reset at every parent model turn) and half of the parent run's remaining cost per call, with 36-iteration, 36-tool-call, and 300-second wall-time nested caps (the wall time is the parent's remaining time clamped to 10–300 seconds); it never creates an executor, staging, or publication authority.
