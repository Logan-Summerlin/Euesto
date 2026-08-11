from src.models import Message
from src.transcript import assemble_activities, assemble_transcript, events_for_branch


def _message(message_id: int, role: str, parent: int | None = None) -> Message:
    return Message(
        id=message_id,
        conversation_id="c",
        role=role,  # type: ignore[arg-type]
        content=str(message_id),
        created_at="2026-01-01T00:00:00+00:00",
        parent_message_id=parent,
    )


def test_activity_is_assigned_to_the_active_assistant_branch() -> None:
    activities = assemble_activities(
        [
            {
                "run_id": "run-a",
                "assistant_message_id": 3,
                "parent_message_id": 2,
            },
            {
                "run_id": "run-b",
                "assistant_message_id": 5,
                "parent_message_id": 4,
            },
        ],
        [
            {
                "run_id": "run-a",
                "event_id": 1,
                "type": "tool.requested",
                "payload": {"tool": "read_file", "request_id": "call-a"},
            },
            {
                "run_id": "run-b",
                "event_id": 1,
                "type": "tool.requested",
                "payload": {"tool": "read_file", "request_id": "call-b"},
            },
        ],
    )
    events = events_for_branch(
        [_message(1, "user"), _message(2, "assistant"), _message(3, "assistant", 2)],
        activities,
    )
    assert [event["run_id"] for event in events] == ["run-a"]


def test_legacy_activity_remains_visible_without_a_run_row() -> None:
    activities = assemble_activities(
        [],
        [
            {
                "run_id": "old",
                "event_id": 1,
                "type": "tool.requested",
                "payload": {"tool": "read_file", "request_id": "call-old"},
            }
        ],
    )
    assert events_for_branch([_message(1, "user")], activities)[0]["run_id"] == "old"


def test_publication_failure_is_distinct_from_agent_run_failure() -> None:
    activities = assemble_activities(
        [{"run_id": "run-1", "assistant_message_id": 2, "parent_message_id": 1}],
        [
            {
                "run_id": "run-1",
                "event_id": 1,
                "type": "publication.failed",
                "payload": {"code": "publication.manifest_failed"},
            }
        ],
    )

    transcript = assemble_transcript(
        [_message(1, "user"), _message(2, "assistant", 1)], activities
    )

    assert transcript[1]["activity"][0]["title"] == "Host publication unavailable"
    assert transcript[1]["activity"][0]["attention"] is True
    assert "Run failed" not in transcript[1]["activitySummary"]
