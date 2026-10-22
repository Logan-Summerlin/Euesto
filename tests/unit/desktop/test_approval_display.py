"""Approval prompts (harness plan 4.7, 4.3, 4.5, 5.4): what the user sees before deciding."""

from __future__ import annotations

import hashlib

from src.approval_display import approval_display, bash_html, publication_summary


def _create(path: str, text: str) -> dict[str, object]:
    return {
        "path": path,
        "operation": "create",
        "base_sha256": None,
        "staged_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
    }


def test_bash_prompt_shows_purpose_directory_timeout_and_network() -> None:
    summary, detail = approval_display(
        "tool",
        "bash",
        {"command": "pytest -q tests", "working_directory": "pkg", "timeout_seconds": 120},
        context={"description": "Run the unit tests", "network": "registries"},
    )
    assert "Purpose (from the model): Run the unit tests" in summary
    assert "Command: pytest -q tests" in summary
    assert "Working directory: /work/pkg" in summary and "Timeout: 120 seconds" in summary
    assert "allowlisted registries" in summary
    assert '"command": "pytest -q tests"' in detail

    offline, _detail = approval_display("tool", "bash", {"command": "ls"}, context={})
    assert "Network: none" in offline and "Timeout: 60 seconds" in offline
    assert "Purpose" not in offline


def test_bash_highlighting_escapes_markup() -> None:
    rendered = bash_html('echo "<b>hi</b>" && rm -rf $HOME')
    assert "<b>hi</b>" not in rendered and "&lt;b&gt;hi&lt;/b&gt;" in rendered
    assert rendered.startswith("<code>") and rendered.endswith("</code>")
    assert ">echo</font>" in rendered and ">rm</font>" in rendered  # commands after &&
    assert ">$HOME</font>" in rendered and ">-rf</font>" in rendered


def test_url_prompt_names_the_full_url_and_the_reason() -> None:
    summary, _detail = approval_display(
        "url",
        "fetch_url",
        {"url": "https://example.com/a?b=1"},
        context={"url": "https://example.com/a?b=1", "approval_reason": "the URL has a query"},
    )
    assert "https://example.com/a?b=1" in summary and "the URL has a query" in summary


def test_publication_lists_flagged_files_first_and_warns_about_secrets() -> None:
    manifest = {
        "operations": [
            _create("src/app.py", "print('ok')\n"),
            _create(".github/workflows/ci.yml", "on: push\n"),
            _create("config/settings.py", 'KEY = "AKIAIOSFODNN7EXAMPLE"\n'),
        ],
        "batch_index": 1,
        "batch_count": 2,
    }
    summary = publication_summary(manifest)
    lines = summary.splitlines()
    assert lines[0] == "Publish approval (batch 1 of 2): 3 file operation(s)"
    assert lines[1].startswith("Needs explicit confirmation")
    assert lines[2] == "create: .github/workflows/ci.yml [runs on the host]"
    assert any(
        line.startswith("Secret warning: config/settings.py would write") for line in lines
    )
    plain = publication_summary({"operations": [_create("src/app.py", "x\n")]})
    assert "Needs explicit confirmation" not in plain and "Secret warning" not in plain
