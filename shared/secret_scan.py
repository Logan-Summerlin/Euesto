"""High-confidence secret formats, shared by gateway redaction and publication review (4.5).

Only formats with a distinctive prefix or structure are matched, so ordinary hashes, UUIDs,
and base64 blobs are never flagged. The gateway replaces each match with
``[REDACTED:<kind>]`` before a tool result reaches the model; the desktop uses the same scan
to warn when a publication would write a secret into the workspace. Neither ever records the
matched value.
"""

from __future__ import annotations

import re

REDACTION_TEMPLATE = "[REDACTED:{kind}]"

# Ordered: the more specific key formats come before the generic ones they would overlap.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws_secret_key",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])aws_?secret_?access_?key[\"']?\s*[:=]\s*[\"']?"
            r"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
        ),
    ),
    ("gcp_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-])")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-])")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{32,}(?![A-Za-z0-9])")),
    (
        "openai_key",
        re.compile(
            r"\bsk-(?!ant-|or-)(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])"
            r"[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])"
        ),
    ),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])")),
    (
        "slack_webhook",
        re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}(?![A-Za-z0-9_\-])"
        ),
    ),
    ("azure_storage_key", re.compile(r"AccountKey=[A-Za-z0-9+/]{40,}={0,2}")),
)
SECRET_KINDS = tuple(kind for kind, _ in SECRET_PATTERNS)


def find_secrets(text: str) -> dict[str, int]:
    """Count the secrets of each kind in ``text`` without returning any value."""
    _, counts = redact_secrets(text)
    return counts


def redact_secrets(text: str) -> tuple[str, dict[str, int]]:
    """``text`` with every high-confidence secret replaced by ``[REDACTED:<kind>]``."""
    counts: dict[str, int] = {}
    if not text:
        return text, counts
    for kind, pattern in SECRET_PATTERNS:
        replacement = REDACTION_TEMPLATE.format(kind=kind)
        text, found = pattern.subn(replacement, text)
        if found:
            counts[kind] = counts.get(kind, 0) + found
    return text, counts
