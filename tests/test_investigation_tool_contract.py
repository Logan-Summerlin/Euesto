from __future__ import annotations

import asyncio
import json

import pytest

from server.agent.budgets import RunBudget
from server.agent.runtime import AgentRuntime
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS, AgentTurn
from shared.investigation import (
    MAX_FINDINGS,
    MAX_INSPECTED_PATHS,
    parse_inspected_paths,
    parse_investigation_report,
    reinspection_target,
)
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


def _investigation_schema() -> dict:
    return next(item for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == "investigate_repository")


def _request() -> AgentRunRequest:
    return AgentRunRequest(
        model="vendor/manager",
        messages=({"role": "user", "content": "Investigate this repository."},),
        mode="agent",
        workspace_id="workspace",
        investigation_model_id="xiaomi/mimo-v2.5",
    )


def _turn(content: str = "", calls: tuple[dict, ...] = ()) -> AgentTurn:
    return AgentTurn(content=content, tool_calls=calls, message={"role": "assistant", "content": content}, usage={})


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def execute(self, request):
        self.requests.append((request.tool, dict(request.arguments)))
        if request.tool == "grep":
            return ToolResult(request.request_id, True, output="src/app.py:12:def main():\nsrc/util.py:3:import os")
        return ToolResult(request.request_id, True, output=f"contents of {request.arguments.get('path')}")


def _run(monkeypatch, turns: list[AgentTurn], arguments: dict, executor=None):
    prompts: list[list[dict]] = []

    async def fake_agent_turn(model, messages, *args, **kwargs):
        prompts.append([dict(item) for item in messages])
        return turns.pop(0)

    events: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_agent_turn)
    executor = executor or RecordingExecutor()
    runtime = AgentRuntime(executor, object(), append)
    runtime._api_keys["run"] = "test-key"
    runtime._tool_result_bytes["run"] = 0
    messages: list[dict[str, object]] = []
    asyncio.run(runtime._investigate_repository("run", _request(), "parent", json.dumps(arguments), messages, RunBudget(10, 120, 1.0, 10, "test")))
    return json.loads(str(messages[-1]["content"])), prompts, events, executor


def test_investigation_repository_schema_accepts_query_and_inspected_paths() -> None:
    schema = _investigation_schema()["function"]
    assert schema["parameters"]["required"] == ["query"]
    assert set(schema["parameters"]["properties"]) == {"query", "inspected_paths"}
    assert schema["parameters"]["additionalProperties"] is False
    inspected = schema["parameters"]["properties"]["inspected_paths"]
    assert inspected["type"] == "array" and inspected["maxItems"] == MAX_INSPECTED_PATHS
    assert "path_hint" not in schema["parameters"]["properties"]
    assert "findings" in schema["description"]


def test_repository_investigation_uses_the_request_as_the_child_prompt(monkeypatch) -> None:
    query = "Find the entry point and explain how requests reach the agent runtime."
    result, prompts, _events, _executor = _run(monkeypatch, [_turn("investigation complete")], {"query": query})
    assert len(prompts) == 1
    assert query in prompts[0][-1]["content"]
    assert "already inspected" not in prompts[0][-1]["content"]
    assert '"findings"' in prompts[0][0]["content"]
    assert result["summary"] == "investigation complete"
    assert result["findings"] == [] and result["structured"] is False


def test_inspected_paths_are_not_reread_and_skips_are_logged(monkeypatch) -> None:
    turns = [
        _turn(calls=(
            _call("c1", "read", {"path": "src/app.py"}),
            _call("c2", "ls", {"path": "./src/"}),
            _call("c3", "read", {"path": "src/other.py"}),
            _call("c4", "grep", {"query": "main", "path": "src"}),
        )),
        _turn('{"summary": "done", "findings": []}'),
    ]
    result, prompts, events, executor = _run(monkeypatch, turns, {"query": "q", "inspected_paths": ["src/app.py", "src"]})

    # The executor never sees the already-inspected paths; new work still runs.
    assert executor.requests == [("read", {"path": "src/other.py"}), ("grep", {"query": "main", "path": "src"})]
    assert "src/app.py, src" in prompts[0][-1]["content"]
    skipped_calls = [payload for kind, payload in events if kind == "subagent.tool_call" and payload.get("skipped")]
    assert [item["request"]["arguments"]["path"] for item in skipped_calls] == ["src/app.py", "./src/"]
    skipped_results = [payload["result"] for kind, payload in events if kind == "subagent.tool_result" and payload.get("skipped")]
    assert {item["error_code"] for item in skipped_results} == {"investigation.already_inspected"}
    assert result["skipped_paths"] == ["src/app.py", "src"]
    assert result["files_examined"] == ["src", "src/other.py"]


def test_investigation_returns_structured_findings(monkeypatch) -> None:
    report = {
        "summary": "Requests enter through main().",
        "findings": [
            {"file": "src/app.py", "line": 12, "justification": "defines main()"},
            {"file": "src/util.py", "line": "3", "justification": "imports os"},
            {"file": "docs/never_read.md", "line": None, "justification": "from memory"},
            {"file": "../escape.py", "line": 1, "justification": "invalid path is dropped"},
            "not an object",
        ],
    }
    turns = [
        _turn(calls=(_call("c1", "read", {"path": "src/app.py"}), _call("c2", "grep", {"query": "import"}))),
        _turn("Here is my report.\n```json\n" + json.dumps(report) + "\n```"),
    ]
    result, _prompts, events, _executor = _run(monkeypatch, turns, {"query": "where do requests enter?"})

    assert result["structured"] is True
    assert result["summary"] == "Requests enter through main()."
    assert result["findings"] == [
        {"file": "src/app.py", "line": 12, "justification": "defines main()", "observed": True},
        {"file": "src/util.py", "line": 3, "justification": "imports os", "observed": True},
        {"file": "docs/never_read.md", "line": None, "justification": "from memory", "observed": False},
    ]
    completed = next(payload for kind, payload in events if kind == "subagent.completed")
    assert completed["findings"] == result["findings"]


def test_forced_synthesis_also_requests_structured_report(monkeypatch) -> None:
    monkeypatch.setattr("server.agent.runtime.INVESTIGATION_MAX_TOOL_CALLS", 2)
    turns = [
        _turn(calls=(_call("c1", "read", {"path": "a.py"}),)),
        _turn('{"summary": "a.py holds it", "findings": [{"file": "a.py", "line": 2, "justification": "x"}]}'),
    ]
    result, prompts, _events, _executor = _run(monkeypatch, turns, {"query": "q"})
    assert result["truncated"] is True and result["structured"] is True
    assert result["findings"][0]["observed"] is True
    assert '"findings"' in prompts[-1][-1]["content"]


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"query": "q", "inspected_paths": "src"}, "array"),
        ({"query": "q", "inspected_paths": ["/etc/passwd"]}, "relative"),
        ({"query": "q", "inspected_paths": ["../outside"]}, "relative"),
        ({"query": "q", "inspected_paths": [f"f{i}" for i in range(MAX_INSPECTED_PATHS + 1)]}, "at most"),
        ({"query": "q", "path_hint": ["src"]}, "Unknown"),
    ],
)
def test_malformed_investigation_arguments_fail_closed(monkeypatch, arguments, message) -> None:
    result, prompts, _events, executor = _run(monkeypatch, [], arguments)
    assert prompts == [] and executor.requests == []
    assert message in result["error"]
    assert result["fallback"]


def test_report_parser_handles_prose_bare_json_and_bounds() -> None:
    assert parse_investigation_report("Just prose.") == ("Just prose.", (), False)
    summary, findings, structured = parse_investigation_report('Preamble {"findings": [{"file": "a.py", "justification": "j"}]} tail')
    assert structured and summary == "Preamble  tail" and findings[0].line is None
    many = {"summary": "s", "findings": [{"file": f"f{i}.py", "line": i + 1, "justification": "j"} for i in range(MAX_FINDINGS + 10)]}
    assert len(parse_investigation_report(json.dumps(many))[1]) == MAX_FINDINGS


def test_reinspection_target_only_covers_read_and_ls() -> None:
    inspected = parse_inspected_paths(["src\\app.py", ".", "./docs/"])
    assert inspected == ("src/app.py", ".", "docs")
    assert reinspection_target("read", {"path": "./src/app.py"}, inspected) == "src/app.py"
    assert reinspection_target("ls", {}, inspected) == "."
    assert reinspection_target("ls", {"path": "docs"}, inspected) == "docs"
    assert reinspection_target("grep", {"query": "x", "path": "docs"}, inspected) is None
    assert reinspection_target("find", {"path": "docs"}, inspected) is None
    assert reinspection_target("read", {"path": "docs/guide.md"}, inspected) is None
