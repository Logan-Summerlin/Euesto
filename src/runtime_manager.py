from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue

import httpx
from PySide6.QtCore import QObject, QThread, Signal

from shared.responses import GatewayStatus

from .gateway_client import DEFAULT_GATEWAY_URL
from .workspace_broker import canonical_workspace, workspace_id

PROJECT_NAME = "local-openrouter-chat"
DEFAULT_GATEWAY_IMAGE = "local-openrouter-chat-gateway:1.1.0"
DEFAULT_EXECUTOR_IMAGE = "local-openrouter-chat-executor:1.1.0"
RUNNER_TARGETS = ("base", "python", "node", "polyglot")
DEFAULT_RUNNER_IMAGES = {
    target: f"local-openrouter-chat-runner-{target}:1.1.0" for target in RUNNER_TARGETS
}
RELEASE_MANIFEST_SCHEMA = 2
# Runner resource ceilings (docs/LIMITS.md, "Runner resources"); values outside fail closed.
RUNNER_CPU_RANGE = (2.0, 8.0)
RUNNER_MEMORY_GB_RANGE = (2, 16)
RUNNER_PIDS_RANGE = (256, 4_096)
DEFAULT_RUNNER_PIDS = 1_024
MAX_GIT_HISTORY_DEPTH = 100_000
WORKSPACE_KEY_LENGTH = 12
STAGED_SESSIONS_FILE = "staged-sessions.json"
DEFAULT_RETENTION_DAYS = 30
VOLUME_KINDS = ("work", "state", "cache")
SESSION_TOKEN_BYTES = 32
READINESS_TIMEOUT_SECONDS = 180
DOCKER_START_TIMEOUT_SECONDS = 120
EGRESS_MODE_ENV = "LOCAL_CHAT_EXECUTOR_EGRESS"
EGRESS_OVERLAY = "compose.egress.yaml"
IMAGE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,511}$")
IMAGE_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")
DOCKER_INFO = ["info", "--format", "{{.ServerVersion}}"]
DOCKER_RESOURCES = ["info", "--format", "{{.NCPU}} {{.MemTotal}}"]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeErrorMessage(RuntimeError):
    """An actionable, user-facing runtime setup failure."""


@dataclass(frozen=True, slots=True)
class RuntimeImages:
    gateway: str
    executor: str
    prebuilt: bool
    runners: tuple[tuple[str, str], ...] = tuple(DEFAULT_RUNNER_IMAGES.items())

    def runner(self, target: str) -> str:
        return dict(self.runners)[target]

    @classmethod
    def for_bundle(
        cls, bundle_root: Path, *, require_manifest: bool | None = None
    ) -> RuntimeImages:
        manifest = bundle_root / "docker" / "release-images.json"
        if not manifest.is_file():
            if require_manifest is None:
                require_manifest = bool(getattr(sys, "frozen", False))
            if require_manifest:
                raise RuntimeErrorMessage(
                    "This Windows release is missing its Docker image manifest. Reinstall the application."
                )
            return cls(DEFAULT_GATEWAY_IMAGE, DEFAULT_EXECUTOR_IMAGE, False)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorMessage(
                f"The bundled Docker image manifest is invalid: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeErrorMessage("The bundled Docker image manifest must be an object.")
        if data.get("schema_version") != RELEASE_MANIFEST_SCHEMA:
            raise RuntimeErrorMessage(
                "The bundled Docker image manifest has an unsupported version."
            )
        gateway = _image_ref(data.get("gateway"), "gateway")
        executor = _image_ref(data.get("executor"), "executor")
        runners = data.get("runners")
        if not isinstance(runners, dict) or set(runners) != set(RUNNER_TARGETS):
            raise RuntimeErrorMessage(
                "The bundled Docker image manifest must list the base, python, node, and "
                "polyglot runner images."
            )
        runner_images = tuple(
            (target, _image_ref(runners[target], f"{target} runner"))
            for target in RUNNER_TARGETS
        )
        references = (gateway, executor, *(image for _target, image in runner_images))
        if not all(IMAGE_DIGEST_PATTERN.search(reference) for reference in references):
            raise RuntimeErrorMessage("The bundled Docker image manifest is not digest-pinned.")
        return cls(gateway, executor, True, runner_images)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """User-chosen runner settings; ``None`` resources default from Docker's allocation."""

    runner_target: str = "auto"
    cpus: float | None = None
    memory_gb: int | None = None
    pids: int = DEFAULT_RUNNER_PIDS
    git_history_depth: int = 200

    def validated(self) -> RuntimeSettings:
        """Return these settings, or fail closed on any value outside its ceiling."""
        if self.runner_target != "auto" and self.runner_target not in RUNNER_TARGETS:
            raise RuntimeErrorMessage(
                "The runner image must be auto, base, python, node, or polyglot."
            )
        if self.cpus is not None and not (
            isinstance(self.cpus, int | float)
            and RUNNER_CPU_RANGE[0] <= self.cpus <= RUNNER_CPU_RANGE[1]
        ):
            raise RuntimeErrorMessage(
                f"Runner CPUs must be from {RUNNER_CPU_RANGE[0]:g} to {RUNNER_CPU_RANGE[1]:g}."
            )
        bounded = [
            ("Runner process limit", self.pids, RUNNER_PIDS_RANGE),
            ("Git history depth", self.git_history_depth, (0, MAX_GIT_HISTORY_DEPTH)),
        ]
        if self.memory_gb is not None:
            bounded.append(("Runner memory (GB)", self.memory_gb, RUNNER_MEMORY_GB_RANGE))
        for name, value, (low, high) in bounded:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not low <= value <= high
            ):
                raise RuntimeErrorMessage(
                    f"{name} must be a whole number from {low} to {high}."
                )
        return self

    def resolved(self, docker_cpus: int, docker_memory_bytes: int) -> tuple[float, int]:
        """CPUs and memory (GB) for the runner: the settings, else a share of Docker's."""
        cpus = self.cpus
        if cpus is None:
            cpus = float(min(max(docker_cpus - 1, RUNNER_CPU_RANGE[0]), RUNNER_CPU_RANGE[1]))
        memory = self.memory_gb
        if memory is None:
            half = docker_memory_bytes // 2 // 1024**3
            memory = min(max(half, RUNNER_MEMORY_GB_RANGE[0]), RUNNER_MEMORY_GB_RANGE[1])
        return cpus, memory


def detect_runner_target(workspace: Path) -> str:
    """Choose a runner image from the project's top-level files (plan 2.2)."""
    try:
        names = {entry.name for entry in workspace.iterdir()}
    except OSError:
        return "base"
    python = bool(
        names
        & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "uv.lock"}
    )
    node = bool(names & {"package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"})
    other = bool(
        names & {"go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts"}
    )
    if other or (python and node):
        return "polyglot"
    if node:
        return "node"
    if python:
        return "python"
    return "base"


def workspace_key(identity: str) -> str:
    """The volume-name key for a workspace identity: its first 12 lowercase hex digits."""
    key = identity[:WORKSPACE_KEY_LENGTH].lower()
    if len(key) != WORKSPACE_KEY_LENGTH or not re.fullmatch(r"[0-9a-f]+", key):
        raise RuntimeErrorMessage("The workspace identity is invalid.")
    return key


@dataclass(frozen=True, slots=True)
class RuntimeTarget:
    workspace: Path | None
    workspace_identity: str | None

    @classmethod
    def from_workspace(cls, workspace: Path | None) -> RuntimeTarget:
        if workspace is None:
            return cls(None, None)
        resolved = canonical_workspace(workspace)
        return cls(resolved, workspace_id(resolved))


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    target: RuntimeTarget
    gateway_token: str


def bundle_root() -> Path:
    """Return the source root or the PyInstaller extraction root."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parents[1]


def locate_docker() -> Path | None:
    found = shutil.which("docker")
    return _first_file(
        [Path(found)] if found else [],
        ("ProgramFiles", "Docker/Docker/resources/bin/docker.exe"),
        ("LOCALAPPDATA", "Docker/resources/bin/docker.exe"),
    )


def locate_docker_desktop() -> Path | None:
    return _first_file(
        [],
        ("ProgramFiles", "Docker/Docker/Docker Desktop.exe"),
        ("LOCALAPPDATA", "Programs/DockerDesktop/Docker Desktop.exe"),
    )


def _first_file(candidates: list[Path], *windows_paths: tuple[str, str]) -> Path | None:
    """The first existing file among ``candidates`` and, on Windows, ``(env root, path)`` pairs."""
    if sys.platform == "win32":
        for variable, relative in windows_paths:
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root, *relative.split("/")))
    return next((path for path in candidates if path.is_file()), None)


def create_session_tokens(session_dir: Path) -> tuple[str, str]:
    session_dir.mkdir(parents=True, exist_ok=True)
    gateway_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    executor_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    _write_secret(session_dir / "gateway_token.txt", gateway_token)
    _write_secret(session_dir / "executor_token.txt", executor_token)
    return gateway_token, executor_token


def compose_base_args(compose_file: Path, *, overlays: tuple[Path, ...] = ()) -> list[str]:
    arguments = ["compose", "--project-name", PROJECT_NAME, "--file", str(compose_file)]
    for overlay in overlays:
        arguments.extend(("--file", str(overlay)))
    return arguments


def egress_overlays(
    bundle: Path, *, prebuilt: bool, environ: dict[str, str] | None = None
) -> tuple[Path, ...]:
    """Compose overlays for the opt-in allowlisted-egress executor profile.

    The default (``LOCAL_CHAT_EXECUTOR_EGRESS`` unset, ``none``, or ``off``) adds nothing, so the
    executor keeps ``network_mode: none``. ``allowlisted`` adds ``docker/compose.egress.yaml``
    (developer bundles only while the profile is a prototype); any other value fails closed.
    """
    mode = (
        (os.environ if environ is None else environ).get(EGRESS_MODE_ENV, "").strip().casefold()
    )
    if mode in {"", "none", "off"}:
        return ()
    if mode != "allowlisted":
        raise RuntimeErrorMessage(f"{EGRESS_MODE_ENV} must be 'allowlisted' or unset.")
    if prebuilt:
        raise RuntimeErrorMessage(
            "Allowlisted executor egress is a developer prototype and is not available "
            "with release images."
        )
    overlay = bundle / "docker" / EGRESS_OVERLAY
    if not overlay.is_file():
        raise RuntimeErrorMessage("The allowlisted egress Compose overlay is missing.")
    return (overlay,)


def _image_ref(value: object, name: str) -> str:
    reference = str(value or "").strip()
    if not IMAGE_REF_PATTERN.fullmatch(reference):
        raise RuntimeErrorMessage(f"The bundled {name} image reference is invalid.")
    return reference


def _write_secret(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    with contextlib.suppress(OSError):
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _spawn_detached(command: list[str], **options: object) -> None:
    """Start a process that outlives the app, without a console window or output pipes."""
    subprocess.Popen(
        command,
        close_fds=True,
        creationflags=CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )


def record_staged_session(
    data_dir: Path, key: str, workspace: Path, runner_target: str, *, now: float | None = None
) -> None:
    """Remember when a workspace's persistent staging was last used (for retention)."""
    sessions = _load_staged_sessions(data_dir)
    sessions[key] = {
        "workspace": str(workspace),
        "runner": runner_target,
        "last_used": time.time() if now is None else now,
    }
    path = data_dir / STAGED_SESSIONS_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sessions, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _load_staged_sessions(data_dir: Path) -> dict[str, dict]:
    try:
        value = json.loads((data_dir / STAGED_SESSIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(item, dict) and re.fullmatch(r"[0-9a-f]{12}", str(key))
    }


def staged_sessions(
    docker: Callable[[list[str]], str],
    data_dir: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Persistent staged sessions Docker holds, with size and age; stale ones are flagged.

    ``docker`` runs one Docker CLI command and returns its output.
    """
    names = docker(
        ["volume", "ls", "--filter", "label=io.euesto.kind=work", "--format", "{{.Name}}"]
    ).split()
    sizes: dict[str, str] = {}
    try:
        usage = json.loads(docker(["system", "df", "--verbose", "--format", "json"]) or "{}")
        for volume in usage.get("Volumes") or ():
            if isinstance(volume, dict):
                sizes[str(volume.get("Name"))] = str(volume.get("Size") or "")
    except (ValueError, RuntimeErrorMessage):
        pass
    records = _load_staged_sessions(data_dir)
    current = time.time() if now is None else now
    sessions: list[dict[str, object]] = []
    for name in names:
        key = name.removeprefix("euesto-work-")
        if not re.fullmatch(r"[0-9a-f]{12}", key):
            continue
        record = records.get(key, {})
        last_used = record.get("last_used")
        age_days = (
            int((current - float(last_used)) // 86_400)
            if isinstance(last_used, int | float)
            else None
        )
        sessions.append(
            {
                "key": key,
                "workspace": str(record.get("workspace") or ""),
                "runner": str(record.get("runner") or ""),
                "size": sizes.get(name, ""),
                "age_days": age_days,
                "stale": age_days is None or age_days >= retention_days,
            }
        )
    return sorted(sessions, key=lambda item: (item["age_days"] is None, item["age_days"] or 0))


def remove_staged_session(
    docker: Callable[[list[str]], str], data_dir: Path, key: str, *, active_key: str | None
) -> None:
    """Delete a workspace's staging, state, and cache volumes (Discard session)."""
    if not re.fullmatch(r"[0-9a-f]{12}", key):
        raise RuntimeErrorMessage("That staged session is not valid.")
    if key == active_key:
        raise RuntimeErrorMessage(
            "Close or switch away from this workspace before removing its staged session."
        )
    docker(["volume", "rm", "--force", *(f"euesto-{kind}-{key}" for kind in VOLUME_KINDS)])
    sessions = _load_staged_sessions(data_dir)
    if sessions.pop(key, None) is not None:
        path = data_dir / STAGED_SESSIONS_FILE
        path.write_text(json.dumps(sessions, indent=1, sort_keys=True), encoding="utf-8")


class RuntimeWorker(QThread):
    progress = Signal(str, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        bundle: Path,
        data_dir: Path,
        target: RuntimeTarget,
        images: RuntimeImages,
        settings: RuntimeSettings | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.bundle = bundle
        self.data_dir = data_dir
        self.target = target
        self.images = images
        self.settings = settings or RuntimeSettings()
        self.runner_target = "base"
        self.stop_event = threading.Event()
        self.docker: Path | None = None
        self.environment: dict[str, str] = {}
        self.secret_values: tuple[str, ...] = ()
        self.compose_file = bundle / "docker" / "compose.yaml"
        self.overlays: tuple[Path, ...] = ()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            if not self.compose_file.is_file():
                raise RuntimeErrorMessage("The bundled Docker Compose file is missing.")
            self.settings.validated()
            if self.target.workspace:
                self.overlays = egress_overlays(self.bundle, prebuilt=self.images.prebuilt)
                self.runner_target = (
                    detect_runner_target(self.target.workspace)
                    if self.settings.runner_target == "auto"
                    else self.settings.runner_target
                )
            self.docker = locate_docker()
            if self.docker is None:
                raise RuntimeErrorMessage(
                    "Docker Desktop is not installed or docker.exe is unavailable. "
                    "Install Docker Desktop using Linux containers, then try again."
                )
            self.secret_values = create_session_tokens(self.data_dir / "gateway-session")
            gateway_token = self.secret_values[0]
            self.environment = self._compose_environment()
            self.progress.emit("starting", "Checking Docker Desktop…")
            self._ensure_docker_ready()
            self._check_stopped()
            if self.target.workspace:
                self.environment.update(self._runner_environment())

            self.progress.emit("stopping", "Reconciling the previous local runtime…")
            self._compose(["down", "--remove-orphans"], timeout=45, allow_failure=True)
            self._check_stopped()

            profile = ["--profile", "agent"] if self.target.workspace else []
            if self.images.prebuilt:
                self.progress.emit("pulling", "Downloading the Local Chat runtime…")
                self._compose([*profile, "pull"], timeout=600)
            else:
                self.progress.emit("building", "Building the developer Docker runtime…")

            self.progress.emit("starting", "Starting the local gateway and executor…")
            up_args = [*profile, "up", "--detach"]
            if not self.images.prebuilt:
                up_args.append("--build")
            self._compose(up_args, timeout=600)
            self._check_stopped()

            self.progress.emit("checking", "Waiting for the selected workspace to be ready…")
            self._wait_for_readiness(gateway_token)
            if self.target.workspace and self.target.workspace_identity:
                with contextlib.suppress(OSError):
                    record_staged_session(
                        self.data_dir,
                        workspace_key(self.target.workspace_identity),
                        self.target.workspace,
                        self.runner_target,
                    )
            self.succeeded.emit(RuntimeResult(self.target, gateway_token))
        except RuntimeErrorMessage as exc:
            if not self.stop_event.is_set():
                self.failed.emit(str(exc))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.failed.emit(f"Local runtime setup failed: {exc}")

    def _compose_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LOCAL_CHAT_SECRETS_DIR": str(self.data_dir / "gateway-session"),
                "LOCAL_CHAT_GATEWAY_IMAGE": self.images.gateway,
                "LOCAL_CHAT_EXECUTOR_IMAGE": self.images.executor,
            }
        )
        if self.target.workspace:
            environment["LOCAL_CHAT_WORKSPACE"] = str(self.target.workspace)
            environment["LOCAL_CHAT_WORKSPACE_ID"] = self.target.workspace_identity or ""
        else:
            environment.pop("LOCAL_CHAT_WORKSPACE", None)
            environment["LOCAL_CHAT_WORKSPACE_ID"] = ""
        return environment

    def _runner_environment(self) -> dict[str, str]:
        """Volume key, runner image, and runner resources for the agent profile (plan 2.1-2.7)."""
        try:
            docker_cpus, docker_memory = (
                int(value) for value in self._run(DOCKER_RESOURCES, timeout=8).split()[:2]
            )
        except (RuntimeErrorMessage, ValueError):
            docker_cpus, docker_memory = 2, 4 * 1024**3
        cpus, memory_gb = self.settings.resolved(docker_cpus, docker_memory)
        return {
            "LOCAL_CHAT_WORKSPACE_KEY": workspace_key(self.target.workspace_identity or ""),
            "LOCAL_CHAT_RUNNER_IMAGE": self.images.runner(self.runner_target),
            "LOCAL_CHAT_RUNNER_TARGET": f"runner-{self.runner_target}",
            "LOCAL_CHAT_RUNNER_CPUS": f"{cpus:g}",
            "LOCAL_CHAT_RUNNER_MEMORY": f"{memory_gb}g",
            "LOCAL_CHAT_RUNNER_PIDS": str(self.settings.pids),
            "LOCAL_CHAT_GIT_HISTORY_DEPTH": str(self.settings.git_history_depth),
        }

    def _ensure_docker_ready(self) -> None:
        if not self._docker_responds():
            desktop = locate_docker_desktop()
            if desktop is None:
                raise RuntimeErrorMessage(
                    "Docker Desktop is installed incorrectly or is not running. "
                    "Start Docker Desktop and try again."
                )
            self.progress.emit("starting", "Starting Docker Desktop…")
            try:
                _spawn_detached([str(desktop)])
            except OSError as exc:
                raise RuntimeErrorMessage(f"Could not start Docker Desktop: {exc}") from exc
            deadline = time.monotonic() + DOCKER_START_TIMEOUT_SECONDS
            while not self._docker_responds():
                self._check_stopped()
                if time.monotonic() >= deadline:
                    raise RuntimeErrorMessage(
                        "Docker Desktop did not become ready within two minutes. "
                        "Open Docker Desktop, confirm Linux containers are enabled, and retry."
                    )
                time.sleep(1)
        try:
            self._run(["compose", "version"], timeout=8)
        except RuntimeErrorMessage as exc:
            raise RuntimeErrorMessage(
                "Docker Compose is not available in this Docker Desktop installation. "
                f"Update Docker Desktop and retry. ({exc})"
            ) from exc

    def _docker_responds(self) -> bool:
        try:
            self._run(DOCKER_INFO, timeout=8)
        except RuntimeErrorMessage:
            return False
        return True

    def _compose(
        self, arguments: list[str], *, timeout: float, allow_failure: bool = False
    ) -> str:
        return self._run(
            [*compose_base_args(self.compose_file, overlays=self.overlays), *arguments],
            timeout=timeout,
            allow_failure=allow_failure,
        )

    def _run(self, arguments: list[str], *, timeout: float, allow_failure: bool = False) -> str:
        assert self.docker is not None
        try:
            process = subprocess.Popen(
                [str(self.docker), *arguments],
                cwd=str(self.bundle),
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise RuntimeErrorMessage(f"Could not run Docker: {exc}") from exc

        output: list[str] = []
        chunks: Queue[str] = Queue()

        def read_output() -> None:
            if not process.stdout:
                return
            try:
                for line in process.stdout:
                    chunks.put(line)
            except OSError:
                return

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if self.stop_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return ""
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise RuntimeErrorMessage(
                    f"Docker command timed out: {' '.join(arguments[:3])}"
                )
            try:
                output.append(chunks.get(timeout=0.1))
            except Empty:
                continue
        reader.join(timeout=1)
        while not chunks.empty():
            output.append(chunks.get_nowait())
        text = "".join(output)
        if process.returncode and not allow_failure:
            raise RuntimeErrorMessage(self._command_error(arguments, text))
        return text

    def _command_error(self, arguments: list[str], output: str) -> str:
        summary = _redact_output(output, self.environment, self.secret_values)
        command = " ".join(arguments[:3])
        if "pull" in arguments:
            return (
                "Could not download the Local Chat runtime images. "
                "Check your internet connection and Docker Desktop registry access. "
                f"({command}: {summary})"
            )
        if "up" in arguments and self.target.workspace:
            return (
                "Could not start the isolated workspace. Docker may not be able to access "
                f"the selected folder. ({command}: {summary})"
            )
        return f"Docker command failed ({command}): {summary}"

    def _wait_for_readiness(self, gateway_token: str) -> None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        headers = {"Authorization": f"Bearer {gateway_token}"}
        last_detail = ""
        while time.monotonic() < deadline:
            self._check_stopped()
            try:
                with httpx.Client(timeout=3, follow_redirects=False) as client:
                    health = client.get(f"{DEFAULT_GATEWAY_URL}/health")
                    if health.status_code != 200:
                        raise RuntimeErrorMessage("Gateway health endpoint is not ready.")
                    status_response = client.get(
                        f"{DEFAULT_GATEWAY_URL}/v1/status", headers=headers
                    )
                    if status_response.status_code == 401:
                        raise RuntimeErrorMessage("Gateway credentials were rejected.")
                    status_response.raise_for_status()
                    payload = status_response.json()
                status = GatewayStatus.from_dict(payload) if isinstance(payload, dict) else None
                if status is None or not status.ready:
                    last_detail = "Gateway is still starting."
                elif not self.target.workspace:
                    return
                elif status.serves_workspace(self.target.workspace_identity):
                    return
                else:
                    last_detail = "The isolated executor is still preparing the workspace."
            except (
                httpx.HTTPError,
                OSError,
                TypeError,
                ValueError,
                RuntimeErrorMessage,
            ) as exc:
                last_detail = str(exc)
            self.progress.emit("checking", last_detail)
            time.sleep(1)
        raise RuntimeErrorMessage(
            f"The local runtime did not become ready within three minutes. {last_detail}"
        )

    def _check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise RuntimeErrorMessage("Runtime setup was cancelled.")


class RuntimeManager(QObject):
    """Own the app-controlled Docker lifecycle without exposing Docker to QML."""

    progressChanged = Signal(str, str)
    ready = Signal(object)
    failed = Signal(str)
    setupStarted = Signal()
    setupFinished = Signal(bool)

    def __init__(self, data_dir: Path, parent: QObject | None = None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.bundle = bundle_root()
        self.image_error = ""
        self.images: RuntimeImages | None = None
        try:
            self.images = RuntimeImages.for_bundle(self.bundle)
        except RuntimeErrorMessage as exc:
            self.image_error = str(exc)
        self.worker: RuntimeWorker | None = None
        self.target: RuntimeTarget | None = None

    def ensure(self, workspace: Path | None, settings: RuntimeSettings | None = None) -> None:
        target = RuntimeTarget.from_workspace(workspace)
        self.stop_worker()
        self.target = target
        self.setupStarted.emit()
        if self.images is None:
            self._failed(self.image_error)
            return
        worker = RuntimeWorker(
            bundle=self.bundle,
            data_dir=self.data_dir,
            target=target,
            images=self.images,
            settings=settings,
            parent=self,
        )
        worker.progress.connect(self.progressChanged)
        worker.succeeded.connect(self._ready)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        self.worker = worker
        worker.start()

    def stop_worker(self) -> None:
        worker = self.worker
        if worker is None:
            return
        worker.stop()
        if not worker.wait(1500):
            worker.terminate()
            worker.wait(500)
        self.worker = None

    def shutdown(self) -> None:
        target = self.target
        self.stop_worker()
        docker = locate_docker()
        compose_file = self.bundle / "docker" / "compose.yaml"
        if docker is None or not compose_file.is_file():
            return
        environment = {
            **os.environ,
            "LOCAL_CHAT_SECRETS_DIR": str(self.data_dir / "gateway-session"),
        }
        agent = bool(target and target.workspace)
        overlays: tuple[Path, ...] = ()
        if agent:
            with contextlib.suppress(RuntimeErrorMessage):
                overlays = egress_overlays(self.bundle, prebuilt=False)
        with contextlib.suppress(OSError):
            _spawn_detached(
                [
                    str(docker),
                    *compose_base_args(compose_file, overlays=overlays),
                    *(["--profile", "agent"] if agent else []),
                    "down",
                    "--remove-orphans",
                ],
                cwd=str(self.bundle),
                env=environment,
            )

    def staged_sessions(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[dict]:
        return staged_sessions(self._docker_cli(), self.data_dir, retention_days=retention_days)

    def remove_staged_session(self, key: str) -> None:
        identity = self.target.workspace_identity if self.target else None
        remove_staged_session(
            self._docker_cli(),
            self.data_dir,
            key,
            active_key=workspace_key(identity) if identity else None,
        )

    def _docker_cli(self) -> Callable[[list[str]], str]:
        docker = locate_docker()
        if docker is None:
            raise RuntimeErrorMessage("Docker Desktop is not installed.")

        def run(arguments: list[str]) -> str:
            try:
                completed = subprocess.run(
                    [str(docker), *arguments],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeErrorMessage(f"Docker command failed: {exc}") from exc
            if completed.returncode:
                raise RuntimeErrorMessage(
                    f"Docker command failed: {_redact_output(completed.stderr, {})}"
                )
            return completed.stdout

        return run

    def _ready(self, result: object) -> None:
        self.ready.emit(result)
        self.setupFinished.emit(True)

    def _failed(self, message: str) -> None:
        self.failed.emit(message)
        self.setupFinished.emit(False)

    def _finished(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.deleteLater()
        self.worker = None


def _redact_output(
    output: str, environment: dict[str, str], secret_values: tuple[str, ...] = ()
) -> str:
    value = output.strip()
    environment_secrets = (
        environment.get(key, "")
        for key in ("LOCAL_CHAT_GATEWAY_TOKEN", "LOCAL_CHAT_EXECUTOR_TOKEN")
    )
    for secret in (*environment_secrets, *secret_values):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    for token_name in ("gateway_token.txt", "executor_token.txt"):
        value = value.replace(token_name, "[TOKEN_FILE]")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "no diagnostic output"
    return " ".join(lines[-3:])[:1_200]
