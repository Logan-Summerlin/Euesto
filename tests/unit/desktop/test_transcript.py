from src.models import Message
from src.transcript import (
    TranscriptActivity,
    assemble_activities,
    assemble_transcript,
    compact_activity_event,
)


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
    transcript = assemble_transcript(
        [_message(1, "user"), _message(2, "assistant"), _message(3, "assistant", 2)],
        activities,
    )
    with_activity = [
        (item["messageId"], item["activity"]) for item in transcript if item["activity"]
    ]
    assert [
        (message_id, [row["key"] for row in rows]) for message_id, rows in with_activity
    ] == [(3, ["run-a:1"])]


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
    transcript = assemble_transcript([_message(1, "user")], activities)
    assert transcript[0]["key"] == "legacy-activity" and transcript[0]["legacy"] is True
    assert [row["key"] for row in transcript[0]["activity"]] == ["old:1"]


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


def _tool_activity(
    run_id: str, assistant_id: int | None, parent_id: int, tool: str
) -> TranscriptActivity:
    event = {
        "run_id": run_id,
        "event_id": 1,
        "type": "tool.requested",
        "payload": {"tool": tool, "request_id": f"{run_id}-call"},
    }
    return TranscriptActivity(
        run_id=run_id,
        assistant_message_id=assistant_id,
        parent_message_id=parent_id,
        events=(event,),
    )


def test_incomplete_run_activity_falls_back_to_parent_user_turn() -> None:
    transcript = assemble_transcript(
        [_message(10, "user")], [_tool_activity("run-1", None, 10, "write")]
    )

    assert transcript[0]["messageId"] == 10
    assert transcript[0]["activitySummary"] == "1 tool call"
    assert transcript[0]["activity"][0]["title"] == "write"


def test_completed_run_activity_stays_on_assistant_turn() -> None:
    transcript = assemble_transcript(
        [_message(10, "user"), _message(11, "assistant", 10)],
        [_tool_activity("run-2", 11, 10, "edit")],
    )

    assert transcript[0]["activity"] == []
    assert transcript[1]["activitySummary"] == "1 tool call"


def test_narration_steering_and_retries_appear_as_activity_rows() -> None:
    events = [
        {
            "run_id": "run-a",
            "event_id": event_id,
            "type": event_type,
            "payload": payload,
        }
        for event_id, event_type, payload in (
            (1, "model.narration", {"text": "Reading   the config\nfirst."}),
            (2, "input.steered", {"text": "Use the v2 API", "chars": 14}),
            (3, "model.retry", {"attempt": 2, "max_attempts": 3}),
            (4, "context.compacted", {"tokens_after": 1000}),
            (5, "model.narration", {"text": ""}),
        )
    ]
    compact = [compact_activity_event(event) for event in events]
    assert compact[0]["payload"]["text"] == "Reading the config first."
    long_text = compact_activity_event(
        {
            "run_id": "r",
            "event_id": 9,
            "type": "model.narration",
            "payload": {"text": "x" * 5000},
        }
    )
    assert len(long_text["payload"]["text"]) == 600

    activities = assemble_activities(
        [{"run_id": "run-a", "assistant_message_id": 2, "parent_message_id": 1}],
        [item for item in compact if item is not None],
    )
    transcript = assemble_transcript(
        [_message(1, "user"), _message(2, "assistant", 1)], activities
    )
    rows = next(item["activity"] for item in transcript if item["activity"])
    assert [(row["title"], row["narration"]) for row in rows] == [
        ("Reading the config first.", True),
        ("Steering: Use the v2 API", True),
        ("Provider error; retrying (attempt 2 of 3)", False),
        ("Context compacted (handoff written)", False),
    ]


def test_harness_events_have_readable_activity_rows() -> None:
    from src.transcript import _activity_items

    events = [
        {"type": "plan.updated", "payload": {"summary": "plan: 1/3 done"}},
        {
            "type": "loop.note",
            "payload": {"kind": "budget_warning", "budget": "Cost", "percent": 85},
        },
        {"type": "loop.note", "payload": {"kind": "repeated_failure"}},
        {"type": "secret.redacted", "payload": {"kinds": {"github_token": 2}}},
        {
            "type": "web.requested",
            "payload": {"tool": "web_search", "query": "pytest fixtures"},
        },
    ]
    rows = _activity_items(events)  # type: ignore[arg-type]
    assert [row["title"] for row in rows] == [
        "Plan: 1/3 done",
        "Cost at 85%; asked the agent to wrap up",
        "Repeated failure; asked the model to change approach",
        "Redacted secrets before they reached the model: github_token ×2",
        "web_search: pytest fixtures",
    ]
    # A redaction always draws attention; the others are routine progress.
    assert [row["attention"] for row in rows] == [False, False, False, True, False]
