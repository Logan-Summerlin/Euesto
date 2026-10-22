"""Unit tests for the desktop services composed by ``DesktopBridge``.

Each service is exercised in isolation against a small fake host that records what it would
show in QML (signals, status line, confirmations) and supplies stub sibling services.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
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
from shared.policy import WorkspacePolicy, parse_policy
from shared.responses import GatewayStatus
from shared.tools import PublishManifest, PublishOperation
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
from src.gateway_client import GatewayConnection, GatewayError
from src.storage import Storage
from src.workers import AgentWorker, StagingWorker
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
        "stateChanged",
        "settingsChanged",
        "conversationsChanged",
        "transcriptChanged",
        "modelsChanged",
        "permissionsChanged",
        "commandsChanged",
        "skillsChanged",
        "presetsChanged",
        "focusComposerRequested",
        "infoRequested",
        "errorRequested",
        "approvalRequested",
        "runtimeSetupStarted",
        "runtimeSetupFinished",
        "fileExported",
        "fileImported",
        "meterChanged",
        "conflictsChanged",
        "leaveRequested",
        "policyChanged",
        "reviewChanged",
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
        self.runtime: Any = SimpleNamespace(
            workspace_path="",
            network_mode="none",
            gateway_token="",
            connection=lambda: None,
            identity=lambda: "workspace",
            has_capability=lambda _name: False,
            workspace_ready=lambda: False,
            show_status=lambda: None,
        )
        self.settings: Any = SimpleNamespace(
            reload_skills=lambda: None,
            refresh_catalog_if_stale=lambda: None,
            catalog=SimpleNamespace(models=lambda: []),
            catalog_autorefresh_attempted=False,
        )
        noop = lambda *_args, **_kwargs: None  # noqa: E731
        self.history: Any = SimpleNamespace(
            refresh_transcript=noop,
            schedule_transcript_refresh=noop,
            new_conversation=noop,
            fork=noop,
            compact_context=noop,
            inspect_context=noop,
            show_usage=noop,
        )
        self.generation: Any = SimpleNamespace(
            running=False,
            live_events=[],
            live_text="",
            auto_mode=False,
            stop_auto=lambda: None,
            reset_for_workspace_change=lambda: None,
            continue_queued_input=lambda: None,
        )
        self.staging: Any = SimpleNamespace(busy=False, publishing=False)
        self.policy: Any = SimpleNamespace(
            effective=lambda: WorkspacePolicy(),
            refresh=noop,
            allows_registries=lambda: False,
            runtime_settings=lambda base, network_mode: base,
        )

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


def _status(workspace: Path, **extra: Any) -> GatewayStatus:
    values = {
        "ready": True,
        "executor_present": True,
        "executor_status": "ready",
        "active_workspace": workspace_id(workspace),
        "supported_modes": ("chat", "plan", "agent"),
        "capabilities": ({"name": "agent_auto"}, {"name": "agent_sandbox"}),
    }
    values.update(extra)
    return GatewayStatus(**values)


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
    assert runtime.has_capability("agent_sandbox") and not runtime.has_capability("other")
    runtime.last_status = _status(workspace, active_workspace="someone-else")
    assert not runtime.workspace_ready() and not runtime.mode_available("agent")


def test_runtime_health_labels_and_catalog_refresh_only_when_healthy(
    host, storage, tmp_path: Path
) -> None:
    refreshes: list[int] = []
    host.settings.refresh_catalog_if_stale = lambda: refreshes.append(1)
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    runtime = _runtime(host, storage)
    runtime.apply_health(HealthResult(HealthState.STARTING, "starting"))
    assert runtime.gateway_text == "Gateway: starting" and refreshes == []
    runtime.workspace_path = str(workspace)
    runtime.apply_health(
        HealthResult(HealthState.READY, "ok", _status(workspace, executor_status="starting"))
    )
    assert runtime.gateway_text == "Executor: unavailable" and refreshes == [1]
    runtime.state = "building"
    runtime.apply_health(HealthResult(HealthState.DISCONNECTED, "offline"))
    assert runtime.gateway_text == "Executor: unavailable"  # ignored while the runtime is busy


def test_runtime_gateway_token_is_used_from_memory_immediately_after_save(
    host, storage, monkeypatch
) -> None:
    saved: list[str] = []
    monkeypatch.setattr(runtime_module, "save_gateway_token", saved.append)
    monkeypatch.setattr(runtime_module, "get_gateway_session_token", lambda: None)
    monkeypatch.setattr(runtime_module, "get_gateway_token", lambda: None)
    monkeypatch.setattr("src.desktop.preferences.get_api_key", lambda: None)
    runtime = _runtime(host, storage)
    settings = SettingsService(
        host, storage, catalog=SimpleNamespace(is_stale=lambda: False, models=lambda: [])
    )
    assert runtime.connection() is None and settings.gateway_settings()["hasToken"] is False
    assert runtime.save_gateway("http://127.0.0.1:8765", "n" * 43) is True
    assert saved == ["n" * 43]
    connection = runtime.connection()
    assert connection is not None and connection.token == "n" * 43
    assert settings.gateway_settings()["hasToken"] is True
    assert runtime.save_gateway("not a url", "") is False
    assert host.errorRequested.calls[-1][0] == "Invalid gateway settings"


def test_runtime_prefers_the_active_local_gateway_session_token(
    host, storage, monkeypatch
) -> None:
    monkeypatch.setattr(runtime_module, "get_gateway_token", lambda: "k" * 43)
    monkeypatch.setattr(runtime_module, "get_gateway_session_token", lambda: "s" * 43)
    assert _runtime(host, storage).gateway_token == "s" * 43
    monkeypatch.setattr(runtime_module, "get_gateway_session_token", lambda: None)
    assert _runtime(host, storage).gateway_token == "k" * 43


def test_runtime_failure_is_reported(host, storage) -> None:
    runtime = _runtime(host, storage)
    runtime._runtime_failed("docker missing")
    assert runtime.state == "failed" and runtime.gateway_text == "Runtime: setup failed"
    assert host.errorRequested.calls == [("Local runtime setup failed", "docker missing")]


def test_runtime_select_workspace_resets_session_and_rejects_unsafe_paths(
    host, storage, tmp_path: Path
) -> None:
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


def test_settings_catalog_autorefresh_happens_once_per_gateway(
    host, storage, monkeypatch
) -> None:
    settings = SettingsService(
        host, storage, catalog=SimpleNamespace(is_stale=lambda: True, models=lambda: [])
    )
    started: list[bool] = []
    monkeypatch.setattr(
        settings,
        "start_catalog_refresh",
        lambda *, report_errors: started.append(report_errors),
    )
    settings.refresh_catalog_if_stale()
    settings.refresh_catalog_if_stale()
    assert started == [False]


def test_settings_preferences_round_trip(host, storage) -> None:
    settings = SettingsService(host, storage)
    settings.set_server_tool("web_search", True)
    settings.set_server_tool("shell", True)
    assert settings.server_tools() == {
        "web_search": True,
        "web_fetch": False,
        "datetime": False,
    }
    settings.set_theme("neon")
    assert settings.theme == "dark"
    settings.save_model_options(
        "vendor/model",
        {
            "max_tokens": "256",
            "reasoning_effort": "extreme",
            "stop": ["END"],
            "data_collection": "allow",
        },
    )
    options = settings.request_options("vendor/model")
    assert (
        options.max_tokens,
        options.reasoning_effort,
        options.stop,
        options.data_collection,
    ) == (256, None, ["END"], "allow")
    assert host.statuses[-1] == "Model and privacy controls saved"


def test_settings_toggle_favorite_model_round_trips(host, storage) -> None:
    settings = SettingsService(host, storage)
    settings.toggle_favorite_model("vendor/model")
    assert storage.favorite_model_ids() == ["vendor/model"]
    settings.toggle_favorite_model("vendor/model")
    assert storage.favorite_model_ids() == []
    assert host.model_reloads == 2


def test_settings_commands_include_builtins_and_invalid_input_is_reported(
    host, storage
) -> None:
    settings = SettingsService(host, storage)
    settings.reload_commands()
    assert {"new", "mode", "pause"} <= {
        item["name"] for item in settings.commands if item["builtin"]
    }
    settings.save_prompt_command("", "", "")
    assert host.errorRequested.calls[-1][0] == "Invalid command"


def test_settings_permission_changes_reload_through_the_bridge(
    host, storage, monkeypatch
) -> None:
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
    assert (
        history.current().title == "Renamed (fork)"
        and host.statuses[-1] == "Conversation forked"
    )


def test_conversation_mutations_are_refused_while_generating(host, storage) -> None:
    history = ConversationService(host, storage)
    history.load()
    host.generation.running = True
    before = history.current().title
    history.rename("Changed")
    history.request_delete()
    history.new_conversation()
    assert (
        history.current().title == before
        and host.confirmations == {}
        and len(history.conversations) == 1
    )


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
    live = {
        "run_id": "run",
        "event_id": 1,
        "type": "tool.requested",
        "payload": {"request_id": "1", "tool": "read"},
    }
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


def test_generation_approval_tiers_reset_with_workspace_and_mode(
    host, storage, tmp_path: Path
) -> None:
    generation = _generation(host, storage, tmp_path)
    generation.select_mode("agent")
    generation.request_sandbox(True)
    host.accept(next(iter(host.confirmations)))
    generation.request_auto_mode(True)
    host.accept(next(iter(host.confirmations)))
    assert generation.approval_policy() == "auto"
    generation.stop_auto()
    assert generation.approval_policy() == "sandbox"
    generation.reset_for_workspace_change()
    assert generation.approval_policy() == "prompt" and generation.mode == "chat"


def test_generation_rejects_agent_mode_without_a_ready_workspace(
    host, storage, tmp_path: Path
) -> None:
    generation = _generation(host, storage, tmp_path)
    host.runtime.last_status = None
    generation.select_mode("agent")
    assert (
        generation.mode == "chat"
        and host.errorRequested.calls[-1][0] == "Workspace is still preparing"
    )


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


def test_generation_routes_manifests_and_stops_auto_on_publication_failure(
    host, storage, tmp_path: Path
) -> None:
    generation = _generation(host, storage, tmp_path)
    manifests: list[dict[str, Any]] = []
    host.staging.handle_manifest = lambda payload, **kwargs: manifests.append(
        {"payload": payload, **kwargs}
    )
    generation.auto_mode = True
    event = EventEnvelope(
        7,
        "run-1",
        "checkpoint.created",
        "2026-01-01T00:00:00Z",
        {"publish_manifest": {"manifest_id": "m"}, "auto_publish": True},
    )
    generation.on_agent_event(event)
    assert manifests == [
        {
            "payload": {"manifest_id": "m"},
            "token": "publish:run-1:7",
            "auto": True,
            "auto_authorized": False,
        }
    ]
    generation.on_agent_event(
        EventEnvelope(8, "run-1", "publication.failed", "2026-01-01T00:00:00Z", {})
    )
    assert generation.auto_mode is False


def test_generation_forwards_tool_approvals_to_qml(host, storage, tmp_path: Path) -> None:
    generation = _generation(host, storage, tmp_path)
    event = EventEnvelope(
        3,
        "run-1",
        "approval.required",
        "2026-01-01T00:00:00Z",
        {
            "approval_id": "a1",
            "kind": "tool",
            "tool": "bash",
            "arguments": {"command": "pytest"},
        },
    )
    generation.on_agent_event(event)
    (request,) = host.approvalRequested.calls
    assert request[0]["key"] == "run-1:a1" and request[0]["allowRule"] is True


def test_streamed_text_is_buffered_and_only_activity_events_refresh_the_transcript(
    host, storage, tmp_path: Path
) -> None:
    history = ConversationService(host, storage)
    history.load()
    generation = _generation(host, storage, tmp_path)
    host.history = history
    generation.controller.state.conversation_id = history.current_id
    refreshes: list[str] = []
    history.refresh_transcript = lambda **_kwargs: refreshes.append("now")
    history.schedule_transcript_refresh = lambda: refreshes.append("scheduled")

    # Streamed text is buffered for the final message, never pushed into the transcript.
    generation.on_stream_chunk("partial ")
    generation.on_stream_chunk("answer")
    assert generation.controller.state.stream_text == "partial answer"
    assert refreshes == []

    # Deltas and tool output are not persisted; activity events are, and schedule one refresh.
    for event_id, event_type in enumerate(("model.delta", "tool.output", "tool.completed"), 1):
        generation.on_agent_event(
            EventEnvelope(
                event_id,
                "run-1",
                event_type,
                "2026-01-01T00:00:00Z",
                {"request_id": "1", "tool": "read", "text": "x", "output": "x"},
            )
        )
    assert storage.list_run_events(history.current_id) == []
    generation.on_agent_event(
        EventEnvelope(
            4,
            "run-1",
            "tool.requested",
            "2026-01-01T00:00:00Z",
            {"request_id": "1", "tool": "read"},
        )
    )
    assert [event["type"] for event in storage.list_run_events(history.current_id)] == [
        "tool.requested"
    ]
    assert refreshes.count("scheduled") >= 1 and "now" not in refreshes

    del history.refresh_transcript
    history.refresh_transcript()
    assert all("partial answer" not in str(item.get("content")) for item in history.transcript)


# -- StagingPublicationService ---------------------------------------------------------


def _manifest(**overrides: Any) -> PublishManifest:
    values = {
        "manifest_id": "m1",
        "run_id": "run",
        "workspace_id": "w",
        "source_snapshot_id": "s",
        "approval_id": "a",
        "operations": [],
        "publication_id": "pub",
        "batch_index": 1,
        "batch_count": 1,
    }
    values.update(overrides)
    return PublishManifest.from_dict(values)


class FakePublicationWorker:
    started: list[FakePublicationWorker] = []

    def __init__(
        self, manifest, workspace_root, recovery_root, *, reseed_client=None, **options
    ) -> None:
        self.manifest = manifest
        self.options = options
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
    service.handle_manifest(
        _manifest().to_dict(), token="publish:run:1", auto=True, auto_authorized=False
    )
    assert FakePublicationWorker.started == [] and stopped == [1]
    assert host.errorRequested.calls[-1] == (
        "Publication blocked",
        "Auto publication is not authorized by this desktop session",
    )


def test_reviewed_publication_starts_only_after_confirmation(host, staging) -> None:
    service, _stopped = staging
    service.handle_manifest(
        _manifest().to_dict(), token="publish:run:1", auto=False, auto_authorized=False
    )
    assert (
        FakePublicationWorker.started == []
        and host.confirmations["publish:run:1"][0] == "Publish staged changes to the host?"
    )
    host.accept("publish:run:1")
    assert len(FakePublicationWorker.started) == 1 and service.busy and service.publishing


def test_multi_batch_publication_confirms_each_batch_and_offers_retry(host, staging) -> None:
    service, stopped = staging
    first = _manifest(batch_count=2)
    service.start_publication(first, auto=False)
    following = _manifest(manifest_id="m2", batch_index=2, batch_count=2)
    service._on_publication_complete(
        {
            "completed_paths": ["a"],
            "checkpoint_id": "abcdef1234",
            "batch_index": 1,
            "batch_count": 2,
            "next_manifest": following.to_dict(),
        }
    )
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
    service._on_publication_complete(
        {
            "completed_paths": [],
            "checkpoint_id": "c",
            "batch_index": 1,
            "batch_count": 2,
            "next_manifest": following.to_dict(),
        }
    )
    service._on_publication_finished()
    assert (
        host.confirmations == {} and FakePublicationWorker.started[-1].manifest.batch_index == 2
    )


def test_auto_batch_without_a_gateway_client_stops_auto(host, staging) -> None:
    service, stopped = staging
    host.runtime.connection = lambda: None
    service.start_publication(_manifest(), auto=True)
    assert FakePublicationWorker.started == [] and stopped == [1]
    assert host.errorRequested.calls[-1] == (
        "Publication blocked",
        "Auto publication requires the local gateway",
    )


def test_idle_publication_finish_resumes_queued_input(host, staging) -> None:
    service, _stopped = staging
    resumed: list[int] = []
    host.generation.continue_queued_input = lambda: resumed.append(1)
    service.start_publication(_manifest(), auto=False)
    service._on_publication_finished()
    assert resumed == [1] and not service.busy


def test_staging_review_describes_the_inspected_state() -> None:
    dirty = staging_module.describe_staged_changes(
        {"data": {"unpublished_changes": True, "file_count": 1200, "total_bytes": 4096}}
    )
    clean = staging_module.describe_staged_changes({"data": {"unpublished_changes": False}})

    assert dirty.startswith("Staging has unpublished changes.")
    assert "1,200 files, 4,096 bytes" in dirty
    assert clean.startswith("Staging is clean")


# -- persistent staging, leaving a workspace, steering, meter, settings (plans 0.6-2.7) ---


class Hook:
    """A signal stand-in that calls what is connected to it."""

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in self.slots:
            slot(*args)


class FakeStagingWorker:
    started: list[FakeStagingWorker] = []

    def __init__(self, client, workspace_id, action, resolutions=None, **options) -> None:
        self.action = action
        self.resolutions = resolutions
        self.options = options
        self.complete, self.failed, self.finished = Hook(), Hook(), Hook()

    def start(self) -> None:
        FakeStagingWorker.started.append(self)

    def deleteLater(self) -> None:
        return None

    def succeed(self, result: dict[str, Any]) -> None:
        self.complete.emit(result)
        self.finished.emit()


class ImmediateTimer:
    @staticmethod
    def singleShot(_delay: int, action: Any) -> None:
        action()


@pytest.fixture
def ready_staging(host, staging, monkeypatch):
    service, _stopped = staging
    FakeStagingWorker.started = []
    monkeypatch.setattr(staging_module, "StagingWorker", FakeStagingWorker)
    monkeypatch.setattr(staging_module, "QTimer", ImmediateTimer)
    host.runtime.connection = lambda: GatewayConnection(token="t" * 43)
    host.runtime.workspace_ready = lambda: True
    return service


DIRTY = {"data": {"unpublished_changes": True, "file_count": 3, "total_bytes": 10}}


def test_leaving_with_clean_or_unreachable_staging_proceeds_at_once(
    host, ready_staging
) -> None:
    left: list[int] = []
    host.runtime.connection = lambda: None
    ready_staging.guard_leave(lambda: left.append(1), closing=True)
    assert left == [1] and FakeStagingWorker.started == []

    host.runtime.connection = lambda: GatewayConnection(token="t" * 43)
    ready_staging.guard_leave(lambda: left.append(2), closing=True)
    FakeStagingWorker.started[-1].succeed({"data": {"unpublished_changes": False}})
    assert left == [1, 2] and host.leaveRequested.calls == []


@pytest.mark.parametrize("choice", ["keep", "discard", "publish", "cancel"])
def test_leaving_with_unpublished_changes_offers_each_choice(
    host, ready_staging, choice: str
) -> None:
    left: list[int] = []
    ready_staging.guard_leave(lambda: left.append(1), closing=True)
    inspect = FakeStagingWorker.started[-1]
    assert inspect.action == "inspect"
    inspect.succeed(DIRTY)
    title, body = host.leaveRequested.calls[-1]
    assert title == "Close with unpublished staged changes?"
    assert "Keep for later" in body and left == []

    ready_staging.resolve_leave(choice)
    if choice == "keep":
        assert left == [1]
    elif choice == "discard":
        discard = FakeStagingWorker.started[-1]
        assert discard.action == "discard" and left == []
        discard.succeed({"ok": True, "file_count": 3})
        assert left == [1]
    elif choice == "publish":
        publish = FakeStagingWorker.started[-1]
        assert publish.action == "publish"
        publish.succeed({"manifest": _manifest(operations=[]).to_dict()})
        assert host.statuses[-1] == "Nothing to publish: staging matches the workspace"
        assert left == []  # leaving waits until the user closes again
    else:
        assert left == [] and host.statuses[-1] == "Kept the workspace open"
    # A choice only applies once.
    ready_staging.resolve_leave("keep")
    assert left.count(1) <= 1


def test_publishing_kept_staged_work_asks_for_review(host, ready_staging) -> None:
    ready_staging.publish_staged()
    manifest = _manifest(
        operations=[
            {
                "path": "a.py",
                "operation": "update",
                "base_sha256": "0" * 64,
                "staged_sha256": hashlib.sha256(b"x").hexdigest(),
                "content": "x",
            }
        ]
    )
    FakeStagingWorker.started[-1].succeed({"manifest": manifest.to_dict()})
    title = host.confirmations["publish-staged:pub:1"][0]
    assert title == "Publish staged changes to the host?"


def test_conflicts_are_listed_and_resolved_one_file_at_a_time(host, ready_staging) -> None:
    ready_staging._on_inspected(
        {
            "data": {
                "unpublished_changes": True,
                "staging_resumed": True,
                "conflicts": [{"path": "a.py", "base_sha256": "0"}, {"path": "b.py"}],
            }
        }
    )
    assert ready_staging.conflicts == [{"path": "a.py"}, {"path": "b.py"}]
    review = host.infoRequested.calls[-1][1]
    assert "resumed from an earlier session" in review and "a.py, b.py" in review

    ready_staging.resolve_conflict("a.py", "take_host")
    rebase = FakeStagingWorker.started[-1]
    assert rebase.action == "rebase" and rebase.resolutions == {"take_host": ["a.py"]}
    rebase.succeed({"absorbed": [], "resolved": ["a.py"], "open_conflicts": [{"path": "b.py"}]})
    assert ready_staging.conflicts == [{"path": "b.py"}] and host.conflictsChanged.calls
    assert host.statuses[-1] == (
        "Rebased staging: 0 host change(s) absorbed · 1 conflict(s) resolved · 1 conflict(s) open"
    )

    ready_staging.resolve_conflict("b.py", "merge")
    assert host.errorRequested.calls[-1][0] == "Invalid conflict resolution"


def test_staging_actions_wait_for_an_active_run(host, ready_staging) -> None:
    host.generation.running = True
    ready_staging.rebase()
    assert FakeStagingWorker.started == []
    assert host.statuses[-1] == "Wait for the active run or staging request to finish…"


def test_steering_goes_to_the_active_agent_run_or_falls_back_to_the_queue(
    host, storage, tmp_path: Path
) -> None:
    generation = _generation(host, storage, tmp_path)
    sent: list[tuple[str, str]] = []
    client = SimpleNamespace(
        cancel=lambda: None, steer=lambda run, text: sent.append((run, text))
    )
    generation.worker = AgentWorker(client, lambda _stop: iter(()))
    generation.controller.state.run_id = "run-1"

    generation.send_message("Use the v2 API", steer=True)
    assert sent == [("run-1", "Use the v2 API")] and generation.controller.pending_count == 0

    def refuse(_run: str, _text: str) -> None:
        raise GatewayError("Run is not active.")

    client.steer = refuse
    generation.send_message("too late", steer=True)
    assert generation.controller.pending_count == 1  # queued for the next turn instead
    generation.send_message("plain follow-up")
    assert generation.controller.pending_count == 2
    generation.worker = None


def test_run_meter_updates_from_agent_events_and_resets_per_run(
    host, storage, tmp_path: Path
) -> None:
    generation = _generation(host, storage, tmp_path)
    generation.on_agent_event(
        EventEnvelope(
            1,
            "run-1",
            "usage.updated",
            "2026-01-01T00:00:00Z",
            {"budget": {"tool_calls": 4, "elapsed_seconds": 61}, "cost": 0.01},
        )
    )
    assert generation.meter.tool_calls == 4 and host.meterChanged.calls
    assert generation.meter.to_dict()["text"].startswith("$0.0100")
    generation._attach(AgentWorker(SimpleNamespace(cancel=lambda: None), lambda _s: iter(())))
    assert generation.meter.tool_calls == 0
    generation.worker = None


def test_gateway_notices_are_shown_once_and_acknowledged(
    host, storage, tmp_path: Path, monkeypatch
) -> None:
    acknowledged: list[str] = []

    class Client:
        def __init__(self, _connection) -> None:
            pass

        def acknowledge_notice(self, notice_id: str) -> None:
            acknowledged.append(notice_id)

    monkeypatch.setattr(runtime_module, "GatewayClient", Client)
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True)
    runtime = _runtime(host, storage)
    runtime.gateway_token = "t" * 43
    status = _status(
        workspace, notices=({"notice_id": "rules-1", "message": "Broad Bash rules disabled."},)
    )
    runtime.apply_health(HealthResult(HealthState.READY, "ok", status))
    runtime.apply_health(HealthResult(HealthState.READY, "ok", status))
    assert host.infoRequested.calls == [("Notice", "Broad Bash rules disabled.")]
    assert acknowledged == ["rules-1"]


def test_runner_settings_are_validated_and_saved(host, storage, tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    host.runtime.workspace_path = str(tmp_path)
    settings = SettingsService(
        host, storage, catalog=SimpleNamespace(is_stale=lambda: False, models=lambda: [])
    )
    view = settings.runtime_settings_view()
    assert view["runnerTarget"] == "auto" and view["detectedTarget"] == "node"
    assert (view["pids"], view["gitHistoryDepth"], view["retentionDays"]) == (1024, 200, 30)

    assert settings.save_runtime_settings(
        {
            "runnerTarget": "polyglot",
            "cpus": 4,
            "memoryGb": 8.0,
            "pids": "2048",
            "gitHistoryDepth": "0",
            "retentionDays": "14",
        }
    )
    saved = settings.runtime_settings()
    assert (saved.runner_target, saved.cpus, saved.memory_gb, saved.pids) == (
        "polyglot",
        4.0,
        8,
        2048,
    )
    assert saved.git_history_depth == 0 and settings.retention_days() == 14

    for bad in (
        {"cpus": 16},
        {"memoryGb": 1},
        {"pids": 128},
        {"pids": "1.5"},
        {"runnerTarget": "ruby"},
        {"retentionDays": 0},
    ):
        assert not settings.save_runtime_settings(bad), bad
        assert host.errorRequested.calls[-1][0] == "Invalid runner settings"
    assert settings.runtime_settings().runner_target == "polyglot"  # unchanged

    storage.set_setting("runtime_settings", '{"cpus": 99}')
    assert settings.runtime_settings().cpus is None  # unreadable values fall back to defaults


def test_staging_worker_publish_and_rebase_actions_call_the_gateway() -> None:
    calls: list[tuple[str, object]] = []
    manifest = _manifest()
    client = SimpleNamespace(
        staged_publication=lambda workspace, paths: calls.append(("publish", workspace))
        or manifest,
        rebase_staging=lambda workspace, **choices: calls.append(("rebase", choices))
        or {"ok": 1},
        inspect_staging=lambda workspace: calls.append(("inspect", workspace)) or {"data": {}},
        discard_staging=lambda workspace: calls.append(("discard", workspace)) or {},
    )
    results: list[dict] = []
    for action, resolutions in (
        ("publish", None),
        ("rebase", {"keep_staged": ["a.py"]}),
        ("inspect", None),
    ):
        worker = StagingWorker(client, "ws", action, resolutions)
        worker.complete.connect(results.append)
        worker.run()
    assert calls == [
        ("publish", "ws"),
        ("rebase", {"keep_staged": ["a.py"], "take_host": ()}),
        ("inspect", "ws"),
    ]
    assert results[0] == {"manifest": manifest.to_dict()}


def _text_operation(path: str, text: str = "x\n") -> dict[str, Any]:
    operation = PublishOperation(
        path=path,
        operation="create",
        base_sha256=None,
        staged_sha256=hashlib.sha256(text.encode()).hexdigest(),
        content=text,
    )
    return asdict(operation)


def test_auto_stops_for_confirmation_at_a_flagged_batch(host, staging) -> None:
    """Plan 4.3 / D5: Auto never publishes a host-executable, dependency, or policy change
    without explicit confirmation; it falls back to the reviewed prompt instead."""
    service, _stopped = staging
    host.generation.auto_mode = True
    flagged = _manifest(operations=[_text_operation(".github/workflows/ci.yml")])
    service.start_publication(flagged, auto=True, client=object())
    assert FakePublicationWorker.started == []
    title, body, _accept, _decline = host.confirmations["publish-flagged:pub:1"]
    assert title == "Publish staged changes to the host?"
    assert "Auto stopped here" in body and "runs on the host" in body
    host.accept("publish-flagged:pub:1")
    assert len(FakePublicationWorker.started) == 1
    # Confirmed by the user: no auto-only check gate applies to the confirmed batch.
    assert FakePublicationWorker.started[0].options["required_checks"] == ()

    plain = _manifest(manifest_id="m2", operations=[_text_operation("src/app.py")])
    service.publication_worker = None
    service.start_publication(plain, auto=True, client=object())
    assert len(FakePublicationWorker.started) == 2


def test_required_checks_gate_only_auto_publication(host, staging) -> None:
    service, _stopped = staging
    policy = parse_policy(
        'version = 1\n[checks]\ntests = "pytest"\n'
        '[publication]\nverify_before_publish = ["tests"]\nnever_publish = ["secrets/**"]\n'
    )
    host.policy.effective = lambda: policy
    service.start_publication(_manifest(), auto=True, client=object())
    options = FakePublicationWorker.started[-1].options
    assert options["required_checks"] == ("tests",)
    assert options["never_publish"] == ("secrets/**",)
    service.publication_worker = None
    service.start_publication(_manifest(manifest_id="m2"), auto=False, client=object())
    assert FakePublicationWorker.started[-1].options["required_checks"] == ()


def test_review_selection_publishes_only_the_selected_paths(host, staging) -> None:
    """Plan 4.4: the review pane keeps the user's choices and publishes a partial selection."""
    service, _stopped = staging
    calls: list[tuple[str, tuple[str, ...] | None]] = []
    service._start_action = lambda action, *_args, paths=None, **_kwargs: calls.append(
        (action, paths)
    )
    review = {
        "summary": "2 changes",
        "changes": [
            {"path": "src/a.py", "operation": "update"},
            {"path": "package.json", "operation": "update"},
        ],
        "diffs": [
            {"path": "src/a.py", "kind": "text", "text": "@@ -1 +1 @@", "added_lines": 1}
        ],
        "checks": [],
    }
    service._on_reviewed(review)
    view = service.review_view()
    assert view["open"] and view["selectedCount"] == 2
    # Flagged files are listed first and say why.
    assert view["files"][0]["path"] == "package.json" and view["files"][0]["flags"]
    service.publish_selection()
    assert calls[-1] == ("publish", None)  # everything selected: a full publication

    service.set_review_selected("package.json", False)
    service.publish_selection()
    assert calls[-1] == ("publish", ("src/a.py",))

    # A refreshed review keeps the user's choices and selects only new files.
    changes = [*review["changes"], {"path": "src/b.py", "operation": "create"}]
    service._on_reviewed({**review, "changes": changes})
    assert service.review_selection == {"src/a.py", "src/b.py"}
    service.set_review_all(False)
    service.publish_selection()
    assert calls[-1] == ("publish", ("src/a.py",)) and host.statuses[-1].startswith("Select")
    service.close_review()
    assert service.review_view() == {"open": False}
