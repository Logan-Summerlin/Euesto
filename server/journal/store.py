from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from shared.events import TERMINAL_EVENT_TYPES, EventEnvelope
from shared.permissions import PermissionDecision, PermissionRule
from shared.tools import ToolRequest


class JournalStore:
    def __init__(
        self,
        path: Path | str,
        *,
        max_events_per_run: int = 20_000,
        max_runs: int = 500,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_events_per_run = max_events_per_run
        self.max_runs = max_runs
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS model_catalog (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    fetched_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS permission_rules (
                    rule_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    path_prefix TEXT,
                    executable TEXT,
                    argument_prefix_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS run_snapshots (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                    request_json TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    visible_messages_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    safe_to_resume INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    internal_messages_json TEXT NOT NULL,
                    visible_messages_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspace_configs (
                    workspace_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_run(self, run_id: str, mode: str, created_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO runs(run_id, mode, state, created_at, updated_at) VALUES (?, ?, 'created', ?, ?)",
                (run_id, mode, created_at, created_at),
            )
            self._prune_runs()

    def append(self, run_id: str, event_type: str, created_at: str, payload: dict[str, Any]) -> EventEnvelope:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) + 1 FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
            event = EventEnvelope(int(row[0]), run_id, event_type, created_at, payload)
            self._connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, event.event_id, event.type, event.created_at, event.schema_version, json.dumps(payload, separators=(",", ":"))),
            )
            state = event.type.removeprefix("run.") if event.type.startswith("run.") else None
            if state in {"started", "waiting", "paused", "resumed", "cancelled", "failed", "completed"}:
                if state == "resumed":
                    state = "started"
                self._connection.execute(
                    "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (state, created_at, run_id),
                )
            self._prune_run(run_id)
            return event

    def events_after(self, run_id: str, event_id: int = 0) -> list[EventEnvelope]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_id > ? ORDER BY event_id",
                (run_id, max(0, event_id)),
            ).fetchall()
        return [
            EventEnvelope(
                event_id=int(row["event_id"]),
                run_id=str(row["run_id"]),
                type=str(row["type"]),
                created_at=str(row["created_at"]),
                schema_version=int(row["schema_version"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def nonterminal_runs(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM runs WHERE state NOT IN ('cancelled', 'failed', 'completed')"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def recover_interrupted_runs(self, created_at: str) -> list[str]:
        resumable: list[str] = []
        for run_id in self.nonterminal_runs():
            run = self.get_run(run_id)
            snapshot = self.load_run_snapshot(run_id)
            if run and run["mode"] in {"plan", "agent"} and snapshot and snapshot["safe_to_resume"]:
                self.append(
                    run_id,
                    "run.paused",
                    created_at,
                    {"reason": "gateway.restarted", "resumable": True},
                )
                resumable.append(run_id)
            else:
                self.append(
                    run_id,
                    "run.failed",
                    created_at,
                    {
                        "code": "gateway.restarted",
                        "message": "The gateway restarted before this run reached a safe resume point.",
                        "retryable": True,
                    },
                )
        return resumable

    def save_run_snapshot(
        self,
        run_id: str,
        request: dict[str, Any],
        messages: list[dict[str, Any]],
        visible_messages: list[dict[str, Any]],
        budget: dict[str, Any],
        *,
        safe_to_resume: bool,
        updated_at: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO run_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    request_json=excluded.request_json,
                    messages_json=excluded.messages_json,
                    visible_messages_json=excluded.visible_messages_json,
                    budget_json=excluded.budget_json,
                    safe_to_resume=excluded.safe_to_resume,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    json.dumps(request, separators=(",", ":")),
                    json.dumps(messages, separators=(",", ":")),
                    json.dumps(visible_messages, separators=(",", ":")),
                    json.dumps(budget, separators=(",", ":")),
                    int(safe_to_resume),
                    updated_at,
                ),
            )

    def load_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM run_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return {
                "run_id": run_id,
                "request": json.loads(row["request_json"]),
                "messages": json.loads(row["messages_json"]),
                "visible_messages": json.loads(row["visible_messages_json"]),
                "budget": json.loads(row["budget_json"]),
                "safe_to_resume": bool(row["safe_to_resume"]),
                "updated_at": str(row["updated_at"]),
            }
        except (TypeError, json.JSONDecodeError):
            return None

    def resumable_runs(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT r.run_id FROM runs r JOIN run_snapshots s ON s.run_id = r.run_id
                WHERE r.state = 'paused' AND s.safe_to_resume = 1
                ORDER BY r.updated_at
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def save_agent_session(
        self,
        session_id: str,
        workspace_id: str,
        mode: str,
        internal_messages: list[dict[str, Any]],
        visible_messages: list[dict[str, Any]],
        updated_at: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    mode=excluded.mode,
                    internal_messages_json=excluded.internal_messages_json,
                    visible_messages_json=excluded.visible_messages_json,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    workspace_id,
                    mode,
                    json.dumps(internal_messages, separators=(",", ":")),
                    json.dumps(visible_messages, separators=(",", ":")),
                    updated_at,
                ),
            )

    def load_agent_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return {
                "session_id": session_id,
                "workspace_id": str(row["workspace_id"]),
                "mode": str(row["mode"]),
                "internal_messages": json.loads(row["internal_messages_json"]),
                "visible_messages": json.loads(row["visible_messages_json"]),
                "updated_at": str(row["updated_at"]),
            }
        except (TypeError, json.JSONDecodeError):
            return None

    def save_workspace_config(self, workspace_id: str, payload: dict[str, Any], updated_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO workspace_configs VALUES (?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at
                """,
                (workspace_id, json.dumps(payload, separators=(",", ":")), updated_at),
            )

    def load_workspace_config(self, workspace_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM workspace_configs WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def is_terminal(self, run_id: str) -> bool:
        events = self.events_after(run_id)
        return bool(events and events[-1].type in TERMINAL_EVENT_TYPES)

    def save_catalog(self, models: list[dict[str, Any]], fetched_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO model_catalog(singleton, fetched_at, payload_json) VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET fetched_at=excluded.fetched_at, payload_json=excluded.payload_json",
                (fetched_at, json.dumps(models, separators=(",", ":"))),
            )

    def load_catalog(self) -> tuple[list[dict[str, Any]], str] | None:
        with self._lock:
            row = self._connection.execute("SELECT fetched_at, payload_json FROM model_catalog WHERE singleton = 1").fetchone()
        if not row:
            return None
        try:
            models = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return (models, str(row["fetched_at"])) if isinstance(models, list) else None

    def save_permission_rule(self, rule: PermissionRule) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO permission_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.rule_id, rule.workspace_id, rule.mode, rule.tool, rule.path_prefix,
                    rule.executable, json.dumps(rule.argument_prefix), int(rule.enabled), None,
                ),
            )

    def permission_rules(self, workspace_id: str, *, include_disabled: bool = False) -> list[PermissionRule]:
        with self._lock:
            enabled_clause = "" if include_disabled else " AND enabled = 1"
            rows = self._connection.execute(
                f"SELECT * FROM permission_rules WHERE workspace_id = ?{enabled_clause}", (workspace_id,)
            ).fetchall()
        return [
            PermissionRule(
                rule_id=str(row["rule_id"]), decision=PermissionDecision.ALLOW_RULE,
                workspace_id=str(row["workspace_id"]), mode=str(row["mode"]), tool=str(row["tool"]),
                path_prefix=str(row["path_prefix"]) if row["path_prefix"] else None,
                executable=str(row["executable"]) if row["executable"] else None,
                argument_prefix=tuple(json.loads(row["argument_prefix_json"])), enabled=bool(row["enabled"]),
                last_used_at=str(row["last_used_at"]) if row["last_used_at"] else None,
            )
            for row in rows
        ]

    def delete_permission_rule(self, rule_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM permission_rules WHERE rule_id = ?", (rule_id,))
        return cursor.rowcount > 0

    def set_permission_rule_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE permission_rules SET enabled = ? WHERE rule_id = ?",
                (int(enabled), rule_id),
            )
        return cursor.rowcount > 0

    def touch_permission_rule(self, rule_id: str, used_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE permission_rules SET last_used_at = ? WHERE rule_id = ?", (used_at, rule_id)
            )

    def rule_for_request(self, request: ToolRequest, workspace_id: str) -> PermissionRule:
        path = str(request.arguments.get("path") or request.arguments.get("directory") or "") or None
        executable = str(request.arguments.get("executable") or "") or None
        arguments = tuple(str(item) for item in request.arguments.get("arguments") or ())
        return PermissionRule(
            rule_id=f"rule-{request.request_id}", decision=PermissionDecision.ALLOW_RULE,
            workspace_id=workspace_id, mode=request.mode, tool=request.tool, path_prefix=path,
            executable=executable, argument_prefix=arguments,
        )

    def _prune_run(self, run_id: str) -> None:
        count = self._connection.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
        if count <= self.max_events_per_run:
            return
        remove = count - self.max_events_per_run
        self._connection.execute(
            "DELETE FROM events WHERE run_id = ? AND event_id IN (SELECT event_id FROM events WHERE run_id = ? ORDER BY event_id LIMIT ?)",
            (run_id, run_id, remove),
        )

    def _prune_runs(self) -> None:
        count = self._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        remove = count - self.max_runs
        if remove <= 0:
            return
        self._connection.execute(
            """
            DELETE FROM runs WHERE run_id IN (
                SELECT run_id FROM runs
                WHERE state IN ('cancelled', 'failed', 'completed')
                ORDER BY updated_at, run_id LIMIT ?
            )
            """,
            (remove,),
        )
