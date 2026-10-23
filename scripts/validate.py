#!/usr/bin/env python3
"""Credential-free validation harness with explicit, reportable tiers."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QML_FILES = ("qml/Main.qml", "qml/Sidebar.qml", "qml/Transcript.qml", "qml/Composer.qml")
PYTHON_PACKAGES = ("PySide6", "pytest", "pytest-timeout", "ruff")
EXECUTABLES = ("pyside6-qmllint", "docker", "docker compose", "pyinstaller")
# Hard ceilings per command. A hung command must fail its tier by name, not stall the whole
# validation step (the release job's Windows step once sat for hours with no output).
COMMAND_TIMEOUT_SECONDS = 1200
QML_SMOKE_TIMEOUT_SECONDS = 300
DOCKER_TIMEOUT_SECONDS = 3600
COMMAND_TIMEOUTS = {
    "docker": DOCKER_TIMEOUT_SECONDS,
    "qml-smoke": QML_SMOKE_TIMEOUT_SECONDS,
}
# After a deadline the command tree is killed and then awaited for at most this long; a process
# that refuses to die is reported as the timeout it already is instead of blocking the tier.
KILL_TIMEOUT_SECONDS = 30
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
# included because tests read them through tempfile and Qt.
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


def _safe_env(*, qt: bool = False, windows: bool | None = None) -> dict[str, str]:
    """Only pass validation variables; credentials and user configuration are excluded.

    ``qt`` adds the offscreen platform variables for the tiers whose child is itself a Qt
    program. Leaking them to everything else told unrelated Python children which Qt platform
    to use and reached the processes those children start. ``windows`` overrides the host
    platform for tests; it defaults to ``os.name == "nt"``.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    if qt:
        env.update(QT_ENV)
    on_windows = os.name == "nt" if windows is None else windows
    if on_windows:
        for var in WINDOWS_ENV:
            value = os.environ.get(var)
            if value:
                env[var] = value
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


def _version(
    command: list[str], runner: Callable[..., subprocess.CompletedProcess] = _run_probe
) -> tuple[str, str]:
    try:
        result = runner(command, timeout=8, env=_safe_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", type(exc).__name__
    output = (result.stdout or result.stderr or "").strip().splitlines()
    candidate = output[0][:120] if output else "unknown"
    version = candidate if re.search(r"\d+\.\d+", candidate) else "unknown"
    return version, "" if result.returncode == 0 else f"exit {result.returncode}"


def _record(
    name: str, status: str, version: str = "unknown", detail: str = ""
) -> dict[str, str]:
    return {"name": name, "status": status, "version": version, "detail": detail[:240]}


def preflight(
    tier: str = "all", runner: Callable[..., subprocess.CompletedProcess] = _run_probe
) -> dict[str, Any]:
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
        command = (
            ["docker", "compose", "version"]
            if name == "docker compose"
            else [executable, "--version"]
        )
        version, error = _version(command, runner)
        records.append(_record(name, "failed" if error else "available", version, error))
    for filename in QML_FILES:
        records.append(
            _record(
                filename,
                "available" if (ROOT / filename).is_file() else "failed",
                detail="required QML component",
            )
        )
    if shutil.which("docker"):
        version, error = _version(["docker", "info", "--format", "{{.ServerVersion}}"], runner)
        records.append(
            _record(
                "docker-daemon",
                "available" if not error else "failed",
                version,
                error or "reachable",
            )
        )
    else:
        records.append(
            _record("docker-daemon", "unavailable", detail="Docker executable unavailable")
        )
    supported = tier != "docker" or (
        platform.system() == "Linux" and shutil.which("docker") is not None
    )
    records.append(
        _record(
            "tier:" + tier,
            "available" if supported else "unavailable",
            detail="Docker validation requires Linux and Docker",
        )
    )
    return {
        "schema_version": 1,
        "tier": tier,
        "generated_at": int(time.time()),
        "checks": records,
    }


def commands(tier: str) -> list[tuple[str, list[str]]]:
    pytest = [sys.executable, "-m", "pytest"]
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


def _run_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess:
    """Run a validation command under a deadline that is never unbounded.

    The command gets no console window and no stdin, so it can neither prompt nor inherit an
    interactive console. On timeout its whole process tree is killed (``subprocess.run`` kills
    only the direct child, and on Windows a grandchild that inherits the step's output keeps
    the CI log open), then the direct child is reaped with its own bounded wait. A child that
    still refuses to exit is reported as the deadline failure it already is.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
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


def execute(
    tier: str,
    allow_unavailable: bool = False,
    report: dict[str, Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = _run_command,
) -> int:
    report = report or {"schema_version": 1, "tier": tier, "checks": []}
    worst = 0
    for name, command in commands(tier):
        print("+", " ".join(command), flush=True)
        timeout = COMMAND_TIMEOUTS.get(name, COMMAND_TIMEOUT_SECONDS)
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
    args = parser.parse_args(argv)
    report = (
        preflight(args.tier)
        if args.tier == "preflight"
        else {"schema_version": 1, "tier": args.tier, "checks": []}
    )
    code = 0 if args.tier == "preflight" else execute(args.tier, args.allow_unavailable, report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['status']:11} {check['name']}: {check.get('detail', '')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
