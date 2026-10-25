"""Risk flags for publication review (harness plan 4.3, decision D5).

Each manifest operation is classified before the user reviews it:

* ``host_executable``: files the host runs without anyone opening them (CI workflows, git
  hooks, editor task and run configurations, task runners, build backends, package-manager
  scripts, Dockerfiles and Compose files, ``conftest.py``, ``.envrc``, ...), and any file that
  gains an executable bit;
* ``dependency``: dependency manifests and lockfiles;
* ``policy``: anything under ``.euesto/`` (the workspace policy, 4.6);
* ``binary``: content carried byte-for-byte rather than as text;
* ``large_deletion``: a deletion in a batch that deletes many files.

Review lists flagged files first. Auto never publishes a batch with a host-executable,
dependency, or policy change without explicit confirmation (``needs_confirmation``). The
same module scans text content for high-confidence secrets so the review can warn before a
secret is written into the workspace (4.5); only kinds and counts are reported.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .secret_scan import find_secrets

RISK_FLAGS = ("host_executable", "dependency", "policy", "binary", "large_deletion")
CONFIRM_FLAGS = frozenset({"host_executable", "dependency", "policy"})
FLAG_LABELS = {
    "host_executable": "runs on the host",
    "dependency": "dependency",
    "policy": "workspace policy",
    "binary": "binary",
    "large_deletion": "large deletion",
}
LARGE_DELETION_FILES = 20
POLICY_DIRECTORY = ".euesto"

# Directories whose files the host executes (matched against the leading path segments).
HOST_EXECUTABLE_DIRECTORIES = (
    ".github/workflows",
    ".github/actions",
    ".gitlab",
    ".circleci",
    ".buildkite",
    ".husky",
    ".githooks",
    ".git-hooks",
    ".devcontainer",
    ".idea/runconfigurations",
)
# File names (any directory), case-insensitive globs.
HOST_EXECUTABLE_NAMES = (
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "jenkinsfile",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    "makefile",
    "gnumakefile",
    "*.mk",
    "justfile",
    "taskfile.yml",
    "taskfile.yaml",
    "rakefile",
    "noxfile.py",
    "tox.ini",
    "setup.py",
    "hatch_build.py",
    "build.rs",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradlew",
    "mvnw",
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    ".pre-commit-config.yaml",
    ".pre-commit-hooks.yaml",
    "lefthook.yml",
    ".envrc",
    "dockerfile",
    "dockerfile.*",
    "*.dockerfile",
    "containerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose.yml",
    "compose.yaml",
    "compose.*.yml",
    "compose.*.yaml",
    "*.code-workspace",
)
HOST_EXECUTABLE_PATHS = (".vscode/tasks.json", ".vscode/launch.json", ".vscode/settings.json")
DEPENDENCY_NAMES = (
    "requirements*.txt",
    "requirements*.in",
    "constraints*.txt",
    "pyproject.toml",
    "setup.cfg",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pdm.lock",
    "environment.yml",
    "environment.yaml",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "bun.lockb",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "go.work",
    "gemfile",
    "gemfile.lock",
    "composer.json",
    "composer.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "gradle.lockfile",
    "*.csproj",
    "packages.lock.json",
    "directory.packages.props",
    "mix.exs",
    "mix.lock",
    "pubspec.yaml",
    "pubspec.lock",
    "podfile",
    "podfile.lock",
    "package.swift",
    "package.resolved",
)

BaseReader = Callable[[str], str | None]


def classify_operation(
    operation: Mapping[str, Any] | Any,
    *,
    base_text: str | None = None,
    deletions: int = 0,
) -> tuple[str, ...]:
    """The risk flags of one manifest operation, in ``RISK_FLAGS`` order.

    ``base_text`` is the host file's current text when the reviewer can read it; it lets a
    ``package.json`` or ``pyproject.toml`` change count as host-executable only when its
    scripts or build backend actually changed. Without it, such a change is flagged.
    ``deletions`` is the number of deletions in the whole batch.
    """
    values = _fields(operation)
    path = str(values.get("path") or "")
    folded = path.casefold()
    name = PurePosixPath(folded).name
    kind = str(values.get("operation") or "")
    flags: set[str] = set()
    if _host_executable_path(folded, name):
        flags.add("host_executable")
    if kind != "delete":
        if _gains_executable_bit(values.get("base_mode"), values.get("staged_mode")):
            flags.add("host_executable")
        if name in {"package.json", "pyproject.toml"} and _scripts_changed(
            name, base_text, values.get("content")
        ):
            flags.add("host_executable")
    if _matches(name, DEPENDENCY_NAMES):
        flags.add("dependency")
    if folded == POLICY_DIRECTORY or folded.startswith(POLICY_DIRECTORY + "/"):
        flags.add("policy")
    if values.get("content_base64") is not None:
        flags.add("binary")
    if kind == "delete" and deletions >= LARGE_DELETION_FILES:
        flags.add("large_deletion")
    return tuple(flag for flag in RISK_FLAGS if flag in flags)


def classify_manifest(
    operations: Sequence[Mapping[str, Any] | Any], *, base_reader: BaseReader | None = None
) -> dict[str, tuple[str, ...]]:
    """Flags for every operation, by path."""
    items = [_fields(item) for item in operations]
    deletions = sum(1 for item in items if item.get("operation") == "delete")
    result: dict[str, tuple[str, ...]] = {}
    for item in items:
        path = str(item.get("path") or "")
        base_text = None
        if base_reader is not None and PurePosixPath(path.casefold()).name in {
            "package.json",
            "pyproject.toml",
        }:
            base_text = base_reader(path)
        result[path] = classify_operation(item, base_text=base_text, deletions=deletions)
    return result


def needs_confirmation(flags: Mapping[str, Sequence[str]]) -> bool:
    """Whether Auto must stop and ask before publishing this batch (D5)."""
    return any(CONFIRM_FLAGS.intersection(item) for item in flags.values())


def review_order(flags: Mapping[str, Sequence[str]]) -> list[str]:
    """Paths with flagged files first (most serious flag first), then the rest by path."""

    def key(path: str) -> tuple[int, str]:
        present = flags.get(path) or ()
        rank = min((RISK_FLAGS.index(flag) for flag in present), default=len(RISK_FLAGS))
        return rank, path

    return sorted(flags, key=key)


def manifest_secrets(
    operations: Sequence[Mapping[str, Any] | Any],
) -> dict[str, dict[str, int]]:
    """Secret kinds and counts that a publication would write, by path (never the values)."""
    found: dict[str, dict[str, int]] = {}
    for operation in operations:
        values = _fields(operation)
        content = values.get("content")
        if isinstance(content, str) and content:
            kinds = find_secrets(content)
            if kinds:
                found[str(values.get("path") or "")] = kinds
    return found


def _fields(operation: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(operation, Mapping):
        return operation
    to_dict = getattr(operation, "to_dict", None)
    return to_dict() if callable(to_dict) else {}


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _host_executable_path(folded: str, name: str) -> bool:
    if any(
        folded == directory or folded.startswith(directory + "/")
        for directory in HOST_EXECUTABLE_DIRECTORIES
    ):
        return True
    if folded in HOST_EXECUTABLE_PATHS or "/.git/hooks/" in f"/{folded}":
        return True
    return _matches(name, HOST_EXECUTABLE_NAMES)


def _gains_executable_bit(base_mode: object, staged_mode: object) -> bool:
    if not isinstance(staged_mode, int) or isinstance(staged_mode, bool):
        return False
    before = base_mode if isinstance(base_mode, int) and not isinstance(base_mode, bool) else 0
    return bool(staged_mode & 0o111) and not before & 0o111


def _scripts_changed(name: str, base_text: str | None, staged_text: object) -> bool:
    """Whether package scripts (``package.json``) or the build backend and entry points
    (``pyproject.toml``) differ; anything unreadable counts as changed."""
    if not isinstance(staged_text, str):
        return True
    staged = _executable_section(name, staged_text)
    if staged is _UNREADABLE:
        return True
    if base_text is None:
        return bool(staged)
    base = _executable_section(name, base_text)
    return base is _UNREADABLE or base != staged


_UNREADABLE = object()


def _executable_section(name: str, text: str) -> object:
    try:
        if name == "package.json":
            data = json.loads(text)
            if not isinstance(data, dict):
                return _UNREADABLE
            return {key: data.get(key) for key in ("scripts", "bin") if key in data}
        data = tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError):
        return _UNREADABLE
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    section = {
        "build-system": data.get("build-system"),
        "scripts": project.get("scripts"),
        "entry-points": project.get("entry-points"),
    }
    return {key: value for key, value in section.items() if value is not None}
