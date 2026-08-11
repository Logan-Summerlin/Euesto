from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .client import OPENROUTER_URL, normalize_usage
from .errors import ProviderError

LOCAL_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "list_files", "description": "List paths; details adds size, include_sha256 adds hashes (100 max).", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}, "glob": {"type": "string"}, "max_depth": {"type": "integer", "minimum": 0, "maximum": 20}, "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}, "details": {"type": "boolean"}, "include_sha256": {"type": "boolean"}, "cursor": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read UTF-8 text; metadata includes full-file hash and size.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "start_byte": {"type": "integer", "minimum": 0}, "cursor": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 256000}}, "oneOf": [{"required": ["path"]}, {"required": ["paths"]}], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_text", "description": "Search text; results include path and line, with optional context.", "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "regex": {"type": "boolean"}, "case_sensitive": {"type": "boolean"}, "include_glob": {"type": "string"}, "exclude_glob": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5}, "include_metadata": {"type": "boolean"}, "cursor": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "inspect_workspace", "description": "List source-to-staging changes and optional bounded diffs.", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "minimum": 1, "maximum": 500}, "cursor": {"type": "string"}, "include_diff": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}, "max_diff_bytes": {"type": "integer", "minimum": 1, "maximum": 256000}, "max_diff_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "inspect_checkpoint", "description": "Inspect a pre-mutation snapshot or diff it to current staging.", "parameters": {"type": "object", "required": ["checkpoint_id"], "properties": {"checkpoint_id": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}, "cursor": {"type": "string"}, "include_diff": {"type": "boolean"}, "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}, "max_diff_bytes": {"type": "integer", "minimum": 1, "maximum": 256000}, "max_diff_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "move_file", "description": "Hash-checked staged move; returns a pre-operation checkpoint.", "parameters": {"type": "object", "required": ["source", "destination", "expected_sha256"], "properties": {"source": {"type": "string"}, "destination": {"type": "string"}, "expected_sha256": {"type": "string"}, "destination_sha256": {"type": ["string", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "copy_file", "description": "Hash-checked staged copy; returns a pre-operation checkpoint.", "parameters": {"type": "object", "required": ["source", "destination", "expected_sha256"], "properties": {"source": {"type": "string"}, "destination": {"type": "string"}, "expected_sha256": {"type": "string"}, "destination_sha256": {"type": ["string", "null"]}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "restore_checkpoint", "description": "Preview or restore an entire pre-mutation staging checkpoint.", "parameters": {"type": "object", "required": ["checkpoint_id"], "properties": {"checkpoint_id": {"type": "string"}, "preview": {"type": "boolean"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "apply_patch", "description": "Create/update staged UTF-8 text. edits[].content replaces the ENTIRE file when mode=replace_file. For a snippet edit, use mode=replace_exact (old_str/new_str, hash-checked). Returns a bounded diff and checkpoint.", "parameters": {"type": "object", "required": ["edits"], "properties": {"edits": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["path", "expected_sha256", "mode"], "properties": {"path": {"type": "string"}, "expected_sha256": {"type": ["string", "null"]}, "mode": {"type": "string", "enum": ["replace_file", "replace_exact"]}, "content": {"type": "string"}, "old_str": {"type": "string", "minLength": 1}, "new_str": {"type": "string"}, "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 1000}}, "additionalProperties": False}}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run literal argv, no shell/pipes/redirects. Bounded stdin is sent then closed. git and interpreters run via approved commands; staged files persist across calls — write real test files instead of one-off -c snippets.", "parameters": {"type": "object", "required": ["executable", "arguments"], "properties": {"executable": {"type": "string"}, "arguments": {"type": "array", "items": {"type": "string"}}, "working_directory": {"type": "string", "description": "Must be an existing directory path, not a file or script."}, "timeout_seconds": {"type": "integer"}, "environment": {"type": "object"}, "stdin": {"type": "string", "maxLength": 256000}}, "additionalProperties": False}}},
]


@dataclass(frozen=True, slots=True)
class AgentTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    message: dict[str, Any]
    usage: dict[str, Any]


async def agent_turn(model: str, messages: list[dict[str, Any]], api_key: str, mode: str, provider_preferences: dict[str, Any] | None = None) -> AgentTurn:
    tools = LOCAL_TOOL_SCHEMAS[:3] if mode == "plan" else LOCAL_TOOL_SCHEMAS
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
    """Keep only provider-portable assistant fields and stable tool-call IDs."""
    normalized: dict[str, Any] = {"role": "assistant", "content": raw.get("content")}
    calls: list[dict[str, Any]] = []
    for item in raw.get("tool_calls") or ():
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"))
        calls.append({"id": str(item.get("id") or uuid.uuid4()), "type": "function", "function": {"name": name, "arguments": str(arguments or "{}")} })
    if calls:
        normalized["tool_calls"] = calls
    return normalized
