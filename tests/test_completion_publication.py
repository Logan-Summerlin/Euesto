from src.models import Message
from src.transcript import TranscriptActivity, assemble_transcript


def _message(message_id: int, role: str, parent: int | None = None) -> Message:
    return Message(
        id=message_id,
        conversation_id="conversation",
        role=role,
        content=role.title(),
        created_at="2026-01-01T00:00:00+00:00",
        parent_message_id=parent,
    )


def test_incomplete_run_activity_falls_back_to_parent_user_turn() -> None:
    user = _message(10, "user")
    activity = TranscriptActivity(
        run_id="run-1",
        assistant_message_id=None,
        parent_message_id=user.id,
        events=(
            {
                "run_id": "run-1",
                "event_id": 1,
                "type": "tool.requested",
                "payload": {"tool": "write", "request_id": "tool-1"},
            },
        ),
    )

    transcript = assemble_transcript([user], [activity])

    assert transcript[0]["messageId"] == user.id
    assert transcript[0]["activitySummary"] == "1 tool call"
    assert transcript[0]["activity"][0]["title"] == "write"


def test_completed_run_activity_stays_on_assistant_turn() -> None:
    user = _message(10, "user")
    assistant = _message(11, "assistant", parent=10)
    activity = TranscriptActivity(
        run_id="run-2",
        assistant_message_id=assistant.id,
        parent_message_id=user.id,
        events=(
            {
                "run_id": "run-2",
                "event_id": 2,
                "type": "tool.requested",
                "payload": {"tool": "edit", "request_id": "tool-2"},
            },
        ),
    )

    transcript = assemble_transcript([user, assistant], [activity])

    assert transcript[0]["activity"] == []
    assert transcript[1]["activitySummary"] == "1 tool call"
