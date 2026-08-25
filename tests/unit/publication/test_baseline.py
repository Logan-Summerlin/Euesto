from __future__ import annotations

from pathlib import Path

import pytest

from executor.config import ExecutorConfig
from executor.staging import advance_published_staging, seed_staging, sha256_file, snapshot_current_staging, workspace_changes
from shared.tools import PublishOperation


def make_config(source: Path, work: Path) -> ExecutorConfig:
    return ExecutorConfig(
        source,
        work,
        work.parent / "executor.sock",
        "t" * 43,
        "workspace",
    )


def test_mark_published_advances_baseline_without_deleting_staged_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    snapshot = seed_staging(make_config(source, work))

    file_path = work / "blackjack.py"
    file_path.write_text("print('ok')", encoding="utf-8")
    assert [change.path for change in workspace_changes(snapshot, work)] == ["blackjack.py"]

    published_snapshot = snapshot_current_staging(work)

    assert file_path.exists()
    assert workspace_changes(published_snapshot, work) == []


def test_changes_after_publication_are_compared_against_published_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    snapshot = seed_staging(make_config(source, work))

    file_path = work / "blackjack.py"
    file_path.write_text("print('first')", encoding="utf-8")
    published_snapshot = snapshot_current_staging(work)
    assert workspace_changes(published_snapshot, work) == []

    file_path.write_text("print('second')", encoding="utf-8")
    changes = workspace_changes(published_snapshot, work)
    assert [(change.path, change.operation) for change in changes] == [("blackjack.py", "update")]
    assert changes[0].base_sha256 == published_snapshot.hashes["blackjack.py"]


def test_deleted_published_file_is_not_recreated_as_a_pending_change(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    snapshot = seed_staging(make_config(source, work))

    file_path = work / "blackjack.py"
    file_path.write_text("print('ok')", encoding="utf-8")
    published_snapshot = snapshot_current_staging(work)
    file_path.unlink()

    changes = workspace_changes(published_snapshot, work)
    assert [(change.path, change.operation) for change in changes] == [("blackjack.py", "delete")]


def test_publication_baseline_advances_only_published_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "agent.py").write_text("print('base')", encoding="utf-8")
    (source / "notes.txt").write_text("base", encoding="utf-8")
    snapshot = seed_staging(make_config(source, work))

    agent_path = work / "agent.py"
    notes_path = work / "notes.txt"
    agent_path.write_text("print('published')", encoding="utf-8")
    notes_path.write_text("still unpublished", encoding="utf-8")
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


def test_publication_baseline_rejects_newer_staged_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    (source / "agent.py").write_text("print('base')", encoding="utf-8")
    snapshot = seed_staging(make_config(source, work))

    agent_path = work / "agent.py"
    agent_path.write_text("print('published')", encoding="utf-8")
    operation = PublishOperation(
        "agent.py",
        "update",
        snapshot.hashes["agent.py"],
        sha256_file(agent_path),
        agent_path.read_text(encoding="utf-8"),
        snapshot.modes["agent.py"],
        snapshot.modes["agent.py"],
    )
    agent_path.write_text("print('newer')", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no longer matches"):
        advance_published_staging(work, snapshot, [operation])
