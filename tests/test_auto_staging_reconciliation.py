from __future__ import annotations

from pathlib import Path

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.staging import advance_published_staging, sha256_file, workspace_changes
from shared.tools import PublishOperation
from src.workspace_broker import WorkspaceBroker, workspace_id


def make_config(source: Path, work: Path) -> ExecutorConfig:
    return ExecutorConfig(
        source_root=source,
        work_root=work,
        socket_path=work.parent / "executor.sock",
        token="t" * 43,
        workspace_id=workspace_id(source),
    )


def test_mark_published_advances_only_the_manifest_baseline(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    service = ExecutorService(make_config(source, work))
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")

    manifest = service.manifest("run-1", "approval-1")
    WorkspaceBroker(source, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )

    assert workspace_changes(service.snapshot, work)
    service.mark_published(manifest)
    assert workspace_changes(service.snapshot, work) == []
    assert staged.read_text(encoding="utf-8") == "published"


def test_published_baseline_keeps_later_staging_changes_dirty(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    service = ExecutorService(make_config(source, work))
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")

    manifest = service.manifest("run-1", "approval-1")
    WorkspaceBroker(source, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )
    service.mark_published(manifest)

    staged.write_text("follow-up change", encoding="utf-8")
    assert [(change.path, change.operation) for change in workspace_changes(service.snapshot, work)] == [
        ("created.txt", "update")
    ]


def test_published_baseline_rejects_stale_manifest_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "created.txt").write_text("base", encoding="utf-8")
    service = ExecutorService(make_config(source, work))

    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    manifest = service.manifest("run-1", "approval-1")
    operation = manifest.operations[0]
    staged.write_text("newer staged change", encoding="utf-8")

    try:
        service.mark_published(manifest)
    except RuntimeError as exc:
        assert "no longer matches" in str(exc)
    else:
        raise AssertionError("stale staged content was incorrectly marked published")


def test_manifest_operation_hash_matches_staged_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    service = ExecutorService(make_config(source, work))
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    manifest = service.manifest("run-1", "approval-1")
    operation = manifest.operations[0]

    assert operation.staged_sha256 == sha256_file(staged)
