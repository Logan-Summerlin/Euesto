"""Live checks for the executor/runner split (plan items 2.1-2.4, 2.7; docs/ARCHITECTURE.md).

Runs only in the container tier with Docker and the disposable fixtures from
``scripts/docker-fixtures.sh``. The isolation checks run *inside a command* the runner's own
command server executed, which is exactly where a model's Bash call lands.

This file is also the one authoritative place for the rendered Compose hardening invariants
(network mode, read-only root, no-new-privileges, capability and mount sets): the workflow only
renders the configuration and uploads it for a reviewer.
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
PROJECT = "local-openrouter-chat"
RUNNER_IMAGE = "local-openrouter-chat-runner-python:1.1.0"
WORKSPACE_KEY = "c1c1c1c1c1c1"
# Every in-test ceiling stays below the docker tier's own deadline (scripts/validate.py), so a
# stuck build reports itself instead of the harness killing the tier with no name.
RUNNER_BUILD_TIMEOUT_SECONDS = 1200

# Every check must hold for a command, or the command prints which one failed.
ISOLATION_SCRIPT = r"""
fail() { echo "FAIL: $*"; exit 1; }
test ! -e /source || fail "the host workspace is mounted"
test -z "$(ls -A /run/secrets 2>/dev/null)" || fail "secrets are mounted"
test ! -e /run/ipc || fail "the executor socket directory is mounted"
test ! -e /state || fail "the state volume is mounted"
[ "$(id -u)" = 10002 ] || fail "not running as 10002"
grep -Eq '^CapEff:[[:space:]]+0+$' /proc/self/status || fail "effective capabilities"
grep -Eq '^CapPrm:[[:space:]]+0+$' /proc/self/status || fail "permitted capabilities"
grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status || fail "no-new-privileges"
[ "$(ls /sys/class/net)" = lo ] || fail "a network interface besides loopback"
python3 -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)" \
  2>/dev/null && fail "outbound network"
printf 'int main(void){return 42;}' > /work/.probe.c
cc -o /work/.probe /work/.probe.c || fail "compile"
chmod u+s /work/.probe
/work/.probe; [ $? = 42 ] || fail "a compiled binary in /work does not run"
mknod /work/.dev c 1 3 2>/dev/null && fail "device nodes can be created"
rm -f /work/.probe /work/.probe.c
echo ISOLATED
"""


def _available() -> bool:
    return bool(
        shutil.which("docker")
        and os.environ.get("LOCAL_CHAT_SECRETS_DIR")
        and os.environ.get("LOCAL_CHAT_WORKSPACE")
    )


def _env(**extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "LOCAL_CHAT_WORKSPACE_ID": os.environ.get("LOCAL_CHAT_WORKSPACE_ID", "ci-workspace"),
        "LOCAL_CHAT_WORKSPACE_KEY": WORKSPACE_KEY,
        **extra,
    }


def _run(
    arguments: list[str],
    *,
    check: bool = True,
    timeout: int = 900,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=env or _env(),
        input=stdin,
    )


def _render(**extra: str) -> dict:
    return json.loads(
        _run(
            [*COMPOSE, "--profile", "agent", "config", "--format", "json"], env=_env(**extra)
        ).stdout
    )


def _client(request: dict, *, image: str = RUNNER_IMAGE) -> list[dict]:
    """Send one request to the runner from a separate container, as the executor does."""
    script = (
        "import json, socket, sys\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        "s.connect('/run/runner/runner.sock')\n"
        "s.sendall(sys.stdin.buffer.read())\n"
        "data = b''\n"
        "while chunk := s.recv(65536):\n"
        "    data += chunk\n"
        "sys.stdout.write(data.decode())\n"
    )
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--user",
            "10002:10001",
            "--volume",
            f"{PROJECT}_runner-ipc:/run/runner",
            "--entrypoint",
            "python3",
            image,
            "-c",
            script,
        ],
        stdin=json.dumps(request) + "\n",
        timeout=300,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
def test_every_service_is_hardened_in_the_rendered_composition() -> None:
    """The authoritative compose-hardening check (it used to be asserted in workflow YAML)."""
    rendered = _render()
    services = rendered["services"]
    assert services
    for name, service in services.items():
        security_opt = service.get("security_opt", [])
        volumes = service.get("volumes", [])
        assert service.get("read_only") is True, name
        assert service.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in security_opt, name
        assert service.get("privileged") is not True, name
        assert not any("seccomp=unconfined" in item for item in security_opt), name
        assert service.get("network_mode") != "host", name
        assert not any("docker.sock" in json.dumps(volume) for volume in volumes), name
    for name in ("executor", "runner"):
        assert services[name]["user"] == "10002:10001", name
    assert "docker.sock" not in json.dumps(rendered)


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
def test_compose_gives_the_runner_only_staging_its_cache_and_its_socket() -> None:
    rendered = _render()
    services = rendered["services"]
    runner, executor = services["runner"], services["executor"]
    assert runner["network_mode"] == "none" and executor["network_mode"] == "none"
    assert runner["read_only"] is True and runner["user"] == "10002:10001"
    assert runner["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in runner["security_opt"]
    assert "secrets" not in runner and "environment" not in runner
    assert {volume["target"] for volume in runner["volumes"]} == {
        "/work",
        "/cache",
        "/run/runner",
    }
    assert {volume["target"] for volume in executor["volumes"]} == {
        "/source",
        "/run/ipc",
        "/run/runner",
        "/work",
        "/state",
    }
    assert not any(item.startswith("/work") for item in executor.get("tmpfs", []))
    gateway_targets = {volume["target"] for volume in services["gateway"]["volumes"]}
    assert not gateway_targets & {"/work", "/state", "/source", "/run/runner"}
    for kind in ("work", "state", "cache"):
        volume = rendered["volumes"][kind]
        assert volume["name"] == f"euesto-{kind}-{WORKSPACE_KEY}"
        assert volume["labels"]["io.euesto.workspace"] == WORKSPACE_KEY
    assert int(runner["pids_limit"]) == 1024


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
def test_runner_resource_settings_render_into_compose() -> None:
    runner = _render(
        LOCAL_CHAT_RUNNER_CPUS="6",
        LOCAL_CHAT_RUNNER_MEMORY="12g",
        LOCAL_CHAT_RUNNER_PIDS="1024",
    )["services"]["runner"]
    assert float(runner["cpus"]) == 6.0
    assert int(runner["mem_limit"]) == 12 * 1024**3
    assert int(runner["pids_limit"]) == 1024


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
@pytest.mark.timeout(1200)
def test_commands_run_isolated_in_the_runner_and_staging_survives_restarts() -> None:
    try:
        _run([*COMPOSE, "--profile", "agent", "up", "--detach", "--build", "--wait", "runner"])
        started, result = _client(
            {
                "op": "run",
                "request_id": "isolation",
                "command": ISOLATION_SCRIPT,
                "cwd": ".",
                "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp/executor-home"},
                "stdin": None,
                "timeout": 120,
                "max_output": 20_000,
            }
        )
        assert started["event"] == "started"
        assert result["exit_code"] == 0, result["stdout"] + result["stderr"]
        assert "ISOLATED" in result["stdout"]
        assert result["log_path"] == ".euesto-output/isolation.log"

        [probe] = _client(
            {"op": "probe", "request_id": "p", "names": ["git", "python3.13", "x"]}
        )
        assert probe["executables"] == ["git", "python3.13"]

        # A file written into the persistent work volume survives a full `down`/`up`.
        _client(
            {
                "op": "run",
                "request_id": "persist",
                "command": "echo kept > kept.txt",
                "cwd": ".",
                "env": {"PATH": "/usr/bin:/bin"},
                "stdin": None,
                "timeout": 30,
                "max_output": 1_000,
            }
        )
        _run([*COMPOSE, "--profile", "agent", "down"])
        _run([*COMPOSE, "--profile", "agent", "up", "--detach", "--wait", "runner"])
        _started, again = _client(
            {
                "op": "run",
                "request_id": "persisted",
                "command": "cat kept.txt",
                "cwd": ".",
                "env": {"PATH": "/usr/bin:/bin"},
                "stdin": None,
                "timeout": 30,
                "max_output": 1_000,
            }
        )
        assert again["stdout"].strip() == "kept"
    finally:
        _run([*COMPOSE, "--profile", "agent", "down", "--remove-orphans"], check=False)
        for kind in ("work", "state", "cache"):
            _run(["docker", "volume", "rm", f"euesto-{kind}-{WORKSPACE_KEY}"], check=False)


@pytest.mark.skipif(not _available(), reason="requires Docker and the container-tier fixtures")
@pytest.mark.timeout(1200)
def test_bash_through_the_executor_runs_in_the_runner() -> None:
    """The whole agent profile: the executor's bash tool reaches the runner (CI tier)."""
    request = {
        "request_id": "split-1",
        "run_id": "run",
        "tool": "bash",
        "mode": "agent",
        "arguments": {"command": ISOLATION_SCRIPT},
    }
    script = (
        "import json, os, sys, http.client, socket\n"
        "class C(http.client.HTTPConnection):\n"
        "    def connect(self):\n"
        "        self.sock = socket.socket(socket.AF_UNIX)\n"
        "        self.sock.connect('/run/ipc/executor.sock')\n"
        "token = open('/run/secrets/executor_token').read().strip()\n"
        "headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}\n"
        "c = C('localhost', timeout=300)\n"
        "c.request('GET', '/v1/status', headers=headers)\n"
        "status = json.loads(c.getresponse().read())\n"
        "c = C('localhost', timeout=300)\n"
        f"c.request('POST', '/v1/tools', body={json.dumps(json.dumps(request))}, "
        "headers=headers)\n"
        "print(json.dumps({'status': status, 'result': json.loads(c.getresponse().read())}))\n"
    )
    try:
        _run([*COMPOSE, "--profile", "agent", "up", "--detach", "--build", "--wait"])
        output = _run(
            [*COMPOSE, "--profile", "agent", "exec", "-T", "executor", "python", "-c", script]
        ).stdout
        reply = json.loads(output.strip().splitlines()[-1])
        environment = reply["status"]["environment"]
        assert environment["command_runner"] == "runner_container"
        assert environment["staging_lifetime"] == "persistent_until_discarded"
        assert "python3.13" in environment["developer_executables"]
        result = reply["result"]
        assert result["ok"], result
        assert "ISOLATED" in result["output"]
    finally:
        _run([*COMPOSE, "--profile", "agent", "down", "--remove-orphans"], check=False)
        for kind in ("work", "state", "cache"):
            _run(["docker", "volume", "rm", f"euesto-{kind}-{WORKSPACE_KEY}"], check=False)


TARGET_EXECUTABLES = {
    "runner-base": ["bash", "git", "rg", "fd", "jq", "make", "patch", "diff", "tree", "unzip",
                    "sqlite3", "ctags", "gcc", "cc", "ps", "python3"],
    "runner-python": ["python3.12", "python3.13", "pip", "uv"],
    "runner-node": ["node", "npm", "npx", "corepack", "pnpm", "yarn"],
    "runner-polyglot": ["python3.13", "uv", "node", "pnpm", "go", "gofmt", "cargo", "rustc",
                        "java", "javac"],
}  # fmt: skip


def _targets() -> list[str]:
    configured = os.environ.get("LOCAL_CHAT_RUNNER_TARGETS", "")
    return [item.strip() for item in configured.split(",") if item.strip()] or list(
        TARGET_EXECUTABLES
    )


def _build_target(target: str, tag: str) -> None:
    """Build one runner target, sharing the build cache across the four parametrized builds.

    ``runner-python`` and ``runner-node`` build on ``runner-base`` and ``runner-polyglot`` on
    ``runner-python``, so a shared cache is what keeps four builds inside one tier deadline.
    The containers job sets up a buildx builder with cache export and points
    ``EUSTO_BUILDX_CACHE_DIR`` at a directory; without it the plain builder and Docker's own
    layer cache are used, which is what a developer's machine has.
    """
    cache = os.environ.get("EUSTO_BUILDX_CACHE_DIR", "")
    arguments = ["docker", "buildx", "build", "--load"] if cache else ["docker", "build"]
    if cache:
        arguments += [
            "--cache-from",
            f"type=local,src={cache}",
            "--cache-to",
            f"type=local,dest={cache},mode=max",
        ]
    arguments += [
        "--file",
        str(ROOT / "docker" / "Dockerfile.runner"),
        "--target",
        target,
        "--tag",
        tag,
        str(ROOT),
    ]
    _run(arguments, timeout=RUNNER_BUILD_TIMEOUT_SECONDS)


@pytest.mark.skipif(not shutil.which("docker"), reason="requires Docker")
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("target", _targets())
def test_each_runner_target_provides_its_toolchain(target: str) -> None:
    tag = f"euesto-{target}:validation"
    _build_target(target, tag)
    expected = list(
        dict.fromkeys(TARGET_EXECUTABLES["runner-base"] + TARGET_EXECUTABLES[target])
    )
    probe = (
        "import json; from runner.server import probe; "
        f"print(json.dumps(probe({json.dumps(expected)})))"
    )
    found = json.loads(
        _run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python3", tag]
            + ["-E", "-s", "-c", probe],
            timeout=120,
        ).stdout
    )
    assert found == expected
