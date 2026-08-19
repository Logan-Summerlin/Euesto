from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .client import OPENROUTER_URL, normalize_usage
from .errors import ProviderError


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    params = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


AGENT_TOOL_PROFILE = "pi-compatible"
LOCAL_TOOL_SCHEMAS = [
    _tool("read", "Read a UTF-8 text file.", {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8_000_000}}, ["path"]),
    _tool("write", "Create or replace a UTF-8 text file.", {"path": {"type": "string"}, "content": {"type": "string"}, "expected_sha256": {"type": ["string", "null"]}, "create_parents": {"type": "boolean"}}, ["path", "content"]),
    _tool("edit", "Replace an exact string in a UTF-8 text file.", {"path": {"type": "string"}, "old_str": {"type": "string", "minLength": 1}, "new_str": {"type": "string"}, "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000}, "expected_sha256": {"type": ["string", "null"]}}, ["path", "old_str", "new_str"]),
    _tool("bash", "Run a non-interactive Bash command in the staged workspace.", {"command": {"type": "string"}, "working_directory": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900}, "env": {"type": "object"}, "stdin": {"type": "string", "maxLength": 8_000_000}}, ["command"]),
    _tool("grep", "Search file contents.", {"query": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}, "case_sensitive": {"type": "boolean"}, "include_glob": {"type": "string"}, "exclude_glob": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 5000}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5}, "include_metadata": {"type": "boolean"}, "cursor": {"type": "string"}}, ["query"]),
    _tool("find", "Recursively find files and directories.", {"path": {"type": "string"}, "glob": {"type": "string"}, "max_depth": {"type": "integer", "minimum": 0, "maximum": 20}, "max_results": {"type": "integer", "minimum": 1, "maximum": 2000}, "details": {"type": "boolean"}}),
    _tool("ls", "List a directory's immediate contents.", {"path": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 2000}, "details": {"type": "boolean"}}),
    _tool("investigate_repository", "Delegate a read-only repository investigation to a cheaper model.", {"query": {"type": "string", "minLength": 1}, "path_hint": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ["query"]),
]


@dataclass(frozen=True, slots=True)
class AgentTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    message: dict[str, Any]
    usage: dict[str, Any]


async def agent_turn(model: str, messages: list[dict[str, Any]], api_key: str, mode: str, provider_preferences: dict[str, Any] | None = None, allowed_tools: set[str] | None = None) -> AgentTurn:
    tools = [item for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] in (allowed_tools or {"read", "grep", "find", "ls"})] if mode == "plan" or allowed_tools is not None else LOCAL_TOOL_SCHEMAS
    privacy = dict(provider_preferences or {})
    payload = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto", "stream": False, "usage": {"include": True}, "provider": {"data_collection": "allow" if privacy.get("data_collection") == "allow" else "deny", "zdr": bool(privacy.get("zdr", False))}}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Local OpenRouter Chat"}
    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=False) as client:
            response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            if response.status_code >= 400:
                raise ProviderError("provider.agent_error", f"OpenRouter agent request failed ({response.status_code}).", retryable=response.status_code >= 500)
            data = response.json()
    except httpx.HTTPError as exc:
        raise ProviderError("provider.connection", f"Agent request failed: {exc}", retryable=True) from exc
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        raise ProviderError("provider.invalid_agent_response", "OpenRouter returned no agent message.")
    message = _normalize_message(choices[0]["message"])
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    calls = tuple(item for item in message.get("tool_calls") or () if isinstance(item, dict))
    return AgentTurn(str(content or ""), calls, message, normalize_usage(data.get("usage") or {}))


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = {"role": "assistant", "content": raw.get("content")}
    calls = []
    for item in raw.get("tool_calls") or ():
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"))
        calls.append({"id": str(item.get("id") or uuid.uuid4()), "type": "function", "function": {"name": str(function.get("name") or ""), "arguments": str(arguments or "{}")} })
    if calls:
        normalized["tool_calls"] = calls
    return normalized
