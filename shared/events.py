from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .protocol import EVENT_SCHEMA_VERSION

EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.started",
        "run.waiting",
        "run.paused",
        "run.resumed",
        "run.cancelled",
        "run.failed",
        "run.completed",
        "model.requested",
        "model.delta",
        "model.reasoning_summary",
        "model.completed",
        "model.failed",
        "plan.updated",
        "context.warning",
        "context.compacted",
        "context.inspected",
        "session.replayed",
        "skill.loaded",
        "command.expanded",
        "capability.discovered",
        "permission.rule_changed",
        "branch.created",
        "tool.requested",
        "approval.required",
        "approval.resolved",
        "permission.auto_granted",
        "tool.started",
        "tool.output",
        "tool.completed",
        "tool.failed",
        "usage.updated",
        "budget.warning",
        "budget.exhausted",
        "checkpoint.created",
        "checkpoint.restored",
        "publication.failed",
        "subagent.started", "subagent.tool_call", "subagent.tool_result",
        "subagent.completed", "subagent.failed",
    }
)

TERMINAL_EVENT_TYPES = frozenset({"run.cancelled", "run.failed", "run.completed"})


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: int
    run_id: str
    type: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_id < 1 or not self.run_id:
            raise ValueError("Event envelope requires a positive ID and run ID")
        if self.type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {self.type}")
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported event schema: {self.schema_version}")
        if not isinstance(self.payload, dict):
            raise ValueError("Event payload must be an object")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EventEnvelope:
        expected = {"event_id", "run_id", "type", "created_at", "schema_version", "payload"}
        unknown = set(data) - expected
        if unknown:
            raise ValueError(f"Unknown event fields: {', '.join(sorted(unknown))}")
        return cls(
            event_id=int(data["event_id"]),
            run_id=str(data["run_id"]),
            type=str(data["type"]),
            created_at=str(data["created_at"]),
            schema_version=int(data["schema_version"]),
            payload=dict(data.get("payload") or {}),
        )
