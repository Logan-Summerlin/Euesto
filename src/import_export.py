from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from shared.coercion import optional_string

from .models import MESSAGE_METADATA_FIELDS
from .storage import Storage

EXPORT_FORMAT = "local-openrouter-chat"
EXPORT_SCHEMA_VERSION = 3
MAX_IMPORT_MESSAGES = 100_000
MAX_IMPORT_CONTENT_BYTES = 10 * 1024 * 1024
ROLE_LABELS = {"system": "System", "user": "You", "assistant": "Assistant"}


class ImportExportError(ValueError):
    pass


def export_json(storage: Storage, conversation_id: str) -> str:
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise ImportExportError("Conversation does not exist.")
    preset = (
        storage.get_prompt_preset(conversation.prompt_preset_id)
        if conversation.prompt_preset_id
        else None
    )
    payload = {
        "format": EXPORT_FORMAT,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "conversation": asdict(conversation),
        "messages": [asdict(message) for message in storage.list_all_messages(conversation_id)],
        "prompt_preset": asdict(preset) if preset else None,
        "compactions": storage.list_compactions(conversation_id),
        "generation_runs": storage.list_generation_runs(conversation_id),
        "run_events": storage.list_run_events(conversation_id),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def import_json(storage: Storage, source: str) -> str:
    try:
        payload = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ImportExportError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ImportExportError("This is not a Local OpenRouter Chat export.")
    if payload.get("schema_version") not in {1, 2, EXPORT_SCHEMA_VERSION}:
        raise ImportExportError("Unsupported export schema version.")
    conversation_data = payload.get("conversation")
    messages = payload.get("messages")
    if not isinstance(conversation_data, dict) or not isinstance(messages, list):
        raise ImportExportError("Export is missing conversation or message data.")
    if len(messages) > MAX_IMPORT_MESSAGES:
        raise ImportExportError("Export contains too many messages.")
    ordered = _parents_first(messages)

    preset_id: str | None = None
    preset_snapshot = optional_string(conversation_data.get("prompt_preset_snapshot"))
    preset = payload.get("prompt_preset")
    if isinstance(preset, dict) and preset.get("name"):
        imported_preset = storage.save_prompt_preset(
            str(preset["name"]), str(preset.get("content") or "")
        )
        preset_id = imported_preset.id
        preset_snapshot = imported_preset.content

    conversation = storage.create_conversation(
        str(conversation_data.get("title") or "Imported conversation"),
        str(conversation_data.get("model") or "openrouter/auto"),
        str(conversation_data.get("system_prompt") or ""),
        prompt_preset_id=preset_id,
        prompt_preset_snapshot=preset_snapshot,
    )
    old_to_new: dict[str, int] = {}
    for raw in ordered:
        message = storage.add_message(
            conversation.id,
            raw["role"],
            str(raw.get("content") or ""),
            parent_message_id=_new_id(old_to_new, raw.get("parent_message_id")),
            activate=False,
            **_imported_metadata(raw),
        )
        old_to_new[str(raw["id"])] = message.id

    leaf = _new_id(old_to_new, conversation_data.get("active_leaf_id"))
    if leaf is None and old_to_new:
        leaf = list(old_to_new.values())[-1]
    if leaf is not None:
        storage.set_active_leaf(conversation.id, leaf)
    if conversation_data.get("pinned_at"):
        storage.pin_conversation(conversation.id, True)
    for raw in payload.get("compactions") or ():
        if not isinstance(raw, dict):
            continue
        covered = [
            old_to_new[str(item)]
            for item in raw.get("covered_message_ids") or ()
            if str(item) in old_to_new
        ]
        storage.save_compaction(
            conversation.id,
            _new_id(old_to_new, raw.get("branch_leaf_id")),
            covered,
            str(raw.get("summary") or ""),
            optional_string(raw.get("model_id")),
        )
    run_ids: dict[str, str] = {}
    for raw in payload.get("generation_runs") or ():
        if not isinstance(raw, dict):
            continue
        old_run_id = str(raw.get("run_id") or "")
        if not old_run_id:
            continue
        run_id = run_ids.setdefault(old_run_id, str(uuid.uuid4()))
        try:
            storage.start_generation_run(
                run_id,
                conversation.id,
                _new_id(old_to_new, raw.get("parent_message_id")),
                str(raw.get("mode") or "chat"),
                str(raw.get("model_id") or conversation.model),
                str(raw.get("started_at") or ""),
            )
            storage.finish_generation_run(
                run_id,
                str(raw.get("status") or "completed"),
                assistant_message_id=_new_id(old_to_new, raw.get("assistant_message_id")),
                error=optional_string(raw.get("error")),
                finished_at=optional_string(raw.get("finished_at")),
            )
        except (TypeError, ValueError):
            continue
    for raw in payload.get("run_events") or ():
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            continue
        old_run_id = str(raw.get("run_id") or "")
        run_id = run_ids.setdefault(old_run_id, str(uuid.uuid4()))
        try:
            storage.save_run_event(
                conversation.id,
                SimpleNamespace(
                    run_id=run_id,
                    event_id=int(raw.get("event_id") or 0),
                    type=str(raw.get("type") or ""),
                    payload=dict(raw["payload"]),
                    created_at=str(raw.get("created_at") or ""),
                ),
            )
        except (TypeError, ValueError):
            continue
    return conversation.id


def export_markdown(storage: Storage, conversation_id: str) -> str:
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise ImportExportError("Conversation does not exist.")
    summary = storage.usage_summary(conversation_id)
    lines = [
        f"# {conversation.title}",
        "",
        f"- Model: `{conversation.model}`",
        f"- Created: {conversation.created_at}",
        f"- Updated: {conversation.updated_at}",
        f"- Tokens: {summary['input_tokens'] + summary['output_tokens']:,}",
        f"- Cost: ${summary['cost']:.6f}",
        "",
    ]
    for message in storage.list_messages(conversation_id):
        metadata = {key: value for key, value in asdict(message).items() if key != "content"}
        lines.extend(
            [
                f"<!-- local-openrouter-chat-message {json.dumps(metadata, sort_keys=True)} -->",
                f"## {ROLE_LABELS[message.role]}",
                "",
                message.content,
                "",
                "<!-- /local-openrouter-chat-message -->",
                "",
            ]
        )
    events = storage.list_run_events(conversation_id)
    if events:
        lines.extend(["## Agent activity", ""])
        for event in events:
            if str(event["type"]).startswith(("tool.", "context.", "skill.")):
                payload = json.dumps(event["payload"], ensure_ascii=False)[:1000]
                lines.append(f"- `{event['type']}` — `{payload}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def import_markdown(storage: Storage, source: str, *, title: str = "Imported chat") -> str:
    title_match = re.search(r"^#\s+(.+)$", source, re.MULTILINE)
    conversation = storage.create_conversation(
        title_match.group(1).strip() if title_match else title,
        "openrouter/auto",
        "",
    )
    pattern = re.compile(
        r"<!-- local-openrouter-chat-message (?P<meta>\{.*?\}) -->\s*"
        r"##\s+(?P<label>System|You|Assistant)\s*\n(?P<content>.*?)\n"
        r"<!-- /local-openrouter-chat-message -->",
        re.DOTALL,
    )
    matches = list(pattern.finditer(source))
    if not matches:
        storage.add_message(conversation.id, "user", source.strip())
        return conversation.id
    id_map: dict[str, int] = {}
    roles = {label: role for role, label in ROLE_LABELS.items()}
    for match in matches:
        try:
            metadata = json.loads(match.group("meta"))
        except json.JSONDecodeError as exc:
            storage.delete_conversation(conversation.id)
            raise ImportExportError("Invalid message metadata in Markdown export.") from exc
        old_parent = metadata.get("parent_message_id")
        if old_parent is not None and str(old_parent) not in id_map:
            storage.delete_conversation(conversation.id)
            raise ImportExportError("Markdown message references a missing parent.")
        message = storage.add_message(
            conversation.id,
            roles[match.group("label")],
            match.group("content").strip(),
            parent_message_id=_new_id(id_map, old_parent),
            **_imported_metadata(metadata),
        )
        id_map[str(metadata.get("id"))] = message.id
    return conversation.id


def export_to_file(storage: Storage, conversation_id: str, path: Path) -> None:
    content = (
        export_json(storage, conversation_id)
        if path.suffix.lower() == ".json"
        else export_markdown(storage, conversation_id)
    )
    path.write_text(content, encoding="utf-8")


def import_from_file(storage: Storage, path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return import_json(storage, content)
    return import_markdown(storage, content, title=path.stem)


def _parents_first(messages: list[object]) -> list[dict[str, Any]]:
    """Validate an exported message tree and order it so every parent precedes its children."""
    by_id: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for raw in messages:
        if not isinstance(raw, dict):
            raise ImportExportError("Message entry must be an object.")
        if raw.get("id") is None:
            raise ImportExportError("Message is missing an ID.")
        if str(raw["id"]) in by_id:
            raise ImportExportError("Message IDs must be unique.")
        if raw.get("role") not in ROLE_LABELS:
            raise ImportExportError("Message has an invalid role.")
        total_bytes += len(str(raw.get("content") or "").encode("utf-8"))
        if total_bytes > MAX_IMPORT_CONTENT_BYTES:
            raise ImportExportError("Export message content is too large.")
        by_id[str(raw["id"])] = raw
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    for message_id in by_id:
        chain: dict[str, None] = {}
        current: str | None = message_id
        while current is not None and current not in placed:
            if current not in by_id:
                raise ImportExportError("Message tree references a missing parent.")
            if current in chain:
                raise ImportExportError("Message tree contains a cycle.")
            chain[current] = None
            parent = by_id[current].get("parent_message_id")
            current = None if parent is None else str(parent)
        for item in reversed(chain):
            placed.add(item)
            ordered.append(by_id[item])
    return ordered


def _new_id(id_map: dict[str, int], old_id: object) -> int | None:
    return None if old_id is None else id_map.get(str(old_id))


def _imported_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    values = {key: coerce(raw.get(key)) for key, coerce in MESSAGE_METADATA_FIELDS.items()}
    values["created_at"] = optional_string(raw.get("created_at"))
    return values
