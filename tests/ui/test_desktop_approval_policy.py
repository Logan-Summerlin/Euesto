"""Desktop selection of the Agent approval tier (prompt / accept_edits / auto)."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from src.qml_backend import DesktopBridge
except ImportError as exc:  # pragma: no cover - Qt unavailable
    pytest.skip(f"Desktop Qt bridge unavailable: {exc}", allow_module_level=True)

from src.storage import Storage
from src.workspace_broker import workspace_id


@pytest.fixture
def bridge(tmp_path: Path):
    storage = Storage(tmp_path / "chat.sqlite3")
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    backend = DesktopBridge(storage)
    backend.health_timer.stop()
    backend.workspace_path = str(workspace.resolve())
    backend.runtime_state = "manual"
    backend.last_gateway_status = SimpleNamespace(
        ready=True,
        executor_present=True,
        executor_status="ready",
        active_workspace=workspace_id(workspace),
        supported_modes=("chat", "plan", "agent"),
        capabilities=({"name": "agent_accept_edits"}, {"name": "agent_auto"}),
    )
    confirmations: list[str] = []
    errors: list[str] = []
    backend.confirmRequested.connect(lambda token, _title, _body: confirmations.append(token))
    backend.errorRequested.connect(lambda title, _body: errors.append(title))
    backend.selectMode("agent")
    yield backend, confirmations, errors
    backend.health_timer.stop()
    storage.close()


def test_accept_edits_requires_confirmation_and_selects_the_middle_tier(bridge) -> None:
    backend, confirmations, errors = bridge
    assert backend.approvalPolicy == "prompt"
    assert backend.acceptEditsAvailable is True

    backend.requestAcceptEdits(True)
    assert errors == [] and len(confirmations) == 1
    assert backend.acceptEditsEnabled is False  # not until the user confirms
    backend.resolveConfirmation(confirmations[0], True)
    assert backend.acceptEditsEnabled is True
    assert backend.approvalPolicy == "accept_edits"

    # Auto supersedes the middle tier while enabled; turning Auto off falls back to it.
    backend.requestAutoMode(True)
    backend.resolveConfirmation(confirmations[-1], True)
    assert backend.approvalPolicy == "auto"
    backend.requestAutoMode(False)
    assert backend.approvalPolicy == "accept_edits"

    backend.requestAcceptEdits(False)
    assert backend.approvalPolicy == "prompt"


def test_declined_confirmation_keeps_prompting(bridge) -> None:
    backend, confirmations, _errors = bridge
    backend.requestAcceptEdits(True)
    backend.resolveConfirmation(confirmations[0], False)
    assert backend.approvalPolicy == "prompt"


def test_accept_edits_resets_on_mode_change(bridge) -> None:
    backend, confirmations, _errors = bridge
    backend.requestAcceptEdits(True)
    backend.resolveConfirmation(confirmations[0], True)
    backend.selectMode("plan")
    assert backend.approvalPolicy == "prompt" and backend.acceptEditsEnabled is False


def test_accept_edits_unavailable_without_gateway_capability(bridge) -> None:
    backend, confirmations, errors = bridge
    backend.last_gateway_status.capabilities = ({"name": "agent_auto"},)
    assert backend.acceptEditsAvailable is False
    backend.requestAcceptEdits(True)
    assert confirmations == [] and errors == ["Accept edits unavailable"]


def test_agent_run_request_carries_the_selected_policy(bridge, monkeypatch) -> None:
    backend, confirmations, _errors = bridge
    backend.requestAcceptEdits(True)
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

    monkeypatch.setattr("src.qml_backend.AgentWorker", FakeWorker)
    monkeypatch.setattr("src.qml_backend.get_api_key", lambda: "key")
    backend._gateway_token = "t" * 43
    backend.sendMessage("change the file", False)
    assert captured["approval_policy"] == "accept_edits"
    assert captured["started"] is True
