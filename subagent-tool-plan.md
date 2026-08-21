# Investigation subagent — implementation plan

## Summary

Add one new tool, `investigate_repository`, that the existing single agent loop can call to
delegate read-only repository investigation to a nested loop running a cheaper model. The
nested loop is scoped to exactly the tools Plan mode already exposes — `list_files`,
`read_file`, `search_text` — and nothing else. It reuses the parent run's existing executor
session, budget, and journal; it does not introduce a second agent identity, credential,
container, or billing/recovery system.

This must work in both Agent prompt-approval mode and Agent Auto mode.

## Relationship to existing project decisions

`AGENTS.md` and `PROJECT_PLAN.md` record a deliberate v0.7 decision to keep multi-agent
orchestration out of scope, specifically because a second agent would need its own authority,
credential, billing, and recovery system. This design is written to stay on the safe side of
that line: `investigate_repository` is implemented as **one more tool the single agent can
call**, not a second agent with independent standing. If this ships, `AGENTS.md`'s
"Deliberate non-goals" section and `PROJECT_PLAN.md` §13 ("Resolved decisions") need to be
updated in the same change so the docs don't contradict the code — this is required by the
repo's own change workflow, not optional cleanup.

## Goals

- Main agent loop can delegate an investigation question to a cheaper model and get back a
  text summary.
- The subagent can only read — it cannot patch, run commands, move/copy, checkpoint, or
  publish, under any circumstance.
- Works identically whether the parent run is in prompt-approval mode or session-scoped Auto.
- Full nested trace (sub-loop's own tool calls, model turns) is durable and replayable in the
  gateway journal, even though it's collapsed in the desktop transcript by default.
- Cost/usage from the sub-loop is attributed and bounded within the parent run's existing
  budget — no new budget pool.

## Non-goals

- No independent executor session, socket, or workspace mount for the subagent.
- No subagent write/patch/command/publish capability, now or as a later toggle on this tool.
- No concurrent subagents each with their own staging view — the nested loop reads through the
  same staging copy the parent run already has open.
- No per-call or model-chosen investigation model — the model is fixed once set in Settings,
  not decided dynamically by the primary model or re-evaluated per call.

## Architecture recap

```
Desktop UI
    | authenticated loopback HTTP
    v
Gateway container                      <- unchanged, sole OpenRouter authority
    Main agent loop (primary model)
        |  calls investigate_repository(query)
        v
    Investigation sub-loop (cheap model)
        - tools: list_files, read_file, search_text  (Plan-mode set only)
        - own iteration / token / wall-time cap, debited from parent run budget
        |  reuses same executor connection, no new socket
        v
Executor (unchanged)                   <- same session for both loops
    read-only source -> ephemeral staging copy
```

The sub-loop is invoked synchronously from inside the tool-call handler for
`investigate_repository`; from the executor's point of view it is indistinguishable from the
main loop calling `read_file` three times in a row.

## Implementation checklist by component

### `shared/`

- [ ] Add a tool schema for `investigate_repository`:
  - input: `{ query: str, path_hint?: [str] }`
  - output: `{ summary: str, files_examined: [str], truncated: bool }`
- [ ] Add a result-envelope variant if the sub-loop's own read results need bounding metadata
      distinct from the parent's (likely: reuse the existing bounded-result convention as-is).
- [ ] Do **not** add this tool to the mutation/command tool family — it must be classified
      alongside `list_files` / `read_file` / `search_text` wherever tool families are declared,
      since that classification is what downstream permission logic keys off of (see Auto
      section below).

### `server/agent/` (runtime, context, budgets)

- [ ] Implement the `investigate_repository` tool handler in the agent runtime, not as a new
      subsystem.
- [ ] Handler opens a nested completion loop against the same OpenRouter client, with:
  - a fixed model ID read from the user's Settings value (`investigation_model_id`), not chosen
    dynamically by the primary model or re-evaluated per call — see the Settings section below.
  - a tool set hard-restricted to `list_files`, `read_file`, `search_text` — this must be
    enforced in code (a fixed allowlist passed into the nested loop), not just by prompting the
    cheap model to behave.
  - a token/iteration ceiling derived from the budget rule below, not a separate fixed constant.
- [ ] Debit the sub-loop's usage against the **parent run's existing budget**, not a separate
      pool. At call time, compute `remaining = parent_budget - parent_used_so_far` and cap the
      sub-loop's allowance at `0.5 * remaining`. Recompute this fresh on every call — a second
      `investigate_repository` call later in the same turn gets 50% of whatever remains *after*
      the first call's usage has already been debited, not 50% of the original run budget.
  - [ ] If `remaining` at call time is too small to run a useful investigation (below some
        floor — e.g. enough for only a couple of tool calls), fail the tool call closed with a
        normal budget-exceeded tool error rather than starting a sub-loop that immediately
        truncates.
- [ ] On completion, return only `{ summary, files_examined, truncated }` as the tool result to
      the parent. No file contents, no raw tool arguments cross back except what's in the
      declared output schema.
- [ ] On sub-loop failure (model error, tool error, budget exhaustion mid-investigation): return
      a normal tool-error result to the parent loop. Do **not** trigger the Auto
      downgrade-to-prompt behavior — that exists for interrupted mutation/publication, and this
      tool never mutates anything, so there's nothing to recover from.

### Desktop settings (model selection)

- [ ] Add a new Settings field, e.g. "Investigation model," alongside wherever the primary
      model is already selected — populated from the same model catalog the primary model
      picker already uses, so picking a cheap model is a selection, not free text.
- [ ] Persist the choice the same way other per-install/per-session settings are persisted
      today; propagate it to the gateway as the `investigation_model_id` config value the
      handler above reads.
- [ ] Require a value to be set before `investigate_repository` is offered to the primary model
      at all — if unset, the tool should be unavailable, not silently fall back to the primary
      model (which would defeat the point and spend at primary-model rates without telling
      anyone).
- [ ] No per-call override — once set in Settings, the model is fixed for the session; the
      primary model cannot request a different investigation model on a given call.

### Auto-mode compatibility (explicit focus)

This is the part that needs to be deliberate, not incidental:

- Per `AGENTS.md`, Auto "removes per-tool and publication prompts for otherwise valid calls" —
  but this only applies to tools that currently *require* a prompt in the first place. Read-only
  tools (`list_files`, `read_file`, `search_text`) already run without per-call approval in
  Agent mode today, in both prompt and Auto sessions; only the mutation/command/publish tools
  are gated by the approval mechanism.
- Therefore: **as long as `investigate_repository` is classified as a read-only tool in the
  permission/policy layer**, it inherits this behavior automatically and needs no new Auto-mode
  logic — it will already run without a prompt in prompt-mode sessions (since it was never
  gated) and without a prompt in Auto sessions (same reason). The risk is purely a
  classification bug: if it gets lumped in with mutation tools by mistake, it will incorrectly
  demand approval or incorrectly get swept into Auto's "requires clean staging" precondition.
- [ ] Confirm the tool-policy/permission layer treats `investigate_repository` as read-only,
      exempt from:
  - per-call prompt-mode approval,
  - Auto's "requires clean staging" precondition (it never touches staging state),
  - Auto's failure-triggered downgrade-to-prompt-approval behavior.
- [ ] Add an explicit test asserting `investigate_repository` is callable in an Auto session
      with dirty staging present (i.e. mid-way through an unpublished set of edits), and that
      calling it does not itself require an approval prompt in a prompt-mode session either.
- [ ] Add an explicit test asserting the sub-loop cannot call `apply_patch`, run a command, or
      any other mutation tool even if the cheap model is prompted or tricked into attempting it
      — the enforcement must be at the tool-allowlist level, not the system prompt level.

### `server/executor/` (Unix-socket client)

- No code changes expected. The sub-loop calls the exact same executor client methods the main
  loop already uses (`list_files`, `read_file`, `search_text`), through the same session, which
  means it automatically reads the **current staging state** — including any edits the main
  loop has already made earlier in the same run — rather than a stale read-only source snapshot.
  Confirm this is the case rather than assuming it; it's the behavior you want (an
  investigation should reflect in-flight edits), so no special-casing should be needed, but it's
  worth an explicit test.
- If Phase E's bounded read-only concurrency (up to four concurrent read-only calls) is in
  place, the sub-loop's internal calls should respect that same shared limit rather than
  introducing a second concurrency budget.

### `server/journal/`

- [ ] Add a nested event family: `subagent.started`, `subagent.tool_call`,
      `subagent.tool_result`, `subagent.completed`, `subagent.failed` — each carrying the
      parent run ID and the specific parent tool-call ID that triggered the delegation.
- [ ] These events must replay after reconnect like every other event family, per the existing
      event-envelope contract (schema version, event ID, run ID, type, timestamp, typed
      payload).
- [ ] Usage/cost fields on `subagent.completed` should be structured the same way the top-level
      run's usage fields are, so aggregation logic can be shared rather than duplicated.

### Desktop (`src/transcript.py`, `qml/Transcript.qml`)

- [ ] `investigate_repository` appears as a flat entry in the existing collapsed tool-name list
      for the turn, exactly like any other tool call — no expandable nested view, no new UI
      contract. (Decided against the expandable nested-entry option considered earlier; the
      full nested trace is still available in the journal per the Auto-mode/journal sections
      above if it's ever needed for debugging.)
- [ ] Decide how usage/cost is displayed for a turn that used two different models. At minimum,
      don't let the parent turn's cost/model summary imply the primary model did work it didn't.

### Tests

- [ ] Tool-allowlist enforcement: sub-loop cannot reach mutation/command/publish tools under any
      model-generated tool call.
- [ ] Budget: sub-loop cost debits the parent run's existing budget; parent run stops correctly
      when the combined total crosses limits.
- [ ] Auto-mode: `investigate_repository` runs without a prompt in both prompt-mode and Auto
      sessions; does not require clean staging; a sub-loop failure does not trigger Auto's
      downgrade-to-prompt behavior.
- [ ] Staging consistency: sub-loop reads reflect edits already staged earlier in the same run.
- [ ] Journal replay: nested `subagent.*` events replay correctly after a simulated reconnect.
- [ ] Journal-vs-UI: nested trace is present and complete in the journal even when the desktop
      transcript shows only the collapsed parent-level tool name.
- [ ] Budget: sub-loop allowance equals 50% of the parent run's *remaining* budget at call time,
      recomputed independently (against the smaller remaining pool) for a second call in the
      same turn.
- [ ] Call cap: a third `investigate_repository` call in the same turn is rejected with a clear
      tool error. A first call that fails or errors still counts toward the cap of two, leaving
      exactly one further attempt, not two.
- [ ] Settings gating: `investigate_repository` is not offered to the primary model when no
      investigation model is configured in Settings; it does not silently fall back to the
      primary model.

### Documentation

- [ ] Update `AGENTS.md`'s "Deliberate non-goals" section to reflect that scoped, read-only
      investigation delegation is now in scope, while general multi-agent orchestration
      (independent write/publish authority, separate executor sessions, concurrent staging)
      remains out of scope.
- [ ] Update `PROJECT_PLAN.md` §13 similarly, and add the new tool to the repository map/tool
      inventory wherever the other executor tools are documented.
- [ ] Do not create a second roadmap document for this — extend the existing canonical docs per
      the repo's documentation policy.
- [ ] Document the "prefer one call; use a second only if the first failed or was unusable"
      guidance wherever the primary model's tool-use instructions already live (system prompt /
      agent-facing guidance), worded consistently with the rest of that guidance, and note the
      hard cap of two calls per turn is enforced in code regardless of what the guidance says.

## Decisions

These were open questions in the previous draft; they're now resolved:

1. **Model selection** — fixed, not dynamically chosen. Selectable by the user in Settings (see
   the Settings section above), not decided by the primary model or by cheapest-price catalog
   lookup at call time.
2. **Sub-loop budget** — 50% of whatever the parent run's remaining budget is at the moment of
   the call, recomputed fresh on each call rather than fixed once per turn.
3. **Transcript UI** — flat collapsed entry, same as any other tool call. No expandable nested
   view in this version.
4. **Calls per turn** — the primary model may call `investigate_repository` at most twice in a
   single turn, enforced as a hard cap in code. Guidance should tell it to make one call and
   treat the result as sufficient in the normal case, using a second only if the first call
   failed, errored, or returned something clearly unusable (e.g. truncated before finding
   anything relevant) — not simply because the summary was inconclusive or more detail would be
   nice to have.

## Remaining open items

- Minimum useful budget floor (below which a call fails closed instead of running a sub-loop
  that would immediately truncate) — needs a concrete number once typical run budget sizes are
  known.
- Exact wording of the system-prompt guidance on when a second call is warranted — needs to live
  wherever the rest of this agent's tool-use guidance already lives, worded consistently with it.
- Whether the Settings picker should filter/sort the model catalog by input-token price to make
  "pick something cheap" easy, or just list all catalog models and trust the user to choose.
