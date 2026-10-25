from __future__ import annotations

import html
import json
import re
from collections.abc import Callable

from shared.publication_risk import (
    FLAG_LABELS,
    classify_manifest,
    manifest_secrets,
    needs_confirmation,
    review_order,
)

BaseReader = Callable[[str], str | None]


def approval_display(
    kind: str,
    tool: str,
    detail: object,
    *,
    context: dict[str, object] | None = None,
    base_reader: BaseReader | None = None,
) -> tuple[str, str]:
    """Return a concise approval prompt and an expandable full-detail payload.

    ``context`` carries the approval event's own fields beside the arguments: a Bash
    command's description, working directory, timeout, and network (plan 4.7), or a URL
    approval's host and reason (5.4). ``base_reader`` reads a host file's current text, so a
    publication's risk flags can tell a dependency change from a package-script change (4.3).
    """
    full_detail = _json_detail(detail)
    if kind == "publish":
        summary = publication_summary(detail, base_reader=base_reader)
    elif kind == "url":
        summary = _url_summary(context or {})
    else:
        summary = _tool_summary(tool, detail, context or {})
    return _limit_words(summary, 200), full_detail[:50_000]


def _tool_summary(tool: str, detail: object, context: dict[str, object]) -> str:
    values = detail if isinstance(detail, dict) else {}
    if tool == "apply_patch" and isinstance(values.get("operations"), list):
        operations = [item for item in values["operations"] if isinstance(item, dict)]
        lines = [
            f"Tool approval: apply_patch · {len(operations)} operation(s), applied all-or-nothing"
        ]
        lines.extend(
            f"{item.get('operation', '?')}: {item.get('path', '?')}" for item in operations[:12]
        )
        if len(operations) > 12:
            lines.append(f"… and {len(operations) - 12} more operation(s)")
        return "\n".join(lines)
    if tool == "bash":
        return _bash_summary(values, context)
    fields = [f"{key}={str(value)[:180]}" for key, value in values.items()]
    return f"Tool approval: {tool}\n" + ("\n".join(fields[:8]) or "No arguments supplied.")


def _bash_summary(values: dict[str, object], context: dict[str, object]) -> str:
    """Everything needed to judge a command, beside the command itself (plan 4.7)."""
    lines = ["Tool approval: bash"]
    description = context.get("description")
    if isinstance(description, str) and description.strip():
        lines.append(f"Purpose (from the model): {description.strip()[:200]}")
    lines.append(f"Command: {str(values.get('command') or '')[:2_000]}")
    directory = context.get("working_directory") or values.get("working_directory") or "."
    timeout = context.get("timeout_seconds") or values.get("timeout_seconds") or 60
    network = context.get("network") or "none"
    lines.append(f"Working directory: /work/{str(directory).lstrip('/') or '.'}")
    lines.append(f"Timeout: {timeout} seconds")
    lines.append(
        "Network: allowlisted registries through the audited proxy"
        if network == "registries"
        else "Network: none"
    )
    return "\n".join(lines)


def _url_summary(context: dict[str, object]) -> str:
    url = str(context.get("url") or "")
    reason = str(context.get("approval_reason") or "it is not on the documentation allowlist")
    return (
        f"fetch_url wants to read {url}\n"
        f"It needs approval because {reason}. The page is fetched by the gateway, never by the "
        "executor, and returned to the model as bounded, untrusted text."
    )


def publication_summary(detail: object, *, base_reader: BaseReader | None = None) -> str:
    """A publication prompt: flagged files first with their risk flags, then secret warnings."""
    values = detail if isinstance(detail, dict) else {}
    raw = values.get("operations") if isinstance(values.get("operations"), list) else []
    operations = [item for item in raw if isinstance(item, dict)]
    batch_count = values.get("batch_count")
    batch = (
        f" (batch {values.get('batch_index')} of {batch_count})"
        if isinstance(batch_count, int) and batch_count > 1
        else ""
    )
    flags = classify_manifest(operations, base_reader=base_reader)
    by_path = {str(item.get("path") or ""): item for item in operations}
    lines = [f"Publish approval{batch}: {len(operations)} file operation(s)"]
    if needs_confirmation(flags):
        lines.append(
            "Needs explicit confirmation: this batch changes files the host runs, "
            "dependencies, or the workspace policy."
        )
    ordered = review_order(flags)
    for path in ordered[:12]:
        item = by_path.get(path, {})
        labels = [FLAG_LABELS[flag] for flag in flags.get(path, ())]
        suffix = f" [{', '.join(labels)}]" if labels else ""
        lines.append(f"{item.get('operation', '?')}: {path}{suffix}")
    if len(ordered) > 12:
        lines.append(f"… and {len(ordered) - 12} more operation(s)")
    secrets = manifest_secrets(operations)
    for path, kinds in sorted(secrets.items())[:6]:
        listed = ", ".join(f"{name} ×{count}" for name, count in sorted(kinds.items()))
        lines.append(f"Secret warning: {path} would write {listed}")
    lines.append("Review the exact manifest in the expandable details before approving.")
    return "\n".join(lines)


_BASH_TOKEN = re.compile(
    r"""(?P<string>'[^']*'?|"(?:\\.|[^"\\])*"?)"""
    r"""|(?P<operator>&&|\|\||[|;&<>]+)"""
    r"""|(?P<variable>\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)"""
    r"""|(?P<flag>(?<![\w-])--?[A-Za-z0-9][\w-]*(?:=\S*)?)"""
    r"""|(?P<word>[^\s'"|;&<>$]+)"""
    r"""|(?P<space>\s+)"""
    r"""|(?P<other>.)""",
    re.DOTALL,
)
BASH_COLORS = {
    "command": "#4f8cc9",
    "string": "#3c9a5f",
    "operator": "#c9534f",
    "variable": "#a66bbe",
    "flag": "#c08a2e",
}


def bash_html(command: str, *, limit: int = 4_000) -> str:
    """The command as escaped rich text with simple syntax highlighting (plan 4.7).

    The first word of each pipeline segment is the command; quoted strings, operators,
    variables, and flags are coloured. Everything is HTML-escaped, so a command can never
    inject markup into the prompt.
    """
    parts: list[str] = []
    expect_command = True
    for match in _BASH_TOKEN.finditer(command[:limit]):
        kind = match.lastgroup or "other"
        text = html.escape(match.group())
        if kind == "space":
            parts.append(text.replace("\n", "<br/>"))
            continue
        if kind == "word" and expect_command and "=" not in match.group():
            parts.append(f'<b><font color="{BASH_COLORS["command"]}">{text}</font></b>')
            expect_command = False
            continue
        if kind == "operator":
            expect_command = True
        elif kind != "word" or "=" not in match.group():
            expect_command = expect_command and kind == "word"
        color = BASH_COLORS.get(kind)
        parts.append(f'<font color="{color}">{text}</font>' if color else text)
    suffix = " …" if len(command) > limit else ""
    return f"<code>{''.join(parts)}{suffix}</code>"


def _json_detail(detail: object) -> str:
    try:
        return json.dumps(detail, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(detail)


def _limit_words(value: str, limit: int) -> str:
    words = value.split()
    if len(words) <= limit:
        return value
    return " ".join(words[:limit]) + " …"
