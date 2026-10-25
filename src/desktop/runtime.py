from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot

from shared.responses import GatewayStatus

from ..connection import GatewayHealthWorker, HealthResult, HealthState
from ..gateway_client import DEFAULT_GATEWAY_URL, GatewayClient, GatewayConnection, GatewayError
from ..runtime_manager import RuntimeManager, RuntimeResult
from ..settings import (
    app_data_dir,
    get_gateway_session_token,
    get_gateway_token,
    save_gateway_token,
)
from ..storage import Storage
from ..workers import CallWorker
from ..workspace_broker import BrokerError, canonical_workspace, workspace_id
from .conversations import local_path
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

    def __init__(
        self,
        host: BridgeHost,
        storage: Storage,
        *,
        automatic: bool | None = None,
        manager: RuntimeManager | None = None,
    ):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.manager = manager or RuntimeManager(app_data_dir(), self)
        self.automatic = bool(getattr(sys, "frozen", False)) if automatic is None else automatic
        self.workspace_path = self.storage.get_setting("workspace_path", "") or ""
        self.gateway_text = "Gateway: checking"
        self.gateway_detail = ""
        self.last_status: GatewayStatus | None = None
        self.state = "starting" if self.automatic else "manual"
        self.detail = (
            "Preparing the local runtime…"
            if self.automatic
            else "The developer runtime is managed by scripts."
        )
        self.target_identity: str | None = None
        # The agent phase's network mode (plan 5.3): ``registries`` only for an Agent session
        # that opted in; it never outlives the workspace or the app.
        self.network_mode = "none"
        self.health_worker: GatewayHealthWorker | None = None
        self.gateway_token = get_gateway_session_token() or get_gateway_token() or ""
        self.closing = False
        self._recheck_requested = False
        # Gateway notices (a migration disabling broad saved rules, say) shown once each.
        self._shown_notices: set[str] = set()
        # Persistent staged sessions Docker holds, for the Settings retention list (2.4).
        self.staged_sessions: list[dict[str, object]] = []
        self.sessions_worker: CallWorker | None = None
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
        QTimer.singleShot(
            0, self._start_for_saved_workspace if self.automatic else self.check_gateway
        )

    # -- queries -------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.state in BUSY_RUNTIME_STATES

    def connection(self) -> GatewayConnection | None:
        if not self.gateway_token:
            return None
        url = (
            self.storage.get_setting("gateway_url", DEFAULT_GATEWAY_URL) or DEFAULT_GATEWAY_URL
        )
        try:
            return GatewayConnection(url, self.gateway_token)
        except ValueError:
            return None

    def has_capability(self, name: str) -> bool:
        capabilities = self.last_status.capabilities if self.last_status else ()
        return any(item.get("name") == name for item in capabilities)

    def identity(self) -> str:
        return workspace_id(Path(self.workspace_path))

    def workspace_ready(self, status: GatewayStatus | None = None) -> bool:
        status = self.last_status if status is None else status
        if not self.workspace_path or self.state not in {"ready", "manual"} or status is None:
            return False
        try:
            return status.serves_workspace(self.identity())
        except BrokerError:
            return False

    def mode_available(self, mode: str) -> bool:
        """Whether the gateway reports ``mode`` ready for the selected workspace."""
        status = self.last_status
        return bool(
            status
            and mode in status.supported_modes
            and status.active_workspace == self.identity()
        )

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

    def is_current_workspace(self, value: str) -> bool:
        """Whether ``value`` names the workspace already selected (no staged work at risk)."""
        local = local_path(value)
        if local is None or not self.workspace_path:
            return False
        try:
            return str(canonical_workspace(local)) == self.workspace_path
        except BrokerError:
            return False

    def select_workspace(self, value: str) -> None:
        local = local_path(value)
        if local is None:
            return
        try:
            path = canonical_workspace(local)
        except BrokerError as exc:
            self.host.errorRequested.emit("Unsafe workspace", str(exc))
            return
        self.workspace_path = str(path)
        self.network_mode = "none"
        self.host.generation.reset_for_workspace_change()
        self.storage.set_setting("workspace_path", self.workspace_path)
        self.target_identity = workspace_id(path)
        self.state = "starting" if self.automatic else "manual"
        self.detail = (
            "Preparing the selected workspace…"
            if self.automatic
            else "Workspace selected. The developer runtime is managed by scripts."
        )
        self.last_status = None
        self.host.settings.catalog_autorefresh_attempted = False
        self.host.settings.reload_skills()
        self.host.policy.refresh()
        self.host.stateChanged.emit()
        self.host.set_status(
            "Preparing workspace…"
            if self.automatic
            else "Workspace selected; checking developer runtime…"
        )
        if self.automatic:
            self._ensure_runtime(path)
        else:
            self.request_check()

    def retry(self) -> None:
        if not self.automatic:
            self.host.errorRequested.emit(
                "Developer runtime is managed manually",
                "Run .\\scripts\\dev-up.ps1 for the selected workspace, then check the gateway again.",
            )
            return
        try:
            workspace = (
                canonical_workspace(Path(self.workspace_path)) if self.workspace_path else None
            )
        except BrokerError as exc:
            self._runtime_failed(str(exc))
            return
        self._ensure_runtime(workspace)

    def set_network_mode(self, mode: str) -> None:
        """Restart the workspace runtime with or without registry access (staging is kept)."""
        if mode not in {"none", "registries"} or mode == self.network_mode:
            return
        self.network_mode = mode
        self.host.stateChanged.emit()
        self.retry()

    def restart_for_policy(self) -> None:
        """Apply a confirmed policy that changes the runtime (image, hosts, secret names,
        withheld paths) now, or say when it applies if work is in flight."""
        if not self.automatic:
            self.host.set_status(
                "Workspace policy confirmed; restart the developer runtime to apply it"
            )
        elif self.host.generation.running or self.host.staging.busy:
            self.host.set_status(
                "Workspace policy confirmed; it applies when the runtime next restarts"
            )
        else:
            self.host.set_status("Workspace policy confirmed; restarting the runtime…")
            self.retry()

    def save_gateway(self, url: str, token: str) -> bool:
        try:
            new_token = token.strip()
            connection = GatewayConnection(
                url.strip() or DEFAULT_GATEWAY_URL, new_token or self.gateway_token
            )
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
            self.apply_health(
                HealthResult(HealthState.DISCONNECTED, "Gateway token not configured")
            )
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
        self.last_status = result.status
        if self.state == "failed":
            self.gateway_text = "Runtime: setup failed"
            self.gateway_detail = self.detail
            self.host.stateChanged.emit()
            return
        self.gateway_text = HEALTH_LABELS[result.state]
        self.gateway_detail = result.message
        if (
            result.state == HealthState.READY
            and self.workspace_path
            and not self.workspace_ready(result.status)
        ):
            self.gateway_text = "Executor: unavailable"
            self.gateway_detail = "The isolated executor is not ready for this workspace. Retry the local runtime setup."
        self.host.stateChanged.emit()
        if result.state in {HealthState.READY, HealthState.DEGRADED}:
            self.host.settings.refresh_catalog_if_stale()
            self._show_notices(result.status)
        if result.state == HealthState.READY and self.workspace_ready(result.status):
            self.host.policy.offer_setup()

    def _show_notices(self, status: GatewayStatus | None) -> None:
        """Show each unacknowledged gateway notice once, then acknowledge it."""
        notices = status.notices if status else ()
        connection = self.connection()
        for notice in notices:
            notice_id = str(notice.get("notice_id") or "")
            message = str(notice.get("message") or "")
            if not notice_id or not message or notice_id in self._shown_notices:
                continue
            self._shown_notices.add(notice_id)
            self.host.infoRequested.emit("Notice", message)
            if connection is not None:
                try:
                    GatewayClient(connection).acknowledge_notice(notice_id)
                except GatewayError:
                    pass

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
        self._ensure_runtime(workspace)

    def _ensure_runtime(self, workspace: Path | None) -> None:
        self.target_identity = workspace_id(workspace) if workspace else None
        try:
            settings = self.host.policy.runtime_settings(
                self.host.settings.runtime_settings(),
                network_mode=self.network_mode if workspace else "none",
            )
            self.manager.ensure(workspace, settings=settings)
        except (OSError, RuntimeError, ValueError) as exc:
            self._runtime_failed(str(exc))

    def _set_runtime_state(self, state: str, detail: str, gateway_text: str) -> None:
        self.state = state
        self.detail = detail
        self.gateway_text = gateway_text
        self.gateway_detail = detail
        self.last_status = None

    @Slot(str, str)
    def _runtime_progress(self, state: str, message: str) -> None:
        self._set_runtime_state(state, message, RUNTIME_LABELS.get(state, "Runtime: working"))
        self.host.stateChanged.emit()

    @Slot(object)
    def _runtime_ready(self, result: object) -> None:
        if not isinstance(result, RuntimeResult):
            self._runtime_failed("The local runtime returned an invalid readiness result.")
            return
        if result.target.workspace_identity != self.target_identity:
            return
        self.gateway_token = result.gateway_token
        self._set_runtime_state(
            "ready",
            "Local runtime started; checking gateway and executor health…",
            "Gateway: checking",
        )
        self.host.settingsChanged.emit()
        self.host.policy.refresh()
        self.host.stateChanged.emit()
        if self.health_worker:
            self._recheck_requested = True
        else:
            QTimer.singleShot(0, self.check_gateway)

    @Slot(str)
    def _runtime_failed(self, message: str) -> None:
        self._set_runtime_state(
            "failed", message or "Local runtime setup failed.", "Runtime: setup failed"
        )
        self.host.stateChanged.emit()
        self.host.errorRequested.emit("Local runtime setup failed", self.detail)

    # -- staged sessions (plan 2.4 retention) -------------------------------------------

    def refresh_staged_sessions(self) -> None:
        if self.sessions_worker or not self.automatic:
            return
        retention = self.host.settings.retention_days()
        self._run_sessions(
            lambda: self.manager.staged_sessions(retention), "Could not list staged sessions"
        )

    def request_remove_staged_session(self, key: str) -> None:
        session = next((item for item in self.staged_sessions if item.get("key") == key), None)
        if session is None or self.sessions_worker:
            return
        self.host.confirm(
            f"remove-staged-session:{key}",
            "Remove this staged session?",
            f"This deletes the staged copy, state, and caches kept for {session.get('workspace') or key}. "
            "Host files are not changed, but any unpublished staged edits there are lost.",
            lambda: self._run_sessions(
                lambda: self._remove_and_list(key), "Could not remove the staged session"
            ),
        )

    def _remove_and_list(self, key: str) -> list[dict[str, object]]:
        self.manager.remove_staged_session(key)
        return self.manager.staged_sessions(self.host.settings.retention_days())

    def _run_sessions(self, action: Callable[[], object], unexpected: str) -> None:
        worker = CallWorker(action, unexpected)
        worker.complete.connect(self._sessions_listed)
        worker.failed.connect(
            lambda message: self.host.errorRequested.emit("Staged sessions", message)
        )
        worker.finished.connect(self._sessions_finished)
        self.sessions_worker = worker
        worker.start()

    @Slot(object)
    def _sessions_listed(self, sessions: object) -> None:
        self.staged_sessions = [item for item in sessions or () if isinstance(item, dict)]
        self.host.settingsChanged.emit()

    @Slot()
    def _sessions_finished(self) -> None:
        if self.sessions_worker:
            self.sessions_worker.deleteLater()
        self.sessions_worker = None

    def shutdown(self) -> None:
        self.closing = True
        self.health_timer.stop()
        if self.health_worker:
            self.health_worker.wait(500)
        if self.automatic:
            self.manager.shutdown()
