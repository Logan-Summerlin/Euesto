from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutorToolError(Exception):
    """A stable, user-safe error returned by one executor tool call."""

    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s,;)]*")


def safe_message(value: object) -> str:
    """Keep diagnostics useful without disclosing executor filesystem paths."""
    message = str(value or "The executor rejected the request.")
    return _ABSOLUTE_PATH.sub("<workspace-path>", message)[:2_000]


def classify_error(exc: BaseException) -> ExecutorToolError:
    """Map legacy tool exceptions to the stable executor error vocabulary."""
    if isinstance(exc, ExecutorToolError):
        return exc
    if isinstance(exc, PermissionError):
        return ExecutorToolError("permission.denied", "The executor denied that operation.")
    if isinstance(exc, TimeoutError):
        return ExecutorToolError(
            "tool.timeout", "The operation exceeded its approved timeout.", retryable=True
        )
    if isinstance(exc, UnicodeError) or "UTF-8" in str(exc) or "utf-8" in str(exc):
        return ExecutorToolError("file.invalid_utf8", "The file is not valid UTF-8 text.")
    message = safe_message(exc)
    lowered = message.casefold()
    if "working directory" in lowered:
        return ExecutorToolError("working_directory.invalid", message)
    if "hash conflict" in lowered or "match conflict" in lowered:
        return ExecutorToolError("staging.conflict", message, retryable=True)
    if "shrink" in lowered:
        return ExecutorToolError("staging.shrink_warning", message)
    if "exceeds" in lowered or "too large" in lowered or "limit" in lowered:
        return ExecutorToolError("limit.exceeded", message)
    if "missing" in lowered or "does not exist" in lowered or "not found" in lowered:
        return ExecutorToolError("path.missing", message)
    if "not a file" in lowered or "regular" in lowered or "directory" in lowered:
        return ExecutorToolError("path.invalid_type", message)
    if isinstance(exc, OSError):
        return ExecutorToolError("io.internal", "The executor could not complete the file operation.", retryable=True)
    return ExecutorToolError("request.invalid_arguments", message)
