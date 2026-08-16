from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from executor.config import ExecutorConfig
from executor.mutations import guard_shrink, sha256
from executor.paths import UnsafePath, assert_unique_paths, normalize_relative, safe_path
from executor.tools.bash import BashRunner, _OutputBuffer
from executor.tools.edit import edit
from executor.tools.find import find
from executor.tools.ls import ls
from executor.tools.read import read
from executor.tools.search_text import search_text
from executor.tools.write import write
from server.agent.budgets import BudgetExceededError, RunBudget


def config(tmp_path: Path, **overrides: object) -> ExecutorConfig:
    values: dict[str, object] = {"source_root": tmp_path / "source", "work_root": tmp_path / "work", "socket_path": tmp_path / "executor.sock", "token": "x" * 32, "workspace_id": "phase6"}
    values.update(overrides)
    return ExecutorConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ExecutorConfig._LIMIT_FIELDS)
def test_every_limit_reports_effective_value_and_enforces_hard_cap(tmp_path: Path, name: str) -> None:
    cfg = config(tmp_path)
    status = cfg.limit_status(name, 10**12)
    assert status["configured"] == getattr(cfg, name)
    assert status["hard_ceiling"] == ExecutorConfig.HARD_CEILINGS[name]
    assert status["effective"] == getattr(cfg, name)
    assert cfg.effective_limit(name, 1) == 1
    with pytest.raises(ValueError, match="positive integers"):
        config(tmp_path, **{name: 0})
    with pytest.raises(ValueError, match="hard ceilings"):
        config(tmp_path, **{name: ExecutorConfig.HARD_CEILINGS[name] + 1})


def test_configuration_contradictions_environment_overrides_and_runtime_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="fit strictly below"):
        config(tmp_path, max_staging_bytes=3_500_000_000, max_checkpoint_bytes=3_500_000_000)
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "phase6-env")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "small")
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "123456")
    cfg = ExecutorConfig.from_environment()
    assert cfg.max_read_bytes == 123456
    assert cfg.sources["max_read_bytes"] == "environment:LOCAL_CHAT_MAX_READ_BYTES"
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "bad")
    with pytest.raises(ValueError, match="positive integer"):
        ExecutorConfig.from_environment()


@pytest.mark.parametrize("size", (16, 70_000, 200_000))
def test_read_small_medium_large_and_utf8_metadata(tmp_path: Path, size: int) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "text.txt"
    path.write_text("x" * size, encoding="utf-8")
    text, metadata = read(root, {"path": "text.txt", "max_bytes": 1_000}, max_bytes=1_000)
    assert len(text.encode()) == min(size, 1_000)
    assert metadata["truncated"] is (size > 1_000)
    assert metadata["sha256"] == sha256(path)
    unicode_path = root / "unicode.txt"
    unicode_path.write_text("a\n€uro\n", encoding="utf-8")
    with pytest.raises(ValueError, match="UTF-8 character boundary"):
        read(root, {"path": "unicode.txt", "offset": 3}, max_bytes=100)


def test_read_binary_invalid_utf8_and_ranges_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "binary").write_bytes(b"a\x00b")
    (root / "invalid").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="Binary files"):
        read(root, {"path": "binary"}, max_bytes=100)
    with pytest.raises(ValueError, match="valid UTF-8"):
        read(root, {"path": "invalid"}, max_bytes=100)
    (root / "lines").write_text("one\ntwo\nthree\n", encoding="utf-8")
    text, metadata = read(root, {"path": "lines", "start_line": 2, "end_line": 2}, max_bytes=100)
    assert text == "two"
    assert metadata["start_line"] == 2


def test_write_edit_hash_occurrence_result_and_shrink_guards(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write(root, {"path": "file.txt", "content": "alpha\nbeta\n"}, max_bytes=100)
    with pytest.raises(ValueError, match="Staging hash conflict"):
        write(root, {"path": "file.txt", "content": "x", "expected_sha256": "0" * 64}, max_bytes=100)
    old_hash = sha256(root / "file.txt")
    _, result = edit(root, {"path": "file.txt", "old_str": "beta", "new_str": "gamma", "expected_occurrences": 1, "expected_sha256": old_hash}, max_target_bytes=100, max_result_bytes=100)
    assert result["old_sha256"] == old_hash
    with pytest.raises(ValueError, match="occurrence conflict"):
        edit(root, {"path": "file.txt", "old_str": "missing", "new_str": "x"}, max_target_bytes=100, max_result_bytes=100)
    with pytest.raises(ValueError, match="mutation limit"):
        write(root, {"path": "large", "content": "x" * 101}, max_bytes=100)
    large = root / "large-edit"
    large.write_text("line\n" * 50, encoding="utf-8")
    with pytest.raises(Exception, match="shrink"):
        guard_shrink("large-edit", large, "line\n" * 20)


def test_search_scan_budget_and_pagination_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "small.txt").write_text("needle\n", encoding="utf-8")
    (root / "large.txt").write_text("needle\n" * 20, encoding="utf-8")
    output, metadata = search_text(root, {"path": ".", "query": "needle", "max_results": 1}, max_bytes=20, max_results=1)
    assert "small.txt:1:needle" in output
    assert metadata["files_skipped_too_large"] == 1
    assert metadata["files_searched"] == 1
    assert metadata["scan_scope_complete"] is False


def test_find_and_ls_pagination_traversal_and_secret_filtering(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.txt").write_text("x", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    first, meta = find(root, {"path": ".", "glob": "*.txt", "max_results": 2}, max_results=2)
    second, _ = find(root, {"path": ".", "glob": "*.txt", "max_results": 2, "cursor": meta["next_cursor"]}, max_results=2)
    assert meta["truncated"] and set(first.splitlines()).isdisjoint(second.splitlines())
    first, meta = ls(root, {"path": ".", "max_results": 2}, max_results=2)
    second, _ = ls(root, {"path": ".", "max_results": 2, "cursor": meta["next_cursor"]}, max_results=2)
    assert meta["truncated"] and set(first.splitlines()).isdisjoint(second.splitlines())
    assert ".env" not in first and ".env" not in second
    with pytest.raises(UnsafePath):
        find(root, {"path": "../", "max_results": 1}, max_results=1)


def test_path_traversal_links_hard_links_and_secret_restrictions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for value in ("../outside", "/absolute", "C:/drive", ".ssh/id_rsa", "credentials/key"):
        with pytest.raises(UnsafePath):
            normalize_relative(value)
    assert_unique_paths(["src/File.py"])
    with pytest.raises(UnsafePath, match="collision"):
        assert_unique_paths(["src/File.py", "src/file.py"])
    outside = tmp_path / "outside"
    outside.write_text("x", encoding="utf-8")
    try:
        (root / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePath, match="Symbolic links"):
        safe_path(root, "link")
    (root / "source").write_text("x", encoding="utf-8")
    os.link(root / "source", root / "hard")
    with pytest.raises(ValueError, match="hard-linked"):
        read(root, {"path": "hard"}, max_bytes=100)


def test_bash_limits_output_retention_and_environment_policy() -> None:
    with pytest.raises(ValueError, match="command exceeds"):
        asyncio.run(BashRunner().run("cmd", Path("."), {"command": "x" * 1_000_001}, max_seconds=10, max_output=100))
    with pytest.raises(ValueError, match="stdin exceeds"):
        asyncio.run(BashRunner().run("stdin", Path("."), {"command": "true", "stdin": "x" * 8_000_001}, max_seconds=10, max_output=100))
    with pytest.raises(ValueError, match="restricted"):
        BashRunner._environment({"PATH": "unsafe"})
    output = _OutputBuffer(8)
    output.append(b"abcdefghijk")
    assert output.truncated and b"output truncated" in output.bytes()


def test_bash_failure_and_timeout_roll_back_staging(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "state"
    target.write_text("before", encoding="utf-8")
    _, result = asyncio.run(BashRunner().run("failure", root, {"command": "printf after > state; exit 7"}, max_seconds=10, max_output=100))
    assert result["rolled_back"] is True and target.read_text(encoding="utf-8") == "before"
    with pytest.raises(TimeoutError):
        asyncio.run(BashRunner().run("timeout", root, {"command": "printf after > state; sleep 5", "timeout_seconds": 1}, max_seconds=1, max_output=100))
    assert target.read_text(encoding="utf-8") == "before"


def test_budget_iteration_tool_wall_and_cost_exhaustion_and_remaining_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    for expected, attribute, value in (("iteration", "iterations", 3), ("tool-call", "tool_calls", 3), ("cost", "cost", 2.0)):
        budget = RunBudget(2, 5, 1.0, max_tool_calls=2)
        setattr(budget, attribute, value)
        with pytest.raises(BudgetExceededError, match=expected):
            budget.check()
    budget = RunBudget(10, 5, 2.0, max_tool_calls=10)
    started = budget.started
    budget.iterations = 3
    budget.tool_calls = 4
    budget.cost = 0.75
    monkeypatch.setattr("server.agent.budgets.time.monotonic", lambda: started + 2)
    snapshot = budget.snapshot()
    assert snapshot["remaining_iterations"] == 7
    assert snapshot["remaining_tool_calls"] == 6
    assert snapshot["remaining_wall_seconds"] == pytest.approx(3)
    assert snapshot["remaining_cost"] == pytest.approx(1.25)
    monkeypatch.setattr("server.agent.budgets.time.monotonic", lambda: started + 6)
    with pytest.raises(BudgetExceededError, match="wall-time"):
        budget.check()


def test_reparse_point_security_contract_and_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.write_text("x", encoding="utf-8")

    class FakeStat:
        st_mode = 0o100644
        st_file_attributes = 0x400

    class FakeStatResult:
        st_file_attributes = 0x400

    monkeypatch.setattr("executor.paths.os.stat_result", FakeStatResult)
    monkeypatch.setattr(Path, "lstat", lambda self: FakeStat())
    with pytest.raises(UnsafePath, match="reparse"):
        safe_path(root, "target")
    assert sha256(target) == sha256(target)
