from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

from .coercion import optional_nonnegative_int, optional_string

# The executor's nine tools, in the order the gateway advertises them. investigate_repository
# runs in the gateway (a nested Plan-tool loop) and never reaches the executor itself.
EXECUTOR_TOOLS = ("read", "write", "edit", "apply_patch", "bash", "grep", "find", "ls", "status")
TOOL_NAMES = frozenset({*EXECUTOR_TOOLS, "investigate_repository"})
PLAN_TOOLS = frozenset({"read", "grep", "find", "ls"})
# Read-only tools: Plan's four, Agent-only staged-change review (status), and investigation.
READ_TOOLS = PLAN_TOOLS | {"status", "investigate_repository"}
MUTATION_TOOLS = frozenset({"write", "edit", "apply_patch", "bash"})
# File edits confined to staging: checkpointed, reversible, previewable as diffs, and still gated
# by publication approval. Bash is a mutation too but its effects are broader and harder to preview.
STAGED_EDIT_TOOLS = frozenset({"write", "edit", "apply_patch"})
# Independent, side-effect-free executor calls that a turn may run concurrently. Investigation
# is read-only too, but it drives a nested model loop against the shared parent budget, so it
# stays serialized with the mutations.
PARALLEL_SAFE_TOOLS = PLAN_TOOLS | {"status"}
# Protocol-level argument cap. It is a malformed-request guard, not a tool limit, so it
# must never bind below any per-tool limit the executor can be configured to accept.
# Derivation from the argument-carrying hard ceilings in executor/config.py
# (ExecutorConfig.HARD_CEILINGS; tests/test_protocol.py asserts this stays in sync):
#   write:       content <= max_write_bytes (8 MB)
#   bash:        command <= max_command_bytes (1 MB) + stdin <= max_bash_stdin_bytes (8 MB)
#                + env <= 64 values x 16,384 bytes (~1.05 MB)                     = ~10.05 MB
#   edit:        old_str + new_str share the max_edit_result_bytes ceiling (16 MB) = 16 MB  <- largest
#   apply_patch: all content/old_str/new_str share max_patch_bytes (16 MB)        = 16 MB  <- largest
# plus a fixed envelope for paths, hashes, env names, flags, and JSON structure.
MAX_TOOL_ARGUMENT_PAYLOAD_BYTES = 16_000_000
TOOL_ARGUMENT_ENVELOPE_BYTES = 1_000_000
MAX_TOOL_ARGUMENT_BYTES = MAX_TOOL_ARGUMENT_PAYLOAD_BYTES + TOOL_ARGUMENT_ENVELOPE_BYTES

# One publication batch: the desktop broker's per-manifest ceiling. Larger changesets are
# published as ordered batches, each separately approved and hash-validated.
PUBLISH_BATCH_MAX_OPERATIONS = 500
PUBLISH_BATCH_MAX_BYTES = 32_000_000
MAX_PUBLISH_BATCHES = 2_000


def tool_argument_bytes(arguments: object) -> int:
    """Measure arguments as unescaped UTF-8 JSON, so escaping never shrinks the usable payload."""
    total = 0
    stack = [arguments]
    while stack and total <= MAX_TOOL_ARGUMENT_BYTES:
        value = stack.pop()
        if isinstance(value, str):
            total += len(value.encode("utf-8")) + 2
        elif isinstance(value, dict):
            total += 2 + max(0, 2 * len(value) - 1)
            for key, item in value.items():
                stack.extend((str(key), item))
        elif isinstance(value, list | tuple):
            total += 2 + max(0, len(value) - 1)
            stack.extend(value)
        elif value is None or isinstance(value, bool | int | float):
            total += len(str(value))
        else:
            total += len(repr(value).encode("utf-8"))
    return total


def _require_known_fields(data: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = set(data) - expected
    if unknown:
        raise ValueError(f"Unknown {label} fields: {', '.join(sorted(unknown))}")


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
        if tool_argument_bytes(self.arguments) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("Tool arguments are too large")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolRequest:
        _require_known_fields(data, {"request_id", "run_id", "tool", "mode", "arguments"}, "tool request")
        arguments = data.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return cls(
            request_id=str(data.get("request_id") or ""),
            run_id=str(data.get("run_id") or ""),
            tool=str(data.get("tool") or ""),
            mode=str(data.get("mode") or ""),
            arguments=dict(arguments),
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
    def from_dict(cls, data: dict[str, Any]) -> ToolResult:
        _require_known_fields(data, {item.name for item in fields(cls)}, "tool result")
        return cls(
            request_id=str(data.get("request_id") or ""),
            ok=bool(data.get("ok")),
            output=str(data.get("output") or ""),
            data=dict(data.get("data") or {}),
            error_code=optional_string(data.get("error_code")),
            truncated=bool(data.get("truncated")),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0),
            returned=optional_nonnegative_int(data.get("returned")),
            total_known=optional_nonnegative_int(data.get("total_known")),
            limit=optional_nonnegative_int(data.get("limit")),
            next_cursor=optional_string(data.get("next_cursor")),
        )


PUBLISH_OPERATIONS = frozenset({"create", "update", "delete"})


@dataclass(frozen=True, slots=True)
class PublishOperation:
    """One host file change. Text travels as ``content``; bytes that are not UTF-8 text
    (images, fonts, fixture databases) travel as ``content_base64``. Binary content is only
    ever copied, replaced, or deleted: no diff or line-based validation applies to it."""

    path: str
    operation: Literal["create", "update", "delete"]
    base_sha256: str | None
    staged_sha256: str | None
    content: str | None = None
    base_mode: int | None = None
    staged_mode: int | None = None
    content_base64: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in PUBLISH_OPERATIONS:
            raise ValueError("Unknown publish operation")
        if self.operation == "create" and self.base_sha256 is not None:
            raise ValueError("Created files cannot have a base hash")
        if self.operation == "delete":
            staged = (self.staged_sha256, self.content, self.content_base64, self.staged_mode)
            if any(item is not None for item in staged):
                raise ValueError("Deleted files cannot include staged content")
            return
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("Staged content must be exactly one of text content or content_base64")
        if hashlib.sha256(self.payload()).hexdigest() != self.staged_sha256:
            raise ValueError("Staged content hash mismatch")
        if self.staged_mode is not None and not 0 <= int(self.staged_mode) <= 0o7777:
            raise ValueError("Invalid staged file mode")

    @property
    def binary(self) -> bool:
        return self.content_base64 is not None

    def payload(self) -> bytes:
        """The exact bytes to publish (empty for a delete)."""
        if self.content_base64 is not None:
            try:
                return base64.b64decode(self.content_base64.encode("ascii"), validate=True)
            except (UnicodeError, binascii.Error, ValueError):
                raise ValueError("Invalid content_base64 encoding") from None
        return (self.content or "").encode("utf-8")

    def payload_bytes(self) -> int:
        if self.content_base64 is not None:
            return len(self.content_base64) * 3 // 4 - self.content_base64[-2:].count("=")
        return len((self.content or "").encode("utf-8"))


def _check_batch_position(batch_index: int, batch_count: int) -> None:
    if not 1 <= batch_index <= batch_count <= MAX_PUBLISH_BATCHES:
        raise ValueError("Publication batch position is out of range")


def _batch_number(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Publication batch numbers must be integers")
    return value


def _with_operation_list(data: dict[str, Any]) -> dict[str, Any]:
    data["operations"] = list(data["operations"])
    return data


def _bounded_operations(data: dict[str, Any], message: str) -> list[Any]:
    raw = data.get("operations")
    if not isinstance(raw, list) or len(raw) > PUBLISH_BATCH_MAX_OPERATIONS:
        raise ValueError(message)
    return raw


@dataclass(frozen=True, slots=True)
class PublishManifest:
    """One reviewed publication batch.

    A changeset larger than one batch (``PUBLISH_BATCH_MAX_OPERATIONS`` operations or
    ``PUBLISH_BATCH_MAX_BYTES`` bytes) is published as ordered batches sharing one
    ``publication_id``; each batch is a complete, independently approved manifest.
    """

    manifest_id: str
    run_id: str
    workspace_id: str
    source_snapshot_id: str
    approval_id: str
    operations: tuple[PublishOperation, ...]
    publication_id: str = ""
    batch_index: int = 1
    batch_count: int = 1

    def __post_init__(self) -> None:
        identity = (self.manifest_id, self.run_id, self.workspace_id, self.source_snapshot_id, self.approval_id)
        if not all(identity):
            raise ValueError("Publish manifest identity is incomplete")
        paths = [item.path.casefold() for item in self.operations]
        if len(paths) != len(set(paths)):
            raise ValueError("Publish manifest contains aliased paths")
        if not self.publication_id:
            object.__setattr__(self, "publication_id", self.manifest_id)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.batch_index, self.batch_count)):
            raise ValueError("Publication batch numbers must be integers")
        _check_batch_position(self.batch_index, self.batch_count)

    @property
    def has_more_batches(self) -> bool:
        return self.batch_index < self.batch_count

    def to_dict(self) -> dict[str, Any]:
        return _with_operation_list(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishManifest:
        if set(data) - {item.name for item in fields(cls)}:
            raise ValueError("Unknown publish manifest fields")
        raw = _bounded_operations(data, "Publish operations must be a bounded array")
        return cls(
            manifest_id=str(data.get("manifest_id") or ""),
            run_id=str(data.get("run_id") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            source_snapshot_id=str(data.get("source_snapshot_id") or ""),
            approval_id=str(data.get("approval_id") or ""),
            operations=tuple(PublishOperation(**item) for item in raw if isinstance(item, dict)),
            publication_id=str(data.get("publication_id") or ""),
            batch_index=_batch_number(data.get("batch_index")),
            batch_count=_batch_number(data.get("batch_count")),
        )


@dataclass(frozen=True, slots=True)
class PublishedOperation:
    """What reached the host for one path: enough to advance the staging baseline."""

    path: str
    operation: Literal["create", "update", "delete"]
    staged_sha256: str | None
    staged_mode: int | None = None

    def __post_init__(self) -> None:
        if self.operation not in PUBLISH_OPERATIONS:
            raise ValueError("Unknown publish operation")
        if (self.operation == "delete") != (self.staged_sha256 is None):
            raise ValueError("Published operation hash does not match its kind")


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Content-free confirmation that one manifest batch was published to the host.

    The staging baseline only needs paths, hashes, and modes, so the hand-off never re-sends
    file content (a batch may carry up to ``PUBLISH_BATCH_MAX_BYTES``).
    """

    manifest_id: str
    run_id: str
    workspace_id: str
    source_snapshot_id: str
    publication_id: str
    batch_index: int
    batch_count: int
    operations: tuple[PublishedOperation, ...]

    def __post_init__(self) -> None:
        identity = (self.manifest_id, self.run_id, self.workspace_id, self.source_snapshot_id, self.publication_id)
        if not all(identity):
            raise ValueError("Publication receipt identity is incomplete")
        if len(self.operations) > PUBLISH_BATCH_MAX_OPERATIONS:
            raise ValueError("Publication receipt exceeds the batch operation limit")
        _check_batch_position(self.batch_index, self.batch_count)

    @classmethod
    def from_manifest(cls, manifest: PublishManifest) -> PublicationReceipt:
        return cls(
            manifest_id=manifest.manifest_id,
            run_id=manifest.run_id,
            workspace_id=manifest.workspace_id,
            source_snapshot_id=manifest.source_snapshot_id,
            publication_id=manifest.publication_id,
            batch_index=manifest.batch_index,
            batch_count=manifest.batch_count,
            operations=tuple(
                PublishedOperation(item.path, item.operation, item.staged_sha256, item.staged_mode)
                for item in manifest.operations
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return _with_operation_list(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublicationReceipt:
        if set(data) - {item.name for item in fields(cls)}:
            raise ValueError("Unknown publication receipt fields")
        raw = _bounded_operations(data, "Publication receipt operations must be a bounded array")
        operation_fields = {item.name for item in fields(PublishedOperation)}
        operations = []
        for item in raw:
            if not isinstance(item, dict) or set(item) - operation_fields:
                raise ValueError("Invalid publication receipt operation")
            mode = item.get("staged_mode")
            operations.append(
                PublishedOperation(
                    path=str(item.get("path") or ""),
                    operation=str(item.get("operation") or ""),
                    staged_sha256=optional_string(item.get("staged_sha256")),
                    staged_mode=int(mode) if mode is not None else None,
                )
            )
        return cls(
            manifest_id=str(data.get("manifest_id") or ""),
            run_id=str(data.get("run_id") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            source_snapshot_id=str(data.get("source_snapshot_id") or ""),
            publication_id=str(data.get("publication_id") or ""),
            batch_index=_batch_number(data.get("batch_index")),
            batch_count=_batch_number(data.get("batch_count")),
            operations=tuple(operations),
        )
