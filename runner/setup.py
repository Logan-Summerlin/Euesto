"""The approved setup phase (harness plan 5.1; docs/EGRESS.md, "Setup phase").

The ``setup`` service in ``docker/compose.setup.yaml`` runs ``python3 -E -s -m runner.setup``
once per approved setup: it runs the workspace's dependency-install command (``uv sync``,
``npm ci``, ...) in the runner image, attached to the internal egress network whose only
route out is the allowlisted proxy. The agent loop's runner never joins that network unless
the session opted into registry access; dependencies persist in the work volume (``.venv``,
``node_modules``) and the cache volume, so later commands find them offline.

The command arrives in ``LOCAL_CHAT_SETUP_COMMAND`` (from the confirmed workspace policy or a
detected command the user confirmed) and runs non-interactively in ``/work`` with a fixed
environment: the command PATH, package-manager caches on ``/cache``, and the proxy variables.
Nothing else from the container environment reaches it. In isolated setup ``/work`` holds only
the dependency manifests and lockfiles plus the ``.venv`` and ``node_modules`` directories, so
install scripts never see the source tree while the network is open.

Standard library only, like the rest of the runner.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .server import COMMAND_PATH, cache_environment

WORK_ROOT = Path("/work")
CACHE_ROOT = Path("/cache")
MAX_COMMAND_CHARS = 1_000
MAX_SETUP_SECONDS = 1_800
DEFAULT_SETUP_SECONDS = 900
TIMEOUT_EXIT = 124
INVALID_EXIT = 2
_PROXY_URL = re.compile(
    r"^http://(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9.-]{0,252}):\d{1,5}$"
)


class SetupError(ValueError):
    """A setup request that cannot run; reported before anything starts."""


def setup_command(environ: Mapping[str, str]) -> tuple[str, int]:
    """The validated command and timeout (seconds) from the service environment."""
    command = environ.get("LOCAL_CHAT_SETUP_COMMAND", "").strip()
    if not command:
        raise SetupError("No setup command was given.")
    if len(command) > MAX_COMMAND_CHARS or "\x00" in command:
        raise SetupError(f"The setup command must be at most {MAX_COMMAND_CHARS} characters.")
    raw = environ.get("LOCAL_CHAT_SETUP_TIMEOUT", str(DEFAULT_SETUP_SECONDS)).strip()
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_SETUP_SECONDS:
        raise SetupError(f"The setup timeout must be 1 to {MAX_SETUP_SECONDS} seconds.")
    return command, int(raw)


def setup_environment(
    environ: Mapping[str, str], *, work_root: Path = WORK_ROOT, cache_root: Path = CACHE_ROOT
) -> dict[str, str]:
    """The fixed environment of a setup command; only the proxy address is taken from outside."""
    environment = {
        "PATH": COMMAND_PATH,
        "HOME": "/tmp/setup-home",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "CI": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "UV_PROJECT_ENVIRONMENT": os.fspath(work_root / ".venv"),
        "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        "npm_config_update_notifier": "false",
        **cache_environment(cache_root),
    }
    proxy = environ.get("LOCAL_CHAT_EGRESS_PROXY", "").strip()
    if proxy:
        if not _PROXY_URL.fullmatch(proxy):
            raise SetupError("LOCAL_CHAT_EGRESS_PROXY must be an http://host:port address.")
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            environment[name] = proxy
        environment.update(
            {
                "NO_PROXY": "",
                "no_proxy": "",
                "npm_config_https_proxy": proxy,
                "npm_config_proxy": proxy,
            }
        )
    return environment


def run_setup(
    command: str, timeout: int, environment: dict[str, str], *, work_root: Path = WORK_ROOT
) -> int:
    """Run the command in its own process group; its output streams to ours. Returns its exit
    code, or ``TIMEOUT_EXIT`` after killing the whole group at the deadline."""
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-o", "pipefail", "-c", command],
        cwd=work_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        print(f"setup: timed out after {timeout} seconds", flush=True)
        return TIMEOUT_EXIT


def main(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    try:
        command, timeout = setup_command(values)
        environment = setup_environment(values)
    except SetupError as exc:
        print(f"setup: {exc}", flush=True)
        return INVALID_EXIT
    print(f"setup: running in /work with a {timeout}-second limit", flush=True)
    code = run_setup(command, timeout, environment)
    print(f"setup: exit {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
