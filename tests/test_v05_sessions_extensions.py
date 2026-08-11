from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from server.agent.budgets import RunBudget
from server.agent.context import compact_agent_context
from server.agent.runtime import AgentRuntime
from server.config import GatewayConfig
from server.extensions.skills import discover_skills, parse_skill, render_skill_context
from server.journal import JournalStore
from server.openrouter.agent import AgentTurn, _normalize_message
from server.service import GatewayService, GatewayServiceError
from shared.requests import AgentRunRequest
from shared.tools import PublishManifest, PublishOperation, ToolResult
from src.commands import expand_prompt_command
from src.context_utils import compact_messages
from src.gateway_client import _public_messages
from src.storage import Storage


class FakeExecutor:
    async def status(self):
        return {"workspace_id": "workspace"}

    async def execute(self, request):
        if request.tool == "read_file" and request.arguments.get("path") == "AGENTS.md":
            return ToolResult(request.request_id, False, error_code="not_found")
        return ToolResult(request.request_id, True, output="important result")

    async def manifest(self, run_id, approval_id):
        return PublishManifest("manifest", run_id, "workspace", "snapshot", approval_id, ())

    async def cancel(self, _request_id):
        return True


class FakeApprovals:
    async def wait(self, *_args, **_kwargs):  # pragma: no cover - read tools do not ask
        raise AssertionError("unexpected approval")


def test_agent_defaults_allow_exactly_one_hundred_tool_calls() -> None:
    request = AgentRunRequest(
        "vendor/model", ({"role": "user", "content": "work"},), "agent", "workspace"
    )
    assert request.max_tool_calls == 100
    assert request.max_iterations == 101
    assert AgentRunRequest.from_dict(request.to_dict()).max_tool_calls == 100
    assert AgentRunRequest.from_dict(request.to_dict()).approval_policy == "prompt"

    budget = RunBudget(
        request.max_iterations,
        request.max_wall_seconds,
        request.max_cost,
        request.max_tool_calls,
    )
    for _ in range(100):
        budget.consume_tool_call()
    assert budget.snapshot()["tool_calls"] == 100
    try:
        budget.consume_tool_call()
    except RuntimeError as exc:
        assert str(exc) == "tool-call budget exhausted"
    else:  # pragma: no cover - the safety boundary must remain enforced
        raise AssertionError("the tool-call budget accepted a 101st call")


def test_run_budget_accumulates_usage_across_provider_calls() -> None:
    budget = RunBudget(10, 900, 1.0)
    budget.add_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 8,
            "reasoning_tokens": 3,
            "total_tokens": 120,
            "cost": 0.01,
        }
    )
    budget.add_usage(
        {
            "prompt_tokens": 40,
            "completion_tokens": 5,
            "total_tokens": 45,
            "cost": 0.02,
        }
    )

    assert budget.usage() == {
        "prompt_tokens": 140,
        "completion_tokens": 25,
        "cached_tokens": 8,
        "reasoning_tokens": 3,
        "total_tokens": 165,
        "cost": 0.03,
    }
    restored = RunBudget(10, 900, 1.0)
    restored.restore(budget.snapshot())
    assert restored.usage() == budget.usage()


def test_agent_usage_event_is_cumulative_for_the_whole_turn(monkeypatch) -> None:
    turns = iter(
        [
            AgentTurn(
                "Inspecting.",
                (
                    {
                        "id": "call-1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    },
                ),
                {
                    "role": "assistant",
                    "content": "Inspecting.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"README.md"}',
                            },
                        }
                    ],
                },
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cached_tokens": 8,
                    "reasoning_tokens": 3,
                    "total_tokens": 120,
                    "cost": 0.01,
                },
            ),
            AgentTurn(
                "Finished.",
                (),
                {"role": "assistant", "content": "Finished."},
                {
                    "prompt_tokens": 40,
                    "completion_tokens": 5,
                    "total_tokens": 45,
                    "cost": 0.02,
                },
            ),
        ]
    )

    async def fake_turn(*_args, **_kwargs):
        return next(turns)

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_turn)
    events = []

    async def append(run_id, event_type, payload):
        events.append((run_id, event_type, payload))

    runtime = AgentRuntime(FakeExecutor(), FakeApprovals(), append)
    request = AgentRunRequest(
        "vendor/model",
        ({"role": "user", "content": "Inspect"},),
        "plan",
        "workspace",
    )
    asyncio.run(runtime.run("run", request, "api-key"))

    updates = [payload for _run_id, event_type, payload in events if event_type == "usage.updated"]
    assert updates[0]["total_tokens"] == 120
    assert updates[0]["cost"] == 0.01
    assert updates[-1]["prompt_tokens"] == 140
    assert updates[-1]["completion_tokens"] == 25
    assert updates[-1]["total_tokens"] == 165
    assert updates[-1]["cost"] == 0.03
    assert updates[-1]["run_cost"] == 0.03


def test_publication_manifest_failure_does_not_fail_completed_agent_run(monkeypatch) -> None:
    class ManifestFailExecutor(FakeExecutor):
        async def manifest(self, _run_id, _approval_id):
            raise ValueError("changed binary or invalid UTF-8 file")

    async def fake_turn(*_args, **_kwargs):
        return AgentTurn(
            "Finished.",
            (),
            {"role": "assistant", "content": "Finished."},
            {"cost": 0.01},
        )

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_turn)
    events = []

    async def append(run_id, event_type, payload):
        events.append((run_id, event_type, payload))

    request = AgentRunRequest(
        "vendor/model",
        ({"role": "user", "content": "Create a file"},),
        "agent",
        "workspace",
    )
    runtime = AgentRuntime(ManifestFailExecutor(), FakeApprovals(), append)

    asyncio.run(runtime.run("run", request, "api-key"))

    event_types = [event_type for _run_id, event_type, _payload in events]
    assert "publication.failed" in event_types
    assert event_types[-1] == "run.completed"
    assert "run.failed" not in event_types


def test_auto_authorizes_mutation_and_offers_automatic_publication(monkeypatch) -> None:
    turns = iter(
        [
            AgentTurn(
                "Working.",
                ({"id": "call", "function": {"name": "run_command", "arguments": '{"executable":"pytest","arguments":[]}' }},),
                {
                    "role": "assistant",
                    "content": "Working.",
                    "tool_calls": [{"id": "call", "type": "function", "function": {"name": "run_command", "arguments": '{"executable":"pytest","arguments":[]}'}}],
                },
                {"cost": 0.01},
            ),
            AgentTurn("Done.", (), {"role": "assistant", "content": "Done."}, {"cost": 0.01}),
        ]
    )

    async def fake_turn(*_args, **_kwargs):
        return next(turns)

    class AutoExecutor(FakeExecutor):
        async def manifest(self, run_id, approval_id):
            content = "done\n"
            operation = PublishOperation(
                "result.txt", "create", None, hashlib.sha256(content.encode()).hexdigest(), content
            )
            return PublishManifest(
                "manifest", run_id, "workspace", "snapshot", approval_id, (operation,)
            )

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_turn)
    events = []

    async def append(run_id, event_type, payload):
        events.append((run_id, event_type, payload))

    request = AgentRunRequest(
        "vendor/model",
        ({"role": "user", "content": "work"},),
        "agent",
        "workspace",
        approval_policy="auto",
    )
    asyncio.run(AgentRuntime(AutoExecutor(), FakeApprovals(), append).run("run", request, "key"))

    event_types = [event_type for _run_id, event_type, _payload in events]
    assert "permission.auto_granted" in event_types
    assert "approval.required" not in event_types
    publish = next(payload for _run_id, event_type, payload in events if event_type == "checkpoint.created")
    assert publish["auto_publish"] is True


def test_gateway_rejects_auto_when_staging_is_not_clean(tmp_path: Path) -> None:
    class DirtyExecutor:
        async def execute(self, request):
            return ToolResult(request.request_id, True, total_known=1)

    async def scenario() -> None:
        service = GatewayService(
            GatewayConfig("t" * 43, tmp_path / "journal.sqlite3", workspace_id="workspace")
        )
        service.configure_client_key("api-key-value")
        service.executor = DirtyExecutor()
        service.agent_runtime = object()
        request = AgentRunRequest(
            "vendor/model",
            ({"role": "user", "content": "work"},),
            "agent",
            "workspace",
            approval_policy="auto",
        )
        try:
            await service.start_agent(request)
        except GatewayServiceError as exc:
            assert exc.code == "staging.not_clean"
        else:
            raise AssertionError("dirty staging was accepted in Auto")
        await service.close()

    asyncio.run(scenario())


def test_agent_session_retains_tool_messages_between_user_prompts(tmp_path, monkeypatch) -> None:
    turns = iter(
        [
            AgentTurn(
                "Inspecting.",
                ({"id": "call-1", "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}},),
                {
                    "role": "assistant",
                    "content": "Inspecting.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                },
                {"cost": 0.01},
            ),
            AgentTurn("Finished.", (), {"role": "assistant", "content": "Finished."}, {"cost": 0.01}),
        ]
    )

    async def fake_turn(*_args, **_kwargs):
        return next(turns)

    monkeypatch.setattr("server.agent.runtime.agent_turn", fake_turn)
    events = []
    sessions = []

    async def append(run_id, event_type, payload):
        events.append((run_id, event_type, payload))

    runtime = AgentRuntime(
        FakeExecutor(),
        FakeApprovals(),
        append,
        session_saver=lambda *args: sessions.append(args),
    )
    request = AgentRunRequest(
        "vendor/model",
        ({"role": "user", "content": "Inspect"},),
        "plan",
        "workspace",
        session_id="conversation",
    )
    asyncio.run(runtime.run("run", request, "api-key"))

    internal = sessions[-1][3]
    assert any(message.get("role") == "tool" for message in internal)
    assert sessions[-1][4][-1] == {"role": "assistant", "content": "Inspecting.Finished."}

    journal = JournalStore(tmp_path / "journal.sqlite3")
    journal.save_agent_session("conversation", "workspace", "plan", internal, sessions[-1][4], "now")
    service = GatewayService(GatewayConfig("t" * 43, tmp_path / "service.sqlite3"))
    service.journal.save_agent_session("conversation", "workspace", "plan", internal, sessions[-1][4], "now")
    follow_up = AgentRunRequest(
        "vendor/model",
        (*request.messages, sessions[-1][4][-1], {"role": "user", "content": "Continue"}),
        "plan",
        "workspace",
        session_id="conversation",
    )
    prepared, replayed = service._prepare_agent_context(follow_up)
    assert replayed
    assert any(message.get("role") == "tool" for message in prepared)
    assert prepared[-1]["content"] == "Continue"
    asyncio.run(service.close())
    journal.close()


def test_context_compaction_bounds_old_tool_output_without_orphaning_tool_messages() -> None:
    messages = [
        {"role": "user", "content": "work"},
        {
            "role": "assistant",
            "content": "reading",
            "tool_calls": [
                {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "x" * 40_000},
        {"role": "user", "content": "continue"},
    ]
    compacted, result = compact_agent_context(messages, 2_000, keep_recent=1)
    assert result is not None and result.after_tokens < result.before_tokens
    for index, message in enumerate(compacted):
        if message.get("role") == "tool":
            assert index and compacted[index - 1].get("role") == "assistant"


def test_provider_assistant_messages_are_portable_and_have_stable_tool_ids() -> None:
    normalized = _normalize_message(
        {
            "role": "assistant",
            "content": "read",
            "reasoning": "provider-only",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a"}}}],
        }
    )
    assert set(normalized) == {"role", "content", "tool_calls"}
    assert normalized["tool_calls"][0]["id"]
    assert normalized["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'


def test_safe_run_snapshot_survives_gateway_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = JournalStore(path)
    journal.create_run("agent-run", "agent", "before")
    journal.append("agent-run", "run.started", "before", {})
    request = AgentRunRequest(
        "vendor/model", ({"role": "user", "content": "work"},), "agent", "workspace"
    )
    journal.save_run_snapshot(
        "agent-run",
        request.to_dict(),
        [dict(item) for item in request.messages],
        [dict(item) for item in request.messages],
        {"iterations": 1, "cost": 0.01, "elapsed_seconds": 2},
        safe_to_resume=True,
        updated_at="before",
    )
    assert journal.recover_interrupted_runs("after") == ["agent-run"]
    assert journal.get_run("agent-run")["state"] == "paused"
    assert journal.resumable_runs() == ["agent-run"]
    journal.close()


def test_gateway_resumes_a_paused_agent_from_the_durable_snapshot(tmp_path: Path) -> None:
    class Runtime:
        def __init__(self):
            self.received = None

        async def run(self, run_id, request, _key, **kwargs):
            self.received = (run_id, request, kwargs)

        async def cancel(self, _run_id):
            return None

    async def scenario() -> None:
        service = GatewayService(
            GatewayConfig("t" * 43, tmp_path / "journal.sqlite3", workspace_id="workspace")
        )
        runtime = Runtime()
        service.agent_runtime = runtime
        service.configure_client_key("api-key-value")
        request = AgentRunRequest(
            "vendor/model",
            ({"role": "user", "content": "work"},),
            "agent",
            "workspace",
            approval_policy="auto",
        )
        service.journal.create_run("paused", "agent", "before")
        service.journal.save_run_snapshot(
            "paused",
            request.to_dict(),
            [dict(item) for item in request.messages],
            [dict(item) for item in request.messages],
            {"iterations": 2, "cost": 0.02, "elapsed_seconds": 3},
            safe_to_resume=True,
            updated_at="before",
        )
        service.journal.append("paused", "run.paused", "before", {"resumable": True})
        assert await service.resume_agent("paused")
        await service._tasks["paused"]
        assert runtime.received[2]["resumed"] is True
        assert runtime.received[1].approval_policy == "prompt"
        assert runtime.received[2]["budget_state"]["iterations"] == 2
        await service.close()

    asyncio.run(scenario())


def test_skills_are_deterministic_prompt_only_and_tool_bounded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "review.md").write_text(
        "---\nname: review\ndescription: Review carefully\nrequired_tools: read_file, search_text\nreferences: guide.txt\n---\nFollow the guide.",
        encoding="utf-8",
    )
    (root / "guide.txt").write_text("reference", encoding="utf-8")
    skills = discover_skills(root)
    assert [skill.name for skill in skills] == ["review"]
    rendered = render_skill_context((skills[0].to_dict(),), {"read_file", "search_text"})
    assert "SKILL [global] review" in rendered
    try:
        parse_skill(
            "---\nname: unsafe\ndescription: bad\nrequired_tools: host_shell\n---\nRun it",
            scope="global",
        )
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown tools must fail closed")


def test_prompt_commands_workspace_config_compactions_and_run_events_persist(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("test", "model", "")
    message = storage.add_message(conversation.id, "user", "goal")
    storage.save_prompt_command("review", "Review it", "Review {{args}}")
    assert expand_prompt_command(storage.list_prompt_commands()[0]["template"], "this") == "Review this"
    storage.save_workspace_config("workspace", "C:/project", {"active_skills": ["review"]})
    assert storage.workspace_config("workspace")["active_skills"] == ["review"]
    storage.save_compaction(conversation.id, message.id, [message.id], "goal summary", "model")
    assert storage.list_compactions(conversation.id)[0]["covered_message_ids"] == [message.id]

    class Event:
        run_id = "run"
        event_id = 1
        type = "tool.started"
        payload = {"request_id": "call", "tool": "read_file"}
        created_at = "now"

    storage.save_run_event(conversation.id, Event())
    assert storage.list_run_events(conversation.id)[0]["type"] == "tool.started"
    storage.close()


def test_desktop_context_removes_internal_message_ids_before_gateway() -> None:
    messages, _inspection, covered = compact_messages(
        [{"role": "user", "content": "Inspect", "_message_id": 42}],
        max_tokens=1_000,
    )

    assert messages == [{"role": "user", "content": "Inspect"}]
    assert covered == []


def test_gateway_transport_strips_desktop_only_message_metadata() -> None:
    assert _public_messages(
        [{"role": "user", "content": "Inspect", "_message_id": 42}]
    ) == [{"role": "user", "content": "Inspect"}]
