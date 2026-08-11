import pytest

from src.gateway_client import GatewayConnection, _iter_sse


def test_gateway_connection_accepts_only_numeric_loopback() -> None:
    assert GatewayConnection("http://127.0.0.1:8765", "t" * 43).base_url.endswith("8765")
    assert GatewayConnection("http://[::1]:8765", "t" * 43).base_url.endswith("8765")
    for url in (
        "http://localhost:8765",
        "http://192.168.1.2:8765",
        "https://example.com",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
    ):
        with pytest.raises(ValueError):
            GatewayConnection(url, "t" * 43)


def test_desktop_sse_parser_handles_comments_and_multiline_data() -> None:
    events = list(
        _iter_sse(
            iter(
                [
                    ": keep-alive",
                    "",
                    'data: {"event_id":1,',
                    'data: "run_id":"r"}',
                    "",
                ]
            )
        )
    )
    assert events == [{"event_id": 1, "run_id": "r"}]
