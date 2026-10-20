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
SESSION_TOKEN_BYTES = 32
READINESS_TIMEOUT_SECONDS = 180
DOCKER_START_TIMEOUT_SECONDS = 120
EGRESS_MODE_ENV = "LOCAL_CHAT_EXECUTOR_EGRESS"
EGRESS_OVERLAY = "compose.egress.yaml"
IMAGE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,511}$")
IMAGE_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")
DOCKER_INFO = ["info", "--format", "{{.ServerVersion}}"]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeErrorMessage(RuntimeError):
    """An actionable, user-facing runtime setup failure."""


@dataclass(frozen=True, slots=True)
class RuntimeImages:
    gateway: str
    executor: str
    prebuilt: bool

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
        if data.get("schema_version") != 1:
            raise RuntimeErrorMessage(
                "The bundled Docker image manifest has an unsupported version."
            )
        gateway = _image_ref(data.get("gateway"), "gateway")
        executor = _image_ref(data.get("executor"), "executor")
        if not IMAGE_DIGEST_PATTERN.search(gateway) or not IMAGE_DIGEST_PATTERN.search(
            executor
        ):
            raise RuntimeErrorMessage("The bundled Docker image manifest is not digest-pinned.")
        return cls(gateway, executor, True)


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
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.bundle = bundle
        self.data_dir = data_dir
        self.target = target
        self.images = images
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
            if self.target.workspace:
                self.overlays = egress_overlays(self.bundle, prebuilt=self.images.prebuilt)
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

    def ensure(self, workspace: Path | None) -> None:
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
