from __future__ import annotations

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

from .workspace_broker import canonical_workspace, workspace_id

PROJECT_NAME = "local-openrouter-chat"
GATEWAY_URL = "http://127.0.0.1:8765"
DEFAULT_GATEWAY_IMAGE = "local-openrouter-chat-gateway:1.1.0"
DEFAULT_EXECUTOR_IMAGE = "local-openrouter-chat-executor:1.1.0"
SESSION_TOKEN_BYTES = 32
READINESS_TIMEOUT_SECONDS = 180
DOCKER_START_TIMEOUT_SECONDS = 120
IMAGE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,511}$")
IMAGE_DIGEST_PATTERN = re.compile(r"@sha256:[0-9a-f]{64}$")


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
            raise RuntimeErrorMessage(f"The bundled Docker image manifest is invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeErrorMessage("The bundled Docker image manifest must be an object.")
        if data.get("schema_version") != 1:
            raise RuntimeErrorMessage("The bundled Docker image manifest has an unsupported version.")
        gateway = _image_ref(data.get("gateway"), "gateway")
        executor = _image_ref(data.get("executor"), "executor")
        if not IMAGE_DIGEST_PATTERN.search(gateway) or not IMAGE_DIGEST_PATTERN.search(executor):
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
    gateway_url: str = GATEWAY_URL
    prebuilt: bool = False


def bundle_root() -> Path:
    """Return the source root or the PyInstaller extraction root."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parents[1]


def locate_docker() -> Path | None:
    candidates: list[Path] = []
    found = shutil.which("docker")
    if found:
        candidates.append(Path(found))
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if program_files:
            candidates.append(Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Docker" / "resources" / "bin" / "docker.exe")
    return next((path for path in candidates if path.is_file()), None)


def locate_docker_desktop() -> Path | None:
    if sys.platform != "win32":
        return None
    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if program_files:
        candidates.append(Path(program_files) / "Docker" / "Docker" / "Docker Desktop.exe")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "DockerDesktop" / "Docker Desktop.exe"
        )
    return next((path for path in candidates if path.is_file()), None)


def create_session_tokens(session_dir: Path) -> tuple[str, str]:
    session_dir.mkdir(parents=True, exist_ok=True)
    gateway_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    executor_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    _write_secret(session_dir / "gateway_token.txt", gateway_token)
    _write_secret(session_dir / "executor_token.txt", executor_token)
    return gateway_token, executor_token


def compose_base_args(compose_file: Path, *, project_name: str = PROJECT_NAME) -> list[str]:
    return [
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(compose_file),
    ]


def _image_ref(value: object, name: str) -> str:
    reference = str(value or "").strip()
    if not IMAGE_REF_PATTERN.fullmatch(reference):
        raise RuntimeErrorMessage(f"The bundled {name} image reference is invalid.")
    return reference


def _write_secret(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


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

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            if not self.compose_file.is_file():
                raise RuntimeErrorMessage("The bundled Docker Compose file is missing.")
            self.docker = locate_docker()
            if self.docker is None:
                raise RuntimeErrorMessage(
                    "Docker Desktop is not installed or docker.exe is unavailable. "
                    "Install Docker Desktop using Linux containers, then try again."
                )
            gateway_token, executor_token = create_session_tokens(
                self.data_dir / "gateway-session"
            )
            self.secret_values = (gateway_token, executor_token)
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
            self.succeeded.emit(
                RuntimeResult(self.target, gateway_token, prebuilt=self.images.prebuilt)
            )
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
        assert self.docker is not None
        try:
            self._run(["info", "--format", "{{.ServerVersion}}"], timeout=8)
        except RuntimeErrorMessage:
            desktop = locate_docker_desktop()
            if desktop is None:
                raise RuntimeErrorMessage(
                    "Docker Desktop is installed incorrectly or is not running. "
                    "Start Docker Desktop and try again."
                ) from None
            self.progress.emit("starting", "Starting Docker Desktop…")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            try:
                subprocess.Popen(
                    [str(desktop)],
                    close_fds=True,
                    creationflags=flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise RuntimeErrorMessage(f"Could not start Docker Desktop: {exc}") from exc

        deadline = time.monotonic() + DOCKER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._check_stopped()
            try:
                self._run(["info", "--format", "{{.ServerVersion}}"], timeout=8)
            except RuntimeErrorMessage:
                time.sleep(1)
                continue
            try:
                self._run(["compose", "version"], timeout=8)
            except RuntimeErrorMessage as exc:
                raise RuntimeErrorMessage(
                    "Docker Compose is not available in this Docker Desktop installation. "
                    f"Update Docker Desktop and retry. ({exc})"
                ) from exc
            return
        raise RuntimeErrorMessage(
            "Docker Desktop did not become ready within two minutes. "
            "Open Docker Desktop, confirm Linux containers are enabled, and retry."
        )

    def _compose(
        self, arguments: list[str], *, timeout: float, allow_failure: bool = False
    ) -> str:
        return self._run(
            [*compose_base_args(self.compose_file), *arguments],
            timeout=timeout,
            allow_failure=allow_failure,
        )

    def _run(
        self,
        arguments: list[str],
        *,
        timeout: float,
        allow_failure: bool = False,
    ) -> str:
        assert self.docker is not None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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
                creationflags=flags,
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
        while True:
            try:
                output.append(chunks.get_nowait())
            except Empty:
                break
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
                    health = client.get(f"{GATEWAY_URL}/health")
                    if health.status_code != 200:
                        raise RuntimeErrorMessage("Gateway health endpoint is not ready.")
                    status_response = client.get(f"{GATEWAY_URL}/v1/status", headers=headers)
                    if status_response.status_code == 401:
                        raise RuntimeErrorMessage("Gateway credentials were rejected.")
                    status_response.raise_for_status()
                    status = status_response.json()
                if not isinstance(status, dict) or not status.get("ready"):
                    last_detail = "Gateway is still starting."
                elif self.target.workspace:
                    active = str(status.get("active_workspace") or "")
                    supported = {str(value) for value in status.get("supported_modes") or ()}
                    if (
                        active == self.target.workspace_identity
                        and status.get("executor_present") is True
                        and status.get("executor_status") == "ready"
                        and {"plan", "agent"}.issubset(supported)
                    ):
                        return
                    last_detail = "The isolated executor is still preparing the workspace."
                else:
                    return
            except (httpx.HTTPError, OSError, ValueError, RuntimeErrorMessage) as exc:
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
        self.require_manifest = bool(getattr(sys, "frozen", False))
        self.image_error = ""
        self.images: RuntimeImages | None = None
        try:
            self.images = RuntimeImages.for_bundle(
                self.bundle, require_manifest=self.require_manifest
            )
        except RuntimeErrorMessage as exc:
            self.image_error = str(exc)
        self.worker: RuntimeWorker | None = None
        self.target: RuntimeTarget | None = None

    @property
    def prebuilt(self) -> bool:
        return bool(self.images and self.images.prebuilt)

    def ensure(self, workspace: Path | None) -> None:
        target = RuntimeTarget.from_workspace(workspace)
        self.stop_worker()
        self.target = target
        self.setupStarted.emit()
        if self.image_error:
            self._failed(self.image_error)
            return
        if self.images is None:
            self._failed("The local Docker image configuration is unavailable.")
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

    def retry(self) -> None:
        target = self.target
        self.ensure(target.workspace if target else None)

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
        environment = os.environ.copy()
        environment["LOCAL_CHAT_SECRETS_DIR"] = str(self.data_dir / "gateway-session")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        try:
            subprocess.Popen(
                [
                    str(docker),
                    *compose_base_args(compose_file),
                    *( ["--profile", "agent"] if target and target.workspace else [] ),
                    "down",
                    "--remove-orphans",
                ],
                cwd=str(self.bundle),
                env=environment,
                close_fds=True,
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

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
    output: str,
    environment: dict[str, str],
    secret_values: tuple[str, ...] = (),
) -> str:
    value = output.strip()
    for secret in (
        *(environment.get(key, "") for key in ("LOCAL_CHAT_GATEWAY_TOKEN", "LOCAL_CHAT_EXECUTOR_TOKEN")),
        *secret_values,
    ):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    for token_name in ("gateway_token.txt", "executor_token.txt"):
        value = value.replace(token_name, "[TOKEN_FILE]")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "no diagnostic output"
    return " ".join(lines[-3:])[:1_200]
