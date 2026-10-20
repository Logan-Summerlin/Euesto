from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from shared.tools import PLAN_TOOLS

from .client import APP_TITLE, OPENROUTER_URL, normalize_usage, provider_routing
from .errors import ProviderError

RETRYABLE_STATUS = frozenset({408, 409, 425, 429})

STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}
OPTIONAL_SHA256 = {"type": ["string", "null"]}
OLD_STR = {"type": "string", "minLength": 1}
OCCURRENCES = {"type": "integer", "minimum": 1, "maximum": 1000}


def _integer(minimum: int, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": minimum}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    params = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


LOCAL_TOOL_SCHEMAS = [
    _tool(
        "read",
        "Read a UTF-8 text file by line range or byte offset.",
        {
            "path": STRING,
            "start_line": _integer(1),
            "end_line": _integer(1),
            "offset": _integer(0),
            "max_bytes": _integer(1, 8_000_000),
        },
        ["path"],
    ),
    _tool(
        "write",
        "Create or replace a UTF-8 text file.",
        {
            "path": STRING,
            "content": STRING,
            "expected_sha256": OPTIONAL_SHA256,
            "create_parents": BOOLEAN,
        },
        ["path", "content"],
    ),
    _tool(
        "edit",
        "Replace an exact string in a UTF-8 text file. If old_str matches nothing only because "
        "of LF/CRLF line endings, it is retried once in the file's convention; failures return "
        "bounded match diagnostics.",
        {
            "path": STRING,
            "old_str": OLD_STR,
            "new_str": STRING,
            "expected_occurrences": OCCURRENCES,
            "expected_sha256": OPTIONAL_SHA256,
        },
        ["path", "old_str", "new_str"],
    ),
    _tool(
        "apply_patch",
        "Apply ordered write/edit/delete operations across one or more UTF-8 text files as one "
        "atomic staged change: every operation succeeds or none is applied.",
        {
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 500,
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["write", "edit", "delete"]},
                        "path": STRING,
                        "content": STRING,
                        "old_str": OLD_STR,
                        "new_str": STRING,
                        "expected_occurrences": OCCURRENCES,
                        "expected_sha256": OPTIONAL_SHA256,
                        "create_parents": BOOLEAN,
                    },
                    "required": ["operation", "path"],
                    "additionalProperties": False,
                },
            }
        },
        ["operations"],
    ),
    _tool(
        "bash",
        "Run a non-interactive Bash command in the staged workspace.",
        {
            "command": STRING,
            "working_directory": STRING,
            "timeout_seconds": _integer(1, 900),
            "env": {"type": "object"},
            "stdin": {"type": "string", "maxLength": 8_000_000},
            "rollback_on_failure": {"type": "boolean", "default": True},
        },
        ["command"],
    ),
    _tool(
        "grep",
        "Search file contents.",
        {
            "query": STRING,
            "path": STRING,
            "regex": BOOLEAN,
            "case_sensitive": BOOLEAN,
            "include_glob": STRING,
            "exclude_glob": STRING,
            "max_results": _integer(1, 5000),
            "context_lines": _integer(0, 5),
            "include_metadata": BOOLEAN,
            "cursor": STRING,
        },
        ["query"],
    ),
    _tool(
        "find",
        "Recursively find files and directories.",
        {
            "path": STRING,
            "glob": STRING,
            "max_depth": _integer(0, 20),
            "max_results": _integer(1, 2000),
            "details": BOOLEAN,
            "cursor": STRING,
        },
    ),
    _tool(
        "ls",
        "List a directory's immediate contents.",
        {
            "path": STRING,
            "max_results": _integer(1, 2000),
            "details": BOOLEAN,
            "cursor": STRING,
        },
    ),
    _tool(
        "status",
        "Summarize every staged change awaiting publication (created, modified, deleted, "
        "permission changes) against the publication baseline, with optional bounded diffs.",
        {
            "paths": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 50},
            "include_diffs": BOOLEAN,
            "max_results": _integer(1, 500),
            "cursor": STRING,
        },
    ),
    _tool(
        "investigate_repository",
        "Delegate a read-only repository investigation. Put the complete investigation request "
        "in `query`, including relevant symptoms, suspected components or files, error messages, "
        "hypotheses, and useful context; the investigation model decides which read-only files "
        "and searches to inspect. Optionally list in `inspected_paths` the files or directories "
        "you have already read or listed yourself so the investigator does not re-read them. "
        "Returns a `summary` plus structured `findings` ({file, line, justification, observed}) "
        "you can verify with one read each.",
        {
            "query": {"type": "string", "minLength": 1},
            "inspected_paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 512},
                "maxItems": 50,
            },
        },
        ["query"],
    ),
]


@dataclass(frozen=True, slots=True)
class AgentTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    message: dict[str, Any]
    usage: dict[str, Any]


def tool_schemas(mode: str, allowed_tools: set[str] | None = None) -> list[dict[str, Any]]:
    """Schemas offered to the model: ``allowed_tools`` when given (even empty), else by mode."""
    names = PLAN_TOOLS if allowed_tools is None and mode == "plan" else allowed_tools
    if names is None:
        return LOCAL_TOOL_SCHEMAS
    return [item for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] in names]


def agent_payload(
    model: str,
    messages: list[dict[str, Any]],
    mode: str,
    provider_preferences: dict[str, Any] | None = None,
    allowed_tools: set[str] | None = None,
) -> dict[str, Any]:
    tools = tool_schemas(mode, allowed_tools)
    # An empty allow-list forbids tool calls; the mode's definitions stay so providers can
    # still validate earlier tool calls in the history.
    return {
        "model": model,
        "messages": messages,
        "tools": tools or tool_schemas(mode),
        "tool_choice": "auto" if tools else "none",
        "stream": False,
        "usage": {"include": True},
        "provider": provider_routing(provider_preferences or {}),
    }


async def agent_turn(
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    mode: str,
    provider_preferences: dict[str, Any] | None = None,
    allowed_tools: set[str] | None = None,
    *,
    timeout: float = 90,
) -> AgentTurn:
    payload = agent_payload(model, messages, mode, provider_preferences, allowed_tools)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": APP_TITLE,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=False
        ) as client:
            response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            if response.status_code >= 400:
                status = response.status_code
                raise ProviderError(
                    "provider.agent_error",
                    f"OpenRouter agent request failed ({status})"
                    f"{_error_detail(_json_or_empty(response))}.",
                    retryable=status in RETRYABLE_STATUS or status >= 500,
                )
            data = response.json()
    except httpx.HTTPError as exc:
        raise ProviderError(
            "provider.connection",
            f"Agent request failed: {type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise ProviderError(
            "provider.invalid_agent_response",
            "OpenRouter returned a non-JSON agent response.",
            retryable=True,
        ) from exc
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        # OpenRouter reports upstream failures after the 200 status line as an error body.
        raise ProviderError(
            "provider.invalid_agent_response",
            f"OpenRouter returned no agent message{_error_detail(data)}.",
            retryable=bool(data.get("error")),
        )
    message = _normalize_message(choices[0]["message"])
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    calls = tuple(item for item in message.get("tool_calls") or () if isinstance(item, dict))
    return AgentTurn(str(content or ""), calls, message, normalize_usage(data.get("usage") or {}))


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_detail(data: dict[str, Any]) -> str:
    error = data.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    text = " ".join(str(message or "").split())[:300]
    return f": {text}" if text else ""


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = {"role": "assistant", "content": raw.get("content")}
    # Reasoning models (tool-calling with interleaved thinking) require their reasoning to be
    # sent back unchanged with the tool calls it produced; dropping it breaks later turns.
    if isinstance(raw.get("reasoning_details"), list) and raw["reasoning_details"]:
        normalized["reasoning_details"] = raw["reasoning_details"]
    if isinstance(raw.get("reasoning"), str) and raw["reasoning"]:
        normalized["reasoning"] = raw["reasoning"]
    calls = []
    for item in raw.get("tool_calls") or ():
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"))
        calls.append({
            "id": str(item.get("id") or uuid.uuid4()),
            "type": "function",
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": str(arguments or "{}"),
            },
        })
    if calls:
        normalized["tool_calls"] = calls
    return normalized
