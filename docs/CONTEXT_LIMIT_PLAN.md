# The 96,000-token working set: what it is today, and a plan to raise it to 150,000

Status: proposed (investigation complete, no code changed yet).

Scope: the agent loop's compaction high-water mark for the `coding` budget profile. It changes
one profile value, one share constant, the derivation that ties them to the model window, and
the tests and documents that pin the current numbers. Compaction behaviour, the handoff, and
the result caps stay as they are.

## 1. What the 96k number is

`BudgetProfile.working_set_tokens` (`server/agent/budgets.py:23`) is the compaction cap of a
profile:

```text
coding          working_set_tokens = 96_000   (default of the dataclass)
extended-coding working_set_tokens = 160_000
large-coding    working_set_tokens = 256_000
```

It is not a hard context limit. The high-water mark that actually triggers a compaction is
the *smaller* of the profile cap and a share of the model window (`_high_water`,
`server/agent/runtime.py:1650`):

```python
min(int(request.context_limit_tokens * 0.6), profile.working_set_tokens)   # floor 4,000
```

and `context_limit_tokens` is itself set by the desktop to 80% of the model's published
window (`src/controllers.py:144`). So the chain in force today is

```text
high water = min(0.6 × 0.8 × window, 96_000) = min(0.48 × window, 96_000)
```

After a compaction the context returns to a third of the high-water mark
(`LOW_WATER_SHARE = 1/3`, `runtime.py:77`), the handoff is capped at 2,500 tokens
(`SUMMARY_MAX_TOKENS`, `runtime.py:79`), and a safety clamp compacts again if the result is
still above 90% of `context_limit_tokens` (`HARD_CEILING_WINDOW_SHARE`, `runtime.py:78`). Each compaction is a
tool-free model turn at low reasoning effort, journaled as `context.compacted` with token
counts before and after.

## 2. What that yields on real models

`src/models.py:162` ships five defaults. The effective high-water mark for each:

| Window | `context_limit` (80%) | 48% of window | Today's high water | Which bound binds |
|---:|---:|---:|---:|---|
| 128,000 | 102,400 | 61,440 | 61,440 | window share |
| 163,840 (DeepSeek V3.1) | 131,072 | 78,643 | 78,643 | window share |
| 200,000 (Claude Haiku 4.5) | 160,000 | 96,000 | 96,000 | both, exactly |
| 400,000 (GPT-5 mini) | 320,000 | 153,600 | 96,000 | profile cap |
| 1,048,576 (Gemini 2.5 Flash, MiMo-V2.5) | 838,860 | 402,652 | 96,000 | profile cap |

Two observations drive the plan:

- For three of the five defaults the *profile cap* is the operative limit, so the 96k number
  is a policy choice, not a window constraint. Raising it is effective there immediately.
- On a 200,000-token window the two bounds coincide at exactly 96,000, which is why the
  number looks like a window limit in journal output when it is not.

The meter the user sees is fed from the `context.inspected` event (`runtime.py:325`:
`limit_tokens`, `high_water_tokens`) into `src/run_meter.py`, so it currently displays
`context / 96,000` for those runs and would follow a changed constant with no other work.

## 3. What the cap buys and what it costs

`docs/AGENTIC_HARNESS_PLAN.md` §1.3 sizes the current value: a 10,000-token static prefix and
session context, compaction at 96,000, return to 32,000, and about 1,500 new tokens per turn
(the previous reply plus the previous result).

- **Cycle length.** (96,000 − 32,000) / 1,500 ≈ 43 turns per compaction cycle, with an average
  in-context prompt of about 64,000 tokens → about 2.7 M prompt tokens per cycle.
- **At 150,000.** (150,000 − 50,000) / 1,500 ≈ 67 turns per cycle, average about 100,000
  tokens → about 6.7 M prompt tokens per cycle.

So per *turn* the prompt grows by about 1.56×, while compaction events per turn fall to about
0.64× (one every 67 turns instead of every 43). What that costs in dollars depends on the
catalog price of the selected model, which ships empty (`ModelOption.prompt_price` is `None`
until the catalog is fetched), so the plan states the formula rather than a number:

```text
per-turn cost ≈ prompt_tokens × (hit_rate × cached_price + (1 − hit_rate) × prompt_price)
```

With the 95% cache-hit target of harness plan §1.2 and illustrative flash-tier rates
($0.30/M prompt, $0.075/M cached), the effective rate is about $0.086/M, so a turn at 64k
costs about $0.0055 and a turn at 100k about $0.0086 — about +$0.31 across a 100-turn run,
offset by roughly half as many compaction turns (each of which pays a full-price prompt plus
up to 2,500 output tokens). The `coding` profile's `max_cost` of $2.00 is not threatened by
the change alone, but a run that hits it will hit it earlier; the wind-down at 85%
(`RunBudget.wind_down_reason`) already covers that.

The counterweight is the risk the current value was chosen against
(`docs/AGENTIC_HARNESS_PLAN.md:157`, E2): long contexts cost more per turn and degrade
flash-tier accuracy. 150k is deliberately not "as large as the window allows"; it is one
moderate step, with a measurement gate in §6.

## 4. The constraint that makes a naive change ineffective

Changing only `working_set_tokens` to 150,000 gives 150k on 400k and 1M windows, but leaves a
200,000-token window at 96,000, because `0.48 × window` still binds:
`min(0.48 × 200,000, 150,000) = 96,000`. To reach 150,000 on a 200k-window model the
window-derived bound must rise to 0.75 of the window, which with the desktop's 0.8 share means
`HIGH_WATER_WINDOW_SHARE = 0.9375` — leaving only 10,000 tokens between the high-water mark
and the safety clamp (`0.9 × 160,000 = 144,000`), which is *below* the new high water. That
configuration is self-defeating: every turn over the high-water mark would trip the
deterministic clamp instead of a planned compaction.

The plan therefore raises the share to a defensible value and lets the window share bind on
small-window models, instead of forcing 150k into every window.

## 5. Plan

### Step 1 — Set the target working set

- `server/agent/budgets.py`: `BudgetProfile.working_set_tokens` 96_000 → 150_000 for
  `STANDARD_CODING_PROFILE`. The dataclass default stays as-is so the value is explicit at
  every profile.
- Keep the 10%-of-working-set result ceiling (`result_cap`): 150,000 × 4 // 10 = 60,000 bytes,
  still above the 32,000-byte `read` cap and the 24,000-byte default, so no result changes
  size. [LIMITS.md](LIMITS.md) §"Model-facing result bounds" records the new ceiling.

### Step 2 — Raise the window share and make the derivation explicit

- `server/agent/runtime.py`: `HIGH_WATER_WINDOW_SHARE` 0.6 → 0.75.
- Rewrite `_high_water` so the three bounds are visible in one place and a small window can
  never be pushed past what it can actually hold:

  ```python
  high = min(
      profile.working_set_tokens,
      int(request.context_limit_tokens * HIGH_WATER_WINDOW_SHARE),
      request.context_limit_tokens - profile.max_output_tokens - TURN_GROWTH_RESERVE_TOKENS,
  )
  return max(4_000, high)
  ```

  with `TURN_GROWTH_RESERVE_TOKENS = 32_000` (the `read` cap in tokens) so the output cap and
  one large result still fit under the request budget.
- `HARD_CEILING_WINDOW_SHARE` stays 0.9 and now sits above the high water at every window
  size, which a test asserts (Step 4).
- `shared/requests.py`: the `context_limit_tokens` default of 100,000 is a prompt budget for a
  128k-class window; raise it to 160,000 so a caller that does not set it explicitly gets the
  200k-class behaviour the desktop already sends.

Resulting values:

| Window | Today | Proposed | Bound |
|---:|---:|---:|---|
| 128,000 | 61,440 | 54,400 | window capacity (deliberate tightening) |
| 163,840 | 78,643 | 83,072 | window capacity |
| 200,000 | 96,000 | 120,000 | 75% share |
| 250,000 | 96,000 | 150,000 | profile cap |
| 400,000 | 96,000 | 150,000 | profile cap |
| 1,048,576 | 96,000 | 150,000 | profile cap |

The 128k row moves down, not up: at 48% of the window a single 32,000-byte read plus the
16,000-token output cap can already exceed the window, and the new derivation makes that
impossible rather than merely unlikely.

### Step 3 — Keep the tiers distinct

`extended-coding` (160,000) and `large-coding` (256,000) are within 10k and 106k of the new
`coding` cap, so the tiers would nearly stop differing. Either:

- (a) raise them to 240,000 and 400,000, keeping their result caps and cost budgets, which
  makes them reachable only on ≥384k and ≥597k windows under Step 2's derivation; or
- (b) leave them and accept that `extended-coding` differs from `coding` only in budget.

Recommended: (a), in the same change, so a profile name still describes a context size. This
is the one item here that needs a product decision (§7).

### Step 4 — Tests

- `tests/unit/gateway/test_harness_loop.py:531` builds its scenario on an 8,000-token window
  whose high water is 4,800 (60% share, below the profile cap). The new derivation subtracts
  the output cap and growth reserve, so that case now floors at 4,000. Make
  `TURN_GROWTH_RESERVE_TOKENS` a module constant the test can monkeypatch to 0, and add an
  explicit test for each row of the table in Step 2 (`_high_water` is a pure function).
- New invariants: high water < `context_limit × HARD_CEILING_WINDOW_SHARE` at every window
  size and profile; high water < `context_limit − max_output_tokens`; the derived low water
  is at least the static prefix plus one exchange.
- A scripted long-run test (`tests/unit/gateway/test_long_runs.py`) that compacts at the new
  mark and journals `context.compacted` with `high_water_tokens == 150_000` for a 400k-window
  request.
- `tests/unit/desktop/test_run_meter.py` passes `high_water_tokens` in a payload, so it needs
  no change; the meter already renders whatever the journal reports.

### Step 5 — Documentation

- [LIMITS.md](LIMITS.md): the working-set row (96,000 in `coding`) and the result-ceiling row
  (38,400 bytes → 60,000 bytes).
- [ARCHITECTURE.md](ARCHITECTURE.md) §"Compaction" and §"Result bounds": the 96,000 example,
  and the stale claim that a result is "never more than 10% of the working set" (the code's
  40%-of-tokens figure equals 10% of the working set in bytes; state it once, correctly).
- [AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md) §1.3: the status line ("working sets
  96,000 / 160,000 / 256,000") and the worked example ("compaction runs at 96,000 tokens and
  returns to 32,000").
- `CHANGELOG.md` entry when the change lands.

## 6. Verification

1. `python scripts/validate.py fast` and `python scripts/validate.py ruff`.
2. Journal check on a long scripted or real run: `context.inspected.high_water_tokens` is
   150,000 for a large-window model, `context.compacted` fires once per cycle rather than
   repeatedly, and `method` is `summary` (not `deterministic`) in the normal case.
3. Cost gate: compare `usage.turn` prompt tokens, cache hit rate, and cost against a run of
   the same task on the current constants. The change is acceptable if per-turn prompt cost
   stays under ~1.6× and compaction turns fall by roughly a third; if cache hit rate drops
   (a longer live context is more sensitive to any prefix edit) or flash-tier answer quality
   regresses, revert Step 2's share first, since it is the single line that moves the mark on
   mid-size windows.
4. Meter check: the desktop shows `context / 150k`, and the run does not clamp
   (`context.compacted.method == "deterministic"` should stay rare).

## 7. Decisions needed

- **Step 3 tiering:** raise `extended-coding`/`large-coding` with `coding` (recommended) or
  leave them.
- **Small-window behaviour:** accept the 128k row moving from 61,440 down to 54,400 as part of
  making the derivation explicit. The alternative is to keep the old formula for windows under
  250k, which keeps today's overflow risk on the smallest windows.

## 8. Out of scope

- The desktop's conversation-history compaction (`src/controllers.py:144`), which shares the
  0.8 window share but is about chat history, not the agent loop.
- The investigation loop's own 64,000-token nested context.
- Provider-specific window quirks and per-model overrides; the change stays provider-agnostic.
