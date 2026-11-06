# Context compaction limits plan — 5% threshold and trigger correctness

Status: implemented. Extends [MODEL_CONTROLS_PLAN.md](MODEL_CONTROLS_PLAN.md) §1 (the user-set
compaction threshold, already implemented in the current tree). No new tool, no protocol field,
and no change to the publication or sandbox boundary. The delta is the setting range, the
effective high-water floor, and the point at which compaction runs.

The two user-visible requirements:

1. The threshold can be set as low as 5% of the model's published context window, including the
   5–10% range the reporter asked for.
2. Once the context reaches that limit, the next ordinary model call compacts; a run must not
   place several API calls after the limit has already been reached.

## 1. Today

- The desktop setting is an integer percentage in `COMPACTION_SHARE_RANGE = (10, 60)`
  (`src/desktop/preferences.py:62`). The Settings spin box is `from: 10`, `to: 60`, step 5
  (`qml/Main.qml:716-724`). `docs/LIMITS.md:51` documents 10–60%.
- `compaction_threshold_tokens(window, share)` (`src/controllers.py:28-36`) turns the share into
  tokens, clamped to the protocol range 4,000–2,000,000. `GenerationController.prepare`
  (`src/controllers.py:159-190`) uses `min(threshold, context_limit)` as the chat compaction
  target and sends the token value to the gateway.
- The gateway's `_high_water` (`server/agent/runtime.py:1690-1709`) takes the smallest of the
  profile's working-set cap (150,000 / 240,000 / 400,000), the user's threshold, 75% of the
  request budget, and the capacity left after one output turn plus a 32,000-token growth reserve.
  It floors at 4,000. The low-water target is a third of the high water.
- `_maybe_compact` (`runtime.py:958-1034`) runs once per loop iteration, before the model call.
  It estimates the prompt with `estimate_message_tokens(messages)`, which serializes only the
  messages. It does not include the offered tool schemas or provider framing, and it does not use
  the previous turn's reported `prompt_tokens`. A run can therefore make calls whose actual
  prompt is above the mark while the estimate is still below it.
- The summary turn is sent with the full pre-compaction messages plus `HANDOFF_REQUEST`
  (`runtime.py:984-989`). That is one deliberate model call at or above the mark.
- Compaction returns to `low = max(1,000, high / 3)` plus the newest exchange. If the static
  prefix plus the handoff plus the newest exchange is larger than `high`, the compacted context is
  still at or above `high`; the next iteration compacts again, paying for a summary turn every
  turn. At a 5% mark on a 200k window (10,000 tokens), the static prompt and session context can
  already exceed the mark.
- The hard ceiling is 90% of the request budget (`HARD_CEILING_WINDOW_SHARE`), checked after
  compaction. It is a safety clamp, not the trigger.

The current tests pin the arithmetic in `tests/unit/gateway/test_context_limits.py`, including the
10–60% setting range indirectly through the desktop tests
(`tests/unit/desktop/test_desktop_services.py:1189-1222`) and the QML form
(`tests/structural/test_qml_structure.py:131-140`).

## 2. Decisions

| Decision | Choice |
|---|---|
| D1 | Widen the setting to **5–60%**, step 1, Automatic still the default. |
| D2 | Keep the existing request-derived bounds (75% share, capacity, 4,000 floor) and add a **fixed-context floor**: the effective mark is never below what the static prefix, one recent exchange, and the hysteresis need. |
| D3 | Trigger compaction at `effective − COMPACTION_REQUEST_RESERVE_TOKENS`, so the summary request itself is not sent above the effective mark. |
| D4 | Estimate the next prompt from the previous turn's reported `prompt_tokens` plus the new messages, and add the measured tool-schema overhead. The previous actual prompt is a lower bound. |
| D5 | After compaction, if the result is still at or above the trigger, raise the effective mark to the compacted size plus hysteresis and journal it; never compact on every turn. |
| D6 | Show the requested token count and the fixed-context floor in Settings; journal the effective mark on every run. |
| D7 | If the fixed context alone cannot fit the request budget, fail closed with a named reason instead of sending a prompt the window cannot hold. |

The 5% setting is honored where it is physically possible. On a 1M window it is about 52,000
tokens, comfortably above the fixed prefix. On a 200k window it is 10,000 tokens, which can be
below the fixed prefix; the gateway raises the effective mark to the floor and says so. The
setting is never silently reinterpreted as a different percentage.

## 3. Design

### 3.1 Setting range

- `src/desktop/preferences.py:62`: `COMPACTION_SHARE_RANGE = (5, 60)`. The error text becomes
  "Enter a whole percentage from 5 to 60 of the model's window."
- `qml/Main.qml:716-724`: `from: 5`, `to: 60`, `stepSize: 1`, `editable: true`. The helper text
  explains the floor (3.5).
- `src/controllers.py:28-36`: unchanged; the 4,000-token protocol floor still applies. On a
  window below 80,000 tokens, 5% is below 4,000 and the protocol floor wins; the form says so.
- No protocol change: `AgentRunRequest.compaction_threshold_tokens` already accepts 4,000–
  2,000,000 (`shared/requests.py:180-190`), and `_high_water` already consumes it.

### 3.2 The effective high-water mark

Keep `_high_water(request, profile)` as the pure request-derived mark so its table stays easy to
test. Add an effective wrapper used by the runtime:

```text
effective_high_water(request, profile, messages, run_id):
    requested = _high_water(request, profile)
    prefix = estimate_message_tokens(leading_system_messages(messages))
    floor = prefix + EXCHANGE_RESERVE_TOKENS + COMPACTION_HYSTERESIS_TOKENS
    learned = self._compaction_floor.get(run_id, 0)
    effective = max(requested, floor, learned)

    if effective > int(context_limit * HARD_CEILING_WINDOW_SHARE):
        raise ContextFloorTooLarge(...)
    if effective + profile.max_output_tokens + TURN_GROWTH_RESERVE_TOKENS > context_limit:
        raise ContextFloorTooLarge(...)
    return effective
```

Constants, next to the existing compaction constants in `server/agent/runtime.py:78-84`:

| Constant | Value | Purpose |
|---|---:|---|
| `EXCHANGE_RESERVE_TOKENS` | 2,000 | Room for the newest assistant/tool exchange that `recent_exchanges` always keeps |
| `COMPACTION_HYSTERESIS_TOKENS` | 2,000 | Keeps the compacted context below the trigger so the next turn does not compact again |
| `COMPACTION_REQUEST_RESERVE_TOKENS` | 1,000 | Room for `HANDOFF_REQUEST` and provider framing in the summary call |
| `PROMPT_ESTIMATE_MARGIN_TOKENS` | 2,000 | Covers the difference between the local estimate and the provider's count |
| `MIN_HIGH_WATER_TOKENS` | 4,000 | Unchanged protocol floor |

The floor uses the actual `_leading_system(messages)` prefix, which includes the static prompt and
the rendered session context. It is computed after `_session_layout`, not from the raw request
messages. The model-written handoff is bounded to the room that remains; if it cannot fit, the
existing deterministic fallback is used. The floor does not include the full handoff cap, because
the handoff is optional and can shrink.

The effective mark is capped by the 90% hard ceiling. If the floor alone exceeds that ceiling,
`ContextFloorTooLarge` pauses the run with `context.floor_too_large` and a message naming the
prefix size, the requested mark, and the prompt budget. This is a fail-closed path for a window
that cannot hold the fixed context plus one turn; it is better than sending a prompt the provider
will reject.

### 3.3 Trigger point

`_maybe_compact` uses:

```text
trigger = max(MIN_HIGH_WATER_TOKENS, effective - COMPACTION_REQUEST_RESERVE_TOKENS)
if before < trigger:
    return messages
```

`before` is the best estimate of the next prompt (3.4). The summary request is
`[*messages, HANDOFF_REQUEST]`; with `before < trigger`, its prompt is at most
`effective − 1,000 + request`, which stays under the effective mark. The summary output
(`SUMMARY_MAX_TOKENS = 2,500`) is covered by the high-water derivation's output reserve.

The `context.compacted` event gains `trigger_tokens` and `effective_high_water_tokens`. The
`run.started` and `context.inspected` payloads keep `compaction_threshold_tokens` as the
request-derived mark; a new `context.high_water` event records the effective mark after the
session layout, with `requested_tokens`, `effective_tokens`, `floor_tokens`, and `binding`
(`requested`, `prefix`, `learned`, or `capacity`).

### 3.4 Estimating the next prompt

The current estimate counts only message JSON. Add two corrections:

1. **Tool-schema overhead.** At run start, serialize the offered schemas (`tool_schemas(mode,
   offered)`) and add `estimate_message_tokens` of that JSON to a `schema_tokens` value. The
   schemas are fixed for the session, so this is measured once.
2. **Learned provider overhead and the last actual prompt.** After every turn, record:

```text
overhead = max(0, turn.usage.prompt_tokens - estimate_message_tokens(messages_before_turn)
                   - schema_tokens)
self._prompt_overhead[run_id] = max(previous_overhead, overhead)
self._last_prompt_tokens[run_id] = turn.usage.prompt_tokens
self._last_prompt_message_count[run_id] = len(messages_before_turn)
```

At the next compaction check:

```text
new_messages = messages[self._last_prompt_message_count[run_id]:]
before = max(
    estimate_message_tokens(messages) + schema_tokens + self._prompt_overhead[run_id]
        + PROMPT_ESTIMATE_MARGIN_TOKENS,
    self._last_prompt_tokens[run_id] + estimate_message_tokens(new_messages),
)
```

The first term covers the full message list with the provider's measured framing and a 2,000-token
margin; the second prevents a provider that reports more than the local estimate from being
ignored. On the first turn there is no last prompt, so `schema_tokens`, an initial overhead of
zero, and the margin are used. The margin is why the trigger reserve can be only 1,000 tokens:
the summary request is still sent below the effective mark when the estimate is off by up to the
margin.

This is the direct fix for "a few API calls placed after the limit": the previous turn's actual
`prompt_tokens` is now a lower bound, so the loop compacts before the next call instead of waiting
for the message-only estimate to catch up.

### 3.5 After compaction

- Compute `after = estimate_message_tokens(compacted)`.
- If `after >= trigger`, set `self._compaction_floor[run_id] = after + COMPACTION_HYSTERESIS_TOKENS`
  and journal `context.compaction_floor` with `compacted_tokens`, `requested_high_water_tokens`,
  `effective_high_water_tokens`, and `floor_tokens`. Subsequent checks use the raised mark, so the
  run does not pay for a summary turn every iteration.
- If `after` is below the trigger, clear any learned floor for the run (the requested mark is
  working again).
- Reset `_last_prompt_message_count` to `len(compacted)` and `_last_prompt_tokens` to 0 after
  compaction. The next turn records the compacted prompt's actual count; keeping the old index
  would make `new_messages` refer to the pre-compaction list.
- Keep the existing hard-ceiling deterministic fallback. It is the last line of defense, not the
  normal path.

### 3.6 Settings and inspection

- `SettingsService.context_compaction_view` (`src/desktop/preferences.py:184-199`) keeps
  `share`, `automatic`, `windowTokens`, and `resolvedTokens` (the requested token count). Add
  `minimumTokens`, a conservative fixed-context floor for the current model: the static prompt
  size plus a fixed session-context allowance plus the exchange and hysteresis reserves. The
  desktop cannot know the gateway's exact rendered prefix, so the form labels this an estimate
  and the run journal carries the exact effective mark.
- The QML helper text becomes:

```text
Requested: about 10,000 tokens of a 200,000-token window (5%). The gateway may raise this to
about 17,000 tokens so the fixed prompt and one exchange fit; the run journal shows the mark
actually used.
```

- `inspect_context` (`src/desktop/conversations.py:270-300`) reports the requested threshold and
  the last run's effective mark when the journal has one, instead of restating the submission
  limit.

## 4. Bounds and examples

The request-derived mark is unchanged: `min(threshold or profile cap, 75% of budget, budget −
16,000 − 32,000)`, floored at 4,000. The effective mark adds the fixed-context floor and any
learned floor, then must satisfy `effective + max_output + 32,000 <= prompt budget`.

| Window | Prompt budget (80%) | Requested at 5% | Requested at 10% | Typical effective at 5% | Typical effective at 10% |
|---|---:|---:|---:|---:|---:|
| 1,048,576 | 838,860 | 52,428 | 104,857 | 52,428 | 104,857 |
| 400,000 | 320,000 | 20,000 | 40,000 | 20,000 | 40,000 |
| 200,000 | 160,000 | 10,000 | 20,000 | about 17,000 (floor) | 20,000 |
| 128,000 | 102,400 | 6,400 | 12,800 | about 17,000 (floor) | about 17,000 (floor) |

The "typical floor" assumes a 13,000-token fixed prefix plus the 2,000-token exchange reserve and
2,000-token hysteresis. The exact value is journaled per run. A 5% request is honored exactly on
windows of roughly 340,000 tokens and above; smaller windows report the raised mark rather than
pretending the request was met.

## 5. Tests

- **Range:** `tests/unit/desktop/test_desktop_services.py:1189-1222` accepts 5 and rejects 4;
  the existing 9 case moves to 4. `tests/structural/test_qml_structure.py:131-140` asserts
  `from: 5` and `stepSize: 1`. `tests/unit/desktop/test_controllers.py` adds a 5% case for a 1M
  window (`52,428` tokens).
- **Effective mark:** extend `tests/unit/gateway/test_context_limits.py` with a table for
  `_effective_high_water`: a 1M window at 5% keeps 52,428; a 200k window at 5% raises to the
  prefix floor; a 128k window at 10% raises to the floor; the effective mark plus output and
  reserve never exceeds the prompt budget; a floor above the 90% ceiling raises
  `context.floor_too_large`.
- **Trigger:** a scripted provider that reports `prompt_tokens` from the serialized payload
  (not the fixed 100-token fixture) drives a run past the mark. Assert that a `context.compacted`
  event appears before the first `usage.turn` whose `prompt_tokens` exceeds the effective mark,
  and that the summary request itself is at or below the mark. Add the same assertion for a
  low 5% mark.
- **No repeated compaction:** a session with a fixed prefix larger than the requested mark
  compacts once, journals `context.compaction_floor`, and does not emit a `context.compacted`
  event on every following turn.
- **Chat path:** `GenerationController.prepare` with a 5% share compacts to the 5% target and
  passes `compaction_threshold_tokens` to the gateway; `inspect_context` reports the requested
  and effective marks.
- **Regression name:** "a 5% threshold compacts before the next ordinary call".

## 6. Documentation

- `docs/LIMITS.md:51`: change 10–60% to 5–60%, describe the fixed-context floor, the trigger
  reserve, and the learned floor. Keep the existing 75%/capacity/4,000 bounds.
- `docs/MODEL_CONTROLS_PLAN.md` §1.2/§1.4: change the range to 5–60% and link here for the floor
  and trigger; the rest of Phase 1 stands.
- `docs/ARCHITECTURE.md` "Compaction": one sentence that the effective mark includes the fixed
  prefix floor and that the summary request is triggered below the mark.
- `docs/README.md`: index this plan.
- `CHANGELOG.md` under `## Unreleased` when the change lands.

## 7. Order of work

1. Range change in the desktop, QML, and their tests (5–60%).
2. Effective mark and fixed-context floor in the runtime, with the `context.high_water` event.
3. Prompt estimation from the previous actual tokens and the tool-schema overhead.
4. Trigger reserve and the summary-request test.
5. Learned floor after compaction and the no-repeated-compaction test.
6. Settings helper, inspection, docs, and the full check tiers.

## 8. Risks

| Risk | Mitigation |
|---|---|
| A 5% request is raised by the fixed prompt, so the user does not get the number they asked for. | The form shows the requested and estimated effective marks before saving; the run journal records the exact effective mark and the binding reason. |
| Compaction at a low mark runs often, increasing summary cost. | The hysteresis and learned floor prevent per-turn summaries; the summary turn is bounded to 2,500 output tokens and its cost is journaled in `context.compacted`. |
| Using the previous actual prompt as a lower bound compacts earlier than the message estimate suggests. | That is the point: the actual prompt is the provider's count. The trigger reserve keeps the summary call below the mark, and the effective mark is journaled so the behavior is auditable. |
| The fixed-context floor exceeds the window on a small model. | `ContextFloorTooLarge` fails closed with a named reason before an over-limit provider call; the user can choose a larger window or a higher threshold. |
| The desktop's `minimumTokens` estimate differs from the gateway's exact prefix. | The form labels it an estimate; the journal and `inspect_context` report the exact effective mark from the run. |

## 9. Definition of done

- Settings accepts 5–60% in 1% steps, with Automatic still the default; 4% is rejected.
- A 5% threshold on a 1M window compacts at about 52,000 tokens; on a 200k window the form and
  the journal show the raised fixed-context floor instead of silently missing the request.
- No ordinary model call after the first turn is knowingly sent above the effective mark: the
  previous turn's actual `prompt_tokens` is a lower bound, the learned overhead and the 2,000-token
  margin cover estimate error, and the summary request is triggered below the mark. A provider
  that reports more than the margin is corrected on the next turn and recorded in the journal.
- A session whose fixed prefix is larger than the requested mark compacts once, records the
  learned floor, and does not compact every turn.
- `docs/LIMITS.md`, `docs/MODEL_CONTROLS_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/README.md`, and
  `CHANGELOG.md` are updated with the change, and the checks in `docs/TESTING.md` pass.
