"""Scoped, read-only repository investigation behind ``investigate_repository``.

Each call runs a bounded nested model loop restricted to the read-only investigation tools
(Plan's reads plus ``status``, so it can review staged changes), through the parent run's
executor session. It is offered in Plan and Agent mode. The loop has its own capped budget,
debits every model call against the parent's budget, and keeps its own evidence ledger: nested
results never enter the parent's context, so they neither consume nor are starved by the
parent's result allowance.
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
    INVESTIGATION_EXECUTOR_UNAVAILABLE,
    INVESTIGATION_FAILED,
    REPORT_FORMAT_INSTRUCTIONS,
    InvestigationResult,
    normalize_investigation_path,
    parse_inspected_paths,
    parse_investigation_report,
    reinspection_target,
    stop_reason_for,
)
from shared.requests import DEFAULT_INVESTIGATION_MODEL, AgentRunRequest
from shared.tools import INVESTIGATION_TOOLS, ToolRequest, ToolResult

from .budgets import BudgetExceededError, RunBudget
from .context import compact_agent_context, estimate_message_tokens
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
# Synthesis must send the whole evidence ledger at once, so it is bounded far higher than the
# exploration turns and never compacted down to exploration size first.
INVESTIGATION_SYNTHESIS_CONTEXT_TOKENS = 96_000
INVESTIGATION_MAX_OUTPUT_CHARS = 40_000
INVESTIGATION_RESULT_BYTES = 512_000
INVESTIGATION_MIN_EVIDENCE_BYTES = 16_000
# Base synthesis reserve, plus one second per 2,500 tokens of nested context the synthesis turn
# has to send: prompt processing dominates a long evidence ledger's latency.
INVESTIGATION_SYNTHESIS_RESERVE_SECONDS = 60
INVESTIGATION_SYNTHESIS_RESERVE_TOKENS = 2_500
# The child refuses to start below this much affordable parent budget.
INVESTIGATION_MIN_COST = 0.01
INVESTIGATION_MIN_TURN_SECONDS = 15
INVESTIGATION_MAX_TURN_SECONDS = 180
INVESTIGATION_PROVIDER_ATTEMPTS = 3
# Digest breadth: enough examined paths to orient the parent, bounded to stay small.
INVESTIGATION_DIGEST_PATHS = 24
# Tools whose result shows file content; every other investigation tool only lists paths.
_CONTENT_TOOLS = frozenset({"read", "grep"})
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
        # Paths seen through find/ls/status: evidence that a path exists, distinct from
        # ``observed``, which means the file's content was read or matched by grep.
        self.listed: set[str] = set()
        self.skipped: list[str] = []
        self.evidence_bytes = 0
        self.successful_calls = 0
        self.degraded = False
        self.executor_unavailable = False
        self.parent_budget_exceeded = False
        self.pending_redactions: list[dict[str, Any]] = []

    async def run(self, raw_arguments: str) -> str:
        """Investigate and return the parent's tool-result content: a report or a digest.

        Exploration stops while one iteration and enough wall time remain, so the loop always
        ends in a tool-free synthesis turn and the parent always receives a summary: even a
        provider failure now returns a digest built from the evidence gathered so far.
        """
        child: RunBudget | None = None
        try:
            query, self.inspected = _parse_investigation_arguments(raw_arguments)
            if self.parent_budget.remaining_cost * 0.5 < INVESTIGATION_MIN_COST:
                # Refuse in the same shape as a failed investigation, so the parent can branch
                # on ``error_code`` and still has a summary instead of an exception.
                return await self._refuse(
                    INVESTIGATION_FAILED,
                    "The remaining parent budget ("
                    f"{self.parent_budget.remaining_cost:.4f}) is too small for an investigation.",
                    summary="No investigation ran: the remaining run budget is too small to fund "
                    "a nested model loop. Continue with read, grep, find, and ls directly.",
                )
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
            # A child limit crossed between steps: no synthesis turn is affordable, so the
            # digest is the answer.
            cause = _cause_from_budget(exc.budget)
            payload = InvestigationResult.failed(
                INVESTIGATION_FAILED,
                "Investigation budget exhausted after partial repository analysis "
                f"({exc.used:g}/{exc.limit:g} {exc.unit}).",
                summary=_digest(cause, self.files, self.observed, self.listed, None),
                files_examined=tuple(sorted(self.files)),
                skipped_paths=tuple(self.skipped),
                stop_cause=cause,
                # A budget stop before synthesis is a partial analysis, never a complete one.
                truncated=True,
                extra={"budget_exhausted": True},
            ).to_dict()
            return await self._finish(payload, child.usage() if child else {})
        except Exception as exc:
            await self.append(
                self.run_id, "subagent.failed", {**self.link, "message": str(exc)[:2000]}
            )
            payload = InvestigationResult.failed(
                INVESTIGATION_FAILED,
                str(exc),
                summary=_digest("provider_error", self.files, self.observed, self.listed, exc),
                files_examined=tuple(sorted(self.files)),
                skipped_paths=tuple(self.skipped),
                stop_cause="provider_error",
            ).to_dict()
            return json.dumps(payload)

    async def _refuse(self, error_code: str, error: str, *, summary: str) -> str:
        """A start-up refusal in the same shape as a failed investigation."""
        payload = InvestigationResult.failed(
            error_code, error, summary=summary, skipped_paths=tuple(self.skipped)
        ).to_dict()
        # Unlike a degraded report, a refusal produced no evidence at all: emit it as a failure.
        await self.append(
            self.run_id,
            "subagent.failed",
            {**self.link, "message": error[:2000], "error_code": error_code},
        )
        return json.dumps(payload)

    @property
    def _has_evidence(self) -> bool:
        """Whether any nested call returned usable evidence: a listing counts as evidence."""
        return bool(self.successful_calls or self.files or self.observed or self.listed)

    async def _investigate(self, child: RunBudget) -> dict[str, Any]:
        """Explore with Plan tools until done or out of room, then synthesize a report."""
        stop_cause: str | None = None
        provider_error: ProviderError | None = None
        last_turn_cost = 0.0
        while stop_cause is None:
            stop_cause = _stop_cause(
                child,
                last_turn_cost,
                self.evidence_bytes,
                estimate_message_tokens(self.messages),
            )
            if stop_cause is not None:
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
                provider_error, stop_cause = exc, "provider_error"
                break
            last_turn_cost = child.cost - cost_before
            self.messages.append(turn.message)
            if not turn.tool_calls:
                if turn.content.strip():
                    # A natural completion still carries the status fields, so the payload shape
                    # does not depend on how the investigation ended.
                    return self._payload(turn.content, truncated=False, extra=self._extra(None))
                # A reasoning-only or empty answer is not a summary; ask for one explicitly.
                stop_cause = "empty_answer"
                break
            for call in turn.tool_calls:
                if stop_cause is None:
                    stop_cause = _stop_cause(
                        child,
                        0.0,
                        self.evidence_bytes,
                        estimate_message_tokens(self.messages),
                        reserve_iteration=False,
                    )
                if stop_cause is not None:
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
        return await self._synthesize(child, stop_cause, provider_error)

    async def _synthesize(
        self, child: RunBudget, stop_cause: str | None, provider_error: ProviderError | None
    ) -> dict[str, Any]:
        """Tool-free synthesis turn over the evidence gathered so far."""
        child.iterations += 1
        # Do not throw the newest evidence away before the turn that must summarize it: bound
        # the nested context at the higher synthesis ceiling instead of compacting it back down
        # to exploration size.
        self.messages = compact_agent_context(
            self.messages, INVESTIGATION_SYNTHESIS_CONTEXT_TOKENS
        )
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
            stop_cause = stop_cause or "provider_error"
        degraded = False
        if not content.strip():
            content = _digest(
                stop_cause, self.files, self.observed, self.listed, provider_error
            )
            degraded = True
        extra = self._extra(stop_cause, degraded=degraded)
        if provider_error is not None:
            extra["provider_error"] = str(provider_error)[:500]
        return self._payload(content, truncated=True, extra=extra)

    def _extra(self, stop_cause: str | None, *, degraded: bool = False) -> dict[str, Any]:
        """The always-present status fields: specific cause, grouped reason, degradation.

        A payload degraded by a lost executor names that code, so the parent can tell "the
        answer is thin because the executor went away" from "the investigation ran fine".
        """
        extra: dict[str, Any] = {
            "stop_reason": stop_reason_for(stop_cause),
            "stop_cause": stop_cause,
            "budget_exhausted": stop_reason_for(stop_cause) == "budget",
            "degraded": bool(degraded or self.degraded or self.parent_budget_exceeded),
            # A summary already paid for is returned even when charging it crossed the
            # parent's limit; the parent loop's own check stops the run at its next step.
            "parent_budget_exceeded": self.parent_budget_exceeded,
        }
        if self.executor_unavailable:
            extra["error_code"] = INVESTIGATION_EXECUTOR_UNAVAILABLE
        return extra

    async def _turn(
        self, child: RunBudget, allowed_tools: set[str], *, synthesis: bool = False
    ) -> AgentTurn:
        """One nested model call, retrying transient provider failures while time allows."""
        tokens = estimate_message_tokens(self.messages)
        reserve = 0.0 if synthesis else investigation_synthesis_reserve(child, tokens)
        # Exploration keeps its floor; synthesis may only spend wall time that is really left,
        # so the child cannot overrun its own cap by one floor.
        floor = 0.0 if synthesis else float(INVESTIGATION_MIN_TURN_SECONDS)
        delay = 1.0
        for attempt in range(1, INVESTIGATION_PROVIDER_ATTEMPTS + 1):
            available = child.remaining_wall_seconds - reserve
            timeout = min(INVESTIGATION_MAX_TURN_SECONDS, max(floor, available))
            if timeout <= 0:
                raise ProviderError(
                    "provider.timeout",
                    "Investigation wall budget is exhausted before the turn could start.",
                )
            try:
                turn = await agent_turn(
                    self.model,
                    self.messages,
                    self.api_key,
                    "plan",
                    self.request.provider_preferences,
                    allowed_tools=allowed_tools,
                    offered_tools=INVESTIGATION_TOOLS,
                    turn_timeout=timeout,
                )
            except ProviderError as exc:
                available = child.remaining_wall_seconds - reserve
                if (
                    not exc.retryable
                    or attempt == INVESTIGATION_PROVIDER_ATTEMPTS
                    or available - delay <= floor
                ):
                    raise
                await asyncio.sleep(delay)
                delay *= 3
                continue
            # The child's own limits are enforced before each step, never after a paid response.
            child.add_usage(turn.usage, check=False)
            self._charge_parent(turn.usage)
            return turn
        raise AssertionError("unreachable")

    def _charge_parent(self, usage: dict[str, Any]) -> None:
        """Charge the parent for a paid turn; a summary already paid for is never discarded."""
        try:
            self.parent_budget.add_usage(usage)
        except BudgetExceededError:
            # The run is over budget by one turn, not broken: the summary is returned and the
            # parent loop's own check stops its next step.
            self.parent_budget_exceeded = True

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
            try:
                result = await self.executor.execute(tool)
            except Exception as exc:
                # The executor can disappear mid-investigation. Answer the call so the loop
                # continues, the model can change approach, and synthesis still runs over the
                # evidence already gathered.
                self.degraded = True
                self.executor_unavailable = True
                error = f"Executor unavailable: {str(exc)[:300]}"
                await self.append(
                    self.run_id,
                    "subagent.executor_error",
                    {**self.link, "tool": sub_name, "message": error},
                )
                result = ToolResult(
                    sub_id,
                    False,
                    output=error
                    + " Continue with the evidence already gathered, or with another tool.",
                    error_code=INVESTIGATION_EXECUTOR_UNAVAILABLE,
                    data={"path": args.get("path")},
                )
            else:
                await self.append(
                    self.run_id,
                    "subagent.tool_result",
                    {**self.link, "result": result.to_dict()},
                )
            if result.ok:
                self.successful_calls += 1
            found = _observed_files(sub_name, args, result)
            if sub_name in _CONTENT_TOOLS:
                self.observed.update(found)
            else:
                self.listed.update(found)
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


def investigation_synthesis_reserve(child: RunBudget, tokens: int | None = None) -> float:
    """Wall time kept back from exploration so the final synthesis turn can still run.

    The reserve is derived from the nested context the synthesis turn has to send: prompt
    processing dominates a long evidence ledger's latency, so a 60 s base grows by one second
    per 2,500 tokens, and is capped by a third of the child's own wall budget.
    """
    estimated = max(0, int(tokens or 0))
    derived = (
        INVESTIGATION_SYNTHESIS_RESERVE_SECONDS
        + estimated / INVESTIGATION_SYNTHESIS_RESERVE_TOKENS
    )
    return min(float(derived), child.max_wall_seconds / 3)


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
        "You are strictly read-only: use only read, grep, find, ls, and status (staged changes). "
        "Never write, edit, execute commands, or publish. Your job is to investigate the user's "
        "question efficiently, not exhaustively. You have a limited investigation budget and must "
        "stop researching once you have enough evidence to answer the question. Return a concise "
        "factual synthesis as soon as the evidence is sufficient. Do not keep searching merely to "
        "increase completeness. The harness will reserve one final iteration and one final "
        "tool-call slot for synthesis; preserve the most relevant findings and file paths in "
        "context. "
        f"Current child budget: {child.max_tool_calls} tool calls, {child.max_iterations} "
        f"iterations, and {child.max_wall_seconds} seconds. "
        "Never intentionally spend the final available tool call on exploratory work; when one "
        "tool call or one iteration remains, stop using repository tools and return your "
        "best-supported summary. Large files are returned in bounded excerpts; use grep or read "
        "with start_line/end_line to target the relevant lines. " + REPORT_FORMAT_INSTRUCTIONS
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


def _stop_cause(
    child: RunBudget,
    last_turn_cost: float,
    ledger_bytes: int,
    context_tokens: int,
    *,
    reserve_iteration: bool = True,
) -> str | None:
    """The specific reason exploration must stop now, or ``None`` to continue.

    Each cause is precise so the digest and the parent's ``stop_cause`` say what actually
    happened; ``stop_reason`` groups the four budget-shaped causes as ``budget``.
    """
    if child.remaining_tool_calls <= 1:
        return "tool_calls"
    if reserve_iteration and child.remaining_iterations <= 1:
        return "iterations"
    if child.remaining_cost <= 0:
        return "cost"
    if reserve_iteration and child.remaining_cost <= max(0.0005, 2 * last_turn_cost):
        return "cost"
    turn_floor = INVESTIGATION_MIN_TURN_SECONDS if reserve_iteration else 0
    if child.remaining_wall_seconds <= (
        investigation_synthesis_reserve(child, context_tokens) + turn_floor
    ):
        return "wall_time"
    if INVESTIGATION_RESULT_BYTES - ledger_bytes < INVESTIGATION_MIN_EVIDENCE_BYTES:
        return "evidence_limit"
    return None


def _cause_from_budget(unit: str) -> str:
    """The exploration cause that matches the child limit a ``BudgetExceededError`` named."""
    return {
        "iteration": "iterations",
        "tool-call": "tool_calls",
        "wall-time": "wall_time",
        "cost": "cost",
    }.get(unit, "cost")


def _digest(
    stop_cause: str | None,
    files: set[str],
    observed: set[str],
    listed: set[str],
    error: Exception | None,
) -> str:
    """A deterministic digest of what was gathered when the model wrote no summary.

    It never claims a budget stop that did not happen: the lead sentence names the actual
    cause, the provider failure is quoted, and it ends in one concrete next step.
    """
    leads = {
        None: "The investigation model returned no final summary.",
        "tool_calls": "The investigation stopped at its tool-call limit before writing a summary.",
        "iterations": "The investigation stopped at its iteration limit before writing a summary.",
        "cost": "The investigation stopped at its cost limit before writing a summary.",
        "wall_time": "The investigation stopped at its wall-time limit before writing a summary.",
        "evidence_limit": (
            "The investigation stopped at its evidence-ledger limit before writing a summary."
        ),
        "empty_answer": "The investigation model returned an empty answer instead of a summary.",
        "provider_error": "The investigation model failed before it could write a summary.",
    }
    lines = [leads.get(stop_cause, leads[None])]
    if error is not None:
        lines.append(f"Provider error: {str(error)[:300]}")
    seen = sorted(observed)
    unread = sorted(listed - observed)
    if seen:
        shown = ", ".join(seen[:INVESTIGATION_DIGEST_PATHS])
        lines.append(f"File content it examined: {shown}.")
    if unread:
        shown = ", ".join(unread[:INVESTIGATION_DIGEST_PATHS])
        lines.append(f"Paths it listed but never read: {shown}.")
    if not seen and not unread:
        lines.append("It examined no repository paths.")
    lines.append(_next_step(seen, unread, files))
    return " ".join(lines)


def _next_step(observed: list[str], unread: list[str], files: set[str]) -> str:
    """One concrete next step for the parent: read the most specific unchecked path, or narrow."""
    target = _most_specific(unread) or _most_specific(observed)
    if target:
        return (
            f"Next step: read {target} (or narrow the query with grep) and continue directly."
        )
    fallback = _most_specific(sorted(files))
    if fallback:
        return f"Next step: read {fallback} and continue directly."
    return "Next step: continue with read, grep, find, and ls directly."


def _most_specific(paths: list[str]) -> str | None:
    """The deepest, longest path, preferring files over directories."""
    if not paths:
        return None
    pool = [path for path in paths if "." in path.rsplit("/", 1)[-1]] or paths
    return max(pool, key=lambda path: (path.count("/"), len(path)))


def _observed_files(tool: str, arguments: dict[str, Any], result: ToolResult) -> set[str]:
    """The paths a successful nested call saw: read targets, grep matches, and listings."""
    if not result.ok:
        return set()
    if tool == "read":
        # The executor returns the resolved target in ``data``; the argument is the fallback.
        path = normalize_investigation_path(result.data.get("path") or arguments.get("path"))
        return {path} if path else set()
    if tool == "grep":
        seen = set()
        for line in result.output.splitlines():
            match = re.match(r"^(.+?):(\d+):", line)
            path = normalize_investigation_path(match.group(1)) if match else None
            if path:
                seen.add(path)
        return seen
    if tool in {"find", "ls"}:
        seen = set()
        for line in result.output.splitlines():
            # Detail lines are "file\t<size>\t<path>"; plain lines are already the path.
            display = line.rsplit("\t", 1)[-1].strip()
            path = normalize_investigation_path(display)
            if path:
                seen.add(path)
        return seen
    if tool == "status":
        seen = set()
        for entry in result.data.get("changes") or ():
            path = normalize_investigation_path(
                entry.get("path") if isinstance(entry, dict) else None
            )
            if path:
                seen.add(path)
        return seen
    return set()
