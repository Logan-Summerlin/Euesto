from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import Message

ACTIVITY_EVENT_TYPES = frozenset(
    {
        "tool.requested",
        "tool.failed",
        "approval.required",
        "run.failed",
        "run.paused",
        "checkpoint.created",
        "checkpoint.restored",
        "publication.failed",
    }
)
ACTIVITY_PAYLOAD_KEYS = ("tool", "request_id", "arguments", "checkpoint_id")


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
    runs = {str(run.get("run_id")): run for run in generation_runs if run.get("run_id")}
    grouped: dict[str, list[dict[str, object]]] = {}
    order: list[str] = []
    for event in run_events:
        compact = compact_activity_event(event)
        if compact is None:
            continue
        run_id = str(compact.get("run_id") or "")
        if not run_id:
            continue
        if run_id not in grouped:
            grouped[run_id] = []
            order.append(run_id)
        grouped[run_id].append(compact)

    activities: list[TranscriptActivity] = []
    for run_id in order:
        run = runs.get(run_id)
        assistant_id = _optional_int(run.get("assistant_message_id")) if run else None
        parent_id = _optional_int(run.get("parent_message_id")) if run else None
        activities.append(
            TranscriptActivity(
                run_id=run_id,
                assistant_message_id=assistant_id,
                parent_message_id=parent_id,
                events=tuple(grouped[run_id]),
                legacy=run is None,
            )
        )
    return activities


def events_for_branch(
    messages: Iterable[Message], activities: Iterable[TranscriptActivity]
) -> list[dict[str, object]]:
    """Flatten only activity belonging to the active message branch."""
    message_ids = {message.id for message in messages if message.id is not None}
    selected: list[dict[str, object]] = []
    for activity in activities:
        associated = activity.assistant_message_id in message_ids
        if activity.assistant_message_id is None:
            associated = activity.legacy or activity.parent_message_id in message_ids
        if associated:
            selected.extend(activity.events)
    return selected


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
    activity_by_assistant: dict[int, TranscriptActivity] = {}
    activity_by_parent: dict[int, TranscriptActivity] = {}
    legacy: list[TranscriptActivity] = []
    for activity in activities:
        if activity.assistant_message_id is not None:
            activity_by_assistant[activity.assistant_message_id] = activity
        elif activity.parent_message_id is not None:
            activity_by_parent[activity.parent_message_id] = activity
        elif activity.legacy:
            legacy.append(activity)

    transcript: list[dict[str, Any]] = []
    for message in branch:
        if message.id is None:
            continue
        activity = (
            activity_by_assistant.get(message.id)
            if message.role == "assistant"
            else activity_by_parent.get(message.id)
        )
        transcript.append(_message_item(message, activity))

    if legacy:
        events = [event for activity in legacy for event in activity.events]
        transcript.insert(
            0,
            {
                "key": "legacy-activity",
                "messageId": None,
                "parentMessageId": None,
                "kind": "activity",
                "role": "activity",
                "content": "",
                "model": "",
                "metadata": "",
                "activity": _activity_items(events),
                "activitySummary": _activity_summary(events, legacy=True),
                "activityExpanded": _activity_needs_attention(events),
                "streaming": False,
                "legacy": True,
            },
        )

    live = list(live_events)
    if live_text or live:
        events = live
        parent_id = branch[-1].id if branch else None
        transcript.append(
            {
                "key": "stream",
                "messageId": None,
                "parentMessageId": parent_id,
                "kind": "assistant",
                "role": "assistant",
                "content": live_text,
                "model": "",
                "metadata": "Generating…",
                "activity": _activity_items(events),
                "activitySummary": _activity_summary(events),
                "activityExpanded": _activity_needs_attention(events),
                "streaming": True,
                "legacy": False,
            }
        )
    return transcript


def _message_item(message: Message, activity: TranscriptActivity | None) -> dict[str, Any]:
    events = list(activity.events) if activity else []
    metadata = _message_metadata(message)
    return {
        "key": f"message-{message.id}",
        "messageId": message.id,
        "parentMessageId": message.parent_message_id,
        "kind": message.role,
        "role": message.role,
        "content": message.content,
        "model": message.model_id or "",
        "metadata": metadata,
        "activity": _activity_items(events),
        "activitySummary": _activity_summary(events),
        "activityExpanded": _activity_needs_attention(events),
        "streaming": False,
        "legacy": False,
    }


def _activity_items(events: Iterable[dict[str, object]]) -> list[dict[str, Any]]:
    values = list(events)
    failed_request_ids = {
        str(_payload(event).get("request_id") or "")
        for event in values
        if event.get("type") == "tool.failed"
    }
    requested_request_ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in values:
        payload = _payload(event)
        event_type = str(event.get("type") or "event")
        request_id = str(payload.get("request_id") or "")
        if event_type == "tool.requested":
            requested_request_ids.add(request_id)
            title = _activity_title(event_type, payload)
            attention = bool(request_id and request_id in failed_request_ids)
        elif event_type == "tool.failed":
            if request_id and request_id in requested_request_ids:
                continue
            title = _activity_title(event_type, payload)
            attention = True
        elif event_type == "approval.required":
            if request_id and request_id in requested_request_ids:
                continue
            title = _activity_title(event_type, payload)
            attention = True
        elif event_type in {"run.failed", "run.paused", "publication.failed"}:
            title = (
                "Run failed"
                if event_type == "run.failed"
                else "Run paused"
                if event_type == "run.paused"
                else "Host publication unavailable"
            )
            attention = True
        elif event_type in {"checkpoint.created", "checkpoint.restored"}:
            title = (
                "Checkpoint restored"
                if event_type == "checkpoint.restored"
                else "Checkpoint created"
            )
            attention = False
        else:
            continue
        result.append(
            {
                "key": f"{event.get('run_id', '')}:{event.get('event_id', '')}",
                "title": title,
                "attention": attention,
            }
        )
    return result


def _activity_summary(events: Iterable[dict[str, object]], *, legacy: bool = False) -> str:
    values = list(events)
    if not values:
        return ""
    tools = sum(1 for event in values if event.get("type") == "tool.requested")
    failures = sum(
        1
        for event in values
        if event.get("type") in {"tool.failed", "run.failed", "publication.failed"}
    )
    approvals = sum(1 for event in values if event.get("type") == "approval.required")
    parts: list[str] = []
    if tools:
        parts.append(f"{tools} tool call{'s' if tools != 1 else ''}")
    if approvals:
        parts.append(f"{approvals} approval{'s' if approvals != 1 else ''}")
    if failures:
        parts.append(f"{failures} failure{'s' if failures != 1 else ''}")
    if legacy:
        parts.append("legacy activity")
    return " · ".join(parts) or "Activity"


def _activity_needs_attention(events: Iterable[dict[str, object]]) -> bool:
    return any(
        event.get("type")
        in {
            "approval.required",
            "tool.failed",
            "run.failed",
            "run.paused",
            "publication.failed",
        }
        for event in events
    )


def _message_metadata(message: Message) -> str:
    parts: list[str] = []
    if message.model_id:
        parts.append(message.model_id)
    tokens = message.total_tokens
    if tokens is None and (
        message.input_tokens is not None or message.output_tokens is not None
    ):
        tokens = (message.input_tokens or 0) + (message.output_tokens or 0)
    if tokens:
        parts.append(f"{tokens:,} tokens")
    if message.cost is not None:
        parts.append(f"${message.cost:.6f}")
    if message.provider_id:
        parts.append(message.provider_id)
    return " · ".join(parts)


def compact_activity_event(event: dict[str, object]) -> dict[str, object] | None:
    event_type = str(event.get("type") or "")
    if event_type not in ACTIVITY_EVENT_TYPES:
        return None
    payload = _payload(event)
    compact_payload = {
        key: payload[key]
        for key in ACTIVITY_PAYLOAD_KEYS
        if key in payload and key != "arguments" and payload[key] is not None
    }
    file_name = _file_name(str(payload.get("tool") or ""), payload)
    if file_name:
        compact_payload["file_name"] = file_name
    return {
        "run_id": str(event.get("run_id") or ""),
        "event_id": _optional_int(event.get("event_id")) or 0,
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
    values = [payload]
    if isinstance(arguments, dict):
        values.insert(0, arguments)
    for value in values:
        for key in ("path", "file", "file_path", "filename"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _clean_file_name(candidate)
        edits = value.get("edits")
        if isinstance(edits, list):
            paths = [
                str(edit.get("path") or "").strip()
                for edit in edits
                if isinstance(edit, dict) and str(edit.get("path") or "").strip()
            ]
            if paths:
                first = _clean_file_name(paths[0])
                return f"{first} (+{len(paths) - 1} more)" if len(paths) > 1 else first
        patch = value.get("patch")
        if isinstance(patch, str) and tool in {"apply_patch", "file_edit"}:
            match = re.search(
                r"\*\*\* (?:Update|Add|Delete) File:\s*([^\s]+)",
                patch,
            )
            if match:
                return _clean_file_name(match.group(1))
        if tool in {"run_command", "file_run"}:
            command_values = [value.get("command"), value.get("executable")]
            command_values.extend(value.get("arguments") or [])
            for command in command_values:
                if not isinstance(command, str):
                    continue
                match = re.search(
                    r"(?<![\w.-])([^\s\"']+\.(?:py|js|ts|tsx|jsx|qml|json|md|txt))(?![\w.-])",
                    command,
                    re.IGNORECASE,
                )
                if match:
                    return _clean_file_name(match.group(1))
    return None


def _clean_file_name(value: str) -> str:
    value = value.strip().replace("\\", "/")
    value = value.splitlines()[0]
    return value[:180]


def _payload(event: dict[str, object]) -> dict[str, object]:
    value = event.get("payload")
    return value if isinstance(value, dict) else {}


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
