from __future__ import annotations

from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.staging import advance_published_staging, sha256_file, workspace_changes
from shared.tools import PublishManifest, PublishOperation
from src.workspace_broker import WorkspaceBroker
from tests.helpers import executor_config


def _service(tmp_path: Path, **files: str) -> tuple[ExecutorService, Path, Path]:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    for name, text in files.items():
        (source / name).write_text(text, encoding="utf-8")
    return ExecutorService(executor_config(source)), source, work


def _publish(service: ExecutorService, source: Path, tmp_path: Path) -> PublishManifest:
    manifest = service.manifest("run-1", "approval-1")
    WorkspaceBroker(source, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )
    service.mark_published(manifest)
    return manifest


def _changes(service: ExecutorService, work: Path) -> list[tuple[str, str]]:
    return [
        (change.path, change.operation) for change in workspace_changes(service.snapshot, work)
    ]


def test_mark_published_advances_the_baseline_without_touching_staged_files(
    tmp_path: Path,
) -> None:
    service, source, work = _service(tmp_path)
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    assert _changes(service, work) == [("created.txt", "create")]

    manifest = _publish(service, source, tmp_path)

    assert manifest.operations[0].staged_sha256 == sha256_file(staged)
    assert _changes(service, work) == []
    assert staged.read_text(encoding="utf-8") == "published"


def test_changes_after_publication_compare_against_the_published_state(tmp_path: Path) -> None:
    service, source, work = _service(tmp_path)
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    manifest = _publish(service, source, tmp_path)

    staged.write_text("follow-up change", encoding="utf-8")

    changes = workspace_changes(service.snapshot, work)
    assert [(change.path, change.operation) for change in changes] == [
        ("created.txt", "update")
    ]
    assert changes[0].base_sha256 == manifest.operations[0].staged_sha256


def test_deleting_a_published_file_is_a_pending_delete(tmp_path: Path) -> None:
    service, source, work = _service(tmp_path)
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    _publish(service, source, tmp_path)

    staged.unlink()

    assert _changes(service, work) == [("created.txt", "delete")]


def test_publication_baseline_advances_only_published_paths(tmp_path: Path) -> None:
    service, _source, work = _service(
        tmp_path, **{"agent.py": "print('base')", "notes.txt": "base"}
    )
    snapshot = service.snapshot
    agent_path = work / "agent.py"
    agent_path.write_text("print('published')", encoding="utf-8")
    (work / "notes.txt").write_text("still unpublished", encoding="utf-8")
    operation = PublishOperation(
        "agent.py",
        "update",
        snapshot.hashes["agent.py"],
        sha256_file(agent_path),
        agent_path.read_text(encoding="utf-8"),
        snapshot.modes["agent.py"],
        snapshot.modes["agent.py"],
    )

    published = advance_published_staging(work, snapshot, [operation])

    changes = workspace_changes(published, work)
    assert [(change.path, change.operation) for change in changes] == [("notes.txt", "update")]
    assert published.hashes["agent.py"] == operation.staged_sha256
    assert agent_path.exists()


def test_mark_published_rejects_staged_content_newer_than_the_manifest(tmp_path: Path) -> None:
    service, _source, work = _service(tmp_path, **{"created.txt": "base"})
    staged = work / "created.txt"
    staged.write_text("published", encoding="utf-8")
    manifest = service.manifest("run-1", "approval-1")
    staged.write_text("newer staged change", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no longer matches"):
        service.mark_published(manifest)
