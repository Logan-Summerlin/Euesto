from __future__ import annotations

import shlex
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from .tools import MUTATION_TOOLS, READ_TOOLS, ToolRequest


class PermissionDecision(StrEnum):
    DENY = "deny"
    ASK = "ask"
    ALLOW_ONCE = "allow_once"
    ALLOW_RUN = "allow_run"
    ALLOW_RULE = "allow_rule"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    rule_id: str
    decision: PermissionDecision
    workspace_id: str
    mode: str
    tool: str
    path_prefix: str | None = None
    executable: str | None = None
    argument_prefix: tuple[str, ...] = ()
    enabled: bool = True
    last_used_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def matches(self, request: ToolRequest, workspace_id: str) -> bool:
        if not self.enabled or self.workspace_id != workspace_id or self.mode != request.mode or self.tool != request.tool:
            return False
        if self.path_prefix is not None:
            normalized_prefix = _permission_path(self.path_prefix)
            paths = [_permission_path(path) for path in request_paths(request)]
            # A multi-file patch matches a path-scoped rule only when every path is in scope.
            if normalized_prefix is None or not paths or any(path is None for path in paths):
                return False
            if not all(path == normalized_prefix or path.startswith(normalized_prefix + "/") for path in paths):
                return False
        if self.executable is not None:
            if request.tool != "bash":
                return False
            try:
                tokens = shlex.split(str(request.arguments.get("command") or ""), posix=True)
            except ValueError:
                return False
            if not tokens or tokens[0] != self.executable or tokens[1 : 1 + len(self.argument_prefix)] != list(self.argument_prefix):
                return False
        return True


def request_paths(request: ToolRequest) -> list[str]:
    """Workspace paths a request names: one ``path``, or every ``patch`` operation path."""
    if request.tool == "patch":
        operations = request.arguments.get("operations")
        return [str(item.get("path") or "") for item in operations if isinstance(item, dict)] if isinstance(operations, list) else []
    return [str(request.arguments.get("path") or request.arguments.get("directory") or "")]


def rule_scope(request: ToolRequest) -> str | None:
    """The path prefix a saved or per-run rule should cover for this request.

    Single-path tools scope to their path. A patch scopes to the deepest directory shared by
    all of its paths, or to the whole workspace (``None``) when they share none.
    """
    paths = [path for path in request_paths(request) if path]
    if request.tool != "patch":
        return paths[0] if paths else None
    normalized = [_permission_path(path) for path in paths]
    if not normalized or any(path is None for path in normalized):
        return None
    parts = [path.split("/")[:-1] for path in normalized if path]
    shared: list[str] = []
    for segments in zip(*parts, strict=False):
        if len(set(segments)) != 1:
            break
        shared.append(segments[0])
    return "/".join(shared) or None


def resolve_permission(request: ToolRequest, workspace_id: str, rules: tuple[PermissionRule, ...] = ()) -> PermissionDecision:
    if request.mode == "plan" and request.tool in MUTATION_TOOLS:
        return PermissionDecision.DENY
    if request.tool not in READ_TOOLS | MUTATION_TOOLS:
        return PermissionDecision.DENY
    matching = [rule for rule in rules if rule.matches(request, workspace_id)]
    if PermissionDecision.DENY in {rule.decision for rule in matching}:
        return PermissionDecision.DENY
    if PermissionDecision.ASK in {rule.decision for rule in matching}:
        return PermissionDecision.ASK
    if matching:
        # Stable specificity: the narrowest (longest) normalized scope wins.
        matching.sort(key=lambda rule: len(_permission_path(rule.path_prefix or ".") or ""), reverse=True)
        return matching[0].decision
    return PermissionDecision.ALLOW_RUN if request.tool in READ_TOOLS else PermissionDecision.ASK


def _permission_path(value: str) -> str | None:
    """Return the canonical relative form used by permission scopes.

    Invalid paths do not match a rule; executor validation remains authoritative.
    Backslashes are accepted here solely to prevent scope ambiguity on Windows.
    """
    if not isinstance(value, str) or "\x00" in value:
        return None
    value = value.replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or value.startswith("//"):
        return None
    parts = [part for part in PurePosixPath(value).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts).casefold() if parts else "."
