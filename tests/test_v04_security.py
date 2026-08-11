from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest

from executor.app import ExecutorService, create_app
from executor.config import ExecutorConfig
from executor.paths import UnsafePath, normalize_relative, safe_path
from shared.permissions import PermissionDecision, PermissionRule, resolve_permission
from shared.tools import PublishManifest, PublishOperation, ToolRequest
from src.workspace_broker import BrokerError, WorkspaceBroker, workspace_id


def _config(tmp_path: Path) -> ExecutorConfig:
    source = tmp_path / "source"
    work = tmp_path / "work"
    ipc = tmp_path / "ipc"
    source.mkdir()
    work.mkdir()
    ipc.mkdir()
    return ExecutorConfig(source, work, ipc / "executor.sock", "x" * 43, "workspace")


@pytest.mark.parametrize("value", ["../escape", "/etc/passwd", "C:/Windows", r"..\escape", "//server/share", "file.txt:secret", "CON.txt", "name."])
def test_executor_rejects_path_aliases_and_traversal(value: str) -> None:
    with pytest.raises(UnsafePath):
        normalize_relative(value)


def test_executor_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePath):
        safe_path(root, "link/secret.txt")


def test_plan_hard_ban_overrides_saved_allow_rule() -> None:
    with pytest.raises(ValueError, match="Plan mode cannot mutate"):
        ToolRequest("request", "run", "apply_patch", "plan", {"edits": []})
    request = ToolRequest("request", "run", "run_command", "agent", {"executable": "pytest", "arguments": []})
    rules = (
        PermissionRule("allow", PermissionDecision.ALLOW_RULE, "w", "agent", "run_command", executable="pytest"),
        PermissionRule("deny", PermissionDecision.DENY, "w", "agent", "run_command", executable="pytest"),
    )
    assert resolve_permission(request, "w", rules) == PermissionDecision.DENY


def test_staging_tools_never_change_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source_file = config.source_root / "hello.txt"
    source_file.write_text("old", encoding="utf-8")
    service = ExecutorService(config)
    digest = hashlib.sha256(b"old").hexdigest()
    request = ToolRequest(
        "request", "run", "apply_patch", "agent",
        {"edits": [{"path": "hello.txt", "expected_sha256": digest, "mode": "replace_file", "content": "new"}]},
    )
    result = asyncio.run(service.execute(request))
    assert result.ok
    assert source_file.read_text() == "old"
    assert (config.work_root / "hello.txt").read_text() == "new"
    with pytest.raises(ValueError):
        ToolRequest("bad", "run", "apply_patch", "plan", request.arguments)


def test_secret_like_files_never_enter_staging_or_tool_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.source_root / ".env").write_text("TOKEN=secret")
    (config.source_root / "visible.txt").write_text("safe")
    service = ExecutorService(config)
    assert not (config.work_root / ".env").exists()
    listed = asyncio.run(service.execute(ToolRequest("list", "run", "list_files", "plan", {"directory": "."})))
    searched = asyncio.run(service.execute(ToolRequest("search", "run", "search_text", "plan", {"query": "secret"})))
    assert ".env" not in listed.output
    assert "secret" not in searched.output
    blocked = asyncio.run(service.execute(ToolRequest("read", "run", "read_file", "plan", {"path": ".env"})))
    assert not blocked.ok


def test_executor_api_authenticates_and_rejects_nonce_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        (config.source_root / "visible.txt").write_text("safe")
        app = create_app(config, ExecutorService(config))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://executor") as client:
            assert (await client.get("/v1/status")).status_code == 401
            headers = {"Authorization": f"Bearer {config.token}", "X-Executor-Nonce": "n" * 24}
            assert (await client.get("/v1/status", headers=headers)).status_code == 200
            replay = await client.get("/v1/status", headers=headers)
            assert replay.status_code == 401
            assert replay.json()["error"]["code"] == "auth.replay"
    asyncio.run(scenario())


def test_command_process_tree_can_be_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        service = ExecutorService(config)
        request = ToolRequest(
            "command", "run", "run_command", "agent",
            {"executable": "python", "arguments": ["-c", "import time; time.sleep(30)"], "timeout_seconds": 30},
        )
        task = asyncio.create_task(service.execute(request))
        for _ in range(100):
            if "command" in service.commands._processes:
                break
            await asyncio.sleep(0.01)
        assert await service.commands.cancel("command")
        result = await asyncio.wait_for(task, timeout=3)
        assert result.ok
        assert result.data["exit_code"] != 0
    asyncio.run(scenario())


def test_command_accepts_bounded_scripted_stdin_without_a_shell(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = ExecutorService(config)
    request = ToolRequest(
        "stdin",
        "run",
        "run_command",
        "agent",
        {
            "executable": "python",
            "arguments": ["-c", "print(input().upper())"],
            "stdin": "hit\n",
        },
    )

    result = asyncio.run(service.execute(request))

    assert result.ok
    assert result.data["stdout"] == "HIT\n"
    assert result.data["stdin_bytes"] == 4


def test_broker_hash_checks_publishes_checkpoints_and_undoes(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "repo"
    recovery = tmp_path / "recovery"
    workspace.mkdir(parents=True)
    target = workspace / "hello.txt"
    target.write_text("old", encoding="utf-8")
    old_hash = hashlib.sha256(b"old").hexdigest()
    new_hash = hashlib.sha256(b"new").hexdigest()
    operation = PublishOperation("hello.txt", "update", old_hash, new_hash, "new")
    manifest = PublishManifest("manifest", "run", workspace_id(workspace), "snapshot", "approval", (operation,))
    broker = WorkspaceBroker(workspace, recovery)
    result = broker.publish(manifest, {"hello.txt"})
    assert target.read_text() == "new"
    assert (recovery / result.checkpoint_id / "files" / "hello.txt").read_text() == "old"
    broker.undo(result.checkpoint_id)
    assert target.read_text() == "old"


def test_broker_rejects_conflict_unapproved_path_and_link(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "repo"
    recovery = tmp_path / "recovery"
    workspace.mkdir(parents=True)
    target = workspace / "hello.txt"
    target.write_text("changed", encoding="utf-8")
    new_hash = hashlib.sha256(b"new").hexdigest()
    manifest = PublishManifest(
        "manifest", "run", workspace_id(workspace), "snapshot", "approval",
        (PublishOperation("hello.txt", "update", hashlib.sha256(b"old").hexdigest(), new_hash, "new"),),
    )
    broker = WorkspaceBroker(workspace, recovery)
    with pytest.raises(BrokerError, match="Approved paths"):
        broker.publish(manifest, set())
    with pytest.raises(BrokerError, match="changed after review"):
        broker.publish(manifest, {"hello.txt"})
    if hasattr(os, "symlink"):
        target.unlink()
        try:
            target.symlink_to(tmp_path / "outside")
        except OSError:
            return
        with pytest.raises(BrokerError):
            broker.publish(manifest, {"hello.txt"})
