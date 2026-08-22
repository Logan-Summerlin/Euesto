from pathlib import Path

from shared.tools import TOOL_NAMES


ROOT = Path(__file__).resolve().parents[1]


def test_documentation_index_and_ownership_map_exist() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for name in ("ARCHITECTURE.md", "CONTRIBUTING.md", "TESTING.md", "TOOLS.md", "LIMITS.md", "PUBLICATION.md", "TROUBLESHOOTING.md"):
        assert name in index
    for owner in ("qml/", "src/", "app.py", "server/", "executor/", "shared/"):
        assert owner in architecture


def test_local_runtime_artifacts_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".local-chat-snapshot.json" in ignored
    assert ".local-chat-checkpoints/" in ignored


def test_public_tool_names_are_documented() -> None:
    docs = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    assert all(f"`{name}`" in docs for name in TOOL_NAMES)
