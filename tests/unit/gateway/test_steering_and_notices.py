"""Steering endpoint bounds, the broad-Bash-rule migration notice, and saved Bash rule scope."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from server.app import create_app
from server.config import GatewayConfig
from server.journal import JournalStore
from server.service import MAX_STEERING_CHARS, GatewayService, GatewayServiceError
from shared.permissions import PermissionDecision, PermissionRule
from shared.tools import PublishManifest, ToolRequest

TOKEN = "t" * 43


class _Runtime:
    def __init__(self, active: set[str], capacity: int = 16) -> None:
        self.active = active
        self.capacity = capacity
        self.queued: list[tuple[str, str]] = []

    def is_active(self, run_id: str) -> bool:
        return run_id in self.active

    def steer(self, run_id: str, text: str) -> bool:
        if len(self.queued) >= self.capacity:
            return False
        self.queued.append((run_id, text))
        return True

    async def cancel(self, _run_id: str) -> None:
        return None


def _service(tmp_path: Path, runtime: _Runtime) -> GatewayService:
    service = GatewayService(GatewayConfig(TOKEN, tmp_path / "gateway.sqlite3"))
    service.agent_runtime = runtime
    service.journal.create_run("live", "agent", "2026-01-01T00:00:00Z")
    service.journal.create_run("done", "agent", "2026-01-01T00:00:00Z")
    service.journal.append("done", "run.completed", "2026-01-01T00:00:01Z", {})
    service.journal.create_run("chat", "chat", "2026-01-01T00:00:00Z")
    return service


def _code(coroutine) -> tuple[str, int]:
    with pytest.raises(GatewayServiceError) as error:
        asyncio.run(coroutine)
    return error.value.code, error.value.status


def test_steering_queues_and_journals_for_an_active_run(tmp_path: Path) -> None:
    runtime = _Runtime({"live"})
    service = _service(tmp_path, runtime)
    asyncio.run(service.steer("live", "  also update the README  "))
    assert runtime.queued == [("live", "also update the README")]
    event = service.journal.events_after("live")[-1]
    assert event.type == "input.steered"
    assert event.payload == {"text": "also update the README", "chars": 22}


def test_steering_rejects_missing_terminal_chat_empty_and_oversized(tmp_path: Path) -> None:
    service = _service(tmp_path, _Runtime({"live", "done", "chat"}))
    assert _code(service.steer("missing", "x")) == ("run.not_found", 404)
    assert _code(service.steer("done", "x")) == ("run.not_active", 409)
    assert _code(service.steer("chat", "x")) == ("run.not_active", 409)
    assert _code(service.steer("live", "   ")) == ("steering.empty", 422)
    assert _code(service.steer("live", "x" * (MAX_STEERING_CHARS + 1))) == (
        "steering.too_large",
        422,
    )


def test_steering_queue_is_bounded(tmp_path: Path) -> None:
    service = _service(tmp_path, _Runtime({"live"}, capacity=1))
    asyncio.run(service.steer("live", "first"))
    assert _code(service.steer("live", "second")) == ("steering.queue_full", 429)


def test_steering_endpoint_accepts_only_a_content_field(tmp_path: Path) -> None:
    runtime = _Runtime({"live"})
    service = _service(tmp_path, runtime)

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=create_app(service.config, service))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            codes = []
            for body in (
                {"content": "go on"},
                {"content": 3},
                {"content": "x", "role": "system"},
            ):
                response = await client.post("/v1/runs/live/messages", json=body)
                codes.append(response.status_code)
            return codes

    assert asyncio.run(scenario()) == [202, 422, 422]
    assert runtime.queued == [("live", "go on")]


def test_rebase_endpoint_forwards_only_well_formed_conflict_resolutions(tmp_path: Path) -> None:
    service = _service(tmp_path, _Runtime(set()))
    forwarded: list[tuple[str, dict]] = []

    async def rebase_staging(workspace_id: str, **choices: list[str]) -> dict:
        forwarded.append((workspace_id, choices))
        return {"ok": True}

    service.rebase_staging = rebase_staging  # type: ignore[method-assign]

    async def scenario() -> list[int]:
        transport = httpx.ASGITransport(app=create_app(service.config, service))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            codes = []
            for body in (
                {},
                {"keep_staged": ["a.txt"], "take_host": ["b.txt"]},
                {"keep_staged": "a.txt"},
                {"keep_staged": [1]},
                {"force": True},
            ):
                response = await client.post("/v1/workspaces/w/staging/rebase", json=body)
                codes.append(response.status_code)
            return codes

    assert asyncio.run(scenario()) == [200, 200, 422, 422, 422]
    assert forwarded == [
        ("w", {"keep_staged": [], "take_host": []}),
        ("w", {"keep_staged": ["a.txt"], "take_host": ["b.txt"]}),
    ]


def test_staged_publication_endpoint_takes_no_fields_and_waits_for_runs(
    tmp_path: Path,
) -> None:
    """Persistent staged work (plan 2.4) publishes through the same reviewed manifest."""
    service = _service(tmp_path, _Runtime(set()))
    requested: list[str] = []
    real = service.staged_publication

    async def staged_publication(workspace_id: str):
        requested.append(workspace_id)
        return PublishManifest(
            manifest_id="staged-1",
            run_id="p",
            workspace_id=workspace_id,
            source_snapshot_id="s",
            approval_id="p",
            operations=(),
            publication_id="p",
        )

    service.staged_publication = staged_publication  # type: ignore[method-assign]

    async def scenario() -> list[tuple[int, str]]:
        transport = httpx.ASGITransport(app=create_app(service.config, service))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            results = []
            for body in ({}, {"run_id": "x"}):
                response = await client.post("/v1/workspaces/w/staging/publication", json=body)
                results.append((response.status_code, response.json().get("manifest_id", "")))
            return results

    assert asyncio.run(scenario()) == [(200, "staged-1"), (422, "")]
    assert requested == ["w"]

    # Without the active workspace executor, or while a run is active, nothing is built.
    assert _code(real("w")) == ("workspace.invalid", 409)
    service.executor = object()  # type: ignore[assignment]
    service.config = replace(service.config, workspace_id="w")

    async def active_run() -> tuple[str, int]:
        task = asyncio.get_running_loop().create_future()
        service._tasks["live"] = task  # type: ignore[assignment]
        try:
            await real("w")
        except GatewayServiceError as error:
            return error.code, error.status
        finally:
            task.cancel()
        return "", 0

    assert asyncio.run(active_run()) == ("staging.busy", 409)


def _legacy_database(path: Path) -> None:
    JournalStore(path).close()
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("PRAGMA user_version = 0")
        connection.executemany(
            "INSERT INTO permission_rules VALUES (?, 'workspace', 'agent', ?, NULL, ?, '[]', 1)",
            [
                ("broad-bash", "bash", None),
                ("narrow-bash", "bash", "pytest"),
                ("write-rule", "write", None),
            ],
        )
    connection.close()


def test_migration_disables_broad_bash_rules_once_and_raises_a_notice(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_database(path)
    journal = JournalStore(path)
    rules = {
        rule.rule_id: rule
        for rule in journal.permission_rules("workspace", include_disabled=True)
    }
    assert not rules["broad-bash"].enabled
    assert rules["narrow-bash"].enabled and rules["write-rule"].enabled
    notices = journal.notices()
    assert [notice["notice_id"] for notice in notices] == ["broad-bash-rules-disabled"]
    assert "1 saved Bash rule(s)" in notices[0]["message"]
    assert journal.acknowledge_notice("broad-bash-rules-disabled")
    journal.close()

    reopened = JournalStore(path)
    assert reopened.notices() == []
    reopened.close()


def test_fresh_database_has_no_notice(tmp_path: Path) -> None:
    journal = JournalStore(tmp_path / "fresh.sqlite3")
    assert journal.notices() == []
    journal.close()


def test_always_allow_bash_rule_is_scoped_to_its_command(tmp_path: Path) -> None:
    journal = JournalStore(tmp_path / "rules.sqlite3")
    request = ToolRequest("req", "run", "bash", "agent", {"command": "pytest -q tests/unit"})
    rule = journal.rule_for_request(request, "workspace")
    assert rule.executable == "pytest" and rule.argument_prefix == ("-q", "tests/unit")
    assert rule.decision == PermissionDecision.ALLOW_RULE
    assert rule.matches(request, "workspace")
    other = ToolRequest("req2", "run", "bash", "agent", {"command": "rm -rf /work"})
    assert not rule.matches(other, "workspace")
    with pytest.raises(ValueError):
        journal.rule_for_request(
            ToolRequest("req3", "run", "bash", "agent", {"command": 'echo "unterminated'}),
            "workspace",
        )
    journal.close()


def test_a_bash_allow_rule_without_an_executable_never_matches() -> None:
    request = ToolRequest("req", "run", "bash", "agent", {"command": "curl example.com"})
    for decision in (PermissionDecision.ALLOW_RULE, PermissionDecision.ALLOW_RUN):
        broad = PermissionRule("broad", decision, "workspace", "agent", "bash")
        assert not broad.matches(request, "workspace")
    deny_all = PermissionRule("deny", PermissionDecision.DENY, "workspace", "agent", "bash")
    assert deny_all.matches(request, "workspace")
