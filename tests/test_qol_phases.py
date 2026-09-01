from __future__ import annotations

import asyncio

import pytest

from executor.errors import safe_message
from server.agent.approvals import ApprovalCoordinator, ApprovalTimeoutError
from shared.permissions import PermissionDecision, PermissionRule, resolve_permission
from shared.tools import ToolRequest


def request(path: str) -> ToolRequest:
    return ToolRequest("r", "run", "read", "agent", {"path": path})


def rule(decision: PermissionDecision, prefix: str) -> PermissionRule:
    return PermissionRule("id-" + prefix, decision, "ws", "agent", "read", path_prefix=prefix)


def test_permission_matching_normalizes_separators_and_case() -> None:
    rules = (rule(PermissionDecision.ALLOW_RULE, "src/project"),)
    assert resolve_permission(request(r"SRC\\PROJECT\\file.txt"), "ws", rules) == PermissionDecision.ALLOW_RULE
    assert resolve_permission(request("src/project/../secret.txt"), "ws", rules) == PermissionDecision.ALLOW_RUN


def test_permission_precedence_uses_most_specific_allow_scope() -> None:
    rules = (rule(PermissionDecision.ALLOW_RUN, "src"), rule(PermissionDecision.ALLOW_RULE, "src/private"))
    assert resolve_permission(request("src/private/a.txt"), "ws", rules) == PermissionDecision.ALLOW_RULE


def test_restrictive_permission_decisions_win() -> None:
    rules = (rule(PermissionDecision.ALLOW_RUN, "src"), rule(PermissionDecision.DENY, "src/private"))
    assert resolve_permission(request("src/private/a.txt"), "ws", rules) == PermissionDecision.DENY


@pytest.mark.asyncio
async def test_approval_timeout_is_bounded_and_removed() -> None:
    coordinator = ApprovalCoordinator()
    with pytest.raises(ApprovalTimeoutError) as error:
        await coordinator.wait("run", "approval", timeout=0.01)
    assert error.value.approval_id == "approval"
    assert coordinator.get("run", "approval") is None


@pytest.mark.asyncio
async def test_approval_can_be_resolved_before_timeout() -> None:
    coordinator = ApprovalCoordinator()
    task = asyncio.create_task(coordinator.wait("run", "approval", timeout=1))
    await asyncio.sleep(0)
    assert coordinator.resolve("run", "approval", PermissionDecision.ALLOW_ONCE)
    assert await task == PermissionDecision.ALLOW_ONCE


def test_safe_message_redacts_drive_unc_and_multiple_paths() -> None:
    message = r"C:\\Users\\alice\\secret.txt and /srv/work/file.py plus \\server\\share\\private.txt"
    safe = safe_message(message)
    assert "C:\\" not in safe and "/srv/" not in safe
    assert "<workspace-path>" in safe
    assert "alice" not in safe
