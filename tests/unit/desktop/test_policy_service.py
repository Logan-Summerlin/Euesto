"""The desktop side of the workspace policy file (harness plan 4.6) and the setup offer (5.1).

The policy is read from the host workspace only, takes effect only after the user confirms
exactly what was reviewed, fails closed when invalid, and needs confirming again after any
change. Dependency setup is offered at the first start and again after a lockfile changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PySide6.QtCore import QObject
except ImportError as exc:  # pragma: no cover - Qt unavailable
    pytest.skip(f"Qt unavailable: {exc}", allow_module_level=True)

from shared.policy import WorkspacePolicy
from src.desktop.policy import POLICY_DIGEST_KEY, SETUP_DIGEST_KEY, PolicyService
from src.runtime_manager import RuntimeSettings
from src.storage import Storage

REGISTRIES_POLICY = 'version = 1\n[network]\nmode = "registries"\n'


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def emit(self, *args: Any) -> None:
        self.calls.append(args)


class FakeHost(QObject):
    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.policyChanged = Recorder()
        self.errorRequested = Recorder()
        self.confirmations: dict[str, tuple[str, str, Any]] = {}
        self.statuses: list[str] = []
        self.restarts: list[str] = []
        self.synced: list[dict[str, Any]] = []
        self.setups: list[Any] = []
        self.manager = SimpleNamespace(
            setup_running=False,
            run_setup=self.setups.append,
            dependencyOutput=SimpleNamespace(connect=lambda _slot: None),
            dependencySetupFinished=SimpleNamespace(connect=lambda _slot: None),
        )
        self.runtime = SimpleNamespace(
            workspace_path=str(workspace),
            identity=lambda: "workspace-1",
            automatic=True,
            network_mode="none",
            workspace_ready=lambda: True,
            restart_for_policy=lambda: self.restarts.append(self.runtime.network_mode),
            manager=self.manager,
        )
        self.settings = SimpleNamespace(
            sync_workspace_config=lambda _identity, config: self.synced.append(dict(config))
        )
        self.generation = SimpleNamespace(running=False)
        self.staging = SimpleNamespace(busy=False)

    def confirm(self, token, title, body, on_accept, _on_decline=None) -> None:
        self.confirmations[token] = (title, body, on_accept)

    def accept(self, token: str) -> None:
        self.confirmations.pop(token)[2]()

    def set_status(self, text: str) -> None:
        self.statuses.append(text)


@pytest.fixture
def setup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage = Storage(tmp_path / "chat.sqlite3")
    host = FakeHost(workspace)
    service = PolicyService(host, storage)  # type: ignore[arg-type]
    yield workspace, storage, host, service
    storage.close()


def _write_policy(workspace: Path, text: str) -> None:
    (workspace / ".euesto").mkdir(parents=True, exist_ok=True)
    (workspace / ".euesto" / "policy.toml").write_text(text, encoding="utf-8")


def _only_token(host: FakeHost) -> str:
    assert len(host.confirmations) == 1, host.confirmations
    return next(iter(host.confirmations))


def test_a_policy_takes_effect_only_after_confirmation(setup) -> None:
    workspace, storage, host, service = setup
    service.refresh()
    assert service.status == "absent" and service.effective() == WorkspacePolicy()

    _write_policy(workspace, REGISTRIES_POLICY)
    service.refresh()
    assert service.status == "unconfirmed"
    assert service.effective() == WorkspacePolicy() and not service.allows_registries()

    service.request_confirm()
    title, body, _accept = host.confirmations[_only_token(host)]
    assert title == "Apply workspace policy?" and "registry access" in body
    host.accept(_only_token(host))
    assert service.status == "confirmed" and service.allows_registries()
    config = storage.workspace_config("workspace-1")
    assert config[POLICY_DIGEST_KEY] == service.file.digest
    assert host.synced and host.synced[-1]["policy"] == service.effective().to_config()
    # Allowing registries is only an opt-in for later sessions: nothing restarts yet.
    assert host.restarts == []


def test_a_changed_policy_needs_review_and_the_confirmed_one_stays_in_effect(setup) -> None:
    workspace, _storage, host, service = setup
    _write_policy(workspace, REGISTRIES_POLICY)
    service.request_confirm()
    host.accept(_only_token(host))
    host.runtime.network_mode = "registries"

    _write_policy(workspace, 'version = 1\n[network]\nmode = "none"\n')
    service.refresh()
    assert service.status == "changed" and service.allows_registries()
    service.request_confirm()
    title, _body, _accept = host.confirmations[_only_token(host)]
    assert title == "Policy changed, review"
    host.accept(_only_token(host))
    # The session loses registry access as soon as the policy stops allowing it.
    assert not service.allows_registries() and host.runtime.network_mode == "none"
    assert host.restarts[-1] == "none"


def test_confirmation_applies_exactly_what_was_reviewed(setup) -> None:
    workspace, _storage, host, service = setup
    _write_policy(workspace, REGISTRIES_POLICY)
    service.request_confirm()
    token = _only_token(host)
    _write_policy(workspace, REGISTRIES_POLICY + '[setup]\ncommand = "npm ci"\n')
    host.accept(token)
    assert service.effective() == WorkspacePolicy()
    assert host.errorRequested.calls[-1][0] == "Workspace policy changed"


def test_an_invalid_policy_fails_closed(setup) -> None:
    workspace, _storage, host, service = setup
    _write_policy(workspace, REGISTRIES_POLICY)
    service.request_confirm()
    host.accept(_only_token(host))

    _write_policy(workspace, 'version = 1\n[network]\nmode = "everything"\n')
    service.refresh()
    assert service.status == "invalid" and service.view()["errors"]
    service.request_confirm()
    assert host.confirmations == {}
    assert host.errorRequested.calls[-1][0] == "Workspace policy is invalid"
    # The previously confirmed policy is never replaced by an unvalidated one.
    assert service.allows_registries()


def test_a_policy_only_in_staging_or_a_removed_policy_has_no_effect(setup, tmp_path) -> None:
    workspace, _storage, host, service = setup
    staging = tmp_path / "staging"
    _write_policy(staging, REGISTRIES_POLICY)
    service.refresh()
    assert service.status == "absent" and not service.allows_registries()

    _write_policy(workspace, REGISTRIES_POLICY)
    service.request_confirm()
    host.accept(_only_token(host))
    (workspace / ".euesto" / "policy.toml").unlink()
    service.refresh()
    # Removal is a change too: the confirmed policy stays until the user confirms it.
    assert service.status == "removed" and service.allows_registries()
    service.request_confirm()
    title, _body, _accept = host.confirmations[_only_token(host)]
    assert title == "Remove the workspace policy?"
    host.accept(_only_token(host))
    assert service.status == "absent" and not service.allows_registries()


def test_runtime_settings_apply_the_confirmed_policy(setup) -> None:
    workspace, _storage, host, service = setup
    _write_policy(
        workspace,
        'version = 1\n[runner]\nimage = "node"\n[network]\nmode = "registries"\n'
        '[publication]\nnever_publish = ["deploy/**"]\n',
    )
    service.request_confirm()
    host.accept(_only_token(host))
    settings = service.runtime_settings(RuntimeSettings(), network_mode="registries")
    assert settings.runner_target == "node" and settings.network_mode == "registries"
    assert settings.never_publish == ("deploy/**",)
    # An explicit runner choice in Settings wins over the policy's image.
    chosen = service.runtime_settings(
        RuntimeSettings(runner_target="python"), network_mode="none"
    )
    assert chosen.runner_target == "python"


def test_setup_is_offered_once_per_lockfile_state_and_again_after_a_change(setup) -> None:
    workspace, storage, host, service = setup
    (workspace / "package-lock.json").write_text("{}", encoding="utf-8")
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    service.refresh()
    service.offer_setup()
    token = _only_token(host)
    title, body, _accept = host.confirmations[token]
    assert title == "Run dependency setup with registry access?"
    assert "npm ci" in body and "Isolated" in body
    host.confirmations.clear()  # declined
    service.offer_setup()
    assert host.confirmations == {}  # not repeated for the same lockfiles

    (workspace / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    service.offer_setup()
    host.accept(_only_token(host))
    assert len(host.setups) == 1 and host.setups[0].isolated is True
    assert host.setups[0].command == "npm ci"
    service._setup_finished(True, {})
    assert storage.workspace_config("workspace-1")[SETUP_DIGEST_KEY]
    assert service.setup_plan()["needed"] is False
    service.offer_setup()
    assert host.confirmations == {}


def test_setup_is_not_offered_while_busy_or_for_developer_runtimes(setup) -> None:
    workspace, _storage, host, service = setup
    (workspace / "uv.lock").write_text("", encoding="utf-8")
    (workspace / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    host.generation.running = True
    service.offer_setup()
    host.generation.running = False
    host.runtime.automatic = False
    service.offer_setup()
    assert host.confirmations == {} and host.errorRequested.calls == []
