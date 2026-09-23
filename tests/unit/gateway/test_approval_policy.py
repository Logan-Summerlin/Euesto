"""The three-position Agent approval policy: prompt, accept_edits, auto."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.agent import runtime as runtime_module
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn
from shared.permissions import (
    APPROVAL_POLICIES,
    PermissionDecision,
    PermissionRule,
    apply_approval_policy,
    resolve_permission,
)
from shared.requests import AgentRunRequest
from shared.tools import MUTATION_TOOLS, READ_TOOLS, STAGED_EDIT_TOOLS, ToolRequest, ToolResult

ARGUMENTS = {
    "read": {"path": "a.txt"},
    "grep": {"query": "x"},
    "find": {},
    "ls": {},
    "status": {},
    "write": {"path": "a.txt", "content": "x"},
    "edit": {"path": "a.txt", "old_str": "x", "new_str": "y"},
    "apply_patch": {"operations": [{"operation": "write", "path": "a.txt", "content": "x"}]},
    "bash": {"command": "pytest -q"},
}


def _decision(tool: str, policy: str, rules: tuple[PermissionRule, ...] = ()) -> PermissionDecision:
    request = ToolRequest("r", "run", tool, "agent", ARGUMENTS[tool])
    return apply_approval_policy(resolve_permission(request, "workspace", rules), request, policy)


def test_policies_are_ordered_and_edit_tier_is_staged_file_edits_only() -> None:
    assert APPROVAL_POLICIES == ("prompt", "accept_edits", "auto")
    assert STAGED_EDIT_TOOLS == {"write", "edit", "apply_patch"}
    assert STAGED_EDIT_TOOLS < MUTATION_TOOLS and "bash" not in STAGED_EDIT_TOOLS


@pytest.mark.parametrize("tool", sorted(ARGUMENTS))
def test_policy_matrix(tool: str) -> None:
    prompt, accept_edits, auto = (_decision(tool, policy) for policy in APPROVAL_POLICIES)
    if tool in READ_TOOLS:
        assert prompt == accept_edits == auto == PermissionDecision.ALLOW_RUN
    elif tool in STAGED_EDIT_TOOLS:
        assert prompt == PermissionDecision.ASK
        assert accept_edits == auto == PermissionDecision.ALLOW_RUN
    else:
        assert tool == "bash"
        assert prompt == accept_edits == PermissionDecision.ASK
        assert auto == PermissionDecision.ALLOW_RUN


def test_deny_rules_and_plan_mode_win_under_every_policy() -> None:
    deny = PermissionRule("deny", PermissionDecision.DENY, "workspace", "agent", "write", path_prefix="secrets")
    request = ToolRequest("r", "run", "write", "agent", {"path": "secrets/key.txt", "content": "x"})
    for policy in APPROVAL_POLICIES:
        assert apply_approval_policy(resolve_permission(request, "workspace", (deny,)), request, policy) == PermissionDecision.DENY
    plan_write = ToolRequest.__new__(ToolRequest)  # bypass validation to model a forged Plan mutation
    object.__setattr__(plan_write, "request_id", "r")
    object.__setattr__(plan_write, "run_id", "run")
    object.__setattr__(plan_write, "tool", "write")
    object.__setattr__(plan_write, "mode", "plan")
    object.__setattr__(plan_write, "arguments", {"path": "a", "content": "x"})
    assert apply_approval_policy(resolve_permission(plan_write, "workspace"), plan_write, "accept_edits") == PermissionDecision.DENY


def test_request_validation_accepts_the_middle_tier_only_in_agent_mode() -> None:
    base = {"model": "m", "messages": [{"role": "user", "content": "go"}], "workspace_id": "workspace"}
    agent = AgentRunRequest.from_dict({**base, "mode": "agent", "approval_policy": "accept_edits"})
    assert AgentRunRequest.from_dict(agent.to_dict()).approval_policy == "accept_edits"
    with pytest.raises(ValueError, match="only in Agent mode"):
        AgentRunRequest.from_dict({**base, "mode": "plan", "approval_policy": "accept_edits"})
    with pytest.raises(ValueError, match="Unknown agent approval policy"):
        AgentRunRequest.from_dict({**base, "mode": "agent", "approval_policy": "edits"})


class RecordingApprovals:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def wait(self, run_id, approval_id, request=None, *args, **kwargs):
        self.asked.append(request.tool if request is not None else "budget")
        return PermissionDecision.ALLOW_ONCE


class StagingExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def status(self):
        return {"workspace_id": "workspace", "environment": {"workspace_root": ".", "limits": {}}}

    async def execute(self, request):
        if request.arguments.get("path") == "AGENTS.md":
            return ToolResult(request.request_id, False, output="missing", error_code="path.missing")
        self.executed.append(request.tool)
        return ToolResult(request.request_id, True, output="ok", data={"workspace_status": {"staged": True}})


def _run(policy: str, monkeypatch) -> tuple[RecordingApprovals, StagingExecutor, list[tuple[str, dict]], list[str]]:
    calls = tuple({"id": f"c-{tool}", "type": "function", "function": {"name": tool, "arguments": json.dumps(ARGUMENTS[tool])}} for tool in ("read", "write", "edit", "apply_patch", "bash"))
    turns: list[int] = []
    contexts: list[str] = []

    async def fake_agent_turn(model, messages, *args, **kwargs):
        turns.append(1)
        contexts.extend(str(item.get("content")) for item in messages if item.get("role") == "system")
        if len(turns) == 1:
            return AgentTurn("", calls, {"role": "assistant", "content": "", "tool_calls": list(calls)}, {})
        return AgentTurn("done", (), {"role": "assistant", "content": "done"}, {})

    events: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    published: list[str] = []

    async def offer_publish(_run_id, approval_policy):
        published.append(approval_policy)

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    approvals, executor = RecordingApprovals(), StagingExecutor()
    runtime = AgentRuntime(executor, approvals, append)
    runtime._offer_publish = offer_publish
    request = AgentRunRequest(model="m", messages=({"role": "user", "content": "go"},), mode="agent", workspace_id="workspace", approval_policy=policy)
    asyncio.run(runtime.run("run-1", request, "key"))
    assert events[-1][0] == "run.completed", events[-1]
    return approvals, executor, events, contexts + published


def test_accept_edits_runs_staged_edits_without_prompts_but_still_asks_for_bash(monkeypatch) -> None:
    approvals, executor, events, extra = _run("accept_edits", monkeypatch)
    assert approvals.asked == ["bash"]
    assert executor.executed == ["read", "write", "edit", "apply_patch", "bash"]
    required = [payload["tool"] for kind, payload in events if kind == "approval.required"]
    assert required == ["bash"]
    assert any("accept_edits: write, edit, and apply_patch run without prompts" in item for item in extra)
    # Publication is offered for separate approval, never auto-published under this tier.
    assert extra[-1] == "accept_edits"


def test_prompt_and_auto_behavior_is_unchanged(monkeypatch) -> None:
    approvals, _executor, _events, _extra = _run("prompt", monkeypatch)
    assert approvals.asked == ["write", "edit", "apply_patch", "bash"]
    approvals, _executor, _events, _extra = _run("auto", monkeypatch)
    assert approvals.asked == []


@pytest.mark.parametrize("policy", ["accept_edits", "auto"])
def test_resume_never_carries_a_session_approval_tier(tmp_path, policy: str) -> None:
    from server.config import GatewayConfig
    from server.service import GatewayService

    async def scenario() -> str:
        socket = tmp_path / "executor.sock"
        socket.touch()
        service = GatewayService(GatewayConfig("t" * 43, tmp_path / "gateway.sqlite3", executor_socket=socket, executor_token="e" * 43, workspace_id="workspace"))
        service._client_openrouter_key = "key"
        captured: list[AgentRunRequest] = []

        async def fake_run_agent(_run_id, request, **_kwargs):
            captured.append(request)

        service._run_agent = fake_run_agent
        request = AgentRunRequest(model="m", messages=({"role": "user", "content": "go"},), mode="agent", workspace_id="workspace", approval_policy=policy)
        service.journal.create_run("run-1", "agent", "2026-01-01T00:00:00Z")
        service.journal.save_run_snapshot("run-1", request.to_dict(), [{"role": "user", "content": "go"}], [{"role": "user", "content": "go"}], {"iterations": 0}, safe_to_resume=True, updated_at="2026-01-01T00:00:00Z")
        service.journal.append("run-1", "run.paused", "2026-01-01T00:00:01Z", {"resumable": True})
        assert await service.resume_agent("run-1") is True
        await service._tasks["run-1"]
        await service.close()
        return captured[0].approval_policy

    assert asyncio.run(scenario()) == "prompt"
