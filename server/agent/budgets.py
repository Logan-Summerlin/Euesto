from __future__ import annotations

import time
from dataclasses import dataclass

from shared.coercion import optional_float, optional_int


@dataclass(slots=True)
class RunBudget:
    max_iterations: int
    max_wall_seconds: int
    max_cost: float
    max_tool_calls: int = 100
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
        total = max(0, reported_total if reported_total is not None else prompt + completion)
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
            raise RuntimeError("iteration budget exhausted")
        if self.tool_calls > self.max_tool_calls:
            raise RuntimeError("tool-call budget exhausted")
        if time.monotonic() - self.started > self.max_wall_seconds:
            raise RuntimeError("wall-time budget exhausted")
        if self.cost > self.max_cost:
            raise RuntimeError("cost budget exhausted")

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def snapshot(self) -> dict[str, float | int]:
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": self.elapsed_seconds,
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
