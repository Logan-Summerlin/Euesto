from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .models import DEFAULT_MODELS, ModelOption
from .storage import Storage

DEFAULT_CACHE_TTL = timedelta(hours=24)
# Below this window an agent run compacts often and loses detail (plan 0.9).
AGENT_MIN_CONTEXT = 64_000
AGENT_MODES = frozenset({"plan", "agent"})


class ModelCatalog:
    def __init__(self, storage: Storage):
        self.storage = storage

    def models(self) -> list[ModelOption]:
        cached = self.storage.list_catalog_models()
        return cached or list(DEFAULT_MODELS)

    def is_stale(self, ttl: timedelta = DEFAULT_CACHE_TTL) -> bool:
        fetched_at = self.storage.catalog_fetched_at()
        if not fetched_at:
            return True
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        return age > ttl

    def cache(self, models: list[ModelOption], fetched_at: str) -> None:
        self.storage.replace_model_catalog(models, fetched_at)


def agent_capable(supported_parameters: frozenset[str] | set[str]) -> bool:
    """Plan and Agent need native tool calling; models without it are hidden there."""
    return "tools" in supported_parameters


def model_warnings(model: ModelOption) -> list[str]:
    """Why a model may be a poor or costly agent: a small window, or no cached-input price."""
    warnings = []
    if model.context_length < AGENT_MIN_CONTEXT:
        warnings.append(f"{model.context_length // 1000}k context: agent runs compact often")
    if model.prompt_price is not None and model.cached_prompt_price is None:
        warnings.append(
            "No cached-input price: every turn pays full price for the whole prompt"
        )
    return warnings


def price_text(model: ModelOption) -> str:
    """Input, cached-input, and output prices per million tokens, as the picker shows them."""
    if model.prompt_price is None and model.completion_price is None:
        return "Prices load with the catalog"

    def per_million(value: float | None) -> str:
        return "–" if value is None else f"${value * 1_000_000:.3g}"

    return (
        f"in {per_million(model.prompt_price)} · cached {per_million(model.cached_prompt_price)}"
        f" · out {per_million(model.completion_price)} /M"
    )


def filter_model_entries(
    entries: list[dict[str, Any]],
    query: str,
    text_only: bool,
    max_price: float,
    max_rank: int,
    year: int,
    *,
    agent_only: bool = False,
) -> list[dict[str, Any]]:
    """Apply the model-browser filters. A negative price, or a non-positive rank or year,
    disables that filter; ``agent_only`` (Plan and Agent) keeps only tool-capable models."""
    query = query.strip().casefold()
    results = []
    for item in entries:
        if text_only and not item.get("textCompatible", False):
            continue
        if agent_only and not item.get("tools", False):
            continue
        if (
            query
            not in " ".join(
                str(item.get(key) or "") for key in ("id", "label", "description")
            ).casefold()
        ):
            continue
        price, rank = item.get("price"), item.get("rank")
        if max_price >= 0 and (price is None or float(price) > max_price):
            continue
        if max_rank > 0 and (rank is None or int(rank) > max_rank):
            continue
        if year > 0 and item.get("year") != year:
            continue
        results.append(item)
    return results
