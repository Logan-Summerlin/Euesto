from __future__ import annotations

import asyncio
from dataclasses import dataclass

from shared.permissions import PermissionDecision
from shared.tools import ToolRequest


class ApprovalTimeoutError(TimeoutError):
    """Raised when an approval outlives the run's permitted waiting window."""


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    future: asyncio.Future[PermissionDecision]
    request: ToolRequest | None = None
    workspace_id: str | None = None


class ApprovalCoordinator:
    def __init__(self) -> None:
        self.pending: dict[tuple[str, str], PendingApproval] = {}

    async def wait(self, run_id: str, approval_id: str, request: ToolRequest | None = None,
                   workspace_id: str | None = None, timeout: float | None = None) -> PermissionDecision:
        future = asyncio.get_running_loop().create_future()
        self.pending[(run_id, approval_id)] = PendingApproval(approval_id, future, request, workspace_id)
        try:
            if timeout is not None and timeout <= 0:
                raise ApprovalTimeoutError("Approval deadline has expired.")
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise ApprovalTimeoutError("Approval deadline expired before a decision was received.") from exc
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
