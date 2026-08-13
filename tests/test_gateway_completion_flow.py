from shared.events import EventEnvelope
from src.gateway_client import _map_event


def test_gateway_maps_final_model_delta_and_completion_events() -> None:
    delta = EventEnvelope(
        event_id=10,
        run_id="run-1",
        type="model.delta",
        created_at="2026-01-01T00:00:00+00:00",
        payload={"text": "Task complete."},
    )
    completed = EventEnvelope(
        event_id=11,
        run_id="run-1",
        type="run.completed",
        created_at="2026-01-01T00:00:01+00:00",
        payload={"iterations": 1},
    )

    mapped_delta = _map_event(delta, run_id="run-1")
    mapped_completed = _map_event(completed, run_id="run-1")

    assert mapped_delta is not None
    assert mapped_delta.text == "Task complete."
    assert mapped_delta.done is False
    assert mapped_completed is not None
    assert mapped_completed.done is True
