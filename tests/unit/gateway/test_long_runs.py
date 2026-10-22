"""Scripted-provider scenarios for long runs (harness plan Phase 3).

``update_plan`` (3.2), budget wind-down and continuation (3.3), loop resilience for smaller
models (3.5), and read-only delegation from Plan mode (3.6).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent import budgets as budgets_module
from server.agent.plan import MAX_PLAN_STEPS, PlanError, PlanState
from server.agent.runtime import AgentRuntime
from server.config import GatewayConfig
from server.service import GatewayService
from tests.unit.gateway.test_harness_loop import (
    AllowApprovals,
    ScriptedExecutor,
    call,
    final_turn,
    kinds,
    make_request,
    run,
    tool_contents,
    tools_turn,
)


def plan_call(call_id: str, steps: list[dict], note: str | None = None) -> dict:
    arguments: dict = {"steps": steps}
    if note is not None:
        arguments["note"] = note
    return call(call_id, "update_plan", arguments)


def raw_call(call_id: str, name: str, raw_arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": raw_arguments},
    }


# -- 3.2 update_plan -------------------------------------------------------------------------


def test_the_first_call_sets_the_plan_and_later_calls_update_steps_by_id() -> None:
    plan = PlanState()
    first = plan.apply(
        {
            "steps": [
                {"id": 1, "title": "Read the parser"},
                {"id": 2, "title": "Fix the bug"},
                {"id": 3, "title": "Run the tests"},
            ]
        }
    )
    assert first == "plan: 0/3 done"
    line = plan.apply(
        {"steps": [{"id": "1", "status": "done"}, {"id": 2, "status": "in_progress"}]}
    )
    assert line == "plan: 1/3 done, step 2 in progress"
    assert "\n" not in line
    assert [step.title for step in plan.steps] == [
        "Read the parser",
        "Fix the bug",
        "Run the tests",
    ]
    plan.apply(
        {"steps": [{"id": 4, "title": "Update the changelog"}], "note": "tests are slow"}
    )
    assert len(plan.steps) == 4 and plan.note == "tests are slow"
    assert "[x] 1. Read the parser" in plan.render() and "[>] 2. Fix the bug" in plan.render()


def test_a_finished_plan_starts_fresh_when_only_new_steps_are_sent() -> None:
    plan = PlanState()
    plan.apply({"steps": [{"id": "a", "title": "One", "status": "done"}]})
    plan.apply({"steps": [{"id": "b", "title": "Next task"}]})
    assert [step.id for step in plan.steps] == ["b"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"steps": []}, "1 to 20"),
        ({"steps": [{"id": index, "title": "t"} for index in range(21)]}, "1 to 20"),
        ({"steps": [{"id": 1, "title": "x" * 121}]}, "at most 120"),
        ({"steps": [{"id": 1}]}, "give it a title"),
        ({"steps": [{"id": 1, "title": "t", "status": "started"}]}, "status must be one of"),
        ({"steps": [{"id": 1, "title": "t"}], "note": "n" * 201}, "at most 200"),
        ({"steps": [{"id": 1, "title": "t"}, {"id": "1", "title": "u"}]}, "appears twice"),
        ({"steps": [{"id": True, "title": "t"}]}, "string or an integer"),
        ({"steps": [{"id": 1, "title": "t", "owner": "me"}]}, "unknown step field"),
    ],
)
def test_plan_bounds_hold(arguments: dict, message: str) -> None:
    plan = PlanState()
    plan.apply({"steps": [{"id": "keep", "title": "Existing"}]})
    with pytest.raises(PlanError, match=message):
        plan.apply(arguments)
    assert [step.id for step in plan.steps] == ["keep"]


def test_a_plan_never_grows_past_its_step_limit() -> None:
    plan = PlanState()
    plan.apply({"steps": [{"id": index, "title": f"s{index}"} for index in range(20)]})
    with pytest.raises(PlanError, match="at most 20 steps"):
        plan.apply({"steps": [{"id": "extra", "title": "one more"}]})
    assert len(plan.steps) == MAX_PLAN_STEPS


def test_update_plan_is_gateway_local_journaled_and_saved_per_session(monkeypatch) -> None:
    executor = ScriptedExecutor()
    saved: dict = {}
    runtime = AgentRuntime(
        executor,
        AllowApprovals(),
        None,
        plan_saver=lambda key, plan: saved.update({key: plan}),
    )
    provider, events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(
                plan_call("p1", [{"id": 1, "title": "Inspect"}, {"id": 2, "title": "Fix"}])
            ),
            tools_turn(plan_call("p2", [{"id": 1, "status": "done"}])),
            final_turn(),
        ],
        executor=executor,
        runtime=runtime,
    )
    assert executor.requests == []  # never reaches the executor
    assert "approval.required" not in kinds(events)
    updates = [payload for kind, payload in events if kind == "plan.updated"]
    assert [item["summary"] for item in updates] == ["plan: 0/2 done", "plan: 1/2 done"]
    assert tool_contents(provider.payloads[-1])[-2:] == ["plan: 0/2 done", "plan: 1/2 done"]
    assert saved["session-1"]["steps"][0] == {"id": "1", "title": "Inspect", "status": "done"}
    completed = next(payload for kind, payload in events if kind == "run.completed")
    assert completed["plan"]["steps"][1]["title"] == "Fix"


def test_the_plan_survives_a_resume_and_is_offered_in_plan_mode(monkeypatch) -> None:
    stored = {"steps": [{"id": "1", "title": "Inspect", "status": "in_progress"}], "note": ""}
    runtime = AgentRuntime(
        ScriptedExecutor(), AllowApprovals(), None, plan_loader=lambda _key: stored
    )
    provider, events, _saved, _ = run(
        monkeypatch,
        [tools_turn(plan_call("p1", [{"id": 1, "status": "done"}])), final_turn()],
        runtime=runtime,
        request=make_request(mode="plan", approval_policy="prompt"),
        resumed=True,
    )
    names = {item["function"]["name"] for item in provider.payloads[0]["tools"]}
    assert {"update_plan", "investigate_repository"} <= names and "write" not in names
    started = next(payload for kind, payload in events if kind == "run.resumed")
    assert started["plan"]["steps"][0]["status"] == "in_progress"
    assert tool_contents(provider.payloads[-1])[-1] == "plan: 1/1 done"


# -- 3.5 loop resilience ------------------------------------------------------------------


def test_malformed_json_and_schema_violations_get_one_line_corrections(monkeypatch) -> None:
    executor = ScriptedExecutor()
    provider, _events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(
                raw_call("bad-json", "read", '{"path": "a.py"'),
                call("bad-type", "read", {"path": "a.py", "start_line": "ten"}),
                call("missing", "edit", {"path": "a.py", "old_str": "x"}),
                call("extra", "grep", {"query": "x", "pattern": "y"}),
            ),
            final_turn(),
        ],
        executor=executor,
    )
    results = tool_contents(provider.payloads[-1])
    assert results[0].startswith(
        "error tool.invalid_arguments: read arguments are not valid JSON"
    )
    assert "start_line must be an integer, got a string" in results[1]
    assert "missing required field new_str" in results[2]
    assert "unknown field pattern; allowed:" in results[3]
    assert all(len(text.splitlines()) == 1 for text in results)
    assert executor.requests == []


def test_an_unknown_tool_lists_the_tools_of_the_mode(monkeypatch) -> None:
    provider, _events, _saved, _ = run(
        monkeypatch,
        [tools_turn(call("x", "write", {"path": "a", "content": "b"})), final_turn()],
        request=make_request(mode="plan", approval_policy="prompt"),
    )
    text = tool_contents(provider.payloads[-1])[0]
    assert text.startswith("error tool.unknown: no tool named write in plan mode; available:")
    assert "investigate_repository, ls, read, update_plan" in text


def test_the_third_identical_failure_asks_for_a_change_of_approach(monkeypatch) -> None:
    turns = [tools_turn(call(f"c{index}", "read", {"path": 5})) for index in range(3)]
    provider, events, _saved, _ = run(monkeypatch, [*turns, final_turn()])
    results = tool_contents(provider.payloads[-1])
    assert "change your approach" not in results[1]
    assert "failed the same way 3 times" in results[2]
    assert ("loop.note", {"kind": "repeated_failure", "tool": "read", "count": 3}) in events


def test_an_empty_reply_is_re_prompted_once_then_the_run_pauses(monkeypatch) -> None:
    empty = final_turn("")
    provider, events, _saved, _ = run(monkeypatch, [empty, final_turn("answer")])
    from server.agent.runtime import EMPTY_REPLY_NOTE

    assert provider.payloads[1]["messages"][-1]["content"] == EMPTY_REPLY_NOTE
    assert kinds(events)[-1] == "run.completed"
    _provider, events, saved, _ = run(monkeypatch, [final_turn(""), final_turn("  ")])
    paused = events[-1]
    assert paused[0] == "run.paused" and paused[1]["reason"] == "empty_reply"
    assert paused[1]["resumable"] is True and saved["snapshot"][4] is True


def test_turns_without_progress_end_in_a_wind_down(monkeypatch) -> None:
    repeated = [tools_turn(call(f"c{index}", "ls", {})) for index in range(5)]
    provider, events, _saved, _ = run(
        monkeypatch, [*repeated, final_turn("I was going in circles.")]
    )
    paused = events[-1][1]
    assert events[-1][0] == "run.paused" and paused["reason"] == "no_progress"
    assert paused["continuation"] == "resume"
    # The first ls was new output; the next four repeated it, so the fifth turn winds down.
    assert len(provider.payloads) == 6
    assert provider.payloads[-1]["tool_choice"] == "none"
    assert ("model.delta", {"text": "I was going in circles."}) in events


# -- 3.3 wind-down and continuation -------------------------------------------------------


def test_a_warning_note_is_added_once_at_85_percent(monkeypatch) -> None:
    request = make_request()
    budget_state = {"iterations": 509, "elapsed_seconds": 0}
    provider, events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(call("a", "read", {"path": "a.py"})),
            tools_turn(call("b", "read", {"path": "b.py"})),
            final_turn(),
        ],
        request=request,
        budget_state=budget_state,
    )
    notes = [payload for kind, payload in events if kind == "loop.note"]
    assert notes == [{"kind": "budget_warning", "budget": "iteration", "percent": 85}]
    warned = [
        item
        for item in provider.payloads[-1]["messages"]
        if "budget is 85% used" in str(item.get("content"))
    ]
    assert len(warned) == 1


def test_exhaustion_produces_a_summary_and_a_resumable_budget_pause(monkeypatch) -> None:
    stored = {"steps": [{"id": "1", "title": "Fix the parser", "status": "in_progress"}]}
    runtime = AgentRuntime(
        ScriptedExecutor(), AllowApprovals(), None, plan_loader=lambda _key: stored
    )
    provider, events, saved, _ = run(
        monkeypatch,
        [
            tools_turn(call("a", "read", {"path": "a.py"})),
            final_turn("Parser half fixed; staging is consistent. Next: run the tests."),
        ],
        runtime=runtime,
        budget_state={"iterations": 598},
    )
    summary_request = provider.payloads[-1]
    assert summary_request["tool_choice"] == "none"
    # The summary turn keeps the session's tool definitions, so the prefix stays cached.
    assert summary_request["tools"] == provider.payloads[0]["tools"]
    assert "WIND-DOWN REQUEST" in summary_request["messages"][-1]["content"]
    assert "[>] 1. Fix the parser" in summary_request["messages"][-1]["content"]
    paused = events[-1]
    assert paused[0] == "run.paused"
    assert paused[1]["reason"] == "budget" and paused[1]["budget_name"] == "iteration"
    assert paused[1]["continuation"] == "new_budget" and paused[1]["resumable"] is True
    delta = next(payload["text"] for kind, payload in events if kind == "model.delta")
    assert delta.startswith("Parser half fixed") and "Plan:\n[>] 1. Fix the parser" in delta
    assert saved["snapshot"][4] is True
    assert saved["snapshot"][1][-1]["content"].startswith("Parser half fixed")


def test_cut_short_tool_calls_get_results_before_the_summary(monkeypatch) -> None:
    provider, events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(
                call("w", "write", {"path": "a.py", "content": "x"}),
                call("b", "bash", {"command": "pytest -q"}),
            ),
            final_turn("Stopped."),
        ],
        budget_state={"tool_calls": 899},
    )
    results = tool_contents(provider.payloads[-1])
    # One tool call remained: the write ran, and the bash call was cut off with a result.
    assert results[0].startswith("write a.py: rewritten")
    assert results[1] == "error budget.exhausted: not executed; the run's budget is used up."
    assert events[-1][1]["budget_name"] == "tool-call"


def test_a_failed_summary_turn_still_pauses_with_a_written_summary(monkeypatch) -> None:
    from server.openrouter.errors import ProviderError

    failure = ProviderError("provider.timeout", "slow", retryable=False)
    _provider, events, _saved, _ = run(
        monkeypatch,
        [failure],
        budget_state={"iterations": 599},
    )
    assert events[-1][0] == "run.paused" and events[-1][1]["reason"] == "budget"
    text = next(payload["text"] for kind, payload in events if kind == "model.delta")
    assert text.startswith("I stopped because the run's turn budget is used up.")


def _service(tmp_path) -> GatewayService:
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
    return service


def test_a_budget_pause_continues_only_with_a_new_budget(tmp_path) -> None:
    from server.service import GatewayServiceError

    async def scenario() -> list[dict]:
        service = _service(tmp_path)
        captured: list[dict] = []

        async def fake_run_agent(_run_id, request, **kwargs):
            captured.append(kwargs)

        service._run_agent = fake_run_agent
        request = make_request(approval_policy="sandbox")
        service.journal.create_run("run-1", "agent", "2026-01-01T00:00:00Z")
        service.journal.save_run_snapshot(
            "run-1",
            request.to_dict(),
            [{"role": "user", "content": "go"}],
            [{"role": "user", "content": "go"}],
            {"iterations": 600},
            safe_to_resume=True,
            updated_at="2026-01-01T00:00:00Z",
        )
        service.journal.append(
            "run-1",
            "run.paused",
            "2026-01-01T00:00:01Z",
            {"reason": "budget", "resumable": True},
        )
        with pytest.raises(GatewayServiceError, match="another budget"):
            await service.resume_agent("run-1")
        assert await service.resume_agent("run-1", new_budget=True) is True
        await service._tasks["run-1"]
        await service.close()
        return captured

    captured = asyncio.run(scenario())
    assert captured[0]["budget_state"] is None and captured[0]["continuation"] is True


def test_an_ordinary_pause_cannot_take_a_new_budget(tmp_path) -> None:
    from server.service import GatewayServiceError

    async def scenario() -> None:
        service = _service(tmp_path)
        request = make_request()
        service.journal.create_run("run-1", "agent", "2026-01-01T00:00:00Z")
        service.journal.save_run_snapshot(
            "run-1",
            request.to_dict(),
            [{"role": "user", "content": "go"}],
            [{"role": "user", "content": "go"}],
            {},
            safe_to_resume=True,
            updated_at="2026-01-01T00:00:00Z",
        )
        service.journal.append(
            "run-1", "run.paused", "2026-01-01T00:00:01Z", {"reason": "user.requested"}
        )
        try:
            with pytest.raises(GatewayServiceError, match="Only a run that used its budget"):
                await service.resume_agent("run-1", new_budget=True)
        finally:
            await service.close()

    asyncio.run(scenario())


def test_a_continuation_restores_the_plan_and_the_handoff(monkeypatch) -> None:
    stored = {"steps": [{"id": "1", "title": "Fix the parser", "status": "in_progress"}]}
    first = AgentRuntime(
        ScriptedExecutor(), AllowApprovals(), None, plan_loader=lambda _key: stored
    )
    _provider, _events, saved, _ = run(
        monkeypatch,
        [tools_turn(call("a", "read", {"path": "a.py"})), final_turn("Half done.")],
        runtime=first,
        budget_state={"iterations": 598},
    )
    messages = saved["snapshot"][1]
    second = AgentRuntime(
        ScriptedExecutor(), AllowApprovals(), None, plan_loader=lambda _key: stored
    )
    provider, events, _saved, _ = run(
        monkeypatch,
        [final_turn("Finished.")],
        runtime=second,
        initial_messages=messages,
        resumed=True,
        continuation=True,
    )
    sent = provider.payloads[0]["messages"]
    assert any(item.get("content") == "Half done." for item in sent)
    assert "another budget" not in sent[-1]["content"]
    assert "the user approved a new one; continue from your summary" in sent[-1]["content"]
    assert kinds(events)[-1] == "run.completed"
    resumed = next(payload for kind, payload in events if kind == "run.resumed")
    assert resumed["plan"]["steps"][0]["title"] == "Fix the parser"
    assert resumed["budget"]["iterations"] == 0


def test_wall_time_reserve_funds_the_summary(monkeypatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(budgets_module.time, "monotonic", lambda: clock["now"])
    provider = None

    def advance(count: int) -> None:
        if count == 1:
            clock["now"] += 1_750  # 50s of 1,800 remain: below the 60s reserve

    turns = [tools_turn(call("a", "read", {"path": "a.py"})), final_turn("Summary.")]
    from server.agent import runtime as runtime_module
    from tests.unit.gateway.test_harness_loop import Provider

    provider = Provider(turns)
    provider.on_call = advance
    monkeypatch.setattr(runtime_module, "agent_turn", provider)
    events: list = []

    async def append(_run_id, kind, payload):
        events.append((kind, payload))

    runtime = AgentRuntime(ScriptedExecutor(), AllowApprovals(), append)
    asyncio.run(runtime.run("run-1", make_request(), "key"))
    assert events[-1][1]["budget_name"] == "wall-time"
    assert provider.payloads[-1]["tool_choice"] == "none"


# -- 3.6 investigation from Plan ----------------------------------------------------------


def test_plan_mode_can_delegate_an_investigation(monkeypatch) -> None:
    from server.agent import investigation as investigation_module
    from server.openrouter.agent import AgentTurn

    nested: list[dict] = []

    async def nested_turn(model, messages, api_key, mode, *args, **kwargs):
        nested.append(kwargs)
        return AgentTurn(
            "The parser lives in src/parser.py.",
            (),
            {"role": "assistant", "content": "The parser lives in src/parser.py."},
            {"total_tokens": 5},
        )

    monkeypatch.setattr(investigation_module, "agent_turn", nested_turn)
    provider, events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(call("i", "investigate_repository", {"query": "where is the parser?"})),
            final_turn(),
        ],
        request=make_request(mode="plan", approval_policy="prompt"),
    )
    assert "subagent.completed" in kinds(events)
    assert nested[0]["offered_tools"] == frozenset({"read", "grep", "find", "ls", "status"})
    result = json.loads(tool_contents(provider.payloads[-1])[0])
    assert result["summary"] == "The parser lives in src/parser.py."
