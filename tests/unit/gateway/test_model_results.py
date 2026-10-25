"""Golden formats for the lean, plain-text model view of tool results (plan item 1.4)."""

from __future__ import annotations

from server.agent.context import _tool_summary
from server.agent.results import (
    bound_text,
    format_error,
    format_tool_result,
    numbered_snippet,
    tail_weighted_excerpt,
)
from shared.tools import ToolResult


def ok(data: dict, output: str = "") -> ToolResult:
    return ToolResult("req", True, output=output, data=data)


def test_read_header_states_range_total_and_exact_continuation() -> None:
    text = format_tool_result(
        "read",
        {"path": "src/app.py"},
        ok(
            {
                "path": "src/app.py",
                "start_line": 1,
                "end_line": 400,
                "line_count": 1832,
                "truncated": True,
                "next_start_line": 401,
                "sha256": "f" * 64,
                "outline": [
                    {"line": 12, "text": "class App:"},
                    {"line": 900, "text": "def main():"},
                ],
            },
            output="line one",
        ),
    )
    header, _, body = text.partition("\n")
    assert (
        header
        == "read src/app.py lines 1–400 of 1832 (truncated; continue with start_line=401)"
    )
    assert body.startswith("line one")
    assert "12: class App:" in body and "900: def main():" in body
    assert "f" * 64 not in text


def test_byte_offset_reads_continue_with_offset() -> None:
    text = format_tool_result(
        "read",
        {"path": "a.txt", "offset": 0},
        ok(
            {
                "path": "a.txt",
                "byte_offset": 0,
                "content_bytes": 10,
                "size_bytes": 99,
                "truncated": True,
                "next_offset": 10,
            },
            output="0123456789",
        ),
    )
    assert (
        text.splitlines()[0]
        == "read a.txt bytes 0+10 of 99 (truncated; continue with offset=10)"
    )


def test_edit_confirmation_is_a_numbered_snippet_without_hashes() -> None:
    diff = {
        "text": "--- a\n+++ a\n@@ -40,3 +40,4 @@\n     if not items:\n-        return None\n"
        "+        return []\n+    # done\n     tail",
        "added_lines": 2,
        "removed_lines": 1,
    }
    text = format_tool_result(
        "edit",
        {"path": "src/app.py"},
        ok(
            {
                "path": "src/app.py",
                "old_sha256": "a" * 64,
                "new_sha256": "b" * 64,
                "actual_occurrences": 1,
                "diff": diff,
                "workspace_status": {"created": [], "modified": ["src/app.py"], "deleted": []},
            }
        ),
    )
    assert text.splitlines() == [
        "edit src/app.py: 1 replacement, +2 −1 lines",
        "    40|     if not items:",
        "+   41|         return []",
        "+   42|     # done",
        "    43|     tail",
        "staged: 0 created, 1 modified, 0 deleted",
    ]
    assert "a" * 64 not in text and "b" * 64 not in text


def test_snippets_are_capped_per_call() -> None:
    body = "\n".join(f"+line {index} " + "x" * 100 for index in range(200))
    snippet = numbered_snippet([{"text": f"@@ -1,0 +1,200 @@\n{body}"}] * 5, 1_500)
    assert len(snippet.encode()) <= 1_600


def test_bash_leads_with_reduction_then_stderr_first_tail_weighted() -> None:
    text = format_tool_result(
        "bash",
        {"command": "pytest"},
        ok(
            {
                "exit_code": 1,
                "stdout": "s" * 20_000 + "STDOUT-TAIL",
                "stderr": "E" * 50 + "STDERR-TAIL",
                "stdout_bytes": 20_011,
                "stderr_bytes": 61,
                "log_path": ".euesto-output/req.log",
                "reduction": "pytest: 1 failed, 3 passed\nFAILED tests/test_a.py::test_x - assert 1 == 2",
                "rolled_back": True,
                "rollback_reason": "nonzero_exit",
            }
        ),
        bash_chars=8_000,
    )
    lines = text.splitlines()
    assert lines[0] == (
        "bash exit 1 · changes rolled back (nonzero_exit) · stdout 20011 bytes · stderr 61 bytes"
        " · full log .euesto-output/req.log"
    )
    assert lines[1] == "pytest: 1 failed, 3 passed"
    assert text.index("[stderr]") < text.index("[stdout]")
    assert text.rstrip().endswith("STDOUT-TAIL") and "STDERR-TAIL" in text
    assert len(text) <= 8_400


def test_tail_weighted_excerpt_states_where_the_rest_is() -> None:
    excerpt = tail_weighted_excerpt("h" * 100 + "t" * 1_000, 400, ".euesto-output/r.log")
    assert excerpt.startswith("h" * 100) and excerpt.endswith("t" * 300)
    assert "full log .euesto-output/r.log" in excerpt


def test_grep_groups_by_path_and_clips_long_lines() -> None:
    output = "src/a.py:3:import os\nsrc/a.py:9:x = 1\nsrc/b.py:1:" + "y" * 600
    text = format_tool_result(
        "grep",
        {"query": "x"},
        ok({"matches_returned": 3, "truncated": True, "next_cursor": "Mw"}, output=output),
    )
    lines = text.splitlines()
    assert lines[0] == "grep 'x': 3 matches in 2 files (truncated; continue with cursor=Mw)"
    assert lines[1:4] == ["src/a.py", "  3: import os", "  9: x = 1"]
    assert lines[4] == "src/b.py" and len(lines[5]) == 240


def test_listing_and_status_are_one_line_per_entry() -> None:
    listing = format_tool_result(
        "ls",
        {"path": "src"},
        ok({"truncated": False}, output="directory\t-\tsrc/pkg\nfile\t12\tsrc/a.py"),
    )
    assert listing.splitlines() == ["ls src: 2 entries", "src/pkg/", "src/a.py  12 bytes"]
    status = format_tool_result(
        "status",
        {},
        ok(
            {
                "counts": {"created": 1, "modified": 0, "deleted": 0, "permission_changes": 0},
                "total_known": 1,
                "changes": [{"path": "new.py", "operation": "create"}],
                "unpublishable_links": ["env/bin/python"],
            }
        ),
    )
    assert status.splitlines() == [
        "status: 1 created, 0 modified, 0 deleted, 0 mode changes (1 total)",
        "A new.py",
        "unpublishable links (never published): 1",
        "L env/bin/python",
    ]


def test_errors_are_one_line_with_a_corrective_hint() -> None:
    result = ToolResult(
        "req", False, output="Staging hash conflict: a.py", error_code="staging.conflict"
    )
    assert format_tool_result("edit", {}, result) == (
        "error staging.conflict: Staging hash conflict: a.py; the file changed since you last "
        "saw it; read it again, then retry"
    )
    assert format_error("edit.no_match", "Edit occurrence conflict", {"closest_line": 7}) == (
        "error edit.no_match: Edit occurrence conflict; read the target range and copy old_str "
        "exactly\nclosest line: 7"
    )


def test_bound_text_keeps_head_and_tail() -> None:
    text, clipped = bound_text("a" * 30_000 + "END", 24_000)
    assert clipped and len(text.encode()) <= 24_000 and text.endswith("END")


def test_compaction_keeps_the_plain_text_header() -> None:
    summary = _tool_summary("read a.py lines 1–9 of 9\n" + "z" * 5_000)
    assert summary.startswith("read a.py lines 1–9 of 9\n[compacted] ")
    assert len(summary) < 1_000
