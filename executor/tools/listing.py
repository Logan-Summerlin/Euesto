"""Pagination cursors and listing lines shared by the read-only tools."""
from __future__ import annotations

import base64
import binascii
from pathlib import Path

from ..errors import INVALID_ARGUMENTS, ExecutorToolError

MAX_CURSOR_OFFSET = 100_000


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, offset)).encode()).decode().rstrip("=")


def decode_cursor(value: object, tool: str, *, maximum: int | None = MAX_CURSOR_OFFSET) -> int:
    """Decode an opaque cursor into an offset; ``maximum=None`` leaves it unbounded."""
    if not value:
        return 0
    try:
        padding = "=" * (-len(str(value)) % 4)
        offset = int(base64.urlsafe_b64decode(str(value) + padding).decode())
    except (ValueError, UnicodeError, binascii.Error):
        raise ExecutorToolError(INVALID_ARGUMENTS, f"Invalid {tool} result cursor") from None
    if offset < 0 or (maximum is not None and offset > maximum):
        raise ExecutorToolError(INVALID_ARGUMENTS, f"{tool.capitalize()} result cursor is outside the bounded pagination range")
    return offset


def requested_results(arguments: dict, default: int) -> int:
    requested = arguments.get("max_results", default)
    if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 2000:
        raise ExecutorToolError(INVALID_ARGUMENTS, "max_results must be an integer from 1 to 2000")
    return requested


def listing_line(root: Path, path: Path, details: bool) -> str:
    display = path.relative_to(root).as_posix()
    if not details:
        return display + ("/" if path.is_dir() else "")
    if path.is_dir():
        return f"directory\t-\t{display}"
    return f"file\t{path.stat().st_size}\t{display}"
