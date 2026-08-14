from __future__ import annotations

from pathlib import Path

from executor.config import ExecutorConfig
from executor.staging import seed_staging, snapshot_current_staging, workspace_changes


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
