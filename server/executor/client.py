from __future__ import annotations

import secrets
from pathlib import Path

import httpx

from shared.tools import PublishManifest, ToolRequest, ToolResult


class ExecutorUnavailable(RuntimeError):
    pass


class ExecutorClient:
    def __init__(self, socket_path: Path, token: str):
        self.socket_path = socket_path
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Executor-Nonce": secrets.token_urlsafe(24)}

    def _transport(self) -> httpx.AsyncHTTPTransport:
        return httpx.AsyncHTTPTransport(uds=str(self.socket_path))

    async def status(self) -> dict:
        try:
            async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=3, follow_redirects=False) as client:
                response = await client.get("/v1/status", headers=self._headers())
                response.raise_for_status()
                return dict(response.json())
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor unavailable: {exc}") from exc

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=None, follow_redirects=False) as client:
                response = await client.post("/v1/tools", headers=self._headers(), json=request.to_dict())
                response.raise_for_status()
                return ToolResult.from_dict(response.json())
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor tool call failed: {exc}") from exc

    async def manifest(self, run_id: str, approval_id: str) -> PublishManifest:
        async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=10, follow_redirects=False) as client:
            response = await client.post("/v1/manifest", headers=self._headers(), json={"run_id": run_id, "approval_id": approval_id})
            response.raise_for_status()
            return PublishManifest.from_dict(response.json())

    async def mark_staging_published(self) -> dict:
        async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=30, follow_redirects=False) as client:
            response = await client.post("/v1/staging/mark-published", headers=self._headers())
            response.raise_for_status()
            return dict(response.json())

    async def cancel(self, request_id: str) -> None:
        async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=3, follow_redirects=False) as client:
            await client.post(f"/v1/tools/{request_id}/cancel", headers=self._headers())

    async def command_events(self, request_id: str, after: int = 0) -> dict:
        async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=3, follow_redirects=False) as client:
            response = await client.get(
                f"/v1/tools/{request_id}/events",
                headers=self._headers(),
                params={"after": max(0, after)},
            )
            response.raise_for_status()
            return dict(response.json())

    async def discard_staging(self) -> dict:
        async with httpx.AsyncClient(transport=self._transport(), base_url="http://executor", timeout=30, follow_redirects=False) as client:
            response = await client.post("/v1/staging/discard", headers=self._headers())
            response.raise_for_status()
            return dict(response.json())
