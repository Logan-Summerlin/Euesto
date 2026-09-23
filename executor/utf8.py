"""Streaming UTF-8 text validation shared by ``read``, ``write``, and ``edit``."""
from __future__ import annotations

import codecs
from pathlib import Path

from .errors import INVALID_UTF8, ExecutorToolError

CHUNK_BYTES = 64 * 1024


class Utf8Validator:
    """Validate a byte stream chunk by chunk: no NUL bytes, and valid, complete UTF-8."""

    def __init__(self, message: str) -> None:
        self.message = message
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        if b"\x00" in chunk:
            raise ExecutorToolError(INVALID_UTF8, self.message)
        try:
            self._decoder.decode(chunk, final=final)
        except UnicodeDecodeError as exc:
            raise ExecutorToolError(INVALID_UTF8, self.message) from exc

    def finish(self) -> None:
        self.feed(b"", final=True)


def validate_utf8_file(path: Path, message: str) -> None:
    validator = Utf8Validator(message)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            validator.feed(chunk)
    validator.finish()
