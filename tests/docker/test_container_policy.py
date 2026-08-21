from pathlib import Path

import pytest

pytestmark = pytest.mark.docker


def test_gateway_compose_keeps_loopback_and_least_privilege() -> None:
    compose = Path("docker/compose.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${LOCAL_CHAT_GATEWAY_PORT:-8765}:8765"' in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "read_only: true" in compose
    assert "gateway_token" in compose
    assert "network_mode: host" not in compose
    assert "/var/run/docker.sock" not in compose
    gateway = compose.split("\n  executor:", 1)[0]
    assert "/source" not in gateway
    assert "network_mode: none" in compose
    assert "target: /source" in compose
    assert "read_only: true" in compose
    assert "executor-ipc:/run/ipc" in compose


def test_gateway_image_runs_as_fixed_non_root_user() -> None:
    dockerfile = Path("docker/Dockerfile.gateway").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "--no-access-log" in dockerfile


def test_executor_image_and_compose_forbid_host_control_and_network() -> None:
    compose = Path("docker/compose.yaml").read_text(encoding="utf-8")
    dockerfile = Path("docker/Dockerfile.executor").read_text(encoding="utf-8")
    assert 'user: "10002:10001"' in compose
    assert "network_mode: none" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "/var/run/docker.sock" not in compose
    assert "privileged: true" not in compose
    assert "seccomp=unconfined" not in compose
    assert "USER 10002:10001" in dockerfile
