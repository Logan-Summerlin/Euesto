from __future__ import annotations

import json


def approval_display(kind: str, tool: str, detail: object) -> tuple[str, str]:
    """Return a concise approval prompt and an expandable full-detail payload."""
    full_detail = _json_detail(detail)
    if kind == "publish":
        summary = _publish_summary(detail)
    elif tool == "run_command":
        summary = _command_summary(detail)
    else:
        summary = _tool_summary(tool, detail)
    return _limit_words(summary, 200), full_detail[:50_000]


def _command_summary(detail: object) -> str:
    values = detail if isinstance(detail, dict) else {}
    executable = str(values.get("executable") or "unknown executable")
    raw_args = values.get("arguments")
    args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
    preview = " ".join(args[:18])
    if len(args) > 18:
        preview += f" … (+{len(args) - 18} more)"
    lines = [f"Command approval: {executable}", f"Arguments: {preview or '(none)'}"]
    if values.get("working_directory"):
        lines.append(f"Working directory: {values['working_directory']}")
    if values.get("timeout_seconds") is not None:
        lines.append(f"Timeout: {values['timeout_seconds']} seconds")
    lines.append("This command will run in the isolated staging workspace.")
    return "\n".join(lines)


def _tool_summary(tool: str, detail: object) -> str:
    values = detail if isinstance(detail, dict) else {}
    if tool == "apply_patch" and isinstance(values.get("edits"), list):
        edits = values["edits"]
        paths = [str(item.get("path") or "?") for item in edits if isinstance(item, dict)]
        return f"Tool approval: apply_patch · {len(paths)} edit(s): {', '.join(paths[:12])}"
    fields = [f"{key}={str(value)[:180]}" for key, value in values.items()]
    return f"Tool approval: {tool}\n" + ("\n".join(fields[:8]) or "No arguments supplied.")


def _publish_summary(detail: object) -> str:
    values = detail if isinstance(detail, dict) else {}
    operations = values.get("operations") if isinstance(values.get("operations"), list) else []
    lines = [f"Publish approval: {len(operations)} file operation(s)"]
    for item in operations[:12]:
        if isinstance(item, dict):
            lines.append(f"{item.get('operation', '?')}: {item.get('path', '?')}")
    if len(operations) > 12:
        lines.append(f"… and {len(operations) - 12} more operation(s)")
    lines.append("Review the exact manifest in the expandable details before approving.")
    return "\n".join(lines)


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
