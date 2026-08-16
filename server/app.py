from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from shared.requests import AgentRunRequest, ChatRequest
from shared.responses import ErrorResponse
from shared.tools import PublishManifest

from .auth import GatewaySecurityMiddleware
from .config import GatewayConfig
from .logging_config import configure_logging
from .openrouter.errors import ProviderError
from .service import GatewayService, GatewayServiceError, utc_now


def create_app(config: GatewayConfig | None = None, service: GatewayService | None = None) -> Starlette:
    configure_logging()
    resolved_config = config or GatewayConfig.from_environment()
    resolved_service = service or GatewayService(resolved_config)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        await resolved_service.close()

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/v1/status", _status, methods=["GET"]),
        Route("/v1/session/openrouter-key", _session_key, methods=["PUT", "DELETE"]),
        Route("/v1/models", _models, methods=["GET"]),
        Route("/v1/models/refresh", _models_refresh, methods=["POST"]),
        Route("/v1/chat/stream", _chat_stream, methods=["POST"]),
        Route("/v1/runs", _create_run, methods=["POST"]),
        Route("/v1/runs/{run_id:str}", _run_status, methods=["GET"]),
        Route("/v1/runs/{run_id:str}/events", _run_events, methods=["GET"]),
        Route("/v1/runs/{run_id:str}/cancel", _cancel_run, methods=["POST"]),
        Route("/v1/runs/{run_id:str}/pause", _pause_run, methods=["POST"]),
        Route("/v1/runs/{run_id:str}/resume", _resume_run, methods=["POST"]),
        Route("/v1/runs/{run_id:str}/approvals/{approval_id:str}", _resolve_approval, methods=["POST"]),
        Route("/v1/permissions", _permission_rules, methods=["GET"]),
        Route("/v1/permissions/{rule_id:str}", _delete_permission_rule, methods=["DELETE"]),
        Route("/v1/permissions/{rule_id:str}/enabled", _set_permission_rule_enabled, methods=["PUT"]),
        Route("/v1/workspaces/{workspace_id:str}/config", _workspace_config, methods=["GET", "PUT"]),
        Route("/v1/workspaces/{workspace_id:str}/staging/inspect", _inspect_staging, methods=["GET"]),
        Route("/v1/workspaces/{workspace_id:str}/staging/discard", _discard_staging, methods=["POST"]),
        Route("/v1/workspaces/{workspace_id:str}/staging/mark-published", _mark_staging_published, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.gateway = resolved_service
    app.add_middleware(GatewaySecurityMiddleware, config=resolved_config)
    return app


async def _health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def _status(request: Request) -> Response:
    return JSONResponse(_service(request).status().to_dict())


async def _session_key(request: Request) -> Response:
    service = _service(request)
    if request.method == "DELETE":
        service.clear_client_key()
        return Response(status_code=204)
    try:
        data = await _json(request)
        service.configure_client_key(str(data.get("api_key") or ""))
    except (TypeError, ValueError) as exc:
        return _error("request.invalid_json", str(exc), status=422)
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse({"configured": True})


async def _models(request: Request) -> Response:
    return await _models_response(request, refresh=False)


async def _models_refresh(request: Request) -> Response:
    return await _models_response(request, refresh=True)


async def _models_response(request: Request, *, refresh: bool) -> Response:
    try:
        models, fetched_at = await _service(request).get_models(refresh=refresh)
    except ProviderError as exc:
        return _error(exc.code, str(exc), retryable=exc.retryable, status=503)
    return JSONResponse({"data": models, "fetched_at": fetched_at})


async def _chat_stream(request: Request) -> Response:
    try:
        chat = ChatRequest.from_dict(await _json(request))
        run_id = await _service(request).start_chat(chat)
    except (KeyError, TypeError, ValueError) as exc:
        return _error("request.invalid_chat", str(exc), status=422)
    except GatewayServiceError as exc:
        return _service_error(exc)
    return StreamingResponse(
        _sse(_service(request), run_id, 0),
        media_type="text/event-stream",
        headers={"X-Run-ID": run_id, "Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _create_run(request: Request) -> Response:
    try:
        agent = AgentRunRequest.from_dict(await _json(request))
        run_id = await _service(request).start_agent(agent)
    except (TypeError, ValueError) as exc:
        return _error("request.invalid_agent", str(exc), status=422)
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse({"run_id": run_id, "events_url": f"/v1/runs/{run_id}/events"}, status_code=202)


async def _run_status(request: Request) -> Response:
    run = _service(request).journal.get_run(request.path_params["run_id"])
    return JSONResponse(run) if run else _error("run.not_found", "Run not found.", status=404)


async def _run_events(request: Request) -> Response:
    run_id = request.path_params["run_id"]
    try:
        after_id = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        return _error("request.invalid_event_id", "Last-Event-ID must be an integer.", status=400)
    if _service(request).journal.get_run(run_id) is None:
        return _error("run.not_found", "Run not found.", status=404)
    return StreamingResponse(
        _sse(_service(request), run_id, max(0, after_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _cancel_run(request: Request) -> Response:
    found = await _service(request).cancel(request.path_params["run_id"])
    return JSONResponse({"cancel_requested": True}, status_code=202) if found else _error("run.not_found", "Run not found.", status=404)


async def _pause_run(request: Request) -> Response:
    found = _service(request).pause(request.path_params["run_id"])
    return JSONResponse({"pause_requested": True}, status_code=202) if found else _error(
        "run.not_pauseable", "Run is not an active Plan or Agent session.", status=409
    )


async def _resume_run(request: Request) -> Response:
    existing = _service(request).journal.events_after(request.path_params["run_id"])
    after_event_id = existing[-1].event_id if existing else 0
    try:
        resumed = await _service(request).resume_agent(request.path_params["run_id"])
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse(
        {"resumed": resumed, "after_event_id": after_event_id},
        status_code=202 if resumed else 200,
    )


async def _resolve_approval(request: Request) -> Response:
    try:
        data = await _json(request)
        found = _service(request).resolve_approval(
            request.path_params["run_id"], request.path_params["approval_id"], str(data.get("decision") or "")
        )
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse({"resolved": True}) if found else _error("approval.not_found", "Approval is no longer pending.", status=404)


async def _permission_rules(request: Request) -> Response:
    workspace = request.query_params.get("workspace_id", "")
    if not workspace:
        return _error("workspace.required", "workspace_id is required.", status=422)
    return JSONResponse({
        "rules": [
            rule.to_dict()
            for rule in _service(request).journal.permission_rules(
                workspace, include_disabled=True
            )
        ]
    })


async def _delete_permission_rule(request: Request) -> Response:
    deleted = _service(request).journal.delete_permission_rule(request.path_params["rule_id"])
    return Response(status_code=204) if deleted else _error("rule.not_found", "Permission rule not found.", status=404)


async def _set_permission_rule_enabled(request: Request) -> Response:
    try:
        data = await _json(request)
        enabled = data["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
    except (KeyError, TypeError, ValueError) as exc:
        return _error("request.invalid_rule", str(exc), status=422)
    updated = _service(request).journal.set_permission_rule_enabled(
        request.path_params["rule_id"], enabled
    )
    return JSONResponse({"enabled": enabled}) if updated else _error(
        "rule.not_found", "Permission rule not found.", status=404
    )


async def _workspace_config(request: Request) -> Response:
    workspace_id = request.path_params["workspace_id"]
    if request.method == "GET":
        return JSONResponse(
            {"config": _service(request).journal.load_workspace_config(workspace_id)}
        )
    try:
        data = await _json(request)
        allowed = {
            "instructions", "active_skills", "default_mode", "context_policy", "custom_tools"
        }
        if set(data) - allowed or len(repr(data).encode("utf-8")) > 128_000:
            raise ValueError("Workspace configuration contains unknown or oversized fields")
    except (TypeError, ValueError) as exc:
        return _error("request.invalid_workspace_config", str(exc), status=422)
    _service(request).journal.save_workspace_config(workspace_id, data, utc_now())
    return JSONResponse({"config": data})


async def _discard_staging(request: Request) -> Response:
    try:
        result = await _service(request).discard_staging(request.path_params["workspace_id"])
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse(result)


async def _mark_staging_published(request: Request) -> Response:
    workspace_id = request.path_params["workspace_id"]
    try:
        manifest = PublishManifest.from_dict(await _json(request))
        result = await _service(request).mark_staging_published(workspace_id, manifest)
    except (KeyError, TypeError, ValueError) as exc:
        return _error("request.invalid_staging_manifest", str(exc), status=422)
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse(result)


async def _inspect_staging(request: Request) -> Response:
    try:
        result = await _service(request).inspect_staging(request.path_params["workspace_id"])
    except GatewayServiceError as exc:
        return _service_error(exc)
    return JSONResponse(result)


async def _sse(service: GatewayService, run_id: str, after_id: int):
    async for event in service.events(run_id, after_id):
        if event is None:
            yield ": keep-alive\n\n"
            continue
        data = json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False)
        yield f"id: {event.event_id}\nevent: {event.type}\ndata: {data}\n\n"


async def _json(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be an object")
    return value


def _service(request: Request) -> GatewayService:
    return request.app.state.gateway


def _service_error(exc: GatewayServiceError) -> JSONResponse:
    return _error(exc.code, str(exc), retryable=exc.retryable, status=exc.status)


def _error(code: str, message: str, *, retryable: bool = False, status: int = 400) -> JSONResponse:
    return JSONResponse(ErrorResponse(code, message, retryable).to_dict(), status_code=status)
