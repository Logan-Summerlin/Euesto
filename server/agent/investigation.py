"""Scoped, read-only repository investigation behind ``investigate_repository``.

Each call runs a bounded nested model loop restricted to the read-only investigation tools
(Plan's reads plus ``status``, so it can review staged changes), through the parent run's
executor session. It is offered in Plan and Agent mode. The loop has its own capped budget, debits every model call against
the parent's budget, and keeps its own evidence ledger: nested results never enter the
parent's context, so they neither consume nor are starved by the parent's result allowance.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any

from server.executor import ExecutorClient
from server.openrouter.agent import AgentTurn, agent_turn
from server.openrouter.errors import ProviderError
from shared.investigation import (
    REPORT_FORMAT_INSTRUCTIONS,
    InvestigationResult,
    normalize_investigation_path,
    parse_inspected_paths,
    parse_investigation_report,
    reinspection_target,
)
from shared.requests import DEFAULT_INVESTIGATION_MODEL, AgentRunRequest
from shared.tools import INVESTIGATION_TOOLS, ToolRequest, ToolResult

from .budgets import BudgetExceededError, RunBudget
from .context import compact_agent_context
from .redaction import Redactor
from .tool_calls import (
    Message,
    bounded_excerpt,
    bounded_tool_result,
    parse_arguments,
    parse_tool_call,
    tool_message,
)

INVESTIGATION_MAX_ITERATIONS = 36
INVESTIGATION_MAX_TOOL_CALLS = 36
INVESTIGATION_MIN_WALL_SECONDS = 10
INVESTIGATION_MAX_WALL_SECONDS = 300
# The nested loop compacts its own context and bounds each result, so a long investigation
# stays inside smaller investigation models' context windows.
INVESTIGATION_CONTEXT_TOKENS = 64_000
INVESTIGATION_MAX_OUTPUT_CHARS = 40_000
INVESTIGATION_RESULT_BYTES = 512_000
INVESTIGATION_MIN_EVIDENCE_BYTES = 16_000
INVESTIGATION_SYNTHESIS_RESERVE_SECONDS = 60
INVESTIGATION_MIN_TURN_SECONDS = 15
INVESTIGATION_MAX_TURN_SECONDS = 180
INVESTIGATION_PROVIDER_ATTEMPTS = 3
SYNTHESIS_REQUEST = (
    "Stop repository exploration now. Use the evidence already gathered and return the final "
    "concise investigation summary. Do not call any tools. "
)


class RepositoryInvestigation:
    """One ``investigate_repository`` call and the evidence it gathers."""

    def __init__(
        self,
        executor: ExecutorClient,
        append: Any,
        api_key: str,
        run_id: str,
        request: AgentRunRequest,
        request_id: str,
        parent_budget: RunBudget,
        *,
        redactor: Redactor | None = None,
    ):
        self.executor = executor
        self.append = append
        self.api_key = api_key
        self.run_id = run_id
        self.request = request
        self.parent_budget = parent_budget
        self.redactor = redactor or Redactor()
        self.link = {"parent_run_id": run_id, "parent_tool_call_id": request_id}
        self.model = str(request.investigation_model_id or DEFAULT_INVESTIGATION_MODEL)
        self.messages: list[Message] = []
        self.inspected: tuple[str, ...] = ()
        self.files: set[str] = set()
        self.observed: set[str] = set()
        self.skipped: list[str] = []
        self.evidence_bytes = 0
        self.pending_redactions: list[dict[str, Any]] = []

    async def run(self, raw_arguments: str) -> str:
        """Investigate and return the parent's tool-result content: a report or an error.

        Exploration stops while one iteration and enough wall time remain, so the loop always
        ends in a tool-free synthesis turn and the parent always receives a summary unless the
        provider fails before any evidence is gathered.
        """
        child: RunBudget | None = None
        try:
            query, self.inspected = _parse_investigation_arguments(raw_arguments)
            if self.parent_budget.remaining_cost * 0.5 < 0.01:
                raise RuntimeError("parent budget is too small for an investigation")
            child = _child_budget(self.parent_budget)
            self.messages = [
                {"role": "system", "content": _system_prompt(child)},
                {"role": "user", "content": _investigation_prompt(query, self.inspected)},
            ]
            await self.append(
                self.run_id,
                "subagent.started",
                {
                    **self.link,
                    "model": self.model,
                    "budget": {
                        "max_tool_calls": child.max_tool_calls,
                        "max_iterations": child.max_iterations,
                        "max_wall_seconds": child.max_wall_seconds,
                    },
                },
            )
            return await self._finish(await self._investigate(child), child.usage())
        except BudgetExceededError as exc:
            # The child's limits are checked before each step, so this is the parent's budget
            # running out.
            payload = InvestigationResult(
                "Investigation budget exhausted after partial repository analysis "
                f"({exc.used:g}/{exc.limit:g} {exc.unit}).",
                files_examined=tuple(sorted(self.files)),
                skipped_paths=tuple(self.skipped),
                truncated=True,
                extra={"budget_exhausted": True, "error": str(exc)},
            ).to_dict()
            return await self._finish(payload, child.usage() if child else {})
        except Exception as exc:
            message = str(exc)[:2000]
            await self.append(self.run_id, "subagent.failed", {**self.link, "message": message})
            return json.dumps(
                {
                    "error": message,
                    "fallback": "Continue with read, grep, find, and ls directly.",
                }
            )

    @property
    def _has_evidence(self) -> bool:
        return bool(self.files or self.observed or self.skipped)

    async def _investigate(self, child: RunBudget) -> dict[str, Any]:
        """Explore with Plan tools until done or out of room, then synthesize a report."""
        stop_reason: str | None = None
        provider_error: ProviderError | None = None
        last_turn_cost = 0.0
        while stop_reason is None:
            stop_reason = _stop_reason(child, last_turn_cost, self.evidence_bytes)
            if stop_reason is not None:
                break
            child.consume_iteration()
            self.messages = compact_agent_context(self.messages, INVESTIGATION_CONTEXT_TOKENS)
            cost_before = child.cost
            try:
                turn = await self._turn(child, set(INVESTIGATION_TOOLS))
            except ProviderError as exc:
                if not self._has_evidence:
                    raise
                # Evidence was gathered: still try to return it through a synthesis turn.
                provider_error, stop_reason = exc, "provider_error"
                break
            last_turn_cost = child.cost - cost_before
            self.messages.append(turn.message)
            if not turn.tool_calls:
                if turn.content.strip():
                    return self._payload(turn.content, truncated=False)
                # A reasoning-only or empty answer is not a summary; ask for one explicitly.
                stop_reason = "empty_answer"
                break
            for call in turn.tool_calls:
                if stop_reason is None:
                    stop_reason = _stop_reason(
                        child, 0.0, self.evidence_bytes, reserve_iteration=False
                    )
                if stop_reason is not None:
                    # Every tool call in the assistant message needs a result, or the provider
                    # rejects the synthesis request that follows.
                    self.messages.append(
                        tool_message(
                            parse_tool_call(call)[0],
                            json.dumps(
                                {
                                    "ok": False,
                                    "error_code": "investigation.budget_reserved",
                                    "output": "Not executed: the remaining investigation budget is reserved "
                                    "for the final summary.",
                                }
                            ),
                        )
                    )
                    continue
                child.consume_tool_call()
                await self._tool_call(call)
        return await self._synthesize(child, stop_reason, provider_error)

    async def _synthesize(
        self, child: RunBudget, stop_reason: str | None, provider_error: ProviderError | None
    ) -> dict[str, Any]:
        """Tool-free synthesis turn over the evidence gathered so far."""
        child.iterations += 1
        self.messages = compact_agent_context(self.messages, INVESTIGATION_CONTEXT_TOKENS)
        self.messages.append(
            {"role": "user", "content": SYNTHESIS_REQUEST + REPORT_FORMAT_INSTRUCTIONS}
        )
        content = ""
        try:
            turn = await self._turn(child, set(), synthesis=True)
            self.messages.append(turn.message)
            content = turn.content
            for call in turn.tool_calls:
                sub_id, sub_name, raw_arguments = parse_tool_call(call)
                await self._reject(
                    sub_id,
                    sub_name,
                    raw_arguments,
                    "investigation.tool_not_permitted",
                    f"Tool '{sub_name}' is not permitted during investigation synthesis. "
                    "No repository tools are available in this phase.",
                )
        except ProviderError as exc:
            if provider_error is None and not self._has_evidence:
                raise
            provider_error = provider_error or exc
        if not content.strip():
            content = _fallback_summary(stop_reason, self.files, self.observed, provider_error)
        extra: dict[str, Any] = {"stop_reason": stop_reason}
        if stop_reason == "budget":
            extra["budget_exhausted"] = True
        if provider_error is not None:
            extra["provider_error"] = str(provider_error)[:500]
        return self._payload(content, truncated=True, extra=extra)

    async def _turn(
        self, child: RunBudget, allowed_tools: set[str], *, synthesis: bool = False
    ) -> AgentTurn:
        """One nested model call, retrying transient provider failures while time allows."""
        reserve = 0.0 if synthesis else investigation_synthesis_reserve(child)
        # The synthesis turn is the one that produces the answer, so it always gets the full
        # reserve.
        floor = (
            INVESTIGATION_SYNTHESIS_RESERVE_SECONDS
            if synthesis
            else INVESTIGATION_MIN_TURN_SECONDS
        )
        delay = 1.0
        for attempt in range(1, INVESTIGATION_PROVIDER_ATTEMPTS + 1):
            available = child.remaining_wall_seconds - reserve
            try:
                turn = await agent_turn(
                    self.model,
                    self.messages,
                    self.api_key,
                    "plan",
                    self.request.provider_preferences,
                    allowed_tools=allowed_tools,
                    offered_tools=INVESTIGATION_TOOLS,
                    turn_timeout=min(INVESTIGATION_MAX_TURN_SECONDS, max(floor, available)),
                )
            except ProviderError as exc:
                if (
                    not exc.retryable
                    or attempt == INVESTIGATION_PROVIDER_ATTEMPTS
                    or available - delay < INVESTIGATION_MIN_TURN_SECONDS
                ):
                    raise
                await asyncio.sleep(delay)
                delay *= 3
                continue
            # The child's own limits are enforced before each step, never after a paid response.
            child.add_usage(turn.usage, check=False)
            self.parent_budget.add_usage(turn.usage)
            return turn
        raise AssertionError("unreachable")

    async def _tool_call(self, call: dict[str, Any]) -> None:
        """Run, refuse, or skip one of the investigation model's tool calls."""
        sub_id, sub_name, raw_arguments = parse_tool_call(call)
        if sub_name not in INVESTIGATION_TOOLS:
            await self._reject(
                sub_id,
                sub_name,
                raw_arguments,
                "investigation.tool_not_permitted",
                f"Tool '{sub_name}' is not permitted in repository investigation. "
                f"Available tools: {', '.join(sorted(INVESTIGATION_TOOLS))}.",
            )
            return
        try:
            args = parse_arguments(raw_arguments)
        except ValueError as exc:
            await self._reject(
                sub_id,
                sub_name,
                raw_arguments,
                "investigation.invalid_tool_arguments",
                f"Invalid arguments for tool '{sub_name}': {exc}",
            )
            return
        tool = ToolRequest(sub_id, self.run_id, sub_name, "plan", args)
        repeated = reinspection_target(sub_name, args, self.inspected)
        if repeated is not None:
            # The parent already has this content: refuse without touching the executor.
            if repeated not in self.skipped:
                self.skipped.append(repeated)
            await self.append(
                self.run_id,
                "subagent.tool_call",
                {**self.link, "request": tool.to_dict(), "skipped": True},
            )
            result = ToolResult(
                sub_id,
                False,
                output=f"'{repeated}' was already inspected by the parent agent; do not re-read "
                "it. Investigate other paths.",
                error_code="investigation.already_inspected",
                data={"path": repeated},
            )
            await self.append(
                self.run_id,
                "subagent.tool_result",
                {**self.link, "result": result.to_dict(), "skipped": True},
            )
        else:
            if args.get("path"):
                self.files.add(str(args["path"]))
            await self.append(
                self.run_id, "subagent.tool_call", {**self.link, "request": tool.to_dict()}
            )
            result = await self.executor.execute(tool)
            await self.append(
                self.run_id, "subagent.tool_result", {**self.link, "result": result.to_dict()}
            )
            self.observed.update(_observed_files(sub_name, args, result))
        self.messages.append(self._result_message(sub_id, sub_name, result))
        for redaction in self.pending_redactions:
            await self.append(self.run_id, "secret.redacted", {**self.link, **redaction})
        self.pending_redactions.clear()

    async def _reject(
        self, sub_id: str, sub_name: str, raw_arguments: str, error_code: str, error: str
    ) -> None:
        request = {
            "request_id": sub_id,
            "tool": sub_name,
            "mode": "plan",
            "arguments": bounded_excerpt(raw_arguments, 4_000),
        }
        await self.append(
            self.run_id,
            "subagent.tool_call",
            {**self.link, "request": request, "rejected": True},
        )
        result = ToolResult(
            sub_id,
            False,
            output=error,
            error_code=error_code,
            data={"allowed_tools": sorted(INVESTIGATION_TOOLS)},
        )
        await self.append(
            self.run_id,
            "subagent.tool_result",
            {**self.link, "result": result.to_dict(), "rejected": True},
        )
        self.messages.append(self._result_message(sub_id, sub_name, result))

    def _result_message(self, sub_id: str, name: str, result: ToolResult) -> Message:
        """A bounded result message, debited from this investigation's evidence ledger."""
        if len(result.output) > INVESTIGATION_MAX_OUTPUT_CHARS:
            result = replace(
                result,
                output=bounded_excerpt(result.output, INVESTIGATION_MAX_OUTPUT_CHARS),
                truncated=True,
            )
        text = bounded_tool_result(
            name, result, INVESTIGATION_RESULT_BYTES - self.evidence_bytes
        )
        text, kinds = self.redactor.redact(name, {"path": result.data.get("path")}, text)
        if kinds:
            self.pending_redactions.append({"request_id": sub_id, "tool": name, "kinds": kinds})
        self.evidence_bytes += len(text.encode())
        return tool_message(sub_id, text)

    def _payload(
        self, content: object, *, truncated: bool, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        summary, findings, structured = parse_investigation_report(
            str(content or ""), self.observed
        )
        return InvestigationResult(
            summary,
            findings,
            tuple(sorted(self.files)),
            tuple(self.skipped),
            structured,
            truncated,
            dict(extra or {}),
        ).to_dict()

    async def _finish(self, payload: dict[str, Any], usage: dict[str, Any]) -> str:
        await self.append(
            self.run_id, "subagent.completed", {**self.link, "usage": usage, **payload}
        )
        return json.dumps(payload)


def investigation_wall_seconds(parent_budget: RunBudget) -> int:
    """Bound a child investigation's wall time independently of the parent's remaining time."""
    remaining = int(parent_budget.remaining_wall_seconds)
    return min(INVESTIGATION_MAX_WALL_SECONDS, max(INVESTIGATION_MIN_WALL_SECONDS, remaining))


def investigation_synthesis_reserve(child: RunBudget) -> float:
    """Wall time kept back from exploration so the final synthesis turn can still run."""
    return min(float(INVESTIGATION_SYNTHESIS_RESERVE_SECONDS), child.max_wall_seconds / 3)


def _child_budget(parent: RunBudget) -> RunBudget:
    """An investigation's own limits: capped, and never more than half the parent's cost."""
    return RunBudget(
        min(parent.remaining_iterations, INVESTIGATION_MAX_ITERATIONS),
        investigation_wall_seconds(parent),
        parent.remaining_cost * 0.5,
        min(parent.remaining_tool_calls, INVESTIGATION_MAX_TOOL_CALLS),
        "investigation",
    )


def _parse_investigation_arguments(raw_arguments: str) -> tuple[str, tuple[str, ...]]:
    arguments = parse_arguments(raw_arguments)
    unknown = set(arguments) - {"query", "inspected_paths"}
    if unknown:
        raise ValueError(
            f"Unknown investigate_repository arguments: {', '.join(sorted(unknown))}"
        )
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    return query, parse_inspected_paths(arguments.get("inspected_paths"))


def _system_prompt(child: RunBudget) -> str:
    return (
        "You are the repository investigation subagent in a bounded plan-mode harness. "
        "You are strictly read-only: use only read, grep, find, ls, and status (staged "
        "changes). Never write, edit, "
        "execute commands, or publish. Your job is to investigate the user's question efficiently, "
        "not exhaustively. You have a limited investigation budget and must stop researching once "
        "you have enough evidence to answer the question. Return a concise factual synthesis as soon "
        "as the evidence is sufficient. Do not keep searching merely to increase completeness. "
        "The harness will reserve one final iteration and one final tool-call slot for synthesis; "
        "preserve the most relevant findings and file paths in context. "
        f"Current child budget: {child.max_tool_calls} tool calls, {child.max_iterations} "
        f"iterations, and {child.max_wall_seconds} seconds. "
        "Never intentionally spend the final available tool call on exploratory work; when one tool "
        "call or one iteration remains, stop using repository tools and return your best-supported "
        "summary. Large files are returned in bounded excerpts; use grep or read with "
        "start_line/end_line to target the relevant lines. " + REPORT_FORMAT_INSTRUCTIONS
    )


def _investigation_prompt(query: str, inspected: tuple[str, ...]) -> str:
    prompt = (
        "Investigate this repository question and return a concise factual summary. Query: "
        + query
    )
    if inspected:
        prompt += (
            "\nThe parent agent has already inspected these paths and has their contents; do not "
            "read or list them again (such calls are refused). Search elsewhere, and cite them in "
            "findings only from the parent's context: " + ", ".join(inspected)
        )
    return prompt


def _stop_reason(
    child: RunBudget,
    last_turn_cost: float,
    ledger_bytes: int,
    *,
    reserve_iteration: bool = True,
) -> str | None:
    """Why exploration must stop now to leave room for synthesis, or ``None`` to continue."""
    if (
        child.remaining_tool_calls <= 1
        or child.remaining_cost <= 0
        or (reserve_iteration and child.remaining_iterations <= 1)
    ):
        return "budget"
    turn_floor = INVESTIGATION_MIN_TURN_SECONDS if reserve_iteration else 0
    if child.remaining_wall_seconds <= investigation_synthesis_reserve(child) + turn_floor:
        return "budget"
    if reserve_iteration and child.remaining_cost <= max(0.0005, 2 * last_turn_cost):
        return "budget"
    if INVESTIGATION_RESULT_BYTES - ledger_bytes < INVESTIGATION_MIN_EVIDENCE_BYTES:
        return "evidence_limit"
    return None


def _fallback_summary(
    stop_reason: str | None, files: set[str], observed: set[str], error: Exception | None
) -> str:
    """Harness-written summary for when the investigation model returned no final text."""
    if stop_reason == "budget":
        lead = (
            "Investigation budget exhausted after partial repository analysis; the investigation "
            "model returned no final summary."
        )
    elif error is not None:
        lead = f"The investigation model failed before summarizing ({str(error)[:300]})."
    else:
        lead = "The investigation model returned no final summary."
    seen = sorted(observed) or sorted(files)
    tail = (
        f" Files it examined: {', '.join(seen[:40])}." if seen else " No files were examined."
    )
    return lead + tail + " Verify directly with read, grep, find, and ls."


def _observed_files(tool: str, arguments: dict[str, Any], result: ToolResult) -> set[str]:
    """Files whose content the investigator actually saw: successful reads and grep matches."""
    if not result.ok:
        return set()
    if tool == "read":
        path = normalize_investigation_path(arguments.get("path"))
        return {path} if path else set()
    if tool == "grep":
        seen = set()
        for line in result.output.splitlines():
            match = re.match(r"^(.+?):(\d+):", line)
            path = normalize_investigation_path(match.group(1)) if match else None
            if path:
                seen.add(path)
        return seen
    return set()
