from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from server.executor import ExecutorClient
from server.extensions.skills import render_skill_context
from server.openrouter.agent import AgentTurn, agent_turn
from server.openrouter.errors import ProviderError
from shared.permissions import (
    PermissionDecision,
    PermissionRule,
    apply_approval_policy,
    bash_command_tokens,
    resolve_permission,
    rule_scope,
)
from shared.requests import AgentRunRequest
from shared.tools import (
    MUTATION_TOOLS,
    PARALLEL_SAFE_TOOLS,
    PLAN_TOOLS,
    TOOL_NAMES,
    ToolRequest,
    ToolResult,
)

from .approvals import ApprovalCoordinator, ApprovalTimeoutError
from .budgets import (
    BudgetExceededError,
    BudgetProfile,
    RunBudget,
    requires_budget_approval,
    resolve_budget_profile,
)
from .context import compact_agent_context, estimate_message_tokens
from .investigation import RepositoryInvestigation
from .prompts import PROMPT_VERSION, agent_prompt
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
from .tool_calls import Message, parse_arguments, parse_tool_call, tool_message
from .usage import MAX_TRACKED_SESSIONS, TurnRecord, UsageLedger, classify_cache_miss

Append = Callable[[str, str, dict[str, Any]], Awaitable[Any]]

MAX_PARALLEL_TOOL_CALLS = 8
# Parent-loop provider retries: the investigation loop's policy, with jitter.
PROVIDER_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_MULTIPLIER = 3.0
# Compaction: the high-water mark is the smaller of this share of the window and the profile's
# working-set cap; compaction returns to a third of it.
HIGH_WATER_WINDOW_SHARE = 0.6
LOW_WATER_SHARE = 1 / 3
HARD_CEILING_WINDOW_SHARE = 0.9
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
    "Objective and constraints\nDecisions made\nFiles touched (from status)\n"
    "Commands run and their results\nPlan state and open questions\nNext steps"
)
LENGTH_NOTE = (
    "RUN NOTE: your last reply hit the output limit and was cut off. Write large files in "
    "parts: `write` the first part, then extend it with `apply_patch`."
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
    ):
        self.executor = executor
        self.approvals = approvals
        self.append = append
        self.rules_loader = rules_loader or (lambda _workspace: ())
        self.snapshot_saver = snapshot_saver
        self.session_saver = session_saver
        self.pause_requested = pause_requested or (lambda _run_id: False)
        self.active_request: dict[str, str] = {}
        self._run_rules: dict[str, list[PermissionRule]] = {}
        self._approved_budget_sessions: set[str] = set()
        self._investigation_calls: dict[str, int] = {}
        self._api_keys: dict[str, str] = {}
        self._steering: dict[str, deque[str]] = {}
        # Per run: the last hash the agent saw for each path, and whether it saw the whole file.
        self._seen_hashes: dict[str, dict[str, tuple[str, bool]]] = {}
        # Per run: (read key) -> (hash, tool_call_id, model-facing text) of earlier reads.
        self._reads: dict[str, dict[tuple[Any, ...], tuple[str, str, str]]] = {}
        self._live: dict[str, list[Message]] = {}
        self._expected_miss: dict[str, str] = {}
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
    ) -> None:
        profile = resolve_budget_profile(request.budget_profile)
        budget = RunBudget.from_profile(profile.name)
        if budget_state:
            budget.restore(budget_state)
        messages = [dict(x) for x in (initial_messages or request.messages)]
        visible = [dict(x) for x in (visible_messages or request.messages)]
        self._api_keys[run_id] = api_key
        self._steering[run_id] = deque()
        self._seen_hashes[run_id] = {}
        self._reads[run_id] = {}
        try:
            await self._approve_budget_profile(run_id, request, profile, budget)
            status = await self.executor.status()
            if status.get("workspace_id") != request.workspace_id:
                raise RuntimeError("executor workspace identity mismatch")
            await self.append(
                run_id,
                "run.resumed" if resumed else "run.started",
                {
                    "mode": request.mode,
                    "workspace_id": request.workspace_id,
                    "approval_policy": request.approval_policy,
                    "prompt_version": PROMPT_VERSION,
                    "budget": budget.snapshot(),
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
            self._save_snapshot(run_id, request, messages, visible, budget, True)
            await self._agent_loop(run_id, request, api_key, messages, visible, budget, profile)
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
            self._seen_hashes.pop(run_id, None)
            self._reads.pop(run_id, None)
            self._live.pop(run_id, None)
            self._expected_miss.pop(run_id, None)

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
        available = PLAN_TOOLS if request.mode == "plan" else TOOL_NAMES
        instructions = request.workspace_config.get("instructions")
        sources = {
            "facts": render_executor_facts(
                status,
                request.mode,
                read_bytes=profile.result_cap("read") - READ_OVERHEAD_BYTES,
                bash_chars=profile.bash_result_chars,
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
        self._live[run_id] = messages
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
            budget.consume_iteration()
            try:
                turn = await self._model_turn(
                    run_id,
                    request,
                    api_key,
                    messages,
                    max_tokens=profile.max_output_tokens,
                    reasoning_effort=request.reasoning_effort,
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
            budget.check()
            messages.append(turn.message)
            content = str(turn.content or "")
            transcript = [*visible, {"role": "assistant", "content": content}]
            if not turn.tool_calls:
                if content:
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
                    },
                )
                return
            if content.strip():
                await self.append(run_id, "model.narration", {"text": content[:4_000]})
            # The investigation call cap is per turn: each model turn starts with a fresh allowance.
            self._investigation_calls.pop(run_id, None)
            for group in tool_call_groups(turn.tool_calls):
                for _ in group:
                    budget.consume_tool_call()
                    self._session_ledger(request, run_id).tool_calls += 1
                if len(group) == 1:
                    mutated = await self._execute_tool_call(
                        run_id, request, group[0], messages, budget, profile
                    )
                    run_mutated = mutated or run_mutated
                    continue
                for buffer in await self._execute_parallel_group(
                    run_id, request, group, budget, profile
                ):
                    messages.extend(buffer)
            if turn.finish_reason == "length":
                messages.append({"role": "user", "content": LENGTH_NOTE})
            await self.append(
                run_id, "usage.updated", {"budget": budget.snapshot(), **budget.usage()}
            )
            self._save_turn(run_id, request, messages, transcript, budget, True)

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
    ) -> AgentTurn:
        """One provider call with jittered retries; a retry resends the identical payload."""
        delay = RETRY_BASE_DELAY
        for attempt in range(1, PROVIDER_ATTEMPTS + 1):
            try:
                return await agent_turn(
                    request.model,
                    messages,
                    api_key,
                    request.mode,
                    request.provider_preferences,
                    allowed_tools,
                    session_id=request.session_id or run_id,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            except ProviderError as exc:
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

    async def _maybe_compact(
        self,
        run_id: str,
        request: AgentRunRequest,
        api_key: str,
        messages: list[Message],
        budget: RunBudget,
        profile: BudgetProfile,
    ) -> list[Message]:
        """Compact only at the high-water mark, back down to the low-water mark."""
        high = _high_water(request, profile)
        before = estimate_message_tokens(messages)
        if before < high:
            return messages
        low = max(1_000, int(high * LOW_WATER_SHARE))
        prefix_length = _prefix_length(messages)
        prefix, history = messages[:prefix_length], messages[prefix_length:]
        summary_usage: dict[str, Any] = {}
        method = "summary"
        try:
            turn = await self._model_turn(
                run_id,
                request,
                api_key,
                [*messages, {"role": "user", "content": HANDOFF_REQUEST}],
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
                f"compacted away)\n{handoff[: SUMMARY_MAX_TOKENS * 6]}",
            }
            room = max(500, low - estimate_message_tokens([*prefix, handoff_message]))
            compacted = [*prefix, handoff_message, *recent_exchanges(history, room)]
        except (ProviderError, ValueError):
            method = "deterministic"
            body = compact_agent_context(
                history, max(500, low - estimate_message_tokens(prefix)), summary_role="user"
            )
            compacted = [*prefix, *body]
        window = request.context_limit_tokens
        if estimate_message_tokens(compacted) > int(window * HARD_CEILING_WINDOW_SHARE):
            body = compact_agent_context(
                compacted[prefix_length:],
                max(500, int(window * 0.8) - estimate_message_tokens(prefix)),
                summary_role="user",
            )
            compacted = [*prefix, *body]
        self._expected_miss[run_id] = "compaction"
        await self.append(
            run_id,
            "context.compacted",
            {
                "method": method,
                "tokens_before": before,
                "tokens_after": estimate_message_tokens(compacted),
                "high_water_tokens": high,
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
    ) -> list[list[Message]]:
        """Run independent read-only calls concurrently; each fills its own message buffer so
        results can be appended in the original call order."""
        limiter = asyncio.Semaphore(MAX_PARALLEL_TOOL_CALLS)
        buffers: list[list[Message]] = [[] for _ in group]

        async def run_one(raw_call: dict[str, Any], buffer: list[Message]) -> bool:
            async with limiter:
                return await self._execute_tool_call(
                    run_id, request, raw_call, buffer, budget, profile, track_active=False
                )

        await asyncio.gather(
            *(
                run_one(raw_call, buffer)
                for raw_call, buffer in zip(group, buffers, strict=True)
            )
        )
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
    ) -> bool:
        """Run one model tool call and append its result; return whether it staged changes."""
        request_id, name, raw_arguments = parse_tool_call(raw_call)
        if name == "investigate_repository":
            return await self._investigate_repository(
                run_id, request, request_id, raw_arguments, messages, budget
            )
        try:
            arguments = parse_arguments(raw_arguments)
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
        decision = await self._permission(run_id, request, tool_request, budget)
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
        self._observe_hashes(run_id, name, executor_arguments, result)
        text = await self._model_tool_result(
            run_id, request_id, name, executor_arguments, result, profile
        )
        messages.append(tool_message(request_id, text))
        if name not in MUTATION_TOOLS or not result.ok:
            return False
        workspace_status = result.data.get("workspace_status")
        return isinstance(workspace_status, dict) and bool(workspace_status.get("staged"))

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
        seen = self._seen_hashes.get(run_id, {})
        if name in {"edit", "write"}:
            _fill_expected_hash(arguments, seen, whole_file_required=name == "write")
        elif name == "apply_patch" and isinstance(arguments.get("operations"), list):
            operations = []
            for item in arguments["operations"]:
                if isinstance(item, dict):
                    item = dict(item)
                    _fill_expected_hash(
                        item, seen, whole_file_required=item.get("operation") == "write"
                    )
                operations.append(item)
            arguments["operations"] = operations
        return arguments

    def _observe_hashes(
        self, run_id: str, name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        seen = self._seen_hashes.setdefault(run_id, {})
        data = result.data or {}
        if not result.ok:
            if result.error_code == "staging.conflict" and data.get("path"):
                seen.pop(str(data["path"]), None)
            return
        if name == "read" and data.get("path") and data.get("sha256"):
            whole = (
                not data.get("truncated")
                and int(data.get("byte_offset") or 0) == 0
                and "start_line" not in arguments
                and "end_line" not in arguments
            )
            previous = seen.get(str(data["path"]))
            keep_whole = bool(previous and previous[0] == data["sha256"] and previous[1])
            seen[str(data["path"])] = (str(data["sha256"]), whole or keep_whole)
        elif name in {"write", "edit"} and data.get("path") and data.get("new_sha256"):
            previous = seen.get(str(data["path"]))
            whole = name == "write" or bool(previous and previous[1])
            seen[str(data["path"])] = (str(data["new_sha256"]), whole)
        elif name == "apply_patch":
            for item in data.get("operations") or ():
                if not isinstance(item, dict) or not item.get("path"):
                    continue
                path = str(item["path"])
                if item.get("operation") == "delete":
                    seen.pop(path, None)
                elif item.get("new_sha256"):
                    previous = seen.get(path)
                    whole = item.get("operation") == "write" or bool(previous and previous[1])
                    seen[path] = (str(item["new_sha256"]), whole)
        elif name == "bash" and not data.get("rolled_back"):
            # A command may have changed anything; the agent must look again.
            seen.clear()
            self._reads.get(run_id, {}).clear()

    async def _model_tool_result(
        self,
        run_id: str,
        request_id: str,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        profile: BudgetProfile,
    ) -> str:
        """The bounded, plain-text model view of one result (each result has its own cap)."""
        text = format_tool_result(name, arguments, result, bash_chars=profile.bash_result_chars)
        if name == "read" and result.ok:
            text = self._unchanged_read(run_id, request_id, arguments, result, text)
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
            and earlier[0] == digest
            and _still_live(self._live.get(run_id, []), earlier[1], earlier[2])
        ):
            return (
                f"read {arguments.get('path')}: unchanged since your earlier read "
                f"(tool call {earlier[1]}); use that result."
            )
        reads[key] = (digest, request_id, text)
        return text

    async def _permission(
        self,
        run_id: str,
        request: AgentRunRequest,
        tool_request: ToolRequest,
        budget: RunBudget,
    ) -> PermissionDecision:
        """Resolve rules and the approval policy, asking the user when they leave it open."""
        rules = (*self.rules_loader(request.workspace_id), *self._run_rules.get(run_id, ()))
        decision = apply_approval_policy(
            resolve_permission(tool_request, request.workspace_id, rules),
            tool_request,
            request.approval_policy,
        )
        if decision != PermissionDecision.ASK:
            return decision
        approval_id = str(uuid.uuid4())
        await self.append(
            run_id,
            "approval.required",
            {
                "approval_id": approval_id,
                "kind": "tool",
                "tool": tool_request.tool,
                "arguments": tool_request.arguments,
                "mutation": tool_request.tool in MUTATION_TOOLS,
                "available_decisions": ["deny", "allow_once", "allow_run", "allow_rule"],
            },
        )
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
            content = json.dumps(
                {
                    "error": f"Investigation call budget exhausted ({limit} calls per turn).",
                    "remaining": 0,
                    "fallback": "Continue with the repository tools directly.",
                }
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
            )
            content = await investigation.run(raw_arguments)
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
    return max(
        4_000,
        min(
            int(request.context_limit_tokens * HIGH_WATER_WINDOW_SHARE),
            profile.working_set_tokens,
        ),
    )


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
    seen: dict[str, tuple[str, bool]],
    *,
    whole_file_required: bool,
) -> None:
    """Fill ``expected_sha256`` from the last hash the agent saw for the path.

    A ``write`` may replace a whole file, and a matching hash downgrades the shrink guard to a
    warning, so it is filled only after the agent saw the entire file (a full read or its own
    write).
    """
    if arguments.get("expected_sha256") is not None:
        return
    path = arguments.get("path")
    known = seen.get(path) if isinstance(path, str) else None
    if known is None or (whole_file_required and not known[1]):
        return
    arguments["expected_sha256"] = known[0]


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
