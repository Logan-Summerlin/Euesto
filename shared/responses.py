from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .coercion import optional_float
from .protocol import GATEWAY_VERSION, PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class GatewayStatus:
    ready: bool
    gateway_version: str = GATEWAY_VERSION
    protocol_version: str = PROTOCOL_VERSION
    event_versions: tuple[int, ...] = (1,)
    supported_tools: tuple[str, ...] = ()
    supported_modes: tuple[str, ...] = ("chat",)
    model_catalog_age_seconds: float | None = None
    executor_present: bool = False
    executor_status: str = "unavailable"
    active_workspace: str | None = None
    openrouter_key_configured: bool = False
    capabilities: tuple[dict[str, Any], ...] = ()
    resumable_runs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_versions"] = list(self.event_versions)
        data["supported_tools"] = list(self.supported_tools)
        data["supported_modes"] = list(self.supported_modes)
        data["capabilities"] = [dict(item) for item in self.capabilities]
        data["resumable_runs"] = list(self.resumable_runs)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayStatus:
        return cls(
            ready=bool(data.get("ready")),
            gateway_version=str(data.get("gateway_version") or ""),
            protocol_version=str(data.get("protocol_version") or ""),
            event_versions=tuple(int(item) for item in data.get("event_versions") or ()),
            supported_tools=tuple(str(item) for item in data.get("supported_tools") or ()),
            supported_modes=tuple(str(item) for item in data.get("supported_modes") or ()),
            model_catalog_age_seconds=optional_float(data.get("model_catalog_age_seconds")),
            executor_present=bool(data.get("executor_present")),
            executor_status=str(data.get("executor_status") or "unavailable"),
            active_workspace=(str(data["active_workspace"]) if data.get("active_workspace") else None),
            openrouter_key_configured=bool(data.get("openrouter_key_configured")),
            capabilities=tuple(dict(item) for item in data.get("capabilities") or () if isinstance(item, dict)),
            resumable_runs=tuple(str(item) for item in data.get("resumable_runs") or ()),
        )


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"error": asdict(self)}
