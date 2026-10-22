from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Slot

from shared.publication_risk import (
    FLAG_LABELS,
    classify_manifest,
    needs_confirmation,
    review_order,
)
from shared.tools import PublishManifest

from ..approval_display import publication_summary
from ..gateway_client import GatewayClient
from ..settings import app_data_dir
from ..workers import PublicationWorker, StagingWorker
from ..workspace_broker import BrokerError
from .host import BridgeHost


class StagingPublicationService(QObject):
    """Staged-change review and discard, and reviewed, batched publication to the host.

    Every publication is an exact, hash-checked manifest applied by the desktop broker. A
    multi-batch publication keeps the batch in flight and the next batch awaiting approval;
    Auto sessions continue automatically, everything else is confirmed batch by batch.

    Staging persists across sessions (plan 2.4), so this service also publishes staged work
    kept from an earlier session, rebases staging onto host changes, resolves conflicts, and
    guards closing the app or switching workspace so staged work is never lost silently (0.6).

    The review pane (4.4) lists each staged change with its diff and risk flags (4.3); the
    user publishes all of it or a selected subset, and the rest stays staged. Declared checks
    (3.4) run from the pane against the current staged snapshot. Auto never publishes a batch
    that changes host-executable files, dependencies, or the workspace policy without explicit
    confirmation, nor before the policy's required checks pass.
    """

    def __init__(self, host: BridgeHost, *, recovery_root: Callable[[], Path] | None = None):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self._recovery_root = recovery_root or (lambda: app_data_dir() / "recovery")
        self.publication_worker: PublicationWorker | None = None
        self.discard_worker: StagingWorker | None = None
        self.inspect_worker: StagingWorker | None = None
        # Publishing kept work, rebasing, or checking staging before leaving the workspace.
        self.action_worker: StagingWorker | None = None
        self._pending_leave: Callable[[], None] | None = None
        # Open drift conflicts from the latest inspection or rebase, shown for resolution.
        self.conflicts: list[dict[str, Any]] = []
        # Multi-batch publication: the batch in flight and the next batch awaiting its turn.
        self.active_publication: tuple[PublishManifest, bool] | None = None
        self.next_publication: tuple[PublishManifest, bool, GatewayClient | None] | None = None
        self._continuation_clients: dict[str, GatewayClient] = {}
        # The review pane: the latest review, the selected paths, and the running check.
        self.review_data: dict[str, Any] | None = None
        self.review_selection: set[str] = set()
        self.review_worker: StagingWorker | None = None
        self.check_worker: StagingWorker | None = None
        self.running_check = ""
        # The path selection of the partial publication in flight; later batches keep it.
        self.publication_paths: tuple[str, ...] | None = None

    @property
    def busy(self) -> bool:
        return any(
            (
                self.discard_worker,
                self.inspect_worker,
                self.publication_worker,
                self.action_worker,
                self.check_worker,
            )
        )

    @property
    def publishing(self) -> bool:
        return self.publication_worker is not None

    # -- publication -------------------------------------------------------------------

    def handle_manifest(
        self, payload: object, *, token: str, auto: bool, auto_authorized: bool
    ) -> None:
        """Publish or ask to publish a manifest announced by an agent run."""
        try:
            manifest = PublishManifest.from_dict(payload)  # type: ignore[arg-type]
            if auto:
                if not auto_authorized:
                    raise ValueError(
                        "Auto publication is not authorized by this desktop session"
                    )
                self.start_publication(manifest, auto=True)
            else:
                self.confirm_publication(manifest, token)
        except (TypeError, ValueError) as exc:
            self.host.generation.stop_auto()
            self.host.stateChanged.emit()
            self.host.errorRequested.emit("Publication blocked", str(exc))

    def start_publication(
        self, value: object, *, auto: bool, client: GatewayClient | None = None
    ) -> None:
        try:
            manifest = (
                value
                if isinstance(value, PublishManifest)
                else PublishManifest.from_dict(value)
            )  # type: ignore[arg-type]
            workspace = self.host.runtime.workspace_path
            if not workspace:
                raise BrokerError("Select the workspace that produced this manifest first")
            if self.publication_worker:
                raise BrokerError("Another publication is already running")
            if client is None:
                connection = self.host.runtime.connection()
                client = GatewayClient(connection) if connection else None
            if auto and client is None:
                raise BrokerError("Auto publication requires the local gateway")
            if auto and needs_confirmation(self.risk_flags(manifest)):
                # D5: Auto stops at host-executable, dependency, and policy changes.
                self.host.set_status(
                    "Auto paused: this batch changes files that need your confirmation"
                )
                self.confirm_publication(
                    manifest,
                    f"publish-flagged:{manifest.publication_id}:{manifest.batch_index}",
                    detail="Auto stopped here because this batch changes files the host runs, "
                    "dependencies, or the workspace policy. ",
                    client=client,
                )
                return
            policy = self.host.policy.effective()
            worker = PublicationWorker(
                manifest,
                Path(workspace),
                self._recovery_root(),
                reseed_client=client,
                paths=self.publication_paths,
                required_checks=policy.verify_before_publish if auto else (),
                never_publish=policy.never_publish,
            )
            self.publication_worker = worker
            self.active_publication = (manifest, auto)
            worker.complete.connect(self._on_publication_complete)
            worker.failed.connect(self._on_publication_failed)
            worker.finished.connect(self._on_publication_finished)
            position = (
                f" (batch {manifest.batch_index} of {manifest.batch_count})"
                if manifest.batch_count > 1
                else ""
            )
            self.host.set_status(f"Publishing validated staged changes{position}…")
            worker.start()
        except (BrokerError, OSError, TypeError, ValueError) as exc:
            if auto:
                self.host.generation.stop_auto()
                self.host.stateChanged.emit()
            self.host.errorRequested.emit("Publication blocked", str(exc))

    def confirm_publication(
        self,
        manifest: PublishManifest,
        token: str,
        *,
        detail: str = "",
        client: GatewayClient | None = None,
    ) -> None:
        if client is not None:
            self._continuation_clients[manifest.manifest_id] = client
        if manifest.batch_count > 1:
            title = f"Publish batch {manifest.batch_index} of {manifest.batch_count}?"
            body = (
                f"{detail}This batch contains {len(manifest.operations)} file operation(s). Large changesets are "
                "published as separately approved, hash-checked batches; each batch is written all-or-nothing."
            )
        else:
            title = "Publish staged changes to the host?"
            body = (
                f"{detail}The approved staging checkpoint contains {len(manifest.operations)} file operation(s). "
                "Confirm to write the exact, hash-checked manifest to the selected workspace."
            )
        body += "\n\n" + publication_summary(manifest.to_dict(), base_reader=self.base_reader())
        required = self.host.policy.effective().verify_before_publish
        if required:
            body += (
                "\n\nThe workspace policy requires these checks before publishing: "
                + ", ".join(required)
                + ". Run them from the review pane; Auto does not publish until they pass."
            )

        def accept() -> None:
            client = self._continuation_clients.pop(manifest.manifest_id, None)
            self.start_publication(manifest, auto=False, client=client)

        self.host.confirm(token, title, body, accept)

    @Slot(dict)
    def _on_publication_complete(self, result: dict[str, Any]) -> None:
        completed = result.get("completed_paths") or ()
        checkpoint = str(result.get("checkpoint_id") or "")
        batch = (
            f" (batch {result['batch_index']} of {result['batch_count']})"
            if int(result.get("batch_count") or 1) > 1
            else ""
        )
        self.host.set_status(
            f"Published {len(completed)} file(s){batch}; checkpoint {checkpoint[:8]}"
        )
        if result.get("reseed_error"):
            self.host.generation.stop_auto()
            progress = f" {result['progress']}" if result.get("progress") else ""
            self.host.errorRequested.emit(
                "Published, but Auto stopped",
                "Host files were updated, but staging could not be reseeded: "
                + str(result["reseed_error"])
                + progress,
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
                auto = bool(
                    self.active_publication
                    and self.active_publication[1]
                    and self.host.generation.auto_mode
                )
                client = (
                    self.publication_worker.continuation_client
                    if self.publication_worker
                    else None
                )
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
        if queued is None:
            self.publication_paths = None
            # A published policy change only takes effect once confirmed (4.6).
            self.host.policy.refresh()
            if self.review_data is not None:
                QTimer.singleShot(0, self.open_review)
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

    def _discard(self, then: Callable[[], None] | None = None) -> None:
        connection = self.host.runtime.connection()
        if not connection or not self.host.runtime.workspace_path:
            self.host.errorRequested.emit(
                "Workspace runtime required", "Select a ready workspace first."
            )
            return
        worker = StagingWorker(
            GatewayClient(connection), self.host.runtime.identity(), "discard"
        )
        self.discard_worker = worker
        worker.complete.connect(lambda result: self._on_discarded(result, then))
        worker.failed.connect(self._on_discard_failed)
        worker.finished.connect(self._on_discard_finished)
        self.host.set_status("Discarding staged changes and reseeding workspace…")
        worker.start()

    def _on_discarded(
        self, result: dict[str, Any], then: Callable[[], None] | None = None
    ) -> None:
        self.host.set_status(
            "Staging reseeded"
            + (f" · {int(result.get('file_count') or 0):,} files" if result else "")
        )
        if then is not None:
            QTimer.singleShot(0, then)

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
            self.host.errorRequested.emit(
                "Workspace runtime required", "Select a ready workspace first."
            )
            return
        worker = StagingWorker(
            GatewayClient(connection), self.host.runtime.identity(), "inspect"
        )
        self.inspect_worker = worker
        worker.complete.connect(self._on_inspected)
        worker.failed.connect(self._on_inspect_failed)
        worker.finished.connect(self._on_inspect_finished)
        self.host.set_status("Reviewing staged changes…")
        worker.start()

    @Slot(dict)
    def _on_inspected(self, result: dict[str, Any]) -> None:
        self.host.infoRequested.emit("Staged workspace review", describe_staged_changes(result))
        self.host.set_status("Staging reviewed")
        self._set_conflicts(staged_conflicts(result))

    @Slot(str)
    def _on_inspect_failed(self, message: str) -> None:
        self.host.errorRequested.emit("Could not review staging", message)

    @Slot()
    def _on_inspect_finished(self) -> None:
        if self.inspect_worker:
            self.inspect_worker.deleteLater()
        self.inspect_worker = None
        self.host.stateChanged.emit()

    # -- persistent staging: publish kept work, rebase, resolve conflicts (plan 2.4) -----

    def _client(self) -> GatewayClient | None:
        connection = self.host.runtime.connection()
        if not connection or not self.host.runtime.workspace_path:
            self.host.errorRequested.emit(
                "Workspace runtime required", "Select a ready workspace first."
            )
            return None
        return GatewayClient(connection)

    def _start_action(
        self,
        action: str,
        status: str,
        on_complete: Callable[[dict[str, Any]], None],
        on_failed: Callable[[str], None],
        resolutions: dict[str, list[str]] | None = None,
        *,
        paths: tuple[str, ...] | None = None,
    ) -> bool:
        if self.host.generation.running or self.busy:
            self.host.set_status("Wait for the active run or staging request to finish…")
            return False
        client = self._client()
        if client is None:
            return False
        worker = StagingWorker(
            client, self.host.runtime.identity(), action, resolutions, paths=paths
        )
        self.action_worker = worker
        worker.complete.connect(on_complete)
        worker.failed.connect(on_failed)
        worker.finished.connect(self._on_action_finished)
        self.host.set_status(status)
        self.host.stateChanged.emit()
        worker.start()
        return True

    @Slot()
    def _on_action_finished(self) -> None:
        if self.action_worker:
            self.action_worker.deleteLater()
        self.action_worker = None
        self.host.stateChanged.emit()

    def publish_staged(self, paths: tuple[str, ...] | None = None) -> None:
        """Review and publish staged work, or only ``paths`` (partial publication, 4.4)."""
        self.publication_paths = tuple(paths) if paths else None
        self._start_action(
            "publish",
            "Preparing staged changes for review…",
            self._on_staged_publication,
            lambda message: self.host.errorRequested.emit(
                "Could not prepare publication", message
            ),
            paths=self.publication_paths,
        )

    def _on_staged_publication(self, result: dict[str, Any]) -> None:
        try:
            manifest = PublishManifest.from_dict(result.get("manifest"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self.host.errorRequested.emit("Publication blocked", str(exc))
            return
        if not manifest.operations:
            self.host.set_status("Nothing to publish: staging matches the workspace")
            return
        self.host.set_status("Review the staged changes to publish")
        self.confirm_publication(
            manifest, f"publish-staged:{manifest.publication_id}:{manifest.batch_index}"
        )

    def rebase(self, resolutions: dict[str, list[str]] | None = None) -> None:
        """Bring host changes into staging; ``resolutions`` settles named conflicts."""
        self._start_action(
            "rebase",
            "Bringing host changes into staging…",
            self._on_rebased,
            lambda message: self.host.errorRequested.emit("Could not rebase staging", message),
            resolutions,
        )

    def resolve_conflict(self, path: str, choice: str) -> None:
        """Settle one conflict: ``keep_staged`` keeps the agent's version, ``take_host`` the host's."""
        if choice not in {"keep_staged", "take_host"} or not path:
            self.host.errorRequested.emit(
                "Invalid conflict resolution",
                "Choose to keep the staged or take the host file.",
            )
            return
        self.rebase({choice: [path]})

    def _on_rebased(self, result: dict[str, Any]) -> None:
        self.host.set_status(describe_rebase(result))
        self._set_conflicts(
            staged_conflicts({"data": {"conflicts": result.get("open_conflicts")}})
        )

    def _set_conflicts(self, conflicts: list[dict[str, Any]]) -> None:
        if conflicts != self.conflicts:
            self.conflicts = conflicts
            self.host.conflictsChanged.emit()

    # -- review pane, partial publication, and declared checks (plan 4.4, 3.4) ---------

    def base_reader(self) -> Callable[[str], str | None]:
        """Read a host file's current text (bounded, never through a link) so risk flags can
        tell a dependency change from a package-script change."""
        root = Path(self.host.runtime.workspace_path or ".")

        def read(path: str) -> str | None:
            target = root / path
            try:
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                    return None
                return target.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return None

        return read

    def risk_flags(self, manifest: PublishManifest) -> dict[str, tuple[str, ...]]:
        return classify_manifest(
            manifest.to_dict()["operations"], base_reader=self.base_reader()
        )

    def open_review(self) -> None:
        """Load the staged changes with per-file diffs and declared check results."""
        if self.review_worker or not self.host.runtime.workspace_path:
            return
        client = self._client()
        if client is None:
            return
        worker = StagingWorker(client, self.host.runtime.identity(), "review")
        self.review_worker = worker
        worker.complete.connect(self._on_reviewed)
        worker.failed.connect(
            lambda message: self.host.errorRequested.emit("Could not review staging", message)
        )
        worker.finished.connect(self._on_review_finished)
        self.host.set_status("Loading staged changes for review…")
        worker.start()

    @Slot(dict)
    def _on_reviewed(self, result: dict[str, Any]) -> None:
        previous = self.review_data
        self.review_data = result
        paths = {str(item.get("path")) for item in result.get("changes") or () if item}
        if previous is None:
            self.review_selection = set(paths)
        else:
            # Keep the user's choices; select files that are new since the last review.
            known = {str(item.get("path")) for item in previous.get("changes") or () if item}
            self.review_selection = (self.review_selection & paths) | (paths - known)
        self.host.set_status(str(result.get("summary") or "Staged changes loaded"))
        self.host.reviewChanged.emit()

    @Slot()
    def _on_review_finished(self) -> None:
        if self.review_worker:
            self.review_worker.deleteLater()
        self.review_worker = None

    def close_review(self) -> None:
        self.review_data = None
        self.review_selection = set()
        self.host.reviewChanged.emit()

    def set_review_selected(self, path: str, selected: bool) -> None:
        if selected:
            self.review_selection.add(path)
        else:
            self.review_selection.discard(path)
        self.host.reviewChanged.emit()

    def set_review_all(self, selected: bool) -> None:
        changes = (self.review_data or {}).get("changes") or ()
        self.review_selection = (
            {str(item.get("path")) for item in changes if isinstance(item, dict)}
            if selected
            else set()
        )
        self.host.reviewChanged.emit()

    def publish_selection(self) -> None:
        """Publish the selected files only; unselected changes stay staged (4.4)."""
        changes = (self.review_data or {}).get("changes") or ()
        all_paths = {str(item.get("path")) for item in changes if isinstance(item, dict)}
        selected = tuple(sorted(self.review_selection & all_paths))
        if not selected:
            self.host.set_status("Select at least one file to publish")
            return
        partial = set(selected) != all_paths or bool((self.review_data or {}).get("truncated"))
        self.publish_staged(selected if partial else None)

    def run_check(self, name: str) -> None:
        """Run one declared check in the runner against the current staged snapshot (3.4)."""
        if self.check_worker or self.host.generation.running or self.publishing:
            self.host.set_status("Wait for the active run or check to finish…")
            return
        client = self._client()
        if client is None:
            return
        worker = StagingWorker(client, self.host.runtime.identity(), "check", check=name)
        self.check_worker = worker
        self.running_check = name
        worker.complete.connect(self._on_check_complete)
        worker.failed.connect(
            lambda message: self.host.errorRequested.emit(
                f"Check {name} failed to run", message
            )
        )
        worker.finished.connect(self._on_check_finished)
        self.host.set_status(f"Running check {name} against the staged snapshot…")
        self.host.stateChanged.emit()
        self.host.reviewChanged.emit()
        worker.start()

    @Slot(dict)
    def _on_check_complete(self, result: dict[str, Any]) -> None:
        self.host.set_status(f"Check {result.get('name', '')}: {result.get('outcome', 'done')}")

    @Slot()
    def _on_check_finished(self) -> None:
        if self.check_worker:
            self.check_worker.deleteLater()
        self.check_worker = None
        self.running_check = ""
        self.host.stateChanged.emit()
        self.host.reviewChanged.emit()
        if self.review_data is not None:
            QTimer.singleShot(0, self.open_review)

    def review_view(self) -> dict[str, Any]:
        """The review pane for QML: flagged files first, each with its diff and selection."""
        data = self.review_data
        if data is None:
            return {"open": False}
        changes = [item for item in data.get("changes") or () if isinstance(item, dict)]
        operations = [
            {
                "path": item.get("path"),
                "operation": item.get("operation"),
                "base_mode": _mode(item.get("base_mode")),
                "staged_mode": _mode(item.get("staged_mode")),
                "content": None,
            }
            for item in changes
        ]
        flags = classify_manifest(operations)
        diffs = {
            str(item.get("path")): item
            for item in data.get("diffs") or ()
            if isinstance(item, dict)
        }
        by_path = {str(item.get("path")): item for item in changes}
        files = []
        for path in review_order(flags):
            item = by_path[path]
            diff = diffs.get(path, {})
            text = str(diff.get("text") or "")
            if not text and diff.get("kind") and diff.get("kind") != "text":
                text = f"({diff.get('kind')})"
            files.append(
                {
                    "path": path,
                    "operation": str(item.get("operation") or ""),
                    "flags": [FLAG_LABELS[flag] for flag in flags.get(path, ())],
                    "confirm": needs_confirmation({path: flags.get(path, ())}),
                    "selected": path in self.review_selection,
                    "diff": text[:20_000],
                    "added": int(diff.get("added_lines") or 0),
                    "removed": int(diff.get("removed_lines") or 0),
                }
            )
        required = set(self.host.policy.effective().verify_before_publish)
        checks = [
            {
                "name": str(item.get("name") or ""),
                "command": str(item.get("command") or ""),
                "status": str(item.get("status") or "not_run"),
                "current": bool(item.get("current")),
                "required": str(item.get("name") or "") in required,
                "running": item.get("name") == self.running_check,
                "output": str((item.get("result") or {}).get("output") or "")[-4_000:]
                if isinstance(item.get("result"), dict)
                else "",
            }
            for item in data.get("checks") or ()
            if isinstance(item, dict)
        ]
        return {
            "open": True,
            "summary": str(data.get("summary") or ""),
            "files": files,
            "selectedCount": len(self.review_selection & set(by_path)),
            "truncated": bool(data.get("truncated")),
            "neverPublish": [str(item) for item in data.get("never_publish") or ()][:50],
            "checks": checks,
            "fingerprint": str(data.get("fingerprint") or "")[:12],
        }

    # -- leaving a workspace (plan 0.6) ------------------------------------------------

    def guard_leave(self, proceed: Callable[[], None], *, closing: bool) -> None:
        """Run ``proceed`` once staged work is safe: clean, or the user chose what to do.

        Staging persists in its workspace's volumes, so *Keep for later* simply proceeds; the
        next session for the same workspace resumes it.
        """
        connection = self.host.runtime.connection()
        if (
            not connection
            or not self.host.runtime.workspace_path
            or not self.host.runtime.workspace_ready()
            or self.busy
        ):
            proceed()
            return
        self._pending_leave = proceed
        worker = StagingWorker(
            GatewayClient(connection), self.host.runtime.identity(), "inspect"
        )
        self.action_worker = worker
        worker.complete.connect(lambda result: self._on_leave_inspected(result, closing))
        worker.failed.connect(lambda _message: self.resolve_leave("keep"))
        worker.finished.connect(self._on_action_finished)
        self.host.set_status("Checking staged changes…")
        worker.start()

    def _on_leave_inspected(self, result: dict[str, Any], closing: bool) -> None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if not data.get("unpublished_changes"):
            self.resolve_leave("keep")
            return
        title = (
            "Close with unpublished staged changes?"
            if closing
            else ("Switch workspace with unpublished staged changes?")
        )
        body = (
            describe_staged_changes(result)
            + "\n\nReview and publish writes them to the workspace after you approve each "
            "file; Discard deletes them; Keep for later leaves them staged for the next "
            "session with this workspace."
        )
        self.host.leaveRequested.emit(title, body)

    def resolve_leave(self, choice: str) -> None:
        proceed, self._pending_leave = self._pending_leave, None
        if proceed is None:
            return
        if choice == "keep":
            QTimer.singleShot(0, proceed)
        elif choice == "discard":
            QTimer.singleShot(0, lambda: self._discard(then=proceed))
        elif choice == "publish":
            # Publication is reviewed; leaving waits for the user to close or switch again.
            QTimer.singleShot(0, self.publish_staged)
        else:
            self.host.set_status("Kept the workspace open")


def staged_conflicts(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The open drift conflicts in a staging inspection, for the conflict list in QML."""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    conflicts = data.get("conflicts") if isinstance(data.get("conflicts"), list) else []
    return [
        {"path": str(item["path"])}
        for item in conflicts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ]


def describe_rebase(result: dict[str, Any]) -> str:
    absorbed = len(result.get("absorbed") or ())
    resolved = len(result.get("resolved") or ())
    conflicts = len(result.get("open_conflicts") or ())
    parts = [f"Rebased staging: {absorbed} host change(s) absorbed"]
    if resolved:
        parts.append(f"{resolved} conflict(s) resolved")
    parts.append(f"{conflicts} conflict(s) open" if conflicts else "no conflicts")
    return " · ".join(parts)


def describe_staged_changes(result: dict[str, Any]) -> str:
    """Summarize a staging inspection: whether staging differs from the published baseline.

    Inspection reads executor status rather than running a tool, so it reports the staged
    snapshot's size, not a per-file list; the publication review shows each changed file.
    """
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if data.get("unpublished_changes"):
        state = (
            "Staging has unpublished changes. Publishing shows each changed file for review; "
            "discarding resets staging to the workspace."
        )
    else:
        state = "Staging is clean: it matches the workspace baseline."
    files = int(data.get("file_count") or 0)
    size = int(data.get("total_bytes") or 0)
    lines = [state, f"Staged snapshot: {files:,} files, {size:,} bytes."]
    if data.get("staging_resumed"):
        lines.append("Staging was resumed from an earlier session with this workspace.")
    conflicts = staged_conflicts(result)
    if conflicts:
        names = ", ".join(item["path"] for item in conflicts[:10])
        more = f" and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
        lines.append(
            f"{len(conflicts)} file(s) changed both on the host and in staging: {names}{more}. "
            "They are held back from publication until you keep the staged or take the host version."
        )
    return "\n".join(lines)


def _mode(value: object) -> int | None:
    """A file mode from the status tool's octal text."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value), 8) if value is not None else None
    except ValueError:
        return None
