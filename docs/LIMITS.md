# Resource Limits

Limits below describe the current `coding` executor profile. `executor/config.py` is authoritative. A request cannot exceed the configured profile value or its hard ceiling, whichever is lower. Container/runtime capacity can impose an additional outer limit.

| Area | Coding default | Hard maximum | Notes |
|---|---:|---:|---|
| `read` bytes | 1,000,000 | 8,000,000 | Exact byte values: 1,000,000 / 8,000,000. Requested value is clamped to effective limit. The `read` tool additionally clamps every call to 256,000 bytes (64,000 default) regardless of profile. |
| `write` bytes | 1,000,000 | 8,000,000 | Exact byte values: 1,000,000 / 8,000,000. UTF-8 text content. |
| `edit` target | 2,000,000 | 16,000,000 | Exact byte values: 2,000,000 / 16,000,000. Target file size. |
| `edit` result | 2,000,000 | 16,000,000 | Exact byte values: 2,000,000 / 16,000,000. Resulting file size. |
| Bash command | 1,000,000 bytes | 1,000,000 bytes | Exact byte values: 1,000,000 / 1,000,000. Hard ceiling equals default. |
| Bash stdin | 1,000,000 bytes | 8,000,000 bytes | Exact byte values: 1,000,000 / 8,000,000. Schema also bounds stdin to 8,000,000 characters. |
| Bash output | 1,000,000 bytes | 8,000,000 bytes | Exact byte values: 1,000,000 / 8,000,000. Output is bounded/truncatable. |
| Bash timeout | 300 s | 900 s | Exact seconds: 300 / 900. Non-interactive process group. |
| `grep` results | 500 | 5,000 | Exact counts: 500 / 5,000. Separate scan-byte budget. |
| `grep` scan | 64 MiB (64,000,000 bytes) | 256 MiB (256,000,000 bytes) | Exact decimal byte values: 64,000,000 / 256,000,000. Search work budget. |
| `find` results | 500 | 2,000 | Exact counts: 500 / 2,000. Depth also capped at 20 by schema. |
| `ls` results | 500 | 2,000 | Exact counts: 500 / 2,000. Immediate directory only. |
| Staged files | 300,000 | 1,000,000 | Exact counts: 300,000 / 1,000,000. Shared staging resource. |
| Staging bytes | 2.5 GB (2,500,000,000 bytes) | 4 GB (4,000,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 4,000,000,000. Must fit the work-volume resource model. |
| Checkpoint bytes | 2.5 GB (2,500,000,000 bytes) | 3.5 GB (3,500,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 3,500,000,000. Shares work-volume capacity with staging/temp headroom. |
| Work capacity | 8 GB (8,000,000,000 bytes) | 8 GB (8,000,000,000 bytes) | Exact configured/ceiling value: 8,000,000,000 bytes. Actual container capacity must be greater than configured capacity and required headroom. |
| Required temp headroom | 1 GB (1,000,000,000 bytes) | 1 GB (1,000,000,000 bytes) | Fixed resource-model constant; staging + checkpoint + headroom must fit strictly below work capacity. |

## Agent run budgets

Separate from executor limits, each Agent run is bounded by a budget profile (`server/agent/budgets.py`):

| Profile | Iterations | Tool calls | Wall time | Cost |
|---|---:|---:|---:|---:|
| `coding` (default) | 600 | 900 | 1,800 s | $2.00 |
| `extended-coding` | 1,200 | 1,800 | 3,600 s | $4.00 |
| `large-coding` | 1,800 | 2,700 | 5,400 s | $8.00 |

A profile that exceeds twice the standard `coding` value on tool calls, wall time, or cost requires explicit user approval before the session runs. Investigation delegation adds its own caps of at most two calls per turn and 36 iterations / 36 tool calls per nested loop, debited against the parent run's remaining budget.

## Profiles

The `small` profile reduces file/output/search limits. `coding` is the default. `large-workspace` raises selected limits while remaining below hard ceilings. Environment variables named `LOCAL_CHAT_<LIMIT>` can override profile values, but `ExecutorConfig` rejects values above hard ceilings or resource-model capacity.

## Which limit wins?

1. The tool schema rejects structurally invalid requests before execution.
2. The protocol rejects oversized serialized arguments above 512 KiB.
3. Executor `effective_limit()` applies the requested value, configured profile value, and hard ceiling; the minimum wins.
4. Tool-specific filesystem/path/security rules can reject an operation independently of byte/count limits.
5. Staging/checkpoint and work-volume resource limits apply to mutations and may reject an otherwise valid operation.
6. Docker CPU, memory, PID, disk, and network isolation are outer containment controls; they never expand application limits.

The effective limits reported by `/v1/status` are intended to make the active profile and source of each configured value inspectable.

## Investigation delegation

Investigation delegation is bounded to two calls per turn and half of the parent run's remaining cost per call, with 36-iteration and 36-tool-call nested caps; it never creates an executor, staging, or publication authority.
