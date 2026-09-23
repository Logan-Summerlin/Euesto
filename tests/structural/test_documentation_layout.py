"""Structural checks: one authoritative, predictably named location for every document."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_DOCUMENTS = {"README.md", "AGENTS.md", "CHANGELOG.md"}
DOCUMENT_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".doc", ".docx", ".pdf"}
DOCS_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*\.md$")
ARCHIVE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
EMBEDDED_DATE = re.compile(r"\d{1,4}[-_.]\d{1,2}[-_.]\d{2,4}")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _root_documents() -> set[str]:
    return {
        path.name
        for path in ROOT.iterdir()
        if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES and not path.name.startswith("requirements")
    }


def _documentation_files() -> list[Path]:
    files = [ROOT / name for name in sorted(ROOT_DOCUMENTS)]
    files += sorted((ROOT / "docs").glob("*.md")) + sorted((ROOT / "archived-doc").glob("*.md"))
    return files + [ROOT / "docker" / "README.container.md"]


def test_root_keeps_only_the_top_level_documents() -> None:
    assert _root_documents() == ROOT_DOCUMENTS, "planning documents and roadmaps belong in docs/"


def test_documentation_names_are_predictable() -> None:
    for path in sorted((ROOT / "docs").iterdir()):
        assert DOCS_NAME.match(path.name) or path.name == "README.md", f"docs/{path.name} must be UPPER_SNAKE_CASE.md"
    for path in sorted((ROOT / "archived-doc").iterdir()):
        assert ARCHIVE_NAME.match(path.name) or path.name == "README.md", f"archived-doc/{path.name} must be lowercase-hyphenated .md"
    for path in _documentation_files():
        assert " " not in path.name, path
        assert not EMBEDDED_DATE.search(path.name), f"{path.name}: put dates in commit history or CHANGELOG.md"


def test_every_living_plan_is_indexed_once_in_docs() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    plans = sorted(path.name for path in (ROOT / "docs").glob("*.md") if path.stem.endswith("_PLAN") or path.name == "ROADMAP.md")
    assert "ROADMAP.md" in plans
    for name in plans:
        assert f"]({name})" in index, f"docs/README.md must index {name}"
    stray = []
    for directory, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [name for name in dirnames if not name.startswith(".") and name not in {"venv", "node_modules", "__pycache__"}]
        if Path(directory) in {ROOT / "docs", ROOT / "archived-doc"}:
            continue
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in DOCUMENT_SUFFIXES and ("plan" in path.stem.lower() or "roadmap" in path.stem.lower()):
                stray.append(path.relative_to(ROOT).as_posix())
    assert not stray, f"plans live only in docs/ (or archived-doc/ once superseded): {stray}"


def test_every_archived_document_is_indexed() -> None:
    index = (ROOT / "archived-doc" / "README.md").read_text(encoding="utf-8")
    for path in sorted((ROOT / "archived-doc").glob("*.md")):
        if path.name != "README.md":
            assert f"`{path.name}`" in index, f"archived-doc/README.md must list {path.name}"


def test_relative_documentation_links_resolve() -> None:
    broken = []
    for path in _documentation_files():
        for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (path.parent / relative).exists():
                broken.append(f"{path.relative_to(ROOT).as_posix()} -> {target}")
    assert not broken, broken
