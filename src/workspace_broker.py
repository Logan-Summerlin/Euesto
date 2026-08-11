from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from executor.paths import UnsafePath, assert_unique_paths, normalize_relative
from shared.tools import PublishManifest

MAX_PUBLISH_FILES = 500
MAX_PUBLISH_BYTES = 32_000_000
FORBIDDEN_ROOT_NAMES = frozenset({
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    ".ssh", ".aws", ".azure", ".gnupg", "docker", "onedrive", "dropbox",
    "google drive", "icloud drive",
})


class BrokerError(RuntimeError):
    pass


def workspace_id(root: Path) -> str:
    canonical = canonical_workspace(root)
    return hashlib.sha256(os.path.normcase(str(canonical)).encode("utf-8")).hexdigest()


def canonical_workspace(root: Path) -> Path:
    if not root.is_dir() or root.is_symlink():
        raise BrokerError("Workspace must be an existing ordinary directory")
    canonical = root.resolve(strict=True)
    anchor = Path(canonical.anchor)
    if canonical == anchor or len(canonical.parts) < len(anchor.parts) + 2:
        raise BrokerError("Drive, profile, and other broad roots cannot be workspaces")
    if any(part.casefold() in FORBIDDEN_ROOT_NAMES for part in canonical.parts):
        raise BrokerError("Protected system, credential, or runtime directories cannot be workspaces")
    home = Path.home().resolve()
    if canonical == home:
        raise BrokerError("The user profile root cannot be a workspace")
    return canonical


@dataclass(frozen=True, slots=True)
class PublishResult:
    checkpoint_id: str
    completed_paths: tuple[str, ...]


class WorkspaceBroker:
    def __init__(self, root: Path, recovery_root: Path):
        self.root = canonical_workspace(root)
        self.recovery_root = recovery_root.resolve()
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        if self.recovery_root.is_relative_to(self.root):
            raise BrokerError("Recovery storage must be outside the workspace")
        self.identity = workspace_id(self.root)

    def publish(self, manifest: PublishManifest, approved_paths: set[str]) -> PublishResult:
        if manifest.workspace_id != self.identity:
            raise BrokerError("Manifest belongs to another workspace")
        if len(manifest.operations) > MAX_PUBLISH_FILES:
            raise BrokerError("Publish manifest exceeds the file limit")
        paths = [normalize_relative(item.path) for item in manifest.operations]
        assert_unique_paths(paths)
        if set(paths) != {normalize_relative(item) for item in approved_paths}:
            raise BrokerError("Approved paths do not exactly match the manifest")
        total = sum(len((item.content or "").encode("utf-8")) for item in manifest.operations)
        if total > MAX_PUBLISH_BYTES:
            raise BrokerError("Publish manifest exceeds the byte limit")
        checkpoint_id = str(uuid.uuid4())
        checkpoint = self.recovery_root / checkpoint_id
        checkpoint.mkdir(mode=0o700)
        metadata: dict[str, dict[str, str | bool | None]] = {}
        completed: list[str] = []
        try:
            for operation in manifest.operations:
                relative = normalize_relative(operation.path)
                target = self._target(relative, may_not_exist=operation.operation == "create")
                current_hash = _hash_file(target) if target.exists() else None
                if current_hash != operation.base_sha256:
                    raise BrokerError(f"Host file changed after review: {relative}")
                metadata[relative] = {
                    "existed": target.exists(), "base_sha256": current_hash,
                    "published_sha256": operation.staged_sha256,
                }
                if target.exists():
                    recovery_file = checkpoint / "files" / relative
                    recovery_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, recovery_file, follow_symlinks=False)
                if operation.operation == "delete":
                    target.unlink()
                else:
                    self._atomic_write(target, operation.content or "")
                    if _hash_file(target) != operation.staged_sha256:
                        raise BrokerError(f"Post-write hash mismatch: {relative}")
                completed.append(relative)
            (checkpoint / "manifest.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
            return PublishResult(checkpoint_id, tuple(completed))
        except Exception as exc:
            (checkpoint / "partial.json").write_text(json.dumps({"completed": completed}), encoding="utf-8")
            if isinstance(exc, (BrokerError, UnsafePath)):
                raise BrokerError(str(exc)) from exc
            raise BrokerError(f"Publication stopped after {len(completed)} operation(s): {exc}") from exc

    def undo(self, checkpoint_id: str) -> PublishResult:
        if not checkpoint_id or any(char not in "0123456789abcdef-" for char in checkpoint_id.casefold()):
            raise BrokerError("Invalid checkpoint identity")
        checkpoint = self.recovery_root / checkpoint_id
        metadata = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
        completed: list[str] = []
        for relative, item in metadata.items():
            target = self._target(normalize_relative(relative), may_not_exist=True)
            current = _hash_file(target) if target.exists() else None
            if current != item["published_sha256"]:
                raise BrokerError(f"Undo conflict: {relative} changed after publication")
            recovery = checkpoint / "files" / relative
            if item["existed"]:
                self._atomic_write(target, recovery.read_text(encoding="utf-8"))
            elif target.exists():
                target.unlink()
            completed.append(relative)
        return PublishResult(checkpoint_id, tuple(completed))

    def _target(self, relative: str, *, may_not_exist: bool) -> Path:
        target = self.root.joinpath(*relative.split("/"))
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if not current.exists():
                break
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise BrokerError(f"Link or reparse point rejected: {relative}")
            if current.is_file() and info.st_nlink > 1:
                raise BrokerError(f"Hard-linked write target rejected: {relative}")
        ancestor = target.parent
        while not ancestor.exists() and ancestor != self.root:
            ancestor = ancestor.parent
        parent = ancestor.resolve(strict=True)
        if not parent.is_relative_to(self.root):
            raise BrokerError("Publish target escaped the workspace")
        if not may_not_exist and not target.is_file():
            raise BrokerError(f"Expected host file is missing: {relative}")
        return target

    def _atomic_write(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        descriptor, name = tempfile.mkstemp(prefix=".local-chat-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _hash_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink > 1:
        raise BrokerError("Publish target is not a safe regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()
