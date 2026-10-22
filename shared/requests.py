from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .permissions import APPROVAL_POLICIES

DEFAULT_INVESTIGATION_MODEL = "xiaomi/mimo-v2.5"
# Hard ceiling on investigate_repository calls per parent turn; also the default budget.
MAX_INVESTIGATION_CALLS = 4
# Workspace configuration the desktop syncs to the gateway, and its serialized size bound.
WORKSPACE_CONFIG_KEYS = frozenset(
    {
        "instructions",
        "active_skills",
        "default_mode",
        "context_policy",
        "custom_tools",
        # Files the user let through secret redaction (4.5), the opt-in documentation tools
        # (5.4), and the confirmed effective workspace policy (4.6).
        "unredacted_paths",
        "web_tools",
        "policy",
    }
)
MAX_WORKSPACE_CONFIG_BYTES = 128_000
NETWORK_MODES = ("none", "registries")


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
            raise ValueError("The chat endpoint accepts Chat mode only")
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
            "model",
            "messages",
            "options",
            "server_tools",
            "supported_parameters",
            "client_request_id",
            "provider_preferences",
            "mode",
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
            server_tools={
                key: bool(value) for key, value in _object(data.get("server_tools")).items()
            },
            supported_parameters=tuple(
                str(item) for item in data.get("supported_parameters") or ()
            ),
            client_request_id=str(data.get("client_request_id") or "") or None,
            provider_preferences=_object(data.get("provider_preferences")),
            mode=str(data.get("mode") or "chat"),
        )


def _object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return dict(value)


REASONING_EFFORTS = (None, "minimal", "low", "medium", "high")


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
    budget_profile: str = "coding"
    provider_preferences: dict[str, Any] = field(default_factory=dict)
    investigation_model_id: str | None = DEFAULT_INVESTIGATION_MODEL
    investigation_call_budget: int = MAX_INVESTIGATION_CALLS
    approval_policy: Literal["prompt", "sandbox", "auto"] = "prompt"
    # The agent phase's network mode (5.3): ``none`` or ``registries``. It is fixed per
    # session; ``registries`` requires a runtime started with the egress overlay.
    network_mode: Literal["none", "registries"] = "none"
    # Sent as ``reasoning.effort`` when set; the desktop sets it only for models that support
    # reasoning, and budget presets default to "low".
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Unknown reasoning effort")
        if (
            self.mode not in {"plan", "agent"}
            or not self.model.strip()
            or not self.workspace_id
        ):
            raise ValueError("Plan/Agent mode, model, and workspace identity are required")
        if not self.budget_profile.strip():
            raise ValueError("Agent budget profile is required")
        calls = self.investigation_call_budget
        if (
            not isinstance(calls, int)
            or isinstance(calls, bool)
            or not 1 <= calls <= MAX_INVESTIGATION_CALLS
        ):
            raise ValueError(
                f"Investigation call budget must be an integer from 1 to {MAX_INVESTIGATION_CALLS}"
            )
        if self.approval_policy not in APPROVAL_POLICIES:
            raise ValueError("Unknown agent approval policy")
        if self.network_mode not in NETWORK_MODES:
            raise ValueError("Unknown agent network mode")
        if self.mode != "agent" and self.network_mode != "none":
            raise ValueError("Network access is available only in Agent mode")
        if self.mode != "agent" and self.approval_policy != "prompt":
            raise ValueError("Automatic approval is available only in Agent mode")
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
        allowed_skill_fields = {
            "name",
            "description",
            "instructions",
            "required_tools",
            "references",
            "scope",
        }
        if any(set(item) - allowed_skill_fields for item in self.skills):
            raise ValueError("Skills contain unknown fields")
        if len(repr(self.workspace_config).encode("utf-8")) > MAX_WORKSPACE_CONFIG_BYTES:
            raise ValueError("Workspace configuration is too large")
        _validate_provider_preferences(self.provider_preferences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [dict(item) for item in self.messages],
            "mode": self.mode,
            "workspace_id": self.workspace_id,
            "approval_policy": self.approval_policy,
            "network_mode": self.network_mode,
            "session_id": self.session_id,
            "context_limit_tokens": self.context_limit_tokens,
            "skills": [dict(item) for item in self.skills],
            "workspace_config": dict(self.workspace_config),
            "budget_profile": self.budget_profile,
            "provider_preferences": dict(self.provider_preferences),
            "investigation_model_id": self.investigation_model_id,
            "investigation_call_budget": self.investigation_call_budget,
            "reasoning_effort": self.reasoning_effort,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRunRequest:
        expected = {
            "model",
            "messages",
            "mode",
            "workspace_id",
            "approval_policy",
            "network_mode",
            "session_id",
            "context_limit_tokens",
            "skills",
            "workspace_config",
            "budget_profile",
            "provider_preferences",
            "investigation_model_id",
            "investigation_call_budget",
            "reasoning_effort",
        }
        if set(data) - expected:
            raise ValueError("Unknown agent run fields")
        messages = data.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(item, dict) for item in messages
        ):
            raise ValueError("messages must be an object array")
        return cls(
            model=str(data.get("model") or ""),
            messages=tuple(dict(item) for item in messages),
            mode=str(data.get("mode") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            approval_policy=str(data.get("approval_policy") or "prompt"),
            network_mode=str(data.get("network_mode") or "none"),
            session_id=str(data["session_id"]) if data.get("session_id") else None,
            context_limit_tokens=int(data.get("context_limit_tokens") or 100_000),
            skills=tuple(
                dict(item) for item in data.get("skills") or () if isinstance(item, dict)
            ),
            workspace_config=_object(data.get("workspace_config")),
            budget_profile=str(data.get("budget_profile") or "coding"),
            provider_preferences=_object(data.get("provider_preferences")),
            investigation_model_id=str(data.get("investigation_model_id") or "")
            or DEFAULT_INVESTIGATION_MODEL,
            investigation_call_budget=int(
                data.get("investigation_call_budget") or MAX_INVESTIGATION_CALLS
            ),
            reasoning_effort=str(data["reasoning_effort"])
            if data.get("reasoning_effort")
            else None,
        )


def _validate_provider_preferences(value: dict[str, Any]) -> None:
    allowed = {"data_collection", "zdr"}
    if set(value) - allowed:
        raise ValueError("Unknown provider privacy preference")
    if value.get("data_collection", "deny") not in {"allow", "deny"}:
        raise ValueError("Provider data collection must be allow or deny")
    if "zdr" in value and not isinstance(value["zdr"], bool):
        raise ValueError("Provider ZDR preference must be boolean")
