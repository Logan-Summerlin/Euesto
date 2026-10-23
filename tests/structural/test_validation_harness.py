from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.validate as validate

ROOT = Path(__file__).resolve().parents[2]


def test_validation_commands_use_explicit_tiers_and_active_python() -> None:
    commands = validate.commands("all")
    assert commands
    assert [name for name, _ in commands] == [
        "fast",
        "slow",
        "ruff",
        "ruff-format",
        "compile",
        "qml",
        "qml-smoke",
        "docker",
    ]
    assert commands[0][1][:3] == [validate.sys.executable, "-m", "pytest"]
    assert commands[0][1][-1] == "not slow and not docker"
    assert validate.commands("slow")[0][1][-1] == "slow and not docker"
    assert validate.commands("qml-smoke") == [
        ("qml-smoke", [validate.sys.executable, "scripts/qml_smoke.py"])
    ]


def test_validation_environment_keeps_qt_for_qt_children_and_no_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-export")
    env = validate._safe_env()
    assert env["PYTHONUNBUFFERED"] == "1"
    # The platform variables go to the Qt children only: a plain Python child that inherits
    # them, and every process it starts, is told to use a platform it never asked for.
    assert "QT_QPA_PLATFORM" not in env
    assert "OPENROUTER_API_KEY" not in env
    qt_env = validate._safe_env(qt=True)
    assert qt_env["QT_QPA_PLATFORM"] == "offscreen"
    assert qt_env["QT_QUICK_BACKEND"] == "software"
    assert qt_env["QT_QUICK_CONTROLS_STYLE"] == "Fusion"


def test_only_the_qt_tiers_get_the_qt_environment() -> None:
    seen: dict[str, dict[str, str]] = {}

    def runner(command, *, env, **kwargs):
        seen["last"] = env
        return subprocess.CompletedProcess(command, 0)

    validate.execute("qml-smoke", runner=runner)
    assert seen["last"]["QT_QPA_PLATFORM"] == "offscreen"
    validate.execute("fast", runner=runner)
    assert "QT_QPA_PLATFORM" not in seen["last"]


def test_windows_children_get_the_os_variables_they_cannot_invent(monkeypatch) -> None:
    for name in validate.WINDOWS_ENV:
        monkeypatch.setenv(name, f"value-of-{name}")
    env = validate._safe_env(windows=True)
    assert set(validate.WINDOWS_ENV) <= set(env)
    for name in (
        "APPDATA",
        "PROGRAMDATA",
        "ProgramFiles",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "VIRTUAL_ENV",
    ):
        assert env[name] == f"value-of-{name}"


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


def test_a_hung_command_fails_its_tier_by_name_with_its_deadline() -> None:
    seen: list[float] = []

    def hung(*args, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    report: dict = {"checks": []}
    assert validate.execute("fast", report=report, runner=hung) == 124
    assert seen == [validate.COMMAND_TIMEOUT_SECONDS]
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["detail"] == (
        f"failed: timed out after {validate.COMMAND_TIMEOUT_SECONDS}s"
    )

    # The QML smoke check has its own, much shorter backstop: a wedged Qt startup must not hold
    # a tier for twenty minutes.
    seen.clear()
    report = {"checks": []}
    assert validate.execute("qml-smoke", report=report, runner=hung) == 124
    assert seen == [validate.QML_SMOKE_TIMEOUT_SECONDS]
    assert report["checks"][0]["detail"] == (
        f"failed: timed out after {validate.QML_SMOKE_TIMEOUT_SECONDS}s"
    )


def test_the_default_runner_kills_a_command_that_outlives_its_deadline() -> None:
    import sys

    with pytest.raises(subprocess.TimeoutExpired):
        validate._run_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=ROOT,
            env=validate._safe_env(),
            timeout=0.5,
        )
    assert (
        validate._run_command(
            [sys.executable, "-c", "pass"], cwd=ROOT, env=validate._safe_env(), timeout=60
        ).returncode
        == 0
    )


def test_the_default_runner_has_no_console_window_and_no_stdin(monkeypatch) -> None:
    """A child must not inherit an interactive console or be able to wait for input."""
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1

        def wait(self, timeout=None):
            captured["wait"] = timeout
            return 0

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(validate.subprocess, "Popen", fake_popen)
    validate._run_command(["tool"], cwd=ROOT, env=validate._safe_env(), timeout=17)
    assert captured["stdin"] == validate.subprocess.DEVNULL
    assert captured["creationflags"] == validate.CREATE_NO_WINDOW
    assert captured["wait"] == 17


def test_a_deadline_that_cannot_reap_its_child_still_reports_the_timeout(monkeypatch) -> None:
    """The wait after a kill is bounded too, so it can never become the hang itself."""
    waits: list[float | None] = []
    kills: list[list[str]] = []

    class StubbornProcess:
        pid = 4321

        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("tool", timeout)

        def kill(self) -> None:
            pass

    def record_kill(command, **kwargs):
        kills.append(list(command))

    monkeypatch.setattr(
        validate.subprocess, "Popen", lambda *args, **kwargs: StubbornProcess()
    )
    monkeypatch.setattr(validate.subprocess, "run", record_kill)
    monkeypatch.setattr(validate.os, "name", "nt")
    with pytest.raises(subprocess.TimeoutExpired):
        validate._run_command(["tool"], cwd=ROOT, env=validate._safe_env(), timeout=1)
    assert waits == [1, validate.KILL_TIMEOUT_SECONDS]
    assert kills and kills[0][:4] == ["taskkill", "/F", "/T", "/PID"]


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
    assert all(
        set(item) == {"name", "status", "version", "detail"} for item in report["checks"]
    )
    assert not any("OPENROUTER" in str(item) for item in report["checks"])


def test_version_output_is_bounded_and_version_like() -> None:
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="secret-token\n", stderr="")

    version, detail = validate._version(["tool", "--version"], runner)
    assert version == "unknown"
    assert detail == ""


def test_validation_scripts_and_lock_are_repository_owned() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "scripts" / "validate.py").is_file()
    assert (root / "scripts" / "bootstrap.py").is_file()
    assert (root / "scripts" / "qml_smoke.py").is_file()
    assert (root / "scripts" / "validation-step.sh").is_file()
    lock = (root / "requirements-dev.lock").read_text(encoding="utf-8")
    assert "PySide6==" in lock and "pytest==" in lock and "ruff==" in lock


def test_validate_script_runs_as_a_command() -> None:
    root = Path(validate.__file__).resolve().parents[1]
    result = subprocess.run(
        [validate.sys.executable, "scripts/validate.py", "preflight"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert " python: " in result.stdout


def test_the_qml_smoke_check_is_bounded_and_not_vacuous() -> None:
    """The startup check must load QML against a real backend and be able to end itself."""
    source = (ROOT / "scripts" / "qml_smoke.py").read_text(encoding="utf-8")
    # A vacuous check loads Main.qml with no context properties: every backend binding is a
    # ReferenceError warning and the root object still exists.
    assert 'setContextProperty("backend"' in source
    assert 'setContextProperty("transcriptModel"' in source
    assert "ReferenceError" in source
    # Three independent bounds, and an exit that skips Qt teardown.
    assert "faulthandler.dump_traceback_later(" in source
    assert "QTimer.singleShot(" in source and "app.quit" in source
    assert "os._exit(" in source


def test_dev_lock_matches_the_gateway_image_web_stack() -> None:
    """Tests run against the same Starlette, uvicorn, and httpx the images ship (plan 0.5)."""
    import re

    def pins(name: str) -> dict[str, str]:
        text = (ROOT / name).read_text(encoding="utf-8")
        return {
            match[0].casefold(): match[1]
            for match in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", text, re.MULTILINE)
        }

    lock, gateway = pins("requirements-dev.lock"), pins("requirements-gateway.txt")
    for package in ("starlette", "uvicorn", "httpx"):
        assert lock[package] == gateway[package], package
