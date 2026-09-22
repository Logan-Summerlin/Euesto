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
