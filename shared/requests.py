from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("Invalid message role")
        if not isinstance(self.content, str):
            raise ValueError("Message content must be text")


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


def _object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    model: str
    messages: tuple[dict[str, Any], ...]
    mode: Literal["plan", "agent"]
    workspace_id: str
    session_id: str | None = None
    context_limit_tokens: int = 100_000
    skills: tuple[dict[str, Any], ...] = ()
    workspace_config: dict[str, Any] = field(default_factory=dict)
    max_iterations: int = 101
    max_tool_calls: int = 100
    max_wall_seconds: int = 900
    max_cost: float = 1.0
    budget_profile: str = "coding"
    provider_preferences: dict[str, Any] = field(default_factory=dict)
    investigation_model_id: str | None = None
    approval_policy: Literal["prompt", "auto"] = "prompt"

    def __post_init__(self) -> None:
        if self.mode not in {"plan", "agent"} or not self.model.strip() or not self.workspace_id:
            raise ValueError("Plan/Agent mode, model, and workspace identity are required")
        if not self.budget_profile.strip():
            raise ValueError("Agent budget profile is required")
        if self.approval_policy not in {"prompt", "auto"}:
            raise ValueError("Unknown agent approval policy")
        if self.mode != "agent" and self.approval_policy != "prompt":
            raise ValueError("Automatic approval is available only in Agent mode")
        if (
            not 1 <= self.max_iterations <= 1000
            or not 1 <= self.max_tool_calls <= 2000
            or not 10 <= self.max_wall_seconds <= 7200
            or not 0 < self.max_cost <= 100
        ):
            raise ValueError("Agent budget is outside safe limits")
        if not self.messages or len(self.messages) > 500:
            raise ValueError("A bounded message context is required")
        total_message_bytes = 0
        for message in self.messages:
            if set(message) - {"role", "content"}:
                raise ValueError("Agent input messages contain unknown fields")
            if message.get("role") not in {"system", "user", "assistant"}:
                raise ValueError("Agent input messages contain an invalid role")
            if not isinstance(message.get("content"), str):
                raise ValueError("Agent message content must be text")
            total_message_bytes += len(message["content"].encode("utf-8"))
        if total_message_bytes > 2_000_000:
            raise ValueError("Agent message context is too large")
        if not 4_000 <= self.context_limit_tokens <= 2_000_000:
            raise ValueError("Context limit is outside safe bounds")
        if len(self.skills) > 20:
            raise ValueError("Too many active skills")
        if any(not isinstance(item, dict) for item in self.skills):
            raise ValueError("Skills must be objects")
        allowed_skill_fields = {"name", "description", "instructions", "required_tools", "references", "scope"}
        if any(set(item) - allowed_skill_fields for item in self.skills):
            raise ValueError("Skills contain unknown fields")
        if len(repr(self.workspace_config).encode("utf-8")) > 128_000:
            raise ValueError("Workspace configuration is too large")
        _validate_provider_preferences(self.provider_preferences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [dict(item) for item in self.messages],
            "mode": self.mode,
            "workspace_id": self.workspace_id,
            "approval_policy": self.approval_policy,
            "session_id": self.session_id,
            "context_limit_tokens": self.context_limit_tokens,
            "skills": [dict(item) for item in self.skills],
            "workspace_config": dict(self.workspace_config),
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "max_wall_seconds": self.max_wall_seconds,
            "max_cost": self.max_cost,
            "budget_profile": self.budget_profile,
            "provider_preferences": dict(self.provider_preferences),
            "investigation_model_id": self.investigation_model_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRunRequest:
        expected = {
            "model", "messages", "mode", "workspace_id", "approval_policy", "session_id",
            "context_limit_tokens", "skills", "workspace_config", "max_iterations",
            "max_tool_calls", "max_wall_seconds", "max_cost", "budget_profile",
            "provider_preferences", "investigation_model_id",
        }
        if set(data) - expected:
            raise ValueError("Unknown agent run fields")
        messages = data.get("messages")
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise ValueError("messages must be an object array")
        return cls(
            model=str(data.get("model") or ""),
            messages=tuple(dict(item) for item in messages),
            mode=str(data.get("mode") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            approval_policy=str(data.get("approval_policy") or "prompt"),
            session_id=str(data["session_id"]) if data.get("session_id") else None,
            context_limit_tokens=int(data.get("context_limit_tokens") or 100_000),
            skills=tuple(dict(item) for item in data.get("skills") or () if isinstance(item, dict)),
            workspace_config=_object(data.get("workspace_config")),
            max_iterations=int(data.get("max_iterations") or 101),
            max_tool_calls=int(data.get("max_tool_calls") or 100),
            max_wall_seconds=int(data.get("max_wall_seconds") or 900),
            max_cost=float(data.get("max_cost") or 1.0),
            budget_profile=str(data.get("budget_profile") or "coding"),
            provider_preferences=_object(data.get("provider_preferences")),
            investigation_model_id=str(data["investigation_model_id"]) if data.get("investigation_model_id") else None,
        )


def _validate_provider_preferences(value: dict[str, Any]) -> None:
    allowed = {"data_collection", "zdr"}
    if set(value) - allowed:
        raise ValueError("Unknown provider privacy preference")
    if value.get("data_collection", "deny") not in {"allow", "deny"}:
        raise ValueError("Provider data collection must be allow or deny")
    if "zdr" in value and not isinstance(value["zdr"], bool):
        raise ValueError("Provider ZDR preference must be boolean")
