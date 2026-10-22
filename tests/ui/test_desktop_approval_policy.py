"""Desktop selection of the Agent approval tier (prompt / sandbox / auto) and the session
network mode (harness plan 4.1, 5.3)."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from src.qml_backend import DesktopBridge
except ImportError as exc:  # pragma: no cover - Qt unavailable
    pytest.skip(f"Desktop Qt bridge unavailable: {exc}", allow_module_level=True)

from shared.policy import parse_policy
from shared.responses import GatewayStatus
from src.desktop.generation import SANDBOX_EXPLAINED_KEY
from src.storage import Storage
from src.workspace_broker import workspace_id


@pytest.fixture
def bridge(tmp_path: Path):
    storage = Storage(tmp_path / "chat.sqlite3")
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    backend = DesktopBridge(storage)
    backend.runtime.health_timer.stop()
    backend.runtime.workspace_path = str(workspace.resolve())
    backend.runtime.state = "manual"
    backend.runtime.last_status = GatewayStatus(
        ready=True,
        executor_present=True,
        executor_status="ready",
        active_workspace=workspace_id(workspace),
        supported_modes=("chat", "plan", "agent"),
        capabilities=({"name": "agent_sandbox"}, {"name": "agent_auto"}),
    )
    confirmations: list[str] = []
    errors: list[str] = []
    backend.confirmRequested.connect(lambda token, _title, _body: confirmations.append(token))
    backend.errorRequested.connect(lambda title, _body: errors.append(title))
    backend.selectMode("agent")
    yield backend, confirmations, errors
    # shutdown() also neutralizes the gateway check queued at construction, so a later test
    # that pumps the Qt event loop cannot run it against the closed storage.
    backend.runtime.shutdown()
    storage.close()


def test_sandbox_is_explained_once_then_selects_the_middle_tier(bridge) -> None:
    backend, confirmations, errors = bridge
    assert backend.generation.approval_policy() == "prompt"
    assert backend.sandboxAvailable is True

    backend.requestSandbox(True)
    assert errors == [] and len(confirmations) == 1
    assert backend.sandboxEnabled is False  # not until the user has read the explanation
    backend.resolveConfirmation(confirmations[0], True)
    assert backend.sandboxEnabled is True
    assert backend.generation.approval_policy() == "sandbox"

    # Auto supersedes the middle tier while enabled; turning Auto off falls back to it.
    backend.requestAutoMode(True)
    backend.resolveConfirmation(confirmations[-1], True)
    assert backend.generation.approval_policy() == "auto"
    backend.requestAutoMode(False)
    assert backend.generation.approval_policy() == "sandbox"

    backend.requestSandbox(False)
    assert backend.generation.approval_policy() == "prompt"
    # The explanation is not repeated.
    count = len(confirmations)
    backend.requestSandbox(True)
    assert len(confirmations) == count and backend.sandboxEnabled is True


def test_sandbox_becomes_the_default_for_new_agent_sessions_after_the_explanation(
    bridge,
) -> None:
    backend, confirmations, _errors = bridge
    backend.selectMode("chat")
    backend.selectMode("agent")
    assert backend.generation.approval_policy() == "prompt"  # not before the explanation
    backend.requestSandbox(True)
    backend.resolveConfirmation(confirmations[0], True)
    assert backend.storage.get_setting(SANDBOX_EXPLAINED_KEY) == "1"
    backend.selectMode("plan")
    assert backend.generation.approval_policy() == "prompt"
    backend.selectMode("agent")
    assert backend.generation.approval_policy() == "sandbox"


def test_declined_explanation_keeps_prompting(bridge) -> None:
    backend, confirmations, _errors = bridge
    backend.requestSandbox(True)
    backend.resolveConfirmation(confirmations[0], False)
    assert backend.generation.approval_policy() == "prompt"
    assert backend.storage.get_setting(SANDBOX_EXPLAINED_KEY) is None


def test_sandbox_resets_on_mode_change(bridge) -> None:
    backend, confirmations, _errors = bridge
    backend.requestSandbox(True)
    backend.resolveConfirmation(confirmations[0], True)
    backend.selectMode("plan")
    assert backend.generation.approval_policy() == "prompt" and backend.sandboxEnabled is False


def test_sandbox_unavailable_without_gateway_capability(bridge) -> None:
    backend, confirmations, errors = bridge
    backend.runtime.last_status = replace(
        backend.runtime.last_status, capabilities=({"name": "agent_auto"},)
    )
    assert backend.sandboxAvailable is False
    backend.requestSandbox(True)
    assert confirmations == [] and errors == ["Sandbox unavailable"]


def test_registry_access_needs_the_policy_and_a_confirmation(bridge, monkeypatch) -> None:
    backend, confirmations, errors = bridge
    backend.runtime.automatic = True
    restarts: list[str] = []
    monkeypatch.setattr(
        backend.runtime, "retry", lambda: restarts.append(backend.runtime.network_mode)
    )
    backend.requestRegistryAccess(True)
    assert errors == ["Registry access not allowed"] and confirmations == []
    assert backend.registriesAllowed is False

    identity = backend.runtime.identity()
    policy = parse_policy('version = 1\n[network]\nmode = "registries"\n')
    backend.storage.save_workspace_config(
        identity, backend.runtime.workspace_path, {"policy": policy.to_config()}
    )
    assert backend.registriesAllowed is True
    backend.requestRegistryAccess(True)
    assert len(confirmations) == 1 and backend.networkMode == "none"
    backend.resolveConfirmation(confirmations[0], True)
    assert backend.networkMode == "registries" and restarts == ["registries"]
    assert backend.generation.network_mode() == "registries"
    backend.selectMode("plan")
    assert backend.generation.network_mode() == "none"
    backend.selectMode("agent")
    backend.requestRegistryAccess(False)
    assert backend.networkMode == "none" and restarts == ["registries", "none"]


def test_agent_run_request_carries_the_selected_policy(bridge, monkeypatch) -> None:
    backend, confirmations, _errors = bridge
    backend.requestSandbox(True)
    backend.resolveConfirmation(confirmations[0], True)
    captured: dict[str, object] = {}

    class FakeWorker:
        auto_approve = False

        @classmethod
        def for_run(cls, _client, **kwargs):
            captured.update(kwargs)
            return cls()

        def __getattr__(self, _name):
            return SimpleNamespace(connect=lambda *_args: None)

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr("src.desktop.generation.AgentWorker", FakeWorker)
    monkeypatch.setattr("src.desktop.generation.get_api_key", lambda: "key")
    backend.runtime.gateway_token = "t" * 43
    backend.sendMessage("change the file", False)
    assert captured["approval_policy"] == "sandbox"
    assert captured["network_mode"] == "none"
    assert captured["started"] is True
