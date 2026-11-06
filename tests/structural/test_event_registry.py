"""Every event type the gateway journals must be registered in ``shared.events.EVENT_TYPES``.

The journal builds an ``EventEnvelope`` for each append, and the envelope rejects an unregistered
type, so a missing registration fails every run that reaches the emitting line. Gateway unit
tests stub ``append`` and never build an envelope, so this check reads the source instead.
"""

import ast
from pathlib import Path

from shared.events import EVENT_TYPES, EventEnvelope

ROOT = Path(__file__).resolve().parents[2]
APPEND_NAMES = frozenset({"append", "append_event"})


def _emitted_event_types() -> dict[str, list[str]]:
    emitted: dict[str, list[str]] = {}
    for path in sorted((ROOT / "server").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            kind = node.args[1]
            if (
                name in APPEND_NAMES
                and isinstance(kind, ast.Constant)
                and "." in str(kind.value)
            ):
                location = f"{path.relative_to(ROOT)}:{node.lineno}"
                emitted.setdefault(str(kind.value), []).append(location)
    return emitted


def test_the_scan_finds_the_gateway_events() -> None:
    emitted = _emitted_event_types()
    assert {"run.failed", "context.inspected", "context.high_water"} <= set(emitted)


def test_every_journaled_event_type_is_registered() -> None:
    missing = {
        kind: places
        for kind, places in _emitted_event_types().items()
        if kind not in EVENT_TYPES
    }
    assert not missing, f"Register these in shared/events.py EVENT_TYPES: {missing}"


def test_every_emitted_event_type_builds_an_envelope() -> None:
    for kind in _emitted_event_types():
        assert EventEnvelope(1, "run", kind, "2026-01-01T00:00:00Z").type == kind
