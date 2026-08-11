from __future__ import annotations

import logging
import re

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]+\b"),
)


def redact(value: object) -> str:
    text = str(value)
    text = SECRET_PATTERNS[0].sub(r"\1[REDACTED]", text)
    return SECRET_PATTERNS[1].sub("[REDACTED]", text)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Format with original argument types first; changing numeric args to strings breaks %d.
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
