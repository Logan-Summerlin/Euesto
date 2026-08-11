from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextInspection:
    estimated_tokens: int
    submitted_tokens: int
    limit_tokens: int
    compacted_messages: int = 0
    summary: str = ""


def estimate_tokens(text: str) -> int:
    """A deliberately conservative tokenizer-free display estimate."""
    return max(1, (len(text) + 3) // 4) if text else 0


def compact_messages(
    messages: Sequence[dict[str, Any]], max_tokens: int
) -> tuple[list[dict[str, Any]], ContextInspection, list[int]]:
    """Compact old visible turns into a separate, inspectable deterministic summary."""
    source = [dict(item) for item in messages]
    before = sum(estimate_tokens(str(item.get("content") or "")) for item in source)
    if before <= max_tokens:
        return (
            [{key: value for key, value in item.items() if key != "_message_id"} for item in source],
            ContextInspection(before, before, max_tokens),
            [],
        )
    system = [source[0]] if source and source[0].get("role") == "system" else []
    body = source[len(system) :]
    recent: list[dict[str, Any]] = []
    recent_tokens = 0
    recent_budget = max(1_000, int(max_tokens * 0.6))
    for item in reversed(body):
        tokens = estimate_tokens(str(item.get("content") or ""))
        if recent and recent_tokens + tokens > recent_budget:
            break
        recent.append(item)
        recent_tokens += tokens
    recent.reverse()
    covered = body[: len(body) - len(recent)]
    summary_lines = ["Earlier active-branch context (deterministic compaction):"]
    covered_ids: list[int] = []
    for item in covered:
        role = str(item.get("role") or "message")
        content = " ".join(str(item.get("content") or "").split())
        summary_lines.append(f"- {role}: {content[:600]}")
        message_id = item.get("_message_id")
        if isinstance(message_id, int):
            covered_ids.append(message_id)
    summary = "\n".join(summary_lines)[: max(2_000, int(max_tokens * 4 * 0.25))]
    compacted = [*system]
    if covered:
        compacted.append({"role": "system", "content": summary})
    compacted.extend(recent)
    for item in compacted:
        item.pop("_message_id", None)
    after = sum(estimate_tokens(str(item.get("content") or "")) for item in compacted)
    return (
        compacted,
        ContextInspection(before, after, max_tokens, len(covered), summary),
        covered_ids,
    )
