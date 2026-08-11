import pytest

from shared.events import EventEnvelope
from shared.protocol import PROTOCOL_VERSION, protocol_is_compatible, protocol_major
from shared.requests import ChatRequest


def test_protocol_compatibility_fails_closed() -> None:
    assert protocol_is_compatible(PROTOCOL_VERSION)
    assert protocol_is_compatible("2.99")
    assert not protocol_is_compatible("1.99")
    assert not protocol_is_compatible("3.0")
    assert not protocol_is_compatible("garbage")
    with pytest.raises(ValueError):
        protocol_major("1")


def test_protocol_rejects_unknown_fields_events_and_agent_mode() -> None:
    with pytest.raises(ValueError, match="Unknown event fields"):
        EventEnvelope.from_dict(
            {
                "event_id": 1,
                "run_id": "run",
                "type": "run.created",
                "created_at": "2026-01-01T00:00:00Z",
                "schema_version": 1,
                "payload": {},
                "surprise": True,
            }
        )
    with pytest.raises(ValueError, match="Unknown event type"):
        EventEnvelope(1, "run", "model.invented", "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="Chat mode only"):
        ChatRequest.from_dict(
            {
                "mode": "agent",
                "model": "vendor/model",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
