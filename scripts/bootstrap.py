#!/usr/bin/env python3
"""Install the locked developer toolchain for Euesto validation.

This intentionally installs into the active interpreter (normally ``.venv``), never into
 the user site, and refuses Python versions other than the supported 3.12 series.
"""
from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-dev.lock"


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        print(f"Python 3.12 is required (found {sys.version.split()[0]})", file=sys.stderr)
        return 2
    if not LOCK.is_file():
        print(f"missing lock file: {LOCK}", file=sys.stderr)
        return 2
    commands = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip"],
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(LOCK)],
    ]
    for command in commands:
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    print("Installed validation dependencies:")
    for requirement in ("PySide6", "pytest", "pytest-asyncio", "pytest-timeout", "ruff", "Pillow", "PyInstaller"):
        try:
            version = importlib.metadata.version(requirement)
        except importlib.metadata.PackageNotFoundError:
            version = "MISSING"
        print(f"  {requirement}=={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
