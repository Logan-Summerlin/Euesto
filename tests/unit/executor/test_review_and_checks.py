"""Review, partial publication, declared checks, and policy paths in the executor
(harness plan 3.4, 4.4, 4.5, 4.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from executor.app import ExecutorService, create_app
from executor.errors import ExecutorToolError
from executor.paths import (
    UnsafePath,
    configure_secret_patterns,
    is_secret_path,
    matches_publish_glob,
    normalize_relative,
    validate_publish_globs,
    validate_secret_patterns,
)
from shared.tools import PublicationReceipt, ToolRequest
from tests.helpers import executor_config


def run(service: ExecutorService, tool: str, arguments: dict):
    return asyncio.run(service.execute(ToolRequest("req", "run", tool, "agent", arguments)))


def make(tmp_path: Path, files: dict[str, str] | None = None, **config) -> ExecutorService:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    for name, content in (files or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return ExecutorService(executor_config(source, **config))


def publish(service: ExecutorService, paths: list[str] | None = None) -> list[str]:
    manifest = service.manifest("run", "approval", paths=paths)
    for operation in manifest.operations:
        target = service.config.source_root / operation.path
        if operation.operation == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(operation.content or "", encoding="utf-8")
    service.mark_published(PublicationReceipt.from_manifest(manifest))
    return [operation.path for operation in manifest.operations]


@pytest.fixture(autouse=True)
def _reset_secret_patterns():
    yield
    configure_secret_patterns(())


# -- 4.4 partial publication -----------------------------------------------------------------


def test_a_subset_publishes_only_its_files_and_the_rest_publish_later(tmp_path: Path) -> None:
    service = make(tmp_path, {"a.py": "a\n", "b.py": "b\n"})
    assert run(service, "write", {"path": "a.py", "content": "A\n"}).ok
    assert run(service, "write", {"path": "b.py", "content": "B\n"}).ok
    assert run(service, "write", {"path": "c.py", "content": "C\n"}).ok

    assert publish(service, ["c.py", "a.py"]) == ["a.py", "c.py"]
    source = service.config.source_root
    assert (source / "a.py").read_text() == "A\n" and (source / "b.py").read_text() == "b\n"
    remaining = service.workspace_status()
    assert remaining["modified"] == ["b.py"] and remaining["created"] == []
    assert publish(service) == ["b.py"]
    assert service.workspace_status()["staged"] is False


def test_a_subset_must_name_pending_changes(tmp_path: Path) -> None:
    service = make(tmp_path, {"a.py": "a\n"})
    assert run(service, "write", {"path": "a.py", "content": "A\n"}).ok
    with pytest.raises(ExecutorToolError, match="Not a publishable staged change"):
        service.manifest("run", "approval", paths=["missing.py"])
    with pytest.raises(ExecutorToolError):
        service.manifest("run", "approval", paths=[])
    with pytest.raises(UnsafePath):
        service.manifest("run", "approval", paths=["../a.py"])


def test_the_review_lists_per_file_diffs_and_the_fingerprint(tmp_path: Path) -> None:
    service = make(tmp_path, {"a.py": "one\ntwo\n"})
    assert run(service, "write", {"path": "a.py", "content": "one\nTWO\n"}).ok
    review = service.review({})
    assert review["counts"]["modified"] == 1
    assert [item["path"] for item in review["changes"]] == ["a.py"]
    assert "-two" in review["diffs"][0]["text"] and "+TWO" in review["diffs"][0]["text"]
    assert review["fingerprint"] == service.workspace_status()["fingerprint"]


# -- 4.6 never_publish -----------------------------------------------------------------------


def test_never_publish_paths_stay_staged_and_are_reported(tmp_path: Path) -> None:
    service = make(tmp_path, {"deploy/.keep": ""}, never_publish=("deploy/**", "*.lock"))
    assert run(service, "write", {"path": "deploy/prod.yaml", "content": "x\n"}).ok
    assert run(service, "write", {"path": "poetry.lock", "content": "x\n"}).ok
    assert run(service, "write", {"path": "app.py", "content": "x\n"}).ok
    manifest = service.manifest("run", "approval")
    assert [operation.path for operation in manifest.operations] == ["app.py"]
    status = service.workspace_status()
    assert status["never_publish"] == ["deploy/prod.yaml", "poetry.lock"]
    assert status["created"] == ["app.py", "deploy/prod.yaml", "poetry.lock"]
    with pytest.raises(ExecutorToolError):
        service.manifest("run", "approval", paths=["deploy/prod.yaml"])


@pytest.mark.parametrize(
    ("path", "patterns", "expected"),
    [
        ("deploy/prod.yaml", ["deploy/**"], True),
        ("deploy", ["deploy/**"], True),
        ("deployment/x", ["deploy/**"], False),
        ("config/Prod.env.json", ["config/*.json"], True),
        ("a/b/c.lock", ["*.lock"], True),
        ("docs/readme.md", ["docs"], True),
        ("src/app.py", ["docs"], False),
    ],
)
def test_never_publish_globs(path: str, patterns: list[str], expected: bool) -> None:
    assert matches_publish_glob(path, validate_publish_globs(patterns)) is expected


@pytest.mark.parametrize("pattern", ["/etc/**", "../x", "a\\b", "", 7])
def test_invalid_never_publish_globs_fail_closed(pattern) -> None:
    with pytest.raises(ValueError):
        validate_publish_globs([pattern])


# -- 4.5 blocked secret names ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "certs/server.pem",
        "tls/private.key",
        "store.p12",
        "store.PFX",
        ".netrc",
        ".git-credentials",
        "ops/kubeconfig",
        "infra/terraform.tfstate",
        "infra/terraform.tfstate.backup",
        ".dockercfg",
        "config/secrets.yaml",
        "Secrets.json",
    ],
)
def test_new_secret_names_are_blocked(path: str) -> None:
    assert is_secret_path(path)
    with pytest.raises(UnsafePath):
        normalize_relative(path)


@pytest.mark.parametrize("path", ["keyboard.py", "src/keys.py", "secretary.md", "pem.txt"])
def test_similar_ordinary_names_are_allowed(path: str) -> None:
    assert not is_secret_path(path)


def test_workspace_patterns_add_to_but_never_remove_the_built_in_set(tmp_path: Path) -> None:
    configure_secret_patterns(["*.vault", "prod-config"])
    assert is_secret_path("ops/site.vault") and is_secret_path("prod-config/app.yaml")
    assert is_secret_path("server.pem") and is_secret_path(".env")
    configure_secret_patterns(())
    assert not is_secret_path("ops/site.vault")
    for invalid in (["a/b"], [""], ["x" * 200], "*.vault"):
        with pytest.raises(ValueError):
            validate_secret_patterns(invalid)


def test_secret_files_never_reach_staging_or_manifests(tmp_path: Path) -> None:
    service = make(
        tmp_path,
        {"app.py": "x\n", "server.pem": "k\n", "infra/terraform.tfstate": "{}"},
        secret_patterns=("*.vault",),
    )
    (service.config.source_root / "site.vault").write_text("v")
    work = service.config.work_root
    assert (
        not (work / "server.pem").exists() and not (work / "infra/terraform.tfstate").exists()
    )
    listing = run(service, "find", {"glob": "*"})
    assert "server.pem" not in listing.output
    refused = run(service, "write", {"path": "new.key", "content": "k"})
    assert not refused.ok and refused.error_code == "path.unsafe"
    created = run(service, "bash", {"command": "echo k > other.pem && echo v > x.vault"})
    assert created.ok
    assert service.manifest("run", "approval").operations == ()


# -- 3.4 declared checks ---------------------------------------------------------------------


@pytest.mark.posix
def test_a_check_runs_against_staging_and_always_restores_it(tmp_path: Path) -> None:
    service = make(tmp_path, {"test.sh": "exit 0\n"})
    assert run(service, "write", {"path": "a.py", "content": "x = 1\n"}).ok
    before = service.workspace_status()["fingerprint"]
    result = asyncio.run(service.run_check("test", "cat a.py && echo artifact > built.txt", 30))
    assert result["outcome"] == "passed" and result["exit_code"] == 0
    assert "x = 1" in result["output"]
    assert result["fingerprint"] == before and result["restored"] is True
    assert not (service.config.work_root / "built.txt").exists()
    assert service.workspace_status()["fingerprint"] == before


@pytest.mark.posix
def test_a_failing_check_reports_its_exit_code(tmp_path: Path) -> None:
    service = make(tmp_path)
    result = asyncio.run(service.run_check("lint", "echo bad >&2; exit 3", 30))
    assert result["outcome"] == "failed" and result["exit_code"] == 3
    assert "bad" in result["output"]


@pytest.mark.posix
def test_a_later_mutation_changes_the_fingerprint(tmp_path: Path) -> None:
    service = make(tmp_path, {"a.py": "a\n"})
    result = asyncio.run(service.run_check("test", "true", 30))
    assert result["fingerprint"] == service.workspace_status()["fingerprint"]
    assert run(service, "edit", {"path": "a.py", "old_str": "a", "new_str": "b"}).ok
    assert result["fingerprint"] != service.workspace_status()["fingerprint"]


def test_check_names_are_validated(tmp_path: Path) -> None:
    service = make(tmp_path)
    for name in ("", "Bad Name", "x" * 40, None):
        with pytest.raises(ExecutorToolError):
            asyncio.run(service.run_check(name, "true", 30))


def test_review_and_check_routes_are_authenticated(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    service = make(tmp_path)
    client = TestClient(create_app(service.config, service))
    for path in ("/v1/staging/review", "/v1/staging/checks"):
        response = client.post(path, json={})
        assert response.status_code == 401
