"""Framework-neutral structures for the ``investigate_repository`` tool.

The parent agent may pass ``inspected_paths`` (files or directories whose contents it already
has) so the investigator does not re-walk them, and receives structured ``findings`` it can
check individually instead of trusting one prose summary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

MAX_INSPECTED_PATHS = 50
MAX_INSPECTED_PATH_CHARS = 512
MAX_FINDINGS = 50
MAX_FINDING_JUSTIFICATION_CHARS = 1_000
MAX_SUMMARY_CHARS = 32_000
MAX_FILES_EXAMINED = 200

# Plan tools whose call on an already-inspected path would only repeat what the parent has.
REINSPECTION_TOOLS = frozenset({"read", "ls"})

REPORT_FORMAT_INSTRUCTIONS = (
    "Finish with a single JSON object and nothing after it, in this shape: "
    '{"summary": "<concise factual answer>", "findings": [{"file": "<relative/path>", '
    '"line": <line number or null>, "justification": "<what this location shows and why it matters>"}]}. '
    "List only findings you can point to in a file; the parent may verify each one with a single read."
)


def normalize_investigation_path(value: object) -> str | None:
    """Canonical relative POSIX form of a workspace path, or ``None`` if it is not one."""
    if not isinstance(value, str) or "\x00" in value:
        return None
    text = value.strip().replace("\\", "/")
    if not text or len(text) > MAX_INSPECTED_PATH_CHARS or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts) or "."


def parse_inspected_paths(value: object) -> tuple[str, ...]:
    """Validate the optional ``inspected_paths`` argument; raises ``ValueError`` when malformed."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("inspected_paths must be an array of relative paths")
    if len(value) > MAX_INSPECTED_PATHS:
        raise ValueError(f"inspected_paths accepts at most {MAX_INSPECTED_PATHS} paths")
    paths: list[str] = []
    for item in value:
        normalized = normalize_investigation_path(item)
        if normalized is None:
            raise ValueError("inspected_paths entries must be relative workspace paths")
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


def reinspection_target(tool: str, arguments: dict[str, Any], inspected: tuple[str, ...] | frozenset[str]) -> str | None:
    """The inspected path a Plan-tool call would re-read, or ``None`` when the call is new work."""
    if tool not in REINSPECTION_TOOLS or not inspected:
        return None
    raw = arguments.get("path", "." if tool == "ls" else None)
    normalized = normalize_investigation_path(raw)
    return normalized if normalized is not None and normalized in inspected else None


@dataclass(frozen=True, slots=True)
class InvestigationFinding:
    file: str
    line: int | None
    justification: str
    # True when the investigator itself read (or matched with grep) this file during the call.
    observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "justification": self.justification, "observed": self.observed}


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    summary: str
    findings: tuple[InvestigationFinding, ...] = ()
    files_examined: tuple[str, ...] = ()
    skipped_paths: tuple[str, ...] = ()
    structured: bool = False
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary[:MAX_SUMMARY_CHARS],
            "findings": [item.to_dict() for item in self.findings[:MAX_FINDINGS]],
            "structured": self.structured,
            "files_examined": list(self.files_examined[:MAX_FILES_EXAMINED]),
            "skipped_paths": list(self.skipped_paths),
            "truncated": self.truncated,
            **self.extra,
        }


def parse_investigation_report(text: str, observed_files: set[str] | frozenset[str] = frozenset()) -> tuple[str, tuple[InvestigationFinding, ...], bool]:
    """Split the investigator's final message into ``(summary, findings, structured)``.

    ``structured`` is true only when the message contained a JSON report with a ``findings``
    array. Otherwise the whole message becomes the summary and ``findings`` is empty, so a
    model that ignores the format still returns its prose rather than failing the call.
    """
    raw = str(text or "")
    report, remainder = _extract_report(raw)
    if report is None:
        return raw.strip(), (), False
    summary = report.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) and summary.strip() else remainder.strip()
    findings: list[InvestigationFinding] = []
    for item in report["findings"]:
        finding = _finding(item, observed_files)
        if finding is not None:
            findings.append(finding)
        if len(findings) >= MAX_FINDINGS:
            break
    return summary_text, tuple(findings), True


def _finding(item: object, observed_files: set[str] | frozenset[str]) -> InvestigationFinding | None:
    if not isinstance(item, dict):
        return None
    path = normalize_investigation_path(item.get("file", item.get("path")))
    if path is None or path == ".":
        return None
    line = item.get("line")
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line.strip())
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        line = None
    justification = item.get("justification", item.get("reason", ""))
    justification = str(justification or "").strip()[:MAX_FINDING_JUSTIFICATION_CHARS]
    return InvestigationFinding(path, line, justification, path in observed_files)


def _extract_report(text: str) -> tuple[dict[str, Any] | None, str]:
    """Find a JSON object with a ``findings`` array; also return the text around it."""
    stripped = text.strip()
    candidates: list[tuple[str, str]] = [(stripped, "")]
    for match in reversed(list(re.finditer(r"```(?:json)?\s*\n?(.*?)```", text, re.S | re.I))):
        candidates.append((match.group(1).strip(), text[: match.start()] + text[match.end():]))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append((text[start : end + 1], text[:start] + text[end + 1 :]))
    for candidate, remainder in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("findings"), list):
            return value, remainder
    return None, text
