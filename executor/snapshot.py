"""A bounded, deterministic workspace snapshot for the session context (plan item 1.7).

It replaces the usual opening ``ls``/``find``/``read README`` turns: the top-level layout to
depth two with file counts, detected languages and manifests, and detected check commands.
It is built from the published workspace (``/source``), so the same workspace always yields
the same bytes, and the gateway freezes it for the session so it stays cached. Every name in it
comes from the workspace, so the gateway labels it untrusted.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

from .paths import is_tool_excluded

MAX_SCAN_ENTRIES = 50_000
MAX_LAYOUT_ENTRIES = 120
MAX_AGENTS_MD_BYTES = 24_000
MAX_MANIFEST_BYTES = 256_000
LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".c": "C",
    ".h": "C/C++ header",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".qml": "QML",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
    ".md": "Markdown",
}
MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "tox.ini",
    "noxfile.py",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "Makefile",
    "CMakeLists.txt",
    "Dockerfile",
)


def workspace_snapshot(source_root: Path) -> dict[str, object]:
    root = source_root.resolve(strict=True)
    files = directories = 0
    truncated = False
    languages: dict[str, int] = {}
    counts: dict[str, int] = {}
    layout: dict[str, str] = {}
    manifests: list[str] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        relative_dir = Path(current).relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not is_tool_excluded(prefix + name)
            and not stat.S_ISLNK((Path(current) / name).lstat().st_mode)
        )
        depth = 0 if not prefix else prefix.count("/")
        for name in dirnames:
            directories += 1
            if depth < 2:
                layout[prefix + name] = "directory"
        for name in sorted(filenames):
            relative = prefix + name
            if is_tool_excluded(relative):
                continue
            files += 1
            if files + directories > MAX_SCAN_ENTRIES:
                truncated = True
                break
            top = relative.split("/", 2)
            for level in range(1, min(len(top), 3)):
                key = "/".join(top[:level])
                counts[key] = counts.get(key, 0) + 1
            if depth == 0:
                layout[relative] = "file"
            language = LANGUAGES.get(Path(name).suffix.casefold())
            if language:
                languages[language] = languages.get(language, 0) + 1
            if depth <= 1 and (name in MANIFESTS or name.startswith("requirements")):
                manifests.append(relative)
        if truncated:
            break
    entries = [
        {
            "path": path,
            "type": kind,
            **({"files": counts.get(path, 0)} if kind == "directory" else {}),
        }
        for path, kind in sorted(layout.items())
    ][:MAX_LAYOUT_ENTRIES]
    return {
        "file_count": files,
        "directory_count": directories,
        "truncated": truncated or len(layout) > MAX_LAYOUT_ENTRIES,
        "languages": [
            {"name": name, "files": count}
            for name, count in sorted(languages.items(), key=lambda item: (-item[1], item[0]))
        ],
        "manifests": sorted(manifests),
        "checks": detect_checks(root, set(manifests)),
        "layout": entries,
    }


def detect_checks(root: Path, manifests: set[str]) -> list[str]:
    """Check commands a contributor would run, inferred from top-level manifests only."""
    checks: list[str] = []
    pyproject = _toml(root / "pyproject.toml") if "pyproject.toml" in manifests else {}
    tools = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    if "ruff" in tools:
        checks.append("ruff check .")
    if "mypy" in tools:
        checks.append("mypy .")
    if "pytest" in tools or (root / "tests").is_dir() and "pyproject.toml" in manifests:
        checks.append("pytest -q")
    package = _json(root / "package.json") if "package.json" in manifests else {}
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    runner = (
        "pnpm"
        if "pnpm-lock.yaml" in manifests
        else "yarn"
        if "yarn.lock" in manifests
        else "npm"
    )
    for script in ("test", "lint", "typecheck", "build"):
        if script in scripts:
            checks.append(f"{runner} test" if script == "test" else f"{runner} run {script}")
    if "tsconfig.json" in manifests and "typecheck" not in scripts:
        checks.append("npx tsc --noEmit")
    if "go.mod" in manifests:
        checks.append("go test ./...")
    if "Cargo.toml" in manifests:
        checks.append("cargo test")
    if "Makefile" in manifests and _make_target(root / "Makefile", "test"):
        checks.append("make test")
    return checks


def agents_md(source_root: Path) -> tuple[str, bool]:
    """The published ``AGENTS.md``, bounded; never followed through a link."""
    path = source_root / "AGENTS.md"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "", False
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return "", False
        raw = handle.read(MAX_AGENTS_MD_BYTES + 1)
    truncated = len(raw) > MAX_AGENTS_MD_BYTES
    return raw[:MAX_AGENTS_MD_BYTES].decode("utf-8", errors="ignore"), truncated


def _read_bounded(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _toml(path: Path) -> dict:
    raw = _read_bounded(path)
    try:
        value = tomllib.loads(raw.decode("utf-8")) if raw is not None else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json(path: Path) -> dict:
    raw = _read_bounded(path)
    try:
        value = json.loads(raw) if raw is not None else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _make_target(path: Path, target: str) -> bool:
    raw = _read_bounded(path)
    if raw is None:
        return False
    return any(
        line.startswith(f"{target}:")
        for line in raw.decode("utf-8", errors="ignore").splitlines()
    )
