"""Framework-neutral desktop/gateway protocol models."""

from .permissions import PermissionDecision, PermissionRule, resolve_permission
from .protocol import GATEWAY_VERSION, PROTOCOL_VERSION, protocol_is_compatible
from .tools import PublishManifest, PublishOperation, ToolRequest, ToolResult

__all__ = [
    "GATEWAY_VERSION", "PROTOCOL_VERSION", "protocol_is_compatible",
    "PermissionDecision", "PermissionRule", "resolve_permission",
    "PublishManifest", "PublishOperation", "ToolRequest", "ToolResult",
]
