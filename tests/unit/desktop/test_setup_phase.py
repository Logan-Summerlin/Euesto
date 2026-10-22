"""The approved dependency setup phase (harness plan 5.1), registry egress as a runtime
setting (5.2, 5.3), and the proxy audit log kept by the desktop."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from runner.setup import (
    TIMEOUT_EXIT,
    SetupError,
    main,
    run_setup,
    setup_command,
    setup_environment,
)
from shared.policy import DEFAULT_REGISTRY_HOSTS
from src.runtime_manager import (
    MAX_AUDIT_ENTRIES,
    ComposeSession,
    RuntimeErrorMessage,
    RuntimeImages,
    RuntimeSettings,
    RuntimeTarget,
    RuntimeWorker,
    SetupRequest,
    SetupWorker,
    append_audit,
    parse_audit_lines,
    read_audit,
    setup_mounts,
)

ROOT = Path(__file__).resolve().parents[3]


# -- runtime settings from the confirmed policy ----------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        RuntimeSettings(network_mode="open"),
        RuntimeSettings(registry_hosts=("github.com",)),
        RuntimeSettings(registry_hosts=()),
        RuntimeSettings(secret_patterns=("a/b",)),
        RuntimeSettings(never_publish=("../x",)),
    ],
)
def test_policy_settings_outside_their_schema_fail_closed(settings: RuntimeSettings) -> None:
    with pytest.raises(RuntimeErrorMessage):
        settings.validated()


def test_the_runtime_passes_the_confirmed_policy_to_the_executor_and_proxy(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings(
        registry_hosts=(*DEFAULT_REGISTRY_HOSTS, "crates.io"),
        secret_patterns=("*.vault",),
        never_publish=("deploy/**",),
    ).validated()
    worker = RuntimeWorker(
        bundle=ROOT,
        data_dir=tmp_path,
        target=RuntimeTarget(None, None),
        images=RuntimeImages("gateway", "executor", False),
        settings=settings,
    )
    environment = worker._compose_environment()
    assert environment["LOCAL_CHAT_EGRESS_ALLOWED_HOSTS"].endswith(",crates.io")
    assert json.loads(environment["LOCAL_CHAT_SECRET_PATTERNS"]) == ["*.vault"]
    assert json.loads(environment["LOCAL_CHAT_NEVER_PUBLISH"]) == ["deploy/**"]
    assert environment["LOCAL_CHAT_EGRESS_PROXY_IMAGE"]


def test_the_compose_files_wire_the_policy_and_the_setup_service() -> None:
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    assert "LOCAL_CHAT_SECRET_PATTERNS: ${LOCAL_CHAT_SECRET_PATTERNS:-[]}" in compose
    assert "LOCAL_CHAT_NEVER_PUBLISH: ${LOCAL_CHAT_NEVER_PUBLISH:-[]}" in compose
    text = (ROOT / "docker" / "compose.setup.yaml").read_text(encoding="utf-8")
    setup = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert 'profiles: ["setup"]' in setup
    assert '"-m", "runner.setup"' in setup
    for absent in ("/source", "secrets:", "/run/runner", "/state", "docker.sock", "privileged"):
        assert absent not in setup
    assert "egress-internal" in setup and "condition: service_healthy" in setup


# -- isolated setup sees only manifests -------------------------------------------------------


def test_isolated_setup_mounts_only_manifests_and_dependency_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "app.py").write_text("SECRET_SOURCE = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    service = setup_mounts(tmp_path, isolated=True)["services"]["setup"]
    volumes = service["volumes"]
    binds = [item for item in volumes if item["type"] == "bind"]
    assert {item["target"] for item in binds} == {"/work/pyproject.toml", "/work/uv.lock"}
    assert all(item["read_only"] for item in binds)
    assert "app.py" not in json.dumps(service) and "/src" not in json.dumps(service)
    subpaths = [item for item in volumes if item["type"] == "volume"]
    assert {(item["target"], item["volume"]["subpath"]) for item in subpaths} == {
        ("/work/.venv", ".venv"),
        ("/work/node_modules", "node_modules"),
    }
    assert any(entry.startswith("/work:") for entry in service["tmpfs"])


def test_full_setup_mounts_the_whole_staging_volume(tmp_path: Path) -> None:
    service = setup_mounts(tmp_path, isolated=False)["services"]["setup"]
    assert service == {"volumes": [{"type": "volume", "source": "work", "target": "/work"}]}


def test_setup_needs_a_running_workspace_runtime(tmp_path: Path) -> None:
    session = ComposeSession(Path("docker"), tmp_path, {}, ())
    worker = SetupWorker(
        session=session,
        workspace=tmp_path,
        data_dir=tmp_path,
        request=SetupRequest("npm ci"),
    )
    with pytest.raises(RuntimeErrorMessage, match="overlays are missing"):
        worker._setup()
    assert [path.name for path in worker.files()] == [
        "compose.egress.yaml",
        "compose.setup.yaml",
        "setup-mounts.json",
    ]


# -- the setup command in the runner image ----------------------------------------------------


def test_the_setup_command_is_validated_before_anything_runs() -> None:
    assert setup_command({"LOCAL_CHAT_SETUP_COMMAND": " npm ci "}) == ("npm ci", 900)
    for environ in (
        {},
        {"LOCAL_CHAT_SETUP_COMMAND": "x" * 1_001},
        {"LOCAL_CHAT_SETUP_COMMAND": "npm ci", "LOCAL_CHAT_SETUP_TIMEOUT": "0"},
        {"LOCAL_CHAT_SETUP_COMMAND": "npm ci", "LOCAL_CHAT_SETUP_TIMEOUT": "99999"},
    ):
        with pytest.raises(SetupError):
            setup_command(environ)
    assert main({}) == 2


def test_the_setup_environment_is_fixed_apart_from_the_proxy() -> None:
    environment = setup_environment(
        {
            "LOCAL_CHAT_EGRESS_PROXY": "http://172.31.250.2:3128",
            "OPENROUTER_API_KEY": "leak",
            "PATH": "/evil",
        },
        cache_root=Path("/cache"),
    )
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert environment["HTTPS_PROXY"] == "http://172.31.250.2:3128"
    assert environment["UV_CACHE_DIR"] == "/cache/uv"
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/work/.venv"
    assert "OPENROUTER_API_KEY" not in environment and "leak" not in environment.values()
    assert "HTTPS_PROXY" not in setup_environment({})
    with pytest.raises(SetupError):
        setup_environment({"LOCAL_CHAT_EGRESS_PROXY": "socks5://x:1"})


@pytest.mark.posix
def test_the_setup_command_runs_in_work_and_is_killed_at_its_limit(tmp_path: Path) -> None:
    environment = setup_environment({}, work_root=tmp_path, cache_root=tmp_path / "cache")
    environment["HOME"] = str(tmp_path / "home")
    assert run_setup("touch .venv-made && exit 3", 30, environment, work_root=tmp_path) == 3
    assert (tmp_path / ".venv-made").exists()
    assert run_setup("sleep 30", 1, environment, work_root=tmp_path) == TIMEOUT_EXIT


# -- the audit log ------------------------------------------------------------------------------


def test_proxy_audit_lines_are_parsed_from_compose_logs() -> None:
    allowed = {"event": "egress.allowed", "host": "pypi.org", "port": 443, "timestamp": 1.0}
    denied = {
        "event": "egress.denied",
        "host": "example.com",
        "reason": "host_not_allowed",
        "timestamp": 2.0,
        "extra": "dropped",
    }
    logs = (
        f"egress-proxy-1  | {json.dumps(allowed)}\n"
        "egress-proxy-1  | egress proxy listening\n"
        f"egress-proxy-1  | {json.dumps(denied)}\n"
        'egress-proxy-1  | {"event": "other"}\n'
        "egress-proxy-1  | {not json\n"
    )
    entries = parse_audit_lines(logs)
    assert [item["host"] for item in entries] == ["pypi.org", "example.com"]
    assert "extra" not in entries[1]


def test_the_kept_audit_log_is_deduplicated_and_bounded(tmp_path: Path) -> None:
    entry = {"event": "egress.allowed", "host": "pypi.org", "timestamp": 1.0}
    append_audit(tmp_path, [entry])
    append_audit(tmp_path, [entry])
    assert read_audit(tmp_path) == [entry]
    append_audit(
        tmp_path,
        [
            {"event": "egress.denied", "host": "x.test", "timestamp": float(index)}
            for index in range(2, MAX_AUDIT_ENTRIES + 10)
        ],
    )
    kept = read_audit(tmp_path)
    assert len(kept) == MAX_AUDIT_ENTRIES and kept[-1]["timestamp"] == MAX_AUDIT_ENTRIES + 9


def test_run_docker_streams_lines_and_honours_the_deadline(tmp_path: Path) -> None:
    from src.runtime_manager import run_docker

    seen: list[str] = []
    code, text = run_docker(
        Path(sys.executable),
        ["-c", "print('one'); print('two')"],
        cwd=tmp_path,
        environment={},
        timeout=30,
        stop_event=threading.Event(),
        on_line=seen.append,
    )
    assert code == 0 and text.split() == ["one", "two"] and len(seen) == 2
    with pytest.raises(RuntimeErrorMessage, match="timed out"):
        run_docker(
            Path(sys.executable),
            ["-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            environment={},
            timeout=0.5,
            stop_event=threading.Event(),
        )
