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
    def __init__(self, inspection: dict) -> None:
        self.inspection = inspection
        self.workspace_ids: list[str] = []

    def inspect_staging(self, workspace_id: str) -> dict:
        self.workspace_ids.append(workspace_id)
        return self.inspection


def _manifest() -> SimpleNamespace:
    return SimpleNamespace(
        workspace_id="workspace-1",
        operations=(SimpleNamespace(path="created.txt"),),
    )


def test_publication_handoff_reconciles_baseline_before_completion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    gateway = FakeGateway({"data": {"unpublished_changes": False}})
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=gateway
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert gateway.workspace_ids == ["workspace-1"]
    assert completed == [
        {
            "completed_paths": ["created.txt"],
            "checkpoint_id": "checkpoint-1",
            "reseeded": True,
        }
    ]


def test_publication_handoff_reports_baseline_failure_instead_of_claiming_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.workers.WorkspaceBroker", FakeBroker)
    gateway = FakeGateway({"data": {"unpublished_changes": True}})
    completed: list[dict] = []

    worker = PublicationWorker(
        _manifest(), tmp_path / "workspace", tmp_path / "recovery", reseed_client=gateway
    )
    worker.complete.connect(completed.append)
    worker.run()

    assert completed[0]["reseeded"] is False
    assert "unpublished staging changes" in completed[0]["reseed_error"]
