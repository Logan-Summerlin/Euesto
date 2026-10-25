#!/usr/bin/env python3
"""Credential-free validation harness with explicit, reportable tiers."""

from __future__ import annotations

import argparse
import atexit
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
QML_FILES = ("qml/Main.qml", "qml/Sidebar.qml", "qml/Transcript.qml", "qml/Composer.qml")
PYTHON_PACKAGES = ("PySide6", "pytest", "pytest-timeout", "ruff")
EXECUTABLES = ("pyside6-qmllint", "docker", "docker compose", "pyinstaller")
# Tools proven present by a ``PATH`` lookup plus the version of the distribution that ships
# them, instead of by launching them. ``pyside6-qmllint`` is the first Qt program a Windows job
# starts, so its launch pays for loading (and virus-scanning) the freshly installed Qt DLLs:
# that measures a cold start, not availability, and under a short probe deadline it reported a
# working linter as failed and turned the release gate red. The ``qml`` tier runs it for real.
DISTRIBUTION_PROBES = {"pyside6-qmllint": "PySide6"}
# The deadline for one version probe. Four tools plus the Docker daemon are the most a
# preflight probes, so this keeps the whole probe phase inside the 60 s preflight deadline
# while still covering a cold first launch; a probe that times out is not a failure verdict.
VERSION_PROBE_TIMEOUT_SECONDS = 10
# The deadline ladder, innermost first: pytest's per-test timeout (`pyproject.toml`, 180 s) <
# the harness deadline for the tier < the workflow step ceiling < the job ceiling. A deadline
# that cannot fire before GitHub kills the step reports nothing, so every tier has its own and
# every step ceiling leaves the harness at least a minute to kill, reap, and name the failure.
TIER_TIMEOUTS = {
    "preflight": 60,
    "collect": 180,
    "fast": 900,
    "slow": 1200,
    "ruff": 300,
    "compile": 300,
    "qml": 300,
    "qml-smoke": 300,
    "docker": 2700,
}
# Each tier's commands inherit that tier's deadline; `ruff-format` shares `ruff`'s entry.
COMMAND_TIMEOUTS = {
    "collect": TIER_TIMEOUTS["collect"],
    "fast": TIER_TIMEOUTS["fast"],
    "slow": TIER_TIMEOUTS["slow"],
    "docker": TIER_TIMEOUTS["docker"],
    "ruff": TIER_TIMEOUTS["ruff"],
    "ruff-format": TIER_TIMEOUTS["ruff"],
    "compile": TIER_TIMEOUTS["compile"],
    "qml": TIER_TIMEOUTS["qml"],
    "qml-smoke": TIER_TIMEOUTS["qml-smoke"],
}
DEFAULT_COMMAND_TIMEOUT_SECONDS = TIER_TIMEOUTS["compile"]
# The tiers `all` expands to, in the order `commands("all")` runs them.
ALL_TIERS = ("collect", "fast", "slow", "ruff", "compile", "qml", "qml-smoke", "docker")
# Tools a tier cannot run without. `preflight --require <tier>` fails when one is unavailable;
# without `--require` preflight only reports, so a developer without Docker can still see the
# rest of the environment.
TIER_REQUIREMENTS = {
    "preflight": ("python", "repository-root"),
    "collect": ("python", "repository-root", "pytest"),
    "fast": ("python", "repository-root", "pytest", "pytest-timeout"),
    "slow": ("python", "repository-root", "pytest", "pytest-timeout"),
    "ruff": ("python", "repository-root", "ruff"),
    "compile": ("python", "repository-root"),
    "qml": ("python", "repository-root", "pyside6-qmllint", *QML_FILES),
    "qml-smoke": ("python", "repository-root", "PySide6", *QML_FILES),
    "docker": (
        "python",
        "repository-root",
        "pytest",
        "docker",
        "docker compose",
        "docker-daemon",
        "tier:docker",
    ),
}
# After a deadline the command tree is killed and then awaited for at most this long; a process
# that refuses to die is reported as the timeout it already is instead of blocking the tier.
KILL_TIMEOUT_SECONDS = 30
# How long the timed-out command tree gets to exit after SIGTERM before SIGKILL goes to the
# whole group. The direct child is reaped by the caller afterwards, still under a bound.
KILL_GRACE_SECONDS = 5
# A running command prints a liveness line this often, so a log shows the tier is alive rather
# than indistinguishable from a wedge.
PROGRESS_INTERVAL_SECONDS = 30
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Tiers whose direct child is a Qt program. Only they receive the Qt platform variables: a
# plain Python child told to use the offscreen platform breaks, and the variable would be
# inherited by every process (and every QApplication decision) that child starts.
QT_TIERS = frozenset({"qml", "qml-smoke"})
QT_ENV = {
    "QT_QPA_PLATFORM": "offscreen",
    "QT_QUICK_BACKEND": "software",
    "QT_QUICK_CONTROLS_STYLE": "Fusion",
}
# OS-level variables a Windows child needs for DLL and service-provider resolution (including
# asyncio/WinSock startup), executable lookup, Path.home()/Path.expanduser(), and imports from
# an active virtual environment. User directories that only a signed-in desktop has are
# included because tests read them through tempfile and Qt; the three that hold user state are
# replaced with the disposable tree in `_safe_env`.
WINDOWS_ENV = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "ProgramFiles",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "VIRTUAL_ENV",
)
# Plain strings, not Path objects: ``pathlib`` picks its flavour from ``os.name``, which the
# tests below patch to describe a Windows child on a POSIX host.
# Variables that steer a tier without opening the environment: the disposable container-tier
# fixtures from `scripts/docker-fixtures.sh` (throwaway directories and a throwaway workspace
# key) and the shared runner-image build cache. Nothing here is a credential, and everything
# else in the host environment stays out of a validation child.
TIER_ENV = (
    "LOCAL_CHAT_CI_ROOT",
    "LOCAL_CHAT_SECRETS_DIR",
    "LOCAL_CHAT_WORKSPACE",
    "LOCAL_CHAT_WORKSPACE_ID",
    "LOCAL_CHAT_WORKSPACE_KEY",
    "LOCAL_CHAT_GATEWAY_PORT",
    "LOCAL_CHAT_RUNNER_TARGETS",
    "LOCAL_CHAT_RUNNER_CPUS",
    "LOCAL_CHAT_RUNNER_MEMORY",
    "LOCAL_CHAT_RUNNER_PIDS",
    "LOCAL_CHAT_SETUP_COMMAND",
    "EUSTO_BUILDX_CACHE_DIR",
)
_ISOLATED_HOME: str | None = None


def isolated_home() -> str:
    """A disposable home for validation children, created once per harness run.

    ``src.settings.app_data_dir()`` reads ``Path.home()``/``%LOCALAPPDATA%`` and the keyring
    reaches the host credential store, so a tier that inherits the runner's real home writes
    real state and is not isolated from the host or from another tier (and a containerised
    runner without a passwd entry has no home at all). One empty tree per run fixes all three.
    """
    global _ISOLATED_HOME
    if _ISOLATED_HOME is None:
        home = tempfile.mkdtemp(prefix="euesto-validation-home-")
        for name in ("data", "config", "cache", "localappdata", "appdata"):
            os.makedirs(os.path.join(home, name), exist_ok=True)
        atexit.register(shutil.rmtree, home, ignore_errors=True)
        _ISOLATED_HOME = home
    return _ISOLATED_HOME


def _safe_env(*, qt: bool = False, windows: bool | None = None) -> dict[str, str]:
    """Only pass validation variables; credentials and user configuration are excluded.

    ``qt`` adds the offscreen platform variables for the tiers whose child is itself a Qt
    program. Leaking them to everything else told unrelated Python children which Qt platform
    to use and reached the processes those children start. ``windows`` overrides the host
    platform for tests; it defaults to ``os.name == "nt"``.
    """
    home = isolated_home()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        # The null keyring never prompts and never reaches a host credential store.
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "HOME": home,
        "XDG_DATA_HOME": os.path.join(home, "data"),
        "XDG_CONFIG_HOME": os.path.join(home, "config"),
        "XDG_CACHE_HOME": os.path.join(home, "cache"),
    }
    for name in TIER_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    if qt:
        env.update(QT_ENV)
    on_windows = os.name == "nt" if windows is None else windows
    if on_windows:
        for var in WINDOWS_ENV:
            value = os.environ.get(var)
            if value:
                env[var] = value
        # A Windows child reads these instead of HOME; the real ones hold the signed-in user's
        # application data, which a tier must never write to.
        env["USERPROFILE"] = home
        env["LOCALAPPDATA"] = os.path.join(home, "localappdata")
        env["APPDATA"] = os.path.join(home, "appdata")
    return env


def _run_probe(
    command: list[str], *, timeout: float, env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Run a version probe without pipes.

    A timed-out probe is killed, but on Windows a grandchild (console-script launchers,
    the Docker CLI) can keep an inherited pipe open, and reading that pipe would then block
    forever. Output goes to a temporary file instead, so only the direct child is awaited.
    """
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        output.seek(0)
        text = output.read().decode("utf-8", "replace")
    return subprocess.CompletedProcess(command, completed.returncode, stdout=text, stderr="")


class _Version(NamedTuple):
    """A tool's version, why it could not be read, and whether the probe ran out of time."""

    version: str
    detail: str
    timed_out: bool


def _version(
    command: list[str], runner: Callable[..., subprocess.CompletedProcess] = _run_probe
) -> _Version:
    """Report a tool's version without turning a slow cold start into a broken tool.

    The deadline is small enough that every probe fits the 60 s preflight deadline and large
    enough for a freshly installed console script on a cold Windows runner to answer: its
    first launch loads, and the host's virus scanner inspects, the package it ships with.
    """
    try:
        result = runner(command, timeout=VERSION_PROBE_TIMEOUT_SECONDS, env=_safe_env())
    except subprocess.TimeoutExpired:
        return _Version(
            "unknown", f"timed out after {VERSION_PROBE_TIMEOUT_SECONDS}s", timed_out=True
        )
    except OSError as exc:
        return _Version("unknown", type(exc).__name__, timed_out=False)
    output = (result.stdout or result.stderr or "").strip().splitlines()
    candidate = output[0][:120] if output else "unknown"
    version = candidate if re.search(r"\d+\.\d+", candidate) else "unknown"
    detail = "" if result.returncode == 0 else f"exit {result.returncode}"
    return _Version(version, detail, timed_out=False)


def _package_version(name: str) -> str:
    """The installed distribution's version, or ``unknown`` when its metadata is absent."""
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "unknown"


def _record(
    name: str, status: str, version: str = "unknown", detail: str = ""
) -> dict[str, str]:
    return {"name": name, "status": status, "version": version, "detail": detail[:240]}


def required_tiers(values: Iterable[str]) -> list[str]:
    """Expand ``--require`` values (comma-separated, repeatable) into concrete tier names."""
    tiers: list[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if not name:
                continue
            tiers.extend(ALL_TIERS if name == "all" else (name,))
    return list(dict.fromkeys(tiers))


def missing_requirements(report: dict[str, Any], tiers: Iterable[str]) -> list[dict[str, str]]:
    """Records a required tier cannot run without that are not reported as available."""
    required = {name for tier in tiers for name in TIER_REQUIREMENTS.get(tier, ())}
    return [
        check
        for check in report.get("checks", [])
        if check["name"] in required and check["status"] != "available"
    ]


def preflight(
    *tiers: str, runner: Callable[..., subprocess.CompletedProcess] = _run_probe
) -> dict[str, Any]:
    """Report every validation tool in bounded records, and whether each tier can run here."""
    requested = tiers or ("all",)
    records = [
        _record("python", "available", platform.python_version(), sys.executable),
        _record(
            "repository-root",
            "available" if Path.cwd().resolve() == ROOT else "failed",
            detail=str(ROOT),
        ),
    ]
    if sys.version_info[:2] != (3, 12):
        records[0].update(status="failed", detail="Python 3.12 is required")
    for package in PYTHON_PACKAGES:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            records.append(
                _record(package, "unavailable", detail="install requirements-dev.lock")
            )
        except Exception as exc:
            records.append(_record(package, "failed", detail=type(exc).__name__))
        else:
            records.append(_record(package, "available", version))
    for name in EXECUTABLES:
        executable = "docker" if name == "docker compose" else name
        if not shutil.which(executable):
            records.append(_record(name, "unavailable", detail="executable not found"))
            continue
        distribution = DISTRIBUTION_PROBES.get(name)
        if distribution:
            records.append(
                _record(
                    name,
                    "available",
                    _package_version(distribution),
                    f"ships with {distribution}",
                )
            )
            continue
        command = (
            ["docker", "compose", "version"]
            if name == "docker compose"
            else [executable, "--version"]
        )
        probe = _version(command, runner)
        # Found on ``PATH`` and failed to answer are different facts: a probe that timed out
        # means the tool was slow to start, and the tier that runs it is the authority on
        # whether it works. Reporting ``failed`` here made the release gate depend on the
        # temperature of a Windows runner's virus scanner.
        records.append(
            _record(
                name,
                "available" if probe.timed_out or not probe.detail else "failed",
                probe.version,
                f"version probe {probe.detail}" if probe.timed_out else probe.detail,
            )
        )
    for filename in QML_FILES:
        records.append(
            _record(
                filename,
                "available" if (ROOT / filename).is_file() else "failed",
                detail="required QML component",
            )
        )
    if shutil.which("docker"):
        command = ["docker", "info", "--format", "{{.ServerVersion}}"]
        probe = _version(command, runner)
        # Unlike a tool's version probe, this one is a reachability verdict: the docker tier
        # cannot run when the daemon does not answer, including when it does not answer in time.
        records.append(
            _record(
                "docker-daemon",
                "available" if not probe.detail else "failed",
                probe.version,
                probe.detail or "reachable",
            )
        )
    else:
        records.append(
            _record("docker-daemon", "unavailable", detail="Docker executable unavailable")
        )
    for name in requested:
        supported = name != "docker" or (
            platform.system() == "Linux" and shutil.which("docker") is not None
        )
        detail = "Docker validation requires Linux and Docker" if name == "docker" else ""
        records.append(
            _record("tier:" + name, "available" if supported else "unavailable", detail)
        )
    return {
        "schema_version": 1,
        "tier": ",".join(requested),
        "generated_at": int(time.time()),
        "checks": records,
    }


def commands(tier: str) -> list[tuple[str, list[str]]]:
    pytest = [sys.executable, "-m", "pytest"]
    if tier == "collect":
        # Collection imports every test module, so an import-time hang is named in seconds
        # instead of at the end of a tier (the collector also prints progress as it goes).
        return [("collect", pytest + ["--collect-only", "-q"])]
    if tier == "fast":
        return [("fast", pytest + ["-m", "not slow and not docker"])]
    if tier == "slow":
        return [("slow", pytest + ["-m", "slow and not docker"])]
    if tier == "docker":
        return [("docker", pytest + ["-m", "docker"])]
    if tier == "qml":
        return [("qml", ["pyside6-qmllint", *QML_FILES])]
    if tier == "qml-smoke":
        return [("qml-smoke", [sys.executable, "scripts/qml_smoke.py"])]
    if tier == "ruff":
        ruff = [sys.executable, "-m", "ruff"]
        return [
            ("ruff", [*ruff, "check", "."]),
            ("ruff-format", [*ruff, "format", "--check", "."]),
        ]
    if tier == "compile":
        return [
            (
                "compile",
                [
                    sys.executable,
                    "-m",
                    "compileall",
                    "-q",
                    "app.py",
                    "src",
                    "server",
                    "shared",
                    "executor",
                    "egress",
                    "tests",
                    "scripts",
                ],
            )
        ]
    if tier == "all":
        return [
            *commands("fast"),
            *commands("slow"),
            *commands("ruff"),
            *commands("compile"),
            *commands("qml"),
            *commands("qml-smoke"),
            *commands("docker"),
        ]
    raise ValueError(f"unknown validation tier: {tier}")


class _Progress:
    """Print a liveness line while a command runs, so a log shows it is still alive.

    A tier that wedges prints nothing at all without this, which is the difference between
    "still working" and "stuck" in a CI log.
    """

    def __init__(self, label: str, timeout: float, interval: float | None = None) -> None:
        self.label = label
        self.timeout = timeout
        self.interval = PROGRESS_INTERVAL_SECONDS if interval is None else interval
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._report, name="validation-progress", daemon=True
        )

    def _report(self) -> None:
        started = time.monotonic()
        while not self.stop.wait(self.interval):
            elapsed = int(time.monotonic() - started)
            print(
                f"[progress] {self.label} still running after {elapsed}s "
                f"(deadline {int(self.timeout)}s)",
                flush=True,
            )

    def __enter__(self) -> _Progress:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=PROGRESS_INTERVAL_SECONDS)


def _label(command: list[str]) -> str:
    label = " ".join(command)
    return label if len(label) <= 120 else label[:117] + "..."


def _signal_group(process: subprocess.Popen, signum: int) -> bool:
    """Signal the command's process group; report whether the group still exists."""
    try:
        os.killpg(os.getpgid(process.pid), signum)
    except (AttributeError, OSError):
        return False
    return True


def _run_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Run a validation command under a deadline that is never unbounded.

    The command gets no console window and no stdin, so it can neither prompt nor inherit an
    interactive console. On POSIX it also gets its own session (``start_new_session``), which
    is the handle a deadline needs: without it the child shares the step's process group, so
    there is nothing to signal but the direct child, and the runner servers and ``bash -lc``
    grandchildren a test starts survive the kill and keep the step's output open.

    A progress line is printed every ``PROGRESS_INTERVAL_SECONDS`` while the command runs. On
    timeout the whole tree is killed, then the direct child is reaped with its own bounded
    wait; a child that still refuses to exit is reported as the deadline failure it already is.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        start_new_session=os.name != "nt",
    )
    try:
        with _Progress(_label(command), timeout):
            code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            process.wait(timeout=KILL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(command, code)


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill a command and its descendants; the caller reaps the direct child afterwards."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    # POSIX: the child owns its own session (``start_new_session``), so the group is signalled
    # rather than the direct child, which would leave its descendants running.
    if not _signal_group(process, signal.SIGTERM):
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, signal.SIGKILL)


# The docker tier reads these to find its disposable fixtures. Without them every container
# check skips itself, which is a green tier that ran nothing, so the tier fails by name instead.
DOCKER_FIXTURES = ("LOCAL_CHAT_SECRETS_DIR", "LOCAL_CHAT_WORKSPACE")


def _missing_fixtures(tier: str) -> str:
    if tier != "docker":
        return ""
    missing = [name for name in DOCKER_FIXTURES if not os.environ.get(name)]
    if not missing:
        return ""
    return (
        "fixtures missing ("
        + ", ".join(missing)
        + "): source scripts/docker-fixtures.sh and call setup_fixtures"
    )


def execute(
    tier: str,
    allow_unavailable: bool = False,
    report: dict[str, Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = _run_command,
) -> int:
    report = report or {"schema_version": 1, "tier": tier, "checks": []}
    missing = _missing_fixtures(tier)
    if missing:
        report.setdefault("checks", []).append(
            _record("docker-fixtures", "unavailable", detail=missing)
        )
        return 0 if allow_unavailable else 1
    worst = 0
    for name, command in commands(tier):
        print("+", " ".join(command), flush=True)
        timeout = COMMAND_TIMEOUTS.get(name, DEFAULT_COMMAND_TIMEOUT_SECONDS)
        try:
            result = runner(
                command,
                cwd=ROOT,
                env=_safe_env(qt=name in QT_TIERS),
                timeout=timeout,
            )
            code = result.returncode
        except (FileNotFoundError, OSError) as exc:
            code, detail = 127, f"unavailable: {type(exc).__name__}"
        except subprocess.TimeoutExpired:
            code, detail = 124, f"failed: timed out after {timeout}s"
        else:
            detail = (
                "passed" if code == 0 else ("empty test tier" if code == 5 else f"exit {code}")
            )
        status = (
            "passed"
            if code == 0
            else ("empty" if code == 5 else ("unavailable" if code == 127 else "failed"))
        )
        report.setdefault("checks", []).append(_record(name, status, detail=detail))
        if status != "passed" and not (
            allow_unavailable and status in ("unavailable", "empty")
        ):
            worst = worst or (1 if status in ("unavailable", "empty") else code or 1)
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tier",
        choices=(
            "preflight",
            "collect",
            "fast",
            "slow",
            "docker",
            "qml",
            "qml-smoke",
            "ruff",
            "compile",
            "all",
        ),
    )
    parser.add_argument("--json", metavar="PATH")
    parser.add_argument("--allow-unavailable", action="store_true")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="TIER",
        help=(
            "comma-separated tiers whose tools must all be available; preflight exits 1 when "
            "one is not"
        ),
    )
    args = parser.parse_args(argv)
    required = required_tiers(args.require)
    unknown = [name for name in required if name not in TIER_REQUIREMENTS]
    if unknown:
        print(f"unknown tier for --require: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if args.tier != "preflight":
        if required:
            print("--require applies to the preflight tier only", file=sys.stderr)
            return 2
        report = {"schema_version": 1, "tier": args.tier, "checks": []}
        code = execute(args.tier, args.allow_unavailable, report)
    else:
        report = preflight(*(required or ("all",)))
        missing = missing_requirements(report, required)
        code = 1 if missing else 0
        for check in missing:
            print(
                f"required tool {check['name']} is {check['status']}: "
                f"{check.get('detail', '')}",
                file=sys.stderr,
            )
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['status']:11} {check['name']}: {check.get('detail', '')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
