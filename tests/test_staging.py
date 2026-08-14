from __future__ import annotations

from pathlib import Path

import pytest

from executor.app import ExecutorService
from executor.config import ExecutorConfig
from executor.staging import seed_staging, workspace_changes
from src.workspace_broker import WorkspaceBroker, workspace_id


def make_config(source: Path, work: Path, **limits: int) -> ExecutorConfig:
    return ExecutorConfig(
        source_root=source,
        work_root=work,
        socket_path=work.parent / "executor.sock",
        token="t" * 43,
        workspace_id="workspace",
        **limits,
    )


def test_default_staging_limits_support_large_projects(tmp_path: Path) -> None:
    config = make_config(tmp_path / "source", tmp_path / "work")

    assert config.max_files == 300_000
    assert config.max_total_bytes == 2_000_000_000


def test_seed_staging_skips_dependency_metadata_and_cache_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "src").mkdir(parents=True)
    (source / ".venv" / "lib").mkdir(parents=True)
    (source / "node_modules" / "package").mkdir(parents=True)
    (source / ".git" / "objects").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    (source / ".venv" / "lib" / "ignored.py").write_text("ignored", encoding="utf-8")
    (source / "node_modules" / "package" / "ignored.js").write_text("ignored", encoding="utf-8")
    (source / ".git" / "objects" / "ignored").write_text("ignored", encoding="utf-8")
    (source / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")

    snapshot = seed_staging(make_config(source, work))

    assert set(snapshot.hashes) == {"src/main.py"}
    assert snapshot.total_bytes == len("print('ok')")
    assert (work / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')"
    assert not (work / ".venv").exists()
    assert not (work / "node_modules").exists()
    assert not (work / ".git").exists()
    assert not (work / "__pycache__").exists()


def test_seed_staging_keeps_limits_for_materialized_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "a.txt").parent.mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "b.txt").write_text("b", encoding="utf-8")

    with pytest.raises(RuntimeError, match="snapshot limits"):
        seed_staging(make_config(source, tmp_path / "work", max_files=1))


def test_generated_runtime_caches_are_not_publication_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    snapshot = seed_staging(make_config(source, work))

    (work / "blackjack.py").write_text("print('ok')", encoding="utf-8")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "blackjack.cpython-312.pyc").write_bytes(b"not text")
    (work / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (work / ".pytest_cache" / "v" / "cache" / "nodeids").write_bytes(b"cache")

    changes = workspace_changes(snapshot, work)

    assert [(item.path, item.operation) for item in changes] == [("blackjack.py", "create")]


def test_publication_manifest_contains_text_file_without_generated_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    service = ExecutorService(make_config(source, work))
    (work / "blackjack.py").write_text("print('ok')", encoding="utf-8")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "blackjack.cpython-312.pyc").write_bytes(b"not text")

    manifest = service.manifest("run", "approval")

    assert [(item.path, item.operation, item.content) for item in manifest.operations] == [
        ("blackjack.py", "create", "print('ok')")
    ]


def test_staged_python_file_publishes_to_the_selected_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "new-project"
    workspace.mkdir(parents=True)
    config = ExecutorConfig(
        workspace,
        tmp_path / "work",
        tmp_path / "executor.sock",
        "t" * 43,
        workspace_id(workspace),
    )
    service = ExecutorService(config)
    (config.work_root / "blackjack.py").write_text("print('ok')", encoding="utf-8")

    manifest = service.manifest("run", "approval")
    result = WorkspaceBroker(workspace, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )

    assert result.completed_paths == ("blackjack.py",)
    assert (workspace / "blackjack.py").read_text(encoding="utf-8") == "print('ok')"


def test_publication_chmod_does_not_require_follow_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "projects" / "new-project"
    workspace.mkdir(parents=True)
    config = ExecutorConfig(
        workspace,
        tmp_path / "work",
        tmp_path / "executor.sock",
        "t" * 43,
        workspace_id(workspace),
    )
    service = ExecutorService(config)
    (config.work_root / "blackjack.py").write_text("print('ok')", encoding="utf-8")
    manifest = service.manifest("run", "approval")

    import src.workspace_broker as broker_module

    real_chmod = broker_module.os.chmod

    def chmod_without_follow_symlinks(path: Path, mode: int, **kwargs: object) -> None:
        if kwargs:
            raise TypeError("follow_symlinks unavailable on this platform")
        real_chmod(path, mode)

    monkeypatch.setattr(broker_module.os, "chmod", chmod_without_follow_symlinks)

    result = WorkspaceBroker(workspace, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )

    assert result.completed_paths == ("blackjack.py",)
