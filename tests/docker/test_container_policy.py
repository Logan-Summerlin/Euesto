"""Image-level container policy for the gateway and executor (docs/ARCHITECTURE.md).

The rendered Compose hardening invariants (network mode, read-only root, no-new-privileges,
capability and mount sets, and the absence of the Docker socket) are asserted once, against the
rendered configuration, in ``tests/docker/test_runner_split.py``. This file keeps only what a
rendered configuration cannot show: how each image is built, and which loopback port and secret
the gateway is given.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

COMPOSE = Path("docker/compose.yaml")


def test_gateway_publishes_only_on_loopback() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert '"127.0.0.1:${LOCAL_CHAT_GATEWAY_PORT:-8765}:8765"' in compose
    assert "network_mode: host" not in compose


def test_gateway_gets_its_token_secret_and_never_the_source_mount() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "gateway_token" in compose
    gateway = compose.split("\n  executor:", 1)[0]
    assert "/source" not in gateway


def test_gateway_image_runs_as_fixed_non_root_user() -> None:
    dockerfile = Path("docker/Dockerfile.gateway").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert "--no-access-log" in dockerfile


def test_executor_image_runs_as_fixed_non_root_user() -> None:
    dockerfile = Path("docker/Dockerfile.executor").read_text(encoding="utf-8")
    assert "USER 10002:10001" in dockerfile
