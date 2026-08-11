from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from shared.coercion import optional_float, optional_int, optional_string

from .models import Message
from .storage import Storage

EXPORT_FORMAT = "local-openrouter-chat"
EXPORT_SCHEMA_VERSION = 3
MAX_IMPORT_MESSAGES = 100_000
MAX_IMPORT_CONTENT_BYTES = 10 * 1024 * 1024


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
    _validate_json_messages(messages)

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
    pending = list(messages)
    total_bytes = 0
    while pending:
        progressed = False
        for raw in list(pending):
            if not isinstance(raw, dict):
                raise ImportExportError("Message entry must be an object.")
            old_id = str(raw.get("id"))
            old_parent = raw.get("parent_message_id")
            if old_parent is not None and str(old_parent) not in old_to_new:
                continue
            role = raw.get("role")
            if role not in {"system", "user", "assistant"}:
                raise ImportExportError("Message has an invalid role.")
            content = str(raw.get("content") or "")
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > MAX_IMPORT_CONTENT_BYTES:
                raise ImportExportError("Export message content is too large.")
            message = storage.add_message(
                conversation.id,
                role,
                content,
                parent_message_id=(
                    old_to_new[str(old_parent)] if old_parent is not None else None
                ),
                activate=False,
                model_id=optional_string(raw.get("model_id")),
                provider_id=optional_string(raw.get("provider_id")),
                finish_reason=optional_string(raw.get("finish_reason")),
                input_tokens=optional_int(raw.get("input_tokens")),
                output_tokens=optional_int(raw.get("output_tokens")),
                cached_tokens=optional_int(raw.get("cached_tokens")),
                reasoning_tokens=optional_int(raw.get("reasoning_tokens")),
                total_tokens=optional_int(raw.get("total_tokens")),
                cost=optional_float(raw.get("cost")),
                time_to_first_token=optional_float(raw.get("time_to_first_token")),
                elapsed_seconds=optional_float(raw.get("elapsed_seconds")),
                tokens_per_second=optional_float(raw.get("tokens_per_second")),
                created_at=optional_string(raw.get("created_at")),
            )
            if message.id is None:
                raise ImportExportError("Could not import message.")
            old_to_new[old_id] = message.id
            pending.remove(raw)
            progressed = True
        if not progressed:
            storage.delete_conversation(conversation.id)
            raise ImportExportError("Message tree has a cycle or missing parent.")

    old_leaf = conversation_data.get("active_leaf_id")
    if old_leaf is not None and str(old_leaf) in old_to_new:
        storage.set_active_leaf(conversation.id, old_to_new[str(old_leaf)])
    elif old_to_new:
        storage.set_active_leaf(conversation.id, list(old_to_new.values())[-1])
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
        old_branch = raw.get("branch_leaf_id")
        storage.save_compaction(
            conversation.id,
            old_to_new.get(str(old_branch)) if old_branch is not None else None,
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
                old_to_new.get(str(raw["parent_message_id"]))
                if raw.get("parent_message_id") is not None
                else None,
                str(raw.get("mode") or "chat"),
                str(raw.get("model_id") or conversation.model),
                str(raw.get("started_at") or ""),
            )
            storage.finish_generation_run(
                run_id,
                str(raw.get("status") or "completed"),
                assistant_message_id=(
                    old_to_new.get(str(raw["assistant_message_id"]))
                    if raw.get("assistant_message_id") is not None
                    else None
                ),
                error=optional_string(raw.get("error")),
                finished_at=optional_string(raw.get("finished_at")),
            )
        except (KeyError, TypeError, ValueError):
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
        metadata = json.dumps(_message_markdown_metadata(message), sort_keys=True)
        label = {"user": "You", "assistant": "Assistant", "system": "System"}[message.role]
        lines.extend(
            [
                f"<!-- local-openrouter-chat-message {metadata} -->",
                f"## {label}",
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
                lines.append(
                    f"- `{event['type']}` — `{json.dumps(event['payload'], ensure_ascii=False)[:1000]}`"
                )
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
    role_map = {"System": "system", "You": "user", "Assistant": "assistant"}
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
            role_map[match.group("label")],
            match.group("content").strip(),
            parent_message_id=(id_map.get(str(old_parent)) if old_parent is not None else None),
            model_id=optional_string(metadata.get("model_id")),
            provider_id=optional_string(metadata.get("provider_id")),
            finish_reason=optional_string(metadata.get("finish_reason")),
            input_tokens=optional_int(metadata.get("input_tokens")),
            output_tokens=optional_int(metadata.get("output_tokens")),
            cached_tokens=optional_int(metadata.get("cached_tokens")),
            reasoning_tokens=optional_int(metadata.get("reasoning_tokens")),
            total_tokens=optional_int(metadata.get("total_tokens")),
            cost=optional_float(metadata.get("cost")),
            time_to_first_token=optional_float(metadata.get("time_to_first_token")),
            elapsed_seconds=optional_float(metadata.get("elapsed_seconds")),
            tokens_per_second=optional_float(metadata.get("tokens_per_second")),
            created_at=optional_string(metadata.get("created_at")),
        )
        if message.id is not None:
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
    return import_json(storage, content) if path.suffix.lower() == ".json" else import_markdown(
        storage, content, title=path.stem
    )


def _message_markdown_metadata(message: Message) -> dict[str, Any]:
    return {key: value for key, value in asdict(message).items() if key != "content"}


def _validate_json_messages(messages: list[object]) -> None:
    ids: set[str] = set()
    parents: dict[str, str | None] = {}
    total_bytes = 0
    for raw in messages:
        if not isinstance(raw, dict):
            raise ImportExportError("Message entry must be an object.")
        if raw.get("id") is None:
            raise ImportExportError("Message is missing an ID.")
        message_id = str(raw["id"])
        if message_id in ids:
            raise ImportExportError("Message IDs must be unique.")
        ids.add(message_id)
        if raw.get("role") not in {"system", "user", "assistant"}:
            raise ImportExportError("Message has an invalid role.")
        total_bytes += len(str(raw.get("content") or "").encode("utf-8"))
        if total_bytes > MAX_IMPORT_CONTENT_BYTES:
            raise ImportExportError("Export message content is too large.")
        parent = raw.get("parent_message_id")
        parents[message_id] = str(parent) if parent is not None else None
    if any(parent is not None and parent not in ids for parent in parents.values()):
        raise ImportExportError("Message tree references a missing parent.")
    for message_id in ids:
        seen: set[str] = set()
        current: str | None = message_id
        while current is not None:
            if current in seen:
                raise ImportExportError("Message tree contains a cycle.")
            seen.add(current)
            current = parents[current]
