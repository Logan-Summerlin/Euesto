"""The live run meter: cost, cache hit rate, context against the working-set cap, tool calls,
and time, folded from the gateway's run events (plan items 0.8 and 1.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.coercion import optional_float, optional_int


@dataclass(slots=True)
class RunMeter:
    cost: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    context_tokens: int | None = None
    working_set_tokens: int | None = None
    tool_calls: int = 0
    elapsed_seconds: float = 0.0
    retries: int = 0
    compactions: int = 0

    @property
    def cache_hit_rate(self) -> float | None:
        if not self.prompt_tokens:
            return None
        return min(self.cached_tokens, self.prompt_tokens) / self.prompt_tokens

    def update(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Fold one event in; return whether the meter changed."""
        if event_type == "context.inspected":
            self.context_tokens = optional_int(payload.get("estimated_tokens"))
            self.working_set_tokens = optional_int(payload.get("high_water_tokens"))
            self._budget(payload.get("budget"))
        elif event_type == "usage.turn":
            self.context_tokens = optional_int(payload.get("prompt_tokens"))
            self._run_totals(payload.get("run"))
        elif event_type == "usage.updated":
            self._budget(payload.get("budget"))
            self._run_totals(payload)
        elif event_type == "context.compacted":
            self.context_tokens = optional_int(payload.get("tokens_after"))
            self.working_set_tokens = (
                optional_int(payload.get("high_water_tokens")) or self.working_set_tokens
            )
            self.compactions += 1
        elif event_type == "model.retry":
            self.retries += 1
        else:
            return False
        return True

    def _run_totals(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        cost = optional_float(value.get("cost"))
        self.cost = cost if cost is not None else self.cost
        self.prompt_tokens = optional_int(value.get("prompt_tokens")) or self.prompt_tokens
        self.cached_tokens = optional_int(value.get("cached_tokens")) or self.cached_tokens

    def _budget(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        self.tool_calls = optional_int(value.get("tool_calls")) or self.tool_calls
        elapsed = optional_float(value.get("elapsed_seconds"))
        self.elapsed_seconds = elapsed if elapsed is not None else self.elapsed_seconds

    def text(self) -> str:
        parts = [f"${self.cost:.4f}"]
        rate = self.cache_hit_rate
        parts.append(f"cache {rate:.0%}" if rate is not None else "cache –")
        if self.context_tokens is not None:
            context = _tokens(self.context_tokens)
            if self.working_set_tokens:
                context += f" / {_tokens(self.working_set_tokens)}"
            parts.append(f"context {context}")
        parts.append(f"{self.tool_calls} tool call{'s' if self.tool_calls != 1 else ''}")
        minutes, seconds = divmod(int(self.elapsed_seconds), 60)
        parts.append(f"{minutes}m {seconds:02d}s")
        if self.retries:
            parts.append(f"{self.retries} retr{'ies' if self.retries != 1 else 'y'}")
        if self.compactions:
            parts.append(f"compacted ×{self.compactions}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        rate = self.cache_hit_rate
        return {
            "text": self.text(),
            "cost": self.cost,
            "cacheHitRate": -1.0 if rate is None else rate,
            "contextTokens": self.context_tokens or 0,
            "workingSetTokens": self.working_set_tokens or 0,
            "contextFraction": (
                min(1.0, (self.context_tokens or 0) / self.working_set_tokens)
                if self.working_set_tokens
                else 0.0
            ),
            "toolCalls": self.tool_calls,
            "elapsedSeconds": self.elapsed_seconds,
        }


def _tokens(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)
