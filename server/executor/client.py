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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None,
        json: dict | None = None,
        check: bool = True,
    ) -> httpx.Response:
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Executor-Nonce": secrets.token_urlsafe(24),
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://executor",
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, headers=headers, json=json)
        if check:
            response.raise_for_status()
        return response

    async def _json(
        self, method: str, path: str, *, timeout: float | None, json: dict | None = None
    ) -> dict:
        return dict((await self._request(method, path, timeout=timeout, json=json)).json())

    async def status(self) -> dict:
        try:
            return await self._json("GET", "/v1/status", timeout=3)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor unavailable: {exc}") from exc

    async def execute(self, request: ToolRequest) -> ToolResult:
        try:
            return ToolResult.from_dict(
                await self._json("POST", "/v1/tools", timeout=None, json=request.to_dict())
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ExecutorUnavailable(f"Executor tool call failed: {exc}") from exc

    async def manifest(
        self,
        run_id: str,
        approval_id: str,
        *,
        publication_id: str | None = None,
        batch_index: int = 1,
    ) -> PublishManifest:
        body: dict[str, object] = {"run_id": run_id, "approval_id": approval_id}
        if publication_id:
            body["publication_id"] = publication_id
            body["batch_index"] = batch_index
        return PublishManifest.from_dict(
            await self._json("POST", "/v1/manifest", timeout=30, json=body)
        )

    async def mark_staging_published(self, receipt: PublicationReceipt) -> dict:
        return await self._json(
            "POST", "/v1/staging/mark-published", timeout=30, json=receipt.to_dict()
        )

    async def cancel(self, request_id: str) -> None:
        await self._request("POST", f"/v1/tools/{request_id}/cancel", timeout=3, check=False)

    async def discard_staging(self) -> dict:
        return await self._json("POST", "/v1/staging/discard", timeout=30)

    async def workspace_snapshot(self) -> dict:
        """The deterministic workspace snapshot and published ``AGENTS.md`` for a session."""
        return await self._json("GET", "/v1/workspace-snapshot", timeout=30)

    async def rebase_staging(self) -> dict:
        """Absorb host changes made since the staging baseline (see docs/PUBLICATION.md)."""
        return await self._json("POST", "/v1/staging/rebase", timeout=120)
