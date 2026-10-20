"""Model tool-call parsing and the bounded, model-facing form of tool results."""
from __future__ import annotations

import json
import uuid
from typing import Any

from shared.tools import MUTATION_TOOLS, ToolResult

Message = dict[str, Any]


def parse_tool_call(call: dict[str, Any]) -> tuple[str, str, str]:
    """Return a model tool call's id (generated when missing), name, and raw JSON arguments."""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return (
        str(call.get("id") or uuid.uuid4()),
        str(function.get("name") or ""),
        str(function.get("arguments") or "{}"),
    )


def parse_arguments(raw_arguments: str) -> dict[str, Any]:
    arguments = json.loads(raw_arguments)
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    return arguments


def tool_message(tool_call_id: str, content: str) -> Message:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def bounded_excerpt(value: object, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit // 2 - 40)
    return text[:head] + "\n… [bounded result omitted] …\n" + text[-tail:]


def bounded_tool_result(name: str, result: ToolResult, remaining: int) -> str:
    """Model-facing JSON for a tool result.

    Bash output, diffs, and status text are excerpted; mutation audit hashes are dropped; and
    the whole result collapses to a short stub when it exceeds the ``remaining`` byte allowance.
    """
    payload = result.to_dict()
    data = dict(payload.get("data") or {})
    if name == "bash":
        payload["output"] = ""
        for key in ("stdout", "stderr"):
            if isinstance(data.get(key), str):
                data[key] = bounded_excerpt(data[key], 32_000)
    if name in MUTATION_TOOLS and result.ok:
        for key in ("checkpoint_id", "old_sha256", "new_sha256"):
            data.pop(key, None)
        if isinstance(data.get("diff"), dict):
            data["diff"] = {**data["diff"], "text": bounded_excerpt(data["diff"].get("text"), 8_000)}
        if isinstance(data.get("operations"), list):
            data["operations"] = [
                _bounded_operation(item) for item in data["operations"] if isinstance(item, dict)
            ]
    if name == "status" and isinstance(data.get("diffs"), list):
        data["diffs"] = [
            {**item, "text": bounded_excerpt(item.get("text"), 16_000)}
            if isinstance(item, dict) and item.get("text")
            else item
            for item in data["diffs"]
        ]
        payload["output"] = bounded_excerpt(payload.get("output"), 16_000)
    payload["data"] = data
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode()) <= max(0, remaining):
        return text
    return json.dumps({
        "ok": result.ok,
        "error_code": result.error_code,
        "truncated": True,
        "output": bounded_excerpt(result.output, 2_000),
        "data": {"truncated": True},
    }, separators=(",", ":"))


def _bounded_operation(item: dict[str, Any]) -> dict[str, Any]:
    """An ``apply_patch`` operation result without hashes and with a bounded diff."""
    item = {key: value for key, value in item.items() if key not in {"old_sha256", "new_sha256"}}
    if isinstance(item.get("diff"), dict):
        item["diff"] = {**item["diff"], "text": bounded_excerpt(item["diff"].get("text"), 4_000)}
    return item
