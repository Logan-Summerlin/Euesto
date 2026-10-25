from shared.tools import EXECUTOR_TOOLS, MUTATION_TOOLS, PLAN_EXECUTOR_TOOLS, ToolRequest

from .errors import PERMISSION_DENIED, ExecutorToolError


def enforce_capability(request: ToolRequest) -> None:
    # Gateway tools (investigation, the plan, web access) never reach the executor.
    if request.tool not in EXECUTOR_TOOLS:
        raise ExecutorToolError(PERMISSION_DENIED, "Unknown capability")
    if request.mode == "plan" and (
        request.tool in MUTATION_TOOLS or request.tool not in PLAN_EXECUTOR_TOOLS
    ):
        raise ExecutorToolError(PERMISSION_DENIED, "Plan mode has a hard mutation ban")
