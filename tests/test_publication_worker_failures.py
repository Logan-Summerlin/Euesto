from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.workers import PublicationWorker


class FakeBroker:
    def __init__(self, _workspace_root: Path, _recovery_root: Path) -> None:
        pass

    def publish(self, _manifest, paths):
        assert paths == {"created.txt"}
        return SimpleNamespace(completed_paths=("created.txt",), checkpoint_id="checkpoint-1")


class BrokenGateway:
    def inspect_staging(self, _workspace_id: str) -> dict:
        raise AttributeError("inspect_staging is unavailable")


class CleanGateway:
    def inspect_staging(self, workspace_id: str) -> dict:
        assert workspace_id == "workspace-1"
        return {"data": {"unpublished_changes": False}}


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id="workspace-1",
        operations=(SimpleNamespace(path="created.txt"),),
    )


def test_publication_reports_unexpected_baseline_error_instead_of_leaking_from_thread(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    completed: list[dict] = []
    failed: list[str] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=BrokenGateway()
    )
    worker.complete.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.run()

    assert failed == []
    assert completed == [
        {
            "completed_paths": ["created.txt"],
            "checkpoint_id": "checkpoint-1",
            "reseeded": False,
            "reseed_error": "Unexpected staging baseline error: inspect_staging is unavailable",
        }
    ]


def test_publication_marks_handoff_safe_only_after_clean_reconciliation(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=CleanGateway()
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert completed[0]["reseeded"] is True
    assert "reseed_error" not in completed[0]
