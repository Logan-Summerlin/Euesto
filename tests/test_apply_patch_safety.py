from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from executor.errors import classify_error
from executor.tools.apply_patch import apply_patch
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS


def _schema(name: str) -> dict:
    return next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)


def test_apply_patch_requires_explicit_mode_and_uses_exact_names(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("one two three", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="mode is required"):
        apply_patch(
            tmp_path,
            {"edits": [{"path": "value.txt", "expected_sha256": digest, "content": "replacement"}]},
            max_bytes=1_000,
        )

    _output, result = apply_patch(
        tmp_path,
        {"edits": [{
            "path": "value.txt",
            "expected_sha256": digest,
            "mode": "replace_exact",
            "old_str": "two",
            "new_str": "2",
        }]},
        max_bytes=1_000,
    )
    assert target.read_text(encoding="utf-8") == "one 2 three"
    assert result["diffs"][0]["text"]
    assert result["diff_truncated"] is False


def test_apply_patch_shrink_guard_blocks_suspicious_whole_file_replacement(tmp_path: Path) -> None:
    target = tmp_path / "large.py"
    original = "value = 1\n" * 40
    target.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode()).hexdigest()

    with pytest.raises(Exception) as exc_info:
        apply_patch(
            tmp_path,
            {"edits": [{
                "path": "large.py",
                "expected_sha256": digest,
                "mode": "replace_file",
                "content": "x\n",
            }]},
            max_bytes=10_000,
        )
    assert getattr(exc_info.value, "code", None) == "staging.shrink_warning"
    assert target.read_text(encoding="utf-8") == original


def test_tool_schema_requires_patch_mode_and_describes_working_directory() -> None:
    patch = _schema("apply_patch")
    edit = patch["parameters"]["properties"]["edits"]["items"]
    assert "mode" in edit["required"]
    assert edit["properties"]["mode"]["enum"] == ["replace_file", "replace_exact"]
    assert "replacements" not in patch["parameters"]["properties"]
    working_directory = _schema("run_command")["parameters"]["properties"]["working_directory"]
    assert "existing directory" in working_directory["description"]


def test_working_directory_has_a_stable_error_code() -> None:
    classified = classify_error(ValueError("Working directory is not a directory"))
    assert classified.code == "working_directory.invalid"
