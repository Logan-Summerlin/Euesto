from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.mutations import bounded_diff
from shared.tools import ToolRequest
from src.workspace_broker import WorkspaceBroker, workspace_id


def make_config(source: Path, work: Path) -> ExecutorConfig:
    return ExecutorConfig(source, work, work.parent / "executor.sock", "t" * 43, "workspace")


def test_edit_reports_actual_changed_lines(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.mkdir(); (source / "example.py").write_text("a\nb\nc\n", encoding="utf-8")
    service = ExecutorService(make_config(source, tmp_path / "work"))

    result = asyncio.run(service.execute(ToolRequest("r1", "run", "edit", "agent", {"path": "example.py", "old_str": "b", "new_str": "B"})))

    assert result.ok
    assert "Changed 1 line." in result.output
    assert result.data["diff"]["changed_lines"] == 2
    assert "-b" in result.data["diff"]["text"]
    assert "+B" in result.data["diff"]["text"]
    assert "[diff truncated" not in result.data["diff"]["text"]


def test_bounded_diff_labels_truncation(tmp_path: Path) -> None:
    old = "\n".join(f"old-{i}" for i in range(500))
    new = "\n".join(f"new-{i}" for i in range(500))
    diff = bounded_diff(tmp_path / "large.txt", old, new)

    assert diff["truncated"] is True
    assert "[diff truncated; showing a bounded preview]" in diff["text"]


def test_write_includes_human_readable_preview(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.mkdir(); (source / "example.py").write_text("print('old')\n", encoding="utf-8")
    service = ExecutorService(make_config(source, tmp_path / "work"))

    result = asyncio.run(service.execute(ToolRequest("r1", "run", "write", "agent", {"path": "example.py", "content": "print('new')\n"})))

    assert result.ok
    assert "example.py" in result.output
    assert "Changed 2 lines." in result.output
    assert result.data["diff"]["added_lines"] == 1
    assert result.data["diff"]["removed_lines"] == 1


def test_status_reports_staged_changes_and_publication_review(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.mkdir(); (source / "example.py").write_text("print('ok')\n", encoding="utf-8")
    service = ExecutorService(make_config(source, tmp_path / "work"))
    (service.config.work_root / "new.py").write_text("print('new')\n", encoding="utf-8")

    status = service.workspace_status()

    assert status["created"] == ["new.py"]
    assert status["staged"] is True
    assert status["publication"] == "pending_review"
    assert "host publication pending review" in status["summary"]


def test_chmod_is_preserved_in_staging_and_publication(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"; workspace.mkdir(); script = workspace / "run.sh"; script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8"); script.chmod(0o644)
    config = ExecutorConfig(workspace, tmp_path / "work", tmp_path / "executor.sock", "t" * 43, workspace_id(workspace))
    service = ExecutorService(config)
    staged = service.config.work_root / "run.sh"
    assert stat.S_IMODE(staged.stat().st_mode) == 0o644

    result = asyncio.run(service.execute(ToolRequest("r1", "run", "bash", "agent", {"command": "chmod +x run.sh"})))
    assert result.ok
    assert stat.S_IMODE(staged.stat().st_mode) == 0o755
    assert result.data["workspace_status"]["permission_changes"] == ["run.sh"]

    manifest = service.manifest("run", "approval")
    operation = manifest.operations[0]
    assert operation.base_mode == 0o644
    assert operation.staged_mode == 0o755

    published = WorkspaceBroker(workspace, tmp_path / "recovery").publish(manifest, {"run.sh"})
    assert published.completed_paths == ("run.sh",)
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
