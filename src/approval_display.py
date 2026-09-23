from __future__ import annotations

import json


def approval_display(kind: str, tool: str, detail: object) -> tuple[str, str]:
    """Return a concise approval prompt and an expandable full-detail payload."""
    full_detail = _json_detail(detail)
    if kind == "publish":
        summary = _publish_summary(detail)
    else:
        summary = _tool_summary(tool, detail)
    return _limit_words(summary, 200), full_detail[:50_000]


def _tool_summary(tool: str, detail: object) -> str:
    values = detail if isinstance(detail, dict) else {}
    if tool == "apply_patch" and isinstance(values.get("operations"), list):
        operations = [item for item in values["operations"] if isinstance(item, dict)]
        lines = [f"Tool approval: apply_patch · {len(operations)} operation(s), applied all-or-nothing"]
        lines.extend(f"{item.get('operation', '?')}: {item.get('path', '?')}" for item in operations[:12])
        if len(operations) > 12:
            lines.append(f"… and {len(operations) - 12} more operation(s)")
        return "\n".join(lines)
    fields = [f"{key}={str(value)[:180]}" for key, value in values.items()]
    return f"Tool approval: {tool}\n" + ("\n".join(fields[:8]) or "No arguments supplied.")


def _publish_summary(detail: object) -> str:
    values = detail if isinstance(detail, dict) else {}
    operations = values.get("operations") if isinstance(values.get("operations"), list) else []
    batch_count = values.get("batch_count")
    batch = f" (batch {values.get('batch_index')} of {batch_count})" if isinstance(batch_count, int) and batch_count > 1 else ""
    lines = [f"Publish approval{batch}: {len(operations)} file operation(s)"]
    for item in operations[:12]:
        if isinstance(item, dict):
            kind = " [binary]" if item.get("content_base64") is not None else ""
            lines.append(f"{item.get('operation', '?')}: {item.get('path', '?')}{kind}")
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
