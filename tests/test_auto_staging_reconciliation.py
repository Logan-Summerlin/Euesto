from __future__ import annotations

from pathlib import Path

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.staging import workspace_changes
from src.workspace_broker import WorkspaceBroker, workspace_id


def make_config(source: Path, work: Path) -> ExecutorConfig:
    return ExecutorConfig(
        source_root=source,
        work_root=work,
        socket_path=work.parent / "executor.sock",
        token="t" * 43,
        workspace_id=workspace_id(source),
    )


def test_reconcile_published_host_clears_stale_baseline_after_host_publication(tmp_path: Path) -> None:
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
    assert service.reconcile_published_host() is True
    assert workspace_changes(service.snapshot, work) == []
    assert staged.read_text(encoding="utf-8") == "published"


def test_reconcile_does_not_hide_unpublished_staging_changes(tmp_path: Path) -> None:
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
    service.mark_published()

    staged.write_text("follow-up change", encoding="utf-8")
    assert service.reconcile_published_host() is False
    assert [(change.path, change.operation) for change in workspace_changes(service.snapshot, work)] == [
        ("created.txt", "update")
    ]


def test_reconcile_is_idempotent_when_staging_is_already_clean(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    service = ExecutorService(make_config(source, work))

    assert service.reconcile_published_host() is False
    assert workspace_changes(service.snapshot, work) == []


def test_reconcile_ignores_host_files_excluded_from_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / ".env.local").write_text("SECRET=ignored", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "package.json").write_text("{}", encoding="utf-8")

    service = ExecutorService(make_config(source, work))
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")

    manifest = service.manifest("run-1", "approval-1")
    WorkspaceBroker(source, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )

    assert service.reconcile_published_host() is True
    assert workspace_changes(service.snapshot, work) == []
