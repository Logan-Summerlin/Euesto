from __future__ import annotations

import re
from pathlib import PurePosixPath


def optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def optional_nonnegative_int(value: object) -> int | None:
    number = optional_int(value)
    return None if number is None else max(0, number)


def optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def optional_string(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def relative_posix_path(value: object) -> str | None:
    """Canonical relative POSIX form of a workspace path (``"."`` for the root).

    Returns ``None`` for non-strings and for NUL-containing, absolute, drive-qualified, or
    parent-escaping paths. Backslashes count as separators so a path cannot be ambiguous on
    Windows. The executor's path validation stays authoritative; this only compares paths.
    """
    if not isinstance(value, str) or "\x00" in value:
        return None
    text = value.replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts) or "."
