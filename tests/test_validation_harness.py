from __future__ import annotations

import subprocess
from pathlib import Path

import scripts.validate as validate


def test_validation_commands_use_explicit_tiers_and_active_python() -> None:
    commands = validate.commands("all")
    assert commands
    assert [name for name, _ in commands] == ["fast", "slow", "ruff", "compile", "qml", "docker"]
    assert commands[0][1][:3] == [validate.sys.executable, "-m", "pytest"]
    assert commands[0][1][-1] == "not slow and not docker"
    assert validate.commands("slow")[0][1][-1] == "slow and not docker"


def test_validation_environment_is_qt_offscreen_and_does_not_leak_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-export")
    env = validate._safe_env()
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert env["QT_QUICK_BACKEND"] == "software"
    assert "OPENROUTER_API_KEY" not in env


def test_missing_and_failed_commands_are_distinguished() -> None:
    report: dict = {"checks": []}

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    assert validate.execute("qml", report=report, runner=missing) == 1
    assert report["checks"][0]["status"] == "unavailable"

    report = {"checks": []}

    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 3)

    assert validate.execute("qml", report=report, runner=failed) == 3
    assert report["checks"][0]["status"] == "failed"


def test_pytest_empty_tier_is_explicit_and_requires_allowance() -> None:
    def empty(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 5)

    assert validate.execute("slow", runner=empty) == 1
    assert validate.execute("slow", allow_unavailable=True, runner=empty) == 0
    report: dict = {"checks": []}
    validate.execute("slow", report=report, runner=empty)
    assert report["checks"][0]["status"] == "empty"


def test_preflight_has_bounded_safe_records(monkeypatch) -> None:
    monkeypatch.setattr(validate.shutil, "which", lambda name: None)
    report = validate.preflight("docker")
    assert report["schema_version"] == 1
    assert all(set(item) == {"name", "status", "version", "detail"} for item in report["checks"])
    assert not any("OPENROUTER" in str(item) for item in report["checks"])


def test_version_output_is_bounded_and_version_like() -> None:
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="secret-token\n", stderr="")

    version, detail = validate._version(["tool", "--version"], runner)
    assert version == "unknown"
    assert detail == ""


def test_validation_scripts_and_lock_are_repository_owned() -> None:
    root = Path(__file__).parents[1]
    assert (root / "scripts" / "validate.py").is_file()
    assert (root / "scripts" / "bootstrap.py").is_file()
    assert (root / "scripts" / "qml_smoke.py").is_file()
    lock = (root / "requirements-dev.lock").read_text(encoding="utf-8")
    assert "PySide6==" in lock and "pytest==" in lock and "ruff==" in lock
