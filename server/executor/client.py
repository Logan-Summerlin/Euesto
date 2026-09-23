from __future__ import annotations

import secrets
from pathlib import Path

import httpx

from shared.tools import PublicationReceipt, PublishManifest, ToolRequest, ToolResult


class ExecutorUnavailable(RuntimeError):
    pass


class ExecutorClient:
    def __init__(self, socket_path: Path, token: str):
        self.socket_path = socket_path
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Executor-Nonce": secrets.token_urlsafe(24)}

    async def _request(self, method: str, path: str, *, timeout: float | None, json: dict | None = None, check: bool = True) -> httpx.Response:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        async with httpx.AsyncClient(transport=transport, base_url="http://executor", timeout=timeout, follow_redirects=False) as client:
            response = await client.request(method, path, headers=self._headers(), json=json)
        if check:
            response.raise_for_status()
        return response

    async def status(self) -> dict:
        try:
            return dict((await self._request("GET", "/v1/status", timeout=3)).json())
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor unavailable: {exc}") from exc

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            return ToolResult.from_dict((await self._request("POST", "/v1/tools", timeout=None, json=request.to_dict())).json())
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor tool call failed: {exc}") from exc

    async def manifest(self, run_id: str, approval_id: str, *, publication_id: str | None = None, batch_index: int = 1) -> PublishManifest:
        body: dict[str, object] = {"run_id": run_id, "approval_id": approval_id}
        if publication_id:
            body["publication_id"] = publication_id
            body["batch_index"] = batch_index
        return PublishManifest.from_dict((await self._request("POST", "/v1/manifest", timeout=30, json=body)).json())

    async def mark_staging_published(self, receipt: PublicationReceipt) -> dict:
        return dict((await self._request("POST", "/v1/staging/mark-published", timeout=30, json=receipt.to_dict())).json())

    async def cancel(self, request_id: str) -> None:
        await self._request("POST", f"/v1/tools/{request_id}/cancel", timeout=3, check=False)

    async def discard_staging(self) -> dict:
        return dict((await self._request("POST", "/v1/staging/discard", timeout=30)).json())
