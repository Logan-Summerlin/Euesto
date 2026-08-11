from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from shared.tools import TOOL_NAMES

MAX_SKILL_BYTES = 128_000
MAX_REFERENCE_BYTES = 64_000
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    instructions: str
    required_tools: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    scope: str = "global"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["required_tools"] = list(self.required_tools)
        data["references"] = list(self.references)
        return data


def discover_skills(root: Path, *, scope: str = "global") -> list[Skill]:
    if scope not in {"global", "workspace"} or not root.is_dir():
        return []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    skills: list[Skill] = []
    for path in sorted(root.glob("*.md"), key=lambda item: item.name.casefold()):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
                continue
            if not path.resolve(strict=True).is_relative_to(resolved_root):
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_SKILL_BYTES or b"\x00" in raw:
                continue
            skill = parse_skill(raw.decode("utf-8"), scope=scope)
        except (OSError, UnicodeError, ValueError):
            continue
        skills.append(skill)
    return skills


def parse_skill(text: str, *, scope: str) -> Skill:
    metadata, instructions = _frontmatter(text)
    name = metadata.get("name", "").strip().casefold()
    description = metadata.get("description", "").strip()
    if not _NAME.fullmatch(name) or not description or not instructions.strip():
        raise ValueError("Skill requires a safe name, description, and instructions")
    required = tuple(_csv(metadata.get("required_tools", "")))
    if any(tool not in TOOL_NAMES for tool in required):
        raise ValueError("Skill requires an unavailable tool")
    references = tuple(_safe_reference(item) for item in _csv(metadata.get("references", "")))
    return Skill(name, description[:1000], instructions.strip(), required, references, scope)


def render_skill_context(skills: tuple[dict[str, object], ...], available_tools: set[str]) -> str:
    rendered: list[str] = []
    seen: set[str] = set()
    for raw in sorted(skills, key=lambda item: str(item.get("name") or "")):
        name = str(raw.get("name") or "").casefold()
        description = str(raw.get("description") or "")
        instructions = str(raw.get("instructions") or "")
        scope = str(raw.get("scope") or "")
        required = tuple(str(item) for item in raw.get("required_tools") or ())
        if name in seen or not _NAME.fullmatch(name) or scope not in {"global", "workspace"}:
            raise ValueError("Malformed or duplicate skill")
        if any(tool not in available_tools for tool in required):
            raise ValueError(f"Skill {name} requires unavailable tools")
        if not description or not instructions or len(instructions.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError("Malformed skill content")
        seen.add(name)
        rendered.append(
            f"SKILL [{scope}] {name}\nDescription: {description}\n"
            f"Required tools: {', '.join(required) or 'none'}\n{instructions}"
        )
    return "\n\n".join(rendered)


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("Skill must begin with YAML-like frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Skill frontmatter is not closed")
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator or key.strip() in metadata:
            raise ValueError("Malformed skill frontmatter")
        metadata[key.strip()] = value.strip()
    return metadata, text[end + 5 :]


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _safe_reference(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value or "\\" in value:
        raise ValueError("Unsafe skill reference")
    return path.as_posix()
