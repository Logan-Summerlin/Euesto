from __future__ import annotations

from shared.requests import DEFAULT_INVESTIGATION_MODEL, AgentRunRequest


def test_agent_requests_have_a_default_investigation_model() -> None:
    request = AgentRunRequest.from_dict(
        {
            "model": "vendor/model",
            "mode": "agent",
            "workspace_id": "workspace",
            "messages": [{"role": "user", "content": "inspect the repository"}],
        }
    )
    assert request.investigation_model_id == DEFAULT_INVESTIGATION_MODEL


def test_blank_investigation_model_uses_the_safe_default() -> None:
    request = AgentRunRequest.from_dict(
        {
            "model": "vendor/model",
            "mode": "agent",
            "workspace_id": "workspace",
            "messages": [{"role": "user", "content": "inspect the repository"}],
            "investigation_model_id": "",
        }
    )
    assert request.investigation_model_id == DEFAULT_INVESTIGATION_MODEL
