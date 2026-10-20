from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime_manager import (
    DEFAULT_EXECUTOR_IMAGE,
    DEFAULT_GATEWAY_IMAGE,
    RuntimeErrorMessage,
    RuntimeImages,
    RuntimeTarget,
    _redact_output,
    compose_base_args,
    create_session_tokens,
)


def write_manifest(root: Path, *, gateway: str, executor: str) -> None:
    docker = root / "docker"
    docker.mkdir()
    (docker / "release-images.json").write_text(
        json.dumps({"schema_version": 1, "gateway": gateway, "executor": executor}),
        encoding="utf-8",
    )


def test_source_bundle_defaults_to_buildable_developer_images(tmp_path: Path) -> None:
    images = RuntimeImages.for_bundle(tmp_path)

    assert images == RuntimeImages(DEFAULT_GATEWAY_IMAGE, DEFAULT_EXECUTOR_IMAGE, False)


def test_release_bundle_fails_closed_without_manifest(tmp_path: Path) -> None:
    with pytest.raises(RuntimeErrorMessage, match="missing its Docker image manifest"):
        RuntimeImages.for_bundle(tmp_path, require_manifest=True)


def test_release_manifest_requires_digest_pinned_images(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    write_manifest(
        tmp_path,
        gateway=f"ghcr.io/example/gateway:v1.1.0@{digest}",
        executor=f"ghcr.io/example/executor:v1.1.0@{digest}",
    )

    images = RuntimeImages.for_bundle(tmp_path)

    assert images.prebuilt is True
    assert images.gateway.endswith(digest)
    assert images.executor.endswith(digest)


def test_release_manifest_rejects_unpinned_image(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        gateway="ghcr.io/example/gateway:v1.1.0",
        executor="ghcr.io/example/executor:v1.1.0",
    )

    with pytest.raises(RuntimeErrorMessage, match="digest-pinned"):
        RuntimeImages.for_bundle(tmp_path)


def test_session_tokens_are_long_random_and_replaced(tmp_path: Path) -> None:
    first = create_session_tokens(tmp_path)
    second = create_session_tokens(tmp_path)

    assert first != second
    for value in second:
        assert len(value.encode("utf-8")) >= 32
        assert "\n" not in value
    assert (tmp_path / "gateway_token.txt").read_text(encoding="utf-8") == second[0]
    assert (tmp_path / "executor_token.txt").read_text(encoding="utf-8") == second[1]


def test_runtime_target_uses_the_canonical_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    target = RuntimeTarget.from_workspace(workspace)

    assert target.workspace == workspace.resolve()
    assert target.workspace_identity


def test_compose_arguments_pin_project_name(tmp_path: Path) -> None:
    arguments = compose_base_args(tmp_path / "compose.yaml")

    assert arguments[:4] == ["compose", "--project-name", "local-openrouter-chat", "--file"]
    assert arguments[4] == str(tmp_path / "compose.yaml")


def test_docker_diagnostics_redact_secrets() -> None:
    result = _redact_output(
        "gateway_token.txt leaked-secret\nexecutor_token.txt another-secret",
        {"LOCAL_CHAT_GATEWAY_TOKEN": "leaked-secret"},
        ("another-secret",),
    )

    assert "leaked-secret" not in result
    assert "another-secret" not in result
    assert "[REDACTED]" in result
    assert "[TOKEN_FILE]" in result
