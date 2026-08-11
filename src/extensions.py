from __future__ import annotations

from pathlib import Path

from server.extensions.skills import MAX_REFERENCE_BYTES, Skill, discover_skills


def available_skills(global_root: Path, workspace: Path | None) -> list[Skill]:
    by_name: dict[str, Skill] = {
        skill.name: skill for skill in discover_skills(global_root, scope="global")
    }
    if workspace:
        workspace_root = workspace / ".local-chat" / "skills"
        for skill in discover_skills(workspace_root, scope="workspace"):
            # A workspace skill deliberately shadows a global skill with the same name.
            by_name[skill.name] = skill
    return [by_name[name] for name in sorted(by_name)]


def load_selected_skills(
    global_root: Path,
    workspace: Path | None,
    selected: list[str],
) -> list[dict[str, object]]:
    skills = {skill.name: skill for skill in available_skills(global_root, workspace)}
    loaded: list[dict[str, object]] = []
    for name in sorted(set(selected)):
        skill = skills.get(name)
        if skill is None:
            continue
        instructions = skill.instructions
        root = global_root if skill.scope == "global" else workspace / ".local-chat" / "skills"
        for reference in skill.references:
            path = root / reference
            try:
                resolved_root = root.resolve(strict=True)
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink > 1:
                    continue
                if not path.resolve(strict=True).is_relative_to(resolved_root):
                    continue
                raw = path.read_bytes()
                if len(raw) > MAX_REFERENCE_BYTES or b"\x00" in raw:
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            instructions += f"\n\nREFERENCE {reference}:\n{text}"
        data = skill.to_dict()
        data["instructions"] = instructions
        loaded.append(data)
    return loaded
