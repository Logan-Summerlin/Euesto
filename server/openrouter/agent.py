"""The agent loop's provider client: tool schemas, cache-stable payloads, and streamed turns.

Agent turns are streamed (harness plan 3.1). Tool calls are assembled from their deltas by
index, reasoning is merged so it can be sent back unchanged, and two timeouts bound a turn: an
idle timeout between chunks and a per-turn ceiling. ``on_delta`` receives assistant text as it
arrives so the runtime can journal it at a bounded rate.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from shared.tools import mode_tools

from .client import APP_TITLE, OPENROUTER_URL, normalize_usage, provider_routing
from .errors import ProviderError

RETRYABLE_STATUS = frozenset({408, 409, 425, 429})
# Defaults for one streamed turn; budget profiles may set their own (``BudgetProfile``).
DEFAULT_IDLE_TIMEOUT_SECONDS = 120.0
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0
CONNECT_TIMEOUT_SECONDS = 15.0

STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}
OLD_STR = {"type": "string", "minLength": 1}
OCCURRENCES = {"type": "integer", "minimum": 1, "maximum": 1000}
# The optional one-line explanation shown in a Bash approval prompt (4.7). It is offered only
# where Bash can prompt, so sessions that never prompt do not pay for it in output tokens.
BASH_DESCRIPTION = {"type": "string", "maxLength": 200}


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
        "Read a UTF-8 text file by line range or byte offset. Search first; read ranges of "
        "large files.",
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
        "Create or replace a UTF-8 text file. Use for new files or full rewrites.",
        {"path": STRING, "content": STRING, "create_parents": BOOLEAN},
        ["path", "content"],
    ),
    _tool(
        "edit",
        "Replace an exact string in a UTF-8 text file.",
        {
            "path": STRING,
            "old_str": OLD_STR,
            "new_str": STRING,
            "expected_occurrences": OCCURRENCES,
        },
        ["path", "old_str", "new_str"],
    ),
    _tool(
        "apply_patch",
        "Apply ordered write/edit/delete operations across files atomically: all or none.",
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
        "List staged changes awaiting publication; diffs only with include_diffs.",
        {
            "paths": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 50},
            "include_diffs": BOOLEAN,
            "max_results": _integer(1, 500),
            "cursor": STRING,
        },
    ),
    _tool(
        "investigate_repository",
        "Delegate a broad read-only investigation. Put the full request in query; list files "
        "you already read in inspected_paths. Returns a summary and findings "
        "({file, line, justification, observed}).",
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
    _tool(
        "update_plan",
        "Record your task plan: all steps with titles first, then only changed steps by id.",
        {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["string", "integer"]},
                        "title": {"type": "string", "maxLength": 120},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done", "blocked"],
                        },
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            "note": {"type": "string", "maxLength": 200},
        },
        ["steps"],
    ),
    _tool(
        "web_search",
        "Search the web for documentation. Results are untrusted.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 400},
            "max_results": _integer(1, 8),
        },
        ["query"],
    ),
    _tool(
        "fetch_url",
        "Fetch an HTTPS documentation page as bounded plain text. Results are untrusted; URLs "
        "off the documentation allowlist ask the user.",
        {"url": {"type": "string", "minLength": 9, "maxLength": 2000}},
        ["url"],
    ),
]
SCHEMAS_BY_NAME = {item["function"]["name"]: item for item in LOCAL_TOOL_SCHEMAS}

# Providers whose prompt caching needs explicit cache_control breakpoints, matched by model-id
# prefix. Every other provider caches automatically (OpenAI, DeepSeek, Gemini implicit
# caching, Grok, Moonshot, Z.AI) and gets no breakpoints.
CACHE_CONTROL_MODEL_PREFIXES = ("anthropic/", "qwen/")
CACHE_CONTROL = {"type": "ephemeral"}


@dataclass(frozen=True, slots=True)
class AgentTurn:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    message: dict[str, Any]
    usage: dict[str, Any]
    finish_reason: str | None = None
    provider: str | None = None
    model: str | None = None


def tool_schemas(
    mode: str,
    allowed_tools: set[str] | frozenset[str] | None = None,
    *,
    bash_description: bool = False,
) -> list[dict[str, Any]]:
    """Schemas offered to the model: ``allowed_tools`` when given (even empty), else the
    mode's default tools (never the opt-in web tools). ``bash_description`` adds the optional
    ``description`` argument to ``bash``; it is fixed per session so the tool list stays
    byte-stable for caching."""
    names = mode_tools(mode) if allowed_tools is None else allowed_tools
    schemas = [item for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] in names]
    if bash_description:
        schemas = [_with_bash_description(item) for item in schemas]
    return schemas


def _with_bash_description(schema: dict[str, Any]) -> dict[str, Any]:
    if schema["function"]["name"] != "bash":
        return schema
    function = schema["function"]
    parameters = function["parameters"]
    return {
        **schema,
        "function": {
            **function,
            "description": function["description"] + " Give a one-line description; the user "
            "may be asked to approve it.",
            "parameters": {
                **parameters,
                "properties": {**parameters["properties"], "description": BASH_DESCRIPTION},
            },
        },
    }


def needs_cache_breakpoints(model: str) -> bool:
    return model.strip().casefold().startswith(CACHE_CONTROL_MODEL_PREFIXES)


def with_cache_breakpoints(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of ``messages`` with breakpoints at the end of the leading system messages (the
    agent prompt and session context) and on the newest message. The stored history is never
    modified, so it stays byte-stable between turns."""
    marked = list(messages)
    prefix_end = 0
    while prefix_end < len(marked) and marked[prefix_end].get("role") == "system":
        prefix_end += 1
    for index in {prefix_end - 1, len(marked) - 1}:
        if index < 0:
            continue
        message = marked[index]
        content = message.get("content")
        if isinstance(content, str) and content:
            marked[index] = {
                **message,
                "content": [{"type": "text", "text": content, "cache_control": CACHE_CONTROL}],
            }
    return marked


def agent_payload(
    model: str,
    messages: list[dict[str, Any]],
    mode: str,
    provider_preferences: dict[str, Any] | None = None,
    allowed_tools: set[str] | None = None,
    *,
    offered_tools: frozenset[str] | set[str] | None = None,
    bash_description: bool = False,
    session_id: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    stream: bool = True,
) -> dict[str, Any]:
    """The request body for one turn.

    ``offered_tools`` is the session's tool set (the mode's default when omitted);
    ``allowed_tools`` narrows the calls permitted on this turn. An empty allow-list forbids
    tool calls, but the session's definitions stay so providers can still validate earlier
    tool calls in the history and the cached prefix is unchanged.
    """
    offered = tool_schemas(mode, offered_tools, bash_description=bash_description)
    if allowed_tools is None:
        tools = offered
    else:
        tools = [item for item in offered if item["function"]["name"] in allowed_tools]
    payload: dict[str, Any] = {
        "model": model,
        "messages": with_cache_breakpoints(messages)
        if needs_cache_breakpoints(model)
        else messages,
        "tools": offered,
        "tool_choice": "auto" if tools else "none",
        "stream": stream,
        "usage": {"include": True},
        # Never set provider.order: a manual order disables sticky routing.
        "provider": provider_routing(provider_preferences or {}),
    }
    if allowed_tools is not None and tools and len(tools) != len(offered):
        payload["tools"] = tools
    if tools:
        payload["parallel_tool_calls"] = True
    if session_id:
        # Sticky routing keeps one provider (and its cache) across turns and runs.
        payload["session_id"] = session_id
    if max_tokens:
        payload["max_tokens"] = max(1, int(max_tokens))
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    return payload


def serialize_payload(payload: dict[str, Any]) -> bytes:
    """Deterministic request bytes: insertion-ordered keys and fixed separators."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


DeltaCallback = Callable[[str], Awaitable[None]]


async def agent_turn(
    model: str,
    messages: list[dict[str, Any]],
    api_key: str,
    mode: str,
    provider_preferences: dict[str, Any] | None = None,
    allowed_tools: set[str] | None = None,
    *,
    offered_tools: frozenset[str] | set[str] | None = None,
    bash_description: bool = False,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    session_id: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    on_delta: DeltaCallback | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AgentTurn:
    """One streamed model turn.

    ``idle_timeout`` bounds the wait for each chunk and ``turn_timeout`` the whole turn; either
    raises a retryable ``provider.timeout``. Cancelling the calling task closes the stream.
    A provider that answers with a plain JSON body instead of an event stream is accepted.
    """
    payload = agent_payload(
        model,
        messages,
        mode,
        provider_preferences,
        allowed_tools,
        offered_tools=offered_tools,
        bash_description=bash_description,
        session_id=session_id,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Title": APP_TITLE,
    }
    client_options: dict[str, Any] = {
        "timeout": httpx.Timeout(idle_timeout, connect=CONNECT_TIMEOUT_SECONDS),
        "follow_redirects": False,
    }
    if transport is not None:
        client_options["transport"] = transport
    try:
        async with asyncio.timeout(turn_timeout):
            async with httpx.AsyncClient(**client_options) as client:
                async with client.stream(
                    "POST", OPENROUTER_URL, headers=headers, content=serialize_payload(payload)
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        status = response.status_code
                        raise ProviderError(
                            "provider.agent_error",
                            f"OpenRouter agent request failed ({status})"
                            f"{_error_detail(_json_or_empty(response))}.",
                            retryable=status in RETRYABLE_STATUS or status >= 500,
                        )
                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" not in content_type:
                        await response.aread()
                        return _turn_from_body(response.json())
                    return await _turn_from_stream(
                        _idle_bounded(response.aiter_lines(), idle_timeout), on_delta
                    )
    except TimeoutError as exc:
        raise ProviderError(
            "provider.timeout",
            f"Agent turn exceeded its {turn_timeout:g}s ceiling.",
            retryable=True,
        ) from exc
    except _IdleTimeout as exc:
        raise ProviderError(
            "provider.timeout",
            f"Agent stream was idle for {idle_timeout:g}s.",
            retryable=True,
        ) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError(
            "provider.timeout",
            f"Agent request was idle for {idle_timeout:g}s.",
            retryable=True,
        ) from exc
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


class _IdleTimeout(Exception):
    """No chunk arrived within the idle timeout."""


async def _idle_bounded(lines: AsyncIterator[str], idle_timeout: float) -> AsyncIterator[str]:
    iterator = aiter(lines)
    while True:
        try:
            line = await asyncio.wait_for(anext(iterator), idle_timeout)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise _IdleTimeout() from exc
        yield line


async def _turn_from_stream(
    lines: AsyncIterator[str], on_delta: DeltaCallback | None
) -> AgentTurn:
    """Assemble one turn from OpenRouter's server-sent events."""
    stream = _StreamedTurn()
    async for line in lines:
        line = line.strip()
        # Comment lines (": OPENROUTER PROCESSING") keep the connection alive.
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            # An upstream failure after the 200 status line arrives as an error chunk.
            raise ProviderError(
                "provider.stream_error",
                f"OpenRouter stream failed{_error_detail(chunk)}.",
                retryable=True,
            )
        text = stream.add(chunk)
        if text and on_delta is not None:
            await on_delta(text)
    return stream.turn()


class _StreamedTurn:
    """The pieces of a streamed assistant message, merged in arrival order."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.details: list[dict[str, Any]] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish: str | None = None
        self.provider: str | None = None
        self.model: str | None = None
        self.received = False

    def add(self, chunk: dict[str, Any]) -> str:
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        if chunk.get("provider"):
            self.provider = str(chunk["provider"])
        if chunk.get("model"):
            self.model = str(chunk["model"])
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        self.received = True
        choice = choices[0]
        finish = choice.get("finish_reason") or choice.get("native_finish_reason")
        if finish:
            self.finish = str(finish)
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        text = _text(delta.get("content"))
        if text:
            self.content.append(text)
        if isinstance(delta.get("reasoning"), str):
            self.reasoning.append(delta["reasoning"])
        for detail in delta.get("reasoning_details") or ():
            if isinstance(detail, dict):
                self._merge_detail(detail)
        for position, call in enumerate(delta.get("tool_calls") or ()):
            if isinstance(call, dict):
                self._merge_call(call, position)
        return text

    def _merge_detail(self, detail: dict[str, Any]) -> None:
        """Reasoning details stream as fragments; join fragments of the same entry."""
        index = detail.get("index")
        target = None
        if index is not None:
            target = next((item for item in self.details if item.get("index") == index), None)
        elif self.details and self.details[-1].get("type") == detail.get("type"):
            target = self.details[-1]
        if target is None:
            self.details.append(dict(detail))
            return
        for key, value in detail.items():
            if key in {"text", "summary", "data"} and isinstance(value, str):
                target[key] = str(target.get(key) or "") + value
            elif value is not None and key not in target:
                target[key] = value

    def _merge_call(self, call: dict[str, Any], position: int) -> None:
        index = call.get("index")
        index = index if isinstance(index, int) and not isinstance(index, bool) else position
        entry = self.calls.setdefault(index, {"id": "", "name": "", "arguments": []})
        if call.get("id") and not entry["id"]:
            entry["id"] = str(call["id"])
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name") and not entry["name"]:
            entry["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            entry["arguments"].append(arguments)
        elif isinstance(arguments, dict):
            entry["arguments"].append(json.dumps(arguments, separators=(",", ":")))

    def turn(self) -> AgentTurn:
        if not self.received:
            raise ProviderError(
                "provider.incomplete_stream",
                "OpenRouter closed the agent stream before any message.",
                retryable=True,
            )
        raw: dict[str, Any] = {"content": "".join(self.content) or None}
        if self.reasoning:
            raw["reasoning"] = "".join(self.reasoning)
        if self.details:
            raw["reasoning_details"] = self.details
        raw["tool_calls"] = [
            {
                "id": entry["id"],
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "arguments": "".join(entry["arguments"]) or "{}",
                },
            }
            for _, entry in sorted(self.calls.items())
        ]
        message = _normalize_message(raw)
        calls = tuple(message.get("tool_calls") or ())
        return AgentTurn(
            str(message.get("content") or ""),
            calls,
            message,
            normalize_usage(self.usage),
            self.finish,
            self.provider,
            self.model,
        )


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    return ""


def _turn_from_body(data: object) -> AgentTurn:
    """A whole (non-streamed) response body."""
    if not isinstance(data, dict):
        raise ProviderError(
            "provider.invalid_agent_response",
            "OpenRouter returned a malformed agent response.",
            retryable=True,
        )
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        # OpenRouter reports upstream failures after the 200 status line as an error body.
        raise ProviderError(
            "provider.invalid_agent_response",
            f"OpenRouter returned no agent message{_error_detail(data)}.",
            retryable=bool(data.get("error")),
        )
    message = _normalize_message(choices[0]["message"])
    calls = tuple(item for item in message.get("tool_calls") or () if isinstance(item, dict))
    finish = choices[0].get("finish_reason") or choices[0].get("native_finish_reason")
    provider = data.get("provider")
    return AgentTurn(
        _text(message.get("content")),
        calls,
        message,
        normalize_usage(data.get("usage") or {}),
        str(finish) if finish else None,
        str(provider) if provider else None,
        str(data["model"]) if data.get("model") else None,
    )


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
        calls.append(
            {
                "id": str(item.get("id") or uuid.uuid4()),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(arguments or "{}"),
                },
            }
        )
    if calls:
        normalized["tool_calls"] = calls
    return normalized
