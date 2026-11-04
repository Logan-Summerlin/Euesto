"""Tool-feedback behavior: coverage, append, truncation, status handoff, and batch dedupe."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from executor.errors import ExecutorToolError
from executor.tools.edit import edit
from executor.tools.read import read
from executor.tools.write import write
from server.agent import runtime as runtime_module
from server.agent.runtime import AgentRuntime, SeenFile, _merge_coverage
from server.openrouter.agent import AgentTurn, agent_payload
from shared.requests import AgentRunRequest
from shared.tools import ToolRequest, ToolResult


class AllowApprovals:
    async def wait(self, *args, **kwargs):
        from shared.permissions import PermissionDecision

        return PermissionDecision.ALLOW_RUN


class ScriptedExecutor:
    """A deterministic read/status executor with optional status failure."""

    def __init__(
        self, *, read_data: dict | None = None, status_ok: bool = True, read_bytes: int = 20
    ) -> None:
        self.read_data = read_data or {}
        self.status_ok = status_ok
        self.read_bytes = read_bytes
        self.requests: list[ToolRequest] = []

    async def status(self):
        return {"workspace_id": "workspace", "environment": {}}

    async def workspace_snapshot(self):
        return {}

    async def execute(self, request):
        self.requests.append(request)
        if request.tool == "read":
            data = {
                "path": request.arguments["path"],
                "sha256": "a" * 64,
                "byte_offset": 0,
                "truncated": False,
                **self.read_data,
            }
            data.setdefault("size_bytes", 100)
            data.setdefault("start_line", 1)
            data.setdefault("end_line", 10)
            data.setdefault("line_count", 10)
            return ToolResult(request.request_id, True, output="x" * self.read_bytes, data=data)
        if request.tool == "status":
            if not self.status_ok:
                return ToolResult(
                    request.request_id,
                    False,
                    output="status unavailable",
                    error_code="io.internal",
                )
            return ToolResult(
                request.request_id,
                True,
                output="status: 1 created, 0 modified, 0 deleted, 0 mode changes (1 total)",
                data={
                    "counts": {
                        "created": 1,
                        "modified": 0,
                        "deleted": 0,
                        "permission_changes": 0,
                    },
                    "total_known": 1,
                    "changes": [{"path": "new.py", "operation": "create"}],
                },
            )
        if request.tool in {"write", "edit"}:
            return ToolResult(
                request.request_id,
                True,
                output="ok",
                data={
                    "path": request.arguments["path"],
                    "mode": request.arguments.get("mode", "replace"),
                    "old_sha256": "a" * 64,
                    "new_sha256": "b" * 64,
                    "size_bytes": 10,
                    "appended_bytes": 0,
                    "workspace_status": {"staged": True, "created": [], "modified": ["a"]},
                },
            )
        return ToolResult(request.request_id, True, output="ok", data={})

    async def cancel(self, request_id):
        return None

    async def manifest(self, run_id, approval_id):
        return SimpleNamespace(operations=())


class RealToolsExecutor(ScriptedExecutor):
    """Delegates read/write/edit to the real executor tools on a temporary staged root."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    async def execute(self, request):
        self.requests.append(request)
        try:
            if request.tool == "read":
                output, data = read(self.root, request.arguments, max_bytes=8_000_000)
            elif request.tool == "write":
                output, data = write(
                    self.root,
                    request.arguments,
                    max_bytes=1_000_000,
                    max_checkpoint_bytes=100_000_000,
                )
            elif request.tool == "edit":
                output, data = edit(
                    self.root,
                    request.arguments,
                    max_target_bytes=2_000_000,
                    max_result_bytes=2_000_000,
                    max_checkpoint_bytes=100_000_000,
                )
            elif request.tool == "status":
                output, data = (
                    "status: 0 created, 0 modified, 0 deleted, 0 mode changes (0 total)",
                    {
                        "counts": {
                            "created": 0,
                            "modified": 0,
                            "deleted": 0,
                            "permission_changes": 0,
                        },
                        "total_known": 0,
                        "changes": [],
                    },
                )
            else:
                output, data = "ok", {}
        except ExecutorToolError as exc:
            return ToolResult(
                request.request_id,
                False,
                output=exc.message,
                error_code=exc.code,
                data=dict(exc.details or {}),
            )
        return ToolResult(request.request_id, True, output=output, data=data)


class Provider:
    def __init__(self, turns) -> None:
        self.turns = list(turns)
        self.payloads: list[dict] = []

    async def __call__(
        self,
        model,
        messages,
        api_key,
        mode,
        provider_preferences=None,
        allowed_tools=None,
        **kwargs,
    ):
        for name in ("idle_timeout", "turn_timeout", "on_delta"):
            kwargs.pop(name, None)
        self.payloads.append(
            agent_payload(model, messages, mode, provider_preferences, allowed_tools, **kwargs)
        )
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def raw_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def tools_turn(*calls, content: str = "", finish_reason: str | None = None) -> AgentTurn:
    return AgentTurn(
        content,
        tuple(calls),
        {"role": "assistant", "content": content, "tool_calls": list(calls)},
        {"prompt_tokens": 100, "total_tokens": 110},
        finish_reason,
        "Provider-A",
    )


def final_turn(text: str = "done") -> AgentTurn:
    return AgentTurn(
        text,
        (),
        {"role": "assistant", "content": text},
        {"prompt_tokens": 100, "total_tokens": 110},
        "stop",
        "Provider-A",
    )


def make_request(**overrides) -> AgentRunRequest:
    values = {
        "model": "openai/test-model",
        "messages": ({"role": "user", "content": "do the task"},),
        "mode": "agent",
        "workspace_id": "workspace",
        "approval_policy": "auto",
        "session_id": "session-1",
        "context_limit_tokens": 400_000,
    }
    values.update(overrides)
    return AgentRunRequest(**values)


def run(monkeypatch, turns, *, executor, request=None):
    provider = Provider(turns)
    monkeypatch.setattr(runtime_module, "agent_turn", provider)
    events: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    runtime = AgentRuntime(executor, AllowApprovals(), append)
    asyncio.run(runtime.run("run-1", request or make_request(), "key"))
    return provider, events, runtime


def tool_contents(payload: dict) -> list[str]:
    return [item["content"] for item in payload["messages"] if item["role"] == "tool"]


def chunk_reads(root: Path, path: str, cap: int = 31_000) -> list[dict]:
    """The range calls that cover a file in contiguous model-facing chunks."""
    arguments: dict = {"path": path}
    reads: list[dict] = []
    while True:
        _text, data = read(root, arguments, max_bytes=cap)
        reads.append(dict(arguments))
        if not data["truncated"]:
            break
        arguments = {"path": path}
        if data.get("next_start_line"):
            arguments["start_line"] = data["next_start_line"]
        else:
            arguments["offset"] = data["next_offset"]
    return reads


def compile_chunks_root(tmp_path: Path) -> tuple[Path, list[dict]]:
    root = tmp_path / "work"
    root.mkdir()
    lines = [f"line {index:05d} " + "x" * 60 for index in range(500)]
    (root / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    reads = chunk_reads(root, "a.py")
    assert len(reads) >= 2
    return root, reads


def test_chunked_reads_confirm_a_shrink_write_with_a_warning(
    tmp_path: Path, monkeypatch
) -> None:
    root, reads = compile_chunks_root(tmp_path)
    executor = RealToolsExecutor(root)
    turns = [
        tools_turn(call(f"r{index}", "read", arguments))
        for index, arguments in enumerate(reads)
    ]
    turns.append(tools_turn(call("w1", "write", {"path": "a.py", "content": "tiny\n"})))
    turns.append(final_turn())
    provider, _events, _runtime = run(monkeypatch, turns, executor=executor)
    write_request = next(item for item in executor.requests if item.tool == "write")
    assert write_request.arguments["expected_sha256"]
    content = tool_contents(provider.payloads[-1])[-1]
    assert content.startswith("write a.py: rewritten")
    assert "warning: a.py shrank" in content
    assert "error staging.shrink_warning" not in content


def test_a_partial_read_still_rejects_the_shrink_and_names_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    root, reads = compile_chunks_root(tmp_path)
    executor = RealToolsExecutor(root)
    turns = [
        tools_turn(call("r1", "read", reads[0])),
        tools_turn(call("w1", "write", {"path": "a.py", "content": "tiny\n"})),
        final_turn(),
    ]
    provider, _events, _runtime = run(monkeypatch, turns, executor=executor)
    write_request = next(item for item in executor.requests if item.tool == "write")
    assert "expected_sha256" not in write_request.arguments
    content = tool_contents(provider.payloads[-1])[-1]
    assert content.startswith("error staging.shrink_warning:")
    assert "coverage: " in content and "bytes remain; read the rest, then retry" in content
    assert "expected_sha256" not in content


def test_a_changed_hash_starts_a_fresh_coverage_record(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    path = root / "a.py"
    path.write_text("first\n" * 10, encoding="utf-8")
    executor = RealToolsExecutor(root)

    async def change_on_second_turn(count: int) -> None:
        if count == 2:
            path.write_text("changed\n" + "filler\n" * 20_000, encoding="utf-8")

    turns = [
        tools_turn(call("r1", "read", {"path": "a.py"})),
        tools_turn(call("r2", "read", {"path": "a.py", "start_line": 1, "end_line": 1})),
        tools_turn(call("w1", "write", {"path": "a.py", "content": "tiny\n"})),
        final_turn(),
    ]
    provider = Provider(turns)
    monkeypatch.setattr(runtime_module, "agent_turn", provider)
    original = provider.__call__

    async def provider_with_change(*args, **kwargs):
        await change_on_second_turn(len(provider.payloads) + 1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "agent_turn", provider_with_change)
    events: list[tuple[str, dict]] = []

    async def append(_run_id, event_type, payload):
        events.append((event_type, payload))

    runtime = AgentRuntime(executor, AllowApprovals(), append)
    asyncio.run(runtime.run("run-1", make_request(), "key"))
    write_request = next(item for item in executor.requests if item.tool == "write")
    assert "expected_sha256" not in write_request.arguments
    assert provider.payloads


def test_an_edit_after_a_partial_read_resets_coverage(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.py").write_text("needle\n" + "filler\n" * 100, encoding="utf-8")
    executor = RealToolsExecutor(root)
    turns = [
        tools_turn(call("r1", "read", {"path": "a.py", "start_line": 1, "end_line": 1})),
        tools_turn(
            call("e1", "edit", {"path": "a.py", "old_str": "needle", "new_str": "changed"})
        ),
        tools_turn(call("w1", "write", {"path": "a.py", "content": "tiny\n"})),
        final_turn(),
    ]
    _provider, _events, _runtime = run(monkeypatch, turns, executor=executor)
    write_request = next(item for item in executor.requests if item.tool == "write")
    assert "expected_sha256" not in write_request.arguments


def test_the_re_read_note_names_coverage_and_the_continuation(monkeypatch) -> None:
    executor = ScriptedExecutor(
        read_data={
            "start_line": 1,
            "end_line": 2,
            "line_count": 10,
            "size_bytes": 100,
            "consumed_bytes": 10,
            "truncated": True,
            "next_start_line": 3,
        }
    )
    provider, _events, _runtime = run(
        monkeypatch,
        [
            tools_turn(call("r1", "read", {"path": "a"})),
            tools_turn(call("r2", "read", {"path": "a"})),
            final_turn(),
        ],
        executor=executor,
    )
    note = tool_contents(provider.payloads[-1])[-1]
    assert "unchanged since tool call r1" in note
    assert "covered lines 1–2 of 10 (10%), not the whole file" in note
    assert "Continue with start_line=3" in note


def test_the_re_read_note_says_full_coverage() -> None:
    runtime = AgentRuntime(ScriptedExecutor(), AllowApprovals(), lambda *args: None)
    result = ToolResult(
        "r1",
        True,
        output="body",
        data={"path": "a", "sha256": "a" * 64, "size_bytes": 10, "consumed_bytes": 10},
    )
    coverage = {
        "size_bytes": 10,
        "covered_bytes": 10,
        "complete": True,
        "ranges": 1,
        "cap_bytes": 31_000,
        "profile": "coding",
    }
    runtime._live["run"] = [{"role": "tool", "tool_call_id": "r1", "content": "first"}]
    first = runtime._unchanged_read("run", "r1", {"path": "a"}, result, "first", coverage)
    second = runtime._unchanged_read("run", "r2", {"path": "a"}, result, "second", coverage)
    assert first == "first"
    assert second == (
        "read a: unchanged since tool call r1; that result covered the whole file "
        "(full coverage)."
    )


def test_a_replace_write_confirms_the_whole_file_it_authored(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    executor = RealToolsExecutor(root)
    turns = [
        tools_turn(call("w1", "write", {"path": "a.py", "content": "first\n"})),
        tools_turn(call("w2", "write", {"path": "a.py", "content": "second\n"})),
        final_turn(),
    ]
    _provider, _events, _runtime = run(monkeypatch, turns, executor=executor)
    writes = [item for item in executor.requests if item.tool == "write"]
    assert len(writes) == 2
    assert "expected_sha256" not in writes[0].arguments
    assert writes[1].arguments["expected_sha256"]
    assert (root / "a.py").read_text(encoding="utf-8") == "second\n"


def test_append_after_a_partial_read_uses_the_known_hash(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.py").write_text("line one\n" + "filler\n" * 100, encoding="utf-8")
    executor = RealToolsExecutor(root)
    turns = [
        tools_turn(call("r1", "read", {"path": "a.py", "start_line": 1, "end_line": 1})),
        tools_turn(
            call("w1", "write", {"path": "a.py", "content": "tail\n", "mode": "append"})
        ),
        final_turn(),
    ]
    _provider, _events, _runtime = run(monkeypatch, turns, executor=executor)
    write_request = next(item for item in executor.requests if item.tool == "write")
    assert write_request.arguments["expected_sha256"]
    assert (root / "a.py").read_text(encoding="utf-8").endswith("tail\n")


def test_a_length_truncated_bash_is_not_executed(monkeypatch) -> None:
    executor = ScriptedExecutor()
    provider, events, _runtime = run(
        monkeypatch,
        [
            tools_turn(
                call("b1", "bash", {"command": "touch should-not-exist"}),
                finish_reason="length",
            ),
            final_turn(),
        ],
        executor=executor,
    )
    content = tool_contents(provider.payloads[-1])[-1]
    assert content.startswith("error tool.output_truncated:")
    assert not [item for item in executor.requests if item.tool == "bash"]
    assert "loop.output_truncated" in [kind for kind, _payload in events]


def test_the_re_read_note_is_absent_once_the_earlier_result_is_gone() -> None:
    runtime = AgentRuntime(ScriptedExecutor(), AllowApprovals(), lambda *args: None)
    result = ToolResult(
        "r1",
        True,
        output="body",
        data={"path": "a", "sha256": "a" * 64, "size_bytes": 100, "consumed_bytes": 10},
    )
    runtime._unchanged_read("run", "r1", {"path": "a"}, result, "first")
    runtime._live["run"] = []
    assert runtime._unchanged_read("run", "r2", {"path": "a"}, result, "second") == "second"


def test_a_length_truncated_write_is_refused_with_the_limit_and_size(monkeypatch) -> None:
    executor = ScriptedExecutor()
    raw = '{"path": "a", "content": "' + "x" * 50
    provider, events, _runtime = run(
        monkeypatch,
        [tools_turn(raw_call("w1", "write", raw), finish_reason="length"), final_turn()],
        executor=executor,
    )
    content = tool_contents(provider.payloads[-1])[-1]
    assert content.startswith("error tool.output_truncated:")
    assert "16,000 tokens" in content
    assert f"{len(raw.encode()):,} argument" in content
    assert not [item for item in executor.requests if item.tool == "write"]
    assert "loop.output_truncated" in [kind for kind, _payload in events]


def test_a_complete_looking_write_on_a_length_turn_is_not_executed(monkeypatch) -> None:
    executor = ScriptedExecutor()
    _provider, _events, _runtime = run(
        monkeypatch,
        [
            tools_turn(
                call("w1", "write", {"path": "a", "content": "complete"}),
                finish_reason="length",
            ),
            final_turn(),
        ],
        executor=executor,
    )
    assert not [item for item in executor.requests if item.tool == "write"]


def test_a_valid_read_on_a_length_turn_still_executes(monkeypatch) -> None:
    executor = ScriptedExecutor()
    run(
        monkeypatch,
        [
            tools_turn(call("r1", "read", {"path": "a"}), finish_reason="length"),
            final_turn(),
        ],
        executor=executor,
    )
    assert [item.tool for item in executor.requests if item.tool == "read"] == ["read"]


def test_a_truncated_read_error_names_the_limit_and_size(monkeypatch) -> None:
    executor = ScriptedExecutor()
    provider, _events, _runtime = run(
        monkeypatch,
        [
            tools_turn(raw_call("r1", "read", '{"path": "a"'), finish_reason="length"),
            final_turn(),
        ],
        executor=executor,
    )
    content = tool_contents(provider.payloads[-1])[-1]
    assert content.startswith("error tool.invalid_arguments:")
    assert "model output limit" in content and "argument bytes received" in content


def test_compaction_uses_a_fresh_status_in_the_handoff(monkeypatch) -> None:
    # A 32,000-token window compacts on the eighth check, one summary request below its
    # 16,000-token effective mark.
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    executor = ScriptedExecutor(read_bytes=6_000)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(8)]
    turns.insert(7, final_turn("Objective: finish.\nNext steps: read f6."))
    provider, events, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=executor,
        request=make_request(context_limit_tokens=32_000),
    )
    status_requests = [item for item in executor.requests if item.tool == "status"]
    assert len(status_requests) == 1
    summary = provider.payloads[7]["messages"][-1]["content"]
    assert "COMPACTION REQUEST" in summary
    assert (
        'Fresh staged status (application-generated; use this for "Files touched")' in summary
    )
    assert "1 created" in summary
    after = provider.payloads[8]["messages"]
    handoff = next(item for item in after if str(item.get("content", "")).startswith("HANDOFF"))
    assert "Fresh staged status (application-generated):" in handoff["content"]
    snapshots = [payload for kind, payload in events if kind == "context.status_snapshot"]
    assert snapshots and snapshots[0]["ok"] is True
    assert snapshots[0]["counts"]["created"] == 1
    assert snapshots[0]["bytes"] <= 4_000


def test_a_failed_status_call_does_not_fail_compaction(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "TURN_GROWTH_RESERVE_TOKENS", 0)
    executor = ScriptedExecutor(status_ok=False, read_bytes=6_000)
    turns = [tools_turn(call(f"r{index}", "read", {"path": f"f{index}"})) for index in range(8)]
    turns.insert(7, final_turn("Objective: finish."))
    provider, events, _runtime = run(
        monkeypatch,
        [*turns, final_turn()],
        executor=executor,
        request=make_request(context_limit_tokens=32_000),
    )
    assert "context.status_unavailable" in [kind for kind, _payload in events]
    compactions = [payload for kind, payload in events if kind == "context.compacted"]
    assert compactions and compactions[0]["method"] == "summary"
    assert (
        "status unavailable; run status after compaction"
        in provider.payloads[7]["messages"][-1]["content"]
    )


def test_two_identical_reads_in_one_batch_execute_once(monkeypatch) -> None:
    executor = ScriptedExecutor()
    provider, events, _runtime = run(
        monkeypatch,
        [
            tools_turn(call("r1", "read", {"path": "a"}), call("r2", "read", {"path": "a"})),
            final_turn(),
        ],
        executor=executor,
    )
    assert [item.tool for item in executor.requests if item.tool == "read"] == ["read"]
    contents = [
        item for item in tool_contents(provider.payloads[-1]) if item.startswith("read")
    ]
    assert contents[0].startswith("read a lines")
    assert contents[1] == "read a: duplicate of tool call r1 in this batch; use that result."
    duplicates = [payload for kind, payload in events if kind == "read.duplicate_batch"]
    assert duplicates == [{"path": "a", "request_id": "r2", "duplicate_of": "r1"}]


def test_coverage_ranges_merge_one_byte_line_boundary_gaps() -> None:
    merged = _merge_coverage([], 0, 100, 200)
    merged = _merge_coverage(merged, 101, 200, 200)
    assert merged == [(0, 200)]
    record = SeenFile("a" * 64, 200, merged, True)
    assert record.complete is True
