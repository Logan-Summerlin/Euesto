from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from server.journal import JournalStore
from shared.coercion import optional_float, optional_int

from .client import APP_TITLE
from .errors import ProviderError

MODELS_URL = "https://openrouter.ai/api/v1/models"


class GatewayCatalog:
    def __init__(
        self,
        journal: JournalStore,
        ttl_seconds: int = 86_400,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.journal = journal
        self.ttl_seconds = ttl_seconds
        self.transport = transport

    async def models(
        self, api_key: str | None, *, refresh: bool = False
    ) -> tuple[list[dict[str, Any]], str]:
        cached = self.journal.load_catalog()
        if cached and not refresh and self.age_seconds(cached[1]) <= self.ttl_seconds:
            return cached
        try:
            fetched = await self._fetch(api_key)
        except ProviderError:
            if cached and not refresh:
                return cached
            raise
        self.journal.save_catalog(*fetched)
        return fetched

    def cached_age_seconds(self) -> float | None:
        cached = self.journal.load_catalog()
        return self.age_seconds(cached[1]) if cached else None

    @staticmethod
    def age_seconds(fetched_at: str) -> float:
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(fetched_at)
        except ValueError:
            return float("inf")
        return max(0.0, age.total_seconds())

    async def _fetch(self, api_key: str | None) -> tuple[list[dict[str, Any]], str]:
        headers = {"X-Title": APP_TITLE}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        params = {"sort": "intelligence-high-to-low", "limit": 1000}
        try:
            async with httpx.AsyncClient(
                timeout=20.0, transport=self.transport, follow_redirects=False
            ) as client:
                response = await client.get(MODELS_URL, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ProviderError(
                "catalog.unavailable", f"Could not refresh the model catalog: {exc}", retryable=True
            ) from exc
        raw_models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            raise ProviderError(
                "catalog.invalid_response", "OpenRouter returned an invalid model catalog."
            )
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        models = [
            normalized
            for rank, raw in enumerate(raw_models, start=1)
            if isinstance(raw, dict) and (normalized := normalize_model(raw, fetched_at, rank))
        ]
        if not models:
            raise ProviderError("catalog.empty", "OpenRouter returned an empty model catalog.")
        return models, fetched_at


def normalize_model(raw: dict[str, Any], fetched_at: str, rank: int) -> dict[str, Any] | None:
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return None
    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    top_provider = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    evaluations = raw.get("evaluations") if isinstance(raw.get("evaluations"), dict) else {}
    score_key = "artificial_analysis_intelligence_index"
    return {
        "id": model_id,
        "label": str(raw.get("name") or model_id),
        "context_length": _positive_int(raw.get("context_length"))
        or _positive_int(top_provider.get("context_length"))
        or 128_000,
        "description": str(raw.get("description") or ""),
        "input_modalities": _strings(architecture.get("input_modalities") or ["text"]),
        "output_modalities": _strings(architecture.get("output_modalities") or ["text"]),
        "supported_parameters": _strings(raw.get("supported_parameters") or []),
        "prompt_price": optional_float(pricing.get("prompt")),
        "completion_price": optional_float(pricing.get("completion")),
        "cached_prompt_price": optional_float(pricing.get("input_cache_read")),
        "created": _positive_int(raw.get("created")),
        "artificial_analysis_score": optional_float(
            raw.get(score_key) or evaluations.get(score_key)
        ),
        "artificial_analysis_rank": rank,
        "fetched_at": fetched_at,
    }


def _strings(values: Any) -> list[str]:
    return [str(item) for item in values]


def _positive_int(value: object) -> int | None:
    number = optional_int(value)
    return number if number is not None and number > 0 else None
