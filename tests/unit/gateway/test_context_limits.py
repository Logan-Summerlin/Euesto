"""The compaction high-water derivation: one table, and the invariants it must preserve.

``_high_water`` is a pure function of the request, the profile, and three module constants, so
these tests read as the arithmetic: the shipped windows in one table, then the properties that
must hold at every window size and profile.
"""

from __future__ import annotations

import pytest

from server.agent import runtime as runtime_module
from server.agent.budgets import BUDGET_PROFILES, BudgetProfile
from server.agent.context import estimate_message_tokens
from server.agent.prompts import agent_prompt
from shared.requests import AgentRunRequest

CODING = BUDGET_PROFILES["coding"]
# The desktop sends 80% of the model's published window as the prompt budget
# (``src/controllers.py``); these are the windows ``src/models.py`` ships plus the 8,000-token
# case the scripted compaction scenarios use.
WINDOWS = (8_000, 128_000, 163_840, 200_000, 250_000, 400_000, 1_048_576)
SHIPPED_WINDOWS = (128_000, 163_840, 200_000, 400_000, 1_048_576)
# window, prompt budget (80%), high-water mark, the bound that produces it
HIGH_WATER_TABLE = (
    (8_000, 6_400, 4_000, "floor"),
    (128_000, 102_400, 54_400, "capacity"),
    (163_840, 131_072, 83_072, "capacity"),
    (200_000, 160_000, 112_000, "capacity"),
    (250_000, 200_000, 150_000, "profile"),
    (400_000, 320_000, 150_000, "profile"),
    (1_048_576, 838_860, 150_000, "profile"),
)


def request_for(window: int) -> AgentRunRequest:
    return AgentRunRequest(
        model="openai/test-model",
        messages=({"role": "user", "content": "do the task"},),
        mode="agent",
        workspace_id="workspace",
        approval_policy="auto",
        session_id="session-1",
        context_limit_tokens=int(window * 0.8),
    )


def bounds(request: AgentRunRequest, profile: BudgetProfile) -> list[int]:
    limit = request.context_limit_tokens
    return [
        profile.working_set_tokens,
        int(limit * runtime_module.HIGH_WATER_WINDOW_SHARE),
        limit - profile.max_output_tokens - runtime_module.TURN_GROWTH_RESERVE_TOKENS,
    ]


def test_the_high_water_mark_of_every_window():
    for window, limit, expected, binding in HIGH_WATER_TABLE:
        request = request_for(window)
        assert request.context_limit_tokens == limit
        values = bounds(request, CODING)
        assert runtime_module._high_water(request, CODING) == expected, (window, values)
        named = {"profile": values[0], "share": values[1], "capacity": values[2]}.get(binding)
        if named is not None:
            assert named == expected, (window, binding, values)


def test_the_growth_reserve_is_what_keeps_a_turn_and_its_results_inside_the_window(
    monkeypatch,
) -> None:
    request = request_for(200_000)
    limit = request.context_limit_tokens
    high = runtime_module._high_water(request, CODING)
    assert high == 112_000
    # One full answer plus a turn's worth of large results still fit above the mark.
    assert limit - high >= CODING.max_output_tokens + runtime_module.TURN_GROWTH_RESERVE_TOKENS
    # Drop the reserve and the 75% share binds instead, leaving the mark 8,000 tokens higher.
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    assert runtime_module._high_water(request, CODING) == 120_000
    assert limit - 120_000 < CODING.max_output_tokens + 32_000


def test_the_mark_never_reaches_the_safety_clamp_or_the_output_cap() -> None:
    reserve = runtime_module.TURN_GROWTH_RESERVE_TOKENS
    for window in SHIPPED_WINDOWS:
        request = request_for(window)
        limit = request.context_limit_tokens
        for profile in BUDGET_PROFILES.values():
            high = runtime_module._high_water(request, profile)
            assert high < limit * runtime_module.HARD_CEILING_WINDOW_SHARE, (
                window,
                profile.name,
                high,
            )
            # One output turn always fits above the mark, and so does the growth reserve.
            assert high + profile.max_output_tokens + reserve <= limit, (
                window,
                profile.name,
                high,
            )
            assert high >= runtime_module.MIN_HIGH_WATER_TOKENS


def test_the_derived_low_water_mark_still_holds_the_prefix_and_one_exchange() -> None:
    prefix = [
        {"role": "system", "content": agent_prompt("agent")},
        {"role": "system", "content": "SESSION CONTEXT\nworkspace: workspace"},
        {"role": "user", "content": "do the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 32_000},
    ]
    needed = estimate_message_tokens(prefix)
    for window in WINDOWS:
        for profile in BUDGET_PROFILES.values():
            high = runtime_module._high_water(request_for(window), profile)
            low = max(1_000, int(high * runtime_module.LOW_WATER_SHARE))
            # A tiny window cannot hold the prefix at all; the floor then keeps compaction
            # meaningful rather than promising room it does not have.
            if high == runtime_module.MIN_HIGH_WATER_TOKENS:
                continue
            assert low >= needed, (window, profile.name, low, needed)


def test_the_coding_tiers_keep_distinct_working_sets() -> None:
    window = 1_048_576
    request = request_for(window)
    marks = {
        name: runtime_module._high_water(request, profile)
        for name, profile in BUDGET_PROFILES.items()
    }
    assert marks == {"coding": 150_000, "extended-coding": 240_000, "large-coding": 400_000}
    # No result cap grew: each is still its byte cap, below 40% of its working set in tokens.
    for profile in BUDGET_PROFILES.values():
        for tool in ("read", "grep"):
            assert profile.result_cap(tool) == min(
                profile.read_result_bytes if tool == "read" else profile.result_bytes,
                profile.working_set_tokens * 4 // 10,
            )
    assert CODING.result_cap("read") == 32_000


def test_the_default_prompt_budget_is_the_200k_class_window() -> None:
    request = AgentRunRequest(
        model="openai/test-model",
        messages=({"role": "user", "content": "go"},),
        mode="agent",
        workspace_id="workspace",
    )
    assert request.context_limit_tokens == 160_000
    restored = AgentRunRequest.from_dict(request.to_dict())
    assert restored.context_limit_tokens == 160_000
    # A stored snapshot from before the default moved keeps working and lands on the new one.
    legacy = {key: value for key, value in request.to_dict().items()}
    legacy.pop("context_limit_tokens")
    assert AgentRunRequest.from_dict(legacy).context_limit_tokens == 160_000


# -- user compaction threshold (MODEL_CONTROLS_PLAN 1.5) -----------------------------------


def request_with_threshold(window: int, threshold: int | None) -> AgentRunRequest:
    return AgentRunRequest(
        model="openai/test-model",
        messages=({"role": "user", "content": "do the task"},),
        mode="agent",
        workspace_id="workspace",
        approval_policy="auto",
        session_id="session-1",
        context_limit_tokens=int(window * 0.8),
        compaction_threshold_tokens=threshold,
    )


def test_a_user_threshold_above_the_profile_cap_binds_on_a_large_window() -> None:
    # 240,000 is above the coding cap of 150,000, so the setting is what moves the mark.
    request = request_with_threshold(1_048_576, 240_000)
    assert runtime_module._high_water(request, CODING) == 240_000


def test_a_user_threshold_is_clamped_by_the_share_of_the_budget() -> None:
    request = request_with_threshold(1_048_576, 700_000)
    limit = request.context_limit_tokens  # 838,860
    high = runtime_module._high_water(request, CODING)
    share = int(limit * runtime_module.HIGH_WATER_WINDOW_SHARE)
    capacity = limit - CODING.max_output_tokens - runtime_module.TURN_GROWTH_RESERVE_TOKENS
    assert high == min(700_000, share, capacity) == share == 629_145
    assert high + CODING.max_output_tokens + runtime_module.TURN_GROWTH_RESERVE_TOKENS <= limit


def test_a_user_threshold_on_a_small_window_stays_capacity_bound() -> None:
    request = request_with_threshold(128_000, 200_000)
    assert request.context_limit_tokens == 102_400
    assert runtime_module._high_water(request, CODING) == 54_400


def test_the_mark_never_reaches_the_safety_clamp_with_a_threshold_set() -> None:
    reserve = runtime_module.TURN_GROWTH_RESERVE_TOKENS
    for window in SHIPPED_WINDOWS:
        for threshold in (4_000, 60_000, 300_000, 2_000_000):
            request = request_with_threshold(window, threshold)
            limit = request.context_limit_tokens
            for profile in BUDGET_PROFILES.values():
                high = runtime_module._high_water(request, profile)
                assert high < limit * runtime_module.HARD_CEILING_WINDOW_SHARE, (
                    window,
                    threshold,
                    profile.name,
                )
                assert high + profile.max_output_tokens + reserve <= limit or (
                    high == runtime_module.MIN_HIGH_WATER_TOKENS
                ), (window, threshold, profile.name, high)
                assert high >= runtime_module.MIN_HIGH_WATER_TOKENS


def test_the_threshold_round_trips_and_defaults_to_the_profile_cap() -> None:
    request = request_with_threshold(1_048_576, 240_000)
    data = request.to_dict()
    assert data["compaction_threshold_tokens"] == 240_000
    assert AgentRunRequest.from_dict(data).compaction_threshold_tokens == 240_000
    # A snapshot without the field defaults to None, which is the profile cap.
    legacy = dict(data)
    legacy.pop("compaction_threshold_tokens")
    restored = AgentRunRequest.from_dict(legacy)
    assert restored.compaction_threshold_tokens is None
    assert runtime_module._high_water(restored, CODING) == 150_000


@pytest.mark.parametrize("bad", [3_999, 2_000_001, True, "240000"])
def test_out_of_range_thresholds_are_rejected(bad) -> None:
    with pytest.raises(ValueError, match="Compaction threshold"):
        request_with_threshold(1_048_576, bad)


# -- the effective mark and the fixed-context floor (CONTEXT_COMPACTION_LIMITS_PLAN 3.2) -------


def prefix_messages(tokens: int) -> list[dict[str, str]]:
    """A leading system message that estimates to exactly ``tokens``, then a user turn."""
    message = {"role": "system", "content": "x" * max(0, 4 * tokens - 33)}
    if tokens:
        assert estimate_message_tokens([message]) == tokens
    return [message, {"role": "user", "content": "go"}]


# window, share, prefix tokens, learned floor, effective mark, binding.
EFFECTIVE_TABLE = (
    (1_048_576, 5, 0, 0, 52_428, "requested"),
    (1_048_576, 10, 0, 0, 104_857, "requested"),
    (400_000, 5, 0, 0, 20_000, "requested"),
    # A 5% request on a 200k window is 10,000 tokens, below the fixed-context floor, so the
    # gateway raises it to 13,000 prefix + 2,000 exchange + 2,000 hysteresis and says so.
    (200_000, 5, 13_000, 0, 17_000, "prefix"),
    (200_000, 10, 13_000, 0, 20_000, "requested"),
    # Both 5% and 10% of a 128k window are below the floor.
    (128_000, 5, 13_000, 0, 17_000, "prefix"),
    (128_000, 10, 13_000, 0, 17_000, "prefix"),
    # Automatic on a 128k window is capacity-bound, not request-bound.
    (128_000, None, 0, 0, 54_400, "capacity"),
    # A floor learned after a compaction that could not reach the low-water mark binds.
    (200_000, 5, 13_000, 25_000, 25_000, "learned"),
)


def test_the_effective_mark_of_every_window():
    for window, share, prefix, learned, expected, binding in EFFECTIVE_TABLE:
        threshold = None if share is None else max(4_000, int(window * share / 100))
        mark = runtime_module.effective_high_water(
            request_with_threshold(window, threshold),
            CODING,
            prefix_messages(prefix),
            learned=learned,
        )
        assert mark.effective == expected, (window, share, prefix, learned, mark)
        assert mark.binding == binding, (window, share, prefix, learned, mark)
        assert mark.requested == runtime_module._high_water(
            request_with_threshold(window, threshold), CODING
        )
        if prefix:
            # The exchange reserve plus the hysteresis (2,000 + 2,000).
            assert mark.floor == prefix + 4_000
        assert mark.effective <= int(window * 0.8 * runtime_module.HARD_CEILING_WINDOW_SHARE)


def test_the_effective_mark_still_leaves_room_for_one_turn_and_the_growth_reserve() -> None:
    for window, share, prefix, learned, _expected, _binding in EFFECTIVE_TABLE:
        threshold = None if share is None else max(4_000, int(window * share / 100))
        request = request_with_threshold(window, threshold)
        limit = request.context_limit_tokens
        for profile in BUDGET_PROFILES.values():
            mark = runtime_module.effective_high_water(
                request, profile, prefix_messages(prefix), learned=learned
            )
            # The effective mark plus one output turn and the growth reserve stays inside the
            # prompt budget, and the 90% safety clamp still holds above it.
            assert (
                mark.effective
                + profile.max_output_tokens
                + runtime_module.TURN_GROWTH_RESERVE_TOKENS
                <= limit
            ), (window, share, profile.name, mark)
            assert mark.effective <= int(limit * runtime_module.HARD_CEILING_WINDOW_SHARE)


def test_the_trigger_sits_one_summary_request_below_the_effective_mark() -> None:
    for effective in (4_000, 4_001, 17_000, 52_428, 150_000):
        trigger = runtime_module._trigger_tokens(effective)
        assert trigger == max(
            4_000, effective - runtime_module.COMPACTION_REQUEST_RESERVE_TOKENS
        )
        assert trigger <= effective
    # The protocol floor wins below 5,000 tokens; a summary call is never triggered by the
    # reserve alone.
    assert runtime_module._trigger_tokens(4_000) == 4_000


@pytest.mark.parametrize(
    "window,prefix",
    [
        # 90% of the 838,860-token budget is 754,974: a prefix that large can never be prompted.
        (1_048_576, 760_000),
        # The capacity bound binds first on a 200k window: 160,000 − 16,000 − 32,000 = 112,000.
        (200_000, 120_000),
    ],
)
def test_a_floor_that_cannot_fit_the_budget_fails_closed(window, prefix) -> None:
    request = request_with_threshold(window, max(4_000, int(window * 5 / 100)))
    with pytest.raises(runtime_module.ContextFloorTooLarge) as raised:
        runtime_module.effective_high_water(request, CODING, prefix_messages(prefix))
    message = str(raised.value)
    assert f"prefix {prefix:,} tokens" in message
    assert "requested mark" in message and "prompt budget" in message
