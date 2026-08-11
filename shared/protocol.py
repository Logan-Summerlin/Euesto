from __future__ import annotations

GATEWAY_VERSION = "1.0.0"
PROTOCOL_VERSION = "2.0"
EVENT_SCHEMA_VERSION = 1


def protocol_major(version: str) -> int:
    """Return a strict numeric protocol major or fail closed."""
    head, separator, _tail = version.partition(".")
    if not separator or not head.isascii() or not head.isdigit():
        raise ValueError("Protocol versions must use major.minor numeric form")
    return int(head)


def protocol_is_compatible(version: str) -> bool:
    try:
        return protocol_major(version) == protocol_major(PROTOCOL_VERSION)
    except (TypeError, ValueError):
        return False
