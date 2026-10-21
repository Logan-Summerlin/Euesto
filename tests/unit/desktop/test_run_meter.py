"""The live run meter (plan 0.8 and 1.1): folded from the gateway's run events."""

from src.run_meter import RunMeter


def test_meter_folds_context_usage_budget_and_compaction_events() -> None:
    meter = RunMeter()
    assert not meter.update("tool.requested", {"tool": "read"})
    assert meter.update(
        "context.inspected",
        {"estimated_tokens": 12_000, "high_water_tokens": 96_000, "budget": {"tool_calls": 2}},
    )
    assert meter.update(
        "usage.turn",
        {
            "prompt_tokens": 48_000,
            "run": {"cost": 0.042, "prompt_tokens": 100_000, "cached_tokens": 91_000},
        },
    )
    assert meter.update(
        "usage.updated", {"budget": {"tool_calls": 12, "elapsed_seconds": 185.4}}
    )
    assert meter.update("model.retry", {"attempt": 1, "max_attempts": 3})

    assert meter.cache_hit_rate == 0.91
    assert meter.text() == (
        "$0.0420 · cache 91% · context 48.0k / 96.0k · 12 tool calls · 3m 05s · 1 retry"
    )
    view = meter.to_dict()
    assert view["contextFraction"] == 0.5 and view["toolCalls"] == 12
    assert view["cacheHitRate"] == 0.91

    assert meter.update("context.compacted", {"tokens_after": 20_000})
    assert meter.context_tokens == 20_000 and meter.working_set_tokens == 96_000
    assert meter.text().endswith("compacted ×1")


def test_meter_reports_an_unknown_cache_rate_before_the_first_turn() -> None:
    meter = RunMeter()
    assert meter.cache_hit_rate is None
    assert meter.text() == "$0.0000 · cache – · 0 tool calls · 0m 00s"
    assert meter.to_dict()["cacheHitRate"] == -1.0 and meter.to_dict()["contextFraction"] == 0
    # Malformed payloads leave the meter as it was.
    meter.update("usage.updated", {"budget": "bad", "cost": "x"})
    assert meter.cost == 0.0 and meter.tool_calls == 0
