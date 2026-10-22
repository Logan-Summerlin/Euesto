"""Gateway endpoints for saved rules (harness plan 4.2), the review pane and partial
publication (4.4), and declared checks tied to the staged fingerprint (3.4)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from executor.app import ExecutorService
from server.app import create_app
from server.config import GatewayConfig
from server.service import GatewayService
from shared.permissions import PermissionDecision, new_permission_rule
from shared.policy import parse_policy
from shared.tools import PublicationReceipt, ToolRequest
from tests.helpers import executor_config

TOKEN = "t" * 43


class LocalExecutor:
    """The executor service in-process, behind the gateway client's interface."""

    def __init__(self, service: ExecutorService) -> None:
        self.service = service

    async def status(self):
        return {"environment": {}, "workspace_status": self.service.workspace_status()}

    async def execute(self, request: ToolRequest):
        return await self.service.execute(request)

    async def manifest(
        self, run_id, approval_id, *, publication_id=None, batch_index=1, paths=None
    ):
        return self.service.manifest(
            run_id,
            approval_id,
            publication_id=publication_id,
            batch_index=batch_index,
            paths=paths,
        )

    async def review_staging(self, body=None):
        return self.service.review(body or {})

    async def run_check(self, name, command, timeout_seconds):
        return await self.service.run_check(name, command, timeout_seconds)

    async def mark_staging_published(self, receipt):
        snapshot = self.service.mark_published(receipt)
        return {"snapshot_id": snapshot.snapshot_id, "file_count": snapshot.file_count}


def setup(tmp_path: Path, files: dict[str, str]) -> tuple[GatewayService, ExecutorService]:
    source = tmp_path / "source"
    source.mkdir()
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    executor = ExecutorService(executor_config(source))
    gateway = GatewayService(GatewayConfig(TOKEN, tmp_path / "gateway.sqlite3"))
    gateway.executor = LocalExecutor(executor)  # type: ignore[assignment]
    gateway.config = replace(gateway.config, workspace_id=executor.config.workspace_id)
    return gateway, executor


def stage(executor: ExecutorService, path: str, content: str) -> None:
    request = ToolRequest("w", "run", "write", "agent", {"path": path, "content": content})
    assert asyncio.run(executor.execute(request)).ok


def request(gateway: GatewayService, method: str, path: str, body=None) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(gateway.config, gateway))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            return await client.request(method, path, json=body)

    return asyncio.run(call())


# -- 4.2 rules -------------------------------------------------------------------------------


def test_a_command_prefix_rule_means_exactly_that_prefix() -> None:
    rule = new_permission_rule("r", "w", {"tool": "bash", "command": "npm test"})
    assert (rule.executable, rule.argument_prefix) == ("npm", ("test",))
    assert rule.decision == PermissionDecision.ALLOW_RULE

    def bash(command: str) -> ToolRequest:
        return ToolRequest("q", "run", "bash", "agent", {"command": command})

    assert rule.matches(bash("npm test -- --watch=false"), "w")
    assert not rule.matches(bash("npm install"), "w")
    assert not rule.matches(bash("npx test"), "w")
    assert not rule.matches(bash("npm test"), "other-workspace")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"tool": "bash"}, "must name a command prefix"),
        ({"tool": "bash", "command": "pytest", "path_prefix": "src"}, "scoped by command"),
        ({"tool": "write", "command": "pytest"}, "Only bash rules"),
        ({"tool": "fetch_url"}, "tool must be one of"),
        ({"tool": "update_plan"}, "tool must be one of"),
        ({"tool": "write", "path_prefix": "../x"}, "relative workspace path"),
        ({"tool": "write", "decision": "allow_run"}, "decision must be one of"),
        ({"tool": "write", "mode": "chat"}, "mode must be"),
        ({"tool": "write", "colour": "red"}, "A rule takes"),
    ],
)
def test_rules_that_could_not_mean_what_they_say_are_refused(spec: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        new_permission_rule("r", "w", spec)


def test_the_rules_endpoint_adds_lists_disables_and_deletes(tmp_path: Path) -> None:
    gateway = GatewayService(GatewayConfig(TOKEN, tmp_path / "gateway.sqlite3"))
    created = request(
        gateway,
        "POST",
        "/v1/permissions",
        {"workspace_id": "w", "tool": "bash", "command": "pytest -q"},
    )
    assert created.status_code == 201
    rule = created.json()["rule"]
    assert rule["executable"] == "pytest" and rule["argument_prefix"] == ["-q"]
    denied = request(
        gateway,
        "POST",
        "/v1/permissions",
        {"workspace_id": "w", "tool": "write", "path_prefix": "secrets", "decision": "deny"},
    )
    assert denied.status_code == 201
    invalid = request(gateway, "POST", "/v1/permissions", {"workspace_id": "w", "tool": "bash"})
    assert invalid.status_code == 422
    listed = request(gateway, "GET", "/v1/permissions?workspace_id=w").json()["rules"]
    assert len(listed) == 2
    rule_id = rule["rule_id"]
    assert (
        request(
            gateway, "PUT", f"/v1/permissions/{rule_id}/enabled", {"enabled": False}
        ).status_code
        == 200
    )
    assert [
        item["enabled"]
        for item in request(gateway, "GET", "/v1/permissions?workspace_id=w").json()["rules"]
        if item["rule_id"] == rule_id
    ] == [False]
    assert request(gateway, "DELETE", f"/v1/permissions/{rule_id}").status_code == 204
    assert len(request(gateway, "GET", "/v1/permissions?workspace_id=w").json()["rules"]) == 1


# -- 4.4 review and partial publication ------------------------------------------------------


def test_the_review_endpoint_shows_diffs_and_a_subset_publishes_alone(tmp_path: Path) -> None:
    gateway, executor = setup(tmp_path, {"a.py": "a\n", "b.py": "b\n"})
    workspace = executor.config.workspace_id
    stage(executor, "a.py", "A\n")
    stage(executor, "b.py", "B\n")
    review = request(gateway, "POST", f"/v1/workspaces/{workspace}/staging/review", {})
    assert review.status_code == 200
    data = review.json()
    assert [item["path"] for item in data["changes"]] == ["a.py", "b.py"]
    assert {item["path"] for item in data["diffs"]} == {"a.py", "b.py"}
    assert data["checks"] == []

    manifest = request(
        gateway, "POST", f"/v1/workspaces/{workspace}/staging/publication", {"paths": ["b.py"]}
    ).json()
    assert [item["path"] for item in manifest["operations"]] == ["b.py"]
    from shared.tools import PublishManifest

    receipt = PublicationReceipt.from_manifest(PublishManifest.from_dict(manifest))
    (executor.config.source_root / "b.py").write_text("B\n", encoding="utf-8")
    marked = request(
        gateway, "POST", f"/v1/workspaces/{workspace}/staging/mark-published", receipt.to_dict()
    )
    assert marked.status_code == 200
    assert executor.workspace_status()["modified"] == ["a.py"]


# -- 3.4 declared checks ---------------------------------------------------------------------


@pytest.mark.posix
def test_check_results_are_tied_to_the_staged_fingerprint(tmp_path: Path) -> None:
    gateway, executor = setup(tmp_path, {"a.py": "a\n"})
    workspace = executor.config.workspace_id
    policy = parse_policy('version = 1\n[checks]\ntest = "grep -q a a.py"\n')
    gateway.journal.save_workspace_config(workspace, {"policy": policy.to_config()}, "now")

    def checks() -> list[dict]:
        return request(
            gateway, "POST", f"/v1/workspaces/{workspace}/staging/review", {}
        ).json()["checks"]

    assert [(item["name"], item["status"]) for item in checks()] == [("test", "not_run")]
    ran = request(gateway, "POST", f"/v1/workspaces/{workspace}/checks/test/run", {})
    assert ran.status_code == 200 and ran.json()["outcome"] == "passed"
    assert [(item["status"], item["current"]) for item in checks()] == [("passed", True)]

    # A later mutation invalidates the result tied to the earlier snapshot.
    stage(executor, "a.py", "b\n")
    assert [(item["status"], item["current"]) for item in checks()] == [("stale", False)]
    request(gateway, "POST", f"/v1/workspaces/{workspace}/checks/test/run", {})
    assert [(item["status"], item["current"]) for item in checks()] == [("failed", True)]

    unknown = request(gateway, "POST", f"/v1/workspaces/{workspace}/checks/deploy/run", {})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "check.unknown"


def test_a_changed_check_command_invalidates_its_result(tmp_path: Path) -> None:
    gateway = GatewayService(GatewayConfig(TOKEN, tmp_path / "gateway.sqlite3"))
    policy = parse_policy('version = 1\n[checks]\ntest = "pytest"\n')
    gateway.journal.save_workspace_config("w", {"policy": policy.to_config()}, "now")
    gateway.journal.save_check_result(
        "w", "test", {"outcome": "passed", "fingerprint": "f", "command": "pytest -x"}, "now"
    )
    assert gateway.check_results("w", "f")[0]["status"] == "stale"
