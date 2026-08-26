from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath

RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
SECRET_PARTS = frozenset({".env", ".aws", ".azure", ".ssh", ".gnupg", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"})
STAGING_EXCLUDED_PARTS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
})


class UnsafePath(ValueError):
    pass


def is_secret_path(value: str) -> bool:
    return any(part.casefold() in SECRET_PARTS or part.casefold().startswith(".env") for part in PurePosixPath(value).parts)


def is_staging_excluded(value: str) -> bool:
    return any(part.casefold() in STAGING_EXCLUDED_PARTS for part in PurePosixPath(value).parts)


def is_tool_excluded(value: str) -> bool:
    """Return whether read-oriented tools must omit this path."""
    parts = PurePosixPath(value).parts
    return is_secret_path(value) or is_staging_excluded(value) or any(part.startswith(".local-chat-") for part in parts)


def normalize_relative(value: str, *, allow_secret: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise UnsafePath("Path must be text without NUL bytes")
    if not value:
        return "."
    if "\\" in value or value.startswith(("/", "\\", "//")) or re.match(r"^[A-Za-z]:", value):
        raise UnsafePath("Absolute, drive, and UNC paths are forbidden")
    path = PurePosixPath(value)
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafePath("Traversal and ambiguous path segments are forbidden")
    normalized_parts: list[str] = []
    for part in parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise UnsafePath("Windows aliases and alternate streams are forbidden")
        base = part.split(".", 1)[0].upper()
        if base in RESERVED:
            raise UnsafePath("Reserved DOS names are forbidden")
        canonical = unicodedata.normalize("NFC", part)
        if canonical != part:
            raise UnsafePath("Non-canonical Unicode path is forbidden")
        if not allow_secret and is_secret_path(part):
            raise UnsafePath("Secret-like paths are blocked")
        normalized_parts.append(part)
    return "/".join(normalized_parts)


def safe_path(root: Path, relative: str, *, must_exist: bool = True, allow_secret: bool = False) -> Path:
    normalized = normalize_relative(relative, allow_secret=allow_secret)
    candidate = root if normalized == "." else root.joinpath(*normalized.split("/"))
    root_real = root.resolve(strict=True)
    if must_exist:
        _reject_links(root, normalized)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root_real):
            raise UnsafePath("Path escaped the workspace")
    else:
        parent = candidate.parent
        if parent.exists():
            _reject_links(root, str(PurePosixPath(normalized).parent))
            if not parent.resolve(strict=True).is_relative_to(root_real):
                raise UnsafePath("Path parent escaped the workspace")
    return candidate


def _reject_links(root: Path, relative: str) -> None:
    current = root
    if relative == ".":
        return
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise UnsafePath("Symbolic links are forbidden")
        if hasattr(os.stat_result, "st_file_attributes"):
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
            if attributes & 0x400:
                raise UnsafePath("Windows reparse points are forbidden")


def assert_unique_paths(paths: list[str]) -> None:
    aliases: set[str] = set()
    for value in paths:
        normalized = normalize_relative(value)
        key = unicodedata.normalize("NFC", normalized).casefold()
        if key in aliases:
            raise UnsafePath("Case or Unicode path collision")
        aliases.add(key)
