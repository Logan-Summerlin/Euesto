from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from shared.coercion import optional_int

from .models import Message, usage_labels

# Run events that are not tool calls, with their transcript titles.
EVENT_TITLES = {
    "run.failed": "Run failed",
    "run.paused": "Run paused",
    "publication.failed": "Host publication unavailable",
    "mutation.rollback": "Changes discarded (rollback)",
    "approval.timeout": "Approval timed out",
    "checkpoint.created": "Checkpoint created",
    "checkpoint.restored": "Checkpoint restored",
    "context.compacted": "Context compacted (handoff written)",
}
# Events whose row shows text: the model's narration between tool calls and steering.
TEXT_EVENT_TYPES = frozenset({"model.narration", "input.steered"})
MAX_ACTIVITY_TEXT_CHARS = 600
ACTIVITY_EVENT_TYPES = frozenset(
    {
        "model.narration",
        "input.steered",
        "model.retry",
        "context.compacted",
        "tool.requested",
        "tool.failed",
        "approval.required",
        "approval.timeout",
        "run.failed",
        "run.paused",
        "checkpoint.created",
        "checkpoint.restored",
        "mutation.rollback",
        "publication.failed",
        # Harness Phases 3-5: the session plan, loop notes, redaction, and web requests.
        "plan.updated",
        "loop.note",
        "secret.redacted",
        "web.requested",
    }
)
LOOP_NOTE_TITLES = {
    "empty_reply": "Empty reply; asked the model to continue",
    "repeated_failure": "Repeated failure; asked the model to change approach",
}
# Events that expand their turn's activity so the user sees them.
ATTENTION_EVENT_TYPES = frozenset(
    {
        "approval.required",
        "approval.timeout",
        "tool.failed",
        "run.failed",
        "run.paused",
        "publication.failed",
        "mutation.rollback",
        "secret.redacted",
    }
)
# Payload keys loaded for activity; arguments only feed the file-name label and never reach QML.
ACTIVITY_PAYLOAD_KEYS = (
    "tool",
    "request_id",
    "arguments",
    "checkpoint_id",
    "text",
    "attempt",
    "max_attempts",
    "summary",
    "kind",
    "budget",
    "percent",
    "kinds",
    "query",
    "url",
)


@dataclass(frozen=True, slots=True)
class TranscriptActivity:
    """Run events associated with one assistant turn.

    Older databases have events but no generation-run row. Those events remain visible as
    legacy activity rather than being silently discarded during the migration.
    """

    run_id: str
    assistant_message_id: int | None
    parent_message_id: int | None
    events: tuple[dict[str, object], ...]
    legacy: bool = False


def assemble_activities(
    generation_runs: Iterable[dict[str, object]],
    run_events: Iterable[dict[str, object]],
) -> list[TranscriptActivity]:
    """Group activity events by run, in first-seen order, linked to their run's messages."""
    runs = {str(run.get("run_id")): run for run in generation_runs if run.get("run_id")}
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in run_events:
        compact = compact_activity_event(event)
        if compact is not None and compact["run_id"]:
            grouped.setdefault(str(compact["run_id"]), []).append(compact)
    activities: list[TranscriptActivity] = []
    for run_id, events in grouped.items():
        run = runs.get(run_id) or {}
        activities.append(
            TranscriptActivity(
                run_id=run_id,
                assistant_message_id=optional_int(run.get("assistant_message_id")),
                parent_message_id=optional_int(run.get("parent_message_id")),
                events=tuple(events),
                legacy=run_id not in runs,
            )
        )
    return activities


def assemble_transcript(
    messages: Iterable[Message],
    activities: Iterable[TranscriptActivity],
    *,
    live_text: str = "",
    live_events: Iterable[dict[str, object]] = (),
) -> list[dict[str, Any]]:
    """Build the semantic transcript consumed by QML.

    Activity is nested inside its assistant turn so a long agent run never creates a
    second top-level transcript. Incomplete runs without an assistant message fall back
    to their parent user turn so activity survives refresh and remains visible.
    """
    branch = list(messages)
    by_assistant: dict[int, TranscriptActivity] = {}
    by_parent: dict[int, TranscriptActivity] = {}
    legacy: list[dict[str, object]] = []
    for activity in activities:
        if activity.assistant_message_id is not None:
            by_assistant[activity.assistant_message_id] = activity
        elif activity.parent_message_id is not None:
            by_parent[activity.parent_message_id] = activity
        elif activity.legacy:
            legacy.extend(activity.events)

    transcript: list[dict[str, Any]] = []
    if legacy:
        transcript.append(
            _item("legacy-activity", "activity", legacy, legacy_summary=True, legacy=True)
        )
    for message in branch:
        if message.id is None:
            continue
        index = by_assistant if message.role == "assistant" else by_parent
        activity = index.get(message.id)
        transcript.append(
            _item(
                f"message-{message.id}",
                message.role,
                activity.events if activity else (),
                message_id=message.id,
                parent_id=message.parent_message_id,
                content=message.content,
                model=message.model_id or "",
                metadata=_message_metadata(message),
            )
        )
    live = list(live_events)
    if live_text or live:
        transcript.append(
            _item(
                "stream",
                "assistant",
                live,
                parent_id=branch[-1].id if branch else None,
                content=live_text,
                metadata="Generating…",
                streaming=True,
            )
        )
    return transcript


def _item(
    key: str,
    kind: str,
    events: Iterable[dict[str, object]],
    *,
    message_id: int | None = None,
    parent_id: int | None = None,
    content: str = "",
    model: str = "",
    metadata: str = "",
    streaming: bool = False,
    legacy: bool = False,
    legacy_summary: bool = False,
) -> dict[str, Any]:
    events = list(events)
    return {
        "key": key,
        "messageId": message_id,
        "parentMessageId": parent_id,
        "kind": kind,
        "role": kind,
        "content": content,
        "model": model,
        "metadata": metadata,
        "activity": _activity_items(events),
        "activitySummary": _activity_summary(events, legacy=legacy_summary),
        "activityExpanded": any(event.get("type") in ATTENTION_EVENT_TYPES for event in events),
        "streaming": streaming,
        "legacy": legacy,
    }


def _activity_items(events: list[dict[str, object]]) -> list[dict[str, Any]]:
    """Activity rows: one per tool call (flagged when it failed) and one per run event."""
    failed = {
        str(_payload(event).get("request_id") or "")
        for event in events
        if event.get("type") == "tool.failed"
    }
    requested: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        payload = _payload(event)
        event_type = str(event.get("type") or "event")
        request_id = str(payload.get("request_id") or "")
        if event_type == "tool.requested":
            requested.add(request_id)
            title = _activity_title(event_type, payload)
            attention = bool(request_id and request_id in failed)
        elif event_type in {"tool.failed", "approval.required"}:
            # A failure or approval of a requested call is shown on that call's row.
            if request_id and request_id in requested:
                continue
            title = _activity_title(event_type, payload)
            attention = True
        elif event_type in TEXT_EVENT_TYPES:
            text = str(payload.get("text") or "")
            if not text:
                continue
            title = f"Steering: {text}" if event_type == "input.steered" else text
            attention = False
        elif event_type == "model.retry":
            title = (
                f"Provider error; retrying (attempt {payload.get('attempt', '?')} "
                f"of {payload.get('max_attempts', '?')})"
            )
            attention = False
        elif event_type in HARNESS_EVENT_TYPES:
            title = _harness_title(event_type, payload)
            attention = event_type in ATTENTION_EVENT_TYPES
        elif event_type in EVENT_TITLES:
            title = EVENT_TITLES[event_type]
            attention = event_type in ATTENTION_EVENT_TYPES
        else:
            continue
        result.append(
            {
                "key": f"{event.get('run_id', '')}:{event.get('event_id', '')}",
                "title": title,
                "eventType": event_type,
                "rollback": event_type == "mutation.rollback",
                "attention": attention,
                "narration": event_type in TEXT_EVENT_TYPES,
            }
        )
    return result


HARNESS_EVENT_TYPES = frozenset(
    {"plan.updated", "loop.note", "secret.redacted", "web.requested"}
)


def _harness_title(event_type: str, payload: dict[str, object]) -> str:
    if event_type == "plan.updated":
        return str(payload.get("summary") or "Plan updated").capitalize()
    if event_type == "loop.note":
        kind = str(payload.get("kind") or "")
        if kind == "budget_warning":
            return (
                f"{payload.get('budget', 'Budget')} at {payload.get('percent', '?')}%; "
                "asked the agent to wrap up"
            )
        return LOOP_NOTE_TITLES.get(kind, "Loop note")
    if event_type == "secret.redacted":
        kinds = payload.get("kinds") if isinstance(payload.get("kinds"), dict) else {}
        listed = ", ".join(f"{name} ×{count}" for name, count in sorted(kinds.items()))
        return f"Redacted secrets before they reached the model: {listed or 'secret'}"
    target = str(payload.get("query") or payload.get("url") or "")[:200]
    return f"{payload.get('tool', 'web')}: {target}" if target else str(payload.get("tool"))


def _activity_summary(events: list[dict[str, object]], *, legacy: bool = False) -> str:
    if not events:
        return ""
    types = [event.get("type") for event in events]
    counts = (
        (types.count("tool.requested"), "tool call"),
        (types.count("approval.required"), "approval"),
        (
            sum(kind in {"tool.failed", "run.failed", "publication.failed"} for kind in types),
            "failure",
        ),
    )
    parts = [f"{count} {noun}{'s' if count != 1 else ''}" for count, noun in counts if count]
    if legacy:
        parts.append("legacy activity")
    return " · ".join(parts) or "Activity"


def _message_metadata(message: Message) -> str:
    parts: list[str] = []
    if message.model_id:
        parts.append(message.model_id)
    tokens = message.total_tokens
    if tokens is None and (
        message.input_tokens is not None or message.output_tokens is not None
    ):
        tokens = (message.input_tokens or 0) + (message.output_tokens or 0)
    parts += usage_labels(tokens, message.cost)
    if message.provider_id:
        parts.append(message.provider_id)
    return " · ".join(parts)


def compact_activity_event(event: dict[str, object]) -> dict[str, object] | None:
    """The QML-safe form of an activity event: no tool arguments, only a file-name label."""
    event_type = str(event.get("type") or "")
    if event_type not in ACTIVITY_EVENT_TYPES:
        return None
    payload = _payload(event)
    compact_payload = {
        key: payload[key]
        for key in ACTIVITY_PAYLOAD_KEYS
        if key != "arguments" and payload.get(key) is not None
    }
    if "text" in compact_payload:
        compact_payload["text"] = " ".join(str(compact_payload["text"]).split())[
            :MAX_ACTIVITY_TEXT_CHARS
        ]
    file_name = _file_name(str(payload.get("tool") or ""), payload)
    if file_name:
        compact_payload["file_name"] = file_name
    return {
        "run_id": str(event.get("run_id") or ""),
        "event_id": optional_int(event.get("event_id")) or 0,
        "type": event_type,
        "payload": compact_payload,
    }


def _activity_title(event_type: str, payload: dict[str, object]) -> str:
    tool = str(payload.get("tool") or "tool")
    if event_type == "approval.required" and not payload.get("tool"):
        tool = "Approval required"
    file_name = str(payload.get("file_name") or "") or _file_name(tool, payload)
    return f"{tool} · {file_name}" if file_name else tool


def _file_name(tool: str, payload: dict[str, object]) -> str | None:
    """Derive a short filename label while keeping tool arguments out of QML."""
    arguments = payload.get("arguments")
    for value in (arguments, payload) if isinstance(arguments, dict) else (payload,):
        for key in ("path", "file", "file_path", "filename"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _clean_file_name(candidate)
        operations = value.get("operations") if tool == "apply_patch" else None
        if isinstance(operations, list):
            paths = [
                str(item.get("path") or "").strip()
                for item in operations
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            ]
            if paths:
                first = _clean_file_name(paths[0])
                return f"{first} (+{len(paths) - 1} more)" if len(paths) > 1 else first
    return None


def _clean_file_name(value: str) -> str:
    return value.strip().replace("\\", "/").splitlines()[0][:180]


def _payload(event: dict[str, object]) -> dict[str, object]:
    value = event.get("payload")
    return value if isinstance(value, dict) else {}
