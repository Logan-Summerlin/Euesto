# Model controls plan — context threshold, cost estimate, thinking levels

Status: proposed. Three user-facing adjustments to desktop model controls. No public tool,
permission, or sandbox behaviour changes; the gateway protocol gains one optional request field
and one widened enum.

Scope, by phase:

| Phase | Change | Size |
|---|---|---|
| 1 | The user sets the context compaction threshold in Settings. | M |
| 2 | The model picker estimates cost per million tokens at 95% input / 5% output with a 95% cache hit rate. | S |
| 3 | Thinking levels are low, medium, high, extra high, and max; `minimal` is removed and every selection reaches the provider as the right `reasoning.effort`. | M |

Non-goals: per-model compaction thresholds, a second provider adapter, prompt-preset reasoning
defaults (still harness plan 7.4), and any change to profile cost budgets, result caps, or
approval policy.

## 1. User-set context compaction threshold

### 1.1 Today

- The prompt budget is a desktop constant: `src/controllers.py:144` computes
  `int(model_context_length(...) * 0.8)` inside `GenerationController.prepare`, which feeds both
  chat compaction (`compact_messages`) and `AgentRunRequest.context_limit_tokens`
  (`src/desktop/generation.py:420`).
- The gateway derives the compaction high-water mark from that budget: `_high_water`
  (`server/agent/runtime.py:1663`) takes the smallest of the profile's `working_set_tokens`
  (`server/agent/budgets.py:42-70`: 150,000 / 240,000 / 400,000), 75% of the prompt budget
  (`HIGH_WATER_WINDOW_SHARE`), and the room left after one output turn plus the 32,000-token
  growth reserve, with a 4,000-token floor (`MIN_HIGH_WATER_TOKENS`). Compaction returns to a
  third of the mark (`LOW_WATER_SHARE`); a safety clamp fires above 90% of the budget
  (`HARD_CEILING_WINDOW_SHARE`).
- Two desktop paths restate the constant instead of reading a setting: manual compaction targets
  40% of the window (`src/desktop/conversations.py:249-250`) and context inspection reports 80%
  (`src/desktop/conversations.py:276`).

So the only user-visible knob today is the model choice; the threshold itself is not settable.
The arithmetic and its invariants are pinned by `tests/unit/gateway/test_context_limits.py`.

### 1.2 Decision

One new global setting, expressed as a share of the selected model's published window so it
follows model switches:

- **Setting:** `context_compaction_share`, an integer percentage of the model's window. Empty or
  absent means *Automatic*, today's derivation. Range 10–60.
- **Default:** Automatic, so behaviour is unchanged until the user opts in.
- **Effective threshold:** `int(window * share / 100)` is sent as a new optional request field and
  the gateway still takes the minimum with the 75% share, the capacity bound, and the floor. This
  is what makes the setting safe at every window size: the user moves the mark inside the
  existing envelope, never outside it.
- **Ownership:** the desktop owns the setting and turns it into tokens; the gateway owns the
  clamping and records the effective mark. The share is never interpreted gateway-side, so a
  stored run stays reproducible against a fixed prompt budget.

Because the `coding` cap is 150,000 tokens, on any window at or above roughly 250,000 tokens the
profile cap is the binding limit, so without this setting the user cannot raise the mark at all. A
threshold above the profile cap is therefore honoured once set — that is the point of the setting
for large windows — while `result_cap` and the profile cost budget stay profile-derived and
unchanged. On a smaller window the capacity and 75% bounds still bind: a threshold above them
changes nothing, and one below them lowers the mark.

### 1.3 Changes

| File | Change |
|---|---|
| `shared/requests.py` | `AgentRunRequest` gains `compaction_threshold_tokens: int \| None = None`; validate 4,000–2,000,000 when set (the same bounds as `context_limit_tokens`, `:187`), add it to `to_dict` and to the `from_dict` expected set. No other protocol change; the thirteen tools are untouched. |
| `server/agent/runtime.py` | `_high_water`: `min(request.compaction_threshold_tokens or profile.working_set_tokens, int(limit * HIGH_WATER_WINDOW_SHARE), capacity)`, still floored at `MIN_HIGH_WATER_TOKENS`. Add `compaction_threshold_tokens` (the resolved mark) to the `run.started` / `run.resumed` payload at `:293-301`. |
| `src/controllers.py` | Keep `CONTEXT_WINDOW_SHARE = 0.8` as a named constant; `PreparedGeneration` gains `compaction_threshold: int \| None`, computed from the saved share and the model window. Chat compaction targets `min(threshold, context_limit)` instead of the raw budget, so one setting governs both modes. |
| `src/desktop/generation.py` | Pass `compaction_threshold_tokens=prepared.compaction_threshold` to `AgentWorker.for_run`. |
| `src/gateway_client.py` | `stream_agent` (`:307`) gains `compaction_threshold_tokens: int \| None = None` and includes it in the run payload. |
| `src/desktop/preferences.py` | `context_compaction_share()` / `set_context_compaction_share(value)` with range 10–60 and empty = automatic, plus a view (`{"share", "automatic", "windowTokens", "resolvedTokens"}`) for the Settings form. The desktop cannot compute the gateway's effective mark (the profile cap is gateway-side), so the form reports the requested threshold in tokens while the run journal carries the resolved one. Reject out-of-range input with an error dialog rather than clamping silently, matching the runner settings. |
| `src/qml_backend.py` | `@Property("QVariantMap") contextThreshold` (notify `settingsChanged`) and `@Slot(int) saveContextCompactionShare(int)`; both delegate to `SettingsService`. |
| `qml/Main.qml` | In Settings → Conversation, under *Model controls* (`:681`): a "Compact context at" spin/field (`objectName: "compactionShare"`) with an *Automatic* checkbox, helper text showing the resolved token count for the current model, and a save button calling `backend.saveContextCompactionShare`. |
| `src/desktop/conversations.py` | Manual compaction targets half the configured threshold (today's 40% is half of 80%), and `inspect_context` reports the configured submission limit and threshold instead of restating 80%. |

### 1.4 Bounds and clamping

| Quantity | Value | Where |
|---|---:|---|
| Setting range | 10–60% of the model's window | desktop form, fails closed |
| Protocol field range | 4,000–2,000,000 tokens | `AgentRunRequest.__post_init__` |
| Effective high water | `min(threshold, 75% of budget, budget − 16,000 − 32,000)`, floor 4,000 | `_high_water` |
| Safety clamp | 90% of the prompt budget | unchanged, `runtime.py:983` |

An out-of-range stored value loads as Automatic; there is no migration, since a missing setting
is already the default.

### 1.5 Tests

- `tests/unit/gateway/test_context_limits.py`: add a user-threshold table — 1M window with a
  240,000 threshold yields 240,000 (setting binds, above the profile cap); 1M window with a
  700,000 threshold is clamped by the 75% share to 629,145 (`int(838,860 × 0.75)`; assert the minimum and that
  `high + max_output_tokens + reserve <= limit`); 128k window with a 200,000 threshold stays
  capacity-bound at 54,400 exactly as today. Extend
  `test_the_mark_never_reaches_the_safety_clamp_or_the_output_cap` to run with a threshold set,
  and round-trip the new field through `to_dict` / `from_dict`, including a snapshot without it
  (defaults to `None` = profile cap).
- `tests/unit/desktop/test_desktop_services.py`: share validation (10–60, junk, empty =
  automatic) and the resolved-token view.
- `tests/unit/desktop/test_controllers.py`: `prepare` passes the threshold through for both a
  window-derived default and a saved share; manual compaction and inspection use the setting.
- `tests/structural/test_qml_structure.py`: the settings form exposes `compactionShare` and calls
  `backend.saveContextCompactionShare`.
- New regression test named for the user-visible behaviour: "a saved threshold changes where an
  agent run compacts".

### 1.6 Documentation

- `docs/LIMITS.md` ("Context, output, and steering"): extend the working-set row — the user
  setting may lower or raise the high water inside the 75%-share, capacity, and 4,000-token
  bounds.
- `docs/ARCHITECTURE.md` §Compaction: one sentence that the high water is the smallest of the
  profile cap, the user's threshold, and the request-derived bounds.
- `CHANGELOG.md` under a new `## Unreleased` heading.

## 2. Model picker cost estimate

### 2.1 Today

`ModelOption.average_price_per_million` (`src/models.py:99-103`) averages the input and output
prices 50/50. The picker shows it as `$X/M avg` (`qml/Main.qml:446`) and the price filter matches
on it (`filter_model_entries`, `src/model_catalog.py:72`); the entry is built in
`src/desktop/preferences.py:224`, and `INVESTIGATION_MODEL_ENTRY["price"]` (`:57`) hard-codes
0.21. That figure describes a workload no agent run has: agent prompts are overwhelmingly input,
and most of that input is served from the prompt cache.

### 2.2 Decision

Replace it with one blended estimate per million tokens, matching how the harness plan models a
run (95%+ cache hit rate, bounded output):

```text
ESTIMATED_INPUT_SHARE        = 0.95   # 95% of tokens are input
ESTIMATED_OUTPUT_SHARE       = 0.05   # 5% of tokens are output
ESTIMATED_CACHE_HIT_SHARE    = 0.95   # 95% of input tokens hit the prompt cache

blended_per_million =
    0.95 * (0.95 * cached_prompt_price + 0.05 * prompt_price)   # input half
  + 0.05 * completion_price                                     # output half
```

all per 1,000,000 tokens, i.e. `0.9025·cached + 0.0475·input + 0.05·output`.

Fallbacks, fail open only where a price is genuinely absent:

| Catalog data | Treatment |
|---|---|
| all three prices | the formula above |
| no `cached_prompt_price` | all input at `prompt_price` (`0.95·input + 0.05·output`) — the honest upper bound, already flagged by `model_warnings` |
| no `prompt_price`, cached present | uncached input share at the cached price |
| no `completion_price` | output share at the cached-input price |
| none of the three | `None`: the entry shows "Prices load with the catalog" and is excluded by any price filter, as today |

### 2.3 Changes

| File | Change |
|---|---|
| `src/models.py` | Add the three module constants next to the dataclass; replace `average_price_per_million` with `blended_price_per_million` (no alias — no compatibility shims) carrying the fallback table in its docstring. |
| `src/desktop/preferences.py` | Use the new property for the entry's `price`; drop the hard-coded 0.21 in `INVESTIGATION_MODEL_ENTRY` in favour of `None` so the fallback entry stops asserting a stale number. |
| `qml/Main.qml` | Label becomes `"$" + Number(modelData.price).toFixed(3) + "/M est."` with a `ToolTip`: "Estimated at 95% input, 5% output, 95% cache hits". |
| `src/model_catalog.py` | No logic change; `filter_model_entries` keeps filtering on the same `price` key, so the existing thresholds now mean the blended figure. |

### 2.4 Tests

- `tests/unit/desktop/test_model_catalog.py`: pin the blended value for a cached model
  (`prompt 0.3`, `cached 0.03`, `completion 2.5` → `0.166`), the uncached fallback
  (`0.95·input + 0.05·output`), the completion-missing fallback, and `None` for an
  all-unknown model. Keep the existing filter tests and flip `ENTRY["price"]` expectations only
  where the value itself is asserted.
- `tests/structural/test_qml_structure.py:115`: keep `modelData.priceText` / `modelData.warnings`,
  and assert the estimate label says `est.`.

## 3. Thinking levels

### 3.1 Today

- Vocabulary: `REASONING_EFFORTS = (None, "minimal", "low", "medium", "high")`
  (`shared/requests.py:121`), duplicated as a frozenset in `src/desktop/preferences.py:32`.
- The desktop stores the raw string per model in `model_options:<model>`
  (`preferences.py:84-89`, `:199-201`) and the QML combo shows
  `["default", "minimal", "low", "medium", "high"]` (`qml/Main.qml:177-181`).
- The value reaches the provider unmapped, in two places: the chat payload
  (`server/openrouter/client.py:125-126`, gated on `"reasoning" in supported_parameters`) and the
  agent payload (`server/openrouter/agent.py:363-364`).
- **Gap:** agent runs never carry an effort from the desktop. `AgentRunRequest.reasoning_effort`
  exists and `server/agent/runtime.py:543` forwards it, but `GatewayClient.stream_agent`
  (`src/gateway_client.py:307-336`) has no such argument and `src/desktop/generation.py` never
  passes one, so Plan and Agent runs always send no reasoning field. The gateway summary turns
  additionally hard-code `"low"` (`runtime.py:742`, `:960`), which stays correct.

### 3.2 Vocabulary and wire mapping

The user-facing and stored vocabulary becomes five levels plus unset:

| Selection (stored token) | Label | `reasoning.effort` sent |
|---|---|---|
| *(unset)* | Default | field omitted |
| `low` | Low | `low` |
| `medium` | Medium | `medium` |
| `high` | High | `high` |
| `extra_high` | Extra high | `xhigh` |
| `max` | Max | `max` |

The two levels above `high` are provider-dependent, so the mapping lives in exactly one place —
`shared/requests.py`, next to the vocabulary — as both the wire map and an ordered fallback chain:

```text
REASONING_EFFORTS          = (None, "low", "medium", "high", "extra_high", "max")
REASONING_EFFORT_PAYLOADS  = {"low": "low", "medium": "medium", "high": "high",
                              "extra_high": "xhigh", "max": "max"}
REASONING_EFFORT_FALLBACKS = {"max": ("max", "xhigh", "high"),
                              "extra_high": ("xhigh", "high", "medium")}
reasoning_payload_effort(effort) -> str | None      # map, or None when unset
```

Implementation gate: before writing the map, confirm the current accepted `reasoning.effort`
values against OpenRouter's reasoning documentation (one `fetch_url` call; the page is outside
the default allowlist, so this asks once). If a value is not accepted, drop it from the map and
from the fallback chain rather than inventing an equivalent: the chain then degrades
`max → extra_high → high`. Every level must reach the provider as *some* effort the API accepts;
none may be silently dropped.

Fallback behaviour at run time: if a provider rejects a turn with a 400 that names the reasoning
effort, the gateway retries once with the next level down the chain for that selection, journals
`reasoning.effort_clamped` with the requested and sent values, and records the value actually
sent in `run.started`. A rejection after the chain is exhausted surfaces as the existing typed
provider error; no further retries are added.

### 3.3 Changes

| File | Change |
|---|---|
| `shared/requests.py` | New vocabulary, wire map, fallback chain, and `reasoning_payload_effort`; `__post_init__` rejection text unchanged in shape ("Unknown reasoning effort"). |
| `server/openrouter/agent.py` | `payload["reasoning"] = {"effort": reasoning_payload_effort(reasoning_effort)}` — never the raw stored token. |
| `server/openrouter/client.py` | Same mapping for the chat payload, still gated on `"reasoning" in supported_parameters`. |
| `src/desktop/preferences.py` | Import the vocabulary from `shared.requests` instead of redefining it; `_request_options` maps a stored legacy `"minimal"` to `"low"` so old saved options keep working (no data rewrite); `set_reasoning_effort` validates against the shared tuple. |
| `src/gateway_client.py` | `stream_agent` gains `reasoning_effort: str \| None = None` and includes it in the run payload; this closes the agent gap. |
| `src/desktop/generation.py` | Pass `reasoning_effort=options.reasoning_effort` for agent runs only when `"reasoning" in prepared.supported_parameters`, mirroring the chat path's gate. |
| `src/qml_backend.py` | `reasoningEffort` keeps returning the stored token or `"default"`; add `@Property("QVariantList") reasoningLevels` (display/values pairs) and `@Property(bool) reasoningSupported` so the control can disable itself for non-reasoning models. |
| `qml/Main.qml` | Combo (`:177`) uses `backend.reasoningLevels` with `textRole`/`valueRole`, `displayText: "Think: " + currentText`, `onActivated: backend.setReasoningEffort(currentValue)`, `enabled: !backend.generating && backend.reasoningSupported`, and a tooltip naming the wire level sent. |

Model capability gating is the same on both paths: a model without `reasoning` in
`supported_parameters` sends no reasoning field regardless of the stored selection, and the
control shows the stored choice greyed out rather than resetting it.

### 3.4 Tests

- New `tests/unit/shared/test_reasoning_levels.py`: the vocabulary is exactly
  `(None, low, medium, high, extra_high, max)`; `minimal` is rejected; the wire map is total over
  the vocabulary; the fallback chain is a subset of accepted values, strictly descending, and
  ends at `high`.
- `tests/unit/gateway/test_harness_loop.py` (near `:636`): `agent_payload` sends the mapped wire
  value for each level, omits the field for unset, and a provider 400 naming the effort is
  retried once with the next level and journals `reasoning.effort_clamped`.
- `tests/unit/gateway/test_gateway.py`: the chat payload maps `extra_high` to `xhigh` and omits
  the field when the model lacks `reasoning`.
- `tests/unit/desktop/test_desktop_services.py`: stored `"minimal"` loads as `"low"`; an unknown
  value loads as unset (the existing `"extreme"` case); saving each of the five levels round-trips.
- `tests/unit/desktop/test_gateway_client.py`: `stream_agent` puts `reasoning_effort` in the run
  body and omits the key when unset.
- `tests/structural/test_qml_structure.py`: the thinking combo offers exactly the five levels,
  offers no `minimal`, and reads `backend.reasoningLevels`.

### 3.5 Documentation

- `docs/AGENTIC_HARNESS_PLAN.md` §1.8 (implemented): the payload carries the request's mapped
  effort, the five levels, and the agent-path wiring.
- `CHANGELOG.md`: `minimal` removed, levels added, the mapping and fallback chain named.

## 4. Order of work

1. Phase 3's vocabulary and mapping in `shared/requests.py` (both other phases touch the same
   settings surface, and the mapping is the risky part).
2. Phase 3 payload and desktop wiring, with its tests.
3. Phase 2 (self-contained, one property, one label).
4. Phase 1 protocol field and `_high_water`, then the desktop setting and QML form.
5. Documentation, `docs/README.md` index row, and `CHANGELOG.md`.

## 5. Verification

Run the tiers that apply, from `docs/TESTING.md` ("Required checks"):

```text
pytest -m "not slow and not docker"
pytest -m "slow and not docker"
ruff check . && ruff format --check .
python -m compileall -q app.py src server shared executor egress tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
python scripts/validate.py qml-smoke
```

No provider credentials and no network are needed: payload tests use the existing fake transport
and the credential-free scripted-provider scenarios.

## 6. Risks

| Risk | Mitigation |
|---|---|
| A user threshold above the profile cap raises prompt cost per turn. | The 75%-share, capacity, and 90% clamp bounds are unchanged; per-run cost budgets are unchanged, so a run still stops at its cost limit. The form shows the resolved threshold in tokens before the setting is saved. |
| `xhigh` / `max` are not universally accepted, so a selection could fail a run. | One mapping table plus a single clamped retry, journaled; the UI names the wire level per selection. |
| The picker's blended estimate reads as a promise. | Label it `est.` and state the three assumptions in the tooltip; the run meter keeps showing actual cost. |
| Two paths (chat, agent) drifting on reasoning gating. | The desktop computes support once from `supported_parameters` and both payload builders share the mapping. |

## 7. Definition of done

- Settings lets the user set the compaction threshold; the resolved mark is visible, bounded, and
  journaled on `run.started`; the automatic default reproduces today's numbers exactly.
- The picker shows `$X/M est.` from the 95/5/95 blend with the documented fallbacks, and the price
  filter uses the same number.
- The thinking control offers low, medium, high, extra high, and max; `minimal` is gone and a
  stored `minimal` loads as `low`; chat and agent requests both send the mapped effort for models
  that support reasoning and omit it otherwise.
- `docs/LIMITS.md`, `docs/ARCHITECTURE.md`, `docs/AGENTIC_HARNESS_PLAN.md`, `docs/README.md`, and
  `CHANGELOG.md` are updated in the same change, and the checks above pass.
