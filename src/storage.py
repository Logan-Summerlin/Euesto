from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .migrations import migrate
from .models import (
    MESSAGE_METADATA_FIELDS,
    Conversation,
    Message,
    ModelOption,
    PromptPreset,
    Role,
    utc_now,
)

_UNSET = object()
_MESSAGE_COLUMNS = (
    "conversation_id", "parent_message_id", "role", "content", "created_at",
    *MESSAGE_METADATA_FIELDS,
)
# Desktop history keeps only this summary of tool output events, or of any oversized event.
_RUN_EVENT_SUMMARY_KEYS = ("request_id", "tool", "ok", "error_code")
_MAX_RUN_EVENT_BYTES = 512_000


class Storage:
    def __init__(self, database_path: Path | str):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        migrate(self._connection)

    def close(self) -> None:
        self._connection.close()

    def create_conversation(
        self,
        title: str,
        model: str,
        system_prompt: str,
        *,
        prompt_preset_id: str | None = None,
        prompt_preset_snapshot: str | None = None,
    ) -> Conversation:
        timestamp = utc_now()
        conversation = Conversation(
            id=str(uuid.uuid4()),
            title=title.strip() or "New conversation",
            model=model,
            system_prompt=system_prompt,
            created_at=timestamp,
            updated_at=timestamp,
            prompt_preset_id=prompt_preset_id,
            prompt_preset_snapshot=prompt_preset_snapshot,
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO conversations (
                    id, title, model, system_prompt, created_at, updated_at,
                    active_leaf_id, pinned_at, archived_at,
                    prompt_preset_id, prompt_preset_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    conversation.id,
                    conversation.title,
                    conversation.model,
                    conversation.system_prompt,
                    conversation.created_at,
                    conversation.updated_at,
                    prompt_preset_id,
                    prompt_preset_snapshot,
                ),
            )
        return conversation

    def list_conversations(
        self, *, query: str = "", archived: bool = False
    ) -> list[Conversation]:
        query = query.strip()
        archive_clause = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
        parameters: list[object] = []
        search_clause = ""
        if query:
            matched_ids = self._search_message_conversation_ids(query)
            placeholders = ",".join("?" for _ in matched_ids)
            title_pattern = f"%{query}%"
            search_clause = " AND (title LIKE ? COLLATE NOCASE"
            parameters.append(title_pattern)
            if matched_ids:
                search_clause += f" OR id IN ({placeholders})"
                parameters.extend(matched_ids)
            search_clause += ")"
        rows = self._connection.execute(
            f"""
            SELECT * FROM conversations
            WHERE {archive_clause}{search_clause}
            ORDER BY (pinned_at IS NULL), pinned_at DESC, updated_at DESC
            """,
            parameters,
        ).fetchall()
        return [self._conversation_from_row(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self._connection.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return self._conversation_from_row(row) if row else None

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | object = _UNSET,
        model: str | object = _UNSET,
        system_prompt: str | object = _UNSET,
        prompt_preset_id: str | None | object = _UNSET,
        prompt_preset_snapshot: str | None | object = _UNSET,
    ) -> None:
        changes = {
            column: value
            for column, value in (
                ("title", title),
                ("model", model),
                ("system_prompt", system_prompt),
                ("prompt_preset_id", prompt_preset_id),
                ("prompt_preset_snapshot", prompt_preset_snapshot),
            )
            if value is not _UNSET
        }
        if changes:
            self._update_conversation(conversation_id, changes)

    def pin_conversation(self, conversation_id: str, pinned: bool) -> None:
        self._update_conversation(conversation_id, {"pinned_at": utc_now() if pinned else None})

    def archive_conversation(self, conversation_id: str, archived: bool) -> None:
        self._update_conversation(
            conversation_id, {"archived_at": utc_now() if archived else None}
        )

    def _update_conversation(self, conversation_id: str, changes: dict[str, object]) -> None:
        """Set trusted column names on a conversation and touch its ``updated_at``."""
        changes = {**changes, "updated_at": utc_now()}
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._connection:
            self._connection.execute(
                f"UPDATE conversations SET {assignments} WHERE id = ?",
                (*changes.values(), conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )

    def add_message(
        self,
        conversation_id: str,
        role: Role,
        content: str,
        *,
        parent_message_id: int | None | object = _UNSET,
        activate: bool = True,
        created_at: str | None = None,
        **metadata: object,
    ) -> Message:
        """Append a message; by default under the active leaf, which it then becomes.

        ``metadata`` takes the provider/usage fields in ``MESSAGE_METADATA_FIELDS``.
        """
        unknown = set(metadata) - set(MESSAGE_METADATA_FIELDS)
        if unknown:
            raise TypeError(f"Unknown message fields: {', '.join(sorted(unknown))}")
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("Conversation does not exist")
        if parent_message_id is _UNSET:
            parent_message_id = conversation.active_leaf_id
        if parent_message_id is not None:
            parent = self.get_message(int(parent_message_id))
            if parent is None or parent.conversation_id != conversation_id:
                raise ValueError("Parent message is not in this conversation")
        message = Message(
            None, conversation_id, role, content, created_at or utc_now(), parent_message_id
        )
        for key, value in metadata.items():
            setattr(message, key, value)
        if message.total_tokens is None and (
            message.input_tokens is not None or message.output_tokens is not None
        ):
            message.total_tokens = (message.input_tokens or 0) + (message.output_tokens or 0)
        with self._connection:
            cursor = self._connection.execute(
                f"INSERT INTO messages ({', '.join(_MESSAGE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _MESSAGE_COLUMNS)})",
                tuple(getattr(message, column) for column in _MESSAGE_COLUMNS),
            )
            message.id = int(cursor.lastrowid)
            if activate:
                self._connection.execute(
                    "UPDATE conversations SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
                    (message.id, message.created_at, conversation_id),
                )
        return message

    def get_message(self, message_id: int) -> Message | None:
        row = self._connection.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._message_from_row(row) if row else None

    def list_messages(self, conversation_id: str) -> list[Message]:
        """Return the active root-to-leaf branch for display and model context."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or conversation.active_leaf_id is None:
            return []
        return self.list_branch_to(conversation_id, conversation.active_leaf_id)

    def list_branch_to(self, conversation_id: str, leaf_id: int | None) -> list[Message]:
        """Return the root-to-message path without changing the active branch."""
        if leaf_id is None:
            return []
        path: list[Message] = []
        seen: set[int] = set()
        message_id: int | None = leaf_id
        while message_id is not None:
            if message_id in seen:
                raise RuntimeError("Message tree contains a cycle")
            seen.add(message_id)
            message = self.get_message(message_id)
            if message is None or message.conversation_id != conversation_id:
                break
            path.append(message)
            message_id = message.parent_message_id
        path.reverse()
        return path

    def list_all_messages(self, conversation_id: str) -> list[Message]:
        rows = self._connection.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def set_active_leaf(self, conversation_id: str, message_id: int | None) -> None:
        if message_id is not None:
            message = self.get_message(message_id)
            if message is None or message.conversation_id != conversation_id:
                raise ValueError("Active leaf must belong to the conversation")
        with self._connection:
            self._connection.execute(
                "UPDATE conversations SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
                (message_id, utc_now(), conversation_id),
            )

    def edit_user_message(self, message_id: int, content: str) -> Message:
        original = self.get_message(message_id)
        if original is None or original.role != "user":
            raise ValueError("Only an existing user message can be edited")
        return self.add_message(
            original.conversation_id,
            "user",
            content,
            parent_message_id=original.parent_message_id,
        )

    def siblings(self, message_id: int) -> list[Message]:
        message = self.get_message(message_id)
        if message is None:
            return []
        parent_clause = (
            "parent_message_id IS NULL"
            if message.parent_message_id is None
            else "parent_message_id = ?"
        )
        parameters: tuple[object, ...] = (message.conversation_id, message.role)
        if message.parent_message_id is not None:
            parameters = (message.conversation_id, message.parent_message_id, message.role)
        rows = self._connection.execute(
            f"""
            SELECT * FROM messages
            WHERE conversation_id = ? AND {parent_clause} AND role = ?
            ORDER BY id
            """,
            parameters,
        ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def activate_branch_from(self, message_id: int) -> int:
        message = self.get_message(message_id)
        if message is None:
            raise ValueError("Message does not exist")
        row = self._connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT ?
                UNION ALL
                SELECT messages.id FROM messages
                JOIN descendants ON messages.parent_message_id = descendants.id
                WHERE messages.conversation_id = ?
            )
            SELECT d.id FROM descendants d
            LEFT JOIN messages child ON child.parent_message_id = d.id
                AND child.conversation_id = ?
            WHERE child.id IS NULL
            ORDER BY d.id DESC LIMIT 1
            """,
            (message_id, message.conversation_id, message.conversation_id),
        ).fetchone()
        leaf_id = int(row[0]) if row else message_id
        self.set_active_leaf(message.conversation_id, leaf_id)
        return leaf_id

    def set_setting(self, key: str, value: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def save_compaction(
        self,
        conversation_id: str,
        branch_leaf_id: int | None,
        covered_message_ids: list[int],
        summary: str,
        model_id: str | None = None,
    ) -> str:
        compaction_id = str(uuid.uuid4())
        with self._connection:
            self._connection.execute(
                "INSERT INTO compactions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    compaction_id,
                    conversation_id,
                    branch_leaf_id,
                    json.dumps(covered_message_ids),
                    summary,
                    model_id,
                    utc_now(),
                ),
            )
        return compaction_id

    def list_compactions(self, conversation_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM compactions WHERE conversation_id = ? ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        values: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            try:
                item["covered_message_ids"] = json.loads(
                    str(item.pop("covered_message_ids_json"))
                )
            except (TypeError, json.JSONDecodeError):
                item["covered_message_ids"] = []
            values.append(item)
        return values

    def save_run_event(self, conversation_id: str, event: object) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            raise ValueError("Run event payload must be an object")
        event_type = str(event.type)
        serialized = json.dumps(payload, separators=(",", ":"))
        if (
            event_type in {"tool.output", "tool.completed"}
            or len(serialized.encode("utf-8")) > _MAX_RUN_EVENT_BYTES
        ):
            summary = {key: payload.get(key) for key in _RUN_EVENT_SUMMARY_KEYS}
            summary["truncated_for_desktop_history"] = True
            serialized = json.dumps(summary, separators=(",", ":"))
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO run_events
                (conversation_id, run_id, event_id, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    str(event.run_id),
                    int(event.event_id),
                    event_type,
                    serialized,
                    str(event.created_at),
                ),
            )
            self._connection.execute(
                """
                DELETE FROM run_events WHERE rowid IN (
                    SELECT rowid FROM run_events WHERE conversation_id = ?
                    ORDER BY rowid DESC LIMIT -1 OFFSET 20000
                )
                """,
                (conversation_id,),
            )

    def start_generation_run(
        self,
        run_id: str,
        conversation_id: str,
        parent_message_id: int | None,
        mode: str,
        model_id: str,
        started_at: str | None = None,
    ) -> None:
        if self.get_conversation(conversation_id) is None:
            raise ValueError("Conversation does not exist")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO generation_runs
                (run_id, conversation_id, parent_message_id, mode, model_id, status, started_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,
                    parent_message_id=excluded.parent_message_id,
                    mode=excluded.mode,
                    model_id=excluded.model_id,
                    status='running',
                    started_at=excluded.started_at,
                    finished_at=NULL,
                    error=NULL
                """,
                (run_id, conversation_id, parent_message_id, mode, model_id, started_at or utc_now()),
            )

    def finish_generation_run(
        self,
        run_id: str,
        status: str,
        *,
        assistant_message_id: int | None = None,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE generation_runs
                SET status=?, assistant_message_id=COALESCE(?, assistant_message_id),
                    finished_at=?, error=?
                WHERE run_id=?
                """,
                (status, assistant_message_id, finished_at or utc_now(), error, run_id),
            )

    def list_generation_runs(self, conversation_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM generation_runs WHERE conversation_id = ? ORDER BY started_at, run_id",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_run_events(
        self,
        conversation_id: str,
        *,
        event_types: frozenset[str] | None = None,
        payload_keys: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        query = """
            SELECT run_id, event_id, type, payload_json, created_at
            FROM run_events WHERE conversation_id = ?
        """
        parameters: list[object] = [conversation_id]
        if event_types:
            ordered_types = sorted(event_types)
            query += f" AND type IN ({','.join('?' for _ in ordered_types)})"
            parameters.extend(ordered_types)
        query += " ORDER BY rowid"
        rows = self._connection.execute(query, parameters).fetchall()
        events: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and payload_keys is not None:
                payload = {
                    key: payload[key]
                    for key in payload_keys
                    if key in payload and payload[key] is not None
                }
            events.append({
                "run_id": str(row["run_id"]),
                "event_id": int(row["event_id"]),
                "type": str(row["type"]),
                "payload": payload if isinstance(payload, dict) else {},
                "created_at": str(row["created_at"]),
            })
        return events

    def save_workspace_config(
        self, workspace_id: str, canonical_path: str, config: dict[str, object]
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO workspace_configs VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    canonical_path=excluded.canonical_path,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    canonical_path,
                    json.dumps(config, separators=(",", ":")),
                    utc_now(),
                ),
            )

    def workspace_config(self, workspace_id: str) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT config_json FROM workspace_configs WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def save_prompt_command(self, name: str, description: str, template: str) -> None:
        normalized = name.strip().lstrip("/").casefold()
        if not normalized or not normalized.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Command name may contain letters, numbers, hyphens, and underscores")
        if not template.strip() or len(template) > 64_000:
            raise ValueError("Command template must be bounded non-empty text")
        timestamp = utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO prompt_commands VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    template=excluded.template,
                    updated_at=excluded.updated_at
                """,
                (normalized, description.strip(), template, timestamp, timestamp),
            )

    def list_prompt_commands(self) -> list[dict[str, str]]:
        rows = self._connection.execute(
            "SELECT * FROM prompt_commands ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [{key: str(row[key]) for key in row.keys()} for row in rows]

    def delete_prompt_command(self, name: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM prompt_commands WHERE name = ?", (name.strip().lstrip("/"),)
            )

    def replace_model_catalog(self, models: list[ModelOption], fetched_at: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM model_catalog")
            self._connection.executemany(
                """
                INSERT INTO model_catalog(model_id, payload_json, fetched_at)
                VALUES (?, ?, ?)
                """,
                [
                    (model.id, json.dumps(model.to_json(), sort_keys=True), fetched_at)
                    for model in models
                ],
            )

    def list_catalog_models(self) -> list[ModelOption]:
        rows = self._connection.execute(
            "SELECT payload_json FROM model_catalog ORDER BY model_id"
        ).fetchall()
        models: list[ModelOption] = []
        for row in rows:
            try:
                models.append(ModelOption.from_json(json.loads(row[0])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return models

    def catalog_fetched_at(self) -> str | None:
        row = self._connection.execute(
            "SELECT MAX(fetched_at) FROM model_catalog"
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def set_model_favorite(self, model_id: str, favorite: bool) -> None:
        with self._connection:
            if favorite:
                self._connection.execute(
                    "INSERT OR REPLACE INTO model_favorites(model_id, created_at) VALUES (?, ?)",
                    (model_id, utc_now()),
                )
            else:
                self._connection.execute(
                    "DELETE FROM model_favorites WHERE model_id = ?", (model_id,)
                )

    def favorite_model_ids(self) -> list[str]:
        return [
            str(row[0])
            for row in self._connection.execute(
                "SELECT model_id FROM model_favorites ORDER BY created_at DESC"
            ).fetchall()
        ]

    def record_recent_model(self, model_id: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO model_recents(model_id, used_at) VALUES (?, ?)",
                (model_id, utc_now()),
            )

    def recent_model_ids(self, limit: int = 8) -> list[str]:
        return [
            str(row[0])
            for row in self._connection.execute(
                "SELECT model_id FROM model_recents ORDER BY used_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def save_prompt_preset(
        self, name: str, content: str, preset_id: str | None = None
    ) -> PromptPreset:
        name = name.strip()
        if not name:
            raise ValueError("Preset name cannot be empty")
        timestamp = utc_now()
        resolved_id = preset_id or str(uuid.uuid4())
        existing = self.get_prompt_preset(resolved_id)
        created_at = existing.created_at if existing else timestamp
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO prompt_presets(id, name, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (resolved_id, name, content, created_at, timestamp),
            )
        return PromptPreset(resolved_id, name, content, created_at, timestamp)

    def list_prompt_presets(self) -> list[PromptPreset]:
        rows = self._connection.execute(
            "SELECT * FROM prompt_presets ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [PromptPreset(**dict(row)) for row in rows]

    def get_prompt_preset(self, preset_id: str) -> PromptPreset | None:
        row = self._connection.execute(
            "SELECT * FROM prompt_presets WHERE id = ?", (preset_id,)
        ).fetchone()
        return PromptPreset(**dict(row)) if row else None

    def delete_prompt_preset(self, preset_id: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM prompt_presets WHERE id = ?", (preset_id,))

    def usage_summary(self, conversation_id: str | None = None) -> dict[str, float | int]:
        where = "WHERE conversation_id = ?" if conversation_id else ""
        parameters = (conversation_id,) if conversation_id else ()
        row = self._connection.execute(
            f"""
            SELECT
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(cached_tokens), 0),
                COALESCE(SUM(reasoning_tokens), 0),
                COALESCE(SUM(cost), 0)
            FROM messages {where}
            """,
            parameters,
        ).fetchone()
        return {
            "input_tokens": int(row[0]),
            "output_tokens": int(row[1]),
            "cached_tokens": int(row[2]),
            "reasoning_tokens": int(row[3]),
            "cost": float(row[4]),
        }

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> Conversation:
        return Conversation(**dict(row))

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(**dict(row))

    def _search_message_conversation_ids(self, query: str) -> list[str]:
        try:
            rows = self._connection.execute(
                "SELECT DISTINCT conversation_id FROM message_fts WHERE message_fts MATCH ?",
                (f'"{query.replace(chr(34), chr(34) * 2)}"',),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = self._connection.execute(
                """
                SELECT DISTINCT conversation_id FROM messages
                WHERE content LIKE ? COLLATE NOCASE LIMIT 500
                """,
                (f"%{query}%",),
            ).fetchall()
        return [str(row[0]) for row in rows]
