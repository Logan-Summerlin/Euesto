from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from executor.checkpoints import CheckpointError, create_checkpoint
from executor.config import ExecutorConfig
from server.agent.budgets import EXTENDED_CODING_PROFILE, LARGE_CODING_PROFILE, STANDARD_CODING_PROFILE, BudgetExceededError, RunBudget, requires_budget_approval
from server.agent.runtime import AgentRuntime
from shared.requests import AgentRunRequest
from shared.tools import ToolResult


def make_config(tmp_path: Path, **overrides: object) -> ExecutorConfig:
    values: dict[str, object] = {"source_root": tmp_path / "source", "work_root": tmp_path / "work", "socket_path": tmp_path / "executor.sock", "token": "x" * 32, "workspace_id": "test-workspace"}
    values.update(overrides)
    return ExecutorConfig(**values)  # type: ignore[arg-type]


def test_standard_coding_profile_is_bounded_and_above_legacy_tool_limit() -> None:
    assert STANDARD_CODING_PROFILE.max_tool_calls > 100
    assert STANDARD_CODING_PROFILE.max_wall_seconds > 900
    assert STANDARD_CODING_PROFILE.max_cost == 2.0
    assert STANDARD_CODING_PROFILE.max_iterations != STANDARD_CODING_PROFILE.max_tool_calls


def test_profile_above_two_x_requires_one_time_session_approval() -> None:
    assert not requires_budget_approval(STANDARD_CODING_PROFILE)
    assert not requires_budget_approval(EXTENDED_CODING_PROFILE)
    assert requires_budget_approval(LARGE_CODING_PROFILE)


def test_executor_resource_formula_leaves_real_headroom(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert config.required_capacity_bytes == 6_000_000_000
    assert config.required_capacity_bytes < config.work_capacity_bytes
    config.validate_storage_capacity(config.work_capacity_bytes)


def test_executor_rejects_a_combined_configuration_that_only_fits_individually(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fit strictly below"):
        make_config(tmp_path, max_staging_bytes=3_500_000_000, max_checkpoint_bytes=3_500_000_000, work_capacity_bytes=8_000_000_000)


def test_executor_rejects_actual_work_capacity_below_configured_capacity(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="actual /work capacity"):
        config.validate_storage_capacity(7_000_000_000)


def test_checkpoint_budget_is_checked_against_staging_and_temp_headroom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "file.txt").write_bytes(b"x" * 100)
    monkeypatch.setattr("executor.checkpoints.shutil.disk_usage", lambda _path: SimpleNamespace(total=1_000_000_200))
    with pytest.raises(CheckpointError, match="combined /work resource budget"):
        create_checkpoint(work, max_total_bytes=100)


def test_checkpoint_fails_when_current_staging_exceeds_checkpoint_budget(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "file.txt").write_bytes(b"x" * 10)
    with pytest.raises(CheckpointError, match="too large"):
        create_checkpoint(work, max_total_bytes=9)


def test_gateway_budget_exhausts_before_executor_operation() -> None:
    budget = RunBudget(10, 60, 10.0, max_tool_calls=1)
    budget.consume_tool_call()
    with pytest.raises(BudgetExceededError, match="tool-call budget exhausted"):
        budget.consume_tool_call()


def test_executor_staging_limit_can_exhaust_before_gateway_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.txt").write_bytes(b"x" * 20)
    config = make_config(tmp_path, source_root=source, max_staging_bytes=10, max_checkpoint_bytes=10)
    from executor.staging import seed_staging
    with pytest.raises(RuntimeError, match="staging limits"):
        seed_staging(config)


class _FakeExecutor:
    def __init__(self) -> None:
        self.request = None
    async def execute(self, request):
        self.request = request
        return ToolResult(request.request_id, True, "ok", {"workspace_status": {"staged": False}})
    async def status(self):
        return {"workspace_id": "workspace", "environment": {"workspace_root": ".", "limits": {}}}


class _FakeApprovals:
    async def wait(self, *_args, **_kwargs):
        from shared.permissions import PermissionDecision
        return PermissionDecision.ALLOW_RUN


async def _assert_bash_timeout_clamp() -> None:
    executor = _FakeExecutor()
    runtime = AgentRuntime(executor, _FakeApprovals(), lambda *_args: asyncio.sleep(0))
    runtime._tool_result_bytes["run"] = 0
    request = AgentRunRequest(model="test-model", messages=({"role": "user", "content": "run bash"},), mode="agent", workspace_id="workspace", approval_policy="auto")
    budget = RunBudget.from_profile("coding")
    budget.started = time.monotonic() - (budget.max_wall_seconds - 17)
    await runtime._execute_tool_call("run", request, {"id": "call", "function": {"name": "bash", "arguments": '{"command":"true","timeout_seconds":300}'}}, [], budget)
    assert executor.request is not None
    assert 1 <= executor.request.arguments["timeout_seconds"] <= 17


def test_bash_timeout_is_clamped_to_remaining_gateway_wall_time() -> None:
    asyncio.run(_assert_bash_timeout_clamp())
