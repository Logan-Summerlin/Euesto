"""A collected test id stays short, so a tier's log names tests instead of echoing payloads."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class _Item:
    """The part of a collected item the conftest policy reads."""

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.keywords: dict[str, object] = {}


def _conftest():
    path = ROOT / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("euesto_tests_conftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_parametrization_that_embeds_its_payload_is_refused() -> None:
    policy = _conftest()
    payload = "x" * (policy.MAX_NODE_ID_CHARS + 1)
    oversized = f"tests/unit/executor/test_runner_protocol.py::test_refused[{payload}]"

    assert policy.unbounded_test_ids([_Item(oversized)]) == [oversized]
    short = "tests/unit/executor/test_write.py::test_a[x]"
    assert policy.unbounded_test_ids([_Item(short)]) == []

    with pytest.raises(pytest.UsageError) as refusal:
        policy.pytest_collection_modifyitems(None, [_Item(oversized)])

    message = str(refusal.value)
    assert "ids=" in message and "docs/TESTING.md" in message
    # The refusal names the offender in a bounded line instead of repeating the payload.
    assert len(message) < 500
