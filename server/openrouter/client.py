from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from shared.coercion import optional_float, optional_int
from shared.requests import ChatRequest
from shared.tools import openrouter_tools

from .errors import ProviderError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass(slots=True)
class ProviderEvent:
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    done: bool = False


class OpenRouterGatewayClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def stream_chat(
        self, request: ChatRequest, api_key: str, cancel_event: asyncio.Event
    ) -> AsyncIterator[ProviderEvent]:
        payload = build_payload(request)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Local OpenRouter Chat",
        }
        timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0)
        started = time.perf_counter()
        first_token_at: float | None = None
        latest_usage: dict[str, Any] = {}
        latest_model: str | None = None
        latest_provider: str | None = None
        latest_finish: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self.transport, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST", OPENROUTER_URL, json=payload, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise _http_error(response)
                    async for line in response.aiter_lines():
                        if cancel_event.is_set():
                            return
                        event = parse_sse_line(line)
                        if event is None:
                            continue
                        if event.text and first_token_at is None:
                            first_token_at = time.perf_counter()
                        if event.usage:
                            latest_usage.update(event.usage)
                        latest_model = event.model_id or latest_model
                        latest_provider = event.provider_id or latest_provider
                        latest_finish = event.finish_reason or latest_finish
                        if event.done:
                            elapsed = max(0.0, time.perf_counter() - started)
                            usage = normalize_usage(latest_usage)
                            usage["elapsed_seconds"] = elapsed
                            if first_token_at is not None:
                                usage["time_to_first_token"] = first_token_at - started
                            completion = optional_int(usage.get("completion_tokens")) or 0
                            if elapsed > 0 and completion:
                                usage["tokens_per_second"] = completion / elapsed
                            yield ProviderEvent(
                                usage=usage,
                                model_id=latest_model,
                                provider_id=latest_provider,
                                finish_reason=latest_finish,
                                done=True,
                            )
                            return
                        yield event
                    if not cancel_event.is_set():
                        raise ProviderError(
                            "provider.incomplete_stream",
                            "OpenRouter closed the stream before a completion event.",
                            retryable=True,
                        )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "provider.timeout", "The OpenRouter request timed out.", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            if cancel_event.is_set():
                return
            raise ProviderError(
                "provider.connection", f"OpenRouter connection failed: {exc}", retryable=True
            ) from exc


def build_payload(request: ChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model.strip(),
        "messages": [{"role": item.role, "content": item.content} for item in request.messages],
        "stream": True,
        "usage": {"include": True},
        "provider": _provider_preferences(request.provider_preferences),
    }
    tools = openrouter_tools(request.server_tools)
    if tools:
        payload["tools"] = tools
    options = request.options
    supported = set(request.supported_parameters)
    if (
        options.get("max_tokens") is not None
        and {"max_tokens", "max_completion_tokens"} & supported
    ):
        payload["max_tokens"] = max(1, int(options["max_tokens"]))
    if options.get("reasoning_effort") and "reasoning" in supported:
        payload["reasoning"] = {"effort": str(options["reasoning_effort"])}
    if options.get("temperature") is not None and "temperature" in supported:
        payload["temperature"] = min(2.0, max(0.0, float(options["temperature"])))
    if options.get("top_p") is not None and "top_p" in supported:
        payload["top_p"] = min(1.0, max(0.0, float(options["top_p"])))
    if options.get("stop") and "stop" in supported:
        payload["stop"] = [str(item) for item in options["stop"] if str(item)][:8]
    return payload


def _provider_preferences(value: dict[str, Any]) -> dict[str, Any]:
    """Fail closed to non-training providers unless the desktop explicitly opts in."""
    return {
        "data_collection": ("allow" if value.get("data_collection") == "allow" else "deny"),
        "zdr": bool(value.get("zdr", False)),
    }


def parse_sse_line(line: str) -> ProviderEvent | None:
    line = line.strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return ProviderEvent(done=True)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if payload.get("error"):
        error = payload["error"]
        detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise ProviderError("provider.stream_error", detail)
    text = ""
    finish_reason: str | None = None
    choices = payload.get("choices") or []
    if choices:
        choice = choices[0]
        content = (choice.get("delta") or {}).get("content")
        finish_reason = choice.get("finish_reason")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    provider = payload.get("provider") or payload.get("provider_name")
    return ProviderEvent(
        text=text,
        usage=normalize_usage(payload.get("usage") or {}),
        model_id=str(payload["model"]) if payload.get("model") else None,
        provider_id=str(provider) if provider else None,
        finish_reason=str(finish_reason) if finish_reason else None,
    )


def normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else {}
    )
    normalized = dict(usage)
    normalized["prompt_tokens"] = optional_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    normalized["completion_tokens"] = optional_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    normalized["cached_tokens"] = optional_int(
        usage.get("cached_tokens", prompt_details.get("cached_tokens"))
    )
    normalized["reasoning_tokens"] = optional_int(
        usage.get("reasoning_tokens", completion_details.get("reasoning_tokens"))
    )
    normalized["total_tokens"] = optional_int(usage.get("total_tokens"))
    if normalized["total_tokens"] is None and (
        normalized["prompt_tokens"] is not None or normalized["completion_tokens"] is not None
    ):
        normalized["total_tokens"] = (normalized["prompt_tokens"] or 0) + (
            normalized["completion_tokens"] or 0
        )
    normalized["cost"] = optional_float(usage.get("cost"))
    return {key: value for key, value in normalized.items() if value is not None}


def _http_error(response: httpx.Response) -> ProviderError:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
    except (ValueError, TypeError):
        detail = response.text.strip()[:300] or response.reason_phrase
    if response.status_code == 401:
        return ProviderError("provider.authentication", "OpenRouter rejected the API key.")
    if response.status_code == 429:
        return ProviderError(
            "provider.rate_or_credit_limit",
            "OpenRouter rate or credit limit reached.",
            retryable=True,
        )
    if response.status_code in {404, 422}:
        return ProviderError("provider.model_unavailable", detail)
    return ProviderError(
        "provider.http_error",
        f"OpenRouter error {response.status_code}: {detail}",
        retryable=response.status_code >= 500,
    )
