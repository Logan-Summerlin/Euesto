from __future__ import annotations

import bleach
import markdown

ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p",
    "pre",
    "code",
    "blockquote",
    "hr",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "del",
}


def render_markdown(source: str) -> str:
    rendered = markdown.markdown(
        source,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html",
    )
    safe = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    # Shared styling is supplied by the QML transcript; avoid a per-message
    # document stylesheet, which creates unnecessary rich-text/font variation.
    return safe
