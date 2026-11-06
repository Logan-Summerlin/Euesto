"""Scripted-provider scenarios for the parent agent loop (harness plan Phases 0 and 1)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from server.agent import runtime as runtime_module
from server.agent.context import estimate_message_tokens
from server.agent.prompts import PROMPT_VERSION, agent_prompt
from server.agent.runtime import AgentRuntime, recent_exchanges
from server.openrouter.agent import AgentTurn, agent_payload, serialize_payload
from server.openrouter.errors import REASONING_EFFORT_REJECTED, ProviderError
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


class ScriptedExecutor:
    """Answers every tool with a deterministic result; reads return large text on demand."""

    def __init__(self, read_bytes: int = 100, *, file_hash: str = "a" * 64) -> None:
        self.read_bytes = read_bytes
        self.file_hash = file_hash
        self.requests = []

    async def status(self):
        return {
            "workspace_id": "workspace",
            "environment": {
                "workspace_root": ".",
                "limits": {"max_command_seconds": 900},
                "developer_executables": ["python3", "git"],
            },
        }

    async def workspace_snapshot(self):
        return {
            "agents_md": "Run the fast checks.",
            "snapshot": {
                "file_count": 3,
                "directory_count": 1,
                "languages": [{"name": "Python", "files": 2}],
                "manifests": ["pyproject.toml"],
                "checks": ["python -m pytest"],
                "layout": [{"path": "src", "type": "directory", "files": 2}],
            },
        }

    async def execute(self, request):
        self.requests.append(request)
        if request.tool == "read":
            text = "x" * min(self.read_bytes, int(request.arguments.get("max_bytes") or 10**9))
            return ToolResult(
                request.request_id,
                True,
                output=text,
                data={
                    "path": request.arguments["path"],
                    "sha256": self.file_hash,
                    "size_bytes": self.read_bytes,
                    "start_line": 1,
                    "end_line": 1,
                    "line_count": 1,
                    "byte_offset": 0,
                    "truncated": False,
                },
            )
        if request.tool in {"edit", "write"}:
            return ToolResult(
                request.request_id,
                True,
                output="ok",
                data={
                    "path": request.arguments["path"],
                    "old_sha256": self.file_hash,
                    "new_sha256": "b" * 64,
                    "actual_occurrences": 1,
                    "diff": {
                        "text": "--- a\n+++ a\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new",
                        "added_lines": 1,
                        "removed_lines": 1,
                    },
                    "workspace_status": {"staged": True, "created": [], "modified": ["a"]},
                },
            )
        return ToolResult(request.request_id, True, output="ok", data={})

    async def cancel(self, request_id):
        return None

    async def manifest(self, run_id, approval_id):
        return SimpleNamespace(operations=())


class AllowApprovals:
    async def wait(self, *args, **kwargs):
        from shared.permissions import PermissionDecision

        return PermissionDecision.ALLOW_RUN


class Provider:
    """A scripted provider: each call pops the next turn (or raises it) and records the
    exact request bytes the gateway would send.

    With ``usage_from_payload`` it reports the prompt tokens the serialized payload actually
    holds (messages plus tool schemas), so a scenario can check that the loop compacts before a
    real prompt exceeds the mark instead of trusting the fixed fixture count.
    """

    def __init__(self, turns, *, usage_from_payload: bool = False) -> None:
        self.turns = list(turns)
        self.usage_from_payload = usage_from_payload
        self.payloads: list[dict] = []
        self.bodies: list[bytes] = []
        self.on_call = None

    async def __call__(
        self,
        model,
        messages,
        api_key,
        mode,
        provider_preferences=None,
        allowed_tools=None,
        **kwargs,
    ):
        for name in ("idle_timeout", "turn_timeout", "on_delta"):
            kwargs.pop(name, None)
        payload = agent_payload(
            model, messages, mode, provider_preferences, allowed_tools, **kwargs
        )
        self.payloads.append(json.loads(serialize_payload(payload)))
        self.bodies.append(serialize_payload(payload))
        if self.on_call:
            self.on_call(len(self.payloads))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return self._measured(turn) if self.usage_from_payload else turn

    def _measured(self, turn: AgentTurn) -> AgentTurn:
        payload = self.payloads[-1]
        schemas = json.dumps(payload["tools"], ensure_ascii=False, separators=(",", ":"))
        prompt = estimate_message_tokens(payload["messages"]) + estimate_message_tokens(
            [{"role": "system", "content": schemas}]
        )
        usage = {**turn.usage, "prompt_tokens": prompt, "cached_tokens": prompt // 2}
        return AgentTurn(
            turn.content,
            turn.tool_calls,
            turn.message,
            usage,
            turn.finish_reason,
            turn.provider,
            turn.model,
        )


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def tools_turn(*calls, content: str = "", usage=None, finish_reason=None) -> AgentTurn:
    return AgentTurn(
        content,
        tuple(calls),
        {"role": "assistant", "content": content, "tool_calls": list(calls)},
        usage or {"prompt_tokens": 100, "cached_tokens": 100, "total_tokens": 110},
        finish_reason,
        "Provider-A",
    )


def final_turn(text: str = "done", usage=None) -> AgentTurn:
    return AgentTurn(
        text,
        (),
        {"role": "assistant", "content": text},
        usage or {"prompt_tokens": 100, "total_tokens": 110},
        "stop",
        "Provider-A",
    )


def make_request(**overrides) -> AgentRunRequest:
    values = {
        "model": "openai/test-model",
        "messages": ({"role": "user", "content": "do the task"},),
        "mode": "agent",
        "workspace_id": "workspace",
        "approval_policy": "auto",
        "session_id": "session-1",
        "context_limit_tokens": 400_000,
    }
    values.update(overrides)
    return AgentRunRequest(**values)


def run(
    monkeypatch,
    turns,
    *,
    executor=None,
    request=None,
    runtime=None,
    events=None,
    usage_from_payload=False,
    **run_kwargs,
):
    provider = Provider(turns, usage_from_payload=usage_from_payload)
    monkeypatch.setattr(runtime_module, "agent_turn", provider)
    monkeypatch.setattr(runtime_module, "RETRY_BASE_DELAY", 0.0)
    events = [] if events is None else events
    saved: dict = {}

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    def snapshot_saver(run_id, request_dict, messages, visible, budget, safe):
        saved["snapshot"] = (request_dict, [dict(m) for m in messages], visible, budget, safe)

    runtime = runtime or AgentRuntime(
        executor or ScriptedExecutor(), AllowApprovals(), append, snapshot_saver=snapshot_saver
    )
    runtime.append = append
    runtime.snapshot_saver = snapshot_saver
    asyncio.run(runtime.run("run-1", request or make_request(), "key", **run_kwargs))
    return provider, events, saved, runtime


def kinds(events) -> list[str]:
    return [kind for kind, _payload in events]


def tool_contents(payload: dict) -> list[str]:
    return [m["content"] for m in payload["messages"] if m["role"] == "tool"]


# -- 0.1 per-result bounds ----------------------------------------------------------------


def test_a_long_run_past_the_old_run_allowance_still_gets_full_results(monkeypatch) -> None:
    # 30 reads of 20,000 bytes is 600,000 bytes: past the old 512,000-byte run allowance.
    turns = [
        tools_turn(call(f"r{index}", "read", {"path": f"f{index}.py"})) for index in range(30)
    ]
    provider, events, _saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=20_000),
        # The large profile's working set holds all 600,000 bytes, so compaction never runs.
        request=make_request(context_limit_tokens=2_000_000, budget_profile="large-coding"),
    )
    last = tool_contents(provider.payloads[-1])
    assert len(last) == 30
    assert all(len(item) > 20_000 and "x" * 20_000 in item for item in last)
    assert "tool.result_bounded" not in kinds(events)


def test_each_result_is_bounded_by_its_tool_cap_and_journaled(monkeypatch) -> None:
    class HugeBash(ScriptedExecutor):
        async def execute(self, request):
            if request.tool == "status":
                return ToolResult(
                    request.request_id,
                    True,
                    output="",
                    data={
                        "counts": {},
                        "changes": [
                            {"path": f"{'d/' * 50}{index}.py", "operation": "create"}
                            for index in range(2_000)
                        ],
                    },
                )
            return await super().execute(request)

    provider, events, _saved, _runtime = run(
        monkeypatch,
        [tools_turn(call("s1", "status", {})), final_turn()],
        executor=HugeBash(),
    )
    result = tool_contents(provider.payloads[-1])[0]
    assert len(result.encode()) <= 24_000
    bounded = [payload for kind, payload in events if kind == "tool.result_bounded"]
    assert bounded and bounded[0]["tool"] == "status"
    assert bounded[0]["original_bytes"] > 24_000 and bounded[0]["cap_bytes"] == 24_000


def test_read_requests_are_clamped_to_the_read_result_cap(monkeypatch) -> None:
    executor = ScriptedExecutor()
    provider, _events, _saved, _runtime = run(
        monkeypatch,
        [tools_turn(call("r1", "read", {"path": "a.py", "max_bytes": 900_000})), final_turn()],
        executor=executor,
    )
    reads = [item for item in executor.requests if item.tool == "read"]
    assert reads[0].arguments["max_bytes"] == 31_000
    assert "capped at 31,000 by the coding profile" in tool_contents(provider.payloads[-1])[0]


# -- 0.2 prompt -----------------------------------------------------------------------------


def test_the_first_system_message_is_the_static_prompt_and_run_started_records_it(
    monkeypatch,
) -> None:
    provider, events, _saved, _runtime = run(monkeypatch, [final_turn()])
    first = provider.payloads[0]["messages"][0]
    assert first == {"role": "system", "content": agent_prompt("agent")}
    started = next(payload for kind, payload in events if kind == "run.started")
    assert started["prompt_version"] == PROMPT_VERSION
    for value in ("session-1", "$", "Approval tier", "auto", "900 tool calls"):
        assert value not in first["content"]


# -- 0.3 workspace instructions ------------------------------------------------------------


def test_saved_workspace_instructions_reach_the_model(monkeypatch) -> None:
    request = make_request(workspace_config={"instructions": "Prefer tabs over spaces."})
    provider, _events, _saved, _runtime = run(monkeypatch, [final_turn()], request=request)
    context = provider.payloads[0]["messages"][1]["content"]
    assert (
        "## USER WORKSPACE CONFIGURATION (user-authored)\nPrefer tabs over spaces." in context
    )
    assert "UNTRUSTED WORKSPACE INSTRUCTIONS" in context and "Run the fast checks." in context


# -- 0.4 retries ----------------------------------------------------------------------------


def test_transient_provider_errors_are_retried_with_the_identical_payload(monkeypatch) -> None:
    failure = ProviderError("provider.agent_error", "upstream 502", retryable=True)
    provider, events, _saved, _runtime = run(monkeypatch, [failure, failure, final_turn()])
    assert kinds(events).count("model.retry") == 2
    assert provider.bodies[0] == provider.bodies[1] == provider.bodies[2]
    assert kinds(events)[-1] == "run.completed"


def test_a_provider_that_keeps_failing_pauses_the_run_resumably(monkeypatch) -> None:
    failure = ProviderError("provider.timeout", "timed out", retryable=True)
    _provider, events, saved, _runtime = run(monkeypatch, [failure, failure, failure])
    assert kinds(events)[-1] == "run.paused"
    paused = events[-1][1]
    assert paused["reason"] == "provider_error" and paused["resumable"] is True
    assert saved["snapshot"][4] is True


def test_a_non_retryable_provider_error_fails_the_run(monkeypatch) -> None:
    failure = ProviderError("provider.agent_error", "bad key (401)", retryable=False)
    _provider, events, _saved, _runtime = run(monkeypatch, [failure])
    assert kinds(events)[-1] == "run.failed" and "model.retry" not in kinds(events)


# -- 0.8 narration and steering -----------------------------------------------------------


def test_text_beside_tool_calls_is_journaled_as_narration(monkeypatch) -> None:
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        [tools_turn(call("r1", "read", {"path": "a"}), content="Checking a."), final_turn()],
    )
    assert ("model.narration", {"text": "Checking a."}) in events


def test_steering_joins_before_the_next_turn_and_never_inside_a_tool_exchange(
    monkeypatch,
) -> None:
    calls = (call("r1", "read", {"path": "a"}), call("r2", "read", {"path": "b"}))
    runtime_holder: dict = {}

    class SteeringExecutor(ScriptedExecutor):
        async def execute(self, request):
            # The user steers while the first tool call is still running.
            if request.tool == "read" and request.arguments["path"] == "a":
                assert runtime_holder["runtime"].steer("run-1", "Also check c.")
            return await super().execute(request)

    events: list = []

    async def append(_run_id, kind, payload):
        events.append((kind, payload))

    runtime = AgentRuntime(SteeringExecutor(), AllowApprovals(), append)
    runtime_holder["runtime"] = runtime
    provider, _events, _saved, _runtime = run(
        monkeypatch, [tools_turn(*calls), final_turn()], runtime=runtime
    )
    roles = [(m["role"], m.get("content")) for m in provider.payloads[1]["messages"]]
    assert roles[-1] == ("user", "Also check c.")
    assert [role for role, _content in roles[-4:-1]] == ["assistant", "tool", "tool"]
    assert runtime.is_active("run-1") is False


# -- 1.1 usage accounting -----------------------------------------------------------------


def test_turn_usage_and_unexpected_misses_are_journaled(monkeypatch) -> None:
    first = tools_turn(
        call("r1", "read", {"path": "a"}),
        usage={"prompt_tokens": 10_000, "cached_tokens": 0, "cost": 0.01},
    )
    second = tools_turn(
        call("r2", "read", {"path": "b"}),
        usage={"prompt_tokens": 11_000, "cached_tokens": 9_800, "cost": 0.002},
    )
    third = final_turn(usage={"prompt_tokens": 12_000, "cached_tokens": 2_000, "cost": 0.01})
    _provider, events, _saved, _runtime = run(monkeypatch, [first, second, third])
    turns = [payload for kind, payload in events if kind == "usage.turn"]
    assert [item["miss_cause"] for item in turns] == ["first_turn", None, "unexpected"]
    assert turns[0]["provider"] == "Provider-A"
    assert kinds(events).count("cache.unexpected_miss") == 1
    completed = events[-1][1]
    assert completed["turns"] == 3
    assert completed["cache_hit_rate"] == pytest.approx(11_800 / 33_000)
    assert completed["session"]["tool_calls"] == 2


# -- 1.2 cache-stable layout ----------------------------------------------------------------


def _assert_extends(previous: bytes, current: bytes) -> None:
    before = json.loads(previous)["messages"]
    after = json.loads(current)["messages"]
    assert after[: len(before)] == before
    head = serialize_payload({"messages": before})[:-2]
    assert serialize_payload({"messages": after}).startswith(head)


def test_every_request_extends_the_previous_one_byte_for_byte(monkeypatch) -> None:
    turns = [
        tools_turn(call("r1", "read", {"path": "a"}), content="Reading."),
        tools_turn(call("e1", "edit", {"path": "a", "old_str": "old", "new_str": "new"})),
        tools_turn(call("b1", "bash", {"command": "pytest -q"})),
        final_turn(),
    ]
    provider, _events, _saved, _runtime = run(monkeypatch, turns)
    for previous, current in zip(provider.bodies, provider.bodies[1:], strict=False):
        _assert_extends(previous, current)
        assert json.loads(previous)["tools"] == json.loads(current)["tools"]
    payload = provider.payloads[0]
    assert payload["session_id"] == "session-1"
    assert payload["parallel_tool_calls"] is True
    assert payload["max_tokens"] == 16_000
    assert "order" not in payload["provider"]
    assert [m["role"] for m in payload["messages"]] == ["system", "system", "user", "user"]
    assert payload["messages"][3]["content"].startswith("RUN NOTES")


def test_follow_up_and_resumed_runs_extend_the_previous_request(monkeypatch) -> None:
    provider, _events, saved, _runtime = run(
        monkeypatch, [tools_turn(call("r1", "read", {"path": "a"})), final_turn()]
    )
    last_request = provider.bodies[-1]
    final_messages = saved["snapshot"][1]
    follow_up, _events, _saved, _runtime = run(
        monkeypatch,
        [final_turn()],
        initial_messages=[*final_messages, {"role": "user", "content": "now b"}],
    )
    _assert_extends(last_request, follow_up.bodies[0])

    # A run paused mid-way resumes from its snapshot and extends that request too.
    paused_runtime_events: list = []

    def pause_after_first(count):
        if count == 1:
            paused_runtime.pause_requested = lambda _run_id: True

    paused_runtime = AgentRuntime(ScriptedExecutor(), AllowApprovals(), None)
    provider_a = Provider([tools_turn(call("r1", "read", {"path": "a"}))])
    provider_a.on_call = pause_after_first
    monkeypatch.setattr(runtime_module, "agent_turn", provider_a)
    holder: dict = {}

    async def append(_run_id, kind, payload):
        paused_runtime_events.append(kind)

    paused_runtime.append = append
    paused_runtime.snapshot_saver = lambda *args: holder.update(
        messages=[dict(m) for m in args[2]]
    )
    asyncio.run(paused_runtime.run("run-2", make_request(), "key"))
    assert paused_runtime_events[-1] == "run.paused"
    resumed, _events, _saved, _runtime = run(
        monkeypatch, [final_turn()], initial_messages=holder["messages"], resumed=True
    )
    before = json.loads(provider_a.bodies[-1])["messages"]
    after = json.loads(resumed.bodies[0])["messages"]
    assert after[: len(before)] == before


def test_a_replayed_session_keeps_its_session_context_verbatim(monkeypatch) -> None:
    provider, _events, saved, _runtime = run(monkeypatch, [final_turn()])
    stored = saved["snapshot"][1]

    class ChangedSnapshot(ScriptedExecutor):
        async def workspace_snapshot(self):
            data = await super().workspace_snapshot()
            data["snapshot"]["file_count"] = 99  # the agent created files since
            return data

    replay, events, _saved, _runtime = run(
        monkeypatch,
        [final_turn()],
        executor=ChangedSnapshot(),
        initial_messages=[*stored, {"role": "user", "content": "next"}],
    )
    assert replay.payloads[0]["messages"][1] == provider.payloads[0]["messages"][1]

    class ChangedAgents(ScriptedExecutor):
        async def workspace_snapshot(self):
            data = await super().workspace_snapshot()
            data["agents_md"] = "Published new rules."
            return data

    rebuilt, events, _saved, _runtime = run(
        monkeypatch,
        [final_turn(usage={"prompt_tokens": 100, "cached_tokens": 0})],
        executor=ChangedAgents(),
        initial_messages=[*stored, {"role": "user", "content": "next"}],
    )
    assert "Published new rules." in rebuilt.payloads[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    ("model", "expected"),
    [("anthropic/claude-x", True), ("qwen/qwen3-coder", True), ("openai/gpt-x", False)],
)
def test_cache_breakpoints_only_for_providers_that_need_them(model, expected) -> None:
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "system", "content": "SESSION CONTEXT"},
        {"role": "user", "content": "go"},
    ]
    payload = agent_payload(model, messages, "agent")
    marked = [
        index
        for index, item in enumerate(payload["messages"])
        if isinstance(item["content"], list) and item["content"][0].get("cache_control")
    ]
    assert marked == ([1, 2] if expected else [])
    assert messages[2] == {"role": "user", "content": "go"}  # history is never modified


# -- 1.3 compaction ------------------------------------------------------------------------


def test_compaction_happens_only_at_the_high_water_mark_and_keeps_tool_pairs(
    monkeypatch,
) -> None:
    # A 32,000-token window with the growth reserve suppressed holds 16,000 tokens once the
    # 16,000-token output cap is set aside, so the capacity bound sets the mark: the request
    # asks for 16,000 and the trigger is one summary request below it, 15,000. Each read adds
    # about 1,550 estimated prompt tokens, and the eighth check compacts.
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(8)]
    handoff = final_turn("Objective: finish.\nNext steps: read f9.")
    turns.insert(7, handoff)
    provider, events, saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=6_000),
        request=make_request(context_limit_tokens=32_000),
    )
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert len(compactions) == 1 and compactions[0]["method"] == "summary"
    journal = compactions[0]
    assert journal["high_water_tokens"] == 16_000  # the request-derived mark
    assert journal["effective_high_water_tokens"] == 16_000
    assert journal["trigger_tokens"] == 15_000
    assert journal["tokens_before"] >= journal["trigger_tokens"]
    assert journal["tokens_after"] < journal["tokens_before"]
    assert journal["tokens_after"] <= journal["low_water_tokens"] == 5_333
    summary_request = provider.payloads[7]
    assert summary_request["tool_choice"] == "none"
    assert summary_request["max_tokens"] == 2_500
    assert summary_request["messages"][-1]["content"].startswith("COMPACTION REQUEST")
    # The summary request itself is sent below the effective mark.
    assert estimate_message_tokens(summary_request["messages"]) <= 16_000
    after = provider.payloads[8]["messages"]
    assert after[0]["content"] == agent_prompt("agent")
    assert after[1]["content"].startswith("SESSION CONTEXT")
    assert after[2]["content"].startswith("HANDOFF")
    ids = {c["id"] for m in after for c in m.get("tool_calls") or ()}
    assert all(m["tool_call_id"] in ids for m in after if m["role"] == "tool")
    assert "HANDOFF" in json.dumps(saved["snapshot"][1])  # survives pause and resume
    misses = [p["miss_cause"] for kind, p in events if kind == "usage.turn"]
    assert "unexpected" not in misses


def test_a_failed_summary_turn_falls_back_to_deterministic_compaction(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(8)]
    turns.insert(7, ProviderError("provider.agent_error", "context too long", retryable=False))
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=6_000),
        request=make_request(context_limit_tokens=32_000),
    )
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert compactions and compactions[0]["method"] == "deterministic"
    assert kinds(events)[-1] == "run.completed"


def test_recent_exchanges_cut_at_a_valid_boundary() -> None:
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "x" * 4_000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "y" * 400},
    ]
    kept = recent_exchanges(history, 200)
    assert kept[0]["role"] == "assistant" and kept[-1]["tool_call_id"] == "b"


# -- 1.4 hashes and re-reads --------------------------------------------------------------


def test_the_gateway_fills_the_last_seen_hash_into_edits(monkeypatch) -> None:
    executor = ScriptedExecutor()
    run(
        monkeypatch,
        [
            tools_turn(call("r1", "read", {"path": "a", "start_line": 1, "end_line": 5})),
            tools_turn(call("e1", "edit", {"path": "a", "old_str": "o", "new_str": "n"})),
            tools_turn(call("w1", "write", {"path": "a", "content": "x"})),
            final_turn(),
        ],
        executor=executor,
    )
    edit = next(item for item in executor.requests if item.tool == "edit")
    write = next(item for item in executor.requests if item.tool == "write")
    assert edit.arguments["expected_sha256"] == "a" * 64
    # A partial read never vouches for a whole-file rewrite.
    assert "expected_sha256" not in write.arguments


def test_an_unchanged_re_read_is_a_one_line_note_while_the_result_is_live(monkeypatch) -> None:
    provider, _events, _saved, _runtime = run(
        monkeypatch,
        [
            tools_turn(call("r1", "read", {"path": "a"})),
            tools_turn(call("r2", "read", {"path": "a"})),
            final_turn(),
        ],
    )
    results = tool_contents(provider.payloads[-1])
    assert results[0].startswith("read a lines 1–1 of 1 (full coverage)")
    assert results[1] == (
        "read a: unchanged since tool call r1; that result covered the whole file "
        "(full coverage)."
    )


# -- 1.8 output discipline ------------------------------------------------------------------


def test_reasoning_effort_and_the_length_note(monkeypatch) -> None:
    provider, _events, _saved, _runtime = run(
        monkeypatch,
        [
            tools_turn(
                call("w1", "write", {"path": "a", "content": "x"}), finish_reason="length"
            ),
            final_turn(),
        ],
        request=make_request(reasoning_effort="low"),
    )
    assert provider.payloads[0]["reasoning"] == {"effort": "low"}
    assert provider.payloads[1]["messages"][-1]["content"].startswith(
        "RUN NOTE: your last reply"
    )


# -- 3.2 thinking levels --------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection,wire",
    [
        (None, None),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("extra_high", "xhigh"),
        ("max", "max"),
    ],
)
def test_agent_payload_sends_the_mapped_effort_for_each_level(
    monkeypatch, selection, wire
) -> None:
    provider, _events, _saved, _runtime = run(
        monkeypatch, [final_turn()], request=make_request(reasoning_effort=selection)
    )
    if wire is None:
        assert "reasoning" not in provider.payloads[0]
    else:
        assert provider.payloads[0]["reasoning"] == {"effort": wire}


def test_a_rejected_effort_steps_down_once_and_holds_for_the_run(monkeypatch) -> None:
    message = "reasoning.effort 'max' is not supported"
    rejected = ProviderError(REASONING_EFFORT_REJECTED, message, retryable=False)
    provider, events, _saved, _runtime = run(
        monkeypatch,
        [rejected, tools_turn(call("r1", "read", {"path": "a"})), final_turn()],
        request=make_request(reasoning_effort="max"),
    )
    assert [item.get("reasoning") for item in provider.payloads] == [
        {"effort": "max"},
        {"effort": "xhigh"},
        {"effort": "xhigh"},
    ]
    clamps = [payload for kind, payload in events if kind == "reasoning.effort_clamped"]
    assert clamps == [
        {"requested": "max", "sent": "xhigh", "selection": "extra_high", "message": message}
    ]
    started = next(payload for kind, payload in events if kind == "run.started")
    assert started["reasoning_effort"] == "max"
    assert kinds(events)[-1] == "run.completed"


def test_a_second_rejection_fails_the_run_with_the_typed_error(monkeypatch) -> None:
    rejected = [
        ProviderError(REASONING_EFFORT_REJECTED, "not supported", retryable=False)
        for _ in range(2)
    ]
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        [*rejected, final_turn()],
        request=make_request(reasoning_effort="max"),
    )
    assert kinds(events).count("reasoning.effort_clamped") == 1
    assert kinds(events)[-1] == "run.failed"


def test_a_level_with_no_lower_step_fails_without_a_retry(monkeypatch) -> None:
    rejected = ProviderError(REASONING_EFFORT_REJECTED, "not supported", retryable=False)
    provider, events, _saved, _runtime = run(
        monkeypatch,
        [rejected, final_turn()],
        request=make_request(reasoning_effort="high"),
    )
    assert len(provider.payloads) == 1
    assert "reasoning.effort_clamped" not in kinds(events)
    assert kinds(events)[-1] == "run.failed"


# -- 1.7 snapshot ---------------------------------------------------------------------------


def test_the_workspace_snapshot_is_in_the_first_request(monkeypatch) -> None:
    provider, _events, _saved, _runtime = run(monkeypatch, [final_turn()])
    context = provider.payloads[0]["messages"][1]["content"]
    assert "## WORKSPACE SNAPSHOT" in context and "detected checks: python -m pytest" in context
    assert "Installed: python3, git." in context


def test_a_saved_threshold_changes_where_an_agent_run_compacts(monkeypatch) -> None:
    # The user's threshold is below the window's own capacity mark, so it binds: the run
    # compacts at the threshold, and the journal records the mark it actually used.
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(3)]
    turns.insert(3, final_turn("Objective: finish.\nNext steps: read f6."))
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=6_000),
        request=make_request(context_limit_tokens=32_000, compaction_threshold_tokens=10_000),
    )
    started = [payload for kind, payload in events if kind == "run.started"]
    assert started[0]["compaction_threshold_tokens"] == 10_000
    # The effective mark is journaled once the session layout exists.
    high_water = [payload for kind, payload in events if kind == "context.high_water"]
    assert high_water[0]["requested_tokens"] == 10_000
    assert high_water[0]["effective_tokens"] == 10_000
    assert high_water[0]["binding"] == "requested"
    assert high_water[0]["trigger_tokens"] == 9_000
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert len(compactions) == 1
    assert compactions[0]["high_water_tokens"] == 10_000
    assert compactions[0]["tokens_before"] >= compactions[0]["trigger_tokens"] == 9_000
    assert kinds(events)[-1] == "run.completed"


def test_a_5_percent_threshold_compacts_before_the_next_ordinary_call(monkeypatch) -> None:
    # 5% of a 200k window is 10,000 tokens, below the fixed-context floor once the workspace
    # instructions render into the session prefix, so the run works against the raised mark and
    # journals it. The provider reports the real size of each payload, so a run that waited for
    # the message-only estimate to catch up would show an ordinary call above the mark.
    turns = [
        tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(10)
    ]
    turns.insert(0, final_turn("Objective: finish.\nNext steps: read f9."))
    provider, events, _saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=6_000),
        request=make_request(
            context_limit_tokens=160_000,
            compaction_threshold_tokens=10_000,
            workspace_config={"instructions": "x" * 32_000},
        ),
        usage_from_payload=True,
    )
    marks = [payload for kind, payload in events if kind == "context.high_water"]
    assert marks[0]["requested_tokens"] == 10_000
    effective = marks[0]["effective_tokens"]
    trigger = marks[0]["trigger_tokens"]
    assert effective == marks[0]["floor_tokens"] > 10_000  # raised to the fixed-context floor
    assert marks[0]["binding"] == "prefix"
    assert trigger == effective - runtime_module.COMPACTION_REQUEST_RESERVE_TOKENS
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert compactions, kinds(events)
    assert compactions[0]["effective_high_water_tokens"] == effective
    assert compactions[0]["trigger_tokens"] == trigger
    # Every ordinary call stayed at or below the mark, and no `usage.turn` reports a larger
    # prompt before the compaction that follows it.
    prompts = [payload["prompt_tokens"] for kind, payload in events if kind == "usage.turn"]
    assert prompts, kinds(events)
    assert all(tokens <= effective for tokens in prompts), prompts
    first_over = next(
        (index for index, tokens in enumerate(prompts) if tokens > effective), None
    )
    if first_over is not None:
        assert kinds(events).index("context.compacted") < _usage_index(events, first_over)
    # The summary request itself is sent at or below the mark.
    summary = next(payload for payload in provider.payloads if payload["tool_choice"] == "none")
    assert _payload_tokens(summary) <= effective


def test_the_previous_actual_prompt_is_a_lower_bound_for_the_next_check(monkeypatch) -> None:
    # A provider that reports far more prompt tokens than the local estimate must not be ignored:
    # the next check compacts, where the message-only estimate alone would not.
    turns = [
        tools_turn(call("r0", "read", {"path": "a"}), usage={"prompt_tokens": 60_000}),
        final_turn("Objective: finish."),
        tools_turn(call("r1", "read", {"path": "b"})),
        final_turn(),
    ]
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        turns,
        executor=ScriptedExecutor(read_bytes=1_000),
        request=make_request(context_limit_tokens=160_000, compaction_threshold_tokens=50_000),
    )
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert len(compactions) == 1, kinds(events)
    # The local estimate of that prompt was a few thousand tokens; the provider's own count is
    # what triggered the compaction.
    assert compactions[0]["tokens_before"] >= 60_000
    first_prompt = next(payload for kind, payload in events if kind == "usage.turn")
    assert first_prompt["prompt_tokens"] == 60_000
    assert kinds(events).index("context.compacted") < _usage_index(events, 1)


def test_a_window_that_cannot_hold_the_fixed_context_fails_closed(monkeypatch) -> None:
    # A 20,000-token prompt budget cannot hold the output turn and the growth reserve, let alone
    # the fixed prefix: the run names the reason instead of sending an over-limit prompt.
    provider, events, _saved, _runtime = run(
        monkeypatch,
        [final_turn()],
        request=make_request(context_limit_tokens=20_000),
    )
    assert provider.payloads == []  # no provider call was made
    failed = [payload for kind, payload in events if kind == "run.failed"]
    assert failed and failed[0]["code"] == "context.floor_too_large"
    assert "fixed session context does not fit" in failed[0]["message"]
    assert "prompt budget 20,000 tokens" in failed[0]["message"]
    assert not any(kind == "context.high_water" for kind, _payload in events)


def _payload_tokens(payload: dict) -> int:
    schemas = json.dumps(payload["tools"], ensure_ascii=False, separators=(",", ":"))
    return estimate_message_tokens(payload["messages"]) + estimate_message_tokens(
        [{"role": "system", "content": schemas}]
    )


def _usage_index(events, ordinal: int) -> int:
    """The event index of the ``ordinal``-th ``usage.turn`` event."""
    seen = 0
    for index, (kind, _payload) in enumerate(events):
        if kind == "usage.turn":
            if seen == ordinal:
                return index
            seen += 1
    return len(events)


def test_a_fixed_prefix_above_the_requested_mark_compacts_once(monkeypatch) -> None:
    # The workspace instructions render into the session context, so the fixed prefix alone is
    # larger than the 5,000-token request. The run compacts once, learns the floor that keeps
    # it from compacting again immediately, and journals that decision.
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(6)]
    turns.insert(0, final_turn("Objective: finish.\nNext steps: read f2."))
    _provider, events, _saved, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=ScriptedExecutor(read_bytes=1_000),
        request=make_request(
            context_limit_tokens=160_000,
            compaction_threshold_tokens=5_000,
            workspace_config={"instructions": "x" * 32_000},
        ),
    )
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    floors = [payload for kind, payload in events if kind == "context.compaction_floor"]
    assert len(compactions) == 1, compactions
    assert len(floors) == 1, floors
    assert compactions[0]["high_water_tokens"] == 5_000
    assert compactions[0]["effective_high_water_tokens"] > 5_000  # raised to the prefix floor
    assert floors[0]["floor_tokens"] > compactions[0]["effective_high_water_tokens"]
    assert floors[0]["compacted_tokens"] > 0
    assert kinds(events)[-1] == "run.completed"
    # The floor outlives the compaction: the following turns are ordinary model calls.
    summary_calls = [p for p in _provider.payloads if p["tool_choice"] == "none"]
    assert len(summary_calls) == 1
