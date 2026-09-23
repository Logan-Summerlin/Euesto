from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .models import DEFAULT_MODELS, ModelOption
from .storage import Storage

DEFAULT_CACHE_TTL = timedelta(hours=24)


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


def filter_model_entries(
    entries: list[dict[str, Any]], query: str, text_only: bool, max_price: float, max_rank: int, year: int
) -> list[dict[str, Any]]:
    """Apply the model-browser filters. A negative price, or a non-positive rank or year, disables that filter."""
    query = query.strip().casefold()
    results = []
    for item in entries:
        if text_only and not item.get("textCompatible", False):
            continue
        if query not in " ".join(str(item.get(key) or "") for key in ("id", "label", "description")).casefold():
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
