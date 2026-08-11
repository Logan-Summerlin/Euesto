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
    return f"""
    <style>
      body {{ font-family: 'Segoe UI'; font-size: 10.5pt; line-height: 1.45; }}
      p {{ margin: 0 0 10px 0; }}
      pre {{ background: #111827; color: #e5e7eb; padding: 12px;
             border-radius: 7px; white-space: pre-wrap; }}
      code {{ font-family: 'Cascadia Code', Consolas, monospace; }}
      blockquote {{ border-left: 3px solid #64748b; margin-left: 0;
                    padding-left: 12px; color: #64748b; }}
      table {{ border-collapse: collapse; }}
      th, td {{ border: 1px solid #64748b; padding: 5px 8px; }}
      a {{ color: #4f7cff; }}
    </style>{safe}
    """
