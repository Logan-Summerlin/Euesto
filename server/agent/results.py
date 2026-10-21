"""Lean, model-facing plain text for executor tool results.

Each result is one header line plus a body: no JSON envelope, request ids, timings, null
fields, constant strings, hashes, or JSON escaping of file text. An error is one line with its
code and a corrective hint. Journaled results keep the full envelope; only the model's view is
built here. Every truncated result states its exact continuation.
"""

from __future__ import annotations

import re
from typing import Any

from shared.tools import ToolResult

SNIPPET_BYTES = 1_500
SNIPPET_LINES_PER_HUNK = 10
LISTING_LINE_CHARS = 240
SNIPPET_LINE_CHARS = 160
DIAGNOSTIC_BYTES = 1_200
# Header and outline room inside a read result; the content itself is clamped to the rest.
READ_OVERHEAD_BYTES = 3_000

ERROR_HINTS = {
    "staging.conflict": "the file changed since you last saw it; read it again, then retry",
    "staging.shrink_warning": (
        "read the whole file first (no range); a rewrite is then accepted as deliberate"
    ),
    "path.missing": "check the path with find or ls",
    "path.unsafe": "use a relative path inside the workspace; links are never followed",
    "path.invalid": "use a relative POSIX path inside the workspace",
    "path.invalid_type": "the target must be a regular file (or a directory for ls/find)",
    "file.invalid_utf8": "the file is binary or not UTF-8; inspect it with bash instead",
    "edit.no_match": "read the target range and copy old_str exactly",
    "edit.too_many_matches": (
        "add surrounding context to old_str or set expected_occurrences to the intended count"
    ),
    "edit.too_few_matches": "set expected_occurrences to the count found, or re-read the file",
    "edit.malformed_context": "old_str must be non-empty and differ from new_str",
    "limit.exceeded": "narrow the request: smaller ranges, fewer files, or split the work",
    "tool.timeout": "raise timeout_seconds or narrow the command",
    "permission.denied": "the user denied this call; do not retry it unchanged",
    "request.invalid_arguments": "fix the arguments as described and retry",
    "command.invalid_arguments": "fix the arguments as described and retry",
    "working_directory.invalid": "use an existing directory relative to the workspace root",
    "apply_patch.malformed": "each operation needs operation, path, and its own fields",
    "tool.invalid_request": "send arguments as one JSON object matching the tool schema",
}

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def format_tool_result(
    name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    *,
    bash_chars: int = 8_000,
) -> str:
    """The plain-text model view of one executor result (before the per-result byte cap)."""
    if not result.ok:
        return format_error(result.error_code, result.output, result.data)
    data = dict(result.data or {})
    formatter = _FORMATTERS.get(name)
    if formatter is None:
        return result.output
    if name == "bash":
        return _bash(data, bash_chars)
    return formatter(arguments, result.output, data)


def format_error(code: str | None, message: str, data: dict[str, Any] | None = None) -> str:
    code = code or "tool.failed"
    text = " ".join(str(message or "").split())[:600]
    hint = ERROR_HINTS.get(code)
    line = f"error {code}: {text}"
    if hint and hint not in text:
        line += f"; {hint}"
    diagnostics = _diagnostics(data or {})
    return f"{line}\n{diagnostics}" if diagnostics else line


def bound_text(text: str, cap: int) -> tuple[str, bool]:
    """Clip ``text`` to ``cap`` UTF-8 bytes, keeping head and tail; return (text, clipped)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, False
    omitted = len(encoded) - cap
    marker = f"\n… [{omitted} bytes omitted by the per-result bound] …\n"
    budget = max(0, cap - len(marker.encode("utf-8")))
    head = encoded[: budget // 2].decode("utf-8", errors="ignore")
    tail = encoded[len(encoded) - (budget - budget // 2) :].decode("utf-8", errors="ignore")
    return head + marker + tail, True


# -- per-tool formatters --------------------------------------------------------------------


def _read(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    path = data.get("path") or arguments.get("path")
    total = data.get("line_count")
    start, end = data.get("start_line"), data.get("end_line")
    if "offset" in arguments:
        header = (
            f"read {path} bytes {data.get('byte_offset', 0)}+{data.get('content_bytes', 0)}"
        )
        header += f" of {data.get('size_bytes', 0)}"
    else:
        header = f"read {path} lines {start}–{end}"
        if total is not None:
            header += f" of {total}"
    if data.get("truncated"):
        if data.get("next_start_line"):
            header += f" (truncated; continue with start_line={data['next_start_line']})"
        elif data.get("next_offset") is not None:
            header += f" (truncated; continue with offset={data['next_offset']})"
    elif data.get("range_clipped"):
        header += " (range ends at end of file)"
    body = output
    outline = data.get("outline")
    if data.get("truncated") and isinstance(outline, list) and outline:
        entries = "\n".join(
            f"{item.get('line')}: {str(item.get('text') or '')[:120]}"
            for item in outline
            if isinstance(item, dict)
        )
        body += f"\n[outline of {path}: top-level definitions by line]\n{entries}"
    return f"{header}\n{body}" if body else header


def _write(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    diff = data.get("diff") if isinstance(data.get("diff"), dict) else {}
    created = data.get("old_sha256") is None
    if created:
        header = f"write {data.get('path')}: created, {diff.get('added_lines', 0)} lines"
    else:
        header = (
            f"write {data.get('path')}: rewritten, +{diff.get('added_lines', 0)} "
            f"−{diff.get('removed_lines', 0)} lines"
        )
    return _mutation(header, [diff], data, snippets=not created)


def _edit(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    diff = data.get("diff") if isinstance(data.get("diff"), dict) else {}
    count = int(data.get("actual_occurrences") or 1)
    header = (
        f"edit {data.get('path')}: {count} replacement{'s' if count != 1 else ''}, "
        f"+{diff.get('added_lines', 0)} −{diff.get('removed_lines', 0)} lines"
    )
    if data.get("line_ending_adjustment"):
        header += f" (line endings adjusted: {data['line_ending_adjustment']})"
    return _mutation(header, [diff], data)


def _apply_patch(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    operations = [item for item in data.get("operations") or () if isinstance(item, dict)]
    paths = data.get("paths") or []
    lines = [f"apply_patch: {len(operations)} operations across {len(paths)} files"]
    diffs = []
    for item in operations:
        diff = item.get("diff") if isinstance(item.get("diff"), dict) else {}
        kind = item.get("operation")
        if kind == "delete":
            lines.append(f"  delete {item.get('path')}")
            continue
        if kind == "write" and item.get("created"):
            lines.append(f"  create {item.get('path')}: {diff.get('added_lines', 0)} lines")
            continue
        lines.append(
            f"  {kind} {item.get('path')}: +{diff.get('added_lines', 0)} "
            f"−{diff.get('removed_lines', 0)} lines"
        )
        diffs.append(diff)
    return _mutation("\n".join(lines), diffs, data)


def _mutation(
    header: str, diffs: list[dict[str, Any]], data: dict[str, Any], *, snippets: bool = True
) -> str:
    parts = [header]
    warnings = data.get("shrink_warnings") or (
        [data["shrink_details"]] if data.get("shrink_details") else []
    )
    for warning in warnings:
        if isinstance(warning, dict):
            parts.append(
                f"warning: {warning.get('path')} shrank from {warning.get('old_lines')} to "
                f"{warning.get('new_lines')} lines"
            )
    if snippets:
        snippet = numbered_snippet(diffs, SNIPPET_BYTES)
        if snippet:
            parts.append(snippet)
    status = data.get("workspace_status")
    if isinstance(status, dict):
        parts.append(_staged_summary(status))
    return "\n".join(parts)


def _staged_summary(status: dict[str, Any]) -> str:
    counts = [
        f"{len(status.get(key) or ())} {key}" for key in ("created", "modified", "deleted")
    ]
    line = "staged: " + ", ".join(counts)
    links = status.get("unpublishable_links") or ()
    if links:
        line += f"; {len(links)} unpublishable link(s)"
    return line


def numbered_snippet(diffs: list[dict[str, Any]], limit: int) -> str:
    """Numbered new-file lines around each change, from unified diffs; at most ``limit`` bytes.

    Added lines are marked ``+``; removed lines are counted in the header, not repeated.
    """
    out: list[str] = []
    used = 0
    for diff in diffs:
        text = str(diff.get("text") or "")
        number = 0
        shown = 0
        in_hunk = False
        for raw in text.splitlines():
            match = _HUNK.match(raw)
            if match:
                number = int(match.group(1))
                shown = 0
                in_hunk = True
                if out:
                    line = "  …"
                    if used + len(line) + 1 > limit:
                        return "\n".join(out)
                    out.append(line)
                    used += len(line) + 1
                continue
            if not in_hunk or raw.startswith(("---", "+++")) or raw.startswith("\\"):
                continue
            if raw.startswith("-"):
                continue
            marker = "+" if raw.startswith("+") else " "
            content = raw[1:] if raw[:1] in {"+", " "} else raw
            if shown < SNIPPET_LINES_PER_HUNK:
                line = f"{marker}{number:>5}| {content[:SNIPPET_LINE_CHARS]}"
                size = len(line.encode("utf-8")) + 1
                if used + size > limit:
                    out.append("  … [snippet bounded; use status with include_diffs]")
                    return "\n".join(out)
                out.append(line)
                used += size
                shown += 1
            number += 1
    return "\n".join(out)


def _grep(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    groups: dict[str, list[str]] = {}
    contexts = data.get("matches") if isinstance(data.get("matches"), list) else None
    if contexts:
        for item in contexts:
            if not isinstance(item, dict):
                continue
            lines = groups.setdefault(str(item.get("path")), [])
            number = int(item.get("line") or 0)
            before = list(item.get("context_before") or ())
            for offset, text in enumerate(before):
                lines.append(_clip(f"  {number - len(before) + offset}- {text}"))
            lines.append(_clip(f"  {number}: {item.get('text')}"))
            for offset, text in enumerate(item.get("context_after") or (), 1):
                lines.append(_clip(f"  {number + offset}- {text}"))
    else:
        for raw in output.splitlines():
            path, sep, rest = raw.partition(":")
            number, sep2, text = rest.partition(":")
            if not sep or not sep2:
                continue
            groups.setdefault(path, []).append(_clip(f"  {number}: {text}"))
    count = int(data.get("matches_returned") or 0)
    header = f"grep {arguments.get('query')!r}: {count} matches in {len(groups)} files"
    header += _continuation(data)
    if data.get("files_skipped_too_large"):
        header += f"; {data['files_skipped_too_large']} large files not scanned"
    if not count:
        return header
    body = []
    for path, lines in groups.items():
        body.append(path)
        body.extend(lines)
    return header + "\n" + "\n".join(body)


def _listing(tool: str):
    def render(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
        entries = []
        for raw in output.splitlines():
            kind, _, rest = raw.partition("\t")
            if rest:
                size, _, path = rest.partition("\t")
                entries.append(
                    _clip(f"{path}/" if kind == "directory" else f"{path}  {size} bytes")
                )
            else:
                entries.append(_clip(raw))
        target = arguments.get("path") or "."
        header = f"{tool} {target}: {len(entries)} entries" + _continuation(data)
        return header + ("\n" + "\n".join(entries) if entries else "")

    return render


def _status(arguments: dict[str, Any], output: str, data: dict[str, Any]) -> str:
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    total = int(data.get("total_known") or 0)
    header = (
        f"status: {counts.get('created', 0)} created, {counts.get('modified', 0)} modified, "
        f"{counts.get('deleted', 0)} deleted, {counts.get('permission_changes', 0)} mode "
        f"changes ({total} total)"
    )
    header += _continuation(data)
    lines = [header]
    for item in data.get("changes") or ():
        if not isinstance(item, dict):
            continue
        code = {"create": "A", "update": "M", "delete": "D"}.get(str(item.get("operation")))
        line = f"{code} {item.get('path')}"
        if item.get("mode_changed"):
            line += f" (mode {item.get('base_mode')} -> {item.get('staged_mode')})"
        lines.append(_clip(line))
    links = data.get("unpublishable_links") or ()
    if links:
        lines.append(f"unpublishable links (never published): {len(links)}")
        lines.extend(_clip(f"L {path}") for path in links[:50])
    conflicts = data.get("conflicts") or ()
    if conflicts:
        lines.append(f"host conflicts (publication blocked until resolved): {len(conflicts)}")
        lines.extend(
            _clip(f"C {item.get('path')}") for item in conflicts[:50] if isinstance(item, dict)
        )
    for item in data.get("diffs") or ():
        if not isinstance(item, dict):
            continue
        if item.get("text"):
            lines.append(str(item["text"]))
        elif item.get("kind") != "text":
            lines.append(f"[{item.get('path')}: {item.get('kind')}, no textual diff]")
    if data.get("diff_truncated"):
        lines.append("[diff budget exhausted; narrow paths or continue with cursor]")
    return "\n".join(lines)


def _bash(data: dict[str, Any], limit: int) -> str:
    exit_code = data.get("exit_code")
    header = f"bash exit {exit_code if exit_code is not None else 'none'}"
    if data.get("cancelled"):
        header += " · cancelled"
    if data.get("rolled_back"):
        header += f" · changes rolled back ({data.get('rollback_reason')})"
    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    header += (
        f" · stdout {int(data.get('stdout_bytes') or 0)} bytes"
        f" · stderr {int(data.get('stderr_bytes') or 0)} bytes"
    )
    log_path = data.get("log_path")
    if log_path:
        header += f" · full log {log_path}"
    parts = [header]
    reduction = str(data.get("reduction") or "").strip()
    remaining = limit
    if reduction:
        reduction = reduction[: max(0, limit // 2)]
        parts.append(reduction)
        remaining -= len(reduction)
    streams = [("stderr", stderr), ("stdout", stdout)]
    present = [(label, text) for label, text in streams if text.strip()]
    if present and remaining > 200:
        # stderr first; each stream gets what it needs, and the rest goes to the other.
        needs = [len(text) for _label, text in present]
        shares = _split_budget(needs, remaining - 40 * len(present))
        for (label, text), share in zip(present, shares, strict=True):
            excerpt = tail_weighted_excerpt(text, share, log_path)
            if len(present) > 1 or label == "stderr":
                parts.append(f"[{label}]")
            parts.append(excerpt)
    workspace = data.get("workspace_status")
    if isinstance(workspace, dict) and not data.get("rolled_back"):
        parts.append(_staged_summary(workspace))
    return "\n".join(parts)


def tail_weighted_excerpt(text: str, limit: int, log_path: object = None) -> str:
    """Keep about a quarter of ``limit`` from the head and the rest from the tail."""
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    head = max(0, limit // 4)
    tail = max(0, limit - head)
    where = f"; full log {log_path}" if log_path else ""
    return (
        text[:head]
        + f"\n… [{len(text) - head - tail} characters omitted{where}] …\n"
        + text[len(text) - tail :]
    )


def _split_budget(needs: list[int], budget: int) -> list[int]:
    budget = max(0, budget)
    shares = [0] * len(needs)
    remaining = budget
    order = sorted(range(len(needs)), key=lambda index: needs[index])
    for position, index in enumerate(order):
        fair = remaining // (len(needs) - position)
        shares[index] = min(needs[index], fair)
        remaining -= shares[index]
    return shares


def _continuation(data: dict[str, Any]) -> str:
    if not data.get("truncated"):
        return ""
    if data.get("next_cursor"):
        return f" (truncated; continue with cursor={data['next_cursor']})"
    reason = data.get("truncation_reason")
    if reason == "time_budget":
        return " (truncated by the time budget; narrow path or pattern)"
    if data.get("output_truncated"):
        return " (truncated by the output limit; narrow the query or path)"
    return " (truncated)"


def _diagnostics(data: dict[str, Any]) -> str:
    lines = []
    if data.get("match_lines"):
        lines.append("matches at lines: " + ", ".join(str(x) for x in data["match_lines"]))
    if data.get("closest_line"):
        lines.append(f"closest line: {data['closest_line']}")
    if data.get("context_preview"):
        lines.append(f"context: {data['context_preview']}")
    if data.get("failed_operation") is not None:
        lines.append(
            f"failed operation {data['failed_operation']} ({data.get('operation')} "
            f"{data.get('path')}); nothing was applied"
        )
    text = "\n".join(lines)
    return text[:DIAGNOSTIC_BYTES]


def _clip(line: str) -> str:
    return line if len(line) <= LISTING_LINE_CHARS else line[: LISTING_LINE_CHARS - 1] + "…"


_FORMATTERS = {
    "read": _read,
    "write": _write,
    "edit": _edit,
    "apply_patch": _apply_patch,
    "bash": _bash,
    "grep": _grep,
    "find": _listing("find"),
    "ls": _listing("ls"),
    "status": _status,
}
