from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.staging import publication_batches, workspace_changes
from server.app import create_app
from server.config import GatewayConfig
from server.service import GatewayService
from shared.tools import PUBLISH_BATCH_MAX_OPERATIONS, PublicationReceipt, PublishManifest
from src.workers import PublicationWorker
from src.workspace_broker import BrokerError, PublicationLedger, WorkspaceBroker, describe_progress, workspace_id

TOTAL = 1_200


def _setup(tmp_path: Path) -> tuple[Path, ExecutorService]:
    workspace = tmp_path / "projects" / "codemod"
    (workspace / "src").mkdir(parents=True)
    for index in range(0, TOTAL, 2):
        (workspace / "src" / f"m{index:04d}.py").write_text(f"old_name = {index}\n", encoding="utf-8")
    config = ExecutorConfig(workspace, tmp_path / "work", tmp_path / "executor.sock", "t" * 43, workspace_id(workspace))
    service = ExecutorService(config)
    for index in range(TOTAL):
        (config.work_root / "src" / f"m{index:04d}.py").write_text(f"new_name = {index}\n", encoding="utf-8")
    return workspace, service


def _publish(broker: WorkspaceBroker, manifest: PublishManifest):
    return broker.publish(manifest, {item.path for item in manifest.operations})


def test_large_changeset_publishes_in_sequential_batches_and_resumes_after_failure(tmp_path: Path) -> None:
    workspace, service = _setup(tmp_path)
    broker = WorkspaceBroker(workspace, tmp_path / "recovery")
    ledger = PublicationLedger(tmp_path / "recovery")

    first = service.manifest("run", "approval-1")
    assert (first.batch_index, first.batch_count, len(first.operations)) == (1, 3, PUBLISH_BATCH_MAX_OPERATIONS)
    _publish(broker, first)
    service.mark_published(PublicationReceipt.from_manifest(first))
    ledger.record(first, "published")

    second = service.manifest("run", "approval-2", publication_id=first.publication_id, batch_index=2)
    assert second.publication_id == first.publication_id
    assert (second.batch_index, second.batch_count, len(second.operations)) == (2, 3, PUBLISH_BATCH_MAX_OPERATIONS)
    assert not {item.path for item in first.operations} & {item.path for item in second.operations}

    # Someone edits a host file mid-sequence: batch 2 fails and is rolled back as a unit.
    conflict = second.operations[250]
    original = (workspace / conflict.path).read_bytes() if (workspace / conflict.path).exists() else None
    (workspace / conflict.path).write_text("human edit\n", encoding="utf-8")
    with pytest.raises(BrokerError) as raised:
        _publish(broker, second)
    message = str(raised.value)
    assert "batch 2 of 3" in message and "restored" in message and conflict.path in message
    for item in second.operations[:250]:
        target = workspace / item.path
        assert not target.exists() or target.read_text(encoding="utf-8").startswith("old_name")
    for item in first.operations:
        assert (workspace / item.path).read_text(encoding="utf-8").startswith("new_name")
    record = ledger.record(second, "failed", error=message)
    assert record["state"] == "failed" and record["published_batches"] == [1] and record["remaining_batches"] == 2
    assert describe_progress(record) == "1 of 3 publication batch(es) are on the host; 2 remain."
    assert ledger.load(first.publication_id)["batches"]["2"]["status"] == "failed"

    # Resolve the conflict and retry the same reviewed batch; the remainder then follows.
    if original is None:
        (workspace / conflict.path).unlink()
    else:
        (workspace / conflict.path).write_bytes(original)
    _publish(broker, second)
    service.mark_published(PublicationReceipt.from_manifest(second))
    ledger.record(second, "published")
    third = service.manifest("run", "approval-3", publication_id=first.publication_id, batch_index=3)
    assert (third.batch_index, third.batch_count, len(third.operations)) == (3, 3, TOTAL - 2 * PUBLISH_BATCH_MAX_OPERATIONS)
    assert not third.has_more_batches
    _publish(broker, third)
    service.mark_published(PublicationReceipt.from_manifest(third))
    assert ledger.record(third, "published")["state"] == "completed"

    assert workspace_changes(service.snapshot, service.config.work_root) == []
    assert all((workspace / "src" / f"m{index:04d}.py").read_text(encoding="utf-8") == f"new_name = {index}\n" for index in range(TOTAL))
    done = service.manifest("run", "approval-4", publication_id=first.publication_id, batch_index=4)
    assert done.operations == () and done.batch_count == 4


def test_single_batch_semantics_and_limits_are_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "small"
    workspace.mkdir(parents=True)
    config = ExecutorConfig(workspace, tmp_path / "work", tmp_path / "executor.sock", "t" * 43, workspace_id(workspace))
    service = ExecutorService(config)
    (config.work_root / "a.py").write_text("a\n", encoding="utf-8")
    manifest = service.manifest("run", "approval")
    assert (manifest.batch_index, manifest.batch_count, manifest.has_more_batches) == (1, 1, False)
    assert manifest.publication_id == manifest.manifest_id
    broker = WorkspaceBroker(workspace, tmp_path / "recovery")
    with pytest.raises(BrokerError, match="exactly match"):
        broker.publish(manifest, {"a.py", "b.py"})
    assert _publish(broker, manifest).completed_paths == ("a.py",)


def test_batches_respect_byte_budget_and_reject_oversized_single_files(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "projects" / "bytes"
    workspace.mkdir(parents=True)
    config = ExecutorConfig(workspace, tmp_path / "work", tmp_path / "executor.sock", "t" * 43, workspace_id(workspace))
    service = ExecutorService(config)
    for index in range(4):
        (config.work_root / f"f{index}.txt").write_text("x" * 400, encoding="utf-8")
    monkeypatch.setattr("executor.staging.PUBLISH_BATCH_MAX_BYTES", 1_000)
    batches = publication_batches(workspace_changes(service.snapshot, config.work_root))
    assert [len(batch) for batch in batches] == [2, 2]
    (config.work_root / "huge.txt").write_text("y" * 1_001, encoding="utf-8")
    with pytest.raises(ValueError, match="huge.txt"):
        service.manifest("run", "approval")


def test_manifest_and_receipt_validate_batch_identity() -> None:
    base = {"manifest_id": "m", "run_id": "r", "workspace_id": "w", "source_snapshot_id": "s", "approval_id": "a", "operations": []}
    assert PublishManifest.from_dict(base).batch_count == 1
    for bad in ({"batch_index": 3, "batch_count": 2}, {"batch_index": 0, "batch_count": 1}, {"batch_index": True, "batch_count": 1}, {"batch_count": 5_000}):
        with pytest.raises(ValueError):
            PublishManifest.from_dict({**base, **bad})
    manifest = PublishManifest.from_dict({**base, "publication_id": "p", "batch_index": 2, "batch_count": 3})
    receipt = PublicationReceipt.from_manifest(manifest)
    assert PublicationReceipt.from_dict(json.loads(json.dumps(receipt.to_dict()))) == receipt
    with pytest.raises(ValueError, match="Unknown"):
        PublicationReceipt.from_dict({**receipt.to_dict(), "content": "x"})


class BatchGateway:
    def __init__(self, following: PublishManifest | None) -> None:
        self.following = following
        self.receipts: list[object] = []
        self.requested: list[PublishManifest] = []

    def mark_staging_published(self, manifest) -> dict:
        self.receipts.append(manifest)
        return {"snapshot_id": "next"}

    def next_publication_batch(self, manifest: PublishManifest) -> PublishManifest:
        self.requested.append(manifest)
        assert self.following is not None
        return self.following


def test_publication_worker_records_progress_and_fetches_the_next_batch(tmp_path: Path) -> None:
    workspace, service = _setup(tmp_path)
    first = service.manifest("run", "approval-1")
    projected = PublishManifest(first.manifest_id, "run", first.workspace_id, first.source_snapshot_id, "approval-2", (), first.publication_id, 2, 3)
    gateway = BatchGateway(projected)
    completed: list[dict] = []
    worker = PublicationWorker(first, workspace, tmp_path / "recovery", reseed_client=gateway)
    worker.complete.connect(completed.append)
    worker.run()
    # An empty follow-up means nothing remains, so no approval is requested for it.
    assert completed[0]["reseeded"] is True and "next_manifest" not in completed[0]
    assert completed[0]["batch_index"] == 1 and completed[0]["batch_count"] == 3
    assert gateway.requested == [first]
    record = PublicationLedger(tmp_path / "recovery").load(first.publication_id)
    assert record["published_batches"] == [1] and record["state"] == "in_progress"

    service.mark_published(first)
    second = service.manifest("run", "approval-2", publication_id=first.publication_id, batch_index=2)
    gateway = BatchGateway(second)
    completed.clear()
    worker = PublicationWorker(first, workspace, tmp_path / "recovery-2", reseed_client=gateway)
    worker.complete.connect(completed.append)
    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.run()
    # The host already holds batch 1, so re-publishing it fails the hash check and is reported.
    assert completed == [] and "batch 1 of 3" in failed[0] and "Retry to resume with batch 1" in failed[0]

    worker = PublicationWorker(second, workspace, tmp_path / "recovery", reseed_client=BatchGateway(service.manifest("run", "approval-3", publication_id=first.publication_id, batch_index=3)))
    worker.complete.connect(completed.append)
    worker.run()
    assert PublishManifest.from_dict(completed[0]["next_manifest"]).batch_index == 3
    assert worker.continuation_client is not None


def test_gateway_serves_next_batches_and_content_free_receipts(tmp_path: Path) -> None:
    calls: list[tuple] = []

    class FakeExecutor:
        async def manifest(self, run_id, approval_id, *, publication_id=None, batch_index=1):
            calls.append(("manifest", run_id, publication_id, batch_index))
            return PublishManifest("m2", run_id, "workspace", "snap", approval_id, (), publication_id, batch_index, batch_index)

        async def mark_staging_published(self, receipt):
            calls.append(("mark", type(receipt).__name__, receipt.batch_index))
            return {"snapshot_id": "s2", "file_count": 0}

    async def scenario() -> None:
        socket = tmp_path / "executor.sock"; socket.touch()
        cfg = GatewayConfig("t" * 43, tmp_path / "gateway.sqlite3", executor_socket=socket, executor_token="e" * 43, workspace_id="workspace")
        service = GatewayService(cfg)
        service.executor = FakeExecutor()
        app = create_app(cfg, service)
        headers = {"Authorization": "Bearer " + "t" * 43, "Content-Type": "application/json"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post("/v1/workspaces/workspace/staging/manifest", headers=headers, json={"run_id": "run", "publication_id": "pub", "batch_index": 2})
            assert response.status_code == 200, response.text
            assert PublishManifest.from_dict(response.json()).batch_index == 2
            rejected = await client.post("/v1/workspaces/workspace/staging/manifest", headers=headers, json={"run_id": "run", "publication_id": "pub", "batch_index": 1})
            assert rejected.status_code == 422
            other = await client.post("/v1/workspaces/other/staging/manifest", headers=headers, json={"run_id": "run", "publication_id": "pub", "batch_index": 2})
            assert other.status_code == 409
            receipt = {"manifest_id": "m", "run_id": "run", "workspace_id": "workspace", "source_snapshot_id": "s", "publication_id": "pub", "batch_index": 2, "batch_count": 3, "operations": [{"path": "a.py", "operation": "create", "staged_sha256": "0" * 64, "staged_mode": 420}]}
            marked = await client.post("/v1/workspaces/workspace/staging/mark-published", headers=headers, json=receipt)
            assert marked.status_code == 200, marked.text
        await service.close()

    asyncio.run(scenario())
    assert calls == [("manifest", "run", "pub", 2), ("mark", "PublicationReceipt", 2)]
