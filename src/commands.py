from __future__ import annotations


def expand_prompt_command(template: str, arguments: str) -> str:
    value = arguments.strip()
    return template.replace("{{args}}", value).replace("$ARGUMENTS", value).strip()
