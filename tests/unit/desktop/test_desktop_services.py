"""Unit tests for the desktop services composed by ``DesktopBridge``.

Each service is exercised in isolation against a small fake host that records what it would
show in QML (signals, status line, confirmations) and supplies stub sibling services.
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

from shared.events import EventEnvelope
from shared.tools import PublishManifest
from src.connection import HealthResult, HealthState
from src.desktop import (
    ConversationService,
    GenerationService,
    RuntimeService,
    SettingsService,
    StagingPublicationService,
)
from src.desktop import runtime as runtime_module
from src.desktop import staging_publication as staging_module
from src.storage import Storage
from src.workspace_broker import workspace_id


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def emit(self, *args: Any) -> None:
        self.calls.append(args)

    def connect(self, *_args: Any) -> None:
        return None

    def __len__(self) -> int:
        return len(self.calls)


class FakeHost(QObject):
    SIGNALS = (
        "stateChanged", "settingsChanged", "conversationsChanged", "transcriptChanged", "modelsChanged",
        "permissionsChanged", "commandsChanged", "skillsChanged", "presetsChanged", "focusComposerRequested",
        "infoRequested", "errorRequested", "approvalRequested", "runtimeSetupStarted", "runtimeSetupFinished",
        "fileExported", "fileImported",
    )

    def __init__(self) -> None:
        super().__init__()
        for name in self.SIGNALS:
            setattr(self, name, Recorder())
        self.status_text = "Ready"
        self.statuses: list[str] = []
        self.confirmations: dict[str, tuple[str, str, Any, Any]] = {}
        self.model_reloads = 0
        self.permission_loads = 0
        self.runtime: Any = SimpleNamespace(workspace_path="", gateway_token="", connection=lambda: None, identity=lambda: "workspace", has_capability=lambda _name: False, workspace_ready=lambda: False, show_status=lambda: None)
        self.settings: Any = SimpleNamespace(reload_skills=lambda: None, refresh_catalog_if_stale=lambda: None, catalog=SimpleNamespace(models=lambda: []), catalog_autorefresh_attempted=False)
        noop = lambda *_args, **_kwargs: None  # noqa: E731
        self.history: Any = SimpleNamespace(refresh_transcript=noop, schedule_transcript_refresh=noop, new_conversation=noop, fork=noop, compact_context=noop, inspect_context=noop, show_usage=noop)
        self.generation: Any = SimpleNamespace(running=False, live_events=[], auto_mode=False, stop_auto=lambda: None, reset_for_workspace_change=lambda: None, continue_queued_input=lambda: None)
        self.staging: Any = SimpleNamespace(busy=False, publishing=False)

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.statuses.append(text)

    def confirm(self, token, title, body, on_accept, on_decline=None) -> None:
        self.confirmations[token] = (title, body, on_accept, on_decline)

    def accept(self, token: str) -> None:
        self.confirmations.pop(token)[2]()

    def reload_models(self) -> None:
        self.model_reloads += 1

    def loadPermissionRules(self) -> None:
        self.permission_loads += 1


@pytest.fixture
def host():
    return FakeHost()


@pytest.fixture
def storage(tmp_path: Path):
    value = Storage(tmp_path / "chat.sqlite3")
    yield value
    value.close()


def _status(workspace: Path, **extra: Any) -> SimpleNamespace:
    values = {
        "ready": True, "executor_present": True, "executor_status": "ready",
        "active_workspace": workspace_id(workspace), "supported_modes": ("chat", "plan", "agent"),
        "capabilities": ({"name": "agent_auto"}, {"name": "agent_accept_edits"}),
    }
    values.update(extra)
    return SimpleNamespace(**values)


# -- RuntimeService --------------------------------------------------------------------


def _runtime(host: FakeHost, storage: Storage) -> RuntimeService:
    service = RuntimeService(host, storage, automatic=False, manager=SimpleNamespace())
    host.runtime = service
    return service


def test_runtime_workspace_readiness_and_capabilities(host, storage, tmp_path: Path) -> None:
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    runtime = _runtime(host, storage)
    runtime.workspace_path = str(workspace)
    assert runtime.workspace_ready() is False  # no gateway status yet
    runtime.last_status = _status(workspace)
    assert runtime.workspace_ready() and runtime.mode_available("agent")
    assert runtime.has_capability("agent_accept_edits") and not runtime.has_capability("other")
    runtime.last_status = _status(workspace, active_workspace="someone-else")
    assert not runtime.workspace_ready() and not runtime.mode_available("agent")


def test_runtime_health_labels_and_catalog_refresh_only_when_healthy(host, storage, tmp_path: Path) -> None:
    refreshes: list[int] = []
    host.settings.refresh_catalog_if_stale = lambda: refreshes.append(1)
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    runtime = _runtime(host, storage)
    runtime.apply_health(HealthResult(HealthState.STARTING, "starting"))
    assert runtime.gateway_text == "Gateway: starting" and refreshes == []
    runtime.workspace_path = str(workspace)
    runtime.apply_health(HealthResult(HealthState.READY, "ok", _status(workspace, executor_status="starting")))
    assert runtime.gateway_text == "Executor: unavailable" and refreshes == [1]
    runtime.state = "building"
    runtime.apply_health(HealthResult(HealthState.DISCONNECTED, "offline"))
    assert runtime.gateway_text == "Executor: unavailable"  # ignored while the runtime is busy


def test_runtime_gateway_token_is_used_from_memory_immediately_after_save(host, storage, monkeypatch) -> None:
    saved: list[str] = []
    monkeypatch.setattr(runtime_module, "save_gateway_token", saved.append)
    runtime = _runtime(host, storage)
    assert runtime.connection() is None
    assert runtime.save_gateway("http://127.0.0.1:8765", "n" * 43) is True
    assert saved == ["n" * 43]
    connection = runtime.connection()
    assert connection is not None and connection.token == "n" * 43
    assert runtime.save_gateway("not a url", "") is False
    assert host.errorRequested.calls[-1][0] == "Invalid gateway settings"


def test_runtime_failure_is_reported(host, storage) -> None:
    runtime = _runtime(host, storage)
    runtime._runtime_failed("docker missing")
    assert runtime.state == "failed" and runtime.gateway_text == "Runtime: setup failed"
    assert host.errorRequested.calls == [("Local runtime setup failed", "docker missing")]


def test_runtime_select_workspace_resets_session_and_rejects_unsafe_paths(host, storage, tmp_path: Path) -> None:
    resets: list[int] = []
    host.generation.reset_for_workspace_change = lambda: resets.append(1)
    runtime = _runtime(host, storage)
    runtime.select_workspace("/")
    assert host.errorRequested.calls[-1][0] == "Unsafe workspace" and resets == []
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    runtime.health_worker = object()  # a check is in flight: the new check is queued behind it
    runtime.select_workspace(str(workspace))
    assert runtime.workspace_path == str(workspace.resolve()) and resets == [1]
    assert storage.get_setting("workspace_path") == str(workspace.resolve())
    assert runtime.target_identity == workspace_id(workspace)
    assert host.statuses[-1] == "Workspace selected; checking developer runtime…"


# -- SettingsService -------------------------------------------------------------------


def test_settings_catalog_autorefresh_happens_once_per_gateway(host, storage, monkeypatch) -> None:
    settings = SettingsService(host, storage, catalog=SimpleNamespace(is_stale=lambda: True, models=lambda: []))
    started: list[bool] = []
    monkeypatch.setattr(settings, "start_catalog_refresh", lambda *, report_errors: started.append(report_errors))
    settings.refresh_catalog_if_stale()
    settings.refresh_catalog_if_stale()
    assert started == [False]


def test_settings_preferences_round_trip(host, storage) -> None:
    settings = SettingsService(host, storage)
    settings.set_server_tool("web_search", True)
    settings.set_server_tool("shell", True)
    assert settings.server_tools() == {"web_search": True, "web_fetch": False, "datetime": False}
    settings.set_theme("neon")
    assert settings.theme == "dark"
    settings.save_model_options("vendor/model", {"max_tokens": "256", "reasoning_effort": "extreme", "stop": ["END"], "data_collection": "allow"})
    options = settings.request_options("vendor/model")
    assert (options.max_tokens, options.reasoning_effort, options.stop, options.data_collection) == (256, None, ["END"], "allow")
    assert host.statuses[-1] == "Model and privacy controls saved"


def test_settings_commands_include_builtins_and_invalid_input_is_reported(host, storage) -> None:
    settings = SettingsService(host, storage)
    settings.reload_commands()
    assert {"new", "mode", "pause"} <= {item["name"] for item in settings.commands if item["builtin"]}
    settings.save_prompt_command("", "", "")
    assert host.errorRequested.calls[-1][0] == "Invalid command"


def test_settings_permission_changes_reload_through_the_bridge(host, storage, monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeClient:
        def __init__(self, _connection) -> None:
            pass

        def set_permission_rule_enabled(self, rule_id, enabled):
            calls.append(("enabled", rule_id, enabled))

        def delete_permission_rule(self, rule_id):
            calls.append(("delete", rule_id))

    monkeypatch.setattr("src.desktop.preferences.GatewayClient", FakeClient)
    host.runtime.connection = lambda: object()
    settings = SettingsService(host, storage)
    settings.set_permission_enabled("rule", False)
    settings.delete_permission("rule")
    assert calls == [("enabled", "rule", False), ("delete", "rule")]
    assert host.permission_loads == 2


# -- ConversationService ---------------------------------------------------------------


def test_conversations_bootstrap_and_housekeeping(host, storage) -> None:
    history = ConversationService(host, storage)
    host.history = history
    history.load()
    assert len(history.conversations) == 1 and history.current() is not None
    history.rename("  Renamed  ")
    assert history.current().title == "Renamed"
    history.toggle_pin()
    assert history.conversations[0]["pinned"] is True
    history.fork()
    assert history.current().title == "Renamed (fork)" and host.statuses[-1] == "Conversation forked"


def test_conversation_mutations_are_refused_while_generating(host, storage) -> None:
    history = ConversationService(host, storage)
    history.load()
    host.generation.running = True
    before = history.current().title
    history.rename("Changed")
    history.request_delete()
    history.new_conversation()
    assert history.current().title == before and host.confirmations == {} and len(history.conversations) == 1


def test_conversation_delete_requires_confirmation(host, storage) -> None:
    history = ConversationService(host, storage)
    history.load()
    doomed = history.current_id
    history.request_delete()
    (token,) = host.confirmations
    assert token == f"delete:{doomed}"
    host.accept(token)
    assert storage.get_conversation(doomed) is None


def test_transcript_shows_live_activity_only_while_generating(host, storage) -> None:
    history = ConversationService(host, storage)
    history.load()
    storage.add_message(history.current_id, "user", "hello")
    live = {"run_id": "run", "event_id": 1, "type": "tool.requested", "payload": {"request_id": "1", "tool": "read"}}
    host.generation.live_events = [live]
    history.refresh_transcript()
    assert [item["kind"] for item in history.transcript] == ["user"]
    host.generation.running = True
    history.refresh_transcript()
    assert [item["kind"] for item in history.transcript][-1] == "assistant"
    assert history.transcript_model.rowCount() == len(history.transcript)


# -- GenerationService -----------------------------------------------------------------


def _generation(host: FakeHost, storage: Storage, tmp_path: Path) -> GenerationService:
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = _runtime(host, storage)
    runtime.workspace_path = str(workspace)
    runtime.state = "manual"
    runtime.last_status = _status(workspace)
    generation = GenerationService(host, storage)
    host.generation = generation
    return generation


def test_generation_approval_tiers_reset_with_workspace_and_mode(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    generation.select_mode("agent")
    generation.request_accept_edits(True)
    host.accept(next(iter(host.confirmations)))
    generation.request_auto_mode(True)
    host.accept(next(iter(host.confirmations)))
    assert generation.approval_policy() == "auto"
    generation.stop_auto()
    assert generation.approval_policy() == "accept_edits"
    generation.reset_for_workspace_change()
    assert generation.approval_policy() == "prompt" and generation.mode == "chat"


def test_generation_rejects_agent_mode_without_a_ready_workspace(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    host.runtime.last_status = None
    generation.select_mode("agent")
    assert generation.mode == "chat" and host.errorRequested.calls[-1][0] == "Workspace is still preparing"


def test_generation_commands_and_publication_gate(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    generation.send_message("/mode plan")
    assert generation.mode == "plan"
    generation.send_message("/nonsense")
    assert host.statuses[-1] == "Unknown command: /nonsense"
    host.staging.publishing = True
    generation.send_message("hello")
    assert host.statuses[-1] == "Wait for staged publication to finish…"


def test_generation_queues_input_while_running(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    generation.worker = object()  # type: ignore[assignment]
    generation.send_message("first")
    generation.send_message("steer now", True)
    assert host.statuses[-1] == "Queued 2 message(s)"
    assert generation.controller.next_input().text == "steer now"


def test_generation_routes_manifests_and_stops_auto_on_publication_failure(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    manifests: list[dict[str, Any]] = []
    host.staging.handle_manifest = lambda payload, **kwargs: manifests.append({"payload": payload, **kwargs})
    generation.auto_mode = True
    event = EventEnvelope(7, "run-1", "checkpoint.created", "2026-01-01T00:00:00Z", {"publish_manifest": {"manifest_id": "m"}, "auto_publish": True})
    generation.on_agent_event(event)
    assert manifests == [{"payload": {"manifest_id": "m"}, "token": "publish:run-1:7", "auto": True, "auto_authorized": False}]
    generation.on_agent_event(EventEnvelope(8, "run-1", "publication.failed", "2026-01-01T00:00:00Z", {}))
    assert generation.auto_mode is False


def test_generation_forwards_tool_approvals_to_qml(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    event = EventEnvelope(3, "run-1", "approval.required", "2026-01-01T00:00:00Z", {"approval_id": "a1", "kind": "tool", "tool": "bash", "arguments": {"command": "pytest"}})
    generation.on_agent_event(event)
    (request,) = host.approvalRequested.calls
    assert request[0]["key"] == "run-1:a1" and request[0]["allowRule"] is True


# -- StagingPublicationService ---------------------------------------------------------


def _manifest(**overrides: Any) -> PublishManifest:
    values = {"manifest_id": "m1", "run_id": "run", "workspace_id": "w", "source_snapshot_id": "s", "approval_id": "a", "operations": [], "publication_id": "pub", "batch_index": 1, "batch_count": 1}
    values.update(overrides)
    return PublishManifest.from_dict(values)


class FakePublicationWorker:
    started: list[FakePublicationWorker] = []

    def __init__(self, manifest, workspace_root, recovery_root, *, reseed_client=None) -> None:
        self.manifest = manifest
        self.continuation_client = None
        self.complete = self.failed = self.finished = Recorder()

    def start(self) -> None:
        FakePublicationWorker.started.append(self)

    def deleteLater(self) -> None:
        return None


@pytest.fixture
def staging(host, tmp_path: Path, monkeypatch):
    FakePublicationWorker.started = []
    monkeypatch.setattr(staging_module, "PublicationWorker", FakePublicationWorker)
    stopped: list[int] = []
    host.generation.stop_auto = lambda: stopped.append(1)
    host.runtime.workspace_path = str(tmp_path)
    service = StagingPublicationService(host, recovery_root=lambda: tmp_path / "recovery")
    host.staging = service
    return service, stopped


def test_unauthorized_auto_publication_is_blocked(host, staging) -> None:
    service, stopped = staging
    service.handle_manifest(_manifest().to_dict(), token="publish:run:1", auto=True, auto_authorized=False)
    assert FakePublicationWorker.started == [] and stopped == [1]
    assert host.errorRequested.calls[-1] == ("Publication blocked", "Auto publication is not authorized by this desktop session")


def test_reviewed_publication_starts_only_after_confirmation(host, staging) -> None:
    service, _stopped = staging
    service.handle_manifest(_manifest().to_dict(), token="publish:run:1", auto=False, auto_authorized=False)
    assert FakePublicationWorker.started == [] and host.confirmations["publish:run:1"][0] == "Publish staged changes to the host?"
    host.accept("publish:run:1")
    assert len(FakePublicationWorker.started) == 1 and service.busy and service.publishing


def test_multi_batch_publication_confirms_each_batch_and_offers_retry(host, staging) -> None:
    service, stopped = staging
    first = _manifest(batch_count=2)
    service.start_publication(first, auto=False)
    following = _manifest(manifest_id="m2", batch_index=2, batch_count=2)
    service._on_publication_complete({"completed_paths": ["a"], "checkpoint_id": "abcdef1234", "batch_index": 1, "batch_count": 2, "next_manifest": following.to_dict()})
    assert host.statuses[-1] == "Published 1 file(s) (batch 1 of 2); checkpoint abcdef12"
    service._on_publication_finished()
    assert host.confirmations["publish:pub:2"][0] == "Publish batch 2 of 2?"
    host.accept("publish:pub:2")
    assert FakePublicationWorker.started[-1].manifest.batch_index == 2
    service._on_publication_failed("hash mismatch")
    assert stopped and host.confirmations["publish-retry:pub:2"][0] == "Publish batch 2 of 2?"


def test_auto_multi_batch_publication_continues_without_prompts(host, staging) -> None:
    service, _stopped = staging
    host.generation.auto_mode = True
    host.runtime.connection = lambda: None
    service.start_publication(_manifest(batch_count=2), auto=True, client=object())
    FakePublicationWorker.started[-1].continuation_client = object()
    following = _manifest(manifest_id="m2", batch_index=2, batch_count=2)
    service._on_publication_complete({"completed_paths": [], "checkpoint_id": "c", "batch_index": 1, "batch_count": 2, "next_manifest": following.to_dict()})
    service._on_publication_finished()
    assert host.confirmations == {} and FakePublicationWorker.started[-1].manifest.batch_index == 2


def test_auto_batch_without_a_gateway_client_stops_auto(host, staging) -> None:
    service, stopped = staging
    host.runtime.connection = lambda: None
    service.start_publication(_manifest(), auto=True)
    assert FakePublicationWorker.started == [] and stopped == [1]
    assert host.errorRequested.calls[-1] == ("Publication blocked", "Auto publication requires the local gateway")


def test_idle_publication_finish_resumes_queued_input(host, staging) -> None:
    service, _stopped = staging
    resumed: list[int] = []
    host.generation.continue_queued_input = lambda: resumed.append(1)
    service.start_publication(_manifest(), auto=False)
    service._on_publication_finished()
    assert resumed == [1] and not service.busy
