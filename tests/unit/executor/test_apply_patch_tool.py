from __future__ import annotations

import asyncio
import hashlib
import importlib
from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.checkpoints import inspect_checkpoint
from executor.config import ExecutorConfig
from executor.errors import ExecutorToolError
from executor.tools import apply_patch as apply_patch_export
from executor.tools import write
from executor.tools.apply_patch import apply_patch
from shared.permissions import PermissionDecision, PermissionRule, resolve_permission, rule_scope
from shared.tools import MUTATION_TOOLS, ToolRequest

LIMITS = {"max_operations": 20, "max_patch_bytes": 100_000, "max_write_bytes": 50_000, "max_edit_target_bytes": 50_000, "max_edit_result_bytes": 50_000}


def _root(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file() and ".local-chat-" not in path.as_posix()}


def test_apply_patch_applies_multi_file_change_in_order_with_one_checkpoint(tmp_path: Path) -> None:
    root = _root(tmp_path, {"src/app.py": "import os\n\ndef main():\n    return 1\n", "src/util.py": "VALUE = 1\n", "old.txt": "remove me\n"})
    output, data = apply_patch(root, {"operations": [
        {"operation": "edit", "path": "src/app.py", "old_str": "return 1", "new_str": "return helper()"},
        {"operation": "edit", "path": "src/app.py", "old_str": "import os\n", "new_str": "import os\nfrom src.util import helper\n"},
        {"operation": "write", "path": "src/util.py", "content": "VALUE = 1\n\ndef helper():\n    return VALUE\n"},
        {"operation": "write", "path": "docs/notes.md", "content": "# Notes\n", "create_parents": True},
        {"operation": "delete", "path": "old.txt"},
    ]}, **LIMITS)
    assert (root / "src/app.py").read_text(encoding="utf-8") == "import os\nfrom src.util import helper\n\ndef main():\n    return helper()\n"
    assert "def helper" in (root / "src/util.py").read_text(encoding="utf-8")
    assert (root / "docs/notes.md").exists() and not (root / "old.txt").exists()
    assert data["paths"] == ["src/app.py", "src/util.py", "docs/notes.md", "old.txt"]
    assert data["operation_counts"] == {"write": 2, "edit": 2, "delete": 1}
    assert [item["operation"] for item in data["operations"]] == ["edit", "edit", "write", "write", "delete"]
    assert data["operations"][3]["created"] is True
    assert data["atomicity"] == "single-checkpoint-all-or-nothing"
    assert "5 operations across 4 files" in output
    # Every changed path's pre-patch state is inside the single checkpoint.
    checkpoint = inspect_checkpoint(root, data["checkpoint_id"], max_results=50)
    assert {"src/app.py", "src/util.py", "old.txt"} <= set(checkpoint["files"])
    assert "docs/notes.md" not in checkpoint["files"]


def test_apply_patch_failure_rolls_back_every_earlier_operation(tmp_path: Path) -> None:
    files = {"a.txt": "alpha\n", "b.txt": "beta\n", "c.txt": "gamma\n"}
    root = _root(tmp_path, files)
    before = _snapshot(root)
    with pytest.raises(ExecutorToolError) as raised:
        apply_patch(root, {"operations": [
            {"operation": "write", "path": "a.txt", "content": "ALPHA\n"},
            {"operation": "delete", "path": "b.txt"},
            {"operation": "write", "path": "new/deep.txt", "content": "x", "create_parents": True},
            {"operation": "edit", "path": "c.txt", "old_str": "missing", "new_str": "x"},
        ]}, **LIMITS)
    error = raised.value
    assert error.code == "edit.no_match"
    assert error.details["failed_operation"] == 3 and error.details["path"] == "c.txt"
    assert error.details["applied_before_failure"] == 3 and error.details["rolled_back"] is True
    assert error.details["cause"]["failure"] == "zero_matches"
    assert "No operation was applied" in error.message
    assert _snapshot(root) == before


def test_apply_patch_rolls_back_on_interruption(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path, {"a.txt": "one\n", "b.txt": "two\n"})
    before = _snapshot(root)
    module = importlib.import_module("executor.tools.apply_patch")

    real = module._delete

    def interrupted(root_path, arguments):
        real(root_path, arguments)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_delete", interrupted)
    with pytest.raises(KeyboardInterrupt):
        apply_patch(root, {"operations": [{"operation": "write", "path": "a.txt", "content": "ONE\n"}, {"operation": "delete", "path": "b.txt"}]}, **LIMITS)
    assert _snapshot(root) == before


def test_apply_patch_validates_structure_paths_and_limits_before_touching_files(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.txt": "a\n"})
    before = _snapshot(root)
    cases = [
        ({"operations": []}, "apply_patch.malformed"),
        ({"operations": [{"operation": "rename", "path": "a.txt"}]}, "apply_patch.malformed"),
        ({"operations": [{"operation": "delete", "path": "a.txt", "content": "x"}]}, "apply_patch.malformed"),
        ({"operations": [{"operation": "write", "path": "../escape.txt", "content": "x"}]}, None),
        ({"operations": [{"operation": "write", "path": ".env", "content": "x"}]}, None),
        ({"operations": [{"operation": "write", "path": f"f{index}.txt", "content": "x"} for index in range(21)]}, "limit.exceeded"),
        ({"operations": [{"operation": "write", "path": "big.txt", "content": "x" * 100_001}]}, "limit.exceeded"),
    ]
    for arguments, code in cases:
        with pytest.raises(ValueError) as raised:
            apply_patch(root, arguments, **LIMITS)
        if code:
            assert getattr(raised.value, "code", None) == code, arguments
    with pytest.raises(ValueError, match="Unknown apply_patch arguments"):
        apply_patch(root, {"operations": [], "extra": 1}, **LIMITS)
    assert _snapshot(root) == before
    assert not (root / ".local-chat-checkpoints").exists()


def test_apply_patch_hash_checks_apply_per_operation(tmp_path: Path) -> None:
    root = _root(tmp_path, {"a.txt": "a\n", "b.txt": "b\n"})
    digest = hashlib.sha256(b"a\n").hexdigest()
    _, data = apply_patch(root, {"operations": [{"operation": "write", "path": "a.txt", "content": "A\n", "expected_sha256": digest}]}, **LIMITS)
    assert data["operations"][0]["old_sha256"] == digest
    with pytest.raises(ExecutorToolError) as raised:
        apply_patch(root, {"operations": [{"operation": "write", "path": "a.txt", "content": "Z\n"}, {"operation": "delete", "path": "b.txt", "expected_sha256": "0" * 64}]}, **LIMITS)
    assert raised.value.code == "staging.conflict"
    assert raised.value.details["cause"]["actual_sha256"] == hashlib.sha256(b"b\n").hexdigest()
    assert (root / "a.txt").read_text(encoding="utf-8") == "A\n" and (root / "b.txt").exists()


def test_apply_patch_is_a_public_mutation_with_consistent_permissions(tmp_path: Path) -> None:
    assert "apply_patch" in MUTATION_TOOLS and callable(apply_patch_export)
    request = ToolRequest("p", "run", "apply_patch", "agent", {"operations": [{"operation": "write", "path": "src/a/x.py", "content": ""}, {"operation": "edit", "path": "src/b.py", "old_str": "a", "new_str": "b"}]})
    assert resolve_permission(request, "ws") == PermissionDecision.ASK
    with pytest.raises(ValueError, match="Plan mode"):
        ToolRequest("p", "run", "apply_patch", "plan", {"operations": []})
    assert rule_scope(request) == "src"
    scoped = PermissionRule("r", PermissionDecision.ALLOW_RULE, "ws", "agent", "apply_patch", "src")
    assert resolve_permission(request, "ws", (scoped,)) == PermissionDecision.ALLOW_RULE
    outside = ToolRequest("p2", "run", "apply_patch", "agent", {"operations": [{"operation": "write", "path": "src/a.py", "content": ""}, {"operation": "write", "path": "tests/t.py", "content": ""}]})
    assert rule_scope(outside) is None
    assert resolve_permission(outside, "ws", (scoped,)) == PermissionDecision.ASK


def test_executor_dispatches_apply_patch_with_status_and_error_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    config = ExecutorConfig(source_root=source, work_root=tmp_path / "work", socket_path=tmp_path / "s.sock", token="t" * 43, workspace_id="ws")
    service = ExecutorService(config)
    ok = asyncio.run(service.execute(ToolRequest("p1", "run", "apply_patch", "agent", {"operations": [{"operation": "edit", "path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}, {"operation": "write", "path": "b.py", "content": "y = 1\n"}]})))
    assert ok.ok, ok.to_dict()
    assert ok.data["workspace_status"]["created"] == ["b.py"] and ok.data["workspace_status"]["modified"] == ["a.py"]
    failed = asyncio.run(service.execute(ToolRequest("p2", "run", "apply_patch", "agent", {"operations": [{"operation": "write", "path": "c.py", "content": ""}, {"operation": "edit", "path": "a.py", "old_str": "x = 1", "new_str": "z"}]})))
    assert not failed.ok and failed.error_code == "edit.no_match"
    assert failed.data["failed_operation"] == 1 and failed.data["rolled_back"] is True
    assert not (config.work_root / "c.py").exists()


def test_confirmed_whole_file_write_shrink_warns_but_unconfirmed_still_blocks(tmp_path: Path) -> None:
    root = _root(tmp_path, {"big.py": "".join(f"line {index} = {index}\n" for index in range(100))})
    with pytest.raises(ExecutorToolError) as raised:
        write(root, {"path": "big.py", "content": "stub = 1\n"}, max_bytes=100_000)
    assert raised.value.code == "staging.shrink_warning"
    assert (root / "big.py").read_text(encoding="utf-8").startswith("line 0")
    digest = hashlib.sha256((root / "big.py").read_bytes()).hexdigest()
    output, data = write(root, {"path": "big.py", "content": "stub = 1\n", "expected_sha256": digest}, max_bytes=100_000)
    assert data["shrink_warning"] is True and data["shrink_details"]["old_lines"] == 100
    assert "shrink" in output
    assert (root / "big.py").read_text(encoding="utf-8") == "stub = 1\n"
