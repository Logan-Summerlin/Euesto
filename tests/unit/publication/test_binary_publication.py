from __future__ import annotations

import asyncio
import base64
import hashlib
import stat
import struct
import zlib
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.staging import workspace_changes
from shared.tools import PublishManifest, PublishOperation, ToolRequest
from src.workspace_broker import BrokerError, WorkspaceBroker, workspace_id


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


def _setup(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, ExecutorService]:
    workspace = tmp_path / "projects" / "app"
    workspace.mkdir(parents=True)
    for relative, content in files.items():
        (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
        (workspace / relative).write_bytes(content)
    config = ExecutorConfig(workspace, tmp_path / "work", tmp_path / "executor.sock", "t" * 43, workspace_id(workspace))
    return workspace, ExecutorService(config)


def test_bash_written_png_publishes_byte_identical_with_mode(tmp_path: Path) -> None:
    workspace, service = _setup(tmp_path, {"README.md": b"# app\n", "assets/old.bin": b"\x00\x01\x02"})
    png = _png()
    encoded = base64.b64encode(png).decode()
    command = f"python3 -c \"import base64,pathlib; pathlib.Path('assets/icon.png').write_bytes(base64.b64decode('{encoded}'))\" && chmod 640 assets/icon.png && printf '\\377\\376binary' > assets/old.bin"
    result = asyncio.run(service.execute(ToolRequest("b1", "run", "bash", "agent", {"command": command})))
    assert result.ok and result.data["exit_code"] == 0, result.to_dict()

    changes = {item.path: item for item in workspace_changes(service.snapshot, service.config.work_root)}
    assert changes["assets/icon.png"].operation == "create"
    assert changes["assets/icon.png"].staged_sha256 == hashlib.sha256(png).hexdigest()
    assert changes["assets/old.bin"].operation == "update"

    manifest = service.manifest("run", "approval")
    operations = {item.path: item for item in manifest.operations}
    assert operations["assets/icon.png"].binary and operations["assets/icon.png"].content is None
    assert base64.b64decode(operations["assets/icon.png"].content_base64) == png
    assert operations["assets/old.bin"].binary
    # The manifest survives the wire format unchanged.
    manifest = PublishManifest.from_dict(manifest.to_dict())

    WorkspaceBroker(workspace, tmp_path / "recovery").publish(manifest, {item.path for item in manifest.operations})
    assert (workspace / "assets/icon.png").read_bytes() == png
    assert stat.S_IMODE((workspace / "assets/icon.png").stat().st_mode) == 0o640
    assert (workspace / "assets/old.bin").read_bytes() == b"\xff\xfebinary"
    service.mark_published(manifest)
    assert workspace_changes(service.snapshot, service.config.work_root) == []


def test_text_files_keep_the_text_path_next_to_binary_files(tmp_path: Path) -> None:
    workspace, service = _setup(tmp_path, {"a.txt": b"one\r\n"})
    work = service.config.work_root
    (work / "a.txt").write_bytes(b"two\r\n")
    (work / "b.dat").write_bytes(b"\x80\x81")
    manifest = service.manifest("run", "approval")
    text, binary = manifest.operations
    assert text.path == "a.txt" and text.content == "two\r\n" and text.content_base64 is None
    assert binary.path == "b.dat" and binary.content is None and binary.payload() == b"\x80\x81"
    WorkspaceBroker(workspace, tmp_path / "recovery").publish(manifest, {"a.txt", "b.dat"})
    assert (workspace / "a.txt").read_bytes() == b"two\r\n" and (workspace / "b.dat").read_bytes() == b"\x80\x81"


def test_binary_delete_and_undo_round_trip(tmp_path: Path) -> None:
    workspace, service = _setup(tmp_path, {"img.bin": b"\x00\xff" * 10})
    (service.config.work_root / "img.bin").unlink()
    manifest = service.manifest("run", "approval")
    assert [(item.path, item.operation) for item in manifest.operations] == [("img.bin", "delete")]
    broker = WorkspaceBroker(workspace, tmp_path / "recovery")
    published = broker.publish(manifest, {"img.bin"})
    assert not (workspace / "img.bin").exists()
    broker.undo(published.checkpoint_id)
    assert (workspace / "img.bin").read_bytes() == b"\x00\xff" * 10


def test_publish_operation_validates_binary_payloads() -> None:
    payload = b"\x00\x01binary\xff"
    digest = hashlib.sha256(payload).hexdigest()
    operation = PublishOperation("a.bin", "create", None, digest, None, None, 0o644, base64.b64encode(payload).decode())
    assert operation.binary and operation.payload() == payload and operation.payload_bytes() == len(payload)
    with pytest.raises(ValueError, match="exactly one"):
        PublishOperation("a.bin", "create", None, digest, "text", None, None, base64.b64encode(payload).decode())
    with pytest.raises(ValueError, match="exactly one"):
        PublishOperation("a.bin", "create", None, digest)
    with pytest.raises(ValueError, match="hash mismatch"):
        PublishOperation("a.bin", "create", None, "0" * 64, None, None, None, base64.b64encode(payload).decode())
    with pytest.raises(ValueError, match="Invalid content_base64"):
        PublishOperation("a.bin", "create", None, digest, None, None, None, "not base64!")
    with pytest.raises(ValueError, match="Deleted files"):
        PublishOperation("a.bin", "delete", digest, None, None, None, None, "AA==")


def test_broker_counts_decoded_binary_bytes_against_the_batch_limit(tmp_path: Path, monkeypatch) -> None:
    workspace, service = _setup(tmp_path, {})
    (service.config.work_root / "blob.bin").write_bytes(b"\xff" * 3_000)
    manifest = service.manifest("run", "approval")
    monkeypatch.setattr("src.workspace_broker.MAX_PUBLISH_BYTES", 2_999)
    with pytest.raises(BrokerError, match="byte limit"):
        WorkspaceBroker(workspace, tmp_path / "recovery").publish(manifest, {"blob.bin"})
    monkeypatch.setattr("src.workspace_broker.MAX_PUBLISH_BYTES", 3_000)
    WorkspaceBroker(workspace, tmp_path / "recovery").publish(manifest, {"blob.bin"})
    assert (workspace / "blob.bin").read_bytes() == b"\xff" * 3_000
