from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.tools.status import MAX_STATUS_DIFF_BYTES, MAX_STATUS_DIFF_LINES
from shared.permissions import PermissionDecision, resolve_permission
from shared.tools import ToolRequest


def _service(tmp_path: Path, files: dict[str, str] | None = None) -> ExecutorService:
    source = tmp_path / "source"
    source.mkdir()
    for relative, content in (files or {}).items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    config = ExecutorConfig(source_root=source, work_root=tmp_path / "work", socket_path=tmp_path / "executor.sock", token="t" * 43, workspace_id="workspace")
    return ExecutorService(config)


def _status(service: ExecutorService, **arguments):
    result = asyncio.run(service.execute(ToolRequest("status-1", "run", "status", "agent", arguments)))
    assert result.ok, result.to_dict()
    return result


def test_status_reports_an_empty_staging_area(tmp_path: Path) -> None:
    result = _status(_service(tmp_path, {"a.txt": "one\n"}))
    assert result.data["staged"] is False
    assert result.data["publication"] == "no_changes"
    assert result.data["changes"] == []
    assert result.data["publication_batches"] == 0
    assert "No staged changes" in result.output


@pytest.mark.posix
def test_status_reports_every_kind_of_change_with_review_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path, {"keep.txt": "keep\n", "edit.txt": "before\n", "gone.txt": "bye\n", "run.sh": "echo hi\n"})
    work = service.config.work_root
    (work / "edit.txt").write_text("after\n", encoding="utf-8")
    (work / "new.txt").write_text("fresh\n", encoding="utf-8")
    (work / "gone.txt").unlink()
    (work / "run.sh").chmod(0o755)
    result = _status(service, include_diffs=True)
    changes = {item["path"]: item for item in result.data["changes"]}
    assert set(changes) == {"edit.txt", "new.txt", "gone.txt", "run.sh"}
    assert changes["edit.txt"]["operation"] == "update" and changes["edit.txt"]["base_sha256"] != changes["edit.txt"]["staged_sha256"]
    assert changes["new.txt"]["operation"] == "create" and changes["new.txt"]["base_sha256"] is None
    assert changes["gone.txt"]["operation"] == "delete" and changes["gone.txt"]["staged_sha256"] is None
    assert changes["run.sh"]["mode_changed"] is True and changes["run.sh"]["staged_mode"] == "755"
    assert result.data["counts"] == {"created": 1, "modified": 2, "deleted": 1, "permission_changes": 1}
    assert result.data["baseline_snapshot_id"] == service.snapshot.snapshot_id
    assert result.data["publication_batches"] == 1
    diffs = {item["path"]: item for item in result.data["diffs"]}
    assert "-before" in diffs["edit.txt"]["text"] and "+after" in diffs["edit.txt"]["text"]
    assert "+fresh" in diffs["new.txt"]["text"] and "-bye" in diffs["gone.txt"]["text"]
    assert diffs["run.sh"]["kind"] == "mode_only"
    assert "M  edit.txt" in result.output and "A  new.txt" in result.output and "D  gone.txt" in result.output


def test_status_excludes_secret_metadata_and_checkpoint_content(tmp_path: Path) -> None:
    service = _service(tmp_path, {"a.txt": "a\n"})
    write = asyncio.run(service.execute(ToolRequest("w", "run", "write", "agent", {"path": "a.txt", "content": "b\n"})))
    assert write.ok and write.data["checkpoint_id"]
    work = service.config.work_root
    (work / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "a.pyc").write_bytes(b"\x00\x01")
    result = _status(service, include_diffs=True)
    reported = [item["path"] for item in result.data["changes"]] + [item["path"] for item in result.data["diffs"]]
    assert reported == ["a.txt", "a.txt"]
    assert "secret" not in result.output and ".local-chat" not in result.output
    with pytest.raises(ValueError):
        ToolRequest("s", "run", "status", "plan", {})
    denied = asyncio.run(service.execute(ToolRequest("s2", "run", "status", "agent", {"paths": [".env"]})))
    assert not denied.ok


def test_status_diffs_are_bounded_by_bytes_and_lines(tmp_path: Path) -> None:
    files = {f"f{index}.txt": "".join(f"line {line} of file {index}\n" for line in range(400)) for index in range(6)}
    service = _service(tmp_path, files)
    work = service.config.work_root
    for relative in files:
        (work / relative).write_text("".join(f"changed {line} with a much longer replacement line body\n" for line in range(400)), encoding="utf-8")
    result = _status(service, include_diffs=True)
    texts = [item.get("text", "") for item in result.data["diffs"]]
    assert result.data["diff_truncated"] is True
    assert sum(len(text.encode("utf-8")) for text in texts) <= MAX_STATUS_DIFF_BYTES
    assert sum(text.count("\n") + 1 for text in texts if text) <= MAX_STATUS_DIFF_LINES
    assert any(item.get("kind") == "omitted" for item in result.data["diffs"]) or any(item.get("truncated") for item in result.data["diffs"])


def test_status_paginates_filters_and_describes_binary_files(tmp_path: Path) -> None:
    service = _service(tmp_path, {"docs/readme.md": "hi\n"})
    work = service.config.work_root
    for index in range(5):
        (work / "src").mkdir(exist_ok=True)
        (work / "src" / f"m{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    (work / "docs" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    first = _status(service, max_results=2)
    assert first.data["returned"] == 2 and first.data["total_known"] == 6 and first.data["truncated"] is True
    second = _status(service, max_results=10, cursor=first.data["next_cursor"])
    assert [item["path"] for item in first.data["changes"] + second.data["changes"]] == ["docs/logo.png", *[f"src/m{index}.py" for index in range(5)]]
    scoped = _status(service, paths=["docs"], include_diffs=True)
    assert [item["path"] for item in scoped.data["changes"]] == ["docs/logo.png"]
    assert scoped.data["diffs"][0]["kind"] == "binary"


def test_status_is_read_only_and_needs_no_approval(tmp_path: Path) -> None:
    request = ToolRequest("s", "run", "status", "agent", {})
    assert resolve_permission(request, "workspace") == PermissionDecision.ALLOW_RUN
    service = _service(tmp_path, {"a.txt": "a\n"})
    (service.config.work_root / "a.txt").write_text("b\n", encoding="utf-8")
    before = service.snapshot
    _status(service, include_diffs=True)
    assert service.snapshot is before
    assert (service.config.work_root / "a.txt").read_text(encoding="utf-8") == "b\n"
    assert not any(path.name.startswith(".local-chat-checkpoints") for path in service.config.work_root.iterdir())
