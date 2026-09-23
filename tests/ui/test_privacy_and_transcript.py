from pathlib import Path
from types import SimpleNamespace

import pytest

from server.openrouter.client import build_payload
from shared.events import EventEnvelope
from shared.requests import AgentRunRequest, ChatMessage, ChatRequest
from src import settings
from src.models import Message
from src.storage import Storage
from src.transcript import (
    ACTIVITY_EVENT_TYPES,
    ACTIVITY_PAYLOAD_KEYS,
    assemble_activities,
    assemble_transcript,
)
from src.workers import AgentWorker


def _message(message_id: int, role: str, parent: int | None = None) -> Message:
    return Message(
        id=message_id,
        conversation_id="conversation",
        role=role,  # type: ignore[arg-type]
        content=f"message {message_id}",
        created_at="2026-08-09T00:00:00+00:00",
        parent_message_id=parent,
        model_id="model/a" if role == "assistant" else None,
    )


def test_qml_transcript_keeps_fifteen_tool_calls_inside_one_assistant_turn() -> None:
    events: list[dict[str, object]] = []
    event_id = 1
    for iteration in range(1, 6):
        events.append(
            {
                "run_id": "run-1",
                "event_id": event_id,
                "type": "model.requested",
                "payload": {"iteration": iteration},
            }
        )
        event_id += 1
        for call in range(3):
            request_id = f"{iteration}-{call}"
            events.extend(
                [
                    {
                        "run_id": "run-1",
                        "event_id": event_id,
                        "type": "tool.requested",
                        "payload": {
                            "request_id": request_id,
                            "tool": "read_file",
                            "arguments": {"path": f"file-{request_id}.py"},
                        },
                    },
                    {
                        "run_id": "run-1",
                        "event_id": event_id + 1,
                        "type": "tool.completed",
                        "payload": {
                            "request_id": request_id,
                            "tool": "read_file",
                            "ok": True,
                        },
                    },
                ]
            )
            event_id += 2

    activities = assemble_activities(
        [
            {
                "run_id": "run-1",
                "parent_message_id": 1,
                "assistant_message_id": 2,
            }
        ],
        events,
    )
    transcript = assemble_transcript(
        [_message(1, "user"), _message(2, "assistant", 1)], activities
    )

    assert [item["kind"] for item in transcript] == ["user", "assistant"]
    assistant = transcript[1]
    assert len(assistant["activity"]) == 15
    assert {
        item["title"].split(" · ", 1)[0] for item in assistant["activity"]
    } == {"read_file"}
    assert all("detail" not in item for item in assistant["activity"])
    assert "15 tool calls" in assistant["activitySummary"]
    assert assistant["activityExpanded"] is False


def test_failed_activity_reopens_by_default() -> None:
    activities = assemble_activities(
        [{"run_id": "run-1", "assistant_message_id": 2, "parent_message_id": 1}],
        [
            {
                "run_id": "run-1",
                "event_id": 1,
                "type": "tool.failed",
                "payload": {"tool": "read_file", "message": "blocked"},
            }
        ],
    )
    transcript = assemble_transcript(
        [_message(1, "user"), _message(2, "assistant", 1)], activities
    )
    assert transcript[1]["activityExpanded"] is True
    assert transcript[1]["activity"][0]["attention"] is True


def test_chat_provider_privacy_defaults_to_deny_collection() -> None:
    request = ChatRequest(
        model="model/a",
        messages=(ChatMessage("user", "hello"),),
    )
    assert build_payload(request)["provider"] == {
        "data_collection": "deny",
        "zdr": False,
    }


def test_provider_zdr_and_explicit_collection_opt_in_round_trip() -> None:
    request = ChatRequest(
        model="model/a",
        messages=(ChatMessage("user", "hello"),),
        provider_preferences={"data_collection": "allow", "zdr": True},
    )
    payload = build_payload(ChatRequest.from_dict(request.to_dict()))
    assert payload["provider"] == {"data_collection": "allow", "zdr": True}

    agent = AgentRunRequest(
        model="model/a",
        messages=({"role": "user", "content": "hello"},),
        mode="plan",
        workspace_id="workspace",
        provider_preferences={"data_collection": "deny", "zdr": True},
    )
    assert AgentRunRequest.from_dict(agent.to_dict()).provider_preferences["zdr"] is True


def test_unknown_provider_privacy_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="Unknown provider privacy"):
        ChatRequest(
            model="model/a",
            messages=(ChatMessage("user", "hello"),),
            provider_preferences={"store_everything": True},
        )


def test_transcript_never_loads_tool_outputs_into_qml_activity(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("Chat", "model/a", "System")
    for event_id, event_type, payload in (
        (1, "tool.requested", {"request_id": "call-1", "tool": "read_file", "arguments": {"path": "large.py"}}),
        (2, "tool.completed", {"request_id": "call-1", "tool": "read_file", "output": "x" * 500_000}),
    ):
        storage.save_run_event(conversation.id, SimpleNamespace(run_id="run-1", event_id=event_id, type=event_type, payload=payload, created_at="2026-08-09T00:00:00+00:00"))

    events = storage.list_run_events(conversation.id, event_types=ACTIVITY_EVENT_TYPES, payload_keys=ACTIVITY_PAYLOAD_KEYS)
    assert len(events) == 1
    event = events[0]
    assert event["run_id"] == "run-1"
    assert event["event_id"] == 1
    assert event["type"] == "tool.requested"
    assert event["created_at"] == "2026-08-09T00:00:00+00:00"
    assert event["payload"]["request_id"] == "call-1"
    assert event["payload"]["tool"] == "read_file"
    assert set(event["payload"]).issubset(ACTIVITY_PAYLOAD_KEYS)

    transcript = assemble_transcript([], assemble_activities([], events))
    assert len(transcript) == 1
    assert transcript[0]["activity"][0]["title"].startswith("read_file")
    assert "arguments" not in repr(transcript)
    assert "output" not in repr(transcript)
    stored_tool_result = storage.list_run_events(conversation.id)[1]["payload"]
    assert "output" not in stored_tool_result
    assert stored_tool_result["truncated_for_desktop_history"] is True
    storage.close()


def test_agent_worker_delivers_one_answer_and_drops_large_transport_events() -> None:
    events = (
        EventEnvelope(1, "run-1", "model.delta", "2026-08-09T00:00:00Z", {"text": "hel"}),
        EventEnvelope(2, "run-1", "model.delta", "2026-08-09T00:00:01Z", {"text": "lo"}),
        EventEnvelope(3, "run-1", "tool.output", "2026-08-09T00:00:02Z", {"request_id": "call-1", "tool": "read_file", "output": "x" * 50_000}),
        EventEnvelope(4, "run-1", "tool.requested", "2026-08-09T00:00:03Z", {"request_id": "call-2", "tool": "write_file"}),
        EventEnvelope(5, "run-1", "usage.updated", "2026-08-09T00:00:04Z", {"tokens": 9}),
    )
    worker = AgentWorker(SimpleNamespace(cancel=lambda: None), lambda _stop: iter(events))
    chunks: list[str] = []
    desktop_events: list[EventEnvelope] = []
    completions: list[tuple[dict[str, object], bool]] = []
    worker.chunk.connect(chunks.append)
    worker.eventReceived.connect(desktop_events.append)
    worker.complete.connect(lambda usage, cancelled: completions.append((usage, cancelled)))
    worker.run()
    assert chunks == ["hello"]
    assert [event.type for event in desktop_events] == ["tool.requested"]
    assert completions == [({"tokens": 9, "run_id": "run-1"}, False)]


def test_agent_worker_flushes_partial_output_before_reporting_failure() -> None:
    events = (
        EventEnvelope(1, "run-1", "model.delta", "2026-08-09T00:00:00Z", {"text": "partial"}),
        EventEnvelope(2, "run-1", "run.failed", "2026-08-09T00:00:01Z", {"message": "boom"}),
    )
    worker = AgentWorker(SimpleNamespace(cancel=lambda: None), lambda _stop: iter(events))
    chunks: list[str] = []
    failures: list[str] = []
    worker.chunk.connect(chunks.append)
    worker.failed.connect(failures.append)
    worker.run()
    assert (chunks, failures, worker.last_usage) == (["partial"], ["boom"], {})


def test_active_local_gateway_token_is_discovered_without_manual_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_data_dir", lambda: tmp_path)
    session_dir = tmp_path / "gateway-session"
    session_dir.mkdir()
    token = "a" * 43
    (session_dir / "gateway_token.txt").write_text(token, encoding="utf-8")
    assert settings.get_gateway_session_token() == token
