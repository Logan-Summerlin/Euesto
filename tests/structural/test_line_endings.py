"""Linux executes these scripts, so they must be LF: a CRLF copy cannot start.

A shell script with CRLF line endings is not a style problem. The kernel takes everything after
``#!`` up to the newline as the interpreter path, so a CRLF ``#!/bin/sh`` makes it look for an
interpreter named ``sh`` plus a carriage return: the container exits before its first command,
and ``docker compose up --wait`` reports a profile that never becomes healthy. A script that a
workflow sources under ``bash -e`` fails earlier still, at a ``set`` builtin whose last word
ends in a carriage return, before it creates the fixtures the container tier needs. Neither
failure names the line endings — a dead container, an empty step — so this file checks the
bytes instead of trusting a review to notice them. ``.gitattributes`` checks ``*.sh`` out with
``eol=lf``; this test covers a checkout that ignored it (docs/TESTING.md, "Line endings").
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_SUFFIXES = {".sh", ".bash"}
SHELL_NAMES = {"sh", "bash", "dash", "ash", "ksh", "zsh"}
# Directory names that hold tool output or build trees (.gitignore) rather than repository
# content: a dependency's own scripts are not the checkout's to fix.
SKIPPED_DIRECTORIES = {
    ".git",
    ".local-chat-checkpoints",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "validation-logs",
    "venv",
}
SKIPPED_PREFIXES = ("euesto-validation-home-",)


def _repository_files() -> list[Path]:
    files: list[Path] = []
    for root, directories, names in os.walk(ROOT):
        directories[:] = [
            name
            for name in directories
            if name not in SKIPPED_DIRECTORIES and not name.startswith(SKIPPED_PREFIXES)
        ]
        files.extend(Path(root) / name for name in names)
    return files


def _shebang_interpreter(path: Path) -> str | None:
    """The program a shebang names, ignoring a leading ``/usr/bin/env``."""
    try:
        first_line = path.read_bytes().split(b"\n", 1)[0]
    except OSError:
        return None
    if not first_line.startswith(b"#!"):
        return None
    words = first_line[2:].decode("ascii", "replace").replace("\r", " ").split()
    if len(words) > 1 and Path(words[0]).name == "env":
        words = words[1:]
    return Path(words[0]).name if words else None


def _runs_under_linux(path: Path) -> bool:
    """Whether Linux executes this file as a script, where a CRLF copy is fatal."""
    if path.suffix.lower() in SCRIPT_SUFFIXES:
        return True
    return _shebang_interpreter(path) in SHELL_NAMES


def test_every_script_that_runs_under_linux_uses_lf_line_endings() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _repository_files()
        if _runs_under_linux(path) and b"\r" in path.read_bytes()
    ]
    assert not offenders, f"Linux cannot execute or source a CRLF script: {offenders}"
