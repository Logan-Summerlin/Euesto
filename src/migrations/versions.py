from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 4


LATEST_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    active_leaf_id INTEGER,
    pinned_at TEXT,
    archived_at TEXT,
    prompt_preset_id TEXT,
    prompt_preset_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_message_id INTEGER REFERENCES messages(id),
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content TEXT NOT NULL,
    model_id TEXT,
    provider_id TEXT,
    finish_reason TEXT,
    created_at TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_tokens INTEGER,
    cost REAL,
    time_to_first_token REAL,
    elapsed_seconds REAL,
    tokens_per_second REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON messages(parent_message_id);
CREATE INDEX IF NOT EXISTS idx_conversations_organization
    ON conversations(archived_at, pinned_at, updated_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_catalog (
    model_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_favorites (
    model_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_recents (
    model_id TEXT PRIMARY KEY,
    used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_aliases (
    alias TEXT PRIMARY KEY COLLATE NOCASE,
    model_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compactions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    branch_leaf_id INTEGER,
    covered_message_ids_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    model_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_run_events_conversation
    ON run_events(conversation_id, run_id, event_id);

CREATE TABLE IF NOT EXISTS generation_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_message_id INTEGER REFERENCES messages(id),
    assistant_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    mode TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_generation_runs_conversation
    ON generation_runs(conversation_id, started_at);
CREATE INDEX IF NOT EXISTS idx_generation_runs_assistant
    ON generation_runs(assistant_message_id);

CREATE TABLE IF NOT EXISTS workspace_configs (
    workspace_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_commands (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    description TEXT NOT NULL,
    template TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


V2_CONVERSATION_COLUMNS = {
    "active_leaf_id": "INTEGER",
    "pinned_at": "TEXT",
    "archived_at": "TEXT",
    "prompt_preset_id": "TEXT",
    "prompt_preset_snapshot": "TEXT",
}

V2_MESSAGE_COLUMNS = {
    "parent_message_id": "INTEGER REFERENCES messages(id)",
    "model_id": "TEXT",
    "provider_id": "TEXT",
    "finish_reason": "TEXT",
    "cached_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "time_to_first_token": "REAL",
    "elapsed_seconds": "REAL",
    "tokens_per_second": "REAL",
}


def migrate(connection: sqlite3.Connection) -> None:
    """Create the latest schema and losslessly upgrade a v0.1 flat transcript."""
    connection.execute("PRAGMA foreign_keys = ON")
    if not _table_exists(connection, "conversations"):
        connection.executescript(LATEST_SCHEMA)
        _ensure_fts(connection)
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        connection.commit()
        return

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 2:
        _migrate_v1_to_v2(connection)
    if version < 4:
        _migrate_v3_to_v4(connection)
    connection.executescript(LATEST_SCHEMA)
    _ensure_fts(connection)
    connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    connection.commit()


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    with connection:
        _add_missing_columns(connection, "conversations", V2_CONVERSATION_COLUMNS)
        _add_missing_columns(connection, "messages", V2_MESSAGE_COLUMNS)

        conversation_rows = connection.execute(
            "SELECT id FROM conversations ORDER BY created_at, id"
        ).fetchall()
        for row in conversation_rows:
            conversation_id = str(row[0])
            message_rows = connection.execute(
                "SELECT id FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            parent_id: int | None = None
            for message_row in message_rows:
                message_id = int(message_row[0])
                connection.execute(
                    "UPDATE messages SET parent_message_id = ? WHERE id = ?",
                    (parent_id, message_id),
                )
                parent_id = message_id
            connection.execute(
                "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
                (parent_id, conversation_id),
            )


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Add durable run-to-message links without rewriting existing event history."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS generation_runs (
            run_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            parent_message_id INTEGER REFERENCES messages(id),
            assistant_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            mode TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_generation_runs_conversation
            ON generation_runs(conversation_id, started_at);
        CREATE INDEX IF NOT EXISTS idx_generation_runs_assistant
            ON generation_runs(assistant_message_id);
        """
    )


def _add_missing_columns(
    connection: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _ensure_fts(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                content,
                conversation_id UNINDEXED,
                message_id UNINDEXED
            );
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO message_fts(content, conversation_id, message_id)
                VALUES (new.content, new.conversation_id, new.id);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                DELETE FROM message_fts WHERE message_id = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content ON messages BEGIN
                DELETE FROM message_fts WHERE message_id = old.id;
                INSERT INTO message_fts(content, conversation_id, message_id)
                VALUES (new.content, new.conversation_id, new.id);
            END;
            """
        )
        connection.execute("DELETE FROM message_fts")
        connection.execute(
            """
            INSERT INTO message_fts(content, conversation_id, message_id)
            SELECT content, conversation_id, id FROM messages
            """
        )
    except sqlite3.OperationalError:
        # Some custom SQLite builds omit FTS5. Storage falls back to bounded LIKE search.
        return


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None
