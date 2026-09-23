from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Slot

from ..connection import GatewayHealthWorker, HealthResult, HealthState
from ..gateway_client import DEFAULT_GATEWAY_URL, GatewayConnection
from ..runtime_manager import RuntimeManager, RuntimeResult
from ..settings import (
    app_data_dir,
    get_gateway_session_token,
    get_gateway_token,
    save_gateway_token,
)
from ..storage import Storage
from ..workspace_broker import BrokerError, canonical_workspace, workspace_id
from .host import BridgeHost

BUSY_RUNTIME_STATES = frozenset({"starting", "stopping", "pulling", "building", "checking"})
RUNTIME_LABELS = {
    "starting": "Docker: starting",
    "stopping": "Docker: stopping",
    "pulling": "Runtime: downloading",
    "building": "Runtime: building",
    "checking": "Runtime: checking",
}
HEALTH_LABELS = {
    HealthState.READY: "Gateway: ready",
    HealthState.STARTING: "Gateway: starting",
    HealthState.DEGRADED: "Gateway: degraded",
    HealthState.INCOMPATIBLE: "Gateway: incompatible",
    HealthState.DISCONNECTED: "Gateway: offline",
}


class RuntimeService(QObject):
    """Selected workspace, local runtime lifecycle, gateway token, and gateway health."""

    def __init__(self, host: BridgeHost, storage: Storage, *, automatic: bool | None = None, manager: RuntimeManager | None = None):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.manager = manager or RuntimeManager(app_data_dir(), self)
        self.automatic = bool(getattr(sys, "frozen", False)) if automatic is None else automatic
        self.workspace_path = self.storage.get_setting("workspace_path", "") or ""
        self.gateway_text = "Gateway: checking"
        self.gateway_detail = ""
        self.health_state = HealthState.STARTING
        self.last_status: object | None = None
        self.state = "starting" if self.automatic else "manual"
        self.detail = "Preparing the local runtime…" if self.automatic else "The developer runtime is managed by scripts."
        self.target_identity: str | None = None
        self.health_worker: GatewayHealthWorker | None = None
        self.gateway_token = get_gateway_session_token() or get_gateway_token() or ""
        self.closing = False
        self._recheck_requested = False
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(10_000)
        self.health_timer.timeout.connect(self.check_gateway)

    def start(self) -> None:
        self.health_timer.start()
        self.manager.progressChanged.connect(self._runtime_progress)
        self.manager.ready.connect(self._runtime_ready)
        self.manager.failed.connect(self._runtime_failed)
        self.manager.setupStarted.connect(self.host.runtimeSetupStarted)
        self.manager.setupFinished.connect(self.host.runtimeSetupFinished)
        QTimer.singleShot(0, self._start_for_saved_workspace if self.automatic else self.check_gateway)

    # -- queries -------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.state in BUSY_RUNTIME_STATES

    def connection(self) -> GatewayConnection | None:
        if not self.gateway_token:
            return None
        url = self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL
        try:
            return GatewayConnection(url, self.gateway_token)
        except ValueError:
            return None

    def has_capability(self, name: str) -> bool:
        capabilities = getattr(self.last_status, "capabilities", ()) if self.last_status else ()
        return any(isinstance(item, dict) and item.get("name") == name for item in capabilities)

    def identity(self) -> str:
        return workspace_id(Path(self.workspace_path))

    def workspace_ready(self, status: object | None = None) -> bool:
        status = self.last_status if status is None else status
        if not self.workspace_path or self.state not in {"ready", "manual"} or status is None:
            return False
        try:
            identity = self.identity()
        except BrokerError:
            return False
        supported = set(getattr(status, "supported_modes", ()))
        return bool(
            getattr(status, "ready", False)
            and getattr(status, "executor_present", False)
            and getattr(status, "executor_status", "") == "ready"
            and getattr(status, "active_workspace", None) == identity
            and {"plan", "agent"}.issubset(supported)
        )

    def mode_available(self, mode: str) -> bool:
        """Whether the gateway reports ``mode`` ready for the selected workspace."""
        status = self.last_status
        supported = tuple(getattr(status, "supported_modes", ())) if status else ()
        active = getattr(status, "active_workspace", None) if status else None
        return mode in supported and active == self.identity()

    def resumable_runs(self) -> list[str]:
        return list(getattr(self.last_status, "resumable_runs", ())) if self.last_status else []

    def status_report(self) -> str:
        status = self.last_status
        if status is None:
            return self.gateway_detail or "Gateway is offline."
        return (
            f"Gateway {status.gateway_version} · protocol {status.protocol_version}\n"
            f"Modes: {', '.join(status.supported_modes)}\n"
            f"Capabilities: {', '.join(str(item.get('name') or '') for item in status.capabilities)}\n"
            f"Resumable runs: {len(status.resumable_runs)}"
        )

    def show_status(self) -> None:
        self.host.infoRequested.emit("Gateway status", self.status_report())

    # -- workspace and gateway configuration --------------------------------------------

    def select_workspace(self, value: str) -> None:
        local = QUrl(value).toLocalFile() if value.startswith("file:") else value
        if not local:
            return
        try:
            path = canonical_workspace(Path(local))
        except BrokerError as exc:
            self.host.errorRequested.emit("Unsafe workspace", str(exc))
            return
        self.workspace_path = str(path)
        self.host.generation.reset_for_workspace_change()
        self.storage.set_setting("workspace_path", self.workspace_path)
        self.target_identity = workspace_id(path)
        self.state = "starting" if self.automatic else "manual"
        self.detail = "Preparing the selected workspace…" if self.automatic else "Workspace selected. The developer runtime is managed by scripts."
        self.last_status = None
        self.host.settings.catalog_autorefresh_attempted = False
        self.host.settings.reload_skills()
        self.host.stateChanged.emit()
        self.host.set_status("Preparing workspace…" if self.automatic else "Workspace selected; checking developer runtime…")
        if self.automatic:
            try:
                self.manager.ensure(path)
            except (OSError, RuntimeError, ValueError) as exc:
                self._runtime_failed(str(exc))
        else:
            self.request_check()

    def retry(self) -> None:
        if not self.automatic:
            self.host.errorRequested.emit(
                "Developer runtime is managed manually",
                "Run .\\scripts\\dev-up.ps1 for the selected workspace, then check the gateway again.",
            )
            return
        workspace = Path(self.workspace_path) if self.workspace_path else None
        try:
            if workspace is not None:
                workspace = canonical_workspace(workspace)
            self.target_identity = workspace_id(workspace) if workspace else None
            self.manager.ensure(workspace)
        except (BrokerError, OSError, RuntimeError, ValueError) as exc:
            self._runtime_failed(str(exc))

    def save_gateway(self, url: str, token: str) -> bool:
        try:
            new_token = token.strip()
            connection = GatewayConnection(url.strip() or DEFAULT_GATEWAY_URL, new_token or self.gateway_token)
            self.storage.set_setting("gateway_url", connection.base_url)
            if new_token:
                save_gateway_token(new_token)
                self.gateway_token = new_token
        except Exception as exc:
            self.host.errorRequested.emit("Invalid gateway settings", str(exc))
            return False
        return True

    # -- health ------------------------------------------------------------------------

    def request_check(self) -> None:
        """Check the gateway now, or right after an in-flight check finishes."""
        if self.health_worker:
            self._recheck_requested = True
        else:
            self.check_gateway()

    @Slot()
    def check_gateway(self) -> None:
        if self.health_worker or self.closing:
            return
        session_token = get_gateway_session_token()
        if session_token and session_token != self.gateway_token:
            self.gateway_token = session_token
            self.host.settingsChanged.emit()
        connection = self.connection()
        if connection is None:
            self.apply_health(HealthResult(HealthState.DISCONNECTED, "Gateway token not configured"))
            return
        worker = GatewayHealthWorker(connection)
        worker.complete.connect(self._health_complete)
        worker.finished.connect(self._health_finished)
        self.health_worker = worker
        worker.start()

    @Slot(object)
    def _health_complete(self, result: object) -> None:
        if isinstance(result, HealthResult):
            self.apply_health(result)

    @Slot()
    def _health_finished(self) -> None:
        if self.health_worker:
            self.health_worker.deleteLater()
        self.health_worker = None
        if self._recheck_requested and not self.closing:
            self._recheck_requested = False
            QTimer.singleShot(0, self.check_gateway)

    def apply_health(self, result: HealthResult) -> None:
        if self.busy:
            return
        self.health_state = result.state
        self.last_status = result.status
        if self.state == "failed":
            self.gateway_text = "Runtime: setup failed"
            self.gateway_detail = self.detail
            self.host.stateChanged.emit()
            return
        self.gateway_text = HEALTH_LABELS[result.state]
        self.gateway_detail = result.message
        if result.state == HealthState.READY and self.workspace_path and not self.workspace_ready(result.status):
            self.gateway_text = "Executor: unavailable"
            self.gateway_detail = "The isolated executor is not ready for this workspace. Retry the local runtime setup."
        self.host.stateChanged.emit()
        if result.state in {HealthState.READY, HealthState.DEGRADED}:
            self.host.settings.refresh_catalog_if_stale()

    # -- runtime manager -----------------------------------------------------------------

    @Slot()
    def _start_for_saved_workspace(self) -> None:
        workspace: Path | None = None
        if self.workspace_path:
            try:
                workspace = canonical_workspace(Path(self.workspace_path))
            except BrokerError:
                self.workspace_path = ""
                self.storage.set_setting("workspace_path", "")
                self.host.settings.reload_skills()
            else:
                self.workspace_path = str(workspace)
                self.storage.set_setting("workspace_path", self.workspace_path)
        self.target_identity = workspace_id(workspace) if workspace else None
        try:
            self.manager.ensure(workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            self._runtime_failed(str(exc))

    @Slot(str, str)
    def _runtime_progress(self, state: str, message: str) -> None:
        self.state = state
        self.detail = message
        self.gateway_text = RUNTIME_LABELS.get(state, "Runtime: working")
        self.gateway_detail = message
        self.last_status = None
        self.host.stateChanged.emit()

    @Slot(object)
    def _runtime_ready(self, result: object) -> None:
        if not isinstance(result, RuntimeResult):
            self._runtime_failed("The local runtime returned an invalid readiness result.")
            return
        if result.target.workspace_identity != self.target_identity:
            return
        self.gateway_token = result.gateway_token
        self.state = "ready"
        self.detail = "Local runtime started; checking gateway and executor health…"
        self.gateway_text = "Gateway: checking"
        self.gateway_detail = self.detail
        self.last_status = None
        self.host.settingsChanged.emit()
        self.host.stateChanged.emit()
        if self.health_worker:
            self._recheck_requested = True
        else:
            QTimer.singleShot(0, self.check_gateway)

    @Slot(str)
    def _runtime_failed(self, message: str) -> None:
        self.state = "failed"
        self.detail = message or "Local runtime setup failed."
        self.gateway_text = "Runtime: setup failed"
        self.gateway_detail = self.detail
        self.last_status = None
        self.host.stateChanged.emit()
        self.host.errorRequested.emit("Local runtime setup failed", self.detail)

    def shutdown(self) -> None:
        self.closing = True
        self.health_timer.stop()
        if self.health_worker:
            self.health_worker.wait(500)
        if self.automatic:
            self.manager.shutdown()
