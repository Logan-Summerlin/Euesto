from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from executor import checkpoints as checkpoints_module
from executor import staging as staging_module
from executor.app import ExecutorService
from executor.checkpoints import create_checkpoint, restore_checkpoint
from executor.config import ExecutorConfig
from executor.staging import clear_hash_cache, visible_files
from shared.tools import ToolRequest


def _config(source: Path, work: Path) -> ExecutorConfig:
    return ExecutorConfig(source_root=source, work_root=work, socket_path=work.parent / "executor.sock", token="t" * 43, workspace_id="workspace")


def _tree(root: Path, count: int) -> None:
    for index in range(count):
        directory = root / f"pkg{index // 100}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"module{index}.py").write_text(f"value = {index}\n" * 4, encoding="utf-8")


def _settle() -> None:
    """Let every file age past the racy window so cached digests become trusted."""
    time.sleep(staging_module.RACY_WINDOW_NS / 1e9 + 0.05)


class HashCounter:
    def __init__(self, monkeypatch) -> None:
        self.paths: list[str] = []
        original = staging_module.sha256_file

        def counting(path: Path) -> str:
            self.paths.append(str(path))
            return original(path)

        monkeypatch.setattr(staging_module, "sha256_file", counting)
        original_store = checkpoints_module._store_object

        def counting_store(source: Path, object_path: Path, digest: str) -> None:
            self.paths.append(f"store:{source}")
            original_store(source, object_path, digest)

        monkeypatch.setattr(checkpoints_module, "_store_object", counting_store)


def _hashes_for_one_write(tmp_path: Path, monkeypatch, file_count: int) -> tuple[int, dict]:
    source = tmp_path / f"source-{file_count}"
    work = tmp_path / f"work-{file_count}"
    _tree(source, file_count)
    service = ExecutorService(_config(source, work))
    # First mutation populates the checkpoint object store; it is the one-time cost.
    first = asyncio.run(service.execute(ToolRequest("w0", "run", "write", "agent", {"path": "pkg0/module0.py", "content": "value = 'first'\n"})))
    assert first.ok, first.to_dict()
    _settle()
    visible_files(work)  # warm: every digest is now observed after its file last changed
    counter = HashCounter(monkeypatch)
    result = asyncio.run(service.execute(ToolRequest("w1", "run", "write", "agent", {"path": "pkg0/module1.py", "content": "value = 'changed'\n"})))
    assert result.ok, result.to_dict()
    monkeypatch.undo()
    return len(counter.paths), result.data


def test_single_file_mutation_hashing_does_not_scale_with_repository_size(tmp_path: Path, monkeypatch) -> None:
    small, small_data = _hashes_for_one_write(tmp_path, monkeypatch, 50)
    large, large_data = _hashes_for_one_write(tmp_path, monkeypatch, 1_500)
    # Checkpoint + post-mutation status re-hash only what changed, whatever the tree size.
    assert small == large
    assert large <= 4
    assert large_data["workspace_status"]["modified"] == ["pkg0/module0.py", "pkg0/module1.py"]
    assert small_data["workspace_status"] == {**large_data["workspace_status"]}


@pytest.mark.posix
def test_cache_detects_same_size_rewrites_and_racy_changes(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("aaaa", encoding="utf-8")
    first = visible_files(root)["a.txt"][0]
    # Rewritten immediately (inside the racy window): still re-hashed, never trusted stale.
    target.write_text("bbbb", encoding="utf-8")
    second = visible_files(root)["a.txt"][0]
    assert second != first
    _settle()
    visible_files(root)
    # Same size, same inode, mtime restored: ctime still changes, so the digest refreshes.
    stat = target.stat()
    with target.open("r+", encoding="utf-8") as handle:
        handle.write("cccc")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    third = visible_files(root)["a.txt"][0]
    assert third not in {first, second}
    target.unlink()
    assert "a.txt" not in visible_files(root)


def test_cached_listing_matches_a_cold_listing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _tree(source, 120)
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    work = tmp_path / "work"
    ExecutorService(_config(source, work))
    (work / "pkg0" / "module3.py").write_text("changed\n", encoding="utf-8")
    (work / "pkg0" / "module4.py").chmod(0o700)
    warm = visible_files(work)
    clear_hash_cache(work)
    cold = visible_files(work)
    assert warm == cold
    assert not any(path.startswith(".git") or ".local-chat-" in path for path in cold)


def test_checkpoint_restore_is_unchanged_with_a_warm_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _tree(source, 30)
    work = tmp_path / "work"
    ExecutorService(_config(source, work))
    _settle()
    before = visible_files(work)
    checkpoint = create_checkpoint(work)
    (work / "pkg0" / "module1.py").write_text("broken\n", encoding="utf-8")
    (work / "pkg0" / "new.py").write_text("new\n", encoding="utf-8")
    (work / "pkg0" / "module2.py").unlink()
    restore_checkpoint(work, checkpoint)
    assert visible_files(work) == before


@pytest.mark.posix
@pytest.mark.slow
@pytest.mark.timeout(600)
def test_large_workspace_mutation_is_dominated_by_the_write_not_the_tree_hash(tmp_path: Path, monkeypatch) -> None:
    """Benchmark: 50,000 small files. Once warm, one write re-hashes O(1) files and its
    checkpoint step is cheaper than a single cold hash of the tree."""
    source = tmp_path / "source"
    for index in range(50_000):
        directory = source / f"d{index // 500}"
        if index % 500 == 0:
            directory.mkdir(parents=True)
        (directory / f"f{index}.txt").write_bytes(os.urandom(512))
    work = tmp_path / "work"
    service = ExecutorService(_config(source, work))
    assert asyncio.run(service.execute(ToolRequest("w0", "run", "write", "agent", {"path": "d0/new.txt", "content": "seed\n"}))).ok
    _settle()
    visible_files(work)
    clear_hash_cache(work)
    cold_started = time.perf_counter()
    visible_files(work)
    cold_seconds = time.perf_counter() - cold_started
    _settle()
    visible_files(work)
    counter = HashCounter(monkeypatch)
    started = time.perf_counter()
    result = asyncio.run(service.execute(ToolRequest("w1", "run", "write", "agent", {"path": "d0/new.txt", "content": "changed\n"})))
    elapsed = time.perf_counter() - started
    assert result.ok, result.to_dict()
    assert len(counter.paths) <= 4
    assert elapsed < cold_seconds * 2, (elapsed, cold_seconds)


def test_prune_reads_manifests_after_a_cold_reference_cache(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    first = create_checkpoint(root, max_checkpoints=2)
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    checkpoints_module._reference_cache.clear()
    second = create_checkpoint(root, max_checkpoints=2)
    (root / "a.txt").write_text("three\n", encoding="utf-8")
    checkpoints_module._reference_cache.clear()
    create_checkpoint(root, max_checkpoints=2)
    stored = {path.name for path in (root / ".local-chat-checkpoints" / "objects").iterdir()}
    # The oldest checkpoint was pruned together with the only object it referenced.
    assert not (root / ".local-chat-checkpoints" / first).exists()
    assert len(stored) == 2
    restore_checkpoint(root, second)
    assert (root / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_executor_serves_read_only_tools_concurrently(tmp_path: Path, monkeypatch) -> None:
    import executor.app as app_module

    source = tmp_path / "source"
    source.mkdir()
    service = ExecutorService(_config(source, tmp_path / "work"))

    def slow_ls(root, arguments, **_kwargs):
        time.sleep(0.3)
        return "", {"count": 0}

    monkeypatch.setattr(app_module, "ls", slow_ls)

    async def scenario() -> float:
        started = time.perf_counter()
        results = await asyncio.gather(*(service.execute(ToolRequest(f"ls-{index}", "run", "ls", "agent", {})) for index in range(4)))
        assert all(result.ok for result in results)
        return time.perf_counter() - started

    assert asyncio.run(scenario()) < 0.9
