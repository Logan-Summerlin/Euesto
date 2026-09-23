from __future__ import annotations

import errno
import re
from dataclasses import dataclass
from typing import Any

# Stable tool error codes, documented in docs/TOOLS.md ("Error codes"). Every rejection the
# executor raises itself names its code at the throw site; a code never depends on message
# wording.
INVALID_ARGUMENTS = "request.invalid_arguments"
PATH_MISSING = "path.missing"
PATH_INVALID_TYPE = "path.invalid_type"
PATH_INVALID = "path.invalid"
PATH_UNSAFE = "path.unsafe"
LIMIT_EXCEEDED = "limit.exceeded"
INVALID_UTF8 = "file.invalid_utf8"
WORKING_DIRECTORY_INVALID = "working_directory.invalid"
COMMAND_INVALID_ARGUMENTS = "command.invalid_arguments"
STAGING_CONFLICT = "staging.conflict"
STAGING_SHRINK_WARNING = "staging.shrink_warning"
EDIT_NO_MATCH = "edit.no_match"
EDIT_TOO_MANY_MATCHES = "edit.too_many_matches"
EDIT_TOO_FEW_MATCHES = "edit.too_few_matches"
EDIT_MALFORMED_CONTEXT = "edit.malformed_context"
APPLY_PATCH_MALFORMED = "apply_patch.malformed"
CHECKPOINT_CORRUPT = "checkpoint.corrupt"
CHECKPOINT_NOT_FOUND = "checkpoint.not_found"
PERMISSION_DENIED = "permission.denied"
TOOL_TIMEOUT = "tool.timeout"
IO_INTERNAL = "io.internal"
TOOL_INTERNAL = "tool.internal"

ERROR_CODES = frozenset({
    INVALID_ARGUMENTS, PATH_MISSING, PATH_INVALID_TYPE, PATH_INVALID, PATH_UNSAFE, LIMIT_EXCEEDED,
    INVALID_UTF8, WORKING_DIRECTORY_INVALID, COMMAND_INVALID_ARGUMENTS, STAGING_CONFLICT,
    STAGING_SHRINK_WARNING, EDIT_NO_MATCH, EDIT_TOO_MANY_MATCHES, EDIT_TOO_FEW_MATCHES,
    EDIT_MALFORMED_CONTEXT, APPLY_PATCH_MALFORMED, CHECKPOINT_CORRUPT, CHECKPOINT_NOT_FOUND,
    PERMISSION_DENIED, TOOL_TIMEOUT, IO_INTERNAL, TOOL_INTERNAL,
})

_CAPACITY_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG})


@dataclass(frozen=True, slots=True)
class ExecutorToolError(ValueError):
    """A classified tool rejection raised at its throw site.

    ``code`` is chosen where the failure is detected and is never inferred from ``message``.
    ``details`` is bounded, JSON-serializable diagnostic data returned to the caller in the
    failed result's ``data`` (for example why an exact edit did not match).
    """

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\s,;)]*|/)[^\s,;)]*")


def safe_message(value: object) -> str:
    return _ABSOLUTE_PATH.sub("<workspace-path>", str(value or "The executor rejected the request."))[:2_000]


def classify_error(exc: BaseException) -> ExecutorToolError:
    """Classify an exception for a tool result.

    Executor rejections carry their own code. Everything else is classified by exception type
    (and ``errno``) only: operating-system, permission, timeout, and decoding failures, plus
    argument errors raised by the standard library while parsing model input. Message text is
    sanitized for display but never consulted.
    """
    if isinstance(exc, ExecutorToolError):
        return exc
    if isinstance(exc, PermissionError):
        return ExecutorToolError(PERMISSION_DENIED, "The executor denied that operation.")
    if isinstance(exc, TimeoutError):
        return ExecutorToolError(TOOL_TIMEOUT, "The operation exceeded its approved timeout.", retryable=True)
    if isinstance(exc, UnicodeError):
        return ExecutorToolError(INVALID_UTF8, "The file is not valid UTF-8 text.")
    if isinstance(exc, FileNotFoundError):
        return ExecutorToolError(PATH_MISSING, "The path does not exist.")
    if isinstance(exc, (IsADirectoryError, NotADirectoryError)):
        return ExecutorToolError(PATH_INVALID_TYPE, "The path is not the expected file or directory type.")
    if isinstance(exc, OSError):
        if exc.errno in _CAPACITY_ERRNOS:
            return ExecutorToolError(LIMIT_EXCEEDED, "The staging volume has no capacity left for this operation.")
        return ExecutorToolError(IO_INTERNAL, "The executor could not complete the operation.", retryable=True)
    if isinstance(exc, (ValueError, TypeError)):
        return ExecutorToolError(INVALID_ARGUMENTS, safe_message(exc))
    return ExecutorToolError(TOOL_INTERNAL, "The executor failed unexpectedly.")
