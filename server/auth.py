from __future__ import annotations

import hmac
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .config import GatewayConfig

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]], Awaitable[None]]


class GatewaySecurityMiddleware:
    """Authenticate and reject browser/public-host requests before reading bodies."""

    def __init__(self, app: ASGIApp, config: GatewayConfig):
        self.app = app
        self.config = config
        self._requests: deque[float] = deque()
        self._chat_requests: deque[float] = deque()

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        host = _host_name(headers.get("host", ""))
        if host not in self.config.allowed_hosts:
            await _error(send, 400, "security.invalid_host", "Gateway requests must use a loopback host.")
            return
        if "origin" in headers:
            await _error(send, 403, "security.browser_origin", "Browser-originated requests are not accepted.")
            return
        if not self._within_rate_limit(path):
            await _error(
                send,
                429,
                "request.rate_limited",
                "Too many local gateway requests. Try again shortly.",
            )
            return
        if path != "/health":
            supplied = headers.get("authorization", "")
            expected = f"Bearer {self.config.session_token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                await _error(send, 401, "auth.invalid_token", "Gateway authentication failed.")
                return
        method = str(scope.get("method") or "GET").upper()
        if method in {"POST", "PUT", "PATCH"}:
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                await _error(send, 415, "request.invalid_content_type", "Use application/json.")
                return
        try:
            declared_length = int(headers.get("content-length", "0"))
        except ValueError:
            await _error(send, 400, "request.invalid_length", "Invalid Content-Length header.")
            return
        if declared_length > self.config.max_body_bytes:
            await _error(send, 413, "request.too_large", "Request body is too large.")
            return
        consumed = 0

        async def bounded_receive() -> dict:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.config.max_body_bytes:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except RequestBodyTooLarge:
            await _error(send, 413, "request.too_large", "Request body is too large.")

    def _within_rate_limit(self, path: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        if len(self._requests) >= self.config.requests_per_minute:
            return False
        self._requests.append(now)
        if path != "/v1/chat/stream":
            return True
        while self._chat_requests and self._chat_requests[0] < cutoff:
            self._chat_requests.popleft()
        if len(self._chat_requests) >= self.config.chat_requests_per_minute:
            return False
        self._chat_requests.append(now)
        return True


class RequestBodyTooLarge(Exception):
    pass


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end >= 0 else value
    return value.split(":", 1)[0]


async def _error(send: Callable, status: int, code: str, message: str) -> None:
    body = json.dumps(
        {"error": {"code": code, "message": message, "retryable": False, "details": {}}},
        separators=(",", ":"),
    ).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
