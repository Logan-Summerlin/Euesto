from __future__ import annotations

import re
from typing import Any

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def discover_custom_capabilities(config: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Expose declarations without making them executable or granting permissions."""
    capabilities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in config.get("custom_tools") or ():
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").casefold()
        modes = [str(item) for item in raw.get("modes") or ()]
        if (
            name in seen
            or not _NAME.fullmatch(name)
            or not modes
            or any(mode not in {"plan", "agent"} for mode in modes)
        ):
            continue
        seen.add(name)
        capabilities.append(
            {
                "name": name,
                "kind": "custom_tool",
                "description": str(raw.get("description") or "")[:1000],
                "modes": modes,
                "requires_approval": True,
                "custom": True,
                "executable": False,
                "status": "declared_unavailable",
            }
        )
    return tuple(sorted(capabilities, key=lambda item: item["name"]))
