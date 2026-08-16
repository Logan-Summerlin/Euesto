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


class FakeGateway:
    def __init__(self) -> None:
        self.manifests: list[object] = []

    def mark_staging_published(self, manifest) -> dict:
        self.manifests.append(manifest)
        return {"snapshot_id": "snapshot-2", "file_count": 1}


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id="workspace-1",
        operations=(SimpleNamespace(path="created.txt"),),
    )


def test_publication_handoff_marks_manifest_published_before_completion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    gateway = FakeGateway()
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=gateway
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert len(gateway.manifests) == 1
    assert gateway.manifests[0].workspace_id == "workspace-1"
    assert completed == [
        {
            "completed_paths": ["created.txt"],
            "checkpoint_id": "checkpoint-1",
            "reseeded": True,
        }
    ]


def test_publication_handoff_reports_baseline_failure_instead_of_claiming_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    gateway = FakeGateway()
    gateway.mark_staging_published = lambda _manifest: (_ for _ in ()).throw(
        RuntimeError("Published staging no longer matches the manifest: created.txt")
    )
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=gateway
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert completed[0]["reseeded"] is False
    assert "no longer matches the manifest" in completed[0]["reseed_error"]
