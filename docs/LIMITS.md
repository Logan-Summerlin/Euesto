# Resource Limits

Limits below describe the current `coding` executor profile. `executor/config.py` is authoritative. A request cannot exceed the configured profile value or its hard ceiling, whichever is lower. Container/runtime capacity can impose an additional outer limit.

| Area | Coding default | Hard maximum | Notes |
|---|---:|---:|---|
| `read` bytes | 1,000,000 | 8,000,000 | Exact byte values: 1,000,000 / 8,000,000. Requested value is clamped to effective limit. Each call returns 32,000 bytes unless `max_bytes` asks for more, up to the profile value. |
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
| Staging bytes | 2.5 GB (2,500,000,000 bytes) | 4 GB (4,000,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 4,000,000,000. Seeding also requires that much free space on the work volume. |
| Checkpoint bytes | 2.5 GB (2,500,000,000 bytes) | 3.5 GB (3,500,000,000 bytes) | Exact decimal byte values: 2,500,000,000 / 3,500,000,000. Each checkpoint also requires free space on the state volume beyond its size plus the 1 GB temp headroom. |
| Work capacity | 8 GB (8,000,000,000 bytes) | 8 GB (8,000,000,000 bytes) | Exact configured/ceiling value: 8,000,000,000 bytes. Actual container capacity must be greater than configured capacity and required headroom. |
| Required temp headroom | 1 GB (1,000,000,000 bytes) | 1 GB (1,000,000,000 bytes) | Fixed resource-model constant; staging + checkpoint + headroom must fit strictly below work capacity. |

Staging, state, and caches live on persistent named volumes (`docs/ARCHITECTURE.md`), so capacity checks use the free space the volume reports rather than a fixed tmpfs size; the staging and checkpoint budgets above still apply.

## Model-facing result bounds

The gateway bounds each tool result it gives the model, independently of the executor limits above (`BudgetProfile.result_cap` in `server/agent/budgets.py`). There is no cumulative per-run allowance: compaction governs the total. A shortened result emits `tool.result_bounded` with its original size.

| Result | `coding` | `extended-coding` | `large-coding` |
|---|---:|---:|---:|
| `read` | 32,000 bytes | 48,000 bytes | 64,000 bytes |
| Every other tool | 24,000 bytes | 32,000 bytes | 48,000 bytes |
| `bash` output shown | 8,000 characters | 12,000 characters | 16,000 characters |
| Ceiling for any result | 10% of the working set (38,400 bytes) | 64,000 bytes | 102,400 bytes |

The ceiling counts four bytes per token. `investigate_repository` keeps its own, separate allowance (below).

## Context, output, and steering

| Bound | Value | Notes |
|---|---:|---|
| Working set (compaction high-water) | 96,000 tokens (`coding`), 160,000 (`extended-coding`), 256,000 (`large-coding`) | The smaller of this and 60% of the model's window applies; compaction returns to a third of it, and never leaves more than 90% of the window. |
| Handoff summary | 2,500 tokens | One tool-free turn at low reasoning effort. |
| Output per model turn (`max_tokens`) | 16,000 tokens | Per profile; a cut-off turn gets a note to write large files in parts. |
| Static system prompt | 4,800 bytes | Per mode; no dynamic values. |
| Session context | 48,000 characters | Workspace instructions 32,000 characters, `AGENTS.md` 24,000 bytes, skills 16,000 characters, conversation prompt 8,000 characters, executor facts 2,000 characters, workspace snapshot 6,000 bytes. |
| Steering message | 8,000 characters | At most 16 queued per run. |
| Provider attempts | 3 per model turn | Jittered backoff; a final retryable failure pauses the run. |

## Runner resources

Commands run in the runner container. Its limits are settings (Settings → Agent → Runner) within ceilings; a value outside its ceiling fails closed instead of being clamped.

| Setting | Default | Range |
|---|---:|---:|
| CPUs | Docker's CPU count minus one, within the range | 2–8 |
| Memory | Half of Docker's memory, within the range | 2–16 GB |
| Process limit (PIDs) | 1,024 | 256–4,096 |
| Git history depth | 200 commits | 0 (full history) – 100,000 |

The control plane keeps small fixed limits (1 CPU, 1 GB of memory, 128 PIDs). Staged sessions unused for 30 days (a setting, 1–365) are flagged in Settings for removal; nothing is removed without confirmation.

## Command output artifacts

Each command's complete output is written to `.euesto-output/` in staging. Later commands can read it; the file tools, checkpoints, and publication never include it. A log is cut at 16,000,000 bytes; the directory keeps at most 200 logs, 64,000,000 bytes, and 24 hours of history, pruning the oldest first. The runner returns at most 512,000 bytes of output to the executor per command (a head and tail preview), and the reduction of test and compiler output that leads a result is at most 3,000 characters.

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

## Long runs

| Bound | Value | Notes |
|---|---:|---|
| Streamed turn idle timeout | 120 s | Longest wait between streamed chunks (`turn_idle_seconds`), every profile. |
| Streamed turn ceiling | 600 s | One model turn (`turn_ceiling_seconds`), never longer than the run's remaining wall time. |
| Live text journaling | every 0.5 s or 4,000 characters | `model.stream` events; at most 64,000 characters per turn, shown live and not saved as the answer. |
| Wind-down warning | 85% of any budget | One `loop.note` (kind `budget_warning`) asking the agent to wrap up. |
| Wind-down reserve | 5% of each budget, at most 60 s, plus 1 iteration | Funds the tool-free summary turn; the run then pauses with reason `budget`. |
| Repeated failures | 3 | The same call failing the same way three times adds a note to change approach. |
| No-progress turns | 4 | Turns with no new tool output and no new staged change before the wind-down starts. |
| Empty replies | 1 re-prompt | A second empty or reasoning-only reply pauses the run. |
| `update_plan` | 20 steps; `id` 40, `title` 120, `note` 200 characters | Stored per session; the result is one line. |

*Continue with another budget* resumes a run paused at its budget with a fresh budget of the same profile; profiles above twice the standard budget still need approval.

## Workspace policy, checks, and setup

| Bound | Value | Notes |
|---|---:|---|
| Policy file | 64,000 bytes | `.euesto/policy.toml` in the host workspace; larger files are invalid. |
| Declared checks | 12 | Names are lowercase identifiers of at most 32 characters; commands at most 1,000 characters. |
| Check timeout | 300 s default, 900 s maximum | Runs in the runner like Bash and always restores staging; the result keeps the last 16,000 characters of output. |
| Setup command | 1,000 characters | Declared or detected; the user approves each run. |
| Setup timeout | 900 s default, 1,800 s maximum | The setup container is killed at the timeout. |
| Extra secret patterns | 50 of at most 100 characters | Added to the built-in secret names; never removing any. |
| Never-publish globs | 50 of at most 100 characters | Enforced by the desktop broker. |
| Redaction exceptions | 50 paths | Per-workspace files whose `read` results reach the model unredacted. |
| Egress audit log | 2,000 entries | Newest kept, in the desktop data directory. |

## Documentation tools

| Bound | Value | Notes |
|---|---:|---|
| `web_search` results | 8 | Snippets of at most 400 characters; 60-second request timeout; charged to the run's cost budget. |
| `fetch_url` URL | 2,000 characters | HTTPS only; hosts must resolve to public addresses. |
| `fetch_url` download | 2,000,000 bytes | 20-second timeout; up to 5 redirects, each re-checked against the allowlist. |
| `fetch_url` page text | 24,000 characters | After scripts, styles, navigation, and markup are removed. |

## Profiles

The `small` profile reduces file/output/search limits. `coding` is the default. `large-workspace` raises selected limits while remaining below hard ceilings. Environment variables named `LOCAL_CHAT_<LIMIT>` can override profile values, but `ExecutorConfig` rejects values above hard ceilings or resource-model capacity.

## Which limit wins?

1. The tool schema rejects structurally invalid requests before execution.
2. The protocol rejects arguments above the 17,000,000-byte `MAX_TOOL_ARGUMENT_BYTES` cap. It sits above every per-tool argument ceiling, so in practice step 3 decides.
3. Executor `effective_limit()` applies the requested value, configured profile value, and hard ceiling; the minimum wins.
4. Tool-specific filesystem/path/security rules can reject an operation independently of byte/count limits.
5. Staging/checkpoint and work-volume resource limits apply to mutations and may reject an otherwise valid operation.
6. Docker CPU, memory, PID, disk, and network isolation (the runner's settings above, the control plane's fixed limits) are outer containment controls; they never expand application limits.

The effective limits reported by `/v1/status` are intended to make the active profile and source of each configured value inspectable.

## Investigation delegation

Investigation delegation accepts up to 50 `inspected_paths` per call and returns at most 50 structured findings. It is bounded to four calls per turn (reset at every parent model turn) and half of the parent run's remaining cost per call, with 36-iteration, 36-tool-call, and 300-second wall-time nested caps (the wall time is the parent's remaining time clamped to 10–300 seconds). Within that, exploration stops early enough to keep a synthesis reserve of 60 seconds (or a third of a shorter nested budget), each nested model request is limited to 15–180 seconds, nested results are bounded to 40,000 characters each and 512,000 bytes per call in their own ledger, and nested context is compacted to about 64,000 tokens. It never creates an executor, staging, or publication authority.
