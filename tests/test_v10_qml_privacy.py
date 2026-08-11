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

ROOT = Path(__file__).resolve().parents[1]


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


def test_v1_uses_qml_as_the_only_main_window_and_packages_assets() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    spec_source = (ROOT / "build" / "chatbot.spec").read_text(encoding="utf-8")

    assert "QQmlApplicationEngine" in app_source
    assert 'QQuickStyle.setStyle("Basic")' in app_source
    assert app_source.index('QQuickStyle.setStyle("Basic")') < app_source.index(
        "QApplication(sys.argv)"
    )
    assert "src.main_window" not in app_source
    assert not (ROOT / "src" / "main_window.py").exists()
    assert not (ROOT / "src" / "chat_view.py").exists()
    for name in ("Main.qml", "Sidebar.qml", "Transcript.qml", "Composer.qml"):
        assert (ROOT / "qml" / name).is_file()
    assert 'project_root / "qml"' in spec_source


def test_settings_loads_optional_values_without_assigning_undefined() -> None:
    source = (ROOT / "qml" / "Main.qml").read_text(encoding="utf-8")

    assert "function optionalText(value)" in source
    assert "temperature.text = optionalText(options.temperature)" in source
    assert "topP.text = optionalText(options.top_p)" in source
    assert 'Shortcut { sequence: "Ctrl+Comma"; onActivated: settingsDialog.openAndLoad() }' in source
    assert 'text: "Auto"' in source
    assert "backend.requestAutoMode(checked)" in source


def test_transcript_uses_native_style_safe_activity_button() -> None:
    source = (ROOT / "qml" / "Transcript.qml").read_text(encoding="utf-8")

    activity_button = source[
        source.index("id: activityButton") : source.index(
            "id: activityLoader", source.index("id: activityButton")
        )
    ]
    assert "contentItem:" not in activity_button
    assert "palette.buttonText: root.mutedColor" in activity_button


def test_transcript_uses_exact_height_scrolling_and_incremental_rows() -> None:
    source = (ROOT / "qml" / "Transcript.qml").read_text(encoding="utf-8")

    assert "Flickable {" in source
    assert "ListView {" not in source
    assert "model: backend.transcriptModel" in source
    assert "required property var rowData" in source
    assert 'objectName: "transcriptMessageBody"' in source
    assert "height: visible ? Math.ceil(contentHeight) : 0" in source
    assert "Layout.preferredHeight: implicitHeight" not in source
    assert "Layout.maximumHeight: implicitHeight" not in source
    assert "verticalAlignment: TextEdit.AlignTop" in source
    assert "contentHeight: Math.ceil(transcriptColumn.implicitHeight)" in source
    assert "height: Math.ceil(content.implicitHeight + 24)" in source
    assert "width: parent.width" in source
    assert "height: active ? implicitHeight : 0" in source
    assert "activityOverrides" in source
    assert "Loader {" in source


def test_tool_checkboxes_use_compact_indicators() -> None:
    source = (ROOT / "qml" / "Composer.qml").read_text(encoding="utf-8")

    assert "component ToolCheckBox: CheckBox" in source
    assert "width: 13" in source
    assert "height: 13" in source
    assert "leftPadding: 0" in source
    assert "leftPadding: control.indicator.width + control.spacing" in source
    assert "contentItem: Label" in source
    assert source.count("ToolCheckBox {") == 3


def test_transcript_never_loads_tool_outputs_into_qml_activity(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "chat.sqlite3")
    conversation = storage.create_conversation("Chat", "model/a", "System")
    for event_id, event_type, payload in (
        (
            1,
            "tool.requested",
            {
                "request_id": "call-1",
                "tool": "read_file",
                "arguments": {"path": "large.py"},
            },
        ),
        (
            2,
            "tool.completed",
            {"request_id": "call-1", "tool": "read_file", "output": "x" * 500_000},
        ),
    ):
        storage.save_run_event(
            conversation.id,
            SimpleNamespace(
                run_id="run-1",
                event_id=event_id,
                type=event_type,
                payload=payload,
                created_at="2026-08-09T00:00:00+00:00",
            ),
        )

    events = storage.list_run_events(
        conversation.id,
        event_types=ACTIVITY_EVENT_TYPES,
        payload_keys=ACTIVITY_PAYLOAD_KEYS,
    )
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


def test_responses_are_buffered_and_optional_qml_values_are_guarded() -> None:
    backend_source = (ROOT / "src" / "qml_backend.py").read_text(encoding="utf-8")
    worker_source = (ROOT / "src" / "workers.py").read_text(encoding="utf-8")
    transcript_source = (ROOT / "qml" / "Transcript.qml").read_text(encoding="utf-8")
    main_source = (ROOT / "qml" / "Main.qml").read_text(encoding="utf-8")
    chunk_handler = backend_source[
        backend_source.index("def onStreamChunk") : backend_source.index(
            "def onStreamComplete"
        )
    ]

    assert "_refresh_transcript" not in chunk_handler
    assert 'live_text=""' in backend_source
    assert 'if event_type not in {"model.delta", "tool.output", "tool.completed"}' in backend_source
    assert 'self.chunk.emit("".join(chunks))' in worker_source
    assert 'if event.type in {"tool.output", "tool.completed"}' in worker_source
    assert "card.value.streaming === true" in transcript_source
    assert 'String(card.value.metadata || "").length' in transcript_source
    assert "policy: ScrollBar.AlwaysOn" in transcript_source
    assert "minimumSize: 0.08" in transcript_source
    assert "self._schedule_transcript_refresh()" in backend_source
    assert "TranscriptListModel" in backend_source
    assert "enabled: presetBox.currentIndex >= 0" in main_source


def test_agent_worker_delivers_one_answer_and_drops_large_transport_events() -> None:
    events = (
        EventEnvelope(1, "run-1", "model.delta", "2026-08-09T00:00:00Z", {"text": "hel"}),
        EventEnvelope(2, "run-1", "model.delta", "2026-08-09T00:00:01Z", {"text": "lo"}),
        EventEnvelope(
            3,
            "run-1",
            "tool.output",
            "2026-08-09T00:00:02Z",
            {"request_id": "call-1", "tool": "read_file", "output": "x" * 50_000},
        ),
        EventEnvelope(
            4,
            "run-1",
            "tool.requested",
            "2026-08-09T00:00:03Z",
            {"request_id": "call-2", "tool": "write_file"},
        ),
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


def test_gateway_token_is_used_from_memory_immediately_after_save() -> None:
    source = (ROOT / "src" / "qml_backend.py").read_text(encoding="utf-8")

    assert 'get_gateway_session_token() or get_gateway_token() or ""' in source
    assert "self._gateway_token = new_token" in source
    assert '"hasToken": bool(self._gateway_token)' in source


def test_active_local_gateway_token_is_discovered_without_manual_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_data_dir", lambda: tmp_path)
    session_dir = tmp_path / "gateway-session"
    session_dir.mkdir()
    token = "a" * 43
    (session_dir / "gateway_token.txt").write_text(token, encoding="utf-8")

    assert settings.get_gateway_session_token() == token


def test_catalog_autorefresh_waits_for_gateway_health_and_stays_nonblocking() -> None:
    source = (ROOT / "src" / "qml_backend.py").read_text(encoding="utf-8")

    assert "QTimer.singleShot(250, self.refreshCatalog)" not in source
    assert "result.state in {HealthState.READY, HealthState.DEGRADED}" in source
    assert "self._start_catalog_refresh(report_errors=False)" in source
