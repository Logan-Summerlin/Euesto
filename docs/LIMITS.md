# Resource Limits

Limits below describe the current `coding` executor profile. `executor/config.py` is authoritative. A request cannot exceed the configured profile value or its hard ceiling, whichever is lower. Container/runtime capacity can impose an additional outer limit.

| Area | Coding default | Hard maximum | Notes |
|---|---:|---:|---|
| `read` bytes | 1,000,000 | 8,000,000 | Requested value is clamped to effective limit. |
| `write` bytes | 1,000,000 | 8,000,000 | UTF-8 text content. |
| `edit` target | 2,000,000 | 16,000,000 | Target file size. |
| `edit` result | 2,000,000 | 16,000,000 | Resulting file size. |
| Bash command | 1,000,000 bytes | 1,000,000 bytes | Hard ceiling equals default. |
| Bash stdin | 1,000,000 bytes | 8,000,000 bytes | Schema also bounds stdin to 8,000,000 characters. |
| Bash output | 1,000,000 bytes | 8,000,000 bytes | Output is bounded/truncatable. |
| Bash timeout | 300 s | 900 s | Non-interactive process group. |
| `grep` results | 500 | 5,000 | Separate scan-byte budget. |
| `grep` scan | 64 MiB | 256 MiB | Search work budget. |
| `find` results | 500 | 2,000 | Depth also capped at 20 by schema. |
| `ls` results | 500 | 2,000 | Immediate directory only. |
| Tool arguments | 512 KiB | 512 KiB | Shared protocol-level request bound. |
| Staged files | 300,000 | 1,000,000 | Shared staging resource. |
| Staging bytes | 2.5 GB (2,500,000,000 bytes) | 4 GB | Must fit the work-volume resource model. |
| Checkpoint bytes | 2.5 GB (2,500,000,000 bytes) | 3.5 GB | Shares work-volume capacity with staging/temp headroom. |
| Work capacity | 8 GB configured | 8 GB configured ceiling | Actual container capacity must be greater than configured capacity and required headroom. |
| Temporary headroom | 1 GB | 1 GB | Reserved by the resource model. |

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
