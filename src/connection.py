from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QThread, Signal

from shared.responses import GatewayStatus

from .gateway_client import (
    GatewayClient,
    GatewayConnection,
    GatewayError,
    IncompatibleGatewayError,
)


class HealthState(StrEnum):
    DISCONNECTED = "disconnected"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class HealthResult:
    state: HealthState
    message: str
    status: GatewayStatus | None = None


class GatewayHealthWorker(QThread):
    complete = Signal(object)

    def __init__(self, connection: GatewayConnection):
        super().__init__()
        self.connection = connection

    def run(self) -> None:
        if not self.connection.token:
            self.complete.emit(HealthResult(HealthState.DISCONNECTED, "Gateway token not configured"))
            return
        try:
            status = GatewayClient(self.connection).status()
        except IncompatibleGatewayError as exc:
            self.complete.emit(HealthResult(HealthState.INCOMPATIBLE, str(exc)))
        except GatewayError as exc:
            self.complete.emit(HealthResult(HealthState.DISCONNECTED, str(exc)))
        else:
            state = HealthState.READY if status.ready else HealthState.DEGRADED
            self.complete.emit(HealthResult(state, "Gateway ready" if status.ready else "Gateway degraded", status))
