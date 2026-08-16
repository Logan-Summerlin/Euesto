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
    def mark_staging_published(self, _manifest) -> dict:
        raise AttributeError("mark_staging_published is unavailable")


class CleanGateway:
    def __init__(self) -> None:
        self.manifests: list[object] = []

    def mark_staging_published(self, manifest) -> dict:
        assert manifest.workspace_id == "workspace-1"
        self.manifests.append(manifest)
        return {"snapshot_id": "snapshot-2", "file_count": 1}


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
            "reseed_error": "Unexpected staging baseline error: mark_staging_published is unavailable",
        }
    ]


def test_publication_marks_handoff_safe_only_after_manifest_baseline_update(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    gateway = CleanGateway()
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=gateway
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert len(gateway.manifests) == 1
    assert completed[0]["reseeded"] is True
    assert "reseed_error" not in completed[0]
