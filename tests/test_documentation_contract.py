from pathlib import Path

from executor.config import ExecutorConfig
from shared.tools import TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]


def test_public_tool_documentation_matches_registry() -> None:
    docs = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    for name in sorted(TOOL_NAMES):
        assert f"`{name}`" in docs
    assert TOOL_NAMES == {"read", "write", "edit", "patch", "bash", "grep", "find", "ls", "status", "investigate_repository"}
    assert "ten model-facing tools" in docs


def test_limits_document_active_coding_defaults_and_ceilings() -> None:
    docs = (ROOT / "docs" / "LIMITS.md").read_text(encoding="utf-8")

    def documented(value: int) -> bool:
        raw = str(value)
        return raw in docs or f"{value:,}" in docs

    config = ExecutorConfig._profiles()["coding"]
    for name, value in config.items():
        assert documented(value), name
    for name, value in ExecutorConfig.HARD_CEILINGS.items():
        assert documented(value), name


def test_documentation_keeps_publication_boundary_explicit() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    publication = (ROOT / "docs" / "PUBLICATION.md").read_text(encoding="utf-8")
    assert "executor cannot publish" in architecture.lower()
    assert "desktop publication broker" in architecture.lower()
    assert "stale" in publication.lower()
    assert "hash" in publication.lower()


def test_documented_tool_defaults_match_code() -> None:
    import importlib
    import re

    from server.agent import runtime
    from shared.tools import MAX_TOOL_ARGUMENT_BYTES

    tools_doc = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    limits_doc = (ROOT / "docs" / "LIMITS.md").read_text(encoding="utf-8")
    ls_module = importlib.import_module("executor.tools.ls")
    find_module = importlib.import_module("executor.tools.find")
    coding = ExecutorConfig._profiles()["coding"]

    def section(name: str) -> str:
        match = re.search(rf"^## `{name}`\n(.*?)(?=^## )", tools_doc, re.S | re.M)
        assert match, name
        return match.group(1)

    for name, default, config_name in (("ls", ls_module.DEFAULT_LS_RESULTS, "max_ls_results"), ("find", find_module.DEFAULT_FIND_RESULTS, "max_find_results")):
        assert default == coding[config_name], name
        assert f"**Default:** {default:,} results when `max_results` is omitted" in section(name)
        assert re.search(rf"^\| `{name}` results \| {default:,} \|", limits_doc, re.M), name
        assert f"{coding['max_search_seconds']}-second time budget" in section(name)
    assert f"{coding['max_search_results']:,} results" in section("grep")
    assert f"{coding['max_grep_output_bytes']:,}-byte output budget" in section("grep")
    assert f"{MAX_TOOL_ARGUMENT_BYTES:,}" in tools_doc
    assert f"| Tool arguments (protocol) | {MAX_TOOL_ARGUMENT_BYTES:,} bytes |" in limits_doc
    assert f"{runtime.INVESTIGATION_MAX_WALL_SECONDS} seconds" in limits_doc
    assert f"{runtime.INVESTIGATION_MIN_WALL_SECONDS}–{runtime.INVESTIGATION_MAX_WALL_SECONDS} seconds" in section("investigate_repository")


_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_CALL_LIMIT_DOCUMENTS = ("README.md", "AGENTS.md", "docs/TOOLS.md", "docs/LIMITS.md", "docs/ARCHITECTURE.md")


def _stated_investigation_call_limits(text: str) -> set[int]:
    """Every per-turn investigation call count a document states, in words or digits."""
    import re

    words = {word: value for value, word in _NUMBER_WORDS.items()}
    pattern = re.compile(r"\b(" + "|".join(words) + r"|\d+)\s+(?:investigation\s+)?calls?\s+(?:are\s+accepted\s+)?per\s+(?:parent\s+)?turn", re.I)
    return {int(raw) if raw.isdigit() else words[raw.casefold()] for raw in pattern.findall(text)}


def test_investigation_call_limit_is_documented_consistently_with_code() -> None:
    from server.agent import runtime
    from shared.requests import AgentRunRequest

    ceiling = runtime.INVESTIGATION_HARD_CALL_CEILING
    assert runtime.DEFAULT_INVESTIGATION_CALL_BUDGET <= ceiling
    assert AgentRunRequest.__dataclass_fields__["investigation_call_budget"].default <= ceiling
    for name in _CALL_LIMIT_DOCUMENTS:
        stated = _stated_investigation_call_limits((ROOT / name).read_text(encoding="utf-8"))
        assert stated, f"{name} must state the investigation call limit"
        assert stated == {ceiling}, f"{name} states {sorted(stated)} investigation calls per turn; code allows {ceiling}"
    # The runtime context shown to the agent must agree too.
    context = runtime._render_executor_context({"environment": {"workspace_root": "."}}, "agent")
    assert _stated_investigation_call_limits(context) == {ceiling}


def test_call_limit_detector_catches_drift() -> None:
    assert _stated_investigation_call_limits("is capped at two calls per turn") == {2}
    assert _stated_investigation_call_limits("Up to 4 calls are accepted per turn") == {4}
    assert _stated_investigation_call_limits("capped at four calls per parent turn") == {4}


def test_readme_search_and_list_default_matches_code() -> None:
    import importlib
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    coding = ExecutorConfig._profiles()["coding"]
    defaults = {
        importlib.import_module("executor.tools.ls").DEFAULT_LS_RESULTS,
        importlib.import_module("executor.tools.find").DEFAULT_FIND_RESULTS,
        coding["max_ls_results"],
        coding["max_find_results"],
        coding["max_search_results"],
    }
    assert len(defaults) == 1, f"ls/find/grep defaults diverged: {sorted(defaults)}"
    stated = re.findall(r"([\d,]+) search/list results", readme)
    assert stated and {int(value.replace(",", "")) for value in stated} == defaults


def test_investigation_hint_and_findings_bounds_are_documented() -> None:
    from shared.investigation import MAX_FINDING_JUSTIFICATION_CHARS, MAX_FINDINGS, MAX_INSPECTED_PATHS

    tools_doc = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    limits_doc = (ROOT / "docs" / "LIMITS.md").read_text(encoding="utf-8")
    assert f"up to {MAX_INSPECTED_PATHS} relative files or directories" in tools_doc
    assert f"up to {MAX_FINDINGS} `{{file, line, justification, observed}}` entries" in tools_doc
    assert f"at most {MAX_FINDING_JUSTIFICATION_CHARS:,} characters" in tools_doc
    assert f"up to {MAX_INSPECTED_PATHS} `inspected_paths`" in limits_doc
    assert f"at most {MAX_FINDINGS} structured findings" in limits_doc


def test_approval_policies_are_documented() -> None:
    from shared.permissions import APPROVAL_POLICIES

    tools_doc = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for policy in APPROVAL_POLICIES:
        assert f"| `{policy}`" in tools_doc, policy
        assert f"`{policy}`" in architecture, policy
