from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextCompaction:
    before_tokens: int
    after_tokens: int
    compacted_messages: int
    summary: str


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Conservative provider-agnostic estimate that includes tool metadata."""
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(serialized) + 3) // 4)


def compact_agent_context(
    messages: list[dict[str, Any]], max_tokens: int, *, keep_recent: int = 4
) -> tuple[list[dict[str, Any]], ContextCompaction | None]:
    """Bound old tool output without breaking assistant/tool-call protocol pairs."""
    before = estimate_message_tokens(messages)
    if before <= max_tokens:
        return [dict(item) for item in messages], None

    compacted = [dict(item) for item in messages]
    protected_from = max(0, len(compacted) - keep_recent)
    changed = 0
    summaries: list[str] = []
    for index, message in enumerate(compacted):
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        normalized_calls: list[dict[str, Any]] = []
        call_changed = False
        for raw in calls:
            if not isinstance(raw, dict):
                continue
            call = dict(raw)
            function = dict(call.get("function") or {})
            arguments = str(function.get("arguments") or "{}")
            if len(arguments) > 4_000:
                function["arguments"] = json.dumps(
                    {"compacted": True, "argument_excerpt": _bounded_excerpt(arguments, 1200)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                call_changed = True
            call["function"] = function
            normalized_calls.append(call)
        if call_changed:
            compacted[index] = {**message, "tool_calls": normalized_calls}
            changed += 1
    for index, message in enumerate(compacted):
        if index >= protected_from or message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if len(content) <= 1200:
            continue
        summary = _tool_summary(content)
        compacted[index] = {**message, "content": summary}
        changed += 1
        summaries.append(summary[:240])
        if estimate_message_tokens(compacted) <= max_tokens:
            break

    if estimate_message_tokens(compacted) > max_tokens:
        for index, message in enumerate(compacted):
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            if len(content) <= 8_000:
                continue
            compacted[index] = {**message, "content": _tool_summary(content)}
            changed += 1
            if estimate_message_tokens(compacted) <= max_tokens:
                break

    # If bounded tool output is still too large, remove only complete old assistant/tool
    # exchanges. A lone tool result would be rejected by OpenAI-compatible providers.
    removed = 0
    index = 0
    while estimate_message_tokens(compacted) > max_tokens and index < max(0, len(compacted) - keep_recent):
        message = compacted[index]
        if message.get("role") == "system":
            index += 1
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            call_ids = {
                str(call.get("id") or "")
                for call in message.get("tool_calls") or ()
                if isinstance(call, dict)
            }
            end = index + 1
            while end < len(compacted) and compacted[end].get("role") == "tool":
                if str(compacted[end].get("tool_call_id") or "") in call_ids:
                    end += 1
                    continue
                break
            summaries.append(_assistant_summary(message))
            del compacted[index:end]
            removed += end - index
            changed += end - index
            protected_from = max(0, len(compacted) - keep_recent)
            continue
        if message.get("role") in {"user", "assistant"}:
            summaries.append(f"{message.get('role')}: {str(message.get('content') or '')[:240]}")
            del compacted[index]
            removed += 1
            changed += 1
            protected_from = max(0, len(compacted) - keep_recent)
            continue
        index += 1

    summary = "Earlier context compacted deterministically.\n" + "\n".join(summaries[-20:])
    if removed:
        insert_at = 1 if compacted and compacted[0].get("role") == "system" else 0
        compacted.insert(insert_at, {"role": "system", "content": summary[:8000]})
    while estimate_message_tokens(compacted) > max_tokens:
        candidates = [
            (len(str(item.get("content") or "")), index)
            for index, item in enumerate(compacted)
            if len(str(item.get("content") or "")) > 800
        ]
        if not candidates:
            break
        _size, index = max(candidates)
        item = compacted[index]
        compacted[index] = {
            **item,
            "content": "[Context excerpt] " + _bounded_excerpt(str(item.get("content") or ""), 600),
        }
        changed += 1
    after = estimate_message_tokens(compacted)
    return compacted, ContextCompaction(before, after, changed, summary[:8000])


def _tool_summary(content: str) -> str:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        output = str(payload.get("output") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        result = {
            "ok": bool(payload.get("ok")),
            "error_code": payload.get("error_code"),
            "data": data,
            "output_excerpt": _bounded_excerpt(output),
            "compacted": True,
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return "[Compacted tool output] " + _bounded_excerpt(content)


def _assistant_summary(message: dict[str, Any]) -> str:
    names = []
    for call in message.get("tool_calls") or ():
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            names.append(str(function.get("name") or "unknown"))
    content = str(message.get("content") or "")[:240]
    return f"assistant called {', '.join(names) or 'tools'}: {content}"


def _bounded_excerpt(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    return value[:half] + "\n… compacted …\n" + value[-half:]
