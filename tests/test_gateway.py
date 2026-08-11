from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from server.app import create_app
from server.config import GatewayConfig
from server.journal import JournalStore
from server.logging_config import redact
from server.openrouter.catalog import GatewayCatalog
from server.openrouter.client import ProviderEvent, build_payload
from server.service import GatewayService
from shared.permissions import PermissionDecision, PermissionRule
from shared.requests import ChatRequest

TOKEN = "t" * 43


class FakeProvider:
    async def stream_chat(self, _request, _key, _cancel_event):
        yield ProviderEvent(text="Hel")
        yield ProviderEvent(text="lo")
        yield ProviderEvent(
            usage={"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.0001},
            model_id="actual/model",
            provider_id="Test Provider",
            finish_reason="stop",
            done=True,
        )


class SlowProvider:
    async def stream_chat(self, _request, _key, cancel_event):
        yield ProviderEvent(text="partial")
        while not cancel_event.is_set():
            await asyncio.sleep(0.01)


def config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(TOKEN, tmp_path / "gateway.sqlite3")


def auth_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **extra}


def test_gateway_auth_host_origin_and_health_boundaries(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = GatewayService(config(tmp_path), provider_factory=FakeProvider)
        app = create_app(config(tmp_path), service)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert (await client.get("/v1/status")).status_code == 401
            assert (await client.get("/v1/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
            assert (await client.get("/v1/status", headers=auth_headers(Origin="https://evil.example"))).status_code == 403
            public = await client.get(
                "/v1/status",
                headers={**auth_headers(), "Host": "192.168.1.20"},
            )
            assert public.status_code == 400
            status = await client.get("/v1/status", headers=auth_headers())
            assert status.status_code == 200
            assert status.json()["supported_modes"] == ["chat"]
            assert status.json()["executor_present"] is False
        await service.close()

    asyncio.run(scenario())


def test_chat_stream_journals_usage_and_replays_after_last_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        cfg = config(tmp_path)
        service = GatewayService(cfg, provider_factory=FakeProvider)
        service.configure_client_key("sk-or-v1-test-key")
        app = create_app(cfg, service)
        transport = httpx.ASGITransport(app=app)
        headers = auth_headers(**{"Content-Type": "application/json"})
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            configured = await client.put(
                "/v1/session/openrouter-key", headers=headers, json={"api_key": "sk-or-v1-test-key"}
            )
            assert configured.status_code == 200
            response = await client.post(
                "/v1/chat/stream",
                headers=headers,
                json={
                    "model": "vendor/model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "mode": "chat",
                },
            )
            assert response.status_code == 200
            run_id = response.headers["x-run-id"]
            assert '"type":"model.delta"' in response.text
            assert '"text":"Hel"' in response.text
            assert '"type":"run.completed"' in response.text
            replay = await client.get(
                f"/v1/runs/{run_id}/events",
                headers=auth_headers(**{"Last-Event-ID": "3"}),
            )
            replay_ids = [int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")]
            assert replay_ids and min(replay_ids) > 3
            assert '"actual_model":"actual/model"' in replay.text
            assert '"provider":"Test Provider"' in replay.text
            run = await client.get(f"/v1/runs/{run_id}", headers=auth_headers())
            assert run.json()["state"] == "completed"
            assert b"sk-or-v1-test-key" not in cfg.journal_path.read_bytes()
        await service.close()

    asyncio.run(scenario())


def test_cancellation_preserves_partial_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        cfg = config(tmp_path)
        service = GatewayService(cfg, provider_factory=SlowProvider)
        service.configure_client_key("sk-or-v1-test-key")
        request = ChatRequest.from_dict(
            {"model": "vendor/model", "messages": [{"role": "user", "content": "work"}]}
        )
        run_id = await service.start_chat(request)
        for _ in range(100):
            if any(event.type == "model.delta" for event in service.journal.events_after(run_id)):
                break
            await asyncio.sleep(0.01)
        assert await service.cancel(run_id)
        for _ in range(100):
            if service.journal.is_terminal(run_id):
                break
            await asyncio.sleep(0.01)
        events = service.journal.events_after(run_id)
        assert any(event.type == "model.delta" and event.payload["text"] == "partial" for event in events)
        assert events[-1].type == "run.cancelled"
        assert events[-1].payload["partial_output_preserved"] is True
        await service.close()

    asyncio.run(scenario())


def test_restart_marks_interrupted_run_failed(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    first = JournalStore(path)
    first.create_run("interrupted", "chat", "2026-01-01T00:00:00Z")
    first.append("interrupted", "run.created", "2026-01-01T00:00:00Z", {})
    first.close()
    service = GatewayService(GatewayConfig(TOKEN, path), provider_factory=FakeProvider)
    events = service.journal.events_after("interrupted")
    assert events[-1].type == "run.failed"
    assert events[-1].payload["code"] == "gateway.restarted"
    asyncio.run(service.close())


def test_journal_prunes_old_terminal_runs(tmp_path: Path) -> None:
    journal = JournalStore(tmp_path / "bounded.sqlite3", max_runs=2)
    for index in range(3):
        run_id = f"run-{index}"
        created = f"2026-01-01T00:00:0{index}Z"
        journal.create_run(run_id, "chat", created)
        journal.append(run_id, "run.completed", created, {})
    assert journal.get_run("run-0") is None
    assert journal.get_run("run-1") is not None
    assert journal.get_run("run-2") is not None
    journal.close()


def test_agent_endpoint_requires_configured_v04_executor(tmp_path: Path) -> None:
    async def scenario() -> None:
        cfg = config(tmp_path)
        service = GatewayService(cfg, provider_factory=FakeProvider)
        service.configure_client_key("sk-or-v1-test-key")
        app = create_app(cfg, service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/v1/runs",
                headers=auth_headers(**{"Content-Type": "application/json"}),
                json={
                    "mode": "agent", "model": "vendor/model", "workspace_id": "workspace",
                    "messages": [{"role": "user", "content": "work"}],
                },
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "workspace.invalid"
        await service.close()

    asyncio.run(scenario())


def test_gateway_payload_keeps_hosted_tools_explicit() -> None:
    request = ChatRequest.from_dict(
        {
            "model": "vendor/model",
            "messages": [{"role": "user", "content": "today"}],
            "server_tools": {"web_search": True, "web_fetch": False, "datetime": True},
        }
    )
    payload = build_payload(request)
    assert payload["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {"search_context_size": "low", "max_total_results": 12, "max_results": 4},
        },
        {"type": "openrouter:datetime"},
    ]
    assert json.dumps(payload)


def test_gateway_catalog_normalizes_and_reuses_durable_cache(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["sort"] == "intelligence-high-to-low"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/model",
                        "name": "Model",
                        "context_length": 123_000,
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    }
                ]
            },
        )

    async def scenario() -> None:
        journal = JournalStore(tmp_path / "catalog.sqlite3")
        catalog = GatewayCatalog(journal, transport=httpx.MockTransport(handler))
        models, fetched_at = await catalog.models(None)
        assert models[0]["id"] == "vendor/model"
        assert models[0]["artificial_analysis_rank"] == 1
        assert models[0]["prompt_price"] == 0.000001
        cached, cached_at = await catalog.models(None)
        assert cached == models
        assert cached_at == fetched_at
        journal.close()

    asyncio.run(scenario())
    assert calls == 1


def test_log_redaction_removes_authorization_and_openrouter_keys() -> None:
    secret = "sk-or-v1-this-must-not-appear"
    output = redact(f"Authorization: Bearer gateway-secret api={secret}")
    assert "gateway-secret" not in output
    assert secret not in output
    assert output.count("[REDACTED]") == 2


def test_v05_workspace_config_capabilities_and_permission_rule_management(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket = tmp_path / "executor.sock"
        socket.touch()
        cfg = GatewayConfig(
            TOKEN,
            tmp_path / "gateway.sqlite3",
            executor_socket=socket,
            executor_token="e" * 43,
            workspace_id="workspace",
        )
        service = GatewayService(cfg, provider_factory=FakeProvider)
        rule = PermissionRule(
            "rule", PermissionDecision.ALLOW_RULE, "workspace", "agent", "run_command",
            executable="pytest",
        )
        service.journal.save_permission_rule(rule)
        app = create_app(cfg, service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            saved = await client.put(
                "/v1/workspaces/workspace/config",
                headers=auth_headers(**{"Content-Type": "application/json"}),
                json={
                    "instructions": "Keep tests fast",
                    "custom_tools": [
                        {"name": "lint.report", "description": "Declared only", "modes": ["agent"]}
                    ],
                },
            )
            assert saved.status_code == 200
            status = await client.get("/v1/status", headers=auth_headers())
            capabilities = {item["name"]: item for item in status.json()["capabilities"]}
            assert capabilities["lint.report"]["executable"] is False

            rules = await client.get(
                "/v1/permissions", headers=auth_headers(), params={"workspace_id": "workspace"}
            )
            assert rules.json()["rules"][0]["enabled"] is True
            disabled = await client.put(
                "/v1/permissions/rule/enabled",
                headers=auth_headers(**{"Content-Type": "application/json"}),
                json={"enabled": False},
            )
            assert disabled.status_code == 200
            rules = await client.get(
                "/v1/permissions", headers=auth_headers(), params={"workspace_id": "workspace"}
            )
            assert rules.json()["rules"][0]["enabled"] is False
        await service.close()

    asyncio.run(scenario())
