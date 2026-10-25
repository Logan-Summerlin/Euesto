from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .coercion import relative_posix_path
from .tools import EXECUTOR_TOOLS, MUTATION_TOOLS, READ_TOOLS, ToolRequest

# Session approval policies for Agent runs, from most to least prompting:
#   prompt   every mutation or command asks unless a rule matches
#   sandbox  staged edits and Bash run without asking while the runner has no network; with
#            network (the egress overlay or the `registries` mode) Bash asks unless a rule matches
#   auto     every otherwise-valid call runs without asking; with network, Bash still asks
# Explicit DENY rules win under every policy, and publication stays separately approved
# (under auto, risk-flagged files still need confirmation).
APPROVAL_POLICIES = ("prompt", "sandbox", "auto")
# Decisions a saved rule may carry (harness plan 4.2). Rules cover executor tools only: the
# gateway's own tools never prompt (update_plan, investigate_repository) or always ask per URL.
RULE_DECISIONS = ("allow_rule", "ask", "deny")
MAX_RULE_COMMAND_CHARS = 500


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def matches(self, request: ToolRequest, workspace_id: str) -> bool:
        if (
            not self.enabled
            or self.workspace_id != workspace_id
            or self.mode != request.mode
            or self.tool != request.tool
        ):
            return False
        if self.path_prefix is not None:
            normalized_prefix = _permission_path(self.path_prefix)
            paths = [_permission_path(path) for path in request_paths(request)]
            # A multi-file apply_patch matches a path-scoped rule only when every path is in scope.
            if normalized_prefix is None or not paths or any(path is None for path in paths):
                return False
            if not all(
                path == normalized_prefix or path.startswith(normalized_prefix + "/")
                for path in paths
            ):
                return False
        if (
            request.tool == "bash"
            and self.executable is None
            and self.decision != PermissionDecision.DENY
        ):
            # An allow rule must name the command it allows; a broad one never matches.
            return False
        if self.executable is not None:
            if request.tool != "bash":
                return False
            command = str(request.arguments.get("command") or "")
            executable, arguments = bash_command_tokens(command)
            if (
                executable != self.executable
                or arguments[: len(self.argument_prefix)] != self.argument_prefix
            ):
                return False
            if self.decision != PermissionDecision.DENY and not simple_command(command):
                # `pytest && curl ...` is not `pytest`: an allow or ask rule covers only a
                # single simple command, never a list, pipeline, redirection, or substitution.
                return False
        return True


def new_permission_rule(
    rule_id: str, workspace_id: str, spec: dict[str, Any]
) -> PermissionRule:
    """A saved rule from the rules screen: a tool, an optional path prefix, and for Bash a
    command prefix such as ``pytest`` or ``npm test`` (the executable plus leading arguments).

    Raises ``ValueError`` for anything a rule cannot mean exactly.
    """
    allowed = {"workspace_id", "mode", "tool", "decision", "path_prefix", "command", "enabled"}
    if not isinstance(spec, dict) or set(spec) - allowed:
        raise ValueError("A rule takes tool, decision, mode, path_prefix, command, enabled")
    tool = spec.get("tool")
    if tool not in EXECUTOR_TOOLS:
        raise ValueError(f"tool must be one of: {', '.join(sorted(EXECUTOR_TOOLS))}")
    decision = spec.get("decision", "allow_rule")
    if decision not in RULE_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(RULE_DECISIONS)}")
    mode = spec.get("mode", "agent")
    if mode not in {"agent", "plan"}:
        raise ValueError("mode must be agent or plan")
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    path_prefix = spec.get("path_prefix")
    if path_prefix in ("", None):
        path_prefix = None
    elif not isinstance(path_prefix, str) or relative_posix_path(path_prefix) is None:
        raise ValueError("path_prefix must be a relative workspace path")
    else:
        path_prefix = relative_posix_path(path_prefix)
    command = spec.get("command")
    executable: str | None = None
    argument_prefix: tuple[str, ...] = ()
    if command not in ("", None):
        if tool != "bash":
            raise ValueError("Only bash rules take a command prefix")
        if not isinstance(command, str) or len(command) > MAX_RULE_COMMAND_CHARS:
            raise ValueError("command must be a short command prefix")
        executable, argument_prefix = bash_command_tokens(command)
        if executable is None:
            raise ValueError("command must name an executable")
    if tool == "bash" and executable is None and decision != "deny":
        raise ValueError("A bash rule that allows or asks must name a command prefix")
    if tool == "bash" and path_prefix is not None:
        raise ValueError("bash rules are scoped by command, not path")
    return PermissionRule(
        rule_id,
        PermissionDecision(decision),
        workspace_id,
        mode,
        tool,
        path_prefix,
        executable,
        argument_prefix,
        enabled,
    )


def bash_command_tokens(command: str) -> tuple[str | None, tuple[str, ...]]:
    """The executable and arguments a Bash rule is scoped to, as POSIX shell tokens.

    Both per-run and saved rules store exactly these, so a rule never widens to every command.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None, ()
    if not tokens:
        return None, ()
    return tokens[0], tuple(tokens[1:])


_SHELL_OPERATOR_CHARS = frozenset(";&|<>()")


def simple_command(command: str) -> bool:
    """Whether ``command`` is one simple command: no command lists, pipelines, redirections,
    subshells, background jobs, or line breaks outside quotes, and no command substitution
    anywhere (it expands inside double quotes too)."""
    if "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    return bool(tokens) and not any(
        token and set(token) <= _SHELL_OPERATOR_CHARS for token in tokens
    )


def request_paths(request: ToolRequest) -> list[str]:
    """Workspace paths a request names: one ``path``, or every ``apply_patch`` operation path."""
    if request.tool == "apply_patch":
        operations = request.arguments.get("operations")
        return (
            [str(item.get("path") or "") for item in operations if isinstance(item, dict)]
            if isinstance(operations, list)
            else []
        )
    return [str(request.arguments.get("path") or "")]


def rule_scope(request: ToolRequest) -> str | None:
    """The path prefix a saved or per-run rule should cover for this request.

    Single-path tools scope to their path. An apply_patch scopes to the deepest directory shared by
    all of its paths, or to the whole workspace (``None``) when they share none.
    """
    paths = [path for path in request_paths(request) if path]
    if request.tool != "apply_patch":
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


def resolve_permission(
    request: ToolRequest, workspace_id: str, rules: tuple[PermissionRule, ...] = ()
) -> PermissionDecision:
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
        matching.sort(
            key=lambda rule: len(_permission_path(rule.path_prefix or ".") or ""), reverse=True
        )
        return matching[0].decision
    return (
        PermissionDecision.ALLOW_RUN if request.tool in READ_TOOLS else PermissionDecision.ASK
    )


def apply_approval_policy(
    decision: PermissionDecision, request: ToolRequest, policy: str, *, network: bool = False
) -> PermissionDecision:
    """Resolve a rule-level ``ASK`` under the session approval policy.

    Only ``ASK`` changes: ``DENY`` (including Plan-mode mutation denial and explicit deny
    rules) and already-allowed decisions pass through untouched. ``network`` is true when the
    runner can reach the network; Bash then asks under every tier unless a rule allowed it.
    """
    if decision != PermissionDecision.ASK:
        return decision
    if request.mode != "agent":
        return PermissionDecision.ASK
    if request.tool == "bash" and network:
        return PermissionDecision.ASK
    if policy == "auto":
        return PermissionDecision.ALLOW_RUN
    if policy == "sandbox" and request.tool in MUTATION_TOOLS:
        return PermissionDecision.ALLOW_RUN
    return PermissionDecision.ASK


def bash_can_prompt(policy: str, *, network: bool) -> bool:
    """Whether Bash may ask the user in this session (it then offers ``description``)."""
    return policy == "prompt" or network


def _permission_path(value: str) -> str | None:
    """The case-insensitive relative form used by permission scopes; invalid paths never match."""
    normalized = relative_posix_path(value)
    return normalized.casefold() if normalized is not None else None
