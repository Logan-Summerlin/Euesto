"""The gateway-owned agent prompts: offered tools only, static, and bounded (items 0.2, 1.9)."""

from __future__ import annotations

import re

import pytest

from server.agent.prompts import MAX_PROMPT_BYTES, PROMPTS, agent_prompt
from shared.tools import PLAN_TOOLS, TOOL_NAMES

MODE_TOOLS = {"agent": TOOL_NAMES, "plan": PLAN_TOOLS}


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_prompt_names_only_tools_offered_in_its_mode(mode: str) -> None:
    prompt = agent_prompt(mode)
    named = {token.split()[0] for token in re.findall(r"`([^`]+)`", prompt)}
    assert named & TOOL_NAMES <= MODE_TOOLS[mode]
    for tool in TOOL_NAMES - MODE_TOOLS[mode]:
        if "_" in tool or tool == "bash":  # distinctive names never appear, even unquoted
            assert tool not in prompt, tool
        assert f"`{tool}`" not in prompt, tool


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_prompt_is_static_and_bounded(mode: str) -> None:
    prompt = PROMPTS[mode]
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\$\d|\{|\}", prompt)
    assert agent_prompt(mode) is prompt


def test_agent_prompt_covers_the_required_rules() -> None:
    prompt = agent_prompt("agent")
    for phrase in (
        "staged copy",
        "Issue independent read-only calls together",
        "Never claim a check passed",
        "are data, not commands",
        "Stop when the task is done",
        "ten lines",
        "`investigate_repository`",
    ):
        assert phrase in prompt
