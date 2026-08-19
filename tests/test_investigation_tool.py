from shared.events import EventEnvelope
from shared.permissions import PermissionDecision, resolve_permission
from shared.tools import INVESTIGATION_TOOLS, READ_TOOLS, ToolRequest


def test_investigation_is_read_only_and_auto_safe():
    request = ToolRequest("r", "run", "investigate_repository", "agent", {"query": "find architecture"})
    assert request.tool in READ_TOOLS
    assert request.tool in INVESTIGATION_TOOLS
    assert resolve_permission(request, "workspace") == PermissionDecision.ALLOW_RUN


def test_nested_event_family_is_replayable():
    for event_type in ("subagent.started", "subagent.tool_call", "subagent.tool_result", "subagent.completed", "subagent.failed"):
        event = EventEnvelope(1, "run", event_type, "2025-01-01T00:00:00Z", {"parent_tool_call_id": "call"})
        assert EventEnvelope.from_dict(event.to_dict()).type == event_type
