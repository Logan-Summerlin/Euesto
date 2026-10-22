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
SETUP = [*EGRESS, "--file", str(ROOT / "docker" / "compose.setup.yaml")]
# What the executor passes into each command's environment in this profile.
PROXY_ENV = {
    "HTTPS_PROXY": "http://172.31.250.2:3128",
    "HTTP_PROXY": "http://172.31.250.2:3128",
}


def _available() -> bool:
    return bool(
        shutil.which("docker")
        and os.environ.get("LOCAL_CHAT_SECRETS_DIR")
        and os.environ.get("LOCAL_CHAT_WORKSPACE")
    )


def _run(
    arguments: list[str],
    *,
    check: bool = True,
    timeout: int = 600,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env={
            **os.environ,
            "LOCAL_CHAT_WORKSPACE_ID": os.environ.get(
                "LOCAL_CHAT_WORKSPACE_ID", "ci-workspace"
            ),
            **(extra_env or {}),
        },
    )


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
def test_default_profile_renders_without_any_network() -> None:
    rendered = json.loads(
        _run([*COMPOSE, "--profile", "agent", "config", "--format", "json"]).stdout
    )
    executor = rendered["services"]["executor"]
    assert executor.get("network_mode") == "none"
    assert rendered["services"]["runner"].get("network_mode") == "none"
    assert "egress-proxy" not in rendered["services"]


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
@pytest.mark.timeout(900)
def test_allowlisted_pip_install_succeeds_and_other_hosts_are_refused_and_logged() -> None:
    rendered = json.loads(
        _run([*EGRESS, "--profile", "agent", "config", "--format", "json"]).stdout
    )
    # Commands run in the runner, so only the runner joins the internal network.
    assert "network_mode" not in rendered["services"]["runner"]
    assert rendered["services"]["executor"]["network_mode"] == "none"
    assert rendered["networks"]["egress-internal"]["internal"] is True
    try:
        _run(
            [*EGRESS, "--profile", "agent", "up", "--detach", "--build", "--wait"], timeout=900
        )
        install = (
            "import os, subprocess, sys\n"
            f"env = {{**os.environ, **{PROXY_ENV!r}, 'HOME': '/tmp'}}\n"
            "subprocess.run([sys.executable, '-m', 'venv', '/work/.egress-venv'], check=True)\n"
            "sys.exit(subprocess.call(['/work/.egress-venv/bin/pip', 'install', '--no-cache-dir', 'six'], env=env))\n"
        )
        result = _run(
            [*EGRESS, "--profile", "agent", "exec", "-T", "runner", "python3", "-c", install],
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        refused = (
            "import os, urllib.request\n"
            f"os.environ.update({PROXY_ENV!r})\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com/', timeout=15)\n"
            "except Exception as exc:\n"
            "    print('refused', type(exc).__name__)\n"
            "else:\n"
            "    raise SystemExit('non-allowlisted host was reachable')\n"
        )
        result = _run(
            [*EGRESS, "--profile", "agent", "exec", "-T", "runner", "python3", "-c", refused],
            check=False,
        )
        assert result.returncode == 0 and "refused" in result.stdout, (
            result.stdout + result.stderr
        )
        direct = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"
        assert (
            _run(
                [
                    *EGRESS,
                    "--profile",
                    "agent",
                    "exec",
                    "-T",
                    "runner",
                    "python3",
                    "-c",
                    direct,
                ],
                check=False,
            ).returncode
            != 0
        )
        logs = _run(
            [*EGRESS, "--profile", "agent", "logs", "--no-color", "egress-proxy"]
        ).stdout
        entries = [
            json.loads(line.split("|", 1)[-1].strip())
            for line in logs.splitlines()
            if "{" in line
        ]
        assert any(
            item.get("event") == "egress.allowed" and item.get("host") == "pypi.org"
            for item in entries
        )
        assert any(
            item.get("event") == "egress.denied" and item.get("host") == "example.com"
            for item in entries
        )
    finally:
        _run(
            [*EGRESS, "--profile", "agent", "down", "--volumes", "--remove-orphans"],
            check=False,
        )


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
@pytest.mark.timeout(1200)
def test_isolated_setup_installs_without_seeing_source_and_the_agent_stays_offline(
    tmp_path: Path,
) -> None:
    """Plan 5.1: setup installs a package through the proxy; isolated setup never sees a
    source file; afterwards the agent's runner has the package and still no network."""
    from src.runtime_manager import setup_mounts

    workspace = Path(os.environ["LOCAL_CHAT_WORKSPACE"])
    (workspace / "requirements.txt").write_text("six\n", encoding="utf-8")
    mounts = tmp_path / "setup-mounts.json"
    mounts.write_text(json.dumps(setup_mounts(workspace, isolated=True)), encoding="utf-8")
    command = (
        "test ! -e fixture.txt && test -f requirements.txt && "
        "python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
    )
    try:
        _run(
            [*COMPOSE, "--profile", "agent", "up", "--detach", "--build", "--wait"], timeout=900
        )
        rendered = json.loads(
            _run([*COMPOSE, "--profile", "agent", "config", "--format", "json"]).stdout
        )
        assert rendered["services"]["runner"]["network_mode"] == "none"
        _run(
            [*COMPOSE, "--profile", "agent", "exec", "-T", "runner", "mkdir", "-p"]
            + ["/work/.venv", "/work/node_modules"]
        )
        result = _run(
            [
                *SETUP,
                "--file",
                str(mounts),
                "--profile",
                "agent",
                "--profile",
                "setup",
                "run",
                "--rm",
                "--no-TTY",
                "--build",
                "setup",
            ],
            check=False,
            timeout=900,
            extra_env={"LOCAL_CHAT_SETUP_COMMAND": command},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        _run([*SETUP, "--profile", "agent", "rm", "--stop", "--force", "egress-proxy"])
        offline = (
            "import socket, subprocess, sys\n"
            "subprocess.run(['/work/.venv/bin/python', '-c', 'import six'], check=True)\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
            "except OSError:\n"
            "    print('offline')\n"
            "else:\n"
            "    sys.exit('the agent runner reached the network')\n"
        )
        result = _run(
            [*COMPOSE, "--profile", "agent", "exec", "-T", "runner", "python3", "-c", offline],
            check=False,
        )
        assert result.returncode == 0 and "offline" in result.stdout, (
            result.stdout + result.stderr
        )
    finally:
        (workspace / "requirements.txt").unlink(missing_ok=True)
        _run(
            [*SETUP, "--profile", "agent", "down", "--volumes", "--remove-orphans"],
            check=False,
        )
