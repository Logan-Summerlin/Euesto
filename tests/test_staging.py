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

    assert config.max_staged_files == 300_000
    assert config.max_staging_bytes == 2_500_000_000
    assert config.max_checkpoint_bytes == 2_500_000_000
    assert config.required_capacity_bytes < config.work_capacity_bytes


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

    with pytest.raises(RuntimeError, match="staging limits"):
        seed_staging(make_config(source, tmp_path / "work", max_staged_files=1))


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


def test_publication_preserves_crlf_bytes_and_does_not_report_post_write_mismatch(tmp_path: Path) -> None:
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
    staged = b"first line\r\nsecond line\r\n"
    (config.work_root / "example.py").write_bytes(staged)

    manifest = service.manifest("run", "approval")
    result = WorkspaceBroker(workspace, tmp_path / "recovery").publish(
        manifest, {item.path for item in manifest.operations}
    )

    assert result.completed_paths == ("example.py",)
    assert (workspace / "example.py").read_bytes() == staged


def test_undo_preserves_recovery_file_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "new-project"
    workspace.mkdir(parents=True)
    original = b"old\r\ncontent\r\n"
    target = workspace / "example.py"
    target.write_bytes(original)
    config = ExecutorConfig(
        workspace,
        tmp_path / "work",
        tmp_path / "executor.sock",
        "t" * 43,
        workspace_id(workspace),
    )
    service = ExecutorService(config)
    replacement = b"new\r\ncontent\r\n"
    (config.work_root / "example.py").write_bytes(replacement)

    manifest = service.manifest("run", "approval")
    broker = WorkspaceBroker(workspace, tmp_path / "recovery")
    result = broker.publish(manifest, {item.path for item in manifest.operations})
    broker.undo(result.checkpoint_id)

    assert target.read_bytes() == original


def _seed_env_and_virtualenv_source(source: Path) -> None:
    (source / "env" / "loaders").mkdir(parents=True)
    (source / "env" / "settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    (source / "env" / "loaders" / "config.py").write_text("def load():\n    return {}\n", encoding="utf-8")
    for name in (".venv", "venv"):
        (source / name / "lib").mkdir(parents=True)
        (source / name / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (source / name / "lib" / "site.py").write_text("needle = 'dependency'\n", encoding="utf-8")


def test_real_env_source_directory_is_staged_while_virtualenvs_stay_excluded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    _seed_env_and_virtualenv_source(source)

    snapshot = seed_staging(make_config(source, work))

    assert set(snapshot.hashes) == {"env/settings.py", "env/loaders/config.py"}
    assert (work / "env" / "settings.py").read_text(encoding="utf-8") == "DEBUG = False\n"
    assert not (work / ".venv").exists()
    assert not (work / "venv").exists()


def test_env_source_directory_is_visible_to_tools_and_publishes(tmp_path: Path) -> None:
    import asyncio

    from shared.tools import ToolRequest

    source = tmp_path / "source"
    work = tmp_path / "work"
    _seed_env_and_virtualenv_source(source)
    (source / "env" / "settings.py").write_text("DEBUG = False  # needle\n", encoding="utf-8")
    service = ExecutorService(make_config(source, work))

    def run(tool: str, mode: str, arguments: dict):
        result = asyncio.run(service.execute(ToolRequest(tool, "run", tool, mode, arguments)))
        assert result.ok, result.to_dict()
        return result

    for mode in ("plan", "agent"):
        assert "env/" in run("ls", mode, {"details": False}).output.splitlines()
        assert ".venv/" not in run("ls", mode, {"details": False}).output.splitlines()
        found = run("find", mode, {"glob": "*.py"}).output.splitlines()
        assert {"env/settings.py", "env/loaders/config.py"} <= set(found)
        assert not any(line.startswith(("venv/", ".venv/")) for line in found)
        grep_paths = {line.split(":", 1)[0] for line in run("grep", mode, {"query": "needle"}).output.splitlines()}
        assert grep_paths == {"env/settings.py"}
        assert run("read", mode, {"path": "env/settings.py"}).output == "DEBUG = False  # needle"

    run("edit", "agent", {"path": "env/settings.py", "old_str": "False", "new_str": "True"})
    manifest = service.manifest("run", "approval")
    assert [(item.path, item.operation) for item in manifest.operations] == [("env/settings.py", "update")]


def test_read_rejects_paths_hidden_from_find_grep_and_ls(tmp_path: Path) -> None:
    import asyncio

    from shared.tools import ToolRequest

    source = tmp_path / "source"
    work = tmp_path / "work"
    _seed_env_and_virtualenv_source(source)
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / "node_modules" / "pkg").mkdir(parents=True)
    (source / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (source / ".local-chat-notes.txt").write_text("executor metadata\n", encoding="utf-8")
    service = ExecutorService(make_config(source, work))
    # Recreate the same hidden text inside staging so Agent mode is checked on real bytes too.
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    for mode in ("plan", "agent"):
        for path in (".git/HEAD", "node_modules/pkg/index.js", "venv/lib/site.py", ".venv/pyvenv.cfg", ".local-chat-notes.txt"):
            result = asyncio.run(service.execute(ToolRequest("read", "run", "read", mode, {"path": path})))
            assert not result.ok, (mode, path)
            assert result.error_code == "path.missing", (mode, path, result.error_code)
