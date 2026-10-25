# `investigate_repository` summary handoff: diagnosis and fix plan

Status: proposed (diagnosis complete, no code changed yet).

Scope: `investigate_repository` only. The nine executor tools, the four gateway tools other
than investigation, approvals, staging, and publication are unchanged. The contract that the
parent always receives a `summary` ([TOOLS.md](TOOLS.md), investigation section) is kept, not
relaxed: the fix makes that promise hold on every failure path instead of only on the happy
one.

## 1. Symptom

A parent agent calls `investigate_repository`, the nested loop works for a while, and the
tool result that comes back contains no usable synthesis: either a one-sentence harness stub
naming the files it looked at, or no `summary` key at all. Journal `subagent.completed` events
show `truncated: true` with `stop_reason: "budget"` and `budget_exhausted: true`, which reads
as "the investigation ran out of budget" even when the nested loop stopped early and had room
left. "After exhausting its tool calls" is the case where this is most reliable, because the
wall-time reserve left for the one call that produces the answer is then at its minimum.

## 2. What a call does today

One call runs a nested read-only model loop over `read`, `grep`, `find`, `ls`, and `status`
through the parent's executor session (`server/agent/investigation.py`).

1. `run` validates the arguments, refuses to start below a $0.01 parent cost floor
   (`investigation.py:106`), builds a child budget (`_child_budget`, `:429`), and enters
   `_investigate` (`:153`).
2. `_investigate` loops while `_stop_reason` returns `None` (`:487`). Before each step it
   reserves one iteration, one tool-call slot, the synthesis wall-time reserve, and a
   per-turn cost margin. On stop it answers any tool calls the model issued past the stop
   point with `investigation.budget_reserved` and breaks.
3. `_synthesize` (`:207`) compacts the nested context to `INVESTIGATION_CONTEXT_TOKENS`
   (64,000), appends `SYNTHESIS_REQUEST` plus `REPORT_FORMAT_INSTRUCTIONS`, and runs one
   tool-free turn through `_turn` (`:244`) with `allowed_tools=set()`.
4. `_payload` (`:395`) parses the final message into `summary`/`findings`/`observed` and
   `_finish` (`:411`) journals `subagent.completed` and returns the JSON the parent sees.

The summary is therefore produced by exactly one provider call, and every path that loses
that call loses the whole handoff.

## 3. Where the handoff is lost

Each item is a defect against the documented contract, ordered by how often it explains the
report.

### 3.1 The synthesis turn gets the smallest slice of time in the run

Exploration turns are capped at `INVESTIGATION_MAX_TURN_SECONDS` = 180 s
(`investigation.py:56`, `:268`), but the synthesis turn is given
`turn_timeout = min(180, max(60, available))` where `available` is what is left after the
60 s reserve (`investigation_synthesis_reserve`, `:424`). Once exploration has used its wall
time, `available` is at or below the 60 s floor, so the summary call gets about 60 s no
matter how much context it must prefill: a 64,000-token nested context plus a 2,500-token
report is a slow first token on a flash-tier investigation model, and a prefill overrun is a
retryable `provider.timeout` (`server/openrouter/agent.py`, turn ceiling). The retry loop
then finds `available - delay < INVESTIGATION_MIN_TURN_SECONDS` and raises (`:270-279`).
Exploration is bounded by the reserve, but the reserve is a constant that ignores how large
the context has grown.

Two smaller problems in the same place:

- The 60 s floor can exceed the remaining wall time, so a synthesis attempt may run up to 60 s
  past the child's own wall budget; the child wall cap is soft by one floor.
- `_synthesize` compacts the nested context to 64,000 tokens *immediately before* asking for
  the report (`:212-215`). For a long investigation this deterministically excerpts away the
  oldest evidence at the exact moment the model needs it, so even a successful synthesis can
  answer from a truncated ledger. `compact_agent_context` is lossy truncation, not a
  handoff.

### 3.2 A paid-for summary is discarded by a budget charge made after the fact

`_turn` records the child's usage without checking (`child.add_usage(..., check=False)`),
then charges the parent with `self.parent_budget.add_usage(turn.usage)` (`:281-282`). That
second call checks the parent's limits and raises `BudgetExceededError` when the parent has
just crossed one — after `turn.content` already holds the finished summary. The exception
unwinds through `_synthesize` (whose handler only catches `ProviderError`, `:231`) and
`_investigate`, and is caught by `run`'s `BudgetExceededError` branch (`:127-138`), which
returns the canned "Investigation budget exhausted after partial repository analysis" text.
The expensive answer is thrown away and replaced by the stub. The same branch is also reached
from the start-up guard (`:106`), which raises `RuntimeError` rather than a budget error and
therefore lands in the generic handler instead.

### 3.3 A transport failure discards everything gathered so far

`result = await self.executor.execute(tool)` (`:340`) is unguarded inside `_tool_call`.
`ExecutorClient.execute` raises `ExecutorUnavailable` (`server/executor/client.py:11`) when
the executor socket is not there, and the exception escapes `_investigate`, so `_synthesize`
never runs and `run`'s generic handler returns `{"error", "fallback"}` with **no `summary`
key** (`:139-147`). The parent's own executor calls are wrapped in `try/finally`
(`server/agent/runtime.py`, `_execute_tool_call`), so the nested loop is the only unguarded
consumer of the same client. An executor restart, or a session that is being torn down while
an investigation is in flight, produces exactly the "no summary" result.

### 3.4 An empty summary passes the emptiness check

`_synthesize` only tests `content.strip()` (`:235`). `parse_investigation_report` can still
return an empty summary: when the model replies with a JSON report whose `summary` is blank
and there is no prose around the object, `_extract_report` matches the whole message as the
candidate, `remainder` is empty, and `summary_text` becomes `""`
(`shared/investigation.py`, `parse_investigation_report`). The result is `summary: ""` with
`truncated: false` and no `stop_reason` — a valid-looking payload with nothing in it.

### 3.5 The evidence ledger undercounts, so real work looks like no work

`_observed_files` (`:531`) records only `read` and `grep`, and its `read` branch expects
`→`-prefixed line numbers that `read` never emits: the tool returns plain text plus a
`data` block (`executor/tools/read.py`). `_has_evidence` (`:150`) is `files or observed or
skipped`. Consequences:

- `find`/`ls`/`status`-only investigations look like failures: the first `ProviderError`
  re-raises because `_has_evidence` is false (the `except ProviderError` branch at `:167-174`
  and the same condition in `_synthesize` at `:232-233`).
- `Observed` flags on findings are wrong for `read`-only work, which weakens the one signal
  the parent uses to trust a finding without re-reading.

### 3.6 The diagnostic text names the wrong cause

`_fallback_summary` (`:511`) checks `stop_reason == "budget"` before the provider error.
`_stop_reason` collapses four different conditions (tool calls, cost, wall time, iterations)
into the single string `"budget"` (`:487-509`), so a synthesis timeout after a wall-time stop
is reported as "Investigation budget exhausted … the investigation model returned no final
summary". That is why the failure reads as a budget problem in the journal and in the parent's
context.

### 3.7 The documented error code is never emitted

[TOOLS.md](TOOLS.md) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) promise
`investigation.failed` and a fallback instruction; the code returns bare
`{"error", "fallback"}`, and `runtime._investigate_repository` returns bare
`{"error", "remaining", "fallback"}` for the per-turn call cap instead of the documented
`investigation.call_limit` (`server/agent/runtime.py`, `_investigate_repository`). Because
nested tool results never enter the parent's context by design, the payload *is* the handoff;
a payload with no `summary` and no code leaves the parent nothing to branch on.

## 4. Root cause

The harness funds the synthesis turn but does not protect it. The summary is a single
all-or-nothing provider call whose inputs (wall time, budget accounting, transport, evidence
ledger) are all allowed to fail in ways that discard it, and every one of those failures
degrades to a stub or to an error payload with no `summary`. The invariant "the parent always
gets a summary" is written down (`investigation.py:99-101`) and tested for three cases
(`tests/unit/gateway/test_investigation_budget.py`), but not for the four paths above.

## 5. Plan

Ordered so that each step is independently shippable and testable. Steps 1–3 remove the
losses; steps 4–6 make a degraded result useful; step 7 aligns documentation.

### Step 1 — Never lose a paid summary (fix 3.2)

- In `_turn`, capture `turn.content`/`turn.message` first, then charge the parent inside
  `try/except BudgetExceededError` and record the condition as a warning
  (`extra["parent_budget_exceeded"] = True`) instead of raising.
- Keep enforcement: the parent loop's `check()` before its next step still stops the run; the
  refused next call, not the finished summary, is what must fail.
- Replace the start-up `RuntimeError` (`:106`) with a structured refusal that carries the same
  shape as `investigation.failed`.
- Files: `server/agent/investigation.py`.

### Step 2 — Bound the reserve by what synthesis must send (fix 3.1)

- Replace the flat `INVESTIGATION_SYNTHESIS_RESERVE_SECONDS = 60` with a derived reserve:
  `max(60, f(estimated_tokens_of_nested_context))` — for example 60 s base plus one second per
  2,500 context tokens, capped by a third of the child budget — and export it as
  `investigation_synthesis_reserve(child, tokens)`.
- Compute `turn_timeout` for synthesis as `min(INVESTIGATION_MAX_TURN_SECONDS, remaining)`
  with a floor that never exceeds the child's remaining wall time, so the child can no longer
  overrun its own cap by one floor.
- Do not compact the nested context immediately before synthesis (`:212-215`). If it must be
  bounded, bound it to a *higher* synthesis-specific ceiling and keep the newest evidence
  intact; the report prompt is appended after that point.
- Files: `server/agent/investigation.py`.

### Step 3 — Survive an executor failure mid-loop (fix 3.3)

- Wrap `self.executor.execute(tool)` in `try/except (ExecutorUnavailable, Exception)`,
  answering the tool call with a bounded `ToolResult` carrying
  `error_code: "investigation.executor_unavailable"` so the loop continues, the model can
  change approach, and synthesis can still run over the evidence already gathered.
- Record `degraded: true` on the payload when any nested call failed this way.
- Files: `server/agent/investigation.py`.

### Step 4 — Make every failure payload a digest, not a stub (fixes 3.5, 3.6)

- Extend `_observed_files` to record `find`, `ls`, and `status` paths, and to derive `read`
  targets from the arguments plus the result's `data` block rather than a `→` line format
  that does not exist. Set `_has_evidence` from any successful nested tool call.
- Split `_stop_reason` into the specific cause it found (`"tool_calls"`, `"cost"`,
  `"wall_time"`, `"iterations"`, `"evidence_limit"`, `"empty_answer"`,
  `"provider_error"`) while keeping `"budget"` as the documented grouping in `extra`
  (`extra["stop_reason"]` plus `extra["stop_cause"]`).
- Rewrite `_fallback_summary` as a deterministic digest: the stop cause in one line, whether
  the provider failed (with the short error), up to N examined files with the grep line
  anchors it already has, and one concrete next step for the parent (`read <file>` on the
  most specific unchecked path, or a narrower query). It must never claim "budget exhausted"
  for a wall-time or provider stop.
- Files: `server/agent/investigation.py`, `shared/investigation.py` (a
  `InvestigationResult.failed(...)` constructor so no caller hand-builds an error dict).

### Step 5 — Guarantee the payload shape (fixes 3.4, 3.7)

- `InvestigationResult.to_dict` derives a non-empty summary from the first findings when the
  summary is blank, so `summary` is never `""`.
- Emit `error_code` on every failure: `investigation.failed`,
  `investigation.executor_unavailable`, `investigation.call_limit`. Add `stop_reason` to
  natural completions too, so the field is always present.
- Make `runtime._investigate_repository`'s call-cap payload structured (code, used, limit,
  `fallback`) instead of the bare dict, matching [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- Files: `shared/investigation.py`, `server/agent/investigation.py`,
  `server/agent/runtime.py`.

### Step 6 — Tests

Add to `tests/unit/gateway/test_investigation_budget.py` and
`tests/unit/gateway/test_investigation_contract.py`:

1. Parent budget is crossed during synthesis → the model's summary is returned, with
   `parent_budget_exceeded: true`.
2. `ExecutorUnavailable` on the second nested tool call → payload has a summary,
   `files_examined`, and `error_code`, and `subagent.failed` is not emitted.
3. A JSON report with `"summary": ""` → non-empty summary, derived from findings.
4. A `find`+`ls`+`status`-only investigation that then hits a provider error → summary, not
   an exception.
5. Synthesis retry never exceeds the child wall budget: total elapsed across attempts is
   bounded by `child.max_wall_seconds + one attempt`.
6. Wall-time stop → the digest says wall time, not "budget exhausted".
7. Every failure payload (`.failed`, call cap, executor unavailable) contains `summary` and
   `error_code`.

Update the existing assertions that encode today's text:
`test_missing_synthesis_text_falls_back_to_harness_summary` (stub wording),
`test_provider_failure_without_evidence_reports_failure` (adds `summary`/`error_code`).

### Step 7 — Documentation

- [TOOLS.md](TOOLS.md) investigation section: result contract including `error_code`,
  `stop_cause`, `degraded`, and the guarantee that `summary` is always present.
- [LIMITS.md](LIMITS.md) investigation paragraph: how the synthesis reserve is derived.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): degraded synthesis (a summary with a stop cause)
  vs. hard failure, and the three error codes.
- `CHANGELOG.md` entry when the change lands.

## 6. Verification

- `python scripts/validate.py fast` (includes the two investigation modules).
- `python scripts/validate.py ruff` (the touched modules are formatted).
- Scenario check in the journal of a real run: every `subagent.completed` payload has a
  non-empty `summary`, a `stop_cause`, and an `error_code` when it is a failure; the
  `subagent.started` budget and the `usage` in `subagent.completed` agree.

## 7. Open decision

The contract stays "always a summary" (steps above add `error_code` alongside it). The
alternative is to return a hard error the parent must retry, which is simpler to reason about
but loses the partial evidence that the nested loop already paid for. Only step 5's payload
shape changes if that alternative is chosen.
