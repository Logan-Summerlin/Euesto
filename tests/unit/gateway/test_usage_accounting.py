"""Usage normalization per provider shape, cache-miss classification, and hit-rate arithmetic."""

from __future__ import annotations

import pytest

from server.agent.budgets import RunBudget, usage_ratios
from server.agent.usage import TurnRecord, UsageLedger, classify_cache_miss
from server.openrouter.client import normalize_usage


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (  # OpenRouter / OpenAI shape
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100},
                "completion_tokens_details": {"reasoning_tokens": 20},
                "cost": 0.001,
            },
            {
                "prompt_tokens": 1000,
                "cached_tokens": 800,
                "cache_write_tokens": 100,
                "reasoning_tokens": 20,
                "total_tokens": 1050,
                "cost": 0.001,
            },
        ),
        (  # Anthropic shape: input_tokens excludes cache reads and writes
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 200,
            },
            {
                "prompt_tokens": 1000,
                "cached_tokens": 700,
                "cache_write_tokens": 200,
                "completion_tokens": 10,
            },
        ),
        (  # DeepSeek shape
            {"prompt_tokens": 500, "completion_tokens": 5, "prompt_cache_hit_tokens": 450},
            {"prompt_tokens": 500, "cached_tokens": 450},
        ),
        (  # Gemini shape leaking through
            {"prompt_tokens": 300, "cached_content_token_count": 256},
            {"prompt_tokens": 300, "cached_tokens": 256},
        ),
    ],
)
def test_provider_usage_shapes_normalize(usage, expected) -> None:
    normalized = normalize_usage(usage)
    for key, value in expected.items():
        assert normalized[key] == value, key


def test_missing_fields_stay_absent() -> None:
    normalized = normalize_usage({"prompt_tokens": 10})
    assert "cached_tokens" not in normalized and "cache_write_tokens" not in normalized
    assert normalize_usage("nonsense") == {}


def _previous(**overrides) -> TurnRecord:
    values = {
        "prompt_tokens": 10_000,
        "provider": "A",
        "model_and_mode": "m|agent",
        "finished_at": 100.0,
    }
    values.update(overrides)
    return TurnRecord(**values)


@pytest.mark.parametrize(
    ("previous", "usage", "kwargs", "cause"),
    [
        (None, {"cached_tokens": 0}, {}, "first_turn"),
        (_previous(), {"cached_tokens": 9_500}, {}, None),
        (_previous(), {"cached_tokens": 100}, {"expected": "compaction"}, "compaction"),
        (
            _previous(),
            {"cached_tokens": 100},
            {"model_and_mode": "m|plan"},
            "model_or_mode_switch",
        ),
        (_previous(), {"cached_tokens": 100}, {"provider": "B"}, "provider_switch"),
        (_previous(), {"cached_tokens": 100}, {"now": 500.0}, "idle"),
        (_previous(), {"cached_tokens": 100}, {}, "unexpected"),
        (_previous(), {}, {}, None),  # caching not reported
    ],
)
def test_miss_classification(previous, usage, kwargs, cause) -> None:
    arguments = {"provider": "A", "model_and_mode": "m|agent", "now": 200.0}
    arguments.update(kwargs)
    assert classify_cache_miss(previous, usage, **arguments) == cause


def test_hit_rate_and_new_tokens_per_turn() -> None:
    assert usage_ratios(0, 0, 0) == {"cache_hit_rate": 0.0, "average_new_tokens_per_turn": 0.0}
    assert usage_ratios(10_000, 9_000, 4) == {
        "cache_hit_rate": 0.9,
        "average_new_tokens_per_turn": 250.0,
    }
    budget = RunBudget(10, 100, 1.0, 10)
    budget.add_usage({"prompt_tokens": 1_000, "cached_tokens": 500, "cache_write_tokens": 50})
    budget.add_usage({"prompt_tokens": 1_000, "cached_tokens": 900})
    usage = budget.usage()
    assert usage["turns"] == 2 and usage["cache_write_tokens"] == 50
    assert usage["cache_hit_rate"] == pytest.approx(0.7)
    ledger = UsageLedger()
    ledger.add({"prompt_tokens": 100, "cached_tokens": 50, "cost": 0.5})
    assert ledger.summary()["cache_hit_rate"] == 0.5 and ledger.summary()["cost"] == 0.5
