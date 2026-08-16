from __future__ import annotations

import time
from dataclasses import dataclass

from shared.coercion import optional_float, optional_int


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    name: str
    max_iterations: int
    max_tool_calls: int
    max_wall_seconds: int
    max_cost: float


STANDARD_CODING_PROFILE = BudgetProfile(
    "coding",
    max_iterations=200,
    max_tool_calls=300,
    max_wall_seconds=1_800,
    max_cost=2.0,
)
EXTENDED_CODING_PROFILE = BudgetProfile(
    "extended-coding",
    max_iterations=400,
    max_tool_calls=600,
    max_wall_seconds=3_600,
    max_cost=4.0,
)
LARGE_CODING_PROFILE = BudgetProfile(
    "large-coding",
    max_iterations=600,
    max_tool_calls=900,
    max_wall_seconds=5_400,
    max_cost=8.0,
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
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    @classmethod
    def from_profile(cls, name: str) -> "RunBudget":
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

    def add_cost(self, value: float) -> None:
        self.cost += max(0, value)
        self.check()

    def add_usage(self, usage: dict[str, object]) -> None:
        """Add one provider call to the cumulative agent-turn usage."""
        prompt = max(
            0,
            optional_int(usage.get("prompt_tokens", usage.get("input_tokens"))) or 0,
        )
        completion = max(
            0,
            optional_int(usage.get("completion_tokens", usage.get("output_tokens"))) or 0,
        )
        cached = max(0, optional_int(usage.get("cached_tokens")) or 0)
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
        self.reasoning_tokens += reasoning
        self.total_tokens += total
        self.cost += cost
        self.check()

    def usage(self) -> dict[str, int | float]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
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
            raise BudgetExceededError(
                "wall-time", elapsed, self.max_wall_seconds, "seconds"
            )
        if self.cost > self.max_cost:
            raise BudgetExceededError("cost", self.cost, self.max_cost, "USD")

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
        self.reasoning_tokens = max(0, int(state.get("reasoning_tokens") or 0))
        self.total_tokens = max(0, int(state.get("total_tokens") or 0))
        elapsed = max(0.0, float(state.get("elapsed_seconds") or 0))
        self.started = time.monotonic() - elapsed
        self.check()
