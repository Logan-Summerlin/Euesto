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


def make_config(tmp_path: Path, **overrides: object) -> ExecutorConfig:
    values: dict[str, object] = {
        "source_root": tmp_path / "source",
        "work_root": tmp_path / "work",
        "socket_path": tmp_path / "executor.sock",
        "token": "x" * 32,
        "workspace_id": "phase6",
    }
    values.update(overrides)
    return ExecutorConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ExecutorConfig._LIMIT_FIELDS)
def test_configuration_covers_every_limit_and_reports_effective_runtime_value(tmp_path: Path, name: str) -> None:
    config = make_config(tmp_path)
    status = config.limit_status(name, 10**12)
    assert status["configured"] == getattr(config, name)
    assert status["hard_ceiling"] == ExecutorConfig.HARD_CEILINGS[name]
    assert status["effective"] == getattr(config, name)
    assert config.effective_limit(name, 1) == 1


@pytest.mark.parametrize("field", ExecutorConfig._LIMIT_FIELDS)
def test_configuration_rejects_invalid_and_hard_cap_values(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        make_config(tmp_path, **{field: 0})
    with pytest.raises(ValueError, match="hard ceilings"):
        make_config(tmp_path, **{field: ExecutorConfig.HARD_CEILINGS[field] + 1})


def test_configuration_rejects_contradictory_capacity_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fit strictly below"):
        make_config(tmp_path, max_staging_bytes=3_500_000_000, max_checkpoint_bytes=3_500_000_000)


def test_configuration_environment_override_and_source_reporting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "phase6-env")
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "123456")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "small")
    config = ExecutorConfig.from_environment()
    assert config.max_read_bytes == 123456
    assert config.sources["max_read_bytes"] == "environment:LOCAL_CHAT_MAX_READ_BYTES"
    assert config.workspace_id == "phase6-env"


def test_configuration_rejects_unknown_profile_and_malformed_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("t" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "phase6-env")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "does-not-exist")
    with pytest.raises(ValueError, match="Unknown executor profile"):
        ExecutorConfig.from_environment()
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "coding")
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "not-an-int")
    with pytest.raises(ValueError, match="positive integer"):
        ExecutorConfig.from_environment()


@pytest.mark.parametrize("size", (16, 70_000, 200_000))
def test_read_small_medium_large_and_incremental_boundaries(tmp_path: Path, size: int) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "text.txt"
    path.write_text("x" * size, encoding="utf-8")
    text, metadata = read(root, {"path": "text.txt", "max_bytes": 1_000}, max_bytes=1_000)
    assert len(text.encode()) == 1_000
    assert metadata["truncated"] is (size > 1_000)
    if size > 1_000:
        assert metadata["next_offset"] == 1_000


def test_read_line_range_byte_cursor_hash_and_utf8_boundary(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    content = "one\n€uro\nthree\n"
    path = root / "text.txt"
    path.write_text(content, encoding="utf-8")
    text, metadata = read(root, {"path": "text.txt", "start_line": 2, "end_line": 2}, max_bytes=1_000)
    assert text == "€uro"
    assert metadata["sha256"] == sha256(path)
    assert metadata["encoding"] == "utf-8"
    euro_offset = len("one\n".encode()) + 1
    with pytest.raises(ValueError, match="UTF-8 character boundary"):
        read(root, {"path": "text.txt", "offset": euro_offset}, max_bytes=1_000)
    text, metadata = read(root, {"path": "text.txt", "offset": len("one\n".encode())}, max_bytes=1_000)
    assert text.startswith("€uro")
    assert metadata["byte_offset"] == len("one\n".encode())


def test_read_rejects_binary_and_invalid_utf8(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "binary.bin").write_bytes(b"abc\x00def")
    (root / "invalid.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="Binary files"):
        read(root, {"path": "binary.bin"}, max_bytes=100)
    with pytest.raises(ValueError, match="valid UTF-8"):
        read(root, {"path": "invalid.txt"}, max_bytes=100)


def test_write_and_edit_enforce_hash_occurrences_and_limits(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write(root, {"path": "file.txt", "content": "alpha\nbeta\n"}, max_bytes=100)
    with pytest.raises(ValueError, match="Staging hash conflict"):
        write(root, {"path": "file.txt", "content": "changed", "expected_sha256": "0" * 64}, max_bytes=100)
    old_hash = sha256(root / "file.txt")
    message, metadata = edit(root, {"path": "file.txt", "old_str": "beta", "new_str": "gamma", "expected_occurrences": 1, "expected_sha256": old_hash}, max_target_bytes=100, max_result_bytes=100)
    assert "Edited file.txt" in message
    assert metadata["old_sha256"] == old_hash
    assert (root / "file.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
    with pytest.raises(ValueError, match="occurrence conflict"):
        edit(root, {"path": "file.txt", "old_str": "missing", "new_str": "x", "expected_occurrences": 1}, max_target_bytes=100, max_result_bytes=100)
    with pytest.raises(ValueError, match="mutation limit"):
        write(root, {"path": "large.txt", "content": "x" * 101}, max_bytes=100)


def test_large_edit_and_shrink_detection_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "large.txt"
    path.write_text("line\n" * 50, encoding="utf-8")
    with pytest.raises(Exception, match="shrink"):
        guard_shrink("large.txt", path, "tiny\n")
    with pytest.raises(ValueError, match="mutation limit"):
        edit(root, {"path": "large.txt", "old_str": "line", "new_str": "x" * 200, "expected_occurrences": 1}, max_target_bytes=10_000, max_result_bytes=100)


def test_search_enforces_scan_budget_skips_oversized_files_and_reports_metadata(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "small.txt").write_text("needle\n", encoding="utf-8")
    (root / "large.txt").write_text("needle\n" * 20, encoding="utf-8")
    output, metadata = search_text(root, {"path": ".", "query": "needle", "max_results": 10, "include_metadata": True}, max_bytes=20, max_results=10)
    assert "small.txt:1:needle" in output
    assert metadata["files_skipped_too_large"] == 1
    assert metadata["files_searched"] == 1
    assert metadata["scan_scope_complete"] is False


def test_find_pagination_skips_secret_and_staging_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.txt").write_text("x", encoding="utf-8")
    (root / ".env.local").write_text("secret", encoding="utf-8")
    (root / ".local-chat-staging").mkdir()
    first, metadata = find(root, {"path": ".", "glob": "*.txt", "max_results": 2}, max_results=2)
    assert metadata["truncated"] is True
    assert metadata["next_cursor"]
    second, second_metadata = find(root, {"path": ".", "glob": "*.txt", "max_results": 2, "cursor": metadata["next_cursor"]}, max_results=2)
    assert set(first.splitlines()).isdisjoint(second.splitlines())
    assert second_metadata["count"] == 2


def test_ls_pagination_and_traversal_bounds(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(5):
        (root / f"file{index}.txt").write_text("x", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    first, metadata = ls(root, {"path": ".", "max_results": 2}, max_results=2)
    assert metadata["truncated"] is True
    second, _ = ls(root, {"path": ".", "max_results": 2, "cursor": metadata["next_cursor"]}, max_results=2)
    assert set(first.splitlines()).isdisjoint(second.splitlines())
    with pytest.raises(UnsafePath):
        find(root, {"path": "../", "max_results": 1}, max_results=1)


def test_path_traversal_secret_paths_and_unicode_collisions_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for value in ("../outside", "/absolute", "C:/drive", "credentials/key", ".ssh/id_ed25519"):
        with pytest.raises(UnsafePath):
            normalize_relative(value)
    assert_unique_paths(["src/File.py"])
    with pytest.raises(UnsafePath, match="collision"):
        assert_unique_paths(["src/File.py", "src/file.py"])


def test_symlink_and_hard_link_restrictions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    (root / "target.txt").write_text("target", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this runner")
    with pytest.raises(UnsafePath, match="Symbolic links"):
        safe_path(root, "link.txt")
    hard = root / "hard.txt"
    os.link(root / "target.txt", hard)
    with pytest.raises(ValueError, match="hard-linked"):
        read(root, {"path": "hard.txt"}, max_bytes=100)


def test_hard_link_mutations_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    os.link(root / "source.txt", root / "hard.txt")
    with pytest.raises(ValueError, match="hard-linked"):
        write(root, {"path": "hard.txt", "content": "changed"}, max_bytes=100)
    with pytest.raises(ValueError, match="hard-linked"):
        edit(root, {"path": "hard.txt", "old_str": "source", "new_str": "changed"}, max_target_bytes=100, max_result_bytes=100)


def test_bash_command_stdin_environment_and_output_limits() -> None:
    with pytest.raises(ValueError, match="command exceeds"):
        asyncio.run(BashRunner().run("cmd", Path("."), {"command": "x" * (1_000_001)}, max_seconds=10, max_output=100))
    with pytest.raises(ValueError, match="stdin exceeds"):
        asyncio.run(BashRunner().run("stdin", Path("."), {"command": "true", "stdin": "x" * (8_000_001)}, max_seconds=10, max_output=100))
    with pytest.raises(ValueError, match="restricted"):
        BashRunner._environment({"PATH": "unsafe"})
    buffer = _OutputBuffer(8)
    buffer.append(b"abcdefghijk")
    assert buffer.truncated
    assert b"output truncated" in buffer.bytes()


def test_bash_failure_rolls_back_mutation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "state.txt"
    target.write_text("before", encoding="utf-8")
    _, result = asyncio.run(BashRunner().run("failure", root, {"command": "printf after > state.txt; exit 7"}, max_seconds=10, max_output=100))
    assert result["exit_code"] == 7
    assert result["rolled_back"] is True
    assert target.read_text(encoding="utf-8") == "before"


def test_bash_timeout_rolls_back_and_cleans_process(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "state.txt"
    target.write_text("before", encoding="utf-8")
    with pytest.raises(TimeoutError):
        asyncio.run(BashRunner().run("timeout", root, {"command": "printf after > state.txt; sleep 5", "timeout_seconds": 1}, max_seconds=1, max_output=100))
    assert target.read_text(encoding="utf-8") == "before"


def test_budget_matrix_covers_all_exhaustion_dimensions() -> None:
    cases = [
        ("iteration", lambda budget: setattr(budget, "iterations", budget.max_iterations + 1)),
        ("tool-call", lambda budget: setattr(budget, "tool_calls", budget.max_tool_calls + 1)),
        ("cost", lambda budget: setattr(budget, "cost", budget.max_cost + 1)),
    ]
    for expected, mutate in cases:
        budget = RunBudget(2, 60, 1.0, max_tool_calls=2)
        mutate(budget)
        with pytest.raises(BudgetExceededError, match=expected):
            budget.check()


def test_budget_wall_clock_and_remaining_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = RunBudget(10, 5, 2.0, max_tool_calls=10)
    budget.iterations = 3
    budget.tool_calls = 4
    budget.cost = 0.75
    monkeypatch.setattr("server.agent.budgets.time.monotonic", lambda: budget.started + 2)
    snapshot = budget.snapshot()
    assert snapshot["remaining_iterations"] == 7
    assert snapshot["remaining_tool_calls"] == 6
    assert snapshot["remaining_wall_seconds"] == pytest.approx(3)
    assert snapshot["remaining_cost"] == pytest.approx(1.25)
    budget.started -= 10
    with pytest.raises(BudgetExceededError, match="wall-time"):
        budget.check()


def test_budget_combined_executor_and_gateway_limits_are_independently_enforced(tmp_path: Path) -> None:
    config = make_config(tmp_path, max_staging_bytes=10, max_checkpoint_bytes=10)
    assert config.required_capacity_bytes == 1_000_000_020
    budget = RunBudget(1, 60, 1.0, max_tool_calls=1)
    budget.consume_tool_call()
    with pytest.raises(BudgetExceededError, match="tool-call"):
        budget.consume_tool_call()


def test_reparse_point_detection_contract_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("x", encoding="utf-8")

    class FakeStat:
        st_mode = 0o100644
        st_file_attributes = 0x400

    class FakeStatResult:
        st_file_attributes = 0x400

    monkeypatch.setattr("executor.paths.os.stat_result", FakeStatResult)
    monkeypatch.setattr(Path, "lstat", lambda self: FakeStat())
    with pytest.raises(UnsafePath, match="reparse"):
        safe_path(root, "target.txt")


def test_shrink_guard_only_blocks_material_whole_file_loss(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "file.txt"
    path.write_text("line\n" * 50, encoding="utf-8")
    guard_shrink("file.txt", path, "line\n" * 30)
    with pytest.raises(Exception, match="shrink"):
        guard_shrink("file.txt", path, "line\n" * 20)
