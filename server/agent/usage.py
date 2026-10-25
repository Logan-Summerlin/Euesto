"""Per-turn usage records and prompt-cache miss classification.

A turn *misses* when its cached tokens fall more than 10% short of the previous request's
prompt size (providers cache in blocks, so a small shortfall is normal). Expected causes are
labelled; anything else is ``unexpected`` and raises ``cache.unexpected_miss``, which flags a
harness regression: the turn should have extended the previous request (same provider, within
five minutes, no compaction) but did not.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.coercion import optional_float, optional_int

from .budgets import usage_ratios

CACHE_LIFETIME_SECONDS = 300
MISS_TOLERANCE = 0.9
MISS_CAUSES = (
    "first_turn",
    "compaction",
    "session_context_changed",
    "idle",
    "provider_switch",
    "model_or_mode_switch",
    "unexpected",
)
MAX_TRACKED_SESSIONS = 256


@dataclass(slots=True)
class TurnRecord:
    """What the next turn's cache classification needs from the previous request."""

    prompt_tokens: int
    provider: str | None
    model_and_mode: str
    finished_at: float


@dataclass(slots=True)
class UsageLedger:
    """Cumulative usage for one session across its runs."""

    prompt_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    turns: int = 0
    tool_calls: int = 0

    def add(self, usage: dict[str, object]) -> None:
        self.prompt_tokens += max(0, optional_int(usage.get("prompt_tokens")) or 0)
        self.cached_tokens += max(0, optional_int(usage.get("cached_tokens")) or 0)
        self.cache_write_tokens += max(0, optional_int(usage.get("cache_write_tokens")) or 0)
        self.completion_tokens += max(0, optional_int(usage.get("completion_tokens")) or 0)
        self.cost += max(0.0, optional_float(usage.get("cost")) or 0.0)
        self.turns += 1

    def summary(self) -> dict[str, int | float]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": self.cost,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            **usage_ratios(self.prompt_tokens, self.cached_tokens, self.turns),
        }


def classify_cache_miss(
    previous: TurnRecord | None,
    usage: dict[str, object],
    *,
    provider: str | None,
    model_and_mode: str,
    now: float,
    expected: str | None = None,
) -> str | None:
    """Return the miss cause for this turn, or ``None`` when it extended the previous request.

    ``expected`` names an expected miss the runtime caused since the previous turn
    (``compaction`` or ``session_context_changed``).
    """
    if previous is None:
        return "first_turn"
    cached = optional_int(usage.get("cached_tokens"))
    if cached is None or previous.prompt_tokens <= 0:
        # The provider did not report caching, so there is nothing to classify.
        return None
    if cached >= previous.prompt_tokens * MISS_TOLERANCE:
        return None
    if expected:
        return expected
    if previous.model_and_mode != model_and_mode:
        return "model_or_mode_switch"
    if provider and previous.provider and provider != previous.provider:
        return "provider_switch"
    if now - previous.finished_at > CACHE_LIFETIME_SECONDS:
        return "idle"
    return "unexpected"
