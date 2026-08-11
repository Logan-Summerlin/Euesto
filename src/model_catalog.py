from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import DEFAULT_MODELS, ModelOption
from .storage import Storage

DEFAULT_CACHE_TTL = timedelta(hours=24)


class ModelCatalog:
    def __init__(self, storage: Storage):
        self.storage = storage

    def models(self) -> list[ModelOption]:
        cached = self.storage.list_catalog_models()
        return cached or list(DEFAULT_MODELS)

    def get(self, model_id: str) -> ModelOption | None:
        return next((model for model in self.models() if model.id == model_id), None)

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


def matches_model_filters(
    model: ModelOption,
    *,
    query: str = "",
    text_only: bool = True,
    max_price_per_million: float | None = None,
    max_artificial_analysis_rank: int | None = None,
    release_year: int | None = None,
) -> bool:
    """Return whether a catalog model passes the model-browser filters."""
    if text_only and not model.text_compatible:
        return False
    haystack = f"{model.id} {model.label} {model.description}".casefold()
    if query.strip().casefold() not in haystack:
        return False
    if max_price_per_million is not None:
        price = model.average_price_per_million
        if price is None or price > max_price_per_million:
            return False
    if max_artificial_analysis_rank is not None:
        rank = model.artificial_analysis_rank
        if rank is None or rank > max_artificial_analysis_rank:
            return False
    return release_year is None or model.release_year == release_year
