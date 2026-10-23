"""Structural check: every wait in the suite is bounded (docs/TESTING.md, "Async test policy").

``--timeout-method=thread`` raises inside the test's thread, which CPython delivers only while
that thread runs bytecode. A task parked in ``epoll_wait`` on an ``Event`` nobody sets never
reaches the deadline, so an unbounded rendezvous turns a broken refactor into a hung tier
instead of a red test. This check makes that failure mode impossible to add silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
# A sleep is allowed when the waiting *is* the subject of the test (docs/TESTING.md: "A real
# process such as `sleep 30` is acceptable when it is the subject of a cancellation or timeout
# test"). Each entry is (file name, test function, seconds); the set must match exactly, so a
# new long sleep needs a review of this list and a removed one needs its entry deleted.
AUDITED_LONG_SLEEPS = {
    ("test_streaming_turns.py", "test_cancellation_mid_stream_stops_the_turn", 10.0),
    ("test_redaction_and_web.py", "test_fetches_time_out", 1.0),
}
MINIMUM_LONG_SLEEP_SECONDS = 1.0


def _is_asyncio_sleep(call: ast.Call) -> bool:
    function = call.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "sleep"
        and isinstance(function.value, ast.Name)
        and function.value.id == "asyncio"
    )


def _constant_seconds(call: ast.Call) -> float | None:
    if not call.args:
        return None
    argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, int | float):
        return float(argument.value)
    return None


class _AwaitVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.test: str | None = None
        self.bare_waits: list[str] = []
        self.long_sleeps: set[tuple[str, str, float]] = set()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        previous = self.test
        if previous is None and node.name.startswith("test_"):
            self.test = node.name
        self.generic_visit(node)
        self.test = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Await(self, node: ast.Await) -> None:
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "wait"
            and not call.args
            and not call.keywords
        ):
            self.bare_waits.append(f"{self.path.name}:{node.lineno}: {ast.unparse(call)}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_asyncio_sleep(node):
            seconds = _constant_seconds(node)
            if seconds is not None and seconds >= MINIMUM_LONG_SLEEP_SECONDS:
                self.long_sleeps.add((self.path.name, self.test or "<module>", seconds))
        self.generic_visit(node)


def _scan() -> tuple[list[str], set[tuple[str, str, float]]]:
    bare_waits: list[str] = []
    long_sleeps: set[tuple[str, str, float]] = set()
    for path in sorted(TESTS.rglob("test_*.py")):
        visitor = _AwaitVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        bare_waits.extend(visitor.bare_waits)
        long_sleeps |= visitor.long_sleeps
    return bare_waits, long_sleeps


def test_no_test_awaits_an_unbounded_rendezvous() -> None:
    bare_waits, _ = _scan()
    assert not bare_waits, (
        "wrap the wait in asyncio.wait_for(..., timeout=RENDEZVOUS_TIMEOUT_SECONDS) so a "
        f"missing signal fails the test instead of wedging the tier: {bare_waits}"
    )


def test_long_sleeps_are_the_subject_of_their_test() -> None:
    _, long_sleeps = _scan()
    assert long_sleeps == AUDITED_LONG_SLEEPS, (
        "a sleep of a second or more must be the subject of a cancellation or timeout test and "
        f"listed in AUDITED_LONG_SLEEPS: added {sorted(long_sleeps - AUDITED_LONG_SLEEPS)}, "
        f"stale {sorted(AUDITED_LONG_SLEEPS - long_sleeps)}"
    )


if __name__ == "__main__":  # pragma: no cover - lets the file self-check without pytest
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("async bounds checked")
