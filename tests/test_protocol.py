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


def test_tool_argument_cap_is_derived_from_argument_carrying_hard_ceilings() -> None:
    from executor.config import ExecutorConfig
    from executor.tools.bash import MAX_ENV_VALUE_BYTES, MAX_ENV_VARS
    from shared.tools import MAX_TOOL_ARGUMENT_BYTES, MAX_TOOL_ARGUMENT_PAYLOAD_BYTES, TOOL_ARGUMENT_ENVELOPE_BYTES

    ceilings = ExecutorConfig.HARD_CEILINGS
    payloads = {
        "write": ceilings["max_write_bytes"],
        "bash": ceilings["max_command_bytes"] + ceilings["max_bash_stdin_bytes"] + MAX_ENV_VARS * MAX_ENV_VALUE_BYTES,
        "edit": ceilings["max_edit_result_bytes"],
    }
    assert MAX_TOOL_ARGUMENT_PAYLOAD_BYTES == max(payloads.values())
    assert MAX_TOOL_ARGUMENT_BYTES == MAX_TOOL_ARGUMENT_PAYLOAD_BYTES + TOOL_ARGUMENT_ENVELOPE_BYTES
    assert all(value + TOOL_ARGUMENT_ENVELOPE_BYTES <= MAX_TOOL_ARGUMENT_BYTES for value in payloads.values())


def test_tool_requests_at_hard_ceilings_pass_the_protocol_layer() -> None:
    from executor.config import ExecutorConfig
    from executor.tools.bash import MAX_ENV_VALUE_BYTES, MAX_ENV_VARS
    from shared.tools import ToolRequest

    ceilings = ExecutorConfig.HARD_CEILINGS
    # Escape-heavy content must not lose capacity to repr/JSON escaping.
    ToolRequest("w", "run", "write", "agent", {"path": "big.txt", "content": "\n" * ceilings["max_write_bytes"], "expected_sha256": "0" * 64, "create_parents": True})
    env = {f"VAR_{index}": "v" * MAX_ENV_VALUE_BYTES for index in range(MAX_ENV_VARS)}
    ToolRequest("b", "run", "bash", "agent", {"command": "\x01" * ceilings["max_command_bytes"], "stdin": "\t" * ceilings["max_bash_stdin_bytes"], "env": env, "working_directory": "src", "timeout_seconds": 900, "rollback_on_failure": False})
    half = ceilings["max_edit_result_bytes"] // 2
    ToolRequest("e", "run", "edit", "agent", {"path": "big.txt", "old_str": "é" * (half // 2), "new_str": "x" * half, "expected_occurrences": 1, "expected_sha256": "0" * 64})


def test_tool_requests_above_the_argument_cap_are_rejected() -> None:
    from shared.tools import MAX_TOOL_ARGUMENT_BYTES, ToolRequest, tool_argument_bytes

    content = "x" * MAX_TOOL_ARGUMENT_BYTES
    assert tool_argument_bytes({"content": content}) > MAX_TOOL_ARGUMENT_BYTES
    with pytest.raises(ValueError, match="too large"):
        ToolRequest("w", "run", "write", "agent", {"path": "big.txt", "content": content})
    with pytest.raises(ValueError, match="too large"):
        ToolRequest("w", "run", "write", "agent", {"path": "big.txt", "content": ["x" * 1_000_000] * 18})


def test_tool_argument_bytes_measures_unescaped_utf8_json() -> None:
    import json

    from shared.tools import tool_argument_bytes

    arguments = {"path": "a/b.py", "content": "héllo\nworld", "flag": True, "count": 3, "items": ["x", None]}
    compact = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    unescaped = compact.replace("\\n", "\n")
    assert tool_argument_bytes(arguments) == len(unescaped.encode("utf-8"))
