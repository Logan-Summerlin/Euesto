from shared.tools import MUTATION_TOOLS, TOOL_NAMES, ToolRequest

from .errors import PERMISSION_DENIED, ExecutorToolError


def enforce_capability(request: ToolRequest) -> None:
    if request.tool not in TOOL_NAMES:
        raise ExecutorToolError(PERMISSION_DENIED, "Unknown capability")
    if request.mode == "plan" and request.tool in MUTATION_TOOLS:
        raise ExecutorToolError(PERMISSION_DENIED, "Plan mode has a hard mutation ban")
