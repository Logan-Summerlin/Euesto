"""Staged rewrites keep file permission modes, so status and publication see no mode change."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.atomic_io import replacement_mode
from executor.config import ExecutorConfig
from shared.tools import ToolRequest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_staged_rewrites_keep_file_modes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name, mode in (("plain.txt", 0o644), ("run.sh", 0o755), ("patched.txt", 0o640), ("both.txt", 0o644)):
        (source / name).write_text("before\n", encoding="utf-8")
        (source / name).chmod(mode)
    work = tmp_path / "work"
    service = ExecutorService(ExecutorConfig(source_root=source, work_root=work, socket_path=tmp_path / "executor.sock", token="x" * 32, workspace_id="modes"))
    new_file_mode = replacement_mode(work / "does-not-exist")
    for request in (
        ToolRequest("w", "run", "write", "agent", {"path": "plain.txt", "content": "after\n"}),
        ToolRequest("e", "run", "edit", "agent", {"path": "run.sh", "old_str": "before", "new_str": "after"}),
        ToolRequest("p", "run", "apply_patch", "agent", {"operations": [
            {"operation": "edit", "path": "patched.txt", "old_str": "before", "new_str": "after"},
            {"operation": "write", "path": "both.txt", "content": "middle\n"},
            {"operation": "edit", "path": "both.txt", "old_str": "middle", "new_str": "after"},
            {"operation": "write", "path": "created.txt", "content": "new\n"},
        ]}),
    ):
        result = asyncio.run(service.execute(request))
        assert result.ok, result.to_dict()
        assert result.data["workspace_status"]["permission_changes"] == []

    assert {name: _mode(work / name) for name in ("plain.txt", "run.sh", "patched.txt", "both.txt")} == {"plain.txt": 0o644, "run.sh": 0o755, "patched.txt": 0o640, "both.txt": 0o644}
    assert _mode(work / "created.txt") == new_file_mode == 0o666 & ~_umask()
    status = asyncio.run(service.execute(ToolRequest("s", "run", "status", "agent", {})))
    assert status.data["counts"] == {"created": 1, "modified": 4, "deleted": 0, "permission_changes": 0}
    # Publication carries each file's own mode, not the temporary file's 0600.
    manifest = service.manifest("run", "approval")
    assert all(operation.staged_mode == operation.base_mode for operation in manifest.operations if operation.operation == "update")
    assert {operation.path: operation.staged_mode for operation in manifest.operations if operation.operation == "create"} == {"created.txt": new_file_mode}


def _umask() -> int:
    current = os.umask(0o022)
    os.umask(current)
    return current
