from pathlib import Path

from executor.config import ExecutorConfig
from shared.tools import TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]


def test_public_tool_documentation_matches_registry() -> None:
    docs = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    for name in sorted(TOOL_NAMES):
        assert f"`{name}`" in docs
    assert TOOL_NAMES == {"read", "write", "edit", "bash", "grep", "find", "ls"}


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
