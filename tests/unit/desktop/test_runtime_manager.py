from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime_manager import (
    DEFAULT_EXECUTOR_IMAGE,
    DEFAULT_GATEWAY_IMAGE,
    RUNNER_TARGETS,
    RuntimeErrorMessage,
    RuntimeImages,
    RuntimeSettings,
    RuntimeTarget,
    _redact_output,
    compose_base_args,
    create_session_tokens,
    detect_runner_target,
    record_staged_session,
    remove_staged_session,
    staged_sessions,
    workspace_key,
)

DIGEST = "sha256:" + "a" * 64


def write_manifest(
    root: Path,
    *,
    gateway: str,
    executor: str,
    runners: dict[str, str] | None = None,
    schema_version: int = 2,
) -> None:
    docker = root / "docker"
    docker.mkdir()
    if runners is None:
        runners = {
            target: f"ghcr.io/example/euesto-runner-{target}:v1.1.0@{DIGEST}"
            for target in RUNNER_TARGETS
        }
    (docker / "release-images.json").write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "gateway": gateway,
                "executor": executor,
                "runners": runners,
            }
        ),
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
    assert (
        images.runner("polyglot") == f"ghcr.io/example/euesto-runner-polyglot:v1.1.0@{DIGEST}"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 1},
        {"runners": {"base": f"ghcr.io/example/base@{DIGEST}"}},
        {"runners": {target: f"ghcr.io/example/{target}:v1" for target in RUNNER_TARGETS}},
    ],
)
def test_release_manifest_requires_schema_two_with_every_pinned_runner(
    tmp_path: Path, change: dict
) -> None:
    write_manifest(
        tmp_path,
        gateway=f"ghcr.io/example/gateway@{DIGEST}",
        executor=f"ghcr.io/example/executor@{DIGEST}",
        **change,
    )
    with pytest.raises(RuntimeErrorMessage):
        RuntimeImages.for_bundle(tmp_path)


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


@pytest.mark.parametrize(
    ("files", "target"),
    [
        ((), "base"),
        (("pyproject.toml",), "python"),
        (("package.json", "pnpm-lock.yaml"), "node"),
        (("pyproject.toml", "package.json"), "polyglot"),
        (("go.mod",), "polyglot"),
        (("Cargo.toml",), "polyglot"),
    ],
)
def test_runner_image_is_detected_from_project_files(
    tmp_path: Path, files: tuple[str, ...], target: str
) -> None:
    for name in files:
        (tmp_path / name).write_text("", encoding="utf-8")
    assert detect_runner_target(tmp_path) == target


@pytest.mark.parametrize(
    "settings",
    [
        RuntimeSettings(runner_target="ubuntu"),
        RuntimeSettings(cpus=1.0),
        RuntimeSettings(cpus=9.0),
        RuntimeSettings(memory_gb=1),
        RuntimeSettings(memory_gb=17),
        RuntimeSettings(pids=128),
        RuntimeSettings(pids=5_000),
        RuntimeSettings(git_history_depth=-1),
    ],
)
def test_runner_settings_outside_their_ceilings_fail_closed(settings: RuntimeSettings) -> None:
    with pytest.raises(RuntimeErrorMessage):
        settings.validated()


def test_runner_resources_default_from_dockers_allocation() -> None:
    settings = RuntimeSettings().validated()
    assert settings.resolved(4, 16 * 1024**3) == (3.0, 8)
    assert settings.resolved(32, 64 * 1024**3) == (8.0, 16)
    assert settings.resolved(1, 2 * 1024**3) == (2.0, 2)
    assert RuntimeSettings(cpus=6, memory_gb=12).validated().resolved(4, 1) == (6, 12)


def test_workspace_key_is_the_identity_prefix() -> None:
    assert workspace_key("ABCDEF0123456789" + "0" * 48) == "abcdef012345"
    with pytest.raises(RuntimeErrorMessage):
        workspace_key("not-hex-identity")


def test_staged_sessions_report_age_size_and_staleness(tmp_path: Path) -> None:
    now = 1_000 * 86_400.0
    record_staged_session(
        tmp_path, "aaaaaaaaaaaa", tmp_path / "a", "python", now=now - 2 * 86_400
    )
    record_staged_session(
        tmp_path, "bbbbbbbbbbbb", tmp_path / "b", "node", now=now - 40 * 86_400
    )
    usage = {"Volumes": [{"Name": "euesto-work-aaaaaaaaaaaa", "Size": "12MB"}]}

    def docker(arguments: list[str]) -> str:
        if arguments[:2] == ["volume", "ls"]:
            return (
                "euesto-work-bbbbbbbbbbbb\neuesto-work-aaaaaaaaaaaa\neuesto-work-cccccccccccc\n"
            )
        return json.dumps(usage)

    sessions = staged_sessions(docker, tmp_path, now=now)
    assert [(item["key"], item["age_days"], item["stale"]) for item in sessions] == [
        ("aaaaaaaaaaaa", 2, False),
        ("bbbbbbbbbbbb", 40, True),
        ("cccccccccccc", None, True),
    ]
    assert sessions[0]["size"] == "12MB" and sessions[0]["runner"] == "python"


def test_removing_a_staged_session_deletes_its_volumes_but_never_the_active_one(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    record_staged_session(tmp_path, "aaaaaaaaaaaa", tmp_path / "a", "python")
    with pytest.raises(RuntimeErrorMessage):
        remove_staged_session(calls.append, tmp_path, "aaaaaaaaaaaa", active_key="aaaaaaaaaaaa")
    with pytest.raises(RuntimeErrorMessage):
        remove_staged_session(calls.append, tmp_path, "../x", active_key=None)
    assert calls == []
    remove_staged_session(
        lambda args: calls.append(args) or "", tmp_path, "aaaaaaaaaaaa", active_key=None
    )
    assert calls == [
        [
            "volume",
            "rm",
            "--force",
            "euesto-work-aaaaaaaaaaaa",
            "euesto-state-aaaaaaaaaaaa",
            "euesto-cache-aaaaaaaaaaaa",
        ]
    ]
    assert staged_sessions(lambda args: "", tmp_path) == []
