"""Full Bash output kept as artifacts (plan 1.5), read limits (0.5, 1.4), and the snapshot (1.7)."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.snapshot import MAX_AGENTS_MD_BYTES, agents_md, workspace_snapshot
from executor.tools.read import DEFAULT_READ_BYTES, read
from runner.process import OUTPUT_DIRECTORY, OutputLog, prune_logs
from shared.tools import ToolRequest
from tests.helpers import executor_config


def run(service: ExecutorService, tool: str, arguments: dict, request_id: str = "req"):
    return asyncio.run(
        service.execute(ToolRequest(request_id, "run", tool, "agent", arguments))
    )


@pytest.fixture
def service(tmp_path: Path) -> ExecutorService:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a\n", encoding="utf-8")
    return ExecutorService(executor_config(source))


# 1.5 full output artifacts


@pytest.mark.posix
def test_bash_keeps_the_full_interleaved_log_and_names_it(service: ExecutorService) -> None:
    command = "python3 -c \"import sys; [print(i) for i in range(50000)]; print('ERR', file=sys.stderr)\""
    result = run(service, "bash", {"command": command}, "req-log")
    assert result.ok
    assert result.data["log_path"] == f"{OUTPUT_DIRECTORY}/req-log.log"
    log = service.config.work_root / OUTPUT_DIRECTORY / "req-log.log"
    content = log.read_text(encoding="utf-8")
    assert "49999" in content and "ERR" in content
    assert result.data["log_bytes"] == len(content.encode())


@pytest.mark.posix
def test_logs_are_hidden_from_tools_status_checkpoints_and_manifests(
    service: ExecutorService,
) -> None:
    assert run(service, "bash", {"command": "echo hi"}, "req-hidden").ok
    for tool, arguments in (
        ("ls", {}),
        ("find", {"pattern": "*.log"}),
        ("grep", {"query": "hi"}),
    ):
        result = run(service, tool, arguments)
        assert OUTPUT_DIRECTORY not in result.output, tool
    assert not run(service, "read", {"path": f"{OUTPUT_DIRECTORY}/req-hidden.log"}).ok
    status = run(service, "status", {})
    assert status.data["total_known"] == 0
    assert service.manifest("run", "approval").operations == ()
    # A failing command rolls back its changes but keeps its log.
    failed = run(service, "bash", {"command": "echo boom; exit 4"}, "req-failed")
    assert failed.data["rolled_back"]
    assert (service.config.work_root / OUTPUT_DIRECTORY / "req-failed.log").exists()


@pytest.mark.posix
def test_a_timeout_names_the_partial_log(service: ExecutorService) -> None:
    result = run(
        service, "bash", {"command": "echo started; sleep 5", "timeout_seconds": 1}, "req-slow"
    )
    assert not result.ok and result.error_code == "tool.timeout"
    assert (service.config.work_root / OUTPUT_DIRECTORY / "req-slow.log").read_text() == (
        "started\n"
    )


@pytest.mark.posix
def test_bash_result_leads_with_a_reduction(service: ExecutorService) -> None:
    command = "printf 'FAILED tests/test_a.py::test_x - assert 1 == 2\\n1 failed, 3 passed in 0.10s\\n'; exit 1"
    result = run(service, "bash", {"command": command})
    assert result.data["reduction"].splitlines()[0] == "pytest: 1 failed, 3 passed"


@pytest.mark.posix
def test_log_pruning_stays_within_age_count_and_size_bounds(tmp_path: Path) -> None:
    directory = tmp_path / OUTPUT_DIRECTORY
    directory.mkdir()
    now = time.time()
    for index in range(10):
        path = directory / f"r{index}.log"
        path.write_bytes(b"x" * 100)
        os.utime(path, (now - 1000 + index, now - 1000 + index))
    old = directory / "old.log"
    old.write_bytes(b"x")
    os.utime(old, (now - 10_000, now - 10_000))
    removed = prune_logs(tmp_path, max_age_seconds=5_000, max_bytes=550, max_files=8, now=now)
    remaining = sorted(path.name for path in directory.iterdir())
    assert "old.log" in removed
    assert remaining == [f"r{index}.log" for index in range(5, 10)]


@pytest.mark.posix
def test_a_log_directory_replaced_by_a_link_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / OUTPUT_DIRECTORY).symlink_to(outside)
    log = OutputLog(work, "req")
    log.write(b"secret")
    log.close()
    assert not log.available
    assert list(outside.iterdir()) == []


def test_log_names_must_be_safe(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OutputLog(tmp_path, "../escape")


# 0.5 and 1.4: read honors the configured limit and reports exact continuations


def test_read_defaults_to_32k_and_honors_larger_configured_limits(tmp_path: Path) -> None:
    lines = [f"line {index:06d} " + "x" * 80 for index in range(5_000)]
    (tmp_path / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    text, data = read(tmp_path, {"path": "big.py"}, max_bytes=1_000_000)
    assert len(text.encode()) <= DEFAULT_READ_BYTES == 32_000
    assert data["truncated"] and data["line_count"] == 5_000
    assert data["next_start_line"] == data["end_line"] + 1
    text, data = read(tmp_path, {"path": "big.py", "max_bytes": 400_000}, max_bytes=1_000_000)
    assert len(text.encode()) > 256_000  # the former fixed clamp no longer applies
    assert data["line_count"] == 5_000


def test_truncated_reads_continue_exactly_and_carry_an_outline(tmp_path: Path) -> None:
    body = ["import os", ""]
    for index in range(60):
        body += [f"def function_{index}():", "    return " + "1 + " * 30 + "1", ""]
    (tmp_path / "mod.py").write_text("\n".join(body), encoding="utf-8")
    first, data = read(tmp_path, {"path": "mod.py", "start_line": 1}, max_bytes=2_000)
    assert data["truncated"]
    continuation = data["next_start_line"]
    assert first.count("\n") + 1 == continuation  # every shown line is complete
    outline = data["outline"]
    assert outline and all(entry["line"] > data["end_line"] for entry in outline)
    assert outline[0]["text"].startswith("def function_")
    second, _ = read(tmp_path, {"path": "mod.py", "start_line": continuation}, max_bytes=2_000)
    lines = (tmp_path / "mod.py").read_text(encoding="utf-8").splitlines()
    assert second.splitlines()[0] == lines[continuation - 1]


def test_a_single_overlong_line_continues_by_offset(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("y" * 5_000, encoding="utf-8")
    _, data = read(tmp_path, {"path": "one.txt", "start_line": 1}, max_bytes=1_000)
    assert data["truncated"] and data["next_start_line"] is None
    assert data["next_offset"] == 1_000


# 1.7 workspace snapshot


def test_workspace_snapshot_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src" / "pkg" / "deep").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.ruff]\n[tool.pytest.ini_options]\n")
    (root / "package.json").write_text('{"scripts": {"test": "vitest", "lint": "eslint ."}}')
    (root / "pnpm-lock.yaml").write_text("")
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "src" / "pkg" / "deep" / "x.ts").write_text("export const x = 1\n")
    (root / "tests" / "test_app.py").write_text("def test(): pass\n")
    (root / "node_modules" / "dep" / "index.js").write_text("")
    (root / ".env").write_text("SECRET=1")

    first = workspace_snapshot(root)
    assert first == workspace_snapshot(root)
    assert first["file_count"] == 6
    assert {"path": "src", "type": "directory", "files": 2} in first["layout"]
    assert {"path": "src/pkg", "type": "directory", "files": 1} in first["layout"]
    assert not any(
        item["path"].startswith(("node_modules", ".env")) for item in first["layout"]
    )
    assert all(item["path"].count("/") <= 1 for item in first["layout"])
    assert first["checks"] == ["ruff check .", "pytest -q", "pnpm test", "pnpm run lint"]
    assert {"name": "Python", "files": 2} in first["languages"]
    assert "pyproject.toml" in first["manifests"]


def test_agents_md_is_bounded_and_never_followed_through_a_link(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("r" * (MAX_AGENTS_MD_BYTES + 10), encoding="utf-8")
    text, truncated = agents_md(tmp_path)
    assert truncated and len(text) == MAX_AGENTS_MD_BYTES
    linked = tmp_path / "linked"
    linked.mkdir()
    if os.name == "posix":
        (linked / "AGENTS.md").symlink_to(tmp_path / "AGENTS.md")
        assert agents_md(linked) == ("", False)


@pytest.mark.posix
def test_a_silent_command_still_names_its_empty_log(service: ExecutorService) -> None:
    result = run(service, "bash", {"command": "exit 3"}, "req-silent")
    assert result.data["log_path"] == f"{OUTPUT_DIRECTORY}/req-silent.log"
    assert result.data["log_bytes"] == 0
