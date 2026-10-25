"""The session plan behind ``update_plan`` (harness plan 3.2).

The plan is gateway-local state: at most 20 steps of ``{id, title, status}`` plus an optional
note. The first call sets the plan; later calls send only the steps that changed, by ``id``,
so the model spends few output tokens. A step with a new ``id`` is appended (its ``title`` is
required then). Once every step is done, a call naming only new steps starts a fresh plan.
The result the model sees is one line; the full plan is journaled for the desktop checklist,
stored per session, and carried into every handoff and wind-down summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_PLAN_STEPS = 20
MAX_STEP_ID_CHARS = 40
MAX_STEP_TITLE_CHARS = 120
MAX_PLAN_NOTE_CHARS = 200
STEP_STATUSES = ("pending", "in_progress", "done", "blocked")
PLAN_ARGUMENTS = frozenset({"steps", "note"})
STEP_FIELDS = frozenset({"id", "title", "status"})


class PlanError(ValueError):
    """An invalid ``update_plan`` call; the message is a one-line corrective hint."""


@dataclass(slots=True)
class PlanStep:
    id: str
    title: str
    status: str = "pending"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "status": self.status}


@dataclass(slots=True)
class PlanState:
    steps: list[PlanStep] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, data: object) -> PlanState:
        """A stored plan; anything malformed is dropped rather than trusted."""
        if not isinstance(data, dict):
            return cls()
        steps = []
        for item in data.get("steps") or ():
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("id") or "")[:MAX_STEP_ID_CHARS]
            title = str(item.get("title") or "")[:MAX_STEP_TITLE_CHARS]
            status = str(item.get("status") or "pending")
            if step_id and title and status in STEP_STATUSES:
                steps.append(PlanStep(step_id, title, status))
        return cls(steps[:MAX_PLAN_STEPS], str(data.get("note") or "")[:MAX_PLAN_NOTE_CHARS])

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps], "note": self.note}

    @property
    def empty(self) -> bool:
        return not self.steps

    def apply(self, arguments: object) -> str:
        """Apply one ``update_plan`` call and return its one-line result."""
        updates, note = _parse(arguments)
        known = {step.id: step for step in self.steps}
        finished = bool(self.steps) and all(step.status == "done" for step in self.steps)
        fresh = not self.steps or (finished and not any(item[0] in known for item in updates))
        steps = (
            [] if fresh else [PlanStep(step.id, step.title, step.status) for step in self.steps]
        )
        by_id = {step.id: step for step in steps}
        for step_id, title, status in updates:
            step = by_id.get(step_id)
            if step is None:
                if not title:
                    raise PlanError(
                        f"step {step_id} is new: give it a title (at most "
                        f"{MAX_STEP_TITLE_CHARS} characters)"
                    )
                if len(steps) >= MAX_PLAN_STEPS:
                    raise PlanError(f"a plan holds at most {MAX_PLAN_STEPS} steps")
                step = PlanStep(step_id, title, status or "pending")
                steps.append(step)
                by_id[step_id] = step
                continue
            if title:
                step.title = title
            if status:
                step.status = status
        self.steps = steps
        if note is not None:
            self.note = note
        elif fresh:
            self.note = ""
        return self.summary()

    def summary(self) -> str:
        """The one-line result, for example ``plan: 3/7 done, step 4 in progress``."""
        total = len(self.steps)
        done = sum(step.status == "done" for step in self.steps)
        parts = [f"plan: {done}/{total} done"]
        active = [step.id for step in self.steps if step.status == "in_progress"]
        if active:
            parts.append(f"step {', '.join(active[:3])} in progress")
        blocked = sum(step.status == "blocked" for step in self.steps)
        if blocked:
            parts.append(f"{blocked} blocked")
        return ", ".join(parts)

    def render(self) -> str:
        """The plan as text for handoffs and wind-down summaries."""
        if not self.steps:
            return ""
        marks = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]", "blocked": "[!]"}
        lines = [f"{marks[step.status]} {step.id}. {step.title}" for step in self.steps]
        if self.note:
            lines.append(f"note: {self.note}")
        return "\n".join(lines)


def _parse(arguments: object) -> tuple[list[tuple[str, str, str]], str | None]:
    if not isinstance(arguments, dict):
        raise PlanError("arguments must be an object with steps")
    unknown = set(arguments) - PLAN_ARGUMENTS
    if unknown:
        raise PlanError(f"unknown field {sorted(unknown)[0]}; use steps and note")
    raw_steps = arguments.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
        raise PlanError(f"steps must be an array of 1 to {MAX_PLAN_STEPS} objects")
    updates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in raw_steps:
        if not isinstance(item, dict):
            raise PlanError("each step must be an object {id, title, status}")
        unknown = set(item) - STEP_FIELDS
        if unknown:
            raise PlanError(f"unknown step field {sorted(unknown)[0]}; use id, title, status")
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, str | int):
            raise PlanError("step id must be a string or an integer")
        step_id = str(raw_id).strip()
        if not step_id or len(step_id) > MAX_STEP_ID_CHARS:
            raise PlanError(f"step id must be 1 to {MAX_STEP_ID_CHARS} characters")
        if step_id in seen:
            raise PlanError(f"step {step_id} appears twice")
        seen.add(step_id)
        title = item.get("title", "")
        if not isinstance(title, str) or len(title.strip()) > MAX_STEP_TITLE_CHARS:
            raise PlanError(
                f"step title must be text of at most {MAX_STEP_TITLE_CHARS} characters"
            )
        status = item.get("status", "")
        if status not in {"", *STEP_STATUSES}:
            raise PlanError(f"step status must be one of {', '.join(STEP_STATUSES)}")
        updates.append((step_id, " ".join(title.split()), str(status)))
    note = arguments.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > MAX_PLAN_NOTE_CHARS:
            raise PlanError(f"note must be text of at most {MAX_PLAN_NOTE_CHARS} characters")
        note = " ".join(note.split())
    return updates, note
