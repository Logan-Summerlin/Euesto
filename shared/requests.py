from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DEFAULT_INVESTIGATION_MODEL = "xiaomi/mimo-v2.5"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Invalid message role")
        if not isinstance(self.content, str):
            raise ValueError("Message content must be text")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    options: dict[str, Any] = field(default_factory=dict)
    server_tools: dict[str, bool] = field(default_factory=dict)
    supported_parameters: tuple[str, ...] = ()
    client_request_id: str | None = None
    provider_preferences: dict[str, Any] = field(default_factory=dict)
    mode: Literal["chat"] = "chat"

    def __post_init__(self) -> None:
        if self.mode != "chat":
            raise ValueError("The v0.3 chat endpoint accepts Chat mode only")
        if not self.model.strip():
            raise ValueError("A model ID is required")
        if not self.messages:
            raise ValueError("At least one message is required")
        if len(self.messages) > 500:
            raise ValueError("Too many messages")
        if sum(len(message.content) for message in self.messages) > 2_000_000:
            raise ValueError("Conversation context is too large")
        unknown_tools = set(self.server_tools) - {"web_search", "web_fetch", "datetime"}
        if unknown_tools:
            raise ValueError(f"Unknown server tools: {', '.join(sorted(unknown_tools))}")
        _validate_provider_preferences(self.provider_preferences)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["messages"] = [asdict(message) for message in self.messages]
        data["supported_parameters"] = list(self.supported_parameters)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatRequest:
        expected = {
            "model", "messages", "options", "server_tools", "supported_parameters",
            "client_request_id", "provider_preferences", "mode",
        }
        unknown = set(data) - expected
        if unknown:
            raise ValueError(f"Unknown chat fields: {', '.join(sorted(unknown))}")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("messages must be an array")
        return cls(
            model=str(data.get("model") or ""),
            messages=tuple(
                ChatMessage(role=str(item.get("role") or ""), content=item.get("content"))
                for item in raw_messages
                if isinstance(item, dict)
            ),
            options=_object(data.get("options")),
            server_tools={key: bool(value) for key, value in _object(data.get("server_tools")).items()},
            supported_parameters=tuple(str(item) for item in data.get("supported_parameters") or ()),
            client_request_id=str(data["client_request_id"]) if data.get("client_request_id") else None,
            provider_preferences=_object(data.get("provider_preferences")),
            mode=str(data.get("mode") or "chat"),
        )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validate_provider_preferences(value: dict[str, Any]) -> None:
    allowed = {"allow", "deny", "require_parameters", "data_collection", "zdr"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown provider preference fields: {', '.join(sorted(unknown))}")
