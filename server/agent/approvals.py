from __future__ import annotations

import asyncio
from dataclasses import dataclass

from shared.permissions import PermissionDecision
from shared.tools import ToolRequest


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    future: asyncio.Future[PermissionDecision]
    request: ToolRequest | None = None
    workspace_id: str | None = None


class ApprovalCoordinator:
    def __init__(self) -> None:
        self.pending: dict[tuple[str, str], PendingApproval] = {}

    async def wait(self, run_id: str, approval_id: str, request: ToolRequest | None = None, workspace_id: str | None = None) -> PermissionDecision:
        future = asyncio.get_running_loop().create_future()
        self.pending[(run_id, approval_id)] = PendingApproval(approval_id, future, request, workspace_id)
        try:
            return await future
        finally:
            self.pending.pop((run_id, approval_id), None)

    def resolve(self, run_id: str, approval_id: str, decision: PermissionDecision) -> bool:
        pending = self.pending.get((run_id, approval_id))
        if not pending or pending.future.done():
            return False
        pending.future.set_result(decision)
        return True

    def get(self, run_id: str, approval_id: str) -> PendingApproval | None:
        return self.pending.get((run_id, approval_id))
