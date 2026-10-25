"""Git history in the sandbox (plan item 2.6)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from executor.app import ExecutorService
from shared.tools import PublicationReceipt, ToolRequest
from tests.helpers import executor_config

pytestmark = [
    pytest.mark.posix,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
]
GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *arguments],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def run(service: ExecutorService, tool: str, arguments: dict):
    return asyncio.run(service.execute(ToolRequest("req", "run", tool, "agent", arguments)))


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "remote.origin.url", "https://user:token@example.com/repo.git")
    git(source, "config", "credential.helper", "store")
    (source / "kept.txt").write_text("one\n", encoding="utf-8")
    (source / "edited.txt").write_text("one\n", encoding="utf-8")
    (source / "removed.txt").write_text("one\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "first")
    (source / "kept.txt").write_text("two\n", encoding="utf-8")
    git(source, "commit", "-q", "-am", "second")
    hooks = source / ".git" / "hooks"
    (hooks / "post-checkout").write_text("#!/bin/sh\ntouch /tmp/euesto-hook-ran\n")
    (hooks / "post-checkout").chmod(0o755)
    # Uncommitted user work is part of the seeded baseline.
    (source / "edited.txt").write_text("user draft\n", encoding="utf-8")
    (source / "untracked.txt").write_text("new\n", encoding="utf-8")
    return source


def test_the_sandbox_repository_has_history_and_no_remotes_hooks_or_credentials(
    repository: Path,
) -> None:
    service = ExecutorService(executor_config(repository))
    work = service.config.work_root
    assert service.git["available"] is True
    assert git(work, "log", "--format=%s", "HEAD").split() == ["second", "first"]
    assert git(work, "remote") == ""
    config = (work / ".git" / "config").read_text()
    assert "example.com" not in config and "credential" not in config and "url" not in config
    assert "hooksPath = /dev/null" in config
    assert not (work / ".git" / "hooks").exists()
    assert git(work, "config", "--get", "core.hooksPath").strip() == "/dev/null"


def test_git_diff_against_the_base_shows_exactly_the_agents_changes(repository: Path) -> None:
    service = ExecutorService(executor_config(repository))
    work = service.config.work_root
    assert git(work, "diff", "--name-status", "euesto/base") == ""
    # The user's uncommitted work shows against HEAD, not against the base.
    assert "edited.txt" in git(work, "diff", "--name-only", "HEAD")

    assert run(service, "edit", {"path": "kept.txt", "old_str": "two", "new_str": "three"}).ok
    assert run(service, "bash", {"command": "rm removed.txt && echo x > created.txt"}).ok
    status = run(service, "status", {})
    expected = {
        ("M" if item["operation"] == "update" else "D"): item["path"]
        for item in status.data["changes"]
        if item["operation"] != "create"
    }
    diff = dict(
        line.split("\t")
        for line in git(work, "diff", "--name-status", "euesto/base").splitlines()
    )
    assert diff == expected == {"M": "kept.txt", "D": "removed.txt"}
    untracked = git(work, "ls-files", "--others", "--exclude-standard").split()
    assert untracked == ["created.txt"]


def test_publication_moves_the_base_and_git_never_reaches_a_manifest(repository: Path) -> None:
    service = ExecutorService(executor_config(repository))
    work = service.config.work_root
    assert run(service, "write", {"path": "kept.txt", "content": "published\n"}).ok
    assert run(service, "bash", {"command": "git status --short > /dev/null"}).ok
    manifest = service.manifest("run", "approval")
    assert [operation.path for operation in manifest.operations] == ["kept.txt"]
    assert not any(".git" in operation.path for operation in manifest.operations)
    for operation in manifest.operations:
        (repository / operation.path).write_text(operation.content or "", encoding="utf-8")
    service.mark_published(PublicationReceipt.from_manifest(manifest))
    assert git(work, "diff", "--name-status", "euesto/base") == ""
    assert git(work, "show", "euesto/base:kept.txt") == "published\n"
    assert git(work, "rev-parse", "euesto/base^") == git(work, "rev-parse", "HEAD")


def test_base_updates_never_follow_links_planted_in_git(
    repository: Path, tmp_path: Path
) -> None:
    service = ExecutorService(executor_config(repository))
    work = service.config.work_root
    planted = tmp_path / "planted"
    planted.mkdir()
    objects = work / ".git" / "objects"
    shutil.rmtree(objects)
    os.symlink(planted, objects)
    assert run(service, "write", {"path": "kept.txt", "content": "next\n"}).ok
    manifest = service.manifest("run", "approval")
    service.mark_published(PublicationReceipt.from_manifest(manifest))
    assert list(planted.iterdir()) == []
    assert service.git["available"] is False


def test_a_workspace_without_git_reports_history_unavailable(tmp_path: Path) -> None:
    service = ExecutorService(executor_config(tmp_path / "plain"))
    assert service.git == {
        "available": False,
        "depth": 200,
        "base_ref": None,
        "reason": "the workspace is not a git repository",
    }
    assert not (service.config.work_root / ".git").exists()
