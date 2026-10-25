from src.model_catalog import (
    AGENT_MIN_CONTEXT,
    agent_capable,
    filter_model_entries,
    model_warnings,
    price_text,
)
from src.models import DEFAULT_MODELS, ModelOption

ENTRY = {
    "id": "vendor/model",
    "label": "Model",
    "description": "",
    "price": 0.4,
    "rank": 8,
    "year": 2025,
    "textCompatible": True,
}
SAVED = {
    "id": "saved/model",
    "label": "saved/model",
    "description": "",
    "price": None,
    "rank": None,
    "year": None,
    "textCompatible": True,
}


def test_price_rank_and_release_year_filters_compose() -> None:
    assert filter_model_entries([ENTRY], "", True, 0.5, 10, 2025) == [ENTRY]
    assert filter_model_entries([ENTRY], "", True, 0.1, 0, 0) == []
    assert filter_model_entries([ENTRY], "", True, -1, 5, 0) == []
    assert filter_model_entries([ENTRY], "", True, -1, 0, 2024) == []


def test_disabled_filters_keep_entries_without_catalog_metadata() -> None:
    assert filter_model_entries([ENTRY, SAVED], " SAVED ", True, -1, 0, 0) == [SAVED]
    assert filter_model_entries(
        [ENTRY, {**SAVED, "textCompatible": False}], "", True, -1, 0, 0
    ) == [ENTRY]
    assert filter_model_entries([SAVED], "", True, 1.0, 0, 0) == []


def test_plan_and_agent_list_only_tool_capable_models() -> None:
    tools = {**ENTRY, "id": "tools/model", "tools": True}
    assert filter_model_entries([ENTRY, tools], "", True, -1, 0, 0) == [ENTRY, tools]
    assert filter_model_entries([ENTRY, tools], "", True, -1, 0, 0, agent_only=True) == [tools]
    assert agent_capable(frozenset({"tools", "reasoning"}))
    assert not agent_capable(frozenset({"reasoning"}))


def test_picker_warns_on_small_windows_and_missing_cached_prices() -> None:
    cached = ModelOption(
        "a/cached",
        "Cached",
        128_000,
        prompt_price=0.0000003,
        completion_price=0.0000025,
        cached_prompt_price=0.00000003,
    )
    assert model_warnings(cached) == []
    assert price_text(cached) == "in $0.3 · cached $0.03 · out $2.5 /M"

    uncached = ModelOption("a/uncached", "Uncached", 32_000, prompt_price=0.000001)
    warnings = model_warnings(uncached)
    assert warnings[0] == "32k context: agent runs compact often"
    assert warnings[1].startswith("No cached-input price")
    assert price_text(uncached) == "in $1 · cached – · out – /M"

    # Unknown prices (offline defaults) are not flagged as uncached.
    offline = ModelOption("a/offline", "Offline", 200_000)
    assert (
        model_warnings(offline) == [] and price_text(offline) == "Prices load with the catalog"
    )


def test_offline_defaults_are_tool_capable_with_large_windows() -> None:
    assert DEFAULT_MODELS
    for model in DEFAULT_MODELS:
        assert agent_capable(model.supported_parameters), model.id
        assert model.context_length >= AGENT_MIN_CONTEXT, model.id
    assert "openrouter/auto" not in {model.id for model in DEFAULT_MODELS}
