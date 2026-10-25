"""The workspace policy file (harness plan 4.6) and registry allowlist additions (5.2)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared.policy import (
    DEFAULT_REGISTRY_HOSTS,
    REGISTRY_ADDITIONS,
    PolicyError,
    WorkspacePolicy,
    detect_setup_command,
    manifests_digest,
    parse_policy,
    read_policy,
    setup_manifests,
)

FULL = """
version = 1

[runner]
image = "python"

[network]
mode = "registries"
allow_hosts = ["crates.io", "Proxy.Golang.org"]

[setup]
command = "uv sync --frozen"
isolated = false
timeout_seconds = 600

[checks]
test = "python -m pytest -q"
lint = { command = "ruff check .", timeout_seconds = 120 }

[secrets]
patterns = ["*.vault"]

[publication]
never_publish = ["deploy/**"]
verify_before_publish = ["test"]
"""


def test_a_full_policy_parses_and_round_trips_through_the_workspace_config() -> None:
    policy = parse_policy(FULL)
    assert policy.runner_image == "python" and policy.network_mode == "registries"
    assert policy.allow_hosts == ("crates.io", "proxy.golang.org")
    assert policy.registry_hosts == (*DEFAULT_REGISTRY_HOSTS, "crates.io", "proxy.golang.org")
    assert policy.setup_command == "uv sync --frozen" and policy.setup_isolated is False
    assert [(check.name, check.timeout_seconds) for check in policy.checks] == [
        ("test", 300),
        ("lint", 120),
    ]
    assert policy.secret_patterns == ("*.vault",) and policy.never_publish == ("deploy/**",)
    config = policy.to_config()
    assert config["checks"]["lint"] == {"command": "ruff check .", "timeout_seconds": 120}
    assert WorkspacePolicy.from_config(config) == policy


def test_an_empty_policy_is_the_default() -> None:
    assert parse_policy("version = 1\n") == WorkspacePolicy()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "version must be 1"),
        ("version = 2", "version must be 1"),
        ("version = 1\nextra = true", "unknown key: extra"),
        ("version = 1\n[runner]\nimage = 'gpu'", "runner.image"),
        ("version = 1\n[runner]\nnetwork = 'x'", "unknown key: runner.network"),
        ("version = 1\n[network]\nmode = 'open'", "network.mode"),
        (
            "version = 1\n[network]\nallow_hosts = ['github.com']",
            "github.com is not an allowed",
        ),
        ("version = 1\n[network]\nallow_hosts = ['*.crates.io']", "not an allowed"),
        ("version = 1\n[setup]\ncommand = ''", "setup.command"),
        ("version = 1\n[setup]\ncommand = 'x'\nisolated = 'yes'", "setup.isolated"),
        ("version = 1\n[setup]\ncommand = 'x'\ntimeout_seconds = 99999", "setup.timeout"),
        ("version = 1\n[checks]\n'Bad Name' = 'x'", "lowercase identifiers"),
        ("version = 1\n[checks]\ntest = { command = 'x', shell = 'zsh' }", "checks.test"),
        ("version = 1\n[checks]\ntest = { command = 'x', timeout_seconds = 0 }", "timeout"),
        ("version = 1\n[secrets]\npatterns = ['a/b']", "secrets.patterns"),
        ("version = 1\n[publication]\nnever_publish = ['../x']", "never_publish"),
        ("version = 1\n[publication]\nverify_before_publish = ['test']", "no check named test"),
        ("version = 1\n[publication", "not valid TOML"),
    ],
)
def test_schema_violations_fail_closed(text: str, message: str) -> None:
    with pytest.raises(PolicyError) as caught:
        parse_policy(text)
    assert message in str(caught.value)


def test_every_problem_is_reported_together() -> None:
    with pytest.raises(PolicyError) as caught:
        parse_policy("version = 3\n[network]\nmode = 'open'\nallow_hosts = ['github.com']")
    assert len(caught.value.errors) == 3


def test_only_public_registries_can_be_added() -> None:
    for host in REGISTRY_ADDITIONS:
        assert parse_policy(f"version = 1\n[network]\nallow_hosts = ['{host}']").allow_hosts
    for host in ("github.com", "raw.githubusercontent.com", "objects.githubusercontent.com"):
        with pytest.raises(PolicyError):
            parse_policy(f"version = 1\n[network]\nallow_hosts = ['{host}']")


def test_the_host_file_is_read_and_its_digest_identifies_it(tmp_path: Path) -> None:
    assert read_policy(tmp_path).status == "absent"
    (tmp_path / ".euesto").mkdir()
    policy_path = tmp_path / ".euesto" / "policy.toml"
    policy_path.write_text(FULL, encoding="utf-8")
    first = read_policy(tmp_path)
    assert first.status == "valid" and first.policy == parse_policy(FULL)
    policy_path.write_text(FULL.replace("120", "60"), encoding="utf-8")
    second = read_policy(tmp_path)
    assert second.status == "valid" and second.digest != first.digest
    policy_path.write_text("version = 1\nnope = 1", encoding="utf-8")
    invalid = read_policy(tmp_path)
    assert invalid.status == "invalid" and invalid.policy is None and invalid.errors


@pytest.mark.posix
def test_a_linked_policy_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".euesto").mkdir()
    target = tmp_path / "elsewhere.toml"
    target.write_text("version = 1\n", encoding="utf-8")
    os.symlink(target, tmp_path / ".euesto" / "policy.toml")
    assert read_policy(tmp_path).status == "invalid"


def test_setup_commands_are_detected_from_lockfiles(tmp_path: Path) -> None:
    assert detect_setup_command(tmp_path) is None
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    (tmp_path / "go.mod").write_text("", encoding="utf-8")
    assert detect_setup_command(tmp_path) == (
        "uv sync --frozen --no-install-project && pnpm install --frozen-lockfile && go mod download"
    )


def test_a_manifest_change_changes_the_setup_digest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    first = manifests_digest(tmp_path)
    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert manifests_digest(tmp_path) == first
    (tmp_path / "uv.lock").write_text("lock", encoding="utf-8")
    assert manifests_digest(tmp_path) != first
    assert setup_manifests(tmp_path) == ["pyproject.toml", "uv.lock"]
