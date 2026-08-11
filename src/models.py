from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from shared.coercion import optional_float, optional_int

Role = Literal["system", "user", "assistant"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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

    def supports(self, parameter: str) -> bool:
        aliases = {
            "max_tokens": {"max_tokens", "max_completion_tokens"},
            "reasoning": {"reasoning"},
            "temperature": {"temperature"},
            "top_p": {"top_p"},
            "stop": {"stop"},
        }
        return bool(aliases.get(parameter, {parameter}) & self.supported_parameters)

    @property
    def release_year(self) -> int | None:
        if self.created is None:
            return None
        try:
            return datetime.fromtimestamp(self.created, UTC).year
        except (OSError, OverflowError, ValueError):
            return None

    @property
    def average_price_per_million(self) -> float | None:
        prices = [
            price
            for price in (self.prompt_price, self.completion_price)
            if price is not None
        ]
        return (sum(prices) / len(prices)) * 1_000_000 if prices else None

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
            artificial_analysis_score=optional_float(
                data.get("artificial_analysis_score")
            ),
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


DEFAULT_MODELS: tuple[ModelOption, ...] = (
    ModelOption("openrouter/auto", "OpenRouter Auto", 200_000),
    ModelOption("openai/gpt-4o-mini", "GPT-4o mini", 128_000),
    ModelOption("google/gemini-2.5-flash", "Gemini 2.5 Flash", 1_000_000),
    ModelOption("anthropic/claude-3.5-haiku", "Claude 3.5 Haiku", 200_000),
    ModelOption("deepseek/deepseek-chat-v3-0324", "DeepSeek V3", 64_000),
)

DEFAULT_SYSTEM_PROMPT = "You are a helpful, concise assistant."


def model_context_length(model_id: str, catalog: list[ModelOption] | None = None) -> int:
    options = catalog or list(DEFAULT_MODELS)
    return next(
        (option.context_length for option in options if option.id == model_id),
        128_000,
    )
