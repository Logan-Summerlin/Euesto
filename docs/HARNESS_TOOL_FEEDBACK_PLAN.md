# Harness tool feedback plan — read coverage, write confirmation, and batch dedupe

Status: implemented. Six failures observed in one agent run. The plan keeps the security boundary
and the thirteen-tool surface: it adds one argument to `write`, removes one model-facing field
from `read`, and makes existing feedback state what the harness actually knows. It does not add
a compatibility tool, an alias, or a second publication path.

The issues, in the reporter's order:

| # | Symptom | Root cause |
|---|---|---|
| 1 | The shrink guard told the model to pass `expected_sha256`, but `write` has no such parameter; repeated full reads did not satisfy it; a new file bypassed it. | The gateway owns the hash and only fills it after a "whole" read, but a whole read is impossible above the model-facing read cap. The guard message promises a parameter the schema deliberately hides. |
| 2 | A no-range read returned only "unchanged since your earlier read", with no content and no statement of coverage. | The re-read note has no coverage record, so it cannot say whether the earlier result counted as the full read the guard wanted. |
| 3 | A large write was reported as `invalid JSON (Unterminated string)` when the model output limit cut the call off. | JSON parsing sees a truncated string; it does not know the turn's `finish_reason` was `length`, and it reports neither the payload size nor the truncation. |
| 4 | `read` silently stopped at about 29 KB, although `max_bytes` accepts up to 8 MB; each truncated result also appended a long outline. | The gateway subtracts a 3,000-byte outline reserve from the 32,000-byte read cap, then the executor's outline is appended to the model view. Neither the cap nor the outline is explained in the result. |
| 5 | The handoff said to call `status` first; the model skipped it and relied on the previous turn's status. | The summary turn is tool-free, so it cannot refresh status. The handoff's "from status" instruction is a suggestion the model can ignore, and the harness has no freshness record. |
| 6 | Two reads of the same file in one batch both returned the whole file. | Consecutive read-only calls run concurrently; the re-read note checks the live history before either same-batch result has been appended. |

## 1. Today

### 1.1 Shrink guard and hashes

- `executor/mutations.py:80-90` rejects a whole-file replacement that drops most of the file and
  says to "review the full replacement before retrying, or pass the current `expected_sha256`".
- `server/openrouter/agent.py:74-78` offers `write` with `path`, `content`, and `create_parents`
  only. `tests/unit/shared/test_tool_contract.py:160-180` pins that `expected_sha256` is hidden
  from the model, because the gateway fills it from the last hash the agent saw.
- `server/agent/runtime.py:1745-1766` fills `expected_sha256` for a `write` only when
  `whole_file_required` and the recorded read was whole. `_observe_hashes`
  (`runtime.py:1399-1436`) sets `whole` only for a read that is not truncated, starts at byte 0,
  and has no line range.
- The gateway clamps a read's content to `profile.result_cap("read") - READ_OVERHEAD_BYTES`:
  32,000 − 3,000 = 29,000 bytes in `coding` (`runtime.py:1379-1382`,
  `server/agent/results.py:22`, `server/agent/budgets.py:34-37`). A file larger than 29,000 bytes
  can therefore never produce a `whole` read, no matter how many times it is read. The guard's
  advertised fix is unreachable, and a `write` to a new path skips the guard entirely because
  `old_hash is None`.

The model never sees a hash. `docs/TOOLS.md` documents `expected_sha256` as an optional `write`
argument, but the model-facing schema does not offer it; the documentation and the schema
disagree. The guard message and `ERROR_HINTS["staging.shrink_warning"]`
(`server/agent/results.py:24-27`) repeat the same promise.

### 1.2 The ambiguous no-range re-read

`_unchanged_read` (`server/agent/runtime.py:1467-1492`) keys a read by
`(path, start_line, end_line, offset, max_bytes)`. When the same key, same hash, and the earlier
result are still live, it returns one line:

```text
read a: unchanged since your earlier read (tool call r1); use that result.
```

Nothing says whether that earlier result was the whole file or a truncated range. The model
cannot tell whether the note satisfies the guard, and the guard's own message asks for a "full
read (no range)" without defining it. The model's several full reads were each truncated, so the
guard kept rejecting.

### 1.3 Length-truncated write arguments

`parse_call_arguments` (`server/agent/arguments.py:26-37`) turns every `json.JSONDecodeError`
into one corrective line:

```text
write arguments are not valid JSON (Unterminated string starting at ...); send one JSON object
```

When a turn hits the per-turn output limit (`profile.max_output_tokens`, 16,000 in `coding`), the
provider returns `finish_reason == "length"` and the tool-call JSON is cut off. The runtime knows
the finish reason in `_agent_loop` (`runtime.py:540-560`), but `_execute_tool_call` does not
receive it. The `LENGTH_NOTE` is appended after the tool calls (`runtime.py:653-654`), so the
model first gets a misleading JSON error and only later a note about output length. A complete
JSON object with truncated `content` is worse: it would be written as if it were intentional.

### 1.4 The read cap and the outline

- The model-facing read cap is 32,000 bytes (`docs/LIMITS.md:39-47`), but the effective content
  is 29,000 because `READ_OVERHEAD_BYTES = 3_000` reserves room for the header and outline.
- The schema accepts `max_bytes` up to 8,000,000 (`server/openrouter/agent.py:61-72`), the
  executor default is 32,000 (`executor/tools/read.py:10`), and the gateway silently replaces a
  larger request with 29,000 (`runtime.py:1379-1382`). The result does not say it was capped.
- A truncated read appends up to 40 outline entries (`server/agent/results.py:120-127`,
  `executor/tools/read.py:12-16`, `:169-182`). The outline is a second copy of information the
  model can get with `grep`, and it consumes the same result budget it is supposed to protect.
- The continuation fields (`next_start_line`, `next_offset`) are already exact. The missing piece
  is a coverage record: what part of the file has been read across calls, and whether that is the
  whole file.

### 1.5 The handoff's first step

`HANDOFF_REQUEST` (`server/agent/runtime.py:95-101`) says "Do not call tools" and asks for
"Files touched (from status)". The summary turn therefore cannot refresh status; it uses the last
status the model happened to run. The resulting handoff can say "Next steps: call status first",
but nothing in the harness enforces or verifies that step. The post-compaction model can act on
a stale status, which is exactly what happened.

### 1.6 Duplicate reads in one batch

`_execute_parallel_group` (`server/agent/runtime.py:1068-1095`) runs consecutive
`PARALLEL_SAFE_TOOLS` calls concurrently. `_unchanged_read` records a read only after its result
is formatted, and `_still_live` checks `self._live[run_id]`, which does not yet contain the
current batch's results. Two identical reads in one batch therefore both execute and both return
the full file. The model made the mistake, but the harness can catch an exact duplicate before
spending the call.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| D1 | Keep hashes gateway-owned; do not add `expected_sha256` to the model schema. | The model never sees a hash, so exposing the parameter without a hash would repeat the bug. The gateway can prove coverage from the reads it already recorded. |
| D2 | Track byte coverage per path and treat contiguous chunks as a complete read. | A file larger than one result can only be covered in chunks. Coverage is the real precondition for a deliberate whole-file rewrite. |
| D3 | Add `mode: "replace" \| "append"` to `write`; `append` is non-destructive and does not require complete coverage. | The output limit makes a large single write impossible. Append lets the model add a part per turn without inventing a new tool or abusing `apply_patch`. |
| D4 | On a length-truncated turn, do not execute mutation calls. | A complete-looking JSON object may still contain truncated content. Refusing the mutation and naming the limit is safer than writing a partial file. |
| D5 | Make `read` results carry a cumulative coverage record and drop the truncated-file outline. | Coverage tells the model when it has the whole file; the continuation line is enough to reach the rest. The outline costs context and duplicates `grep`. |
| D6 | Refresh `status` inside the compaction turn and put it in the handoff. | The summary turn cannot call tools, so the harness should run the read-only status call for it. This makes "Files touched" accurate without relying on the model's memory. |
| D7 | Deduplicate exact read keys within one parallel batch. | The first call pays for the content; later identical calls get a one-line pointer. Order is preserved and no executor call is spent. |

The guard remains a safety check. A matching hash still downgrades a large shrink to a warning;
the change is how the gateway obtains that hash and how the message explains it.

## 3. Design

### 3.1 Byte coverage and the shrink guard

Replace the `(hash, whole)` tuple in `self._seen_hashes` with a small record:

```text
SeenFile:
    sha256: str
    size_bytes: int
    covered: list[(start_byte, end_byte)]   # merged, sorted, non-overlapping
    complete: bool                          # covered == [(0, size_bytes)]
```

- **Read:** if the result's `sha256` differs from the recorded one, start a new record. Merge
  `[byte_offset, byte_offset + consumed_bytes]` into `covered`, clipped to `size_bytes`. A read
  that starts at byte 0 and consumes the whole file is complete in one call; several contiguous
  chunks are equally complete.
- **Exact byte count:** `content_bytes` is the displayed text length, but a non-truncated read
  normalizes line endings and drops a trailing newline (`executor/tools/read.py:95`, `:135`),
  so it can undercount the bytes actually read. Add `consumed_bytes` to
  `_metadata` from the `_decode_bounded` return value and use it for coverage. Keep
  `content_bytes` for the model's size display.
- **Line-boundary gaps:** a line-range read stops before the final newline, and the next line
  range starts after it, leaving a one-byte gap. Merge ranges that overlap or are separated by at
  most one byte; a larger gap is a real unread region.
- **Write (replace):** the new file is fully covered by the write itself.
- **Write (append):** the new bytes are covered, but the old bytes are only covered if the
  previous record was complete or the file is new. A blind append must not make a later replace
  look reviewed; the model has not seen the old content.
- **Edit:** if the old record was complete, keep it complete (the model knew the old file and
  supplied the exact replacement). If it was partial, reset coverage to empty because byte
  offsets may have shifted; a later whole-file write then requires fresh coverage.
- **Apply patch:** `write` sets complete; `edit` follows the edit rule; `delete` removes the
  record.
- **Bash:** clear all records, as today, because a command may have changed anything.

`_fill_expected_hash` (`runtime.py:1745-1766`) becomes:

| Tool | Fill when |
|---|---|
| `write` with `mode="replace"` | `known.complete` |
| `write` with `mode="append"` | `known` is present (any coverage; append does not destroy unseen content) |
| `edit` | `known` is present (unchanged) |

The executor's guard message becomes generic: "read the whole file in contiguous parts before
retrying; a deliberate rewrite is confirmed automatically." The executor cannot know the
gateway's cumulative coverage, so the gateway enriches the failed result before formatting it:
when `result.error_code == "staging.shrink_warning"`, look up the `SeenFile` for the path and add
`coverage` (merged ranges), `size_bytes`, `covered_bytes`, and `complete` to `result.data`.
`format_error` renders a second line from those fields. The model-facing message becomes:

```text
error staging.shrink_warning: whole-file edit for src/app.py would shrink the file from
120,000 to 4,000 bytes and from 3,000 to 20 lines
coverage: 58,000 of 120,000 bytes in 2 ranges; 62,000 bytes remain; read the rest, then retry
```

`ERROR_HINTS["staging.shrink_warning"]` becomes "read the remaining bytes; the gateway confirms
the rewrite after full coverage." It never names a parameter the schema does not offer.

### 3.2 Read coverage in the model-facing result

Add a coverage view to the read header. The gateway computes it from the `SeenFile` record after
the read; the executor keeps returning exact byte fields.

Examples:

```text
read src/app.py lines 1–706 of 1,832 (39% covered; continue with start_line=707)
read src/app.py lines 707–1,832 of 1,832 (full coverage)
read src/app.py lines 1–706 of 1,832 (39% covered; requested max_bytes 1,000,000, capped at
31,000 by the coding profile; continue with start_line=707)
```

- `format_tool_result` gains optional `coverage` and `requested_max_bytes` arguments. `_read`
  renders the percentage and the continuation. The existing `next_start_line` / `next_offset`
  text stays the source of truth for the next call.
- The no-range re-read note becomes:

```text
read src/app.py: unchanged since tool call r1; that result covered lines 1–706 of 1,832 (39%),
not the whole file. Continue with start_line=707, or use the earlier result for that range.
```

  When the earlier result was complete, the note says "that result covered the whole file
  (full coverage)." A note is never returned without saying what the earlier result covered.
- The executor's `outline` is removed: delete `MAX_OUTLINE_ENTRIES`, `MAX_OUTLINE_LINE_CHARS`,
  `_OUTLINE`, `outline()`, and the `data["outline"]` assignments in `executor/tools/read.py`.
  Delete the outline block in `server/agent/results.py:_read`. The continuation line already
  names the exact next range.
- `READ_OVERHEAD_BYTES` drops from 3,000 to 1,000, which raises the effective content cap from
  29,000 to 31,000 bytes. The header and coverage line fit in 1,000 bytes; if a future header
  grows, the formatter must stay within the reserve rather than clip content (a clipped body
  would make `next_start_line` wrong).
- The `read` schema description says that `max_bytes` is the executor request and that the
  gateway caps each model-facing result, naming the effective cap in the result. The schema
  maximum stays 8,000,000 because that is the executor's contract; the result no longer pretends
  the cap is silent.

### 3.3 Length-truncated tool calls

- `parse_call_arguments(name, raw_arguments, *, output_truncated=False)`:
  - If `output_truncated` and JSON is invalid, raise an `ArgumentError` whose text names the
    output limit and the raw argument size, not "Unterminated string".
  - If JSON is invalid for another reason, include `len(raw_arguments)` in the existing message.
- `_execute_tool_call` and `_execute_parallel_group` gain an `output_truncated` flag from
  `turn.finish_reason == "length"`.
- Before parsing or dispatching, if `output_truncated` and the tool is in `MUTATION_TOOLS`
  (`write`, `edit`, `apply_patch`, `bash`), do not execute it. Return a synthetic result:

```text
error tool.output_truncated: the model output limit (16,000 tokens) cut off this write after
58,231 argument characters; nothing was applied. Split the content into smaller calls; for a
large file, use write mode="append" one part at a time.
```

  The result's `data` carries `tool`, `raw_argument_bytes`, and `output_limit_tokens`. Read-only
  calls in the same turn still execute; a truncated read is a normal parse error with the size.
- Add `tool.output_truncated` to `ERROR_HINTS` and journal `loop.output_truncated` with the turn,
  tool names, and argument sizes.
- Update `LENGTH_NOTE` and `AGENT_PROMPT` efficiency rule 3 to say `write` the first part, then
  `write` with `mode="append"`, instead of extending with `apply_patch`.

### 3.4 `write` append mode

`write` gains `mode`, an enum of `"replace"` (default) and `"append"`.

- **Schema:** `server/openrouter/agent.py:74-78` adds `"mode": {"type": "string", "enum":
  ["replace", "append"], "default": "replace"}` and updates the description. `WRITE_ARGUMENTS`
  in `executor/tools/write.py:28` adds `"mode"`.
- **Apply patch:** its `write` operation keeps replace-only semantics. `apply_patch.py` uses a
  separate `PATCH_WRITE_ARGUMENTS` set without `mode`, so an append cannot hide inside an atomic
  patch.
- **Prepare:** `prepare_write` validates the mode. For `append`:
  - `old_hash` is the current file hash (or `None` for a new file).
  - `expected_sha256` is checked as today.
  - The shrink guard is skipped; appending never removes bytes.
  - The resulting size (`old_size + len(content)`) must be at most `max_bytes`, the same
    `max_write_bytes` limit that bounds a replace. Per-call content is also bounded by
    `max_write_bytes`.
  - `create_parents` works as today.
- **Commit:** add `atomic_append_text` to `executor/atomic_io.py` (copy the old file to a temp
  file, append, fsync, atomic replace) so a crash cannot leave a half-appended file. The existing
  checkpoint restores the old file on failure.
- **Result:** `write_result` gains `mode` and `appended_bytes`; `size_bytes` is the new total.
  `server/agent/results.py:_write` renders "created", "rewritten", or "appended N lines
  (+X bytes)". A diff is produced when the old file fits `WRITE_DIFF_MEMORY_BYTES`; otherwise the
  bounded edit summary says how many lines were appended.
- **Gateway hash fill:** append fills `expected_sha256` from any known coverage (D1 table). A
  blind append to a path the model never read is allowed, exactly as a blind replace of a small
  file is allowed today; the checkpoint and publication review remain the safety net.
- **Registry:** `shared/tools.py` needs no new tool, permission, or argument-cap change: `write`
  stays in `MUTATION_TOOLS` and `STAGED_EDIT_TOOLS`, and `mode` fits inside the existing
  `MAX_TOOL_ARGUMENT_BYTES` envelope. Add the schema/registry consistency test so the thirteen
  names and the `write` properties cannot drift.

### 3.5 Fresh status in the compaction handoff

Before the summary model turn in `_maybe_compact` (`server/agent/runtime.py:958-1034`):

1. Run one read-only `status` request through the executor (allowed in Plan and Agent:
   `shared/tools.py:PLAN_EXECUTOR_TOOLS`). Use the same request-id shape as other executor
   calls, with `paths` omitted and `include_diffs` false.
2. Format it with `format_tool_result("status", {}, result)` and clip it to 4,000 characters,
   keeping the header and counts. If the call fails, use "status unavailable; run status after
   compaction" and journal `context.status_unavailable`.
3. Append the text to the summary prompt:

```text
Fresh staged status (application-generated; use this for "Files touched"):
<status text>
```

4. Include the same bounded text in the handoff message that survives compaction, after the
   model's handoff and the plan note.
5. Journal `context.status_snapshot` with `ok`, `counts`, and `bytes`.

The handoff request keeps "Do not call tools"; the harness has already made the call. The
post-compaction history therefore starts with a fresh status instead of the previous turn's. The
model may still call `status` for details; it no longer has to remember to.

### 3.6 Duplicate reads in one batch

Before `_execute_parallel_group` schedules a group:

- Canonicalize each `read` call's key: `(path, start_line, end_line, offset, max_bytes)`, treating
  an omitted `max_bytes` as the same key as an explicit default.
- The first occurrence executes. Each later identical key gets a synthetic result without an
  executor call:

```text
read a: duplicate of tool call r1 in this batch; use that result.
```

- Preserve call order in the buffers. Journal `read.duplicate_batch` with `path`, `request_id`,
  and `duplicate_of`. A duplicate in a later turn is still handled by the existing live-history
  note, now with coverage.

This catches only exact duplicates. A range read that is contained in a no-range read is left to
the model; detecting that safely requires knowing whether the first read truncated, which is only
known after it runs.

## 4. Contracts and bounds

| Item | Value after the change |
|---|---|
| `read` model-facing content cap (`coding`) | 31,000 bytes (32,000 result cap − 1,000 header reserve); `extended-coding` 47,000; `large-coding` 63,000 |
| `read` requested `max_bytes` | Still up to 8,000,000 at the executor; the result states the effective model-facing cap |
| `read` coverage | Merged byte ranges per path and hash; complete when they cover `[0, size_bytes)` |
| `read` truncated result | Exact `next_start_line` or `next_offset`; no outline |
| `write` modes | `replace` (default), `append` |
| `write` size | Per-call content and resulting file at most `max_write_bytes`; append does not shrink |
| `write` shrink guard | Replace only; a complete-coverage hash downgrades it to a warning |
| Length-truncated turn | Mutation calls are not executed; `tool.output_truncated` names the limit and argument size |
| Duplicate read | One executor call per identical read key per parallel batch |

## 5. Tests

- **Executor read:** update `tests/unit/executor/test_output_artifacts.py:141` to assert exact
  continuation and no `outline` key; keep the single-overlong-line offset test.
- **Gateway read format:** update `tests/unit/gateway/test_model_results.py:22-45` so the golden
  header carries coverage and the body does not contain the outline.
- **Executor write:** new `tests/unit/executor/test_write_append.py`: create, append, append to a
  missing parent with `create_parents`, reject a resulting size over `max_write_bytes`, preserve
  the file mode, checkpoint rollback on a failed append, and no shrink guard on append.
- **Shared schema:** `tests/unit/shared/test_tool_contract.py:160-180` adds `mode` to
  `SCHEMA_PROPERTIES["write"]`, keeps `expected_sha256` out of both `write` and the
  `apply_patch` operation, and asserts `apply_patch` rejects `mode`.
- **Gateway coverage:** new `tests/unit/gateway/test_tool_feedback.py`:
  - two contiguous chunked reads mark a file complete and a following shrink `write` is accepted
    with a warning, not rejected;
  - a partial read still rejects the shrink and the error names the covered and remaining bytes;
  - a changed hash resets coverage;
  - an `edit` after a partial read resets coverage, so a later `write` needs a fresh read.
- **Gateway re-read note:** the note names coverage and the continuation; a full-coverage re-read
  says so; the note is absent once the earlier result is compacted away.
- **Gateway output truncation:** a length-truncated `write` call with unterminated JSON returns
  `tool.output_truncated`, records no executor request, and names the argument size; a
  length-truncated `read` still executes; a complete-looking `write` on a length turn is not
  executed.
- **Gateway handoff:** a compaction scenario records a `status` executor call, the summary
  provider payload contains the fresh status, and the post-compaction messages contain it. A
  failing status call falls back without failing compaction.
- **Gateway duplicate reads:** two identical reads in one turn produce one executor request, two
  ordered tool messages, and a `read.duplicate_batch` event.
- **Docs contract:** the existing documentation tests still pass; `docs/TOOLS.md` and
  `docs/LIMITS.md` match the new schema and caps.

## 6. Documentation

- `docs/TOOLS.md`: `read` coverage, cap reporting, no outline; `write` `mode` and append
  semantics; remove the claim that the model passes `expected_sha256` for `write` or `edit` (it is
  gateway-owned); the guard's coverage-based confirmation.
- `docs/LIMITS.md`: the 31,000-byte effective read content cap, append's resulting-size rule, and
  the length-truncated mutation rule.
- `docs/AGENTIC_HARNESS_PLAN.md` §1.4: mark the truncated-file outline as superseded by this
  plan; the continuation line and coverage record replace it.
- `docs/README.md`: index this plan.
- `CHANGELOG.md` under `## Unreleased` when the change lands.

## 7. Order of work

1. Coverage record and guard messages (3.1), with the executor tests that prove chunked coverage.
2. Read header, cap reporting, and outline removal (3.2).
3. `write` append mode (3.4), because the output-truncation guidance depends on it.
4. Length-truncated mutation refusal (3.3).
5. Fresh compaction status (3.5).
6. Batch duplicate reads (3.6).
7. Documentation, index, and the full check tiers.

## 8. Risks

| Risk | Mitigation |
|---|---|
| A coverage bug could let a stale whole-file write through. | Coverage is keyed by the current hash and reset on any change; a changed hash, a bash command, or a partial edit clears it. Tests cover each reset. |
| Append can grow a file across many turns. | The resulting size is bounded by `max_write_bytes`, the checkpoint restores on failure, and publication still requires review. |
| Dropping the outline makes the model search more. | The exact continuation line remains, and `grep`/`find` are cheaper than a repeated outline in every truncated read. |
| Refusing mutations on a length turn may feel like a failed call. | The error names the limit, the argument size, and the append remedy in one line; read-only calls in the same turn still run. |
| A fresh status call adds executor work per compaction. | It is read-only, bounded, and replaces a model turn the handoff asked for anyway; a failure falls back without failing compaction. |
| Duplicate detection misses overlapping but non-identical reads. | Exact duplicates are the safe, high-value case; containment needs the first result and is left to the existing live-history note. |

## 9. Definition of done

- A file larger than the read cap can be read in contiguous chunks, the model sees a coverage
  record, and a deliberate whole-file shrink is accepted without the model ever seeing a hash.
- The shrink guard's message names the coverage it counted and never asks for a parameter the
  schema does not offer.
- A length-truncated write returns `tool.output_truncated` with the limit and argument size; no
  mutation from that turn is applied.
- A truncated read has an exact continuation, no outline, and an explicit effective cap when the
  requested `max_bytes` was reduced.
- A no-range re-read note says whether the earlier result was complete.
- A compaction handoff carries a fresh status, and the post-compaction model can rely on it.
- Two identical reads in one batch execute once.
- `docs/TOOLS.md`, `docs/LIMITS.md`, `docs/AGENTIC_HARNESS_PLAN.md`, `docs/README.md`, and
  `CHANGELOG.md` are updated with the change, and the checks in `docs/TESTING.md` pass.
