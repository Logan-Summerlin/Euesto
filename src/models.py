from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from shared.coercion import optional_float, optional_int, optional_string

Role = Literal["system", "user", "assistant"]
# Per-message provider/usage metadata carried by forks and imports, with its coercion.
MESSAGE_METADATA_FIELDS = {
    "model_id": optional_string,
    "provider_id": optional_string,
    "finish_reason": optional_string,
    "input_tokens": optional_int,
    "output_tokens": optional_int,
    "cached_tokens": optional_int,
    "reasoning_tokens": optional_int,
    "total_tokens": optional_int,
    "cost": optional_float,
    "time_to_first_token": optional_float,
    "elapsed_seconds": optional_float,
    "tokens_per_second": optional_float,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Blended picker estimate (MODEL_CONTROLS_PLAN §2): a typical agent workload is mostly input,
# and most of that input is served from the prompt cache.
ESTIMATED_INPUT_SHARE = 0.95
ESTIMATED_OUTPUT_SHARE = 0.05
ESTIMATED_CACHE_HIT_SHARE = 0.95


@dataclass(slots=True)
class Conversation:
    id: str
    title: str
    model: str
    system_prompt: str
    created_at: str
    updated_at: str
    active_leaf_id: int | None = None
    pinned_at: str | None = None
    archived_at: str | None = None
    prompt_preset_id: str | None = None
    prompt_preset_snapshot: str | None = None


@dataclass(slots=True)
class Message:
    id: int | None
    conversation_id: str
    role: Role
    content: str
    created_at: str
    parent_message_id: int | None = None
    model_id: str | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    time_to_first_token: float | None = None
    elapsed_seconds: float | None = None
    tokens_per_second: float | None = None


@dataclass(frozen=True, slots=True)
class ModelOption:
    id: str
    label: str
    context_length: int = 128_000
    description: str = ""
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    supported_parameters: frozenset[str] = frozenset()
    prompt_price: float | None = None
    completion_price: float | None = None
    cached_prompt_price: float | None = None
    created: int | None = None
    artificial_analysis_score: float | None = None
    artificial_analysis_rank: int | None = None
    fetched_at: str | None = None

    @property
    def text_compatible(self) -> bool:
        return "text" in self.input_modalities and "text" in self.output_modalities

    @property
    def release_year(self) -> int | None:
        if self.created is None:
            return None
        try:
            return datetime.fromtimestamp(self.created, UTC).year
        except (OSError, OverflowError, ValueError):
            return None

    @property
    def blended_price_per_million(self) -> float | None:
        """Estimated USD per million tokens at 95% input, 5% output, and 95% cache hits.

        Blend: ``0.95 * (0.95 * cached + 0.05 * prompt) + 0.05 * completion``, which is
        ``0.9025·cached + 0.0475·prompt + 0.05·completion`` per million tokens.

        Missing prices fall back to the nearest published one; a model without any input price
        has no estimate and returns ``None``:

        - no ``cached_prompt_price``: all input at ``prompt_price``;
        - no ``prompt_price``: uncached input at the cached price;
        - no ``completion_price``: output at the cached-input price, else the prompt price;
        - none of the three: ``None``.
        """
        cached, prompt, completion = (
            self.cached_prompt_price,
            self.prompt_price,
            self.completion_price,
        )
        # Input prices present, cached first: rates[0] is the cached price, or the prompt price
        # when no cached price is published; rates[-1] is the prompt price, or the cached one.
        input_rates = [price for price in (cached, prompt) if price is not None]
        if not input_rates:
            return None
        output_rate = completion if completion is not None else input_rates[0]
        input_rate = (
            ESTIMATED_CACHE_HIT_SHARE * input_rates[0]
            + (1 - ESTIMATED_CACHE_HIT_SHARE) * input_rates[-1]
        )
        blended = ESTIMATED_INPUT_SHARE * input_rate + ESTIMATED_OUTPUT_SHARE * output_rate
        return blended * 1_000_000

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["input_modalities"] = list(self.input_modalities)
        data["output_modalities"] = list(self.output_modalities)
        data["supported_parameters"] = sorted(self.supported_parameters)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ModelOption:
        return cls(
            id=str(data["id"]),
            label=str(data.get("label") or data["id"]),
            context_length=max(1, int(data.get("context_length") or 128_000)),
            description=str(data.get("description") or ""),
            input_modalities=tuple(data.get("input_modalities") or ("text",)),
            output_modalities=tuple(data.get("output_modalities") or ("text",)),
            supported_parameters=frozenset(data.get("supported_parameters") or ()),
            prompt_price=optional_float(data.get("prompt_price")),
            completion_price=optional_float(data.get("completion_price")),
            cached_prompt_price=optional_float(data.get("cached_prompt_price")),
            created=optional_int(data.get("created")),
            artificial_analysis_score=optional_float(data.get("artificial_analysis_score")),
            artificial_analysis_rank=optional_int(data.get("artificial_analysis_rank")),
            fetched_at=str(data["fetched_at"]) if data.get("fetched_at") else None,
        )


@dataclass(slots=True)
class RequestOptions:
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    data_collection: Literal["allow", "deny"] = "deny"
    zero_data_retention: bool = False


@dataclass(frozen=True, slots=True)
class ServerToolOptions:
    web_search: bool = False
    web_fetch: bool = False
    datetime: bool = False


@dataclass(slots=True)
class PromptPreset:
    id: str
    name: str
    content: str
    created_at: str
    updated_at: str


# Offline defaults until the catalog loads (plan 0.9): low-cost models that call tools and
# support prompt caching. The catalog supplies current prices; these carry none.
_AGENT_PARAMETERS = frozenset({"tools", "tool_choice", "reasoning", "max_tokens"})
DEFAULT_MODELS: tuple[ModelOption, ...] = (
    ModelOption(
        "google/gemini-2.5-flash",
        "Gemini 2.5 Flash",
        1_048_576,
        "",
        supported_parameters=_AGENT_PARAMETERS,
    ),
    ModelOption(
        "openai/gpt-5-mini", "GPT-5 mini", 400_000, "", supported_parameters=_AGENT_PARAMETERS
    ),
    ModelOption(
        "anthropic/claude-haiku-4.5",
        "Claude Haiku 4.5",
        200_000,
        "",
        supported_parameters=_AGENT_PARAMETERS,
    ),
    ModelOption(
        "deepseek/deepseek-chat-v3.1",
        "DeepSeek V3.1",
        163_840,
        "",
        supported_parameters=_AGENT_PARAMETERS,
    ),
    ModelOption(
        "xiaomi/mimo-v2.5", "MiMo-V2.5", 1_000_000, "", supported_parameters=_AGENT_PARAMETERS
    ),
)

DEFAULT_SYSTEM_PROMPT = "You are a helpful, concise assistant."


def model_context_length(model_id: str, catalog: list[ModelOption] | None = None) -> int:
    options = catalog or list(DEFAULT_MODELS)
    return next(
        (option.context_length for option in options if option.id == model_id),
        128_000,
    )


def usage_labels(
    tokens: int | None,
    cost: float | None,
    cached: int | None = None,
    reasoning: int | None = None,
    prompt: int | None = None,
) -> list[str]:
    """Token and cost labels, worded the same in the status bar, transcript, and usage dialog.

    With ``prompt`` (input tokens), the cached count carries the cache hit rate (plan 1.1).
    """
    labels = [f"{tokens:,} tokens"] if tokens else []
    if cached:
        rate = min(cached, prompt) / prompt if prompt else None
        share = (
            ""
            if rate is None
            else (" (<1% of input)" if rate < 0.01 else f" ({rate:.0%} of input)")
        )
        labels.append(f"{cached:,} cached{share}")
    labels += [f"{reasoning:,} reasoning"] if reasoning else []
    if cost is not None:
        labels.append(f"${cost:.6f}")
    return labels
