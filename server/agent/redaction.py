"""Secret redaction for provider-bound tool results (harness plan 4.5).

Every model-facing tool result passes through :class:`Redactor` before it enters the history,
so a secret never reaches the provider and redaction never rewrites an already-cached message.
Matches become ``[REDACTED:<kind>]`` and the runtime journals ``secret.redacted`` with the kinds
and counts only. A workspace may let specific files through unredacted: that is a per-file user
decision stored in the workspace configuration (``unredacted_paths``), and it applies only to
``read`` results of exactly those paths.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from shared.coercion import relative_posix_path
from shared.secret_scan import redact_secrets

MAX_UNREDACTED_PATHS = 50


class Redactor:
    def __init__(self, unredacted_paths: Iterable[object] = ()):
        exempt: set[str] = set()
        for item in list(unredacted_paths or ())[:MAX_UNREDACTED_PATHS]:
            path = relative_posix_path(str(item)) if isinstance(item, str) else None
            if path:
                exempt.add(path.casefold())
        self.exempt = frozenset(exempt)

    @classmethod
    def from_workspace_config(cls, config: dict[str, Any]) -> Redactor:
        paths = config.get("unredacted_paths") if isinstance(config, dict) else None
        return cls(paths if isinstance(paths, list) else ())

    def redact(
        self, tool: str, arguments: dict[str, Any], text: str
    ) -> tuple[str, dict[str, int]]:
        """``text`` with secrets replaced, and the count of each kind replaced."""
        if tool == "read" and self.exempt:
            path = relative_posix_path(str(arguments.get("path") or ""))
            if path and path.casefold() in self.exempt:
                return text, {}
        return redact_secrets(text)
