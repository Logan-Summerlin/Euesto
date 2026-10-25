"""The static prefix (agent prompt plus tool schemas) stays within a fixed byte budget (1.9).

Adding a tool or a parameter must fit within the bound, or raise it deliberately here.
"""

from __future__ import annotations

import json

import pytest

from server.agent.prompts import agent_prompt
from server.openrouter.agent import tool_schemas

PREFIX_BUDGET_BYTES = {"agent": 12_000, "plan": 6_000}


def _prefix_bytes(mode: str) -> int:
    schemas = json.dumps(tool_schemas(mode), separators=(",", ":"), ensure_ascii=False)
    return len(agent_prompt(mode).encode("utf-8")) + len(schemas.encode("utf-8"))


@pytest.mark.parametrize("mode", sorted(PREFIX_BUDGET_BYTES))
def test_static_prefix_fits_its_budget(mode: str) -> None:
    assert _prefix_bytes(mode) <= PREFIX_BUDGET_BYTES[mode]


def test_plan_prefix_is_smaller_than_agent() -> None:
    assert _prefix_bytes("plan") < _prefix_bytes("agent")


@pytest.mark.parametrize("mode", sorted(PREFIX_BUDGET_BYTES))
def test_schemas_are_closed_and_described_briefly(mode: str) -> None:
    for schema in tool_schemas(mode):
        function = schema["function"]
        parameters = function["parameters"]
        assert parameters.get("additionalProperties") is False, function["name"]
        # One sentence, plus a usage hint only where it changes behavior.
        assert function["description"].count(". ") <= 2, function["name"]
        assert len(function["description"]) <= 200, function["name"]
        for name, prop in parameters.get("properties", {}).items():
            assert len(prop.get("description", "")) <= 160, (function["name"], name)
