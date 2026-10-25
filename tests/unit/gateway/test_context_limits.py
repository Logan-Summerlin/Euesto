"""The compaction high-water derivation: one table, and the invariants it must preserve.

``_high_water`` is a pure function of the request, the profile, and three module constants, so
these tests read as the arithmetic: the shipped windows in one table, then the properties that
must hold at every window size and profile.
"""

from __future__ import annotations

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
