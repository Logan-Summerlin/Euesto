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
TOOL_NAMES = frozenset({"read", "write", "edit", "bash", "grep", "find", "ls", "investigate_repository"})
PLAN_TOOLS = frozenset({"read", "grep", "find", "ls"})
INVESTIGATION_TOOLS = frozenset({"investigate_repository"})
AGENT_TOOLS = TOOL_NAMES
READ_TOOLS = PLAN_TOOLS | INVESTIGATION_TOOLS
MUTATION_TOOLS = frozenset({"write", "edit", "bash"})
# Protocol-level argument cap. It is a malformed-request guard, not a tool limit, so it
# must never bind below any per-tool limit the executor can be configured to accept.
# Derivation from the argument-carrying hard ceilings in executor/config.py
# (ExecutorConfig.HARD_CEILINGS; tests/test_protocol.py asserts this stays in sync):
#   write: content <= max_write_bytes (8 MB)
#   bash:  command <= max_command_bytes (1 MB) + stdin <= max_bash_stdin_bytes (8 MB)
#          + env <= 64 values x 16,384 bytes (~1.05 MB)                          = ~10.05 MB
#   edit:  old_str + new_str share the max_edit_result_bytes ceiling (16 MB)     = 16 MB  <- largest
# plus a fixed envelope for paths, hashes, env names, flags, and JSON structure.
MAX_TOOL_ARGUMENT_PAYLOAD_BYTES = 16_000_000
TOOL_ARGUMENT_ENVELOPE_BYTES = 1_000_000
MAX_TOOL_ARGUMENT_BYTES = MAX_TOOL_ARGUMENT_PAYLOAD_BYTES + TOOL_ARGUMENT_ENVELOPE_BYTES


def tool_argument_bytes(arguments: object) -> int:
    """Measure arguments as unescaped UTF-8 JSON, so escaping never shrinks the usable payload."""
    total = 0
    stack = [arguments]
    while stack:
        value = stack.pop()
        if isinstance(value, str): total += len(value.encode("utf-8")) + 2
        elif isinstance(value, dict):
            total += 2 + max(0, 2 * len(value) - 1)
            for key, item in value.items(): stack.append(str(key)); stack.append(item)
        elif isinstance(value, (list, tuple)):
            total += 2 + max(0, len(value) - 1); stack.extend(value)
        elif value is None or isinstance(value, (bool, int, float)): total += len(str(value))
        else: total += len(repr(value).encode("utf-8"))
        if total > MAX_TOOL_ARGUMENT_BYTES: break
    return total

@dataclass(frozen=True, slots=True)
class InvestigationResult:
    summary: str
    files_examined: tuple[str, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "files_examined": list(self.files_examined), "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_id: str
    run_id: str
    tool: str
    mode: Literal["plan", "agent"]
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id or not self.run_id: raise ValueError("Tool request IDs are required")
        if self.tool not in TOOL_NAMES: raise ValueError(f"Unknown tool: {self.tool}")
        if self.mode not in {"plan", "agent"}: raise ValueError("Executor tools require Plan or Agent mode")
        if self.mode == "plan" and self.tool not in PLAN_TOOLS: raise ValueError("Plan mode only permits read-only tools")
        if tool_argument_bytes(self.arguments) > MAX_TOOL_ARGUMENT_BYTES: raise ValueError("Tool arguments are too large")

    def to_dict(self) -> dict[str, Any]: return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolRequest":
        expected = {"request_id", "run_id", "tool", "mode", "arguments"}
        unknown = set(data) - expected
        if unknown: raise ValueError(f"Unknown tool request fields: {', '.join(sorted(unknown))}")
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict): raise ValueError("Tool arguments must be an object")
        return cls(str(data.get("request_id") or ""), str(data.get("run_id") or ""), str(data.get("tool") or ""), str(data.get("mode") or ""), dict(arguments))

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
    def to_dict(self) -> dict[str, Any]: return asdict(self)
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolResult":
        expected = {"request_id", "ok", "output", "data", "error_code", "truncated", "elapsed_seconds", "returned", "total_known", "limit", "next_cursor"}
        if set(data) - expected: raise ValueError("Unknown tool result fields")
        return cls(str(data.get("request_id") or ""), bool(data.get("ok")), str(data.get("output") or ""), dict(data.get("data") or {}), str(data["error_code"]) if data.get("error_code") else None, bool(data.get("truncated")), float(data.get("elapsed_seconds") or 0), _optional_nonnegative_int(data.get("returned")), _optional_nonnegative_int(data.get("total_known")), _optional_nonnegative_int(data.get("limit")), str(data["next_cursor"]) if data.get("next_cursor") else None)

def _optional_nonnegative_int(value: object) -> int | None:
    if value is None: return None
    try: parsed = int(value)
    except (TypeError, ValueError): return None
    return max(0, parsed)

@dataclass(frozen=True, slots=True)
class PublishOperation:
    path: str
    operation: Literal["create", "update", "delete"]
    base_sha256: str | None
    staged_sha256: str | None
    content: str | None = None
    base_mode: int | None = None
    staged_mode: int | None = None
    def __post_init__(self) -> None:
        import hashlib
        if self.operation not in {"create", "update", "delete"}: raise ValueError("Unknown publish operation")
        if self.operation == "create" and self.base_sha256 is not None: raise ValueError("Created files cannot have a base hash")
        if self.operation == "delete" and (self.staged_sha256 is not None or self.content is not None or self.staged_mode is not None): raise ValueError("Deleted files cannot include staged content")
        if self.operation != "delete":
            digest = hashlib.sha256((self.content or "").encode("utf-8")).hexdigest()
            if self.content is None or digest != self.staged_sha256: raise ValueError("Staged content hash mismatch")
            if self.staged_mode is not None and not 0 <= int(self.staged_mode) <= 0o7777: raise ValueError("Invalid staged file mode")

@dataclass(frozen=True, slots=True)
class PublishManifest:
    manifest_id: str
    run_id: str
    workspace_id: str
    source_snapshot_id: str
    approval_id: str
    operations: tuple[PublishOperation, ...]
    def __post_init__(self) -> None:
        if not all((self.manifest_id, self.run_id, self.workspace_id, self.source_snapshot_id, self.approval_id)): raise ValueError("Publish manifest identity is incomplete")
        paths = [item.path.casefold() for item in self.operations]
        if len(paths) != len(set(paths)): raise ValueError("Publish manifest contains aliased paths")
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self); data["operations"] = [asdict(item) for item in self.operations]; return data
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PublishManifest":
        expected = {"manifest_id", "run_id", "workspace_id", "source_snapshot_id", "approval_id", "operations"}
        if set(data) - expected: raise ValueError("Unknown publish manifest fields")
        raw = data.get("operations")
        if not isinstance(raw, list) or len(raw) > 500: raise ValueError("Publish operations must be a bounded array")
        return cls(str(data.get("manifest_id") or ""), str(data.get("run_id") or ""), str(data.get("workspace_id") or ""), str(data.get("source_snapshot_id") or ""), str(data.get("approval_id") or ""), tuple(PublishOperation(**item) for item in raw if isinstance(item, dict)))
