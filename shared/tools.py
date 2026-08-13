from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


def openrouter_tools(enabled: Mapping[str, bool]) -> list[dict[str, Any]]:
    """Build the visible OpenRouter-hosted tools from composer settings."""
    tools: list[dict[str, Any]] = []
    if enabled.get("web_search"):
        tools.append({"type": "openrouter:web_search", "parameters": {"search_context_size": "low", "max_total_results": 12, "max_results": 4}})
    if enabled.get("web_fetch"):
        tools.append({"type": "openrouter:web_fetch", "parameters": {"max_content_tokens": 3000}})
    if enabled.get("datetime"):
        tools.append({"type": "openrouter:datetime"})
    return tools


TOOL_PROFILE = "pi-compatible"
TOOL_NAMES = frozenset({"read", "write", "edit", "bash", "grep", "find", "ls"})
PLAN_TOOLS = frozenset({"read", "grep", "find", "ls"})
AGENT_TOOLS = TOOL_NAMES
READ_TOOLS = PLAN_TOOLS
MUTATION_TOOLS = frozenset({"write", "edit", "bash"})
MAX_TOOL_ARGUMENT_BYTES = 512_000


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_id: str
    run_id: str
    tool: str
    mode: Literal["plan", "agent"]
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id or not self.run_id:
            raise ValueError("Tool request IDs are required")
        if self.tool not in TOOL_NAMES:
            raise ValueError(f"Unknown tool: {self.tool}")
        if self.mode not in {"plan", "agent"}:
            raise ValueError("Executor tools require Plan or Agent mode")
        if self.mode == "plan" and self.tool not in PLAN_TOOLS:
            raise ValueError("Plan mode only permits read-only tools")
        if len(repr(self.arguments).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("Tool arguments are too large")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolRequest":
        expected = {"request_id", "run_id", "tool", "mode", "arguments"}
        unknown = set(data) - expected
        if unknown:
            raise ValueError(f"Unknown tool request fields: {', '.join(sorted(unknown))}")
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return cls(
            request_id=str(data.get("request_id") or ""), run_id=str(data.get("run_id") or ""),
            tool=str(data.get("tool") or ""), mode=str(data.get("mode") or ""), arguments=dict(arguments),
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    request_id: str
    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    truncated: bool = False
    elapsed_seconds: float = 0.0
    returned: int | None = None
    total_known: int | None = None
    limit: int | None = None
    next_cursor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolResult":
        expected = {"request_id", "ok", "output", "data", "error_code", "truncated", "elapsed_seconds", "returned", "total_known", "limit", "next_cursor"}
        if set(data) - expected:
            raise ValueError("Unknown tool result fields")
        return cls(
            request_id=str(data.get("request_id") or ""), ok=bool(data.get("ok")), output=str(data.get("output") or ""),
            data=dict(data.get("data") or {}), error_code=str(data["error_code"]) if data.get("error_code") else None,
            truncated=bool(data.get("truncated")), elapsed_seconds=float(data.get("elapsed_seconds") or 0),
            returned=_optional_nonnegative_int(data.get("returned")), total_known=_optional_nonnegative_int(data.get("total_known")),
            limit=_optional_nonnegative_int(data.get("limit")), next_cursor=(str(data["next_cursor"]) if data.get("next_cursor") else None),
        )


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


@dataclass(frozen=True, slots=True)
class PublishOperation:
    path: str
    operation: Literal["create", "update", "delete"]
    base_sha256: str | None
    staged_sha256: str | None
    content: str | None = None

    def __post_init__(self) -> None:
        import hashlib
        if self.operation not in {"create", "update", "delete"}:
            raise ValueError("Unknown publish operation")
        if self.operation == "create" and self.base_sha256 is not None:
            raise ValueError("Created files cannot have a base hash")
        if self.operation == "delete" and (self.staged_sha256 is not None or self.content is not None):
            raise ValueError("Deleted files cannot include staged content")
        if self.operation != "delete":
            digest = hashlib.sha256((self.content or "").encode("utf-8")).hexdigest()
            if self.content is None or digest != self.staged_sha256:
                raise ValueError("Staged content hash mismatch")


@dataclass(frozen=True, slots=True)
class PublishManifest:
    manifest_id: str
    run_id: str
    workspace_id: str
    source_snapshot_id: str
    approval_id: str
    operations: tuple[PublishOperation, ...]

    def __post_init__(self) -> None:
        if not all((self.manifest_id, self.run_id, self.workspace_id, self.source_snapshot_id, self.approval_id)):
            raise ValueError("Publish manifest identity is incomplete")
        paths = [item.path.casefold() for item in self.operations]
        if len(paths) != len(set(paths)):
            raise ValueError("Publish manifest contains aliased paths")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["operations"] = [asdict(item) for item in self.operations]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishManifest":
        expected = {"manifest_id", "run_id", "workspace_id", "source_snapshot_id", "approval_id", "operations"}
        if set(data) - expected:
            raise ValueError("Unknown publish manifest fields")
        raw = data.get("operations")
        if not isinstance(raw, list) or len(raw) > 500:
            raise ValueError("Publish operations must be a bounded array")
        return cls(
            manifest_id=str(data.get("manifest_id") or ""), run_id=str(data.get("run_id") or ""), workspace_id=str(data.get("workspace_id") or ""),
            source_snapshot_id=str(data.get("source_snapshot_id") or ""), approval_id=str(data.get("approval_id") or ""),
            operations=tuple(PublishOperation(**item) for item in raw if isinstance(item, dict)),
        )
