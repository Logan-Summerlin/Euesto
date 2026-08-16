from __future__ import annotations

from pathlib import Path

import pytest

from executor.config import ExecutorConfig


def _config(tmp_path: Path, **overrides: int) -> ExecutorConfig:
    values = {
        "source_root": tmp_path / "source",
        "work_root": tmp_path / "work",
        "socket_path": tmp_path / "executor.sock",
        "token": "x" * 32,
        "workspace_id": "test-workspace",
    }
    values.update(overrides)
    return ExecutorConfig(**values)


def test_coding_profile_defaults_are_explicit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.max_read_bytes == 1_000_000
    assert config.max_write_bytes == 1_000_000
    assert config.max_edit_target_bytes == 2_000_000
    assert config.max_edit_result_bytes == 2_000_000
    assert config.max_bash_output_bytes == 1_000_000
    assert config.max_bash_stdin_bytes == 1_000_000
    assert config.max_command_seconds == 300
    assert config.max_search_results == 500
    assert config.max_staging_bytes == 2_500_000_000
    assert config.max_checkpoint_bytes == 2_500_000_000
    assert config.work_capacity_bytes == 8_000_000_000
    assert config.required_capacity_bytes == 6_000_000_000


def test_every_limit_is_a_positive_integer(tmp_path: Path) -> None:
    for name in ExecutorConfig.HARD_CEILINGS:
        with pytest.raises(ValueError, match="positive integers"):
            _config(tmp_path, **{name: 0})
        with pytest.raises(ValueError, match="positive integers"):
            _config(tmp_path, **{name: -1})


def test_limits_cannot_exceed_hard_ceilings(tmp_path: Path) -> None:
    for name, ceiling in ExecutorConfig.HARD_CEILINGS.items():
        with pytest.raises(ValueError, match="hard ceilings"):
            _config(tmp_path, **{name: ceiling + 1})


def test_checkpoint_and_staging_limits_are_consistent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cover the maximum staged content"):
        _config(tmp_path, max_checkpoint_bytes=500, max_staging_bytes=501)
    with pytest.raises(ValueError, match="fit strictly below"):
        _config(
            tmp_path,
            max_checkpoint_bytes=3_500_000_000,
            max_staging_bytes=3_500_000_000,
        )


def test_effective_limit_reports_requested_configured_and_hard_values(tmp_path: Path) -> None:
    config = _config(tmp_path, max_read_bytes=1_000_000)
    status = config.limit_status("max_read_bytes", requested=2_000_000)
    assert status == {
        "requested": 2_000_000,
        "configured": 1_000_000,
        "hard_ceiling": 8_000_000,
        "effective": 1_000_000,
        "source": "constructor",
    }
    assert config.effective_limit("max_read_bytes", 100_000) == 100_000


def test_environment_profile_and_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("x" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "env-workspace")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "small")
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "123456")

    config = ExecutorConfig.from_environment()
    assert config.workspace_id == "env-workspace"
    assert config.max_read_bytes == 123_456
    assert config.max_write_bytes == 256_000
    assert config.max_staging_bytes == 512_000_000
    assert config.max_checkpoint_bytes == 512_000_000
    assert config.sources["max_read_bytes"] == "environment:LOCAL_CHAT_MAX_READ_BYTES"
    assert config.sources["max_write_bytes"] == "profile:small"


def test_invalid_environment_values_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("x" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "env-workspace")
    monkeypatch.setenv("LOCAL_CHAT_MAX_READ_BYTES", "not-an-int")
    with pytest.raises(ValueError, match="LOCAL_CHAT_MAX_READ_BYTES"):
        ExecutorConfig.from_environment()


def test_unknown_profile_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("x" * 32, encoding="utf-8")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE_ID", "env-workspace")
    monkeypatch.setenv("LOCAL_CHAT_EXECUTOR_PROFILE", "unbounded")
    with pytest.raises(ValueError, match="Unknown executor profile"):
        ExecutorConfig.from_environment()


def test_runtime_limit_status_contains_all_limit_sources(tmp_path: Path) -> None:
    config = _config(tmp_path)
    status = config.limits_status()
    assert set(ExecutorConfig.HARD_CEILINGS).issubset(status)
    for name in ExecutorConfig.HARD_CEILINGS:
        values = status[name]
        assert values["configured"] == getattr(config, name)
        assert values["hard_ceiling"] == ExecutorConfig.HARD_CEILINGS[name]
        assert values["effective"] == getattr(config, name)
        assert values["source"] == "constructor"
    assert status["required_temp_headroom_bytes"]["configured"] == 1_000_000_000
