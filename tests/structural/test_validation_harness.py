from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
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
    assert validate.commands("collect") == [
        ("collect", [validate.sys.executable, "-m", "pytest", "--collect-only", "-q"])
    ]
    # `all` runs the checks; collection is a diagnostic step the workflows run first.
    assert "collect" not in [name for name, _ in commands]


def test_validation_environment_keeps_qt_for_qt_children_and_no_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-export")
    env = validate._safe_env()
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    # The platform variables go to the Qt children only: a plain Python child that inherits
    # them, and every process it starts, is told to use a platform it never asked for.
    assert "QT_QPA_PLATFORM" not in env
    assert "OPENROUTER_API_KEY" not in env
    qt_env = validate._safe_env(qt=True)
    assert qt_env["QT_QPA_PLATFORM"] == "offscreen"
    assert qt_env["QT_QUICK_BACKEND"] == "software"
    assert qt_env["QT_QUICK_CONTROLS_STYLE"] == "Fusion"


def test_container_tier_fixtures_reach_the_validation_child(monkeypatch) -> None:
    """The docker tier reads its fixtures from the environment.

    Filtering them out of a validation child did not fail the tier: every container check
    skipped itself as "requires the container-tier fixtures", which is a green job that ran
    nothing.
    """
    monkeypatch.setenv("LOCAL_CHAT_SECRETS_DIR", "/tmp/euesto/secrets")
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE", "/tmp/euesto/workspace")
    monkeypatch.setenv("EUSTO_BUILDX_CACHE_DIR", "/tmp/euesto/cache")
    env = validate._safe_env()
    assert env["LOCAL_CHAT_SECRETS_DIR"] == "/tmp/euesto/secrets"
    assert env["LOCAL_CHAT_WORKSPACE"] == "/tmp/euesto/workspace"
    assert env["EUSTO_BUILDX_CACHE_DIR"] == "/tmp/euesto/cache"
    assert "OPENROUTER_API_KEY" not in env


@pytest.mark.posix
@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the fixture script is checked through the file modes it sets",
)
def test_container_fixture_credentials_are_readable_by_the_container_users(tmp_path) -> None:
    """A credential the containers cannot read fails every whole-profile check.

    Compose mounts a `file:` secret as a bind mount, so a container sees the host file's owner
    and mode. These fixtures belong to the CI user, while the gateway runs as UID 10001 and the
    executor as UID 10002: with a 0600 credential the executor's entrypoint exited at its
    credential check, the gateway refused a token it never read, and `docker compose up --wait`
    failed for both egress checks and the executor-to-runner check while the runner-only check
    passed, with no error naming the credential.
    """
    root = tmp_path / "euesto-validation"
    subprocess.run(
        ["bash", "-c", "set -euo pipefail; source scripts/docker-fixtures.sh; setup_fixtures"],
        cwd=ROOT,
        env={**os.environ, "LOCAL_CHAT_CI_ROOT": str(root)},
        check=True,
        capture_output=True,
        text=True,
    )
    secrets = root / "secrets"
    for name in ("gateway_token.txt", "executor_token.txt"):
        mode = stat.S_IMODE((secrets / name).stat().st_mode)
        assert mode & stat.S_IROTH, f"{name} is mode {mode:o}; a container user cannot read it"
    # The directory stays private: only the two files the containers mount are readable.
    assert stat.S_IMODE(secrets.stat().st_mode) & 0o077 == 0


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
        "PROGRAMDATA",
        "ProgramFiles",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "VIRTUAL_ENV",
    ):
        assert env[name] == f"value-of-{name}"
    # The three directories that hold user state are the disposable tree, not the real ones.
    for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        assert env[name] != f"value-of-{name}"
        assert Path(env[name]).is_relative_to(Path(env["HOME"]))


def test_validation_children_get_a_disposable_home(monkeypatch) -> None:
    """A tier must not write real state under the runner's home or prompt the host keyring."""
    monkeypatch.setenv("USERPROFILE", "C:/Users/real")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/real/AppData/Local")
    monkeypatch.setenv("APPDATA", "C:/Users/real/AppData/Roaming")
    env = validate._safe_env()
    home = Path(env["HOME"])
    assert home.name.startswith("euesto-validation-home-")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        assert Path(env[name]).is_relative_to(home)
    windows = validate._safe_env(windows=True)
    assert windows["USERPROFILE"] == env["HOME"]
    assert windows["LOCALAPPDATA"] != "C:/Users/real/AppData/Local"
    assert windows["APPDATA"] != "C:/Users/real/AppData/Roaming"


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


def test_a_hung_command_fails_its_tier_by_name_with_its_deadline(monkeypatch) -> None:
    seen: list[float] = []

    def hung(*args, **kwargs):
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    report: dict = {"checks": []}
    assert validate.execute("fast", report=report, runner=hung) == 124
    assert seen == [validate.TIER_TIMEOUTS["fast"]]
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][0]["detail"] == (
        f"failed: timed out after {validate.TIER_TIMEOUTS['fast']}s"
    )

    # Every tier has its own deadline, so no tier has to borrow another's budget.
    for tier in ("slow", "ruff", "compile", "qml", "qml-smoke"):
        seen.clear()
        report = {"checks": []}
        assert validate.execute(tier, report=report, runner=hung) == 124
        assert set(seen) == {validate.TIER_TIMEOUTS[tier]}, tier
        assert report["checks"][0]["detail"].endswith(f"after {validate.TIER_TIMEOUTS[tier]}s")

    monkeypatch.setenv("LOCAL_CHAT_SECRETS_DIR", "/tmp/euesto/secrets")
    monkeypatch.setenv("LOCAL_CHAT_WORKSPACE", "/tmp/euesto/workspace")
    seen.clear()
    report = {"checks": []}
    assert validate.execute("docker", report=report, runner=hung) == 124
    assert seen == [validate.TIER_TIMEOUTS["docker"]]
    assert report["checks"][0]["detail"].endswith(f"after {validate.TIER_TIMEOUTS['docker']}s")


def test_the_docker_tier_fails_by_name_when_its_fixtures_are_missing(monkeypatch) -> None:
    """Without fixtures every container check skipped, which is a green tier that ran nothing."""
    for name in validate.DOCKER_FIXTURES:
        monkeypatch.delenv(name, raising=False)
    report: dict = {"checks": []}
    assert validate.execute("docker", report=report) == 1
    assert report["checks"][0]["name"] == "docker-fixtures"
    assert "setup_fixtures" in report["checks"][0]["detail"]
    assert validate.execute("docker", allow_unavailable=True) == 0

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    for name in validate.DOCKER_FIXTURES:
        monkeypatch.setenv(name, "/tmp/euesto/value")
    assert validate.execute("docker", runner=runner) == 0
    assert calls == [validate.commands("docker")[0][1]]


def test_every_tier_has_its_own_deadline_above_the_per_test_timeout() -> None:
    """The harness deadline is the inner bound; the workflows keep their ceiling outside it."""
    for tier, deadline in validate.TIER_TIMEOUTS.items():
        assert deadline > 180 or tier in {"preflight", "collect"}, tier
    assert validate.TIER_TIMEOUTS["fast"] < validate.TIER_TIMEOUTS["slow"]
    assert validate.TIER_TIMEOUTS["docker"] > validate.TIER_TIMEOUTS["slow"]


def test_the_default_runner_kills_a_command_that_outlives_its_deadline() -> None:
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
    # POSIX needs its own session, which is the only handle a deadline has on the whole tree.
    assert captured["start_new_session"] is (validate.os.name != "nt")
    assert captured["wait"] == 17


def test_a_posix_deadline_signals_the_whole_process_group(monkeypatch) -> None:
    """A direct-child-only kill leaves runner servers and `bash -lc` grandchildren alive."""
    signalled: list[tuple[int, int]] = []
    waits: list[float | None] = []

    class GroupProcess:
        pid = 4321

        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("tool", timeout)

        def kill(self) -> None:
            raise AssertionError("the process group must be signalled, not just the child")

    monkeypatch.setattr(validate.subprocess, "Popen", lambda *args, **kwargs: GroupProcess())
    monkeypatch.setattr(validate.os, "name", "posix")
    monkeypatch.setattr(validate.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        validate.os, "killpg", lambda pgid, signum: signalled.append((pgid, signum))
    )
    with pytest.raises(subprocess.TimeoutExpired):
        validate._run_command(["tool"], cwd=ROOT, env=validate._safe_env(), timeout=1)
    assert signalled == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert waits == [1, validate.KILL_GRACE_SECONDS, validate.KILL_TIMEOUT_SECONDS]


def test_a_running_command_reports_progress(monkeypatch, capsys) -> None:
    """A command that is alive but silent must still show liveness in the log."""
    monkeypatch.setattr(validate, "PROGRESS_INTERVAL_SECONDS", 0.05)
    validate._run_command(
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        cwd=ROOT,
        env=validate._safe_env(),
        timeout=60,
    )
    assert "[progress]" in capsys.readouterr().out


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

    monkeypatch.setattr(validate.subprocess, "Popen", lambda *args, **kwargs: StubbornProcess())
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


def test_preflight_require_fails_when_a_required_tool_is_unavailable(monkeypatch) -> None:
    """`preflight` is a gate in both workflows, so it has to be able to fail (plan W1)."""
    report = {
        "schema_version": 1,
        "tier": "fast",
        "checks": [
            validate._record("python", "available"),
            validate._record("repository-root", "available"),
            validate._record("pytest", "unavailable", detail="install requirements-dev.lock"),
        ],
    }
    monkeypatch.setattr(validate, "preflight", lambda *tiers: report)
    assert validate.main(["preflight", "--require", "fast"]) == 1
    # Reporting only: a developer without a tool still sees the rest of the environment.
    assert validate.main(["preflight"]) == 0
    assert validate.main(["preflight", "--require", "compile"]) == 0
    assert validate.main(["preflight", "--require", "nonsense"]) == 2
    assert validate.main(["fast", "--require", "fast"]) == 2


def test_preflight_reports_the_tiers_it_is_asked_about() -> None:
    report = validate.preflight("fast", "docker", runner=lambda *a, **k: _version_stub())
    names = {check["name"] for check in report["checks"]}
    assert {"tier:fast", "tier:docker"} <= names
    assert report["tier"] == "fast,docker"


def _version_stub() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout="tool 1.2.3", stderr="")


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
