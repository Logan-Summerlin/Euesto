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
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
QML_FILES = ("qml/Main.qml", "qml/Sidebar.qml", "qml/Transcript.qml", "qml/Composer.qml")
PYTHON_PACKAGES = ("PySide6", "pytest", "pytest-asyncio", "pytest-timeout", "ruff", "Pillow")
EXECUTABLES = ("pyside6-qmllint", "docker", "docker compose", "pyinstaller")


def _safe_env() -> dict[str, str]:
    """Only pass validation variables; credentials and user configuration are excluded."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
    }
    if os.name == "nt":
        # Windows subprocesses need these OS-level variables for DLL and
        # service-provider resolution (including asyncio/WinSock startup).
        for var in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP"):
            value = os.environ.get(var)
            if value:
                env[var] = value
    return env


def _version(command: list[str], runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> tuple[str, str]:
    try:
        result = runner(command, capture_output=True, text=True, timeout=8, env=_safe_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", type(exc).__name__
    output = (result.stdout or result.stderr or "").strip().splitlines()
    candidate = output[0][:120] if output else "unknown"
    version = candidate if re.search(r"\d+\.\d+", candidate) else "unknown"
    return version, "" if result.returncode == 0 else f"exit {result.returncode}"


def _record(name: str, status: str, version: str = "unknown", detail: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "version": version, "detail": detail[:240]}


def preflight(tier: str = "all", runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    records = [_record("python", "available", platform.python_version(), sys.executable),
               _record("repository-root", "available" if Path.cwd().resolve() == ROOT else "failed", detail=str(ROOT))]
    if sys.version_info[:2] != (3, 12):
        records[0].update(status="failed", detail="Python 3.12 is required")
    for package in PYTHON_PACKAGES:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            records.append(_record(package, "unavailable", detail="install requirements-dev.lock"))
        except Exception as exc:
            records.append(_record(package, "failed", detail=type(exc).__name__))
        else:
            records.append(_record(package, "available", version))
    for name in EXECUTABLES:
        executable = "docker" if name == "docker compose" else name
        if not shutil.which(executable):
            records.append(_record(name, "unavailable", detail="executable not found"))
            continue
        command = ["docker", "compose", "version"] if name == "docker compose" else [executable, "--version"]
        version, error = _version(command, runner)
        records.append(_record(name, "failed" if error else "available", version, error))
    for filename in QML_FILES:
        records.append(_record(filename, "available" if (ROOT / filename).is_file() else "failed",
                                detail="required QML component"))
    if shutil.which("docker"):
        version, error = _version(["docker", "info", "--format", "{{.ServerVersion}}"], runner)
        records.append(_record("docker-daemon", "available" if not error else "failed", version, error or "reachable"))
    else:
        records.append(_record("docker-daemon", "unavailable", detail="Docker executable unavailable"))
    supported = tier != "docker" or (platform.system() == "Linux" and shutil.which("docker") is not None)
    records.append(_record("tier:" + tier, "available" if supported else "unavailable",
                           detail="Docker validation requires Linux and Docker"))
    return {"schema_version": 1, "tier": tier, "generated_at": int(time.time()), "checks": records}


def commands(tier: str) -> list[tuple[str, list[str]]]:
    pytest = [sys.executable, "-m", "pytest"]
    if tier == "fast": return [("fast", pytest + ["-m", "not slow and not docker"])]
    if tier == "slow": return [("slow", pytest + ["-m", "slow and not docker"])]
    if tier == "docker": return [("docker", pytest + ["-m", "docker"])]
    if tier == "qml": return [("qml", ["pyside6-qmllint", *QML_FILES])]
    if tier == "ruff": return [("ruff", [sys.executable, "-m", "ruff", "check", "."])]
    if tier == "compile": return [("compile", [sys.executable, "-m", "compileall", "-q", "app.py", "src", "server", "shared", "executor", "tests", "scripts"])]
    if tier == "all": return [*commands("fast"), *commands("slow"), *commands("ruff"), *commands("compile"), *commands("qml"), *commands("docker")]
    raise ValueError(f"unknown validation tier: {tier}")


def execute(tier: str, allow_unavailable: bool = False, report: dict[str, Any] | None = None,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    report = report or {"schema_version": 1, "tier": tier, "checks": []}
    worst = 0
    for name, command in commands(tier):
        print("+", " ".join(command), flush=True)
        try:
            result = runner(command, cwd=ROOT, env=_safe_env())
            code = result.returncode
        except (FileNotFoundError, OSError) as exc:
            code, detail = 127, f"unavailable: {type(exc).__name__}"
        except subprocess.TimeoutExpired:
            code, detail = 124, "failed: timed out"
        else:
            detail = "passed" if code == 0 else ("empty test tier" if code == 5 else f"exit {code}")
        status = "passed" if code == 0 else ("empty" if code == 5 else ("unavailable" if code == 127 else "failed"))
        report.setdefault("checks", []).append(_record(name, status, detail=detail))
        if status != "passed" and not (allow_unavailable and status in ("unavailable", "empty")):
            worst = worst or (1 if status in ("unavailable", "empty") else code or 1)
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=("preflight", "fast", "slow", "docker", "qml", "ruff", "compile", "all"))
    parser.add_argument("--json", metavar="PATH")
    parser.add_argument("--allow-unavailable", action="store_true")
    args = parser.parse_args(argv)
    report = preflight(args.tier) if args.tier == "preflight" else {"schema_version": 1, "tier": args.tier, "checks": []}
    code = 0 if args.tier == "preflight" else execute(args.tier, args.allow_unavailable, report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['status']:11} {check['name']}: {check.get('detail', '')}")
    return code
