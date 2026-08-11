from __future__ import annotations

import hashlib
from pathlib import Path

from executor.errors import classify_error
from executor.tools.apply_patch import apply_patch
from server.openrouter.agent import LOCAL_TOOL_SCHEMAS


def _schema(name: str) -> dict:
    return next(item["function"] for item in LOCAL_TOOL_SCHEMAS if item["function"]["name"] == name)


def test_apply_patch_accepts_a_single_edit_object(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    _output, result = apply_patch(
        tmp_path,
        {
            "edits": {
                "path": "value.txt",
                "expected_sha256": digest,
                "mode": "replace_file",
                "content": "after",
            }
        },
        max_bytes=1_000,
    )

    assert target.read_text(encoding="utf-8") == "after"
    assert result["changed"][0]["path"] == "value.txt"


def test_apply_patch_schema_documents_single_edit_form() -> None:
    patch = _schema("apply_patch")
    description = patch["description"]
    assert "one edit object or an array" in description
    assert "mode=replace_file" in description
    assert "mode=replace_exact" in description
    assert "Example:" in description


def test_run_command_schema_explains_noninteractive_stdin() -> None:
    command = _schema("run_command")
    assert "non-interactively" in command["description"]
    assert "stdin" in command["description"]
    assert command["parameters"]["properties"]["stdin"]["maxLength"] == 256000


def test_run_command_argument_errors_have_a_stable_code() -> None:
    error = classify_error(
        ValueError("Invalid run_command argument 'arguments': must be an array of strings")
    )
    assert error.code == "command.invalid_arguments"
    assert "arguments" in error.message


def test_legacy_run_command_argument_error_gets_field_specific_guidance() -> None:
    error = classify_error(ValueError("Command arguments must be a bounded string array"))
    assert error.code == "command.invalid_arguments"
    assert "arguments" in error.message
    assert "array of strings" in error.message
