from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from server.executor import ExecutorClient
from server.extensions.skills import render_skill_context
from server.openrouter.agent import AgentTurn, agent_turn, tool_schemas
from server.openrouter.errors import REASONING_EFFORT_REJECTED, ProviderError
from shared.investigation import INVESTIGATION_CALL_LIMIT, InvestigationResult
from shared.permissions import (
    PermissionDecision,
    PermissionRule,
    apply_approval_policy,
    bash_can_prompt,
    bash_command_tokens,
    resolve_permission,
    rule_scope,
)
from shared.requests import AgentRunRequest, reasoning_fallback, reasoning_payload_effort
from shared.tools import (
    MUTATION_TOOLS,
    PARALLEL_SAFE_TOOLS,
    WEB_TOOLS,
    ToolRequest,
    ToolResult,
    mode_tools,
)

from .approvals import ApprovalCoordinator, ApprovalTimeoutError
from .arguments import ArgumentError, parse_call_arguments, validate_arguments
from .budgets import (
    WIND_DOWN_WARNING_SHARE,
    BudgetExceededError,
    BudgetProfile,
    RunBudget,
    requires_budget_approval,
    resolve_budget_profile,
)
from .context import compact_agent_context, estimate_message_tokens
from .investigation import RepositoryInvestigation
from .plan import PlanError, PlanState
from .prompts import PROMPT_VERSION, agent_prompt
from .redaction import Redactor
from .results import READ_OVERHEAD_BYTES, bound_text, format_error, format_tool_result
from .session_context import (
    HANDOFF_HEADER,
    MAX_AGENTS_MD_BYTES,
    is_session_context,
    render_executor_facts,
    render_run_notes,
    render_session_context,
    render_snapshot,
    stable_sources,
)
from .tool_calls import Message, parse_tool_call, tool_message
from .usage import MAX_TRACKED_SESSIONS, TurnRecord, UsageLedger, classify_cache_miss
from .web import WebToolError, WebTools, check_url

Append = Callable[[str, str, dict[str, Any]], Awaitable[Any]]

MAX_PARALLEL_TOOL_CALLS = 8
# Parent-loop provider retries: the investigation loop's policy, with jitter.
PROVIDER_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_MULTIPLIER = 3.0
# Compaction: the high-water mark is the smallest of the profile's working-set cap, this share
# of the window, and what the window can still hold after one full output turn and one large
# result; compaction returns to a third of it.
HIGH_WATER_WINDOW_SHARE = 0.75
LOW_WATER_SHARE = 1 / 3
HARD_CEILING_WINDOW_SHARE = 0.9
# Room for one turn's output plus one large result (the ``read`` cap in tokens) below the high
# water, so a single over-large turn can never fill the request budget.
TURN_GROWTH_RESERVE_TOKENS = 32_000
MIN_HIGH_WATER_TOKENS = 4_000
# The effective mark is never below what the fixed context needs: the leading system prefix,
# one newest exchange that ``recent_exchanges`` always keeps, and the hysteresis that keeps the
# compacted context below the trigger.
EXCHANGE_RESERVE_TOKENS = 2_000
COMPACTION_HYSTERESIS_TOKENS = 2_000
# Room for ``HANDOFF_REQUEST`` and provider framing in the summary call, so the summary request
# itself is sent below the effective mark.
COMPACTION_REQUEST_RESERVE_TOKENS = 1_000
# Covers the difference between the local estimate and the provider's own count.
PROMPT_ESTIMATE_MARGIN_TOKENS = 2_000
SUMMARY_MAX_TOKENS = 2_500
MAX_STEERING_MESSAGES = 16
# Prefixes of system messages written by earlier gateway versions; dropped when a stored session
# is brought into the cache-stable layout.
LEGACY_CONTEXT_PREFIXES = (
    "UNTRUSTED WORKSPACE INSTRUCTIONS",
    "SKILL [",
    "USER WORKSPACE CONFIGURATION",
    "LOCAL EXECUTOR CONTEXT",
)
HANDOFF_REQUEST = (
    "COMPACTION REQUEST (application-generated): the context is about to be compacted and "
    "earlier turns will be removed. Do not call tools. Write a HANDOFF block for yourself, at "
    "most about 1,500 words, with these headings:\n"
    "Objective and constraints\nDecisions made\nFiles touched (from the fresh status below)\n"
    "Commands run and their results\nPlan state and open questions\nNext steps"
)
LENGTH_NOTE = (
    "RUN NOTE: your last reply hit the output limit and was cut off, so its mutation calls "
    "were not applied. Write large files in parts: `write` the first part, then `write` with "
    '`mode="append"` one part at a time.'
)
# The compaction handoff carries a fresh read-only status snapshot, bounded like other results.
STATUS_SNAPSHOT_BYTES = 4_000
# Budget wind-down (3.3).
WRAP_UP_NOTE = (
    "RUN NOTE: {budget} budget is {percent}% used. Wrap up: finish the current step, leave "
    "staging consistent, and avoid starting new work."
)
WIND_DOWN_REQUEST = (
    "WIND-DOWN REQUEST (application-generated): {why} Do not call tools. Write a short "
    "summary for the user, at most about 250 words: what was done, whether staging is "
    "consistent, what remains, and the next step."
)
WIND_DOWN_REASONS = {
    "iteration": "the run's turn budget is used up.",
    "tool-call": "the run's tool-call budget is used up.",
    "wall-time": "the run's time budget is used up.",
    "cost": "the run's cost budget is used up.",
    "no_progress": "the last several turns produced no new tool output and no staged change.",
}
NOT_EXECUTED = "error budget.exhausted: not executed; the run's budget is used up."
# Loop resilience (3.5).
EMPTY_REPLY_NOTE = (
    "RUN NOTE: your last reply was empty. Continue the task with a tool call, or write your "
    "final answer."
)
REPEAT_NOTE = (
    "NOTE: this exact call has now failed the same way {count} times. Do not repeat it; "
    "change your approach."
)
REPEATED_FAILURES = 3
NO_PROGRESS_TURNS = 4
# Streamed text is journaled as ``model.stream`` at a bounded rate (3.1).
STREAM_FLUSH_SECONDS = 0.5
STREAM_FLUSH_CHARS = 4_000
STREAM_MAX_CHARS = 64_000


class ContextFloorTooLarge(Exception):
    """The fixed session context cannot fit the request's prompt budget.

    Raised before an over-limit provider call: a window that cannot hold the static prefix, one
    exchange, and one output turn is a configuration error, not a prompt to send.
    """


@dataclass(frozen=True, slots=True)
class HighWater:
    """The compaction marks of one check: the request-derived mark, the fixed-context floor, the
    floor learned from a previous compaction, and the effective mark they produce."""

    requested: int
    floor: int
    learned: int
    effective: int
    binding: str


@dataclass(slots=True)
class RunState:
    """Per-run facts fixed at start (tool shape, network) and the loop's own bookkeeping."""

    offered: frozenset[str]
    network: bool
    bash_description: bool
    redactor: Redactor
    plan: PlanState
    plan_key: str
    tool_shape: str
    warned: bool = False
    empty_replies: int = 0
    idle_turns: int = 0
    progressed: bool = False
    streamed_turns: int = 0
    seen_results: set[str] = field(default_factory=set)
    failures: dict[str, int] = field(default_factory=dict)
    # The selection sent on this run's turns; a provider rejection steps it down (3.2).
    effort: str | None = None


@dataclass(slots=True)
class SeenFile:
    """What the model has seen of one staged file, keyed by its current hash.

    ``covered`` holds merged, sorted, non-overlapping byte ranges, so several contiguous
    chunked reads prove the same thing as one whole-file read. A changed hash starts a new
    record: coverage of older content never vouches for a rewrite of newer content.
    """

    sha256: str
    size_bytes: int
    covered: list[tuple[int, int]] = field(default_factory=list)
    complete: bool = False


class _DeltaJournal:
    """Journal streamed text as ``model.stream`` events, coalesced and capped per turn."""

    def __init__(self, append: Append, run_id: str, turn: int):
        self.append = append
        self.run_id = run_id
        self.turn = turn
        self.buffer: list[str] = []
        self.pending = 0
        self.sent = 0
        self.last = time.monotonic()

    async def __call__(self, text: str) -> None:
        if self.sent >= STREAM_MAX_CHARS:
            return
        self.buffer.append(text)
        self.pending += len(text)
        now = time.monotonic()
        if self.pending >= STREAM_FLUSH_CHARS or now - self.last >= STREAM_FLUSH_SECONDS:
            await self.flush()

    async def flush(self) -> None:
        if not self.buffer:
            return
        text = "".join(self.buffer)[: max(0, STREAM_MAX_CHARS - self.sent)]
        self.buffer.clear()
        self.pending = 0
        self.last = time.monotonic()
        if not text:
            return
        self.sent += len(text)
        await self.append(
            self.run_id,
            "model.stream",
            {"turn": self.turn, "text": text, "capped": self.sent >= STREAM_MAX_CHARS},
        )


class AgentRuntime:
    def __init__(
        self,
        executor: ExecutorClient,
        approvals: ApprovalCoordinator,
        append: Append,
        *,
        rules_loader: Callable[[str], tuple[PermissionRule, ...]] | None = None,
        snapshot_saver: Callable[..., None] | None = None,
        session_saver: Callable[..., None] | None = None,
        pause_requested: Callable[[str], bool] | None = None,
        plan_loader: Callable[[str], dict[str, Any]] | None = None,
        plan_saver: Callable[[str, dict[str, Any]], None] | None = None,
        web: WebTools | None = None,
    ):
        self.executor = executor
        self.approvals = approvals
        self.append = append
        self.rules_loader = rules_loader or (lambda _workspace: ())
        self.snapshot_saver = snapshot_saver
        self.session_saver = session_saver
        self.pause_requested = pause_requested or (lambda _run_id: False)
        self.plan_loader = plan_loader or (lambda _key: {})
        self.plan_saver = plan_saver
        self.web = web or WebTools()
        self._state: dict[str, RunState] = {}
        self.active_request: dict[str, str] = {}
        self._run_rules: dict[str, list[PermissionRule]] = {}
        self._approved_budget_sessions: set[str] = set()
        self._investigation_calls: dict[str, int] = {}
        self._api_keys: dict[str, str] = {}
        self._steering: dict[str, deque[str]] = {}
        # Per run: what the agent has read of each path, keyed by the current content hash.
        self._seen_files: dict[str, dict[str, SeenFile]] = {}
        # Per run: (read key) -> (hash, tool_call_id, model text, read view) of earlier reads.
        self._reads: dict[
            str, dict[tuple[Any, ...], tuple[str, str, str, dict[str, Any] | None]]
        ] = {}
        self._live: dict[str, list[Message]] = {}
        self._expected_miss: dict[str, str] = {}
        # Compaction accounting per run: the measured tool-schema overhead, the provider's
        # framed-prompt overhead over the local estimate, the previous turn's actual prompt, and
        # the floor learned from a compaction that could not reach the low-water mark.
        self._schema_tokens: dict[str, int] = {}
        self._prompt_overhead: dict[str, int] = {}
        self._last_prompt_tokens: dict[str, int] = {}
        self._last_prompt_message_count: dict[str, int] = {}
        self._compaction_floor: dict[str, int] = {}
        # Per session: the previous request (for cache classification) and cumulative usage.
        self._last_turn: OrderedDict[str, TurnRecord] = OrderedDict()
        self._session_usage: OrderedDict[str, UsageLedger] = OrderedDict()

    # -- public ------------------------------------------------------------------------------

    def steer(self, run_id: str, text: str) -> bool:
        """Queue a user message for the next model turn of an active run."""
        queue = self._steering.get(run_id)
        if queue is None or len(queue) >= MAX_STEERING_MESSAGES:
            return False
        queue.append(text)
        return True

    def is_active(self, run_id: str) -> bool:
        return run_id in self._steering

    async def cancel(self, run_id: str) -> None:
        request_id = self.active_request.get(run_id)
        if request_id:
            await self.executor.cancel(request_id)

    async def run(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        *,
        initial_messages: list[Message] | None = None,
        visible_messages: list[Message] | None = None,
        budget_state: dict[str, Any] | None = None,
        resumed: bool = False,
        continuation: bool = False,
    ) -> None:
        """Run one Plan or Agent run. ``continuation`` marks a resume with a fresh budget
        after a budget pause (3.3)."""
        profile = resolve_budget_profile(request.budget_profile)
        budget = RunBudget.from_profile(profile.name)
        if budget_state:
            budget.restore(budget_state)
        messages = [dict(x) for x in (initial_messages or request.messages)]
        visible = [dict(x) for x in (visible_messages or request.messages)]
        self._api_keys[run_id] = api_key
        self._steering[run_id] = deque()
        self._seen_files[run_id] = {}
        self._reads[run_id] = {}
        self._schema_tokens.pop(run_id, None)
        self._prompt_overhead.pop(run_id, None)
        self._last_prompt_tokens.pop(run_id, None)
        self._last_prompt_message_count.pop(run_id, None)
        self._compaction_floor.pop(run_id, None)
        try:
            await self._approve_budget_profile(run_id, request, profile, budget)
            status = await self.executor.status()
            if status.get("workspace_id") != request.workspace_id:
                raise RuntimeError("executor workspace identity mismatch")
            state = self._state[run_id] = self._run_state(run_id, request, status)
            if request.network_mode == "registries" and not state.network:
                raise RuntimeError(
                    "The registries network mode needs a runtime started with registry access."
                )
            await self.append(
                run_id,
                "run.resumed" if resumed else "run.started",
                {
                    "mode": request.mode,
                    "workspace_id": request.workspace_id,
                    "approval_policy": request.approval_policy,
                    "network_mode": request.network_mode,
                    "network": state.network,
                    "reasoning_effort": reasoning_payload_effort(state.effort),
                    "compaction_threshold_tokens": _high_water(request, profile),
                    "prompt_version": PROMPT_VERSION,
                    "budget": budget.snapshot(),
                    "plan": state.plan.to_dict() if not state.plan.empty else None,
                },
            )
            # A resumed snapshot already has the layout, unless an earlier version saved it.
            if not resumed or not (len(messages) >= 2 and is_session_context(messages[1])):
                messages = await self._session_layout(
                    run_id, request, messages, status, profile
                )
            # Run notes follow the run's user message; nothing above the history is edited.
            messages.append(
                {
                    "role": "user",
                    "content": render_run_notes(
                        request.mode,
                        request.approval_policy,
                        budget.snapshot(),
                        resumed=resumed,
                        network=state.network and request.mode == "agent",
                        continuation=continuation,
                    ),
                }
            )
            await self.append(
                run_id,
                "context.inspected",
                {
                    "estimated_tokens": estimate_message_tokens(messages),
                    "limit_tokens": request.context_limit_tokens,
                    "high_water_tokens": _high_water(request, profile),
                    "message_count": len(messages),
                    "session_id": request.session_id,
                    "budget": budget.snapshot(),
                },
            )
            # The effective mark depends on the rendered prefix, so it is journaled once the
            # session layout exists; `context.compacted` and `context.compaction_floor` record
            # any later movement (CONTEXT_COMPACTION_LIMITS_PLAN 3.3).
            mark = self._effective_high_water(run_id, request, profile, messages)
            await self.append(
                run_id,
                "context.high_water",
                {
                    **_mark_payload(mark),
                    "schema_tokens": self._schema_overhead(run_id, request),
                    "trigger_tokens": _trigger_tokens(mark.effective),
                    "limit_tokens": request.context_limit_tokens,
                },
            )
            self._save_snapshot(run_id, request, messages, visible, budget, True)
            await self._agent_loop(run_id, request, api_key, messages, visible, budget, profile)
        except ContextFloorTooLarge as exc:
            await self._fail(run_id, "context.floor_too_large", str(exc), budget)
        except ApprovalTimeoutError as exc:
            await self.append(
                run_id,
                "approval.timeout",
                {
                    "approval_id": exc.approval_id,
                    "message": str(exc),
                    "reason": "wall_time_budget",
                    "budget": budget.snapshot(),
                },
            )
            await self._fail(run_id, "approval.timeout", str(exc), budget)
        except ProviderError as exc:
            await self._fail(run_id, exc.code, str(exc), budget, retryable=exc.retryable)
        except BudgetExceededError as exc:
            await self._fail(run_id, f"budget.{exc.budget}", str(exc)[:2000], budget)
        except Exception as exc:
            await self._fail(run_id, "agent.failed", str(exc)[:2000], budget)
        finally:
            self.active_request.pop(run_id, None)
            self._run_rules.pop(run_id, None)
            self._investigation_calls.pop(run_id, None)
            self._api_keys.pop(run_id, None)
            self._steering.pop(run_id, None)
            self._seen_files.pop(run_id, None)
            self._reads.pop(run_id, None)
            self._live.pop(run_id, None)
            self._expected_miss.pop(run_id, None)
            self._schema_tokens.pop(run_id, None)
            self._prompt_overhead.pop(run_id, None)
            self._last_prompt_tokens.pop(run_id, None)
            self._last_prompt_message_count.pop(run_id, None)
            self._compaction_floor.pop(run_id, None)
            self._state.pop(run_id, None)

    def _get_state(self, run_id: str, request: AgentRunRequest) -> RunState:
        """The run's state; a call outside ``run`` (a direct test call) gets the defaults."""
        state = self._state.get(run_id)
        if state is None:
            state = self._state[run_id] = self._run_state(run_id, request, {})
        return state

    def _run_state(
        self, run_id: str, request: AgentRunRequest, status: dict[str, Any]
    ) -> RunState:
        """The run's fixed tool shape: offered tools, network, and the bash description."""
        environment = status.get("environment") if isinstance(status, dict) else None
        egress = environment.get("egress") if isinstance(environment, dict) else None
        network = isinstance(egress, dict) and bool(egress.get("enabled"))
        agent = request.mode == "agent"
        offered = mode_tools(
            request.mode, web=agent and request.workspace_config.get("web_tools") is True
        )
        bash_description = agent and bash_can_prompt(request.approval_policy, network=network)
        shape = hashlib.sha256(
            json.dumps([sorted(offered), bash_description]).encode("utf-8")
        ).hexdigest()[:12]
        plan_key = request.session_id or f"run:{run_id}"
        return RunState(
            offered,
            network,
            bash_description,
            Redactor.from_workspace_config(request.workspace_config),
            PlanState.from_dict(self.plan_loader(plan_key)),
            plan_key,
            shape,
            effort=request.reasoning_effort,
        )

    # -- layout --------------------------------------------------------------------------------

    async def _session_layout(
        self,
        run_id: str,
        request: AgentRunRequest,
        messages: list[Message],
        status: dict[str, Any],
        profile: BudgetProfile,
    ) -> list[Message]:
        """Bring the message list into the cache-stable layout.

        ``[agent prompt, session context, *history]``. A replayed session keeps its session
        context verbatim (including the frozen snapshot) unless one of its sources changed;
        a rebuild is an expected cache miss.
        """
        prompt = {"role": "system", "content": agent_prompt(request.mode)}
        conversation_prompt = "\n\n".join(
            str(item.get("content") or "")
            for item in _leading_system(list(request.messages))
            if str(item.get("content") or "").strip()
        )
        has_layout = len(messages) >= 2 and is_session_context(messages[1])
        if has_layout:
            existing = str(messages[1].get("content") or "")
            history = messages[2:]
        else:
            existing = ""
            history = _history_without_context(messages)
        snapshot_data = await self._workspace_snapshot(run_id, request)
        available = self._get_state(run_id, request).offered
        instructions = request.workspace_config.get("instructions")
        sources = {
            "facts": render_executor_facts(
                status,
                request.mode,
                read_bytes=profile.result_cap("read") - READ_OVERHEAD_BYTES,
                bash_chars=profile.bash_result_chars,
                declared_checks=_declared_checks(request.workspace_config),
            ),
            "conversation_prompt": conversation_prompt,
            "workspace_instructions": str(instructions)
            if isinstance(instructions, str)
            else "",
            "agents_md": str(snapshot_data.get("agents_md") or ""),
            "agents_md_truncated": bool(snapshot_data.get("agents_md_truncated")),
            "skills": render_skill_context(request.skills, set(available)),
        }
        candidate = render_session_context(**sources)
        if has_layout and stable_sources(existing) == stable_sources(candidate):
            context = existing
        else:
            context = render_session_context(
                **sources, snapshot=render_snapshot(snapshot_data.get("snapshot") or {})
            )
            if has_layout:
                self._expected_miss[run_id] = "session_context_changed"
        return [prompt, {"role": "system", "content": context}, *history]

    async def _workspace_snapshot(
        self, run_id: str, request: AgentRunRequest
    ) -> dict[str, Any]:
        """The executor's snapshot and published AGENTS.md; falls back to reading AGENTS.md."""
        try:
            data = await self.executor.workspace_snapshot()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        try:
            result = await self.executor.execute(
                ToolRequest(
                    str(uuid.uuid4()),
                    run_id,
                    "read",
                    request.mode,
                    {"path": "AGENTS.md", "max_bytes": MAX_AGENTS_MD_BYTES},
                )
            )
        except Exception:
            return {}
        if not result.ok:
            return {}
        return {
            "agents_md": result.output,
            "agents_md_truncated": bool(result.data.get("truncated")),
        }

    # -- loop ----------------------------------------------------------------------------------

    async def _agent_loop(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        messages: list[Message],
        visible: list[Message],
        budget: RunBudget,
        profile: BudgetProfile,
    ) -> None:
        run_mutated = False
        state = self._get_state(run_id, request)
        self._live[run_id] = messages
        try:
            while True:
                if self.pause_requested(run_id):
                    self._save_snapshot(run_id, request, messages, visible, budget, True)
                    await self.append(
                        run_id,
                        "run.paused",
                        {
                            "reason": "user.requested",
                            "resumable": True,
                            "budget": budget.snapshot(),
                        },
                    )
                    return
                # Steering joins between turns only, after every tool result of the last turn.
                queue = self._steering.get(run_id)
                while queue:
                    messages.append({"role": "user", "content": queue.popleft()})
                messages = await self._maybe_compact(
                    run_id, request, api_key, messages, budget, profile
                )
                self._live[run_id] = messages
                reason = budget.wind_down_reason()
                if reason is not None:
                    await self._wind_down(
                        run_id, request, api_key, messages, visible, budget, reason, run_mutated
                    )
                    return
                await self._maybe_warn(run_id, messages, budget, state)
                budget.consume_iteration()
                try:
                    turn = await self._model_turn(
                        run_id,
                        request,
                        api_key,
                        messages,
                        max_tokens=profile.max_output_tokens,
                        reasoning_effort=state.effort,
                        stream=True,
                    )
                except ProviderError as exc:
                    if not exc.retryable:
                        raise
                    # The snapshot holds the messages before this turn, so a resume retries it.
                    self._save_snapshot(run_id, request, messages, visible, budget, True)
                    await self.append(
                        run_id,
                        "run.paused",
                        {
                            "reason": "provider_error",
                            "resumable": True,
                            "code": exc.code,
                            "message": str(exc)[:2000],
                            "budget": budget.snapshot(),
                        },
                    )
                    return
                budget.add_usage(turn.usage, check=False)
                await self._record_turn(run_id, request, turn, budget)
                # Record the measured prompt before this turn's message is appended, so the next
                # compaction check knows how much the provider actually counted.
                self._observe_prompt(run_id, request, messages, turn)
                budget.check()
                content = str(turn.content or "")
                if not turn.tool_calls and not content.strip():
                    # An empty or reasoning-only reply gets one re-prompt, then the run stops.
                    state.empty_replies += 1
                    if state.empty_replies == 1:
                        messages.append({"role": "user", "content": EMPTY_REPLY_NOTE})
                        await self.append(run_id, "loop.note", {"kind": "empty_reply"})
                        continue
                    self._save_turn(run_id, request, messages, visible, budget, True)
                    await self.append(
                        run_id,
                        "run.paused",
                        {
                            "reason": "empty_reply",
                            "resumable": True,
                            "message": "The model returned empty replies twice in a row.",
                            "budget": budget.snapshot(),
                        },
                    )
                    return
                state.empty_replies = 0
                messages.append(turn.message)
                transcript = [*visible, {"role": "assistant", "content": content}]
                if not turn.tool_calls:
                    await self.append(run_id, "model.delta", {"text": content})
                    await self.append(
                        run_id,
                        "usage.updated",
                        {**turn.usage, "budget": budget.snapshot(), **budget.usage()},
                    )
                    self._save_turn(run_id, request, messages, transcript, budget, False)
                    if request.mode == "agent" and run_mutated:
                        await self._offer_publish(run_id, request.approval_policy)
                    await self.append(
                        run_id,
                        "run.completed",
                        {
                            "iterations": budget.iterations,
                            "tool_calls": budget.tool_calls,
                            **budget.usage(),
                            "session": self._session_ledger(request, run_id).summary(),
                            "budget": budget.snapshot(),
                            "plan": state.plan.to_dict() if not state.plan.empty else None,
                        },
                    )
                    return
                if content.strip():
                    await self.append(run_id, "model.narration", {"text": content[:4_000]})
                # The investigation call cap is per turn: each turn starts with a fresh allowance.
                self._investigation_calls.pop(run_id, None)
                state.progressed = False
                output_truncated = turn.finish_reason == "length"
                groups = tool_call_groups(turn.tool_calls)
                for group in groups:
                    reason = budget.wind_down_reason(between_turns=False)
                    if reason is None and budget.remaining_tool_calls < len(group):
                        reason = "tool-call"
                    if reason is not None:
                        _fill_missing_results(messages)
                        await self._wind_down(
                            run_id,
                            request,
                            api_key,
                            messages,
                            visible,
                            budget,
                            reason,
                            run_mutated,
                        )
                        return
                    for _ in group:
                        budget.consume_tool_call()
                        self._session_ledger(request, run_id).tool_calls += 1
                    if len(group) == 1:
                        mutated = await self._execute_tool_call(
                            run_id,
                            request,
                            group[0],
                            messages,
                            budget,
                            profile,
                            output_truncated=output_truncated,
                        )
                        run_mutated = mutated or run_mutated
                        continue
                    for buffer in await self._execute_parallel_group(
                        run_id,
                        request,
                        group,
                        budget,
                        profile,
                        output_truncated=output_truncated,
                    ):
                        messages.extend(buffer)
                if turn.finish_reason == "length":
                    messages.append({"role": "user", "content": LENGTH_NOTE})
                await self.append(
                    run_id, "usage.updated", {"budget": budget.snapshot(), **budget.usage()}
                )
                self._save_turn(run_id, request, messages, transcript, budget, True)
                # Several turns with no new tool output and no staged change end in a wind-down.
                state.idle_turns = 0 if state.progressed else state.idle_turns + 1
                if state.idle_turns >= NO_PROGRESS_TURNS:
                    await self._wind_down(
                        run_id,
                        request,
                        api_key,
                        messages,
                        visible,
                        budget,
                        "no_progress",
                        run_mutated,
                    )
                    return
        except BudgetExceededError as exc:
            # A hard limit broke mid-turn (one response overspent the reserve): pause without
            # another paid turn, keeping the history valid for a continuation.
            _fill_missing_results(messages)
            await self._wind_down(
                run_id,
                request,
                api_key,
                messages,
                visible,
                budget,
                exc.budget,
                run_mutated,
                summarize=False,
            )

    async def _maybe_warn(
        self, run_id: str, messages: list[Message], budget: RunBudget, state: RunState
    ) -> None:
        """Once per run, at 85% of any budget, ask the agent to wrap up."""
        if state.warned:
            return
        name, share = budget.usage_fraction()
        if share < WIND_DOWN_WARNING_SHARE:
            return
        state.warned = True
        percent = min(100, int(share * 100))
        messages.append(
            {
                "role": "user",
                "content": WRAP_UP_NOTE.format(budget=f"The {name}", percent=percent),
            }
        )
        await self.append(
            run_id, "loop.note", {"kind": "budget_warning", "budget": name, "percent": percent}
        )

    async def _wind_down(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        messages: list[Message],
        visible: list[Message],
        budget: RunBudget,
        reason: str,
        run_mutated: bool,
        *,
        summarize: bool = True,
    ) -> None:
        """End the run with a summary and pause it so it can continue later (3.3).

        The summary turn uses the session's tool definitions with tool calls forbidden, so it
        extends the cached prefix; it is funded from the budget's reserve. A budget pause is
        resumed only with a new budget; a no-progress pause resumes with the remaining one.
        """
        state = self._get_state(run_id, request)
        plan_text = state.plan.render()
        why = WIND_DOWN_REASONS.get(reason, f"the {reason} limit was reached.")
        summary = ""
        usage: dict[str, Any] = {}
        if summarize:
            request_text = WIND_DOWN_REQUEST.format(why=why[0].upper() + why[1:])
            if plan_text:
                request_text += f"\nCurrent plan (from update_plan):\n{plan_text}"
            candidate = [*messages, {"role": "user", "content": request_text}]
            try:
                turn = await self._model_turn(
                    run_id,
                    request,
                    api_key,
                    candidate,
                    allowed_tools=set(),
                    max_tokens=SUMMARY_MAX_TOKENS,
                    reasoning_effort="low" if request.reasoning_effort else None,
                    turn_timeout=max(10.0, budget.remaining_wall_seconds),
                )
            except ProviderError:
                turn = None
            if turn is not None:
                usage = dict(turn.usage)
                budget.add_usage(turn.usage, check=False)
                summary = str(turn.content or "").strip()
                if summary:
                    # Tool calls are forbidden here; drop any a model sent anyway, so the
                    # history stays valid for a continuation.
                    messages.append(candidate[-1])
                    messages.append(
                        {
                            key: value
                            for key, value in turn.message.items()
                            if key != "tool_calls"
                        }
                    )
        if not summary:
            summary = f"I stopped because {why}"
            if request.mode == "agent":
                summary += " Staged changes are kept; review them with Status or publish them."
            messages.append({"role": "assistant", "content": summary})
        display = summary
        if plan_text and plan_text not in summary:
            display += f"\n\nPlan:\n{plan_text}"
        await self.append(run_id, "model.delta", {"text": display})
        await self.append(
            run_id, "usage.updated", {**usage, "budget": budget.snapshot(), **budget.usage()}
        )
        transcript = [*visible, {"role": "assistant", "content": display}]
        self._save_turn(run_id, request, messages, transcript, budget, True)
        budget_pause = reason != "no_progress"
        await self.append(
            run_id,
            "run.paused",
            {
                "reason": "budget" if budget_pause else reason,
                "budget_name": reason if budget_pause else None,
                "resumable": True,
                "continuation": "new_budget" if budget_pause else "resume",
                "staged_changes": run_mutated,
                "budget": budget.snapshot(),
                "plan": state.plan.to_dict() if not state.plan.empty else None,
            },
        )

    async def _model_turn(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        messages: list[Message],
        *,
        allowed_tools: set[str] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        stream: bool = False,
        turn_timeout: float | None = None,
    ) -> AgentTurn:
        """One provider call with jittered retries; a retry resends the identical payload.

        Every call offers the session's tool shape (``RunState``), so summary and handoff
        turns extend the same cached prefix. ``stream`` journals the text as it arrives.
        """
        state = self._get_state(run_id, request)
        profile = resolve_budget_profile(request.budget_profile)
        delay = RETRY_BASE_DELAY
        stepped = False
        for attempt in range(1, PROVIDER_ATTEMPTS + 1):
            if stream and attempt == 1:
                state.streamed_turns += 1
            deltas = (
                _DeltaJournal(self.append, run_id, state.streamed_turns) if stream else None
            )
            ceiling = float(profile.turn_ceiling_seconds)
            if turn_timeout is not None:
                ceiling = min(ceiling, turn_timeout)
            try:
                turn = await agent_turn(
                    request.model,
                    messages,
                    api_key,
                    request.mode,
                    request.provider_preferences,
                    allowed_tools,
                    offered_tools=state.offered,
                    bash_description=state.bash_description,
                    idle_timeout=float(profile.turn_idle_seconds),
                    turn_timeout=ceiling,
                    session_id=request.session_id or run_id,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    on_delta=deltas,
                )
                if deltas is not None:
                    await deltas.flush()
                return turn
            except ProviderError as exc:
                lower = reasoning_fallback(reasoning_effort)
                if (
                    exc.code == REASONING_EFFORT_REJECTED
                    and lower is not None
                    and not stepped
                    and attempt < PROVIDER_ATTEMPTS
                ):
                    # Retry once a level down; the lower level holds for the rest of the run.
                    stepped = True
                    await self.append(
                        run_id,
                        "reasoning.effort_clamped",
                        {
                            "requested": reasoning_payload_effort(reasoning_effort),
                            "sent": reasoning_payload_effort(lower),
                            "selection": lower,
                            "message": str(exc)[:500],
                        },
                    )
                    reasoning_effort = state.effort = lower
                    continue
                if not exc.retryable or attempt == PROVIDER_ATTEMPTS:
                    raise
                wait = delay * random.uniform(0.5, 1.5)
                await self.append(
                    run_id,
                    "model.retry",
                    {
                        "attempt": attempt,
                        "max_attempts": PROVIDER_ATTEMPTS,
                        "code": exc.code,
                        "message": str(exc)[:500],
                        "delay_seconds": round(wait, 3),
                    },
                )
                await asyncio.sleep(wait)
                delay *= RETRY_MULTIPLIER
        raise AssertionError("unreachable")

    async def _record_turn(
        self, run_id: str, request: AgentRunRequest, turn: AgentTurn, budget: RunBudget
    ) -> None:
        """Journal one turn's usage and classify any prompt-cache miss."""
        session_key = request.session_id or f"run:{run_id}"
        previous = self._last_turn.get(session_key)
        now = time.monotonic()
        model_and_mode = f"{request.model}|{request.mode}"
        expected = self._expected_miss.pop(run_id, None)
        cause = classify_cache_miss(
            previous,
            turn.usage,
            provider=turn.provider,
            model_and_mode=model_and_mode,
            now=now,
            expected=expected,
        )
        ledger = self._session_ledger(request, run_id)
        ledger.add(turn.usage)
        payload = {
            "turn": budget.turns,
            "prompt_tokens": turn.usage.get("prompt_tokens"),
            "cached_tokens": turn.usage.get("cached_tokens"),
            "cache_write_tokens": turn.usage.get("cache_write_tokens"),
            "completion_tokens": turn.usage.get("completion_tokens"),
            "reasoning_tokens": turn.usage.get("reasoning_tokens"),
            "cost": turn.usage.get("cost"),
            "provider": turn.provider,
            "model": turn.model or request.model,
            "finish_reason": turn.finish_reason,
            "miss_cause": cause,
            "previous_prompt_tokens": previous.prompt_tokens if previous else None,
            "run": budget.usage(),
            "session": ledger.summary(),
        }
        await self.append(run_id, "usage.turn", payload)
        if cause == "unexpected":
            await self.append(
                run_id,
                "cache.unexpected_miss",
                {
                    "turn": budget.turns,
                    "provider": turn.provider,
                    "prompt_tokens": turn.usage.get("prompt_tokens"),
                    "cached_tokens": turn.usage.get("cached_tokens"),
                    "previous_prompt_tokens": previous.prompt_tokens if previous else None,
                },
            )
        prompt_tokens = int(turn.usage.get("prompt_tokens") or 0)
        self._last_turn[session_key] = TurnRecord(
            prompt_tokens,
            turn.provider or (previous.provider if previous else None),
            model_and_mode,
            now,
        )
        self._last_turn.move_to_end(session_key)
        while len(self._last_turn) > MAX_TRACKED_SESSIONS:
            self._last_turn.popitem(last=False)

    def _session_ledger(self, request: AgentRunRequest, run_id: str) -> UsageLedger:
        key = request.session_id or f"run:{run_id}"
        ledger = self._session_usage.get(key)
        if ledger is None:
            ledger = self._session_usage[key] = UsageLedger()
        self._session_usage.move_to_end(key)
        while len(self._session_usage) > MAX_TRACKED_SESSIONS:
            self._session_usage.popitem(last=False)
        return ledger

    # -- compaction ----------------------------------------------------------------------------

    def _schema_overhead(self, run_id: str, request: AgentRunRequest) -> int:
        """The measured token cost of the offered tool schemas, computed once per run.

        The schemas are fixed for the session and the provider counts them on every call, but
        ``estimate_message_tokens`` serializes messages only; without this the local estimate
        understates every prompt by the size of the tool list.
        """
        cached = self._schema_tokens.get(run_id)
        if cached is not None:
            return cached
        state = self._get_state(run_id, request)
        schemas = tool_schemas(
            request.mode, state.offered, bash_description=state.bash_description
        )
        serialized = json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
        tokens = estimate_message_tokens([{"role": "system", "content": serialized}])
        self._schema_tokens[run_id] = tokens
        return tokens

    def _observe_prompt(
        self, run_id: str, request: AgentRunRequest, messages: list[Message], turn: AgentTurn
    ) -> None:
        """Record the provider's own prompt count as a lower bound for the next estimate."""
        prompt_tokens = int(turn.usage.get("prompt_tokens") or 0)
        if prompt_tokens <= 0:
            return
        schema_tokens = self._schema_overhead(run_id, request)
        overhead = max(0, prompt_tokens - estimate_message_tokens(messages) - schema_tokens)
        self._prompt_overhead[run_id] = max(self._prompt_overhead.get(run_id, 0), overhead)
        self._last_prompt_tokens[run_id] = prompt_tokens
        self._last_prompt_message_count[run_id] = len(messages)

    def _prompt_estimate(
        self, run_id: str, request: AgentRunRequest, messages: list[Message]
    ) -> int:
        """The best estimate of the next prompt: local estimate plus framing and margin, or the
        previous actual prompt plus the messages added since (CONTEXT_COMPACTION_LIMITS_PLAN
        3.4). The previous actual count is a lower bound, so a provider that reports more than
        the local estimate is never ignored."""
        local = (
            estimate_message_tokens(messages)
            + self._schema_overhead(run_id, request)
            + self._prompt_overhead.get(run_id, 0)
            + PROMPT_ESTIMATE_MARGIN_TOKENS
        )
        last = self._last_prompt_tokens.get(run_id, 0)
        if last <= 0:
            return local
        index = min(self._last_prompt_message_count.get(run_id, 0), len(messages))
        return max(local, last + estimate_message_tokens(messages[index:]))

    def _effective_high_water(
        self,
        run_id: str,
        request: AgentRunRequest,
        profile: BudgetProfile,
        messages: list[Message],
    ) -> HighWater:
        """The mark compaction actually works against, including any floor learned this run."""
        return effective_high_water(
            request, profile, messages, learned=self._compaction_floor.get(run_id, 0)
        )

    async def _fresh_status(self, run_id: str, request: AgentRunRequest) -> str:
        """Refresh staged status for the compaction handoff (the summary turn cannot call tools)."""
        request_id = str(uuid.uuid4())
        try:
            result = await self.executor.execute(
                ToolRequest(request_id, run_id, "status", request.mode, {})
            )
        except Exception as exc:
            return await self._status_unavailable(run_id, request_id, str(exc))
        if not result.ok:
            return await self._status_unavailable(
                run_id, request_id, result.output or result.error_code or "status failed"
            )
        text = format_tool_result("status", {}, result)
        bounded, _clipped = bound_text(text, STATUS_SNAPSHOT_BYTES)
        counts = result.data.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        await self.append(
            run_id,
            "context.status_snapshot",
            {"ok": True, "counts": counts, "bytes": len(bounded.encode("utf-8"))},
        )
        return bounded

    async def _status_unavailable(self, run_id: str, request_id: str, message: str) -> str:
        text = "status unavailable; run status after compaction"
        await self.append(
            run_id,
            "context.status_snapshot",
            {"ok": False, "counts": None, "bytes": len(text.encode("utf-8"))},
        )
        await self.append(
            run_id,
            "context.status_unavailable",
            {"request_id": request_id, "message": " ".join(message.split())[:500]},
        )
        return text

    async def _maybe_compact(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        messages: list[Message],
        budget: RunBudget,
        profile: BudgetProfile,
    ) -> list[Message]:
        """Compact at the effective high-water mark, back down to the low-water mark.

        The trigger sits one summary request below the effective mark, so the summary call
        itself is sent below the mark (CONTEXT_COMPACTION_LIMITS_PLAN 3.3).
        """
        # The base mark ignores the learned floor, so the post-compaction check can tell whether
        # the requested mark is working again (CONTEXT_COMPACTION_LIMITS_PLAN 3.5).
        base = effective_high_water(request, profile, messages)
        mark = self._effective_high_water(run_id, request, profile, messages)
        trigger = _trigger_tokens(mark.effective)
        before = self._prompt_estimate(run_id, request, messages)
        if before < trigger:
            return messages
        low = max(1_000, int(mark.effective * LOW_WATER_SHARE))
        plan_text = self._get_state(run_id, request).plan.render()
        plan_note = f"\n\nCurrent plan (from update_plan):\n{plan_text}" if plan_text else ""
        status_text = await self._fresh_status(run_id, request)
        status_prompt_note = (
            "\n\nFresh staged status (application-generated; use this for \"Files touched\"):\n"
            f"{status_text}"
        )
        status_handoff_note = f"\n\nFresh staged status (application-generated):\n{status_text}"
        prefix_length = _prefix_length(messages)
        prefix, history = messages[:prefix_length], messages[prefix_length:]
        summary_usage: dict[str, Any] = {}
        method = "summary"
        try:
            turn = await self._model_turn(
                run_id,
                request,
                api_key,
                [
                    *messages,
                    {"role": "user", "content": f"{HANDOFF_REQUEST}{status_prompt_note}"},
                ],
                allowed_tools=set(),
                max_tokens=SUMMARY_MAX_TOKENS,
                reasoning_effort="low" if request.reasoning_effort else None,
            )
            summary_usage = dict(turn.usage)
            budget.add_usage(turn.usage, check=False)
            handoff = str(turn.content or "").strip()
            if not handoff:
                raise ValueError("empty handoff")
            handoff_message = {
                "role": "user",
                "content": f"{HANDOFF_HEADER} (written by you before earlier turns were "
                f"compacted away)\n{handoff[: SUMMARY_MAX_TOKENS * 6]}{plan_note}"
                f"{status_handoff_note}",
            }
            room = max(500, low - estimate_message_tokens([*prefix, handoff_message]))
            compacted = [*prefix, handoff_message, *recent_exchanges(history, room)]
        except (ProviderError, ValueError):
            method = "deterministic"
            body = compact_agent_context(
                history, max(500, low - estimate_message_tokens(prefix)), summary_role="user"
            )
            if plan_note or status_text:
                body = [
                    {
                        "role": "user",
                        "content": f"{HANDOFF_HEADER}{plan_note}{status_handoff_note}",
                    },
                    *body,
                ]
            compacted = [*prefix, *body]
        window = request.context_limit_tokens
        after_raw = estimate_message_tokens(compacted)
        if after_raw > int(window * HARD_CEILING_WINDOW_SHARE):
            body = compact_agent_context(
                compacted[prefix_length:],
                max(500, int(window * 0.8) - estimate_message_tokens(prefix)),
                summary_role="user",
            )
            compacted = [*prefix, *body]
            after_raw = estimate_message_tokens(compacted)
        # The next turn measures the compacted prompt from scratch, not against the index of the
        # pre-compaction list.
        self._last_prompt_tokens[run_id] = 0
        self._last_prompt_message_count[run_id] = len(compacted)
        after = self._prompt_estimate(run_id, request, compacted)
        learned = after + COMPACTION_REQUEST_RESERVE_TOKENS + COMPACTION_HYSTERESIS_TOKENS
        fits = learned <= _mark_room(request, profile)
        if after >= _trigger_tokens(base.effective) and fits:
            # The fixed context keeps the compacted prompt at or above what the base mark
            # allows: raise the mark so the run does not pay for a summary turn every iteration
            # (D5). The trigger subtracts the request reserve, so adding it back here keeps the
            # compacted context one full hysteresis below the new trigger. A later compaction
            # that fits the base mark clears it again. A floor that would not fit the window is
            # never recorded: it could only fail the next check closed.
            self._compaction_floor[run_id] = learned
            await self.append(
                run_id,
                "context.compaction_floor",
                {
                    "compacted_tokens": after_raw,
                    "requested_high_water_tokens": mark.requested,
                    "effective_high_water_tokens": mark.effective,
                    "floor_tokens": self._compaction_floor[run_id],
                    "trigger_tokens": trigger,
                },
            )
        else:
            self._compaction_floor.pop(run_id, None)
        self._expected_miss[run_id] = "compaction"
        await self.append(
            run_id,
            "context.compacted",
            {
                "method": method,
                "tokens_before": before,
                "tokens_after": estimate_message_tokens(compacted),
                "tokens_after_estimated_prompt": after,
                "high_water_tokens": mark.requested,
                "effective_high_water_tokens": mark.effective,
                "trigger_tokens": trigger,
                "low_water_tokens": low,
                "summary_cost": summary_usage.get("cost"),
                "summary_usage": summary_usage,
            },
        )
        budget.check()
        return compacted

    # -- tools ---------------------------------------------------------------------------------

    async def _approve_budget_profile(
        self, run_id: str, request: AgentRunRequest, profile: BudgetProfile, budget: RunBudget
    ) -> None:
        """Ask once per session before running a profile well above the standard budget."""
        session_key = request.session_id or f"run:{run_id}"
        if (
            not requires_budget_approval(profile)
            or session_key in self._approved_budget_sessions
        ):
            return
        approval_id = str(uuid.uuid4())
        await self.append(
            run_id,
            "approval.required",
            {
                "approval_id": approval_id,
                "kind": "budget",
                "budget_profile": profile.name,
                "standard_profile": "coding",
                "budgets": {
                    "max_iterations": profile.max_iterations,
                    "max_tool_calls": profile.max_tool_calls,
                    "max_wall_seconds": profile.max_wall_seconds,
                    "max_cost": profile.max_cost,
                },
                "approval_reason": (
                    "This profile exceeds the standard coding profile by more than 2x on at least "
                    "one approved resource budget."
                ),
                "available_decisions": ["deny", "allow_run"],
            },
        )
        decision = await self.approvals.wait(
            run_id, approval_id, timeout=budget.remaining_wall_seconds
        )
        if decision != PermissionDecision.ALLOW_RUN:
            raise RuntimeError(
                f"Budget profile '{profile.name}' requires explicit user approval before the "
                "session can run."
            )
        self._approved_budget_sessions.add(session_key)

    async def _execute_parallel_group(
        self,
        run_id: str,
        request: AgentRunRequest,
        group: list[dict[str, Any]],
        budget: RunBudget,
        profile: BudgetProfile,
        *,
        output_truncated: bool = False,
    ) -> list[list[Message]]:
        """Run independent read-only calls concurrently; each fills its own message buffer so
        results can be appended in the original call order.

        Exact duplicate reads in one batch share the first call's result: the duplicate gets a
        one-line pointer without spending an executor call.
        """
        limiter = asyncio.Semaphore(MAX_PARALLEL_TOOL_CALLS)
        buffers: list[list[Message]] = [[] for _ in group]
        cap = profile.result_cap("read") - READ_OVERHEAD_BYTES
        first_by_key: dict[tuple[Any, ...], str] = {}
        scheduled: list[tuple[dict[str, Any], list[Message]]] = []

        async def run_one(raw_call: dict[str, Any], buffer: list[Message]) -> bool:
            async with limiter:
                return await self._execute_tool_call(
                    run_id,
                    request,
                    raw_call,
                    buffer,
                    budget,
                    profile,
                    track_active=False,
                    output_truncated=output_truncated,
                )

        for raw_call, buffer in zip(group, buffers, strict=True):
            request_id, name, raw_arguments = parse_tool_call(raw_call)
            duplicate_of = None
            parsed: dict[str, Any] | None = None
            if name == "read":
                try:
                    parsed = parse_call_arguments(name, raw_arguments)
                    validate_arguments(
                        name, self._schema(self._get_state(run_id, request), name), parsed
                    )
                except ArgumentError:
                    parsed = None
                if parsed is not None:
                    key = _read_dedup_key(parsed, cap)
                    duplicate_of = first_by_key.get(key)
                    if duplicate_of is None:
                        first_by_key[key] = request_id
            if duplicate_of is None:
                scheduled.append((raw_call, buffer))
                continue
            path = str((parsed or {}).get("path") or "")
            buffer.append(
                tool_message(
                    request_id,
                    f"read {path}: duplicate of tool call {duplicate_of} in this batch; "
                    "use that result.",
                )
            )
            await self.append(
                run_id,
                "read.duplicate_batch",
                {"path": path, "request_id": request_id, "duplicate_of": duplicate_of},
            )
        await asyncio.gather(*(run_one(raw_call, buffer) for raw_call, buffer in scheduled))
        return buffers

    async def _execute_tool_call(
        self,
        run_id: str,
        request: AgentRunRequest,
        raw_call: dict[str, Any],
        messages: list[Message],
        budget: RunBudget,
        profile: BudgetProfile,
        *,
        track_active: bool = True,
        output_truncated: bool = False,
    ) -> bool:
        """Run one model tool call and append its result; return whether it staged changes."""
        request_id, name, raw_arguments = parse_tool_call(raw_call)
        state = self._get_state(run_id, request)
        if name not in state.offered:
            error = format_error(
                "tool.unknown",
                f"no tool named {name or '(empty)'} in {request.mode} mode; available: "
                f"{', '.join(sorted(state.offered))}",
            )
            messages.append(
                tool_message(
                    request_id, await self._failure(run_id, name, raw_arguments, error)
                )
            )
            return False
        if output_truncated and name in MUTATION_TOOLS:
            raw_bytes = len(raw_arguments.encode("utf-8"))
            await self.append(
                run_id,
                "loop.output_truncated",
                {
                    "turn": budget.turns,
                    "tool": name,
                    "raw_argument_bytes": raw_bytes,
                    "output_limit_tokens": profile.max_output_tokens,
                },
            )
            messages.append(
                tool_message(
                    request_id,
                    format_error(
                        "tool.output_truncated",
                        f"the model output limit ({profile.max_output_tokens:,} tokens) cut off "
                        f"this {name} after {raw_bytes:,} argument characters; nothing was "
                        "applied",
                    ),
                )
            )
            return False
        try:
            arguments = parse_call_arguments(
                name, raw_arguments, output_truncated=output_truncated
            )
            validate_arguments(name, self._schema(state, name), arguments)
        except ArgumentError as exc:
            error = format_error("tool.invalid_arguments", str(exc))
            messages.append(
                tool_message(
                    request_id, await self._failure(run_id, name, raw_arguments, error)
                )
            )
            return False
        if name == "investigate_repository":
            return await self._investigate_repository(
                run_id, request, request_id, raw_arguments, messages, budget
            )
        if name == "update_plan":
            return await self._update_plan(run_id, request_id, arguments, messages)
        if name in WEB_TOOLS:
            return await self._web_tool(
                run_id, request, request_id, name, arguments, messages, budget, profile
            )
        # The approval prompt shows ``description``; the executor never receives it.
        description = arguments.pop("description", None) if name == "bash" else None
        requested_max_bytes = arguments.get("max_bytes") if name == "read" else None
        try:
            if name == "bash":
                _clamp_bash_timeout(arguments, budget)
            executor_arguments = self._executor_arguments(run_id, name, arguments, profile)
            tool_request = ToolRequest(
                request_id, run_id, name, request.mode, executor_arguments
            )
        except (TypeError, ValueError) as exc:
            messages.append(
                tool_message(request_id, format_error("tool.invalid_request", str(exc)))
            )
            return False
        await self.append(
            run_id, "tool.requested", {**tool_request.to_dict(), "budget": budget.snapshot()}
        )
        decision = await self._permission(
            run_id, request, tool_request, budget, description=description
        )
        if decision == PermissionDecision.DENY:
            result = ToolResult(
                request_id, False, output="Permission denied.", error_code="permission.denied"
            )
        else:
            if track_active:
                self.active_request[run_id] = request_id
            try:
                result = await self.executor.execute(tool_request)
            finally:
                if track_active:
                    self.active_request.pop(run_id, None)
        # Coverage and guarded-write enrichment must be visible to the journaled result.
        self._observe_files(run_id, name, executor_arguments, result)
        await self.append(run_id, "tool.output", result.to_dict())
        if name == "bash" and bool(result.data.get("rolled_back")):
            await self.append(
                run_id,
                "mutation.rollback",
                {
                    "request_id": request_id,
                    "tool": name,
                    "reason": result.data.get("rollback_reason", "unknown"),
                    "checkpoint_id": result.data.get("checkpoint_id"),
                },
            )
        if result.data.get("checkpoint_id"):
            await self.append(
                run_id,
                "checkpoint.created",
                {
                    "checkpoint_id": str(result.data["checkpoint_id"]),
                    "request_id": request_id,
                    "tool": name,
                },
            )
        text = await self._model_tool_result(
            run_id,
            request_id,
            name,
            executor_arguments,
            result,
            profile,
            requested_max_bytes=requested_max_bytes,
        )
        if not result.ok:
            text = await self._failure(run_id, name, raw_arguments, text)
        workspace_status = result.data.get("workspace_status")
        staged = (
            name in MUTATION_TOOLS
            and result.ok
            and isinstance(workspace_status, dict)
            and bool(workspace_status.get("staged"))
        )
        _note_progress(state, name, executor_arguments, text, staged)
        messages.append(tool_message(request_id, text))
        return staged

    def _schema(self, state: RunState, name: str) -> dict[str, Any]:
        schemas = tool_schemas("agent", {name}, bash_description=state.bash_description)
        return schemas[0]["function"]["parameters"] if schemas else {}

    async def _failure(self, run_id: str, name: str, raw_arguments: str, text: str) -> str:
        """Count a failed call; the third identical failure tells the agent to change approach."""
        state = self._state.setdefault(run_id, _fallback_state(run_id))
        key = hashlib.sha256(
            f"{name}\0{_canonical(raw_arguments)}\0{text.splitlines()[0] if text else ''}".encode()
        ).hexdigest()
        count = state.failures.get(key, 0) + 1
        state.failures[key] = count
        if count < REPEATED_FAILURES:
            return text
        await self.append(
            run_id, "loop.note", {"kind": "repeated_failure", "tool": name, "count": count}
        )
        return f"{text}\n{REPEAT_NOTE.format(count=count)}"

    async def _update_plan(
        self,
        run_id: str,
        request_id: str,
        arguments: dict[str, Any],
        messages: list[Message],
    ) -> bool:
        """Record the session plan (3.2): gateway-local, never prompts, one-line result."""
        state = self._state.setdefault(run_id, _fallback_state(run_id))
        try:
            line = state.plan.apply(arguments)
        except PlanError as exc:
            messages.append(
                tool_message(
                    request_id, format_error("tool.invalid_arguments", f"update_plan: {exc}")
                )
            )
            return False
        if self.plan_saver:
            self.plan_saver(state.plan_key, state.plan.to_dict())
        await self.append(
            run_id,
            "plan.updated",
            {"request_id": request_id, "summary": line, "plan": state.plan.to_dict()},
        )
        messages.append(tool_message(request_id, line))
        return False

    async def _web_tool(
        self,
        run_id: str,
        request: AgentRunRequest,
        request_id: str,
        name: str,
        arguments: dict[str, Any],
        messages: list[Message],
        budget: RunBudget,
        profile: BudgetProfile,
    ) -> bool:
        """``web_search`` and ``fetch_url`` (5.4): gateway-side, never the executor."""
        try:
            if name == "web_search":
                query = " ".join(str(arguments["query"]).split())
                await self.append(
                    run_id,
                    "web.requested",
                    {"request_id": request_id, "tool": name, "query": query},
                )
                results = await self.web.search(
                    self._api_keys[run_id],
                    request.model,
                    query,
                    int(arguments.get("max_results") or 5),
                    request.provider_preferences,
                )
                budget.charge(results.usage)
                text = results.model_text()
            else:
                check = check_url(arguments["url"])
                approved = None
                if check.needs_approval:
                    if not await self._url_approval(run_id, request_id, check, budget):
                        text = format_error(
                            "permission.denied", f"the user declined to fetch {check.url}"
                        )
                        messages.append(tool_message(request_id, text))
                        return False
                    approved = check.url
                await self.append(
                    run_id,
                    "web.requested",
                    {
                        "request_id": request_id,
                        "tool": name,
                        "url": check.url,
                        "approved": approved is not None,
                    },
                )
                page = await self.web.fetch(check.url, approved_url=approved)
                text = page.model_text()
        except WebToolError as exc:
            text = format_error(exc.code, str(exc))
        text = await self._redact(run_id, request_id, name, arguments, text)
        text, _ = bound_text(text, profile.result_cap(name))
        _note_progress(self._get_state(run_id, request), name, arguments, text, False)
        messages.append(tool_message(request_id, text))
        return False

    async def _url_approval(
        self, run_id: str, request_id: str, check: Any, budget: RunBudget
    ) -> bool:
        """Ask for one URL off the documentation allowlist or with a query string. No saved
        rule or approval tier answers this prompt, and the answer covers this URL only."""
        approval_id = str(uuid.uuid4())
        await self.append(
            run_id,
            "approval.required",
            {
                "approval_id": approval_id,
                "kind": "url",
                "tool": "fetch_url",
                "request_id": request_id,
                "url": check.url,
                "host": check.host,
                "approval_reason": "off the documentation allowlist"
                if not check.allowlisted
                else "the URL has a query string",
                "available_decisions": ["deny", "allow_once"],
            },
        )
        decision = await self.approvals.wait(
            run_id, approval_id, timeout=budget.remaining_wall_seconds
        )
        return decision in {PermissionDecision.ALLOW_ONCE, PermissionDecision.ALLOW_RUN}

    async def _redact(
        self, run_id: str, request_id: str, name: str, arguments: dict[str, Any], text: str
    ) -> str:
        """Replace secrets before the result enters the history (4.5)."""
        redacted, kinds = self._state.setdefault(
            run_id, _fallback_state(run_id)
        ).redactor.redact(name, arguments, text)
        if kinds:
            await self.append(
                run_id,
                "secret.redacted",
                {"request_id": request_id, "tool": name, "kinds": kinds},
            )
        return redacted

    def _executor_arguments(
        self, run_id: str, name: str, arguments: dict[str, Any], profile: BudgetProfile
    ) -> dict[str, Any]:
        """Arguments as sent to the executor: reads clamped to the result cap, and the last
        hash the agent saw filled in as ``expected_sha256`` so a stale write fails clearly."""
        arguments = dict(arguments)
        if name == "read":
            cap = profile.result_cap("read") - READ_OVERHEAD_BYTES
            requested = arguments.get("max_bytes")
            if not isinstance(requested, int) or isinstance(requested, bool) or requested > cap:
                arguments["max_bytes"] = cap
            return arguments
        seen = self._seen_files.get(run_id, {})
        if name in {"edit", "write"}:
            _fill_expected_hash(arguments, seen, tool=name)
        elif name == "apply_patch" and isinstance(arguments.get("operations"), list):
            operations = []
            for item in arguments["operations"]:
                if isinstance(item, dict):
                    item = dict(item)
                    _fill_expected_hash(item, seen, tool=str(item.get("operation") or ""))
                operations.append(item)
            arguments["operations"] = operations
        return arguments

    def _observe_files(
        self, run_id: str, name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        """Update the per-path coverage record from one executor result."""
        seen = self._seen_files.setdefault(run_id, {})
        data = result.data or {}
        if name == "bash":
            # A command may have changed anything, including on a kept non-zero exit; the
            # agent must look again rather than trust an earlier hash or coverage record.
            if not data.get("rolled_back"):
                seen.clear()
                self._reads.get(run_id, {}).clear()
            return
        if not result.ok:
            path = str(data.get("path") or "")
            if result.error_code == "staging.conflict" and path:
                seen.pop(path, None)
            elif result.error_code == "staging.shrink_warning" and path:
                _enrich_shrink_coverage(result, seen.get(path))
            return
        if name == "read" and data.get("path") and data.get("sha256"):
            path = str(data["path"])
            digest = str(data["sha256"])
            raw_size = data.get("size_bytes")
            if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                # Older/partial result shapes: a no-range, untruncated read is a whole read.
                whole = (
                    not data.get("truncated")
                    and "start_line" not in arguments
                    and "end_line" not in arguments
                    and "offset" not in arguments
                )
                seen[path] = SeenFile(digest, _int_field(data, "content_bytes"), [], whole)
                return
            size = max(0, raw_size)
            record = seen.get(path)
            if record is None or record.sha256 != digest:
                record = SeenFile(digest, size)
            else:
                record.size_bytes = size
            offset = _int_field(data, "byte_offset")
            consumed = _consumed_bytes(data, arguments, size)
            record.covered = _merge_coverage(record.covered, offset, offset + consumed, size)
            record.complete = _coverage_complete(record.covered, size)
            seen[path] = record
        elif name == "write" and data.get("path") and data.get("new_sha256"):
            path = str(data["path"])
            digest = str(data["new_sha256"])
            size = _int_field(data, "size_bytes")
            mode = str(data.get("mode") or "replace")
            old_digest = data.get("old_sha256")
            previous = seen.get(path)
            known = previous if previous and previous.sha256 == str(old_digest or "") else None
            if mode == "append" and old_digest is not None:
                covered = list(known.covered) if known else []
                appended = _int_field(data, "appended_bytes")
                old_size = max(0, size - appended)
                covered = _merge_coverage(covered, old_size, size, size)
                complete = bool(known and known.complete)
                seen[path] = SeenFile(digest, size, covered, complete)
            else:
                seen[path] = _seen_whole(digest, size)
        elif name == "edit" and data.get("path") and data.get("new_sha256"):
            path = str(data["path"])
            digest = str(data["new_sha256"])
            size = _int_field(data, "size_bytes")
            previous = seen.get(path)
            old_digest = str(data.get("old_sha256") or "")
            complete = bool(previous and previous.complete and previous.sha256 == old_digest)
            seen[path] = _seen_whole(digest, size) if complete else SeenFile(digest, size)
        elif name == "apply_patch":
            for item in data.get("operations") or ():
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                path = str(item["path"])
                if item.get("operation") == "delete":
                    seen.pop(path, None)
                elif item.get("new_sha256"):
                    digest = str(item["new_sha256"])
                    size = _int_field(item, "size_bytes")
                    if item.get("operation") == "write":
                        seen[path] = _seen_whole(digest, size)
                    else:
                        previous = seen.get(path)
                        old_digest = str(item.get("old_sha256") or "")
                        complete = bool(
                            previous and previous.complete and previous.sha256 == old_digest
                        )
                        seen[path] = (
                            _seen_whole(digest, size) if complete else SeenFile(digest, size)
                        )

    def _read_coverage(
        self, run_id: str, result: ToolResult, profile: BudgetProfile
    ) -> dict[str, Any] | None:
        """The cumulative byte-coverage view of the file a read just returned."""
        path = str((result.data or {}).get("path") or "")
        record = self._seen_files.get(run_id, {}).get(path)
        if record is None:
            return None
        return {
            "size_bytes": record.size_bytes,
            "covered_bytes": _covered_bytes(record.covered),
            "complete": record.complete,
            "ranges": len(record.covered),
            "cap_bytes": profile.result_cap("read") - READ_OVERHEAD_BYTES,
            "profile": profile.name,
        }

    async def _model_tool_result(
        self,
        run_id: str,
        request_id: str,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        profile: BudgetProfile,
        *,
        requested_max_bytes: int | None = None,
    ) -> str:
        """The bounded, plain-text model view of one result (each result has its own cap)."""
        coverage = (
            self._read_coverage(run_id, result, profile)
            if name == "read" and result.ok
            else None
        )
        text = format_tool_result(
            name,
            arguments,
            result,
            bash_chars=profile.bash_result_chars,
            coverage=coverage,
            requested_max_bytes=requested_max_bytes,
        )
        text = await self._redact(run_id, request_id, name, arguments, text)
        if name == "read" and result.ok:
            text = self._unchanged_read(run_id, request_id, arguments, result, text, coverage)
        cap = profile.result_cap(name)
        bounded, clipped = bound_text(text, cap)
        if clipped:
            await self.append(
                run_id,
                "tool.result_bounded",
                {
                    "request_id": request_id,
                    "tool": name,
                    "original_bytes": len(text.encode("utf-8")),
                    "bounded_bytes": len(bounded.encode("utf-8")),
                    "cap_bytes": cap,
                },
            )
        return bounded

    def _unchanged_read(
        self,
        run_id: str,
        request_id: str,
        arguments: dict[str, Any],
        result: ToolResult,
        text: str,
        coverage: dict[str, Any] | None = None,
    ) -> str:
        """A one-line note when the same range, unchanged, is still in the live context."""
        reads = self._reads.setdefault(run_id, {})
        key = tuple(
            arguments.get(field)
            for field in ("path", "start_line", "end_line", "offset", "max_bytes")
        )
        digest = str(result.data.get("sha256") or "")
        earlier = reads.get(key)
        if (
            earlier
            and earlier[3] is not None
            and earlier[0] == digest
            and _still_live(self._live.get(run_id, []), earlier[1], earlier[2])
        ):
            return _unchanged_note(str(arguments.get("path")), earlier[1], earlier[3])
        view = _read_view(arguments, result.data or {}, coverage)
        reads[key] = (digest, request_id, text, view)
        return text

    async def _permission(
        self,
        run_id: str,
        request: AgentRunRequest,
        tool_request: ToolRequest,
        budget: RunBudget,
        *,
        description: object = None,
    ) -> PermissionDecision:
        """Resolve rules and the approval policy, asking the user when they leave it open."""
        state = self._get_state(run_id, request)
        rules = (*self.rules_loader(request.workspace_id), *self._run_rules.get(run_id, ()))
        decision = apply_approval_policy(
            resolve_permission(tool_request, request.workspace_id, rules),
            tool_request,
            request.approval_policy,
            network=state.network,
        )
        if decision != PermissionDecision.ASK:
            return decision
        approval_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "approval_id": approval_id,
            "kind": "tool",
            "tool": tool_request.tool,
            "arguments": tool_request.arguments,
            "mutation": tool_request.tool in MUTATION_TOOLS,
            "available_decisions": ["deny", "allow_once", "allow_run", "allow_rule"],
        }
        if tool_request.tool == "bash":
            # 4.7: everything needed to judge the command, beside the command itself.
            payload.update(
                {
                    "description": " ".join(str(description).split())[:200]
                    if isinstance(description, str)
                    else None,
                    "working_directory": str(
                        tool_request.arguments.get("working_directory") or "."
                    ),
                    "timeout_seconds": tool_request.arguments.get("timeout_seconds", 60),
                    "network": "registries" if state.network else "none",
                }
            )
        await self.append(run_id, "approval.required", payload)
        decision = await self.approvals.wait(
            run_id,
            approval_id,
            tool_request,
            request.workspace_id,
            budget.remaining_wall_seconds,
        )
        if decision == PermissionDecision.ALLOW_RUN:
            rule = _run_rule_for_request(tool_request, request.workspace_id)
            self._run_rules.setdefault(run_id, []).append(rule)
        return decision

    # -- persistence ---------------------------------------------------------------------------

    async def _fail(
        self,
        run_id: str,
        code: str,
        message: str,
        budget: RunBudget,
        *,
        retryable: bool = False,
    ) -> None:
        await self.append(
            run_id,
            "run.failed",
            {
                "code": code,
                "message": message,
                "retryable": retryable,
                "budget": budget.snapshot(),
            },
        )

    def _save_snapshot(self, run_id, request, messages, visible, budget, safe_to_resume):
        if self.snapshot_saver:
            self.snapshot_saver(
                run_id, request.to_dict(), messages, visible, budget.snapshot(), safe_to_resume
            )

    def _save_turn(self, run_id, request, messages, visible, budget, safe_to_resume):
        if request.session_id and self.session_saver:
            self.session_saver(
                request.session_id, request.workspace_id, request.mode, messages, visible
            )
        self._save_snapshot(run_id, request, messages, visible, budget, safe_to_resume)

    async def _offer_publish(self, run_id: str, approval_policy: str) -> None:
        approval_id = str(uuid.uuid4())
        manifest = await self.executor.manifest(run_id, approval_id)
        if manifest.operations:
            await self.append(
                run_id,
                "checkpoint.created",
                {
                    "checkpoint_id": manifest.approval_id,
                    "publish_manifest": manifest.to_dict(),
                    "auto_publish": approval_policy == "auto",
                },
            )

    # -- repository investigation ----------------------------------------------------------

    async def _investigate_repository(
        self,
        run_id: str,
        request: AgentRunRequest,
        request_id: str,
        raw_arguments: str,
        messages: list[Message],
        parent_budget: RunBudget,
    ) -> bool:
        """Run one ``investigate_repository`` call within the per-turn call cap."""
        count = self._investigation_calls.get(run_id, 0)
        self._investigation_calls[run_id] = count + 1
        limit = request.investigation_call_budget
        if count >= limit:
            # A structured refusal, not a bare error dict: the parent branches on ``error_code``
            # and still has a summary telling it how to continue.
            content = json.dumps(
                InvestigationResult.failed(
                    INVESTIGATION_CALL_LIMIT,
                    f"Investigation call budget exhausted ({limit} calls per turn).",
                    summary=(
                        f"No investigation ran: this turn already used its {limit} "
                        "investigate_repository calls. Continue with read, grep, find, and ls "
                        "directly, or ask again next turn."
                    ),
                    extra={"used": count, "limit": limit, "remaining": 0},
                ).to_dict()
            )
        else:
            investigation = RepositoryInvestigation(
                self.executor,
                self.append,
                self._api_keys[run_id],
                run_id,
                request,
                request_id,
                parent_budget,
                redactor=self._get_state(run_id, request).redactor,
            )
            content = await investigation.run(raw_arguments)
        _note_progress(
            self._get_state(run_id, request), "investigate_repository", {}, content, False
        )
        messages.append(tool_message(request_id, content))
        return False


def tool_call_groups(
    tool_calls: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group a turn's calls for execution, preserving order.

    Runs of consecutive parallel-safe (read-only, independent) calls form one concurrent
    group; every other call (mutations, Bash, investigation, malformed calls) is its own
    serialized group, so a read issued after a write still observes that write.
    """
    groups: list[list[dict[str, Any]]] = []
    previous_parallel = False
    for raw_call in tool_calls:
        parallel = parse_tool_call(raw_call)[1] in PARALLEL_SAFE_TOOLS
        if parallel and previous_parallel:
            groups[-1].append(raw_call)
        else:
            groups.append([raw_call])
        previous_parallel = parallel
    return groups


def recent_exchanges(history: list[Message], budget_tokens: int) -> list[Message]:
    """The newest messages that fit ``budget_tokens``, cut at a valid tool-call boundary.

    A cut never starts at a tool message, so every tool result keeps its assistant call. At
    least the newest exchange is kept.
    """
    boundaries = [
        index for index, item in enumerate(history) if item.get("role") in {"user", "assistant"}
    ]
    if not boundaries:
        return []
    chosen = boundaries[-1]
    for index in reversed(boundaries):
        if estimate_message_tokens(history[index:]) > budget_tokens:
            break
        chosen = index
    return [dict(item) for item in history[chosen:]]


def _high_water(request: AgentRunRequest, profile: BudgetProfile) -> int:
    """The compaction high-water mark: every bound visible in one place.

    The smallest of the profile's working-set cap, ``HIGH_WATER_WINDOW_SHARE`` of the request's
    prompt budget, and the room the window has left after one full output turn and one large
    result (``TURN_GROWTH_RESERVE_TOKENS``). The capacity bound is what keeps a small window
    from being pushed past what it can actually hold; the floor keeps compaction meaningful on
    a tiny window.
    """
    capacity = (
        request.context_limit_tokens - profile.max_output_tokens - TURN_GROWTH_RESERVE_TOKENS
    )
    threshold = request.compaction_threshold_tokens
    high = min(
        profile.working_set_tokens if threshold is None else threshold,
        int(request.context_limit_tokens * HIGH_WATER_WINDOW_SHARE),
        capacity,
    )
    return max(MIN_HIGH_WATER_TOKENS, high)


def _mark_room(request: AgentRunRequest, profile: BudgetProfile) -> int:
    """The largest mark the request can carry: the 90% clamp and the capacity bound."""
    limit = request.context_limit_tokens
    ceiling = int(limit * HARD_CEILING_WINDOW_SHARE)
    capacity = limit - profile.max_output_tokens - TURN_GROWTH_RESERVE_TOKENS
    return min(ceiling, capacity)


def effective_high_water(
    request: AgentRunRequest,
    profile: BudgetProfile,
    messages: list[Message],
    *,
    learned: int = 0,
) -> HighWater:
    """The mark compaction actually works against.

    The request-derived mark is unchanged (``_high_water``). The effective mark also honours the
    fixed context -- the rendered leading system prefix, one exchange, and the hysteresis that
    keeps a compacted context below the trigger -- and any floor learned from a compaction that
    could not reach the low-water mark. A floor that cannot fit the prompt budget fails closed
    rather than sending a prompt the window cannot hold.
    """
    requested = _high_water(request, profile)
    limit = request.context_limit_tokens
    prefix = estimate_message_tokens(_leading_system(messages))
    floor = prefix + EXCHANGE_RESERVE_TOKENS + COMPACTION_HYSTERESIS_TOKENS
    learned = max(0, learned)
    effective = max(requested, floor, learned)
    if effective > _mark_room(request, profile):
        raise ContextFloorTooLarge(
            "the fixed session context does not fit this model's prompt budget: "
            f"prefix {prefix:,} tokens, requested mark {requested:,} tokens, effective "
            f"mark {effective:,} tokens, prompt budget {limit:,} tokens with a "
            f"{profile.max_output_tokens:,}-token output turn and a "
            f"{TURN_GROWTH_RESERVE_TOKENS:,}-token growth reserve. Choose a larger model "
            "window or a higher compaction threshold."
        )
    return HighWater(
        requested,
        floor,
        learned,
        effective,
        _mark_binding(request, profile, requested, floor, learned, effective),
    )


def _trigger_tokens(effective: int) -> int:
    """Compact one summary request below the effective mark, never below the protocol floor."""
    return max(MIN_HIGH_WATER_TOKENS, effective - COMPACTION_REQUEST_RESERVE_TOKENS)


def _request_binding(request: AgentRunRequest, profile: BudgetProfile) -> str:
    """Which bound produced the request-derived mark: ``capacity`` or the user/profile request."""
    limit = request.context_limit_tokens
    capacity = limit - profile.max_output_tokens - TURN_GROWTH_RESERVE_TOKENS
    threshold = request.compaction_threshold_tokens
    requested_bound = profile.working_set_tokens if threshold is None else threshold
    if capacity <= min(requested_bound, int(limit * HIGH_WATER_WINDOW_SHARE)):
        return "capacity"
    return "requested"


def _mark_binding(
    request: AgentRunRequest,
    profile: BudgetProfile,
    requested: int,
    floor: int,
    learned: int,
    effective: int,
) -> str:
    """Name the term that set the effective mark: ``learned``, ``prefix``, ``capacity``, or
    ``requested``. A tie prefers the request-derived mark, so the reason stays the one a reader
    would expect."""
    if effective == requested:
        return _request_binding(request, profile)
    if effective == learned:
        return "learned"
    if effective == floor:
        return "prefix"
    return "requested"


def _mark_payload(mark: HighWater) -> dict[str, Any]:
    return {
        "requested_tokens": mark.requested,
        "effective_tokens": mark.effective,
        "floor_tokens": mark.floor,
        "learned_tokens": mark.learned,
        "binding": mark.binding,
    }


def _prefix_length(messages: list[Message]) -> int:
    length = 0
    while length < len(messages) and messages[length].get("role") == "system":
        length += 1
    return length


def _leading_system(messages: list[Message]) -> list[Message]:
    return messages[: _prefix_length(messages)]


def _history_without_context(messages: list[Message]) -> list[Message]:
    """History from a list without the cache-stable layout (a fresh session, or one saved by
    an earlier gateway version): leading system messages and legacy context are dropped; any
    other system message becomes a user note, since providers may hoist system messages."""
    history = []
    for index, item in enumerate(messages):
        if item.get("role") != "system":
            history.append(dict(item))
            continue
        content = str(item.get("content") or "")
        if index < _prefix_length(messages) or content.startswith(LEGACY_CONTEXT_PREFIXES):
            continue
        history.append({"role": "user", "content": f"CONTEXT NOTE: {content}"})
    return history


def _still_live(messages: list[Message], tool_call_id: str, text: str) -> bool:
    return any(
        item.get("role") == "tool"
        and item.get("tool_call_id") == tool_call_id
        and item.get("content") == text
        for item in messages
    )


def _fill_expected_hash(
    arguments: dict[str, Any],
    seen: dict[str, SeenFile],
    *,
    tool: str,
) -> None:
    """Fill ``expected_sha256`` from the hash the agent last saw for the path.

    A replace destroys the whole file, so it is confirmed only after the agent covered every
    byte (in one read or several contiguous chunks). An append never removes unseen content,
    and an exact edit is itself a confirmation, so either needs only a known hash.
    """
    if arguments.get("expected_sha256") is not None:
        return
    path = arguments.get("path")
    known = seen.get(path) if isinstance(path, str) else None
    if known is None:
        return
    mode = arguments.get("mode", "replace") if tool == "write" else None
    if tool == "write" and mode != "append" and not known.complete:
        return
    arguments["expected_sha256"] = known.sha256


def _read_dedup_key(arguments: dict[str, Any], cap: int) -> tuple[Any, ...]:
    """Canonical identity of a read: an omitted or larger max_bytes is the effective cap."""
    max_bytes = arguments.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        max_bytes = cap
    return (
        arguments.get("path"),
        arguments.get("start_line"),
        arguments.get("end_line"),
        arguments.get("offset"),
        min(max_bytes, cap),
    )


def _int_field(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _consumed_bytes(data: dict[str, Any], arguments: dict[str, Any], size: int) -> int:
    """The exact bytes a read consumed, tolerant of older result shapes in tests."""
    value = data.get("consumed_bytes")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    content = data.get("content_bytes")
    if isinstance(content, int) and not isinstance(content, bool):
        return max(0, content)
    if (
        not data.get("truncated")
        and "start_line" not in arguments
        and "offset" not in arguments
    ):
        return size
    return 0


def _merge_coverage(
    covered: list[tuple[int, int]], start: int, end: int, size: int
) -> list[tuple[int, int]]:
    """Clip one byte range into the file and merge it with adjacent ranges.

    Line-range reads stop before a trailing newline and the next range starts after it, so a
    one-byte gap is contiguous coverage, not unread content.
    """
    if size <= 0:
        return []
    start = max(0, min(start, size))
    end = max(0, min(end, size))
    ranges = list(covered)
    if end > start:
        ranges.append((start, end))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for range_start, range_end in ranges:
        if merged and range_start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, range_end))
        else:
            merged.append((range_start, range_end))
    return merged


def _covered_bytes(covered: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in covered)


def _coverage_complete(covered: list[tuple[int, int]], size: int) -> bool:
    if size <= 0:
        return True
    if not covered or covered[0][0] != 0:
        return False
    return _covered_bytes(covered) >= size


def _seen_whole(digest: str, size: int) -> SeenFile:
    return SeenFile(digest, size, [(0, size)] if size else [], True)


def _enrich_shrink_coverage(result: ToolResult, record: SeenFile | None) -> None:
    """Give a refused shrink the gateway's cumulative coverage, which the executor cannot know."""
    if record is None or not isinstance(result.data, dict):
        return
    covered = _covered_bytes(record.covered)
    result.data.update(
        {
            "size_bytes": record.size_bytes,
            "covered_bytes": covered,
            "coverage_ranges": len(record.covered),
            "remaining_bytes": max(0, record.size_bytes - covered),
            "complete": record.complete,
        }
    )


def _coverage_percent(covered: int, size: int) -> int:
    if size <= 0:
        return 100
    return min(100, (covered * 100 + size // 2) // size)


def _read_view(
    arguments: dict[str, Any],
    data: dict[str, Any],
    coverage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What an earlier read result covered, for the unchanged re-read note."""
    if coverage is None:
        return None
    if "offset" in arguments:
        kind = "bytes"
        start = _int_field(data, "byte_offset")
        end = start + _int_field(data, "consumed_bytes")
        total = _int_field(data, "size_bytes")
    else:
        kind = "lines"
        start = _int_field(data, "start_line") or 1
        end = _int_field(data, "end_line") or start
        total = _int_field(data, "line_count") or end
    next_hint = None
    if data.get("next_start_line"):
        next_hint = f"start_line={data['next_start_line']}"
    elif data.get("next_offset") is not None:
        next_hint = f"offset={data['next_offset']}"
    elif not coverage.get("complete") and kind == "lines" and end < total:
        next_hint = f"start_line={end + 1}"
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "total": total,
        "complete": bool(coverage.get("complete")),
        "covered_bytes": int(coverage.get("covered_bytes") or 0),
        "size_bytes": int(coverage.get("size_bytes") or 0),
        "next": next_hint,
    }


def _unchanged_note(path: str, tool_call_id: str, view: dict[str, Any] | None) -> str:
    """Never a bare pointer: say whether the earlier result was the whole file."""
    prefix = f"read {path}: unchanged since tool call {tool_call_id};"
    if not view:
        return f"{prefix} use that result."
    if view.get("complete"):
        return f"{prefix} that result covered the whole file (full coverage)."
    share = _coverage_percent(
        int(view.get("covered_bytes") or 0), int(view.get("size_bytes") or 0)
    )
    note = (
        f"{prefix} that result covered {view.get('kind')} {view.get('start')}–"
        f"{view.get('end')} of {view.get('total')} ({share}%), not the whole file."
    )
    if view.get("next"):
        note += f" Continue with {view['next']}, or use the earlier result for that range."
    else:
        note += " Use the earlier result for that range."
    return note


def _fallback_state(run_id: str) -> RunState:
    return RunState(frozenset(), False, False, Redactor(), PlanState(), f"run:{run_id}", "")


def _fill_missing_results(messages: list[Message]) -> None:
    """Give every tool call of the newest assistant message a result, so the history stays
    valid when a turn is cut short."""
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if item.get("role") != "assistant":
            continue
        answered = {
            str(entry.get("tool_call_id"))
            for entry in messages[index + 1 :]
            if entry.get("role") == "tool"
        }
        for raw_call in item.get("tool_calls") or ():
            call_id = parse_tool_call(raw_call)[0] if isinstance(raw_call, dict) else ""
            if call_id and call_id not in answered:
                messages.append(tool_message(call_id, NOT_EXECUTED))
        return


def _note_progress(
    state: RunState, name: str, arguments: dict[str, Any], text: str, staged: bool
) -> None:
    """A turn makes progress with a staged change or a tool result not seen before."""
    if name == "update_plan":
        return
    digest = hashlib.sha256(
        json.dumps([name, arguments, text], sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if staged or digest not in state.seen_results:
        state.progressed = True
    state.seen_results.add(digest)


def _canonical(raw_arguments: str) -> str:
    try:
        return json.dumps(json.loads(raw_arguments), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return raw_arguments


def _declared_checks(config: dict[str, Any]) -> dict[str, str]:
    """Check commands declared in the confirmed workspace policy (3.4, 4.6)."""
    policy = config.get("policy") if isinstance(config, dict) else None
    checks = policy.get("checks") if isinstance(policy, dict) else None
    if not isinstance(checks, dict):
        return {}
    declared: dict[str, str] = {}
    for name, value in checks.items():
        command = value.get("command") if isinstance(value, dict) else value
        if isinstance(command, str) and command.strip():
            declared[str(name)] = " ".join(command.split())[:200]
    return declared


def _clamp_bash_timeout(arguments: dict[str, Any], budget: RunBudget) -> None:
    """Never let one command outlive the run's remaining wall time."""
    remaining = budget.remaining_wall_seconds
    if remaining < 1:
        budget.check()
        raise RuntimeError("wall-time budget exhausted before Bash could start")
    requested = arguments.get("timeout_seconds", 60)
    if isinstance(requested, int) and not isinstance(requested, bool):
        arguments["timeout_seconds"] = min(requested, max(1, int(remaining)))


def _run_rule_for_request(request: ToolRequest, workspace_id: str) -> PermissionRule:
    """The per-run rule an "allow for this run" approval adds; Bash scopes to its command."""
    executable = None
    args: tuple[str, ...] = ()
    if request.tool == "bash":
        executable, args = bash_command_tokens(str(request.arguments.get("command") or ""))
    return PermissionRule(
        f"run-{request.run_id}-{request.request_id}",
        PermissionDecision.ALLOW_RUN,
        workspace_id,
        request.mode,
        request.tool,
        rule_scope(request),
        executable,
        args,
    )
