"""The three-position Agent approval policy: prompt, sandbox, auto (harness plan 4.1)."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent import runtime as runtime_module
from server.agent.approvals import ApprovalCoordinator, ApprovalTimeoutError
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import AgentTurn, tool_schemas
from shared.permissions import (
    APPROVAL_POLICIES,
    PermissionDecision,
    PermissionRule,
    apply_approval_policy,
    bash_can_prompt,
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


def _decision(
    tool: str, policy: str, rules: tuple[PermissionRule, ...] = (), *, network: bool = False
) -> PermissionDecision:
    request = ToolRequest("r", "run", tool, "agent", ARGUMENTS[tool])
    return apply_approval_policy(
        resolve_permission(request, "workspace", rules), request, policy, network=network
    )


def test_policies_are_ordered_and_sandbox_replaces_accept_edits() -> None:
    assert APPROVAL_POLICIES == ("prompt", "sandbox", "auto")
    assert "accept_edits" not in APPROVAL_POLICIES
    assert STAGED_EDIT_TOOLS < MUTATION_TOOLS and "bash" not in STAGED_EDIT_TOOLS


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("tool", sorted(ARGUMENTS))
def test_policy_matrix(tool: str, network: bool) -> None:
    prompt, sandbox, auto = (
        _decision(tool, policy, network=network) for policy in APPROVAL_POLICIES
    )
    if tool in READ_TOOLS:
        assert prompt == sandbox == auto == PermissionDecision.ALLOW_RUN
    elif tool in STAGED_EDIT_TOOLS:
        assert prompt == PermissionDecision.ASK
        assert sandbox == auto == PermissionDecision.ALLOW_RUN
    else:
        assert tool == "bash"
        assert prompt == PermissionDecision.ASK
        if network:
            # With registry access Bash asks under every tier unless a rule allows it.
            assert sandbox == auto == PermissionDecision.ASK
        else:
            assert sandbox == auto == PermissionDecision.ALLOW_RUN


def test_a_saved_rule_still_allows_bash_when_the_runner_has_network() -> None:
    rule = PermissionRule(
        "pytest",
        PermissionDecision.ALLOW_RULE,
        "workspace",
        "agent",
        "bash",
        executable="pytest",
    )
    for policy in APPROVAL_POLICIES:
        assert _decision("bash", policy, (rule,), network=True) == PermissionDecision.ALLOW_RULE


def test_bash_offers_description_only_where_it_can_prompt() -> None:
    assert bash_can_prompt("prompt", network=False)
    assert not bash_can_prompt("sandbox", network=False)
    assert not bash_can_prompt("auto", network=False)
    assert bash_can_prompt("sandbox", network=True) and bash_can_prompt("auto", network=True)

    def bash_properties(**options):
        schema = next(
            item
            for item in tool_schemas("agent", **options)
            if item["function"]["name"] == "bash"
        )
        return schema["function"]["parameters"]["properties"]

    assert "description" not in bash_properties()
    assert bash_properties(bash_description=True)["description"]["maxLength"] == 200


def test_deny_rules_and_plan_mode_win_under_every_policy() -> None:
    deny = PermissionRule(
        "deny", PermissionDecision.DENY, "workspace", "agent", "write", path_prefix="secrets"
    )
    bash_deny = PermissionRule(
        "no-curl", PermissionDecision.DENY, "workspace", "agent", "bash", executable="pytest"
    )
    request = ToolRequest(
        "r", "run", "write", "agent", {"path": "secrets/key.txt", "content": "x"}
    )
    for policy in APPROVAL_POLICIES:
        assert (
            apply_approval_policy(
                resolve_permission(request, "workspace", (deny,)), request, policy
            )
            == PermissionDecision.DENY
        )
        assert _decision("bash", policy, (bash_deny,)) == PermissionDecision.DENY
    plan_write = ToolRequest.__new__(
        ToolRequest
    )  # bypass validation to model a forged Plan mutation
    object.__setattr__(plan_write, "request_id", "r")
    object.__setattr__(plan_write, "run_id", "run")
    object.__setattr__(plan_write, "tool", "write")
    object.__setattr__(plan_write, "mode", "plan")
    object.__setattr__(plan_write, "arguments", {"path": "a", "content": "x"})
    assert (
        apply_approval_policy(
            resolve_permission(plan_write, "workspace"), plan_write, "sandbox"
        )
        == PermissionDecision.DENY
    )


def test_request_validation_accepts_sandbox_only_in_agent_mode() -> None:
    base = {
        "model": "m",
        "messages": [{"role": "user", "content": "go"}],
        "workspace_id": "workspace",
    }
    agent = AgentRunRequest.from_dict({**base, "mode": "agent", "approval_policy": "sandbox"})
    assert AgentRunRequest.from_dict(agent.to_dict()).approval_policy == "sandbox"
    with pytest.raises(ValueError, match="only in Agent mode"):
        AgentRunRequest.from_dict({**base, "mode": "plan", "approval_policy": "sandbox"})
    for retired in ("accept_edits", "edits"):
        with pytest.raises(ValueError, match="Unknown agent approval policy"):
            AgentRunRequest.from_dict({**base, "mode": "agent", "approval_policy": retired})
    registries = AgentRunRequest.from_dict(
        {**base, "mode": "agent", "network_mode": "registries"}
    )
    assert AgentRunRequest.from_dict(registries.to_dict()).network_mode == "registries"
    with pytest.raises(ValueError, match="only in Agent mode"):
        AgentRunRequest.from_dict({**base, "mode": "plan", "network_mode": "registries"})
    with pytest.raises(ValueError, match="Unknown agent network mode"):
        AgentRunRequest.from_dict({**base, "mode": "agent", "network_mode": "internet"})


class RecordingApprovals:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def wait(self, run_id, approval_id, request=None, *args, **kwargs):
        self.asked.append(request.tool if request is not None else "budget")
        return PermissionDecision.ALLOW_ONCE


class StagingExecutor:
    def __init__(self, *, network: bool = False) -> None:
        self.executed: list[str] = []
        self.arguments: list[dict] = []
        self.network = network

    async def status(self):
        environment = {"workspace_root": ".", "limits": {}}
        if self.network:
            environment["egress"] = {"enabled": True, "allowed_hosts": ["pypi.org"]}
        return {"workspace_id": "workspace", "environment": environment}

    async def execute(self, request):
        if request.arguments.get("path") == "AGENTS.md":
            return ToolResult(
                request.request_id, False, output="missing", error_code="path.missing"
            )
        self.executed.append(request.tool)
        self.arguments.append(dict(request.arguments))
        return ToolResult(
            request.request_id, True, output="ok", data={"workspace_status": {"staged": True}}
        )


def _run(
    policy: str, monkeypatch, *, network: bool = False, bash_arguments: dict | None = None
) -> tuple[RecordingApprovals, StagingExecutor, list[tuple[str, dict]], list[str]]:
    arguments = {**ARGUMENTS, "bash": bash_arguments or ARGUMENTS["bash"]}
    calls = tuple(
        {
            "id": f"c-{tool}",
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments[tool])},
        }
        for tool in ("read", "write", "edit", "apply_patch", "bash")
    )
    turns: list[int] = []
    contexts: list[str] = []

    async def fake_agent_turn(model, messages, *args, **kwargs):
        turns.append(1)
        contexts.extend(
            str(item.get("content"))
            for item in messages
            if item.get("role") in {"system", "user"}
        )
        if len(turns) == 1:
            return AgentTurn(
                "", calls, {"role": "assistant", "content": "", "tool_calls": list(calls)}, {}
            )
        return AgentTurn("done", (), {"role": "assistant", "content": "done"}, {})

    events: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    published: list[str] = []

    async def offer_publish(_run_id, approval_policy):
        published.append(approval_policy)

    monkeypatch.setattr(runtime_module, "agent_turn", fake_agent_turn)
    approvals, executor = RecordingApprovals(), StagingExecutor(network=network)
    runtime = AgentRuntime(executor, approvals, append)
    runtime._offer_publish = offer_publish
    request = AgentRunRequest(
        model="m",
        messages=({"role": "user", "content": "go"},),
        mode="agent",
        workspace_id="workspace",
        approval_policy=policy,
    )
    asyncio.run(runtime.run("run-1", request, "key"))
    assert events[-1][0] == "run.completed", events[-1]
    return approvals, executor, events, contexts + published


def test_sandbox_runs_edits_and_bash_without_prompts_while_offline(monkeypatch) -> None:
    approvals, executor, events, extra = _run("sandbox", monkeypatch)
    assert approvals.asked == []
    assert executor.executed == ["read", "write", "edit", "apply_patch", "bash"]
    assert not [payload for kind, payload in events if kind == "approval.required"]
    assert any("Approval tier: sandbox." in item for item in extra)
    # Publication is offered for separate approval, never auto-published under this tier.
    assert extra[-1] == "sandbox"


def test_sandbox_bash_asks_again_with_network_and_shows_the_full_context(
    monkeypatch,
) -> None:
    approvals, executor, events, extra = _run(
        "sandbox",
        monkeypatch,
        network=True,
        bash_arguments={
            "command": "pip install requests",
            "working_directory": "app",
            "timeout_seconds": 120,
            "description": "Install the HTTP client the tests need",
        },
    )
    assert approvals.asked == ["bash"]
    prompt = next(payload for kind, payload in events if kind == "approval.required")
    assert prompt["description"] == "Install the HTTP client the tests need"
    assert prompt["working_directory"] == "app" and prompt["timeout_seconds"] == 120
    assert prompt["network"] == "registries"
    # The description is for the prompt only; the executor never receives it.
    assert "description" not in executor.arguments[-1]
    assert any("allowlisted package registries" in item for item in extra)


def test_prompt_and_auto_behavior_is_unchanged(monkeypatch) -> None:
    approvals, _executor, _events, _extra = _run("prompt", monkeypatch)
    assert approvals.asked == ["write", "edit", "apply_patch", "bash"]
    approvals, _executor, _events, _extra = _run("auto", monkeypatch)
    assert approvals.asked == []


@pytest.mark.parametrize("policy", ["sandbox", "auto"])
def test_resume_never_carries_a_session_approval_tier(tmp_path, policy: str) -> None:
    from server.config import GatewayConfig
    from server.service import GatewayService

    async def scenario() -> str:
        socket = tmp_path / "executor.sock"
        socket.touch()
        service = GatewayService(
            GatewayConfig(
                "t" * 43,
                tmp_path / "gateway.sqlite3",
                executor_socket=socket,
                executor_token="e" * 43,
                workspace_id="workspace",
            )
        )
        service._client_openrouter_key = "key"
        captured: list[AgentRunRequest] = []

        async def fake_run_agent(_run_id, request, **_kwargs):
            captured.append(request)

        service._run_agent = fake_run_agent
        request = AgentRunRequest(
            model="m",
            messages=({"role": "user", "content": "go"},),
            mode="agent",
            workspace_id="workspace",
            approval_policy=policy,
        )
        service.journal.create_run("run-1", "agent", "2026-01-01T00:00:00Z")
        service.journal.save_run_snapshot(
            "run-1",
            request.to_dict(),
            [{"role": "user", "content": "go"}],
            [{"role": "user", "content": "go"}],
            {"iterations": 0},
            safe_to_resume=True,
            updated_at="2026-01-01T00:00:00Z",
        )
        service.journal.append(
            "run-1", "run.paused", "2026-01-01T00:00:01Z", {"resumable": True}
        )
        assert await service.resume_agent("run-1") is True
        await service._tasks["run-1"]
        await service.close()
        return captured[0].approval_policy

    assert asyncio.run(scenario()) == "prompt"


# Permission-rule resolution


def _read_request(path: str) -> ToolRequest:
    return ToolRequest("r", "run", "read", "agent", {"path": path})


def _read_rule(decision: PermissionDecision, prefix: str) -> PermissionRule:
    return PermissionRule("id-" + prefix, decision, "ws", "agent", "read", path_prefix=prefix)


def test_permission_matching_normalizes_separators_and_case() -> None:
    rules = (_read_rule(PermissionDecision.ALLOW_RULE, "src/project"),)
    assert (
        resolve_permission(_read_request(r"SRC\\PROJECT\\file.txt"), "ws", rules)
        == PermissionDecision.ALLOW_RULE
    )
    assert (
        resolve_permission(_read_request("src/project/../secret.txt"), "ws", rules)
        == PermissionDecision.ALLOW_RUN
    )


def test_permission_precedence_uses_most_specific_allow_scope() -> None:
    rules = (
        _read_rule(PermissionDecision.ALLOW_RUN, "src"),
        _read_rule(PermissionDecision.ALLOW_RULE, "src/private"),
    )
    assert (
        resolve_permission(_read_request("src/private/a.txt"), "ws", rules)
        == PermissionDecision.ALLOW_RULE
    )


def test_restrictive_permission_decisions_win() -> None:
    rules = (
        _read_rule(PermissionDecision.ALLOW_RUN, "src"),
        _read_rule(PermissionDecision.DENY, "src/private"),
    )
    assert (
        resolve_permission(_read_request("src/private/a.txt"), "ws", rules)
        == PermissionDecision.DENY
    )


# Approval waits


def test_approval_timeout_is_bounded_and_removed() -> None:
    coordinator = ApprovalCoordinator()
    with pytest.raises(ApprovalTimeoutError) as error:
        asyncio.run(coordinator.wait("run", "approval", timeout=0.01))
    assert error.value.approval_id == "approval"
    assert coordinator.get("run", "approval") is None


def test_approval_can_be_resolved_before_timeout() -> None:
    coordinator = ApprovalCoordinator()

    async def resolve_while_waiting() -> PermissionDecision:
        task = asyncio.create_task(coordinator.wait("run", "approval", timeout=1))
        await asyncio.sleep(0)  # let the wait register its pending approval
        assert coordinator.resolve("run", "approval", PermissionDecision.ALLOW_ONCE)
        return await task

    assert asyncio.run(resolve_while_waiting()) == PermissionDecision.ALLOW_ONCE
