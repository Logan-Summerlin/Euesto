from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.tools import find, grep, ls
from shared.tools import ToolRequest

# executor.tools re-exports the tool functions under their module names, so reach the
# modules through importlib to patch their clocks.
grep_module = importlib.import_module("executor.tools.grep")
find_module = importlib.import_module("executor.tools.find")
ls_module = importlib.import_module("executor.tools.ls")


class SteppingClock:
    """Monotonic clock that advances one second every time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now


def _service(tmp_path: Path, **limits: int) -> ExecutorService:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    config = ExecutorConfig(source_root=source, work_root=tmp_path / "work", socket_path=tmp_path / "executor.sock", token="t" * 43, workspace_id="workspace", **limits)
    return ExecutorService(config)


def _tree(root: Path, count: int = 40) -> None:
    for index in range(count):
        directory = root / f"d{index:03d}"
        directory.mkdir(parents=True)
        (directory / "match.txt").write_text("needle\n", encoding="utf-8")


def _run(service: ExecutorService, tool: str, arguments: dict):
    return asyncio.run(service.execute(ToolRequest(f"{tool}-1", "run", tool, "plan", arguments)))


def test_grep_dispatch_honors_configured_search_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path / "source")
    service = _service(tmp_path, max_search_seconds=5)
    monkeypatch.setattr(grep_module, "time", SteppingClock())

    result = _run(service, "grep", {"query": "needle"})

    assert result.ok, result.to_dict()
    assert result.truncated
    assert result.data["truncation_reason"] == "time_budget"
    assert 0 < result.data["matches_returned"] < 40


def test_search_seconds_override_from_environment_reaches_grep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("t" * 43, encoding="utf-8")
    source = tmp_path / "source"
    _tree(source)
    for name, value in {"LOCAL_CHAT_EXECUTOR_TOKEN_FILE": str(token), "LOCAL_CHAT_SOURCE_ROOT": str(source), "LOCAL_CHAT_WORK_ROOT": str(tmp_path / "work"), "LOCAL_CHAT_WORKSPACE_ID": "workspace", "LOCAL_CHAT_MAX_SEARCH_SECONDS": "1"}.items():
        monkeypatch.setenv(name, value)
    config = ExecutorConfig.from_environment()
    assert config.max_search_seconds == 1
    service = ExecutorService(config)
    monkeypatch.setattr(grep_module, "time", SteppingClock())

    result = _run(service, "grep", {"query": "needle"})

    assert result.data["truncation_reason"] == "time_budget"
    assert result.data["matches_returned"] == 0


def test_find_stops_at_time_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(find_module, "time", SteppingClock())

    output, data = find(tmp_path, {"glob": "*.txt"}, max_results=500, max_seconds=10)

    assert data["truncated"] is True
    assert data["truncation_reason"] == "time_budget"
    assert "next_cursor" not in data
    assert 0 < data["returned"] < 40
    assert all(line.endswith("match.txt") for line in output.splitlines())


def test_ls_stops_at_time_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path)
    monkeypatch.setattr(ls_module, "time", SteppingClock())

    _, data = ls(tmp_path, {}, max_results=500, max_seconds=10)

    assert data["truncated"] is True
    assert data["truncation_reason"] == "time_budget"
    assert "next_cursor" not in data
    assert 0 < data["returned"] < 40


def test_find_and_ls_dispatch_honor_configured_search_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tree(tmp_path / "source")
    service = _service(tmp_path, max_search_seconds=5)
    monkeypatch.setattr(find_module, "time", SteppingClock())
    monkeypatch.setattr(ls_module, "time", SteppingClock())

    for tool in ("find", "ls"):
        result = _run(service, tool, {})
        assert result.ok, result.to_dict()
        assert result.data["truncation_reason"] == "time_budget", tool


def test_untruncated_walks_do_not_report_a_time_budget(tmp_path: Path) -> None:
    _tree(tmp_path, 3)
    _, find_data = find(tmp_path, {"glob": "*.txt"})
    _, ls_data = ls(tmp_path, {})
    assert find_data["truncated"] is False and "truncation_reason" not in find_data
    assert ls_data["truncated"] is False and "truncation_reason" not in ls_data


def test_ls_and_grep_defaults_match_documented_result_counts(tmp_path: Path) -> None:
    for index in range(600):
        (tmp_path / f"f{index:03d}.txt").write_text("needle\n", encoding="utf-8")

    _, ls_data = ls(tmp_path, {"details": False})
    _, find_data = find(tmp_path, {"glob": "*.txt"})
    _, grep_data = grep(tmp_path, {"query": "needle"}, max_scan_bytes=64_000, max_output_bytes=1_000_000)

    assert ls_data["returned"] == ls_module.DEFAULT_LS_RESULTS == 500
    assert find_data["returned"] == find_module.DEFAULT_FIND_RESULTS == 500
    assert grep_data["matches_returned"] == 500
    assert ls_data["truncation_reason"] == find_data["truncation_reason"] == grep_data["truncation_reason"] == "result_limit"


def test_ls_dispatch_default_uses_configured_ls_results(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(600):
        (source / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
    service = _service(tmp_path)

    result = _run(service, "ls", {"details": False})

    assert result.returned == 500
    assert result.truncated


def test_grep_output_clip_is_independent_of_scan_budget(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle " + "x" * 200 + "\n", encoding="utf-8")

    small_scan, small_scan_data = grep(tmp_path, {"query": "needle"}, max_scan_bytes=1_000, max_output_bytes=100_000)
    assert small_scan_data["files_searched"] == 1
    assert "output_truncated" not in small_scan_data
    assert len(small_scan.encode("utf-8")) > 100

    clipped, clipped_data = grep(tmp_path, {"query": "needle"}, max_scan_bytes=64_000_000, max_output_bytes=50)
    assert clipped_data["files_searched"] == 1
    assert clipped_data["output_truncated"] is True
    assert len(clipped.encode("utf-8")) <= 50

    _, skipped_data = grep(tmp_path, {"query": "needle"}, max_scan_bytes=10, max_output_bytes=100_000)
    assert skipped_data["files_skipped_too_large"] == 1
    assert "output_truncated" not in skipped_data
