from shared.tools import MUTATION_TOOLS, TOOL_NAMES, ToolRequest


def enforce_capability(request: ToolRequest) -> None:
    if request.tool not in TOOL_NAMES:
        raise PermissionError("Unknown capability")
    if request.mode == "plan" and request.tool in MUTATION_TOOLS:
        raise PermissionError("Plan mode has a hard mutation ban")
