from __future__ import annotations

import time
from dataclasses import dataclass

from shared.coercion import optional_float, optional_int


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    """Resource budgets plus the model-facing context and result bounds of one profile.

    ``working_set_tokens`` is the compaction high-water cap: the high water is the smallest of
    it, 75% of the request's prompt budget, and what the prompt budget can hold once one
    output turn and one large result are subtracted. Result caps bound each model-facing tool result; none may
    exceed 40% of the working set in tokens (10% of it in bytes, at four bytes per token).
    ``max_output_tokens`` caps one model turn.
    """

    name: str
    max_iterations: int
    max_tool_calls: int
    max_wall_seconds: int
    max_cost: float
    working_set_tokens: int = 96_000
    result_bytes: int = 24_000
    read_result_bytes: int = 32_000
    bash_result_chars: int = 8_000
    max_output_tokens: int = 16_000
    # One streamed model turn: the longest wait between chunks, and the turn's ceiling (3.1).
    turn_idle_seconds: int = 120
    turn_ceiling_seconds: int = 600

    def result_cap(self, tool: str) -> int:
        """Model-facing byte cap for one result of ``tool``."""
        cap = self.read_result_bytes if tool == "read" else self.result_bytes
        return min(cap, self.working_set_tokens * 4 // 10)


STANDARD_CODING_PROFILE = BudgetProfile(
    "coding",
    max_iterations=600,
    max_tool_calls=900,
    max_wall_seconds=1_800,
    max_cost=2.0,
    working_set_tokens=150_000,
)
EXTENDED_CODING_PROFILE = BudgetProfile(
    "extended-coding",
    max_iterations=1_200,
    max_tool_calls=1_800,
    max_wall_seconds=3_600,
    max_cost=4.0,
    working_set_tokens=240_000,
    result_bytes=32_000,
    read_result_bytes=48_000,
    bash_result_chars=12_000,
)
LARGE_CODING_PROFILE = BudgetProfile(
    "large-coding",
    max_iterations=1_800,
    max_tool_calls=2_700,
    max_wall_seconds=5_400,
    max_cost=8.0,
    working_set_tokens=400_000,
    result_bytes=48_000,
    read_result_bytes=64_000,
    bash_result_chars=16_000,
)

BUDGET_PROFILES = {
    profile.name: profile
    for profile in (
        STANDARD_CODING_PROFILE,
        EXTENDED_CODING_PROFILE,
        LARGE_CODING_PROFILE,
    )
}


def resolve_budget_profile(name: str) -> BudgetProfile:
    try:
        return BUDGET_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown agent budget profile: {name}") from exc


def requires_budget_approval(profile: BudgetProfile) -> bool:
    """Return whether the profile exceeds the standard profile by more than 2x.

    Iteration count is intentionally not part of the approval threshold: the
    approval rule is specifically about tool calls, wall time, and cost.
    """
    standard = STANDARD_CODING_PROFILE
    return (
        profile.max_tool_calls > standard.max_tool_calls * 2
        or profile.max_wall_seconds > standard.max_wall_seconds * 2
        or profile.max_cost > standard.max_cost * 2
    )


# Wind-down (3.3): a note at this share of any budget, and a reserve for the summary turn.
WIND_DOWN_WARNING_SHARE = 0.85
WIND_DOWN_SHARE = 0.05
WIND_DOWN_MAX_RESERVE_SECONDS = 60.0
WIND_DOWN_RESERVE_ITERATIONS = 1


class BudgetExceededError(RuntimeError):
    def __init__(self, budget: str, used: float, limit: float, unit: str):
        self.budget = budget
        self.used = used
        self.limit = limit
        self.unit = unit
        super().__init__(
            f"{budget} budget exhausted: {used:g}/{limit:g} {unit} used; 0 remaining."
        )


@dataclass(slots=True)
class RunBudget:
    max_iterations: int
    max_wall_seconds: int
    max_cost: float
    max_tool_calls: int = 100
    profile_name: str = "custom"
    started: float = 0
    iterations: int = 0
    tool_calls: int = 0
    cost: float = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    turns: int = 0

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    @classmethod
    def from_profile(cls, name: str) -> RunBudget:
        profile = resolve_budget_profile(name)
        return cls(
            profile.max_iterations,
            profile.max_wall_seconds,
            profile.max_cost,
            profile.max_tool_calls,
            profile.name,
        )

    def consume_iteration(self) -> None:
        self.iterations += 1
        self.check()

    def add_usage(self, usage: dict[str, object], *, check: bool = True) -> None:
        """Add one provider call to the cumulative agent-turn usage.

        ``check=False`` records a response that was already paid for without discarding it;
        the caller then enforces the limits before its next step.
        """
        prompt = max(
            0,
            optional_int(usage.get("prompt_tokens", usage.get("input_tokens"))) or 0,
        )
        completion = max(
            0,
            optional_int(usage.get("completion_tokens", usage.get("output_tokens"))) or 0,
        )
        cached = max(0, optional_int(usage.get("cached_tokens")) or 0)
        cache_write = max(0, optional_int(usage.get("cache_write_tokens")) or 0)
        reasoning = max(0, optional_int(usage.get("reasoning_tokens")) or 0)
        reported_total = optional_int(usage.get("total_tokens"))
        total = max(
            0,
            reported_total if reported_total is not None else prompt + completion,
        )
        cost = max(0.0, optional_float(usage.get("cost")) or 0.0)

        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += cached
        self.cache_write_tokens += cache_write
        self.turns += 1
        self.reasoning_tokens += reasoning
        self.total_tokens += total
        self.cost += cost
        if check:
            self.check()

    def charge(self, usage: dict[str, object]) -> None:
        """Charge a gateway tool's provider call (``web_search``) without counting a turn."""
        self.cost += max(0.0, optional_float(usage.get("cost")) or 0.0)

    def usage(self) -> dict[str, int | float]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "turns": self.turns,
            **usage_ratios(self.prompt_tokens, self.cached_tokens, self.turns),
        }

    def consume_tool_call(self) -> None:
        self.tool_calls += 1
        self.check()

    def check(self) -> None:
        if self.iterations > self.max_iterations:
            raise BudgetExceededError(
                "iteration", self.iterations, self.max_iterations, "iterations"
            )
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceededError(
                "tool-call", self.tool_calls, self.max_tool_calls, "tool calls"
            )
        elapsed = self.elapsed_seconds
        if elapsed > self.max_wall_seconds:
            raise BudgetExceededError("wall-time", elapsed, self.max_wall_seconds, "seconds")
        if self.cost > self.max_cost:
            raise BudgetExceededError("cost", self.cost, self.max_cost, "USD")

    def usage_fraction(self) -> tuple[str, float]:
        """The budget closest to its limit and the share of it used (0.0 to 1.0+)."""
        shares = {
            "iteration": self.iterations / self.max_iterations if self.max_iterations else 1.0,
            "tool-call": self.tool_calls / self.max_tool_calls if self.max_tool_calls else 1.0,
            "wall-time": self.elapsed_seconds / self.max_wall_seconds
            if self.max_wall_seconds
            else 1.0,
            "cost": self.cost / self.max_cost if self.max_cost else 1.0,
        }
        name = max(shares, key=lambda key: shares[key])
        return name, shares[name]

    @property
    def reserve_seconds(self) -> float:
        """Wall time kept back for the wind-down summary turn (3.3)."""
        return min(WIND_DOWN_MAX_RESERVE_SECONDS, self.max_wall_seconds * WIND_DOWN_SHARE)

    @property
    def reserve_cost(self) -> float:
        """Cost kept back for the wind-down summary turn (3.3)."""
        return self.max_cost * WIND_DOWN_SHARE

    def wind_down_reason(self, *, between_turns: bool = True) -> str | None:
        """The budget that leaves only the wind-down reserve, or ``None`` to keep going.

        The loop stops for a summary while one iteration, the reserved wall time, and the
        reserved cost remain, so the summary is funded without breaking a hard limit. Within
        a turn (``between_turns=False``) the iteration is already spent and is not checked.
        """
        if between_turns and self.remaining_iterations <= WIND_DOWN_RESERVE_ITERATIONS:
            return "iteration"
        if self.remaining_tool_calls <= 0:
            return "tool-call"
        if self.remaining_wall_seconds <= self.reserve_seconds:
            return "wall-time"
        if self.remaining_cost <= self.reserve_cost:
            return "cost"
        return None

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    @property
    def remaining_iterations(self) -> int:
        return max(0, self.max_iterations - self.iterations)

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls)

    @property
    def remaining_wall_seconds(self) -> float:
        return max(0.0, self.max_wall_seconds - self.elapsed_seconds)

    @property
    def remaining_cost(self) -> float:
        return max(0.0, self.max_cost - self.cost)

    def snapshot(self) -> dict[str, float | int | str | dict[str, float | int]]:
        return {
            "profile": self.profile_name,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_iterations": self.remaining_iterations,
            "remaining_tool_calls": self.remaining_tool_calls,
            "remaining_wall_seconds": self.remaining_wall_seconds,
            "remaining_cost": self.remaining_cost,
            "limits": {
                "max_iterations": self.max_iterations,
                "max_tool_calls": self.max_tool_calls,
                "max_wall_seconds": self.max_wall_seconds,
                "max_cost": self.max_cost,
            },
            **self.usage(),
        }

    def restore(self, state: dict[str, object]) -> None:
        self.iterations = max(0, int(state.get("iterations") or 0))
        self.tool_calls = max(0, int(state.get("tool_calls") or 0))
        self.cost = max(0.0, float(state.get("cost") or 0))
        self.prompt_tokens = max(0, int(state.get("prompt_tokens") or 0))
        self.completion_tokens = max(0, int(state.get("completion_tokens") or 0))
        self.cached_tokens = max(0, int(state.get("cached_tokens") or 0))
        self.cache_write_tokens = max(0, int(state.get("cache_write_tokens") or 0))
        self.turns = max(0, int(state.get("turns") or 0))
        self.reasoning_tokens = max(0, int(state.get("reasoning_tokens") or 0))
        self.total_tokens = max(0, int(state.get("total_tokens") or 0))
        elapsed = max(0.0, float(state.get("elapsed_seconds") or 0))
        self.started = time.monotonic() - elapsed
        self.check()


def usage_ratios(prompt_tokens: int, cached_tokens: int, turns: int) -> dict[str, float]:
    """Cache hit rate (cached / prompt tokens) and average new prompt tokens per turn."""
    prompt = max(0, prompt_tokens)
    cached = min(prompt, max(0, cached_tokens))
    return {
        "cache_hit_rate": cached / prompt if prompt else 0.0,
        "average_new_tokens_per_turn": (prompt - cached) / turns if turns else 0.0,
    }
