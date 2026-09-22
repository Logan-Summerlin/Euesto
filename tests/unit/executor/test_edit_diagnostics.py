from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from executor.errors import ExecutorToolError
from executor.tools.edit import edit

LIMITS = {"max_target_bytes": 2_000_000, "max_result_bytes": 2_000_000}


def _file(tmp_path: Path, content: bytes, name: str = "f.txt") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_lf_old_str_matches_a_crlf_file_and_keeps_its_convention(tmp_path: Path) -> None:
    path = _file(tmp_path, b"first\r\nsecond\r\nthird\r\n")
    output, data = edit(tmp_path, {"path": "f.txt", "old_str": "first\nsecond\n", "new_str": "one\ntwo\nextra\n"}, **LIMITS)
    assert path.read_bytes() == b"one\r\ntwo\r\nextra\r\nthird\r\n"
    assert data["line_ending_adjustment"] == "lf_to_crlf"
    assert "line-ending translation" in output


def test_crlf_old_str_matches_an_lf_file(tmp_path: Path) -> None:
    path = _file(tmp_path, b"a\nb\nc\n")
    _, data = edit(tmp_path, {"path": "f.txt", "old_str": "a\r\nb\r\n", "new_str": "A\r\nB\r\n"}, **LIMITS)
    assert path.read_bytes() == b"A\nB\nc\n"
    assert data["line_ending_adjustment"] == "crlf_to_lf"


def test_exact_matches_never_translate_and_counts_still_bind(tmp_path: Path) -> None:
    path = _file(tmp_path, b"x\r\ny\nx\r\ny\n")
    _, data = edit(tmp_path, {"path": "f.txt", "old_str": "x\r\ny", "new_str": "z", "expected_occurrences": 2}, **LIMITS)
    assert path.read_bytes() == b"z\nz\n" and data["line_ending_adjustment"] is None
    _file(tmp_path, b"k\r\nk\r\n", "g.txt")
    with pytest.raises(ExecutorToolError) as raised:
        edit(tmp_path, {"path": "g.txt", "old_str": "k\n", "new_str": "m\n"}, **LIMITS)
    assert raised.value.code == "edit.too_many_matches"
    assert (tmp_path / "g.txt").read_bytes() == b"k\r\nk\r\n"


def test_zero_match_diagnostics_are_bounded_and_escaped(tmp_path: Path) -> None:
    body = "def handler(event):\n    value = event['x']\n    return value\n" + "# filler\n" * 200
    path = _file(tmp_path, body.encode())
    with pytest.raises(ExecutorToolError) as raised:
        edit(tmp_path, {"path": "f.txt", "old_str": "def handler(event):\n  value = event['x']", "new_str": "pass"}, **LIMITS)
    error = raised.value
    assert error.code == "edit.no_match"
    assert "expected 1, found 0" in error.message
    details = error.details
    assert details["failure"] == "zero_matches" and details["actual_occurrences"] == 0
    assert details["closest_line"] == 1
    assert details["context_preview"][0] in {"'", '"'} and len(details["context_preview"]) <= 242
    assert any("whitespace" in hint for hint in details["hints"])
    assert path.read_bytes() == body.encode()


def test_too_many_and_too_few_matches_report_line_numbers(tmp_path: Path) -> None:
    _file(tmp_path, b"".join(b"item\n" if index % 3 == 0 else b"other\n" for index in range(60)))
    with pytest.raises(ExecutorToolError) as raised:
        edit(tmp_path, {"path": "f.txt", "old_str": "item", "new_str": "x"}, **LIMITS)
    assert raised.value.code == "edit.too_many_matches"
    assert raised.value.details["match_lines"] == [1, 4, 7, 10, 13, 16, 19, 22, 25, 28]
    with pytest.raises(ExecutorToolError) as raised:
        edit(tmp_path, {"path": "f.txt", "old_str": "item", "new_str": "x", "expected_occurrences": 30}, **LIMITS)
    assert raised.value.code == "edit.too_few_matches" and raised.value.details["actual_occurrences"] == 20


def test_hash_conflict_and_malformed_context_are_distinct_failures(tmp_path: Path) -> None:
    path = _file(tmp_path, b"alpha\n")
    with pytest.raises(ExecutorToolError) as raised:
        edit(tmp_path, {"path": "f.txt", "old_str": "alpha", "new_str": "beta", "expected_sha256": "0" * 64}, **LIMITS)
    assert raised.value.code == "staging.conflict"
    assert raised.value.details == {"failure": "hash_conflict", "path": "f.txt", "expected_sha256": "0" * 64, "actual_sha256": hashlib.sha256(b"alpha\n").hexdigest()}
    for arguments in ({"old_str": "", "new_str": "x"}, {"old_str": "a\x00", "new_str": "x"}, {"old_str": "alpha", "new_str": "x", "expected_occurrences": 0}):
        with pytest.raises(ExecutorToolError) as raised:
            edit(tmp_path, {"path": "f.txt", **arguments}, **LIMITS)
        assert raised.value.code == "edit.malformed_context"
    assert path.read_bytes() == b"alpha\n"
