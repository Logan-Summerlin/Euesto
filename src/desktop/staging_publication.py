from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot

from shared.tools import PublishManifest

from ..gateway_client import GatewayClient
from ..settings import app_data_dir
from ..workers import PublicationWorker, StagingDiscardWorker, StagingInspectWorker
from ..workspace_broker import BrokerError
from .host import BridgeHost


class StagingPublicationService(QObject):
    """Staged-change review and discard, and reviewed, batched publication to the host.

    Every publication is an exact, hash-checked manifest applied by the desktop broker. A
    multi-batch publication keeps the batch in flight and the next batch awaiting approval;
    Auto sessions continue automatically, everything else is confirmed batch by batch.
    """

    def __init__(self, host: BridgeHost, *, recovery_root: Callable[[], Path] | None = None):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self._recovery_root = recovery_root or (lambda: app_data_dir() / "recovery")
        self.publication_worker: PublicationWorker | None = None
        self.discard_worker: StagingDiscardWorker | None = None
        self.inspect_worker: StagingInspectWorker | None = None
        # Multi-batch publication: the batch in flight and the next batch awaiting its turn.
        self.active_publication: tuple[PublishManifest, bool] | None = None
        self.next_publication: tuple[PublishManifest, bool, GatewayClient | None] | None = None
        self._continuation_clients: dict[str, GatewayClient] = {}

    @property
    def busy(self) -> bool:
        return any((self.discard_worker, self.inspect_worker, self.publication_worker))

    @property
    def publishing(self) -> bool:
        return self.publication_worker is not None

    # -- publication -------------------------------------------------------------------

    def handle_manifest(self, payload: object, *, token: str, auto: bool, auto_authorized: bool) -> None:
        """Publish or ask to publish a manifest announced by an agent run."""
        try:
            manifest = PublishManifest.from_dict(payload)  # type: ignore[arg-type]
            if auto:
                if not auto_authorized:
                    raise ValueError("Auto publication is not authorized by this desktop session")
                self.start_publication(manifest, auto=True)
            else:
                self.confirm_publication(manifest, token)
        except (TypeError, ValueError) as exc:
            self.host.generation.stop_auto()
            self.host.stateChanged.emit()
            self.host.errorRequested.emit("Publication blocked", str(exc))

    def start_publication(self, value: object, *, auto: bool, client: GatewayClient | None = None) -> None:
        try:
            manifest = value if isinstance(value, PublishManifest) else PublishManifest.from_dict(value)  # type: ignore[arg-type]
            workspace = self.host.runtime.workspace_path
            if not workspace:
                raise BrokerError("Select the workspace that produced this manifest first")
            if self.publication_worker:
                raise BrokerError("Another publication is already running")
            connection = self.host.runtime.connection() if auto or client is None and manifest.batch_count > 1 else None
            if auto and connection is None and client is None:
                raise BrokerError("Auto publication requires the local gateway")
            worker = PublicationWorker(manifest, Path(workspace), self._recovery_root(), reseed_client=client or (GatewayClient(connection) if connection else None))
            self.publication_worker = worker
            self.active_publication = (manifest, auto)
            worker.complete.connect(self._on_publication_complete)
            worker.failed.connect(self._on_publication_failed)
            worker.finished.connect(self._on_publication_finished)
            position = f" (batch {manifest.batch_index} of {manifest.batch_count})" if manifest.batch_count > 1 else ""
            self.host.set_status(f"Publishing validated staged changes{position}…")
            worker.start()
        except (BrokerError, OSError, TypeError, ValueError) as exc:
            if auto:
                self.host.generation.stop_auto()
                self.host.stateChanged.emit()
            self.host.errorRequested.emit("Publication blocked", str(exc))

    def confirm_publication(self, manifest: PublishManifest, token: str, *, detail: str = "") -> None:
        if manifest.batch_count > 1:
            title = f"Publish batch {manifest.batch_index} of {manifest.batch_count}?"
            body = (
                f"{detail}This batch contains {len(manifest.operations)} file operation(s). Large changesets are "
                "published as separately approved, hash-checked batches; each batch is written all-or-nothing."
            )
        else:
            title = "Publish staged changes to the host?"
            body = (
                f"The approved staging checkpoint contains {len(manifest.operations)} file operation(s). "
                "Confirm to write the exact, hash-checked manifest to the selected workspace."
            )

        def accept() -> None:
            client = self._continuation_clients.pop(manifest.manifest_id, None)
            self.start_publication(manifest, auto=False, client=client)

        self.host.confirm(token, title, body, accept)

    @Slot(dict)
    def _on_publication_complete(self, result: dict[str, Any]) -> None:
        completed = result.get("completed_paths") or ()
        checkpoint = str(result.get("checkpoint_id") or "")
        batch = f" (batch {result['batch_index']} of {result['batch_count']})" if int(result.get("batch_count") or 1) > 1 else ""
        self.host.set_status(f"Published {len(completed)} file(s){batch}; checkpoint {checkpoint[:8]}")
        if result.get("reseed_error"):
            self.host.generation.stop_auto()
            progress = f" {result['progress']}" if result.get("progress") else ""
            self.host.errorRequested.emit(
                "Published, but Auto stopped",
                "Host files were updated, but staging could not be reseeded: " + str(result["reseed_error"]) + progress,
            )
        elif result.get("next_batch_error"):
            self.host.generation.stop_auto()
            self.host.errorRequested.emit(
                "Publication paused",
                f"Batch {result.get('batch_index')} was published, but the next batch could not be prepared: "
                f"{result['next_batch_error']} {result.get('progress') or ''}".strip(),
            )
        elif result.get("next_manifest"):
            try:
                following = PublishManifest.from_dict(result["next_manifest"])
            except (TypeError, ValueError) as exc:
                self.host.generation.stop_auto()
                self.host.errorRequested.emit("Publication blocked", str(exc))
            else:
                auto = bool(self.active_publication and self.active_publication[1] and self.host.generation.auto_mode)
                client = self.publication_worker.continuation_client if self.publication_worker else None
                self.next_publication = (following, auto, client)
        self.host.stateChanged.emit()

    @Slot(str)
    def _on_publication_failed(self, message: str) -> None:
        self.host.generation.stop_auto()
        self.host.stateChanged.emit()
        self.host.errorRequested.emit("Publication blocked", message)
        active = self.active_publication
        if active and active[0].batch_count > 1:
            # The failed batch was rolled back on the host; earlier batches stay published.
            # Offer to retry the same reviewed batch, which resumes the remainder.
            manifest = active[0]
            self.confirm_publication(
                manifest,
                f"publish-retry:{manifest.publication_id}:{manifest.batch_index}",
                detail=f"Batch {manifest.batch_index} of {manifest.batch_count} failed and was rolled back. ",
            )

    @Slot()
    def _on_publication_finished(self) -> None:
        if self.publication_worker:
            self.publication_worker.deleteLater()
        self.publication_worker = None
        self.active_publication = None
        self.host.stateChanged.emit()
        queued, self.next_publication = self.next_publication, None
        if queued is not None:
            manifest, auto, client = queued
            if auto:
                self.start_publication(manifest, auto=True, client=client)
            else:
                if client is not None:
                    self._continuation_clients[manifest.manifest_id] = client
                self.confirm_publication(
                    manifest,
                    f"publish:{manifest.publication_id}:{manifest.batch_index}",
                    detail=f"Batch {manifest.batch_index - 1} of {manifest.batch_count} is published. ",
                )
            return
        if not self.host.generation.running:
            self.host.generation.continue_queued_input()

    # -- staging review and discard ----------------------------------------------------

    def request_discard(self) -> None:
        workspace = self.host.runtime.workspace_path
        if self.host.generation.running or self.discard_worker or not workspace:
            return
        self.host.confirm(
            f"discard-staging:{workspace}",
            "Discard staged changes?",
            "This deletes the private staged copy and reseeds it from the selected source workspace. "
            "It does not change host files, but staged edits will be lost.",
            self._discard,
        )

    def _discard(self) -> None:
        connection = self.host.runtime.connection()
        if not connection or not self.host.runtime.workspace_path:
            self.host.errorRequested.emit("Workspace runtime required", "Select a ready workspace first.")
            return
        worker = StagingDiscardWorker(GatewayClient(connection), self.host.runtime.identity())
        self.discard_worker = worker
        worker.complete.connect(self._on_discarded)
        worker.failed.connect(self._on_discard_failed)
        worker.finished.connect(self._on_discard_finished)
        self.host.set_status("Discarding staged changes and reseeding workspace…")
        worker.start()

    @Slot(dict)
    def _on_discarded(self, result: dict[str, Any]) -> None:
        self.host.set_status("Staging reseeded" + (f" · {int(result.get('file_count') or 0):,} files" if result else ""))

    @Slot(str)
    def _on_discard_failed(self, message: str) -> None:
        self.host.errorRequested.emit("Could not discard staging", message)

    @Slot()
    def _on_discard_finished(self) -> None:
        if self.discard_worker:
            self.discard_worker.deleteLater()
        self.discard_worker = None
        self.host.stateChanged.emit()

    def review(self) -> None:
        if self.host.generation.running or self.busy or not self.host.runtime.workspace_path:
            return
        connection = self.host.runtime.connection()
        if not connection:
            self.host.errorRequested.emit("Workspace runtime required", "Select a ready workspace first.")
            return
        worker = StagingInspectWorker(GatewayClient(connection), self.host.runtime.identity())
        self.inspect_worker = worker
        worker.complete.connect(self._on_inspected)
        worker.failed.connect(self._on_inspect_failed)
        worker.finished.connect(self._on_inspect_finished)
        self.host.set_status("Reviewing staged changes…")
        worker.start()

    @Slot(dict)
    def _on_inspected(self, result: dict[str, Any]) -> None:
        self.host.infoRequested.emit("Staged workspace review", describe_staged_changes(result))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        self.host.set_status(f"Reviewed {len(changes)} staged change(s)")

    @Slot(str)
    def _on_inspect_failed(self, message: str) -> None:
        self.host.errorRequested.emit("Could not review staging", message)

    @Slot()
    def _on_inspect_finished(self) -> None:
        if self.inspect_worker:
            self.inspect_worker.deleteLater()
        self.inspect_worker = None
        self.host.stateChanged.emit()


def describe_staged_changes(result: dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    changes = data.get("changes") if isinstance(data.get("changes"), list) else []
    lines = [f"Staged changes: {len(changes)} shown"]
    lines.extend(f"{item.get('operation', '?')}: {item.get('path', '?')}" for item in changes[:500] if isinstance(item, dict))
    if data.get("truncated"):
        lines.append("More changes are available through the bounded cursor.")
    return "\n".join(lines)
