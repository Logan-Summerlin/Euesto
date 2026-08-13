from pathlib import Path

from server.openrouter.agent import LOCAL_TOOL_SCHEMAS
from shared.tools import AGENT_TOOLS, PLAN_TOOLS, TOOL_NAMES


def test_public_tool_contract_is_exactly_seven_tools() -> None:
    names = [item["function"]["name"] for item in LOCAL_TOOL_SCHEMAS]
    assert names == ["read", "write", "edit", "bash", "grep", "find", "ls"]
    assert TOOL_NAMES == set(names)
    assert PLAN_TOOLS == {"read", "grep", "find", "ls"}
    assert AGENT_TOOLS == TOOL_NAMES


def test_mutation_schemas_allow_optional_hashes() -> None:
    for name in ("write", "edit"):
        assert "expected_sha256" not in next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)["parameters"].get("required", [])


def test_find_and_ls_use_path() -> None:
    for name in ("find", "ls"):
        schema = next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)
        assert "path" in schema["parameters"]["properties"]
