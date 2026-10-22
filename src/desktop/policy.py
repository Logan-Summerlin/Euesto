from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Slot

from shared.policy import (
    POLICY_PATH,
    PolicyError,
    PolicyFile,
    WorkspacePolicy,
    detect_setup_command,
    manifests_digest,
    read_policy,
    setup_manifests,
)

from ..runtime_manager import RuntimeSettings, SetupRequest
from ..storage import Storage
from ..workers import CallWorker
from .host import BridgeHost

# Keys this service keeps in the desktop's workspace configuration. Only ``policy`` is synced
# to the gateway (it is in ``WORKSPACE_CONFIG_KEYS``); the rest are desktop bookkeeping.
POLICY_KEY = "policy"
POLICY_DIGEST_KEY = "policy_digest"
SETUP_COMMAND_KEY = "setup_command"
SETUP_DIGEST_KEY = "setup_digest"
MAX_POLICY_TEXT = 8_000
MAX_SETUP_LOG_LINES = 400
# Policy fields that only take effect when the runtime starts.
RUNTIME_FIELDS = ("runner_image", "allow_hosts", "secret_patterns", "never_publish")


class PolicyService(QObject):
    """The workspace policy file (harness plan 4.6), the approved dependency setup phase
    (5.1), and the egress audit log (5.2).

    The policy is read from the **host** workspace only, never from staging: an agent can
    propose a change only as a staged edit, which does nothing until it is published *and* the
    user confirms it here. Until then the previously confirmed policy (or none) stays in
    effect; an invalid file never takes effect (fail closed).
    """

    def __init__(self, host: BridgeHost, storage: Storage):
        super().__init__(host)  # type: ignore[arg-type]
        self.host = host
        self.storage = storage
        self.file = PolicyFile("absent")
        # absent | confirmed | unconfirmed | changed | invalid | removed
        self.status = "absent"
        self.setup_log: list[str] = []
        self.setup_state = "idle"  # idle | running | succeeded | failed
        self.setup_message = ""
        self._setup_isolated = False
        self.audit: list[dict[str, object]] = []
        self.audit_worker: CallWorker | None = None
        self._connected = False

    # -- reading and confirming the policy ---------------------------------------------

    def _workspace(self) -> Path | None:
        path = self.host.runtime.workspace_path
        return Path(path) if path else None

    def _config(self) -> dict[str, object]:
        workspace = self._workspace()
        return self.storage.workspace_config(self.host.runtime.identity()) if workspace else {}

    def refresh(self) -> None:
        """Re-read the host policy file and compare it with the confirmed one."""
        workspace = self._workspace()
        if workspace is None:
            self.file, self.status = PolicyFile("absent"), "absent"
        else:
            self.file = read_policy(workspace)
            confirmed = self._config().get(POLICY_DIGEST_KEY)
            if self.file.status == "absent":
                self.status = "removed" if confirmed else "absent"
            elif self.file.status == "invalid":
                self.status = "invalid"
            elif self.file.digest == confirmed:
                self.status = "confirmed"
            else:
                self.status = "changed" if confirmed else "unconfirmed"
        self.host.policyChanged.emit()

    def effective(self) -> WorkspacePolicy:
        """The confirmed policy in effect; a stored policy that fails validation counts as
        none, so nothing unvalidated ever takes effect."""
        try:
            return WorkspacePolicy.from_config(self._config().get(POLICY_KEY))
        except PolicyError:
            return WorkspacePolicy()

    def allows_registries(self) -> bool:
        """Whether Agent sessions in this workspace may opt into registry access (5.3)."""
        return self.effective().network_mode == "registries"

    def runtime_settings(self, base: RuntimeSettings, *, network_mode: str) -> RuntimeSettings:
        """The runner settings with the confirmed policy and the session's network mode."""
        policy = self.effective()
        runner = base.runner_target
        if runner == "auto" and policy.runner_image != "auto":
            runner = policy.runner_image
        return replace(
            base,
            runner_target=runner,
            network_mode=network_mode,
            registry_hosts=policy.registry_hosts,
            secret_patterns=policy.secret_patterns,
            never_publish=policy.never_publish,
        )

    def request_confirm(self) -> None:
        """Ask to apply the host policy (or its removal); nothing changes until confirmed."""
        self.refresh()
        if self.status in {"absent", "confirmed"}:
            self.host.set_status("The workspace policy is already in effect")
            return
        if self.status == "invalid":
            self.host.errorRequested.emit(
                "Workspace policy is invalid",
                f"{POLICY_PATH} does not take effect until it validates:\n"
                + "\n".join(f"• {item}" for item in self.file.errors[:20]),
            )
            return
        if self.status == "removed":
            title = "Remove the workspace policy?"
            body = (
                f"{POLICY_PATH} was removed from the workspace. Confirm to stop applying the "
                "previously confirmed policy and return to the defaults."
            )
            policy = None
        else:
            assert self.file.policy is not None
            policy = self.file.policy
            title = (
                "Policy changed, review"
                if self.status == "changed"
                else "Apply workspace policy?"
            )
            body = (
                f"{POLICY_PATH} in the workspace "
                + ("changed since you confirmed it" if self.status == "changed" else "is new")
                + ". It takes effect only after you confirm it here.\n\n"
                + describe_policy(policy)
            )
        digest = self.file.digest
        self.host.confirm(
            f"policy:{self.host.runtime.identity()}:{digest or 'removed'}",
            title,
            body,
            lambda: self._apply(policy, digest),
        )

    def _apply(self, policy: WorkspacePolicy | None, digest: str | None) -> None:
        workspace = self._workspace()
        if workspace is None:
            return
        # Confirm exactly what was reviewed: a file changed since the prompt is reviewed again.
        current = read_policy(workspace)
        if current.digest != digest or (policy is None) != (current.status == "absent"):
            self.refresh()
            self.host.errorRequested.emit(
                "Workspace policy changed",
                "The policy file changed while it was being reviewed. Review it again.",
            )
            return
        before = self.effective()
        identity = self.host.runtime.identity()
        config = self.storage.workspace_config(identity)
        if policy is None:
            config.pop(POLICY_KEY, None)
            config.pop(POLICY_DIGEST_KEY, None)
        else:
            config[POLICY_KEY] = policy.to_config()
            config[POLICY_DIGEST_KEY] = digest
        self.storage.save_workspace_config(identity, str(workspace), config)
        self.host.settings.sync_workspace_config(identity, config)
        after = policy or WorkspacePolicy()
        self.refresh()
        restart = any(getattr(before, name) != getattr(after, name) for name in RUNTIME_FIELDS)
        if after.network_mode != "registries" and self.host.runtime.network_mode != "none":
            # The policy no longer allows registry access: the session loses it now.
            self.host.runtime.network_mode = "none"
            restart = True
        if restart:
            self.host.runtime.restart_for_policy()
        else:
            self.host.set_status("Workspace policy confirmed")

    # -- dependency setup (plan 5.1) ---------------------------------------------------

    def setup_plan(self) -> dict[str, Any]:
        """The setup command (declared, confirmed, or detected) and whether it should run."""
        workspace = self._workspace()
        if workspace is None:
            return {"command": "", "source": "", "needed": False}
        policy = self.effective()
        config = self._config()
        if policy.setup_command:
            command, source = policy.setup_command, "policy"
        elif isinstance(config.get(SETUP_COMMAND_KEY), str) and config[SETUP_COMMAND_KEY]:
            command, source = str(config[SETUP_COMMAND_KEY]), "confirmed"
        else:
            command, source = detect_setup_command(workspace) or "", "detected"
        digest = manifests_digest(workspace) if command else ""
        return {
            "command": command,
            "source": source,
            "isolated": policy.setup_isolated,
            "timeoutSeconds": policy.setup_timeout_seconds,
            "manifests": setup_manifests(workspace),
            "hosts": list(policy.registry_hosts),
            "needed": bool(command) and digest != config.get(SETUP_DIGEST_KEY),
            "lastRun": "never" if not config.get(SETUP_DIGEST_KEY) else "done",
        }

    def request_setup(self, full: bool = False) -> None:
        """Ask to run dependency setup once with registry access; runs only after approval."""
        runtime = self.host.runtime
        if not runtime.automatic:
            self.host.errorRequested.emit(
                "Setup unavailable",
                "Dependency setup runs in the app-managed runtime; developer runtimes are "
                "managed by scripts.",
            )
            return
        if (
            not runtime.workspace_ready()
            or self.host.generation.running
            or self.host.staging.busy
            or runtime.manager.setup_running
        ):
            self.host.errorRequested.emit(
                "Setup unavailable", "Wait for a ready, idle workspace runtime first."
            )
            return
        plan = self.setup_plan()
        command = str(plan["command"])
        if not command:
            self.host.errorRequested.emit(
                "No setup command",
                "No lockfile was recognized. Declare [setup] command in "
                f"{POLICY_PATH} to install dependencies.",
            )
            return
        isolated = bool(plan["isolated"]) and not full
        mode = (
            "Isolated: the setup container sees only "
            + (", ".join(plan["manifests"]) or "the dependency manifests")
            + ", never the source tree."
            if isolated
            else "Full: the setup container sees the whole staged copy, because this project "
            "needs its source during install. Install scripts can read the code while the "
            "network is open."
        )
        source = {
            "policy": f"declared in {POLICY_PATH}",
            "confirmed": "confirmed earlier",
            "detected": "detected from the lockfiles; confirming runs and remembers it",
        }[str(plan["source"])]
        body = (
            f"Command ({source}):\n{command}\n\n{mode}\n\n"
            "It runs once in the runner image with access only to these registries, through "
            "the audited proxy: " + ", ".join(plan["hosts"]) + ". It holds no credentials. "
            "Dependencies stay in .venv, node_modules, and the cache volume; afterwards the "
            "agent's runner still has no network."
        )
        self.host.confirm(
            f"setup:{runtime.identity()}:{command}:{isolated}",
            "Run dependency setup with registry access?",
            body,
            lambda: self._start_setup(command, isolated, int(plan["timeoutSeconds"]), plan),
        )

    def _start_setup(
        self, command: str, isolated: bool, timeout: int, plan: dict[str, Any]
    ) -> None:
        manager = self.host.runtime.manager
        if not self._connected:
            manager.dependencyOutput.connect(self._setup_output)
            manager.dependencySetupFinished.connect(self._setup_finished)
            self._connected = True
        if plan["source"] == "detected":
            identity = self.host.runtime.identity()
            config = self.storage.workspace_config(identity)
            config[SETUP_COMMAND_KEY] = command
            self.storage.save_workspace_config(
                identity, self.host.runtime.workspace_path, config
            )
        self.setup_log = []
        self.setup_state = "running"
        self.setup_message = "Installing dependencies with registry access…"
        self._setup_isolated = isolated
        self.host.set_status(self.setup_message)
        self.host.policyChanged.emit()
        manager.run_setup(SetupRequest(command, isolated, timeout))

    @Slot(str)
    def _setup_output(self, line: str) -> None:
        self.setup_log.append(line.rstrip("\n")[:2_000])
        del self.setup_log[:-MAX_SETUP_LOG_LINES]
        self.host.policyChanged.emit()

    @Slot(bool, object)
    def _setup_finished(self, ok: bool, result: object) -> None:
        workspace = self._workspace()
        if ok and workspace is not None:
            identity = self.host.runtime.identity()
            config = self.storage.workspace_config(identity)
            config[SETUP_DIGEST_KEY] = manifests_digest(workspace)
            self.storage.save_workspace_config(identity, str(workspace), config)
            self.setup_state = "succeeded"
            self.setup_message = "Dependencies installed; the agent's runner stays offline."
            self.host.set_status(self.setup_message)
        else:
            self.setup_state = "failed"
            self.setup_message = str(result)[:1_200]
            hint = (
                " Isolated setup gives install scripts only the manifests; projects that need "
                "their source during install (editable installs, workspace packages) can run a "
                "full setup instead."
                if self._setup_isolated
                else ""
            )
            self.host.errorRequested.emit("Dependency setup failed", self.setup_message + hint)
        self.host.policyChanged.emit()

    # -- audit log (plan 5.2) ------------------------------------------------------------

    def load_audit(self) -> None:
        if self.audit_worker is not None:
            return
        worker = CallWorker(self.host.runtime.manager.egress_audit, "Could not read audit log")
        worker.complete.connect(self._audit_loaded)
        worker.failed.connect(
            lambda message: self.host.errorRequested.emit("Egress audit log", message)
        )
        worker.finished.connect(self._audit_finished)
        self.audit_worker = worker
        worker.start()

    @Slot(object)
    def _audit_loaded(self, entries: object) -> None:
        self.audit = [item for item in entries or () if isinstance(item, dict)]
        self.host.policyChanged.emit()

    @Slot()
    def _audit_finished(self) -> None:
        if self.audit_worker:
            self.audit_worker.deleteLater()
        self.audit_worker = None

    # -- QML view ------------------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        effective = self.effective()
        pending = self.file.policy
        return {
            "status": self.status,
            "path": POLICY_PATH,
            "errors": list(self.file.errors[:20]),
            "text": self.file.text[:MAX_POLICY_TEXT],
            "effective": describe_policy(effective),
            "pending": describe_policy(pending) if pending is not None else "",
            "allowsRegistries": effective.network_mode == "registries",
            "setup": {
                **self.setup_plan(),
                "state": self.setup_state,
                "message": self.setup_message,
                "log": "\n".join(self.setup_log[-200:]),
            },
            "audit": [_audit_row(item) for item in self.audit[:200]],
        }

    def shutdown(self) -> None:
        if self.audit_worker:
            self.audit_worker.wait(500)


def describe_policy(policy: WorkspacePolicy) -> str:
    """The policy as plain lines for review in Settings and the confirmation prompt."""
    checks = ", ".join(f"{item.name} ({item.command})" for item in policy.checks) or "none"
    lines = [
        f"Runner image: {policy.runner_image}",
        "Network: "
        + (
            "Agent sessions may opt into registry access"
            if policy.network_mode == "registries"
            else "none (no registry access during Agent sessions)"
        ),
        "Registries: " + ", ".join(policy.registry_hosts),
        "Setup: "
        + (
            f"{policy.setup_command} ({'isolated' if policy.setup_isolated else 'full'})"
            if policy.setup_command
            else "detected from lockfiles"
        ),
        f"Checks: {checks}",
        "Required before publishing: " + (", ".join(policy.verify_before_publish) or "none"),
        "Extra secret names: " + (", ".join(policy.secret_patterns) or "none"),
        "Never published: " + (", ".join(policy.never_publish) or "none"),
    ]
    return "\n".join(lines)


def _audit_row(entry: dict[str, object]) -> dict[str, object]:
    return {
        "allowed": entry.get("event") == "egress.allowed",
        "host": str(entry.get("host") or ""),
        "port": entry.get("port") or "",
        "reason": str(entry.get("reason") or ""),
        "timestamp": entry.get("timestamp") or 0,
    }
