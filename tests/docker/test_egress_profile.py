"""Live checks for the opt-in allowlisted egress profile (docs/EGRESS.md).

Runs only in the container tier with Docker and the disposable fixtures from
``scripts/docker-fixtures.sh``; it needs outbound access to PyPI from the proxy container.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "--file", str(ROOT / "docker" / "compose.yaml")]
EGRESS = [*COMPOSE, "--file", str(ROOT / "docker" / "compose.egress.yaml")]


def _available() -> bool:
    return bool(shutil.which("docker") and os.environ.get("LOCAL_CHAT_SECRETS_DIR") and os.environ.get("LOCAL_CHAT_WORKSPACE"))


def _run(arguments: list[str], *, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=check, env={**os.environ, "LOCAL_CHAT_WORKSPACE_ID": os.environ.get("LOCAL_CHAT_WORKSPACE_ID", "ci-workspace")})


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
def test_default_profile_renders_without_any_network() -> None:
    rendered = json.loads(_run([*COMPOSE, "--profile", "agent", "config", "--format", "json"]).stdout)
    executor = rendered["services"]["executor"]
    assert executor.get("network_mode") == "none"
    assert "egress-proxy" not in rendered["services"]


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
@pytest.mark.timeout(900)
def test_allowlisted_pip_install_succeeds_and_other_hosts_are_refused_and_logged() -> None:
    rendered = json.loads(_run([*EGRESS, "--profile", "agent", "config", "--format", "json"]).stdout)
    assert "network_mode" not in rendered["services"]["executor"]
    assert rendered["networks"]["egress-internal"]["internal"] is True
    try:
        _run([*EGRESS, "--profile", "agent", "up", "--detach", "--build", "--wait"], timeout=900)
        install = (
            "import os, subprocess, sys\n"
            "from executor.egress import proxy_environment\n"
            "env = {**os.environ, **proxy_environment(), 'HOME': '/tmp'}\n"
            "subprocess.run([sys.executable, '-m', 'venv', '/work/.egress-venv'], check=True)\n"
            "sys.exit(subprocess.call(['/work/.egress-venv/bin/pip', 'install', '--no-cache-dir', 'six'], env=env))\n"
        )
        result = _run([*EGRESS, "--profile", "agent", "exec", "-T", "executor", "python", "-c", install], check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        refused = (
            "import os, urllib.request\n"
            "from executor.egress import proxy_environment\n"
            "os.environ.update(proxy_environment())\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com/', timeout=15)\n"
            "except Exception as exc:\n"
            "    print('refused', type(exc).__name__)\n"
            "else:\n"
            "    raise SystemExit('non-allowlisted host was reachable')\n"
        )
        result = _run([*EGRESS, "--profile", "agent", "exec", "-T", "executor", "python", "-c", refused], check=False)
        assert result.returncode == 0 and "refused" in result.stdout, result.stdout + result.stderr
        direct = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
        assert _run([*EGRESS, "--profile", "agent", "exec", "-T", "executor", "python", "-c", direct], check=False).returncode != 0
        logs = _run([*EGRESS, "--profile", "agent", "logs", "--no-color", "egress-proxy"]).stdout
        entries = [json.loads(line.split("|", 1)[-1].strip()) for line in logs.splitlines() if "{" in line]
        assert any(item.get("event") == "egress.allowed" and item.get("host") == "pypi.org" for item in entries)
        assert any(item.get("event") == "egress.denied" and item.get("host") == "example.com" for item in entries)
    finally:
        _run([*EGRESS, "--profile", "agent", "down", "--volumes", "--remove-orphans"], check=False)
