from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from executor.paths import UnsafePath, assert_unique_paths, normalize_relative
from shared.tools import PUBLISH_BATCH_MAX_BYTES, PUBLISH_BATCH_MAX_OPERATIONS, PublishManifest

# Per-batch ceilings. Larger changesets arrive as ordered batches (see PublicationLedger).
MAX_PUBLISH_FILES = PUBLISH_BATCH_MAX_OPERATIONS
MAX_PUBLISH_BYTES = PUBLISH_BATCH_MAX_BYTES
FORBIDDEN_ROOT_NAMES = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "appdata",
        ".ssh",
        ".aws",
        ".azure",
        ".gnupg",
        "docker",
        "onedrive",
        "dropbox",
        "google drive",
        "icloud drive",
    }
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


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
        raise BrokerError(
            "Protected system, credential, or runtime directories cannot be workspaces"
        )
    if canonical == Path.home().resolve():
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
        """Publish one batch all-or-nothing.

        Each host file is copied to the batch's recovery checkpoint before it is touched. If
        any operation fails, every host file this batch already touched is restored from those
        copies before the error is raised, so a failed batch never leaves the workspace
        partially published; earlier batches stay published and a retry re-attempts only the
        failed batch and the remainder.
        """
        if manifest.workspace_id != self.identity:
            raise BrokerError("Manifest belongs to another workspace")
        if len(manifest.operations) > MAX_PUBLISH_FILES:
            raise BrokerError("Publish manifest exceeds the file limit")
        paths = [normalize_relative(item.path) for item in manifest.operations]
        assert_unique_paths(paths)
        if set(paths) != {normalize_relative(item) for item in approved_paths}:
            raise BrokerError("Approved paths do not exactly match the manifest")
        if sum(item.payload_bytes() for item in manifest.operations) > MAX_PUBLISH_BYTES:
            raise BrokerError("Publish manifest exceeds the byte limit")
        checkpoint_id = str(uuid.uuid4())
        checkpoint = self.recovery_root / checkpoint_id
        checkpoint.mkdir(mode=0o700)
        metadata: dict[str, dict[str, str | bool | int | None]] = {}
        completed: list[str] = []
        touched: list[str] = []
        try:
            for operation in manifest.operations:
                relative = normalize_relative(operation.path)
                target = self._target(relative, may_not_exist=operation.operation == "create")
                existed = target.exists()
                current_hash = _hash_file(target) if existed else None
                current_mode = _mode(target) if existed else None
                if current_hash != operation.base_sha256:
                    raise BrokerError(f"Host file changed after review: {relative}")
                if not _modes_equivalent(current_mode, operation.base_mode):
                    raise BrokerError(f"Host file permissions changed after review: {relative}")
                metadata[relative] = {
                    "existed": existed,
                    "base_sha256": current_hash,
                    "base_mode": current_mode,
                    "published_sha256": operation.staged_sha256,
                    "published_mode": operation.staged_mode,
                    "binary": operation.binary,
                }
                if existed:
                    recovery_file = checkpoint / "files" / relative
                    recovery_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, recovery_file, follow_symlinks=False)
                touched.append(relative)
                if operation.operation == "delete":
                    target.unlink()
                else:
                    if operation.content is None and operation.content_base64 is None:
                        raise BrokerError(f"Publication content is missing: {relative}")
                    _atomic_write(target, operation.payload(), operation.staged_mode)
                    if _hash_file(target) != operation.staged_sha256:
                        raise BrokerError(f"Post-write publication mismatch: {relative}")
                    if operation.staged_mode is not None and not _modes_equivalent(
                        _mode(target), operation.staged_mode
                    ):
                        raise BrokerError(f"Post-write permission mismatch: {relative}")
                completed.append(relative)
            (checkpoint / "manifest.json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
            )
            return PublishResult(checkpoint_id, tuple(completed))
        except Exception as exc:
            restored, rollback_errors = self._restore_touched(checkpoint, metadata, touched)
            (checkpoint / "partial.json").write_text(
                json.dumps(
                    {
                        "completed": completed,
                        "restored": restored,
                        "rollback_errors": rollback_errors,
                    }
                ),
                encoding="utf-8",
            )
            if manifest.batch_count > 1:
                position = f"batch {manifest.batch_index} of {manifest.batch_count}"
            else:
                position = "publication"
            if rollback_errors:
                outcome = (
                    f"{len(restored)} of {len(touched)} touched file(s) were restored; could not "
                    f"restore {', '.join(rollback_errors[:5])}. Recovery copies are in "
                    f"checkpoint {checkpoint_id}."
                )
            else:
                outcome = (
                    f"the {len(touched)} file(s) it had touched were restored, so none of this "
                    "batch remains on the host."
                )
            if isinstance(exc, BrokerError | UnsafePath):
                reason = str(exc)
            else:
                reason = f"{type(exc).__name__}: {exc}"
            raise BrokerError(
                f"Publication stopped ({position}) after {len(completed)} operation(s): "
                f"{reason}; {outcome}"
            ) from exc

    def _restore_touched(
        self,
        checkpoint: Path,
        metadata: dict[str, dict[str, str | bool | int | None]],
        touched: list[str],
    ) -> tuple[list[str], list[str]]:
        """Restore touched host files, newest first; return restored and failed paths."""
        restored: list[str] = []
        errors: list[str] = []
        for relative in reversed(touched):
            item = metadata.get(relative) or {}
            try:
                target = self._target(relative, may_not_exist=True)
                if item.get("existed"):
                    base_mode = item.get("base_mode")
                    _atomic_write(
                        target,
                        (checkpoint / "files" / relative).read_bytes(),
                        int(base_mode) if base_mode is not None else None,
                    )
                    if _hash_file(target) != item.get("base_sha256"):
                        raise BrokerError("restored content mismatch")
                elif target.exists():
                    target.unlink()
                restored.append(relative)
            except Exception:
                errors.append(relative)
        return restored, errors

    def _target(self, relative: str, *, may_not_exist: bool) -> Path:
        """The host path for ``relative``, rejecting links, hard links, and escapes."""
        target = self.root.joinpath(*relative.split("/"))
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if not current.exists():
                break
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise BrokerError(f"Link or reparse point rejected: {relative}")
            if current.is_file() and info.st_nlink > 1:
                raise BrokerError(f"Hard-linked write target rejected: {relative}")
        ancestor = target.parent
        while not ancestor.exists() and ancestor != self.root:
            ancestor = ancestor.parent
        if not ancestor.resolve(strict=True).is_relative_to(self.root):
            raise BrokerError("Publish target escaped the workspace")
        if not may_not_exist and not target.is_file():
            raise BrokerError(f"Expected host file is missing: {relative}")
        return target


def _atomic_write(
    target: Path, content: bytes, mode: int | None = None, *, prefix: str = ".local-chat-"
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
        if mode is not None:
            os.chmod(target, mode)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _modes_equivalent(actual: int | None, expected: int | None) -> bool:
    if actual is None or expected is None:
        return actual == expected
    # Windows chmod/stat only has meaningful read-only semantics. Comparing the full
    # POSIX mode causes false publication failures for otherwise identical files.
    if os.name == "nt":
        return bool(actual & 0o200) == bool(expected & 0o200)
    return actual == expected


def _hash_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink > 1:
        raise BrokerError("Publish target is not a safe regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicationLedger:
    """Durable per-publication progress, stored with the broker's recovery copies.

    Each batch of a (possibly multi-batch) publication is recorded as it is published,
    fails, or cannot advance the staging baseline, so the approver can see exactly which
    batches reached the host and a retry resumes with the unpublished remainder.
    """

    def __init__(self, recovery_root: Path):
        self.directory = recovery_root.resolve() / "publications"

    def _path(self, publication_id: str) -> Path:
        if not publication_id or any(
            char not in "0123456789abcdef-" for char in publication_id.casefold()
        ):
            raise BrokerError("Invalid publication identity")
        return self.directory / f"{publication_id}.json"

    def load(self, publication_id: str) -> dict | None:
        try:
            value = json.loads(self._path(publication_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def record(
        self,
        manifest: PublishManifest,
        status: str,
        *,
        checkpoint_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        if status not in {"published", "failed", "baseline_failed"}:
            raise BrokerError("Unknown publication batch status")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        record = self.load(manifest.publication_id) or {
            "publication_id": manifest.publication_id,
            "workspace_id": manifest.workspace_id,
            "run_id": manifest.run_id,
            "batches": {},
        }
        batches = record.setdefault("batches", {})
        batches[str(manifest.batch_index)] = {
            "manifest_id": manifest.manifest_id,
            "status": status,
            "operations": len(manifest.operations),
            "checkpoint_id": checkpoint_id,
            "error": (error or "")[:2000] or None,
            "recorded_at": now,
        }
        published = sorted(
            int(index) for index, item in batches.items() if item.get("status") == "published"
        )
        record["batch_count"] = manifest.batch_count
        record["published_batches"] = published
        record["remaining_batches"] = max(0, manifest.batch_count - len(published))
        if status == "published":
            record["state"] = "in_progress" if manifest.has_more_batches else "completed"
        else:
            record["state"] = status
        record["updated_at"] = now
        _atomic_write(
            self._path(manifest.publication_id),
            json.dumps(record, indent=2, sort_keys=True).encode("utf-8"),
            prefix=".ledger-",
        )
        return record


def describe_progress(record: dict | None) -> str:
    """A one-line human summary of a multi-batch publication ledger record."""
    count = int((record or {}).get("batch_count") or 1)
    if count <= 1:
        return ""
    published = record.get("published_batches") or []
    remaining = int(record.get("remaining_batches") or 0)
    return f"{len(published)} of {count} publication batch(es) are on the host; {remaining} remain."
