"""Persistent staging (plan 2.4/2.5) and unwedged staging after a symlink (plan 0.7)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.state import WorkspaceIdentityMismatch
from shared.tools import PublicationReceipt, ToolRequest
from tests.helpers import executor_config


def run(service: ExecutorService, tool: str, arguments: dict, request_id: str = "req"):
    return asyncio.run(
        service.execute(ToolRequest(request_id, "run", tool, "agent", arguments))
    )


def make(tmp_path: Path, **files: str) -> ExecutorService:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    return ExecutorService(executor_config(source))


def publish(service: ExecutorService) -> None:
    manifest = service.manifest("run", "approval")
    for operation in manifest.operations:
        target = service.config.source_root / operation.path
        if operation.operation == "delete":
            target.unlink()
        else:
            target.write_text(operation.content or "", encoding="utf-8")
    service.mark_published(PublicationReceipt.from_manifest(manifest))


# 0.7: a symlink in staging is an unpublishable entry, not a fatal error


@pytest.mark.posix
def test_a_virtualenv_link_does_not_wedge_status_write_bash_or_publication(
    tmp_path: Path,
) -> None:
    service = make(tmp_path, **{"app.py": "print('a')\n"})
    created = run(
        service,
        "bash",
        {"command": "mkdir -p env/bin && ln -s /usr/local/bin/python3 env/bin/python"},
    )
    assert created.ok, created.output
    assert created.data["workspace_status"]["unpublishable_links"] == ["env/bin/python"]

    status = run(service, "status", {})
    assert status.ok and status.data["unpublishable_links"] == ["env/bin/python"]
    assert "L  env/bin/python" in status.output
    assert status.data["total_known"] == 0

    assert run(service, "write", {"path": "new.py", "content": "x = 1\n"}).ok
    assert run(service, "bash", {"command": "echo still works"}).ok
    # The file tools still refuse to traverse the link.
    refused = run(service, "read", {"path": "env/bin/python"})
    assert not refused.ok and refused.error_code in {"path.unsafe", "path.invalid_type"}

    manifest = service.manifest("run", "approval")
    assert [operation.path for operation in manifest.operations] == ["new.py"]


@pytest.mark.posix
def test_rollback_removes_new_links_and_restores_removed_ones(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    work = service.config.work_root
    failed = run(service, "bash", {"command": "ln -s /etc escape && exit 3"})
    assert failed.ok and failed.data["rolled_back"]
    assert not os.path.lexists(work / "escape")

    assert run(service, "bash", {"command": "ln -s a.txt kept"}).ok
    assert run(service, "bash", {"command": "rm kept; exit 1"}).data["rolled_back"]
    assert os.readlink(work / "kept") == "a.txt"


@pytest.mark.posix
def test_rollback_restores_files_through_a_directory_replaced_by_a_link(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("original\n", encoding="utf-8")
    service = ExecutorService(executor_config(source))
    work = service.config.work_root
    result = run(service, "bash", {"command": "rm -rf pkg && ln -s /etc pkg && exit 2"})
    assert result.ok and result.data["rolled_back"]
    assert not (work / "pkg").is_symlink()
    assert (work / "pkg" / "mod.py").read_text(encoding="utf-8") == "original\n"


# 2.4: staging survives a restart; host drift is absorbed; conflicts block only their file


def test_staging_and_baseline_survive_an_executor_restart(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "b.txt", "content": "b"}).ok
    snapshot_id = service.snapshot.snapshot_id

    restarted = ExecutorService(service.config)
    assert restarted.resumed
    assert restarted.snapshot.snapshot_id == snapshot_id
    assert (restarted.config.work_root / "b.txt").read_text(encoding="utf-8") == "b"
    assert [item.path for item in restarted.manifest("run", "approval").operations] == ["b.txt"]


def test_a_removed_work_volume_with_surviving_state_reseeds(tmp_path: Path) -> None:
    import shutil

    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "b.txt", "content": "b"}).ok
    shutil.rmtree(service.config.work_root)

    restarted = ExecutorService(service.config)
    assert not restarted.resumed
    assert (restarted.config.work_root / "a.txt").read_text(encoding="utf-8") == "a"
    assert restarted.manifest("run", "approval").operations == ()


def test_staging_without_a_baseline_is_cleared_and_reseeded(tmp_path: Path) -> None:
    import shutil

    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "orphan.txt", "content": "x"}).ok
    shutil.rmtree(service.config.state_directory)

    restarted = ExecutorService(service.config)
    assert not restarted.resumed
    assert not (restarted.config.work_root / "orphan.txt").exists()
    assert restarted.manifest("run", "approval").operations == ()


def test_checkpoints_live_on_the_state_volume_not_in_staging(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "a.txt", "content": "changed"}).ok
    assert not (service.config.work_root / ".local-chat-checkpoints").exists()
    assert any((service.config.state_directory / "checkpoints").iterdir())


def test_host_drift_without_conflicts_is_absorbed(tmp_path: Path) -> None:
    service = make(tmp_path, **{"host.txt": "v1", "staged.txt": "s1", "gone.txt": "x"})
    assert run(service, "write", {"path": "staged.txt", "content": "s2"}).ok
    source = service.config.source_root
    (source / "host.txt").write_text("v2", encoding="utf-8")
    (source / "added.txt").write_text("new", encoding="utf-8")
    (source / "gone.txt").unlink()

    report = service.rebase()
    assert sorted(report["absorbed"]) == ["added.txt", "gone.txt", "host.txt"]
    assert report["conflicts"] == []
    work = service.config.work_root
    assert (work / "host.txt").read_text(encoding="utf-8") == "v2"
    assert (work / "added.txt").read_text(encoding="utf-8") == "new"
    assert not (work / "gone.txt").exists()
    # The agent's own change is still pending and is the only thing to publish.
    assert [item.path for item in service.manifest("run", "approval").operations] == [
        "staged.txt"
    ]


def test_restart_absorbs_drift_and_rehashes_only_changed_host_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make(tmp_path, **{"same.txt": "same", "edited.txt": "v1"})
    (service.config.source_root / "edited.txt").write_text("v2-longer", encoding="utf-8")
    hashed: list[str] = []
    import executor.drift as drift

    original = drift.sha256_file
    monkeypatch.setattr(
        drift, "sha256_file", lambda path: hashed.append(path.name) or original(path)
    )
    restarted = ExecutorService(service.config)
    assert restarted.last_rebase["absorbed"] == ["edited.txt"]
    assert hashed == ["edited.txt"]


def test_a_conflict_blocks_publication_of_that_file_only(tmp_path: Path) -> None:
    service = make(tmp_path, **{"both.txt": "base", "mine.txt": "m1"})
    assert run(service, "write", {"path": "both.txt", "content": "agent"}).ok
    assert run(service, "write", {"path": "mine.txt", "content": "m2"}).ok
    (service.config.source_root / "both.txt").write_text("host", encoding="utf-8")

    report = service.rebase()
    assert report["conflicts"] == ["both.txt"]
    assert (service.config.work_root / "both.txt").read_text(encoding="utf-8") == "agent"
    status = run(service, "status", {})
    assert [item["path"] for item in status.data["conflicts"]] == ["both.txt"]
    assert "C  both.txt" in status.output
    assert [item.path for item in service.manifest("run", "approval").operations] == [
        "mine.txt"
    ]
    # Conflicts persist across a restart.
    assert set(ExecutorService(service.config).conflicts) == {"both.txt"}


def test_conflicts_resolve_by_keeping_staged_or_taking_host(tmp_path: Path) -> None:
    service = make(tmp_path, **{"keep.txt": "base", "take.txt": "base"})
    for name in ("keep.txt", "take.txt"):
        assert run(service, "write", {"path": name, "content": "agent"}).ok
        (service.config.source_root / name).write_text("host", encoding="utf-8")
    assert sorted(service.rebase()["conflicts"]) == ["keep.txt", "take.txt"]

    result = service.rebase(keep_staged=["keep.txt"], take_host=["take.txt"])
    assert sorted(result["resolved"]) == ["keep.txt", "take.txt"]
    assert result["open_conflicts"] == []
    work = service.config.work_root
    assert (work / "take.txt").read_text(encoding="utf-8") == "host"
    manifest = service.manifest("run", "approval")
    [operation] = manifest.operations
    assert operation.path == "keep.txt" and operation.content == "agent"
    # Publishing now overwrites the host version the user chose to replace.
    assert operation.base_sha256 == service.snapshot.hashes["keep.txt"]
    publish(service)
    assert (service.config.source_root / "keep.txt").read_text(encoding="utf-8") == "agent"


def test_resolving_a_path_that_is_not_a_conflict_is_refused(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    with pytest.raises(ValueError, match="current conflicts"):
        service.rebase(keep_staged=["a.txt"])


def test_a_volume_belonging_to_another_workspace_is_refused(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    other = executor_config(
        service.config.source_root, workspace_id="another-workspace-identity"
    )
    with pytest.raises(WorkspaceIdentityMismatch):
        ExecutorService(other)


def test_discard_reseeds_from_the_host_and_clears_checkpoints(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "a.txt", "content": "changed"}).ok
    assert run(service, "bash", {"command": "echo hi"}).ok
    service.discard()
    work = service.config.work_root
    assert (work / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (work / ".euesto-output").exists()
    assert not any((service.config.state_directory / "checkpoints").iterdir())
    assert service.manifest("run", "approval").operations == ()


def test_publication_survives_restart_without_republishing(tmp_path: Path) -> None:
    service = make(tmp_path, **{"a.txt": "a"})
    assert run(service, "write", {"path": "a.txt", "content": "published"}).ok
    publish(service)
    restarted = ExecutorService(service.config)
    assert restarted.last_rebase["absorbed"] == []
    assert restarted.manifest("run", "approval").operations == ()
