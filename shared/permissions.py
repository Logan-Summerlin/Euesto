from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from enum import StrEnum
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
        path = str(request.arguments.get("path") or request.arguments.get("directory") or "")
        if self.path_prefix is not None and not (path == self.path_prefix or path.startswith(self.path_prefix.rstrip("/") + "/")):
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


def resolve_permission(request: ToolRequest, workspace_id: str, rules: tuple[PermissionRule, ...] = ()) -> PermissionDecision:
    if request.mode == "plan" and request.tool in MUTATION_TOOLS:
        return PermissionDecision.DENY
    if request.tool not in READ_TOOLS | MUTATION_TOOLS:
        return PermissionDecision.DENY
    matching = [rule.decision for rule in rules if rule.matches(request, workspace_id)]
    if PermissionDecision.DENY in matching:
        return PermissionDecision.DENY
    if PermissionDecision.ASK in matching:
        return PermissionDecision.ASK
    if matching:
        return matching[0]
    return PermissionDecision.ALLOW_RUN if request.tool in READ_TOOLS else PermissionDecision.ASK
