from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from executor.app import ExecutorService, create_app
from executor.config import ExecutorConfig

TOKEN = "t" * 43


def _profile_config(tmp_path: Path, profile: str) -> ExecutorConfig:
    source = tmp_path / "source"
    source.mkdir()
    return ExecutorConfig(source_root=source, work_root=tmp_path / "work", socket_path=tmp_path / "executor.sock", token=TOKEN, workspace_id="workspace", **ExecutorConfig._profiles()[profile])


def _post_tool(client: TestClient, tool: str, arguments: dict) -> dict:
    body = {"request_id": str(uuid.uuid4()), "run_id": "run", "tool": tool, "mode": "agent", "arguments": arguments}
    response = client.post("/v1/tools", json=body, headers={"authorization": f"Bearer {TOKEN}", "x-executor-nonce": uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("profile", ["small", "coding", "large-workspace"])
def test_write_at_exact_profile_limit_succeeds_end_to_end(tmp_path: Path, profile: str) -> None:
    config = _profile_config(tmp_path, profile)
    service = ExecutorService(config)
    # Newline-heavy content would expand under repr/JSON escaping; it must still fit.
    content = ("x" * 63 + "\n") * (config.max_write_bytes // 64) + "y" * (config.max_write_bytes % 64)
    assert len(content.encode("utf-8")) == config.max_write_bytes

    with TestClient(create_app(config, service)) as client:
        result = _post_tool(client, "write", {"path": "big.txt", "content": content})
        assert result["ok"], result
        assert result["data"]["size_bytes"] == config.max_write_bytes
        staged = (config.work_root / "big.txt").read_bytes()
        assert len(staged) == config.max_write_bytes
        assert hashlib.sha256(staged).hexdigest() == hashlib.sha256(content.encode("utf-8")).hexdigest()

        over = _post_tool(client, "write", {"path": "over.txt", "content": content + "z"})
        assert not over["ok"]
        assert over["error_code"] == "limit.exceeded"
        assert not (config.work_root / "over.txt").exists()


@pytest.mark.posix
@pytest.mark.parametrize("profile", ["coding", "large-workspace"])
def test_bash_stdin_at_exact_profile_limit_succeeds_end_to_end(tmp_path: Path, profile: str) -> None:
    config = _profile_config(tmp_path, profile)
    service = ExecutorService(config)
    stdin = "s" * config.max_bash_stdin_bytes

    with TestClient(create_app(config, service)) as client:
        result = _post_tool(client, "bash", {"command": "wc -c < /dev/stdin > count.txt", "stdin": stdin})
        assert result["ok"], result
        assert (config.work_root / "count.txt").read_text(encoding="utf-8").strip() == str(config.max_bash_stdin_bytes)
