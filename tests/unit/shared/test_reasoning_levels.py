"""Thinking levels: vocabulary, wire map, and the one-step fallback (model controls 3.2)."""

from __future__ import annotations

import pytest

from shared.requests import (
    REASONING_EFFORT_FALLBACKS,
    REASONING_EFFORT_PAYLOADS,
    REASONING_EFFORTS,
    AgentRunRequest,
    reasoning_fallback,
    reasoning_payload_effort,
)

LEVELS = (None, "low", "medium", "high", "extra_high", "max")


def _request(**overrides) -> AgentRunRequest:
    values = {
        "model": "vendor/model",
        "messages": ({"role": "user", "content": "hi"},),
        "mode": "agent",
        "workspace_id": "workspace",
        "reasoning_effort": None,
    }
    values.update(overrides)
    return AgentRunRequest(**values)


def test_the_vocabulary_is_five_levels_plus_unset() -> None:
    assert REASONING_EFFORTS == LEVELS


def test_minimal_is_no_longer_a_level() -> None:
    with pytest.raises(ValueError, match="Unknown reasoning effort"):
        _request(reasoning_effort="minimal")
    with pytest.raises(ValueError):
        reasoning_payload_effort("minimal")


def test_every_level_maps_to_a_wire_value() -> None:
    assert set(REASONING_EFFORT_PAYLOADS) == set(LEVELS) - {None}
    assert reasoning_payload_effort(None) is None
    assert {level: reasoning_payload_effort(level) for level in LEVELS[1:]} == {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "extra_high": "xhigh",
        "max": "max",
    }


def test_fallback_chains_start_at_the_request_and_descend_to_high() -> None:
    order = LEVELS[1:]
    for effort, chain in REASONING_EFFORT_FALLBACKS.items():
        assert chain[0] == effort
        assert set(chain) <= set(REASONING_EFFORT_PAYLOADS)
        positions = [order.index(item) for item in chain]
        assert positions == sorted(positions, reverse=True)
        assert len(set(positions)) == len(positions)
        assert chain[-1] == "high"


def test_a_rejected_level_steps_down_exactly_one_level() -> None:
    assert reasoning_fallback("max") == "extra_high"
    assert reasoning_fallback("extra_high") == "high"
    for level in (None, "low", "medium", "high"):
        assert reasoning_fallback(level) is None
