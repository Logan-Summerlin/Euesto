"""Bring host changes made since the staging baseline into persistent staging (plan 2.4, 2.5).

For each path, compare the baseline (B), the host file now (H), and the staged file now (S):

* H == B: the host did not change it; nothing to do.
* S == H: both sides made the same change; the baseline moves to H.
* S == B: only the host changed it; staging and the baseline take the host version.
* otherwise: a *conflict*. Staging keeps its version and the baseline is unchanged, so the
  file is withheld from publication until the user resolves it (keep staged, or take host).

Only host files whose (size, mtime) signature changed are re-hashed, so a restart over an
unchanged workspace costs one ``stat`` per file instead of a full copy.
"""

from __future__ import annotations

import os
import stat
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExecutorConfig
from .errors import INVALID_ARGUMENTS, ExecutorToolError
from .paths import (
    UnsafePath,
    is_secret_path,
    is_staging_excluded,
    normalize_relative,
    safe_path,
)
from .staging import (
    Snapshot,
    copy_with_sha256,
    sha256_file,
    visible_files,
)
from .state import Baseline, Conflict

_Entry = tuple[str, int] | None  # (sha256, mode)


@dataclass(frozen=True, slots=True)
class HostFile:
    sha256: str
    size: int
    mode: int
    signature: tuple[int, int]


@dataclass(slots=True)
class RebaseReport:
    absorbed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "absorbed": self.absorbed,
            "conflicts": self.conflicts,
            "resolved": self.resolved,
            "changed": bool(self.absorbed or self.resolved),
        }


def host_files(config: ExecutorConfig, previous: Snapshot) -> dict[str, HostFile]:
    """The publishable host files now, hashing only those whose signature changed."""
    source = config.source_root.resolve(strict=True)
    result: dict[str, HostFile] = {}
    total = 0
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        retained: list[str] = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            relative = path.relative_to(source).as_posix()
            if stat.S_ISLNK(path.lstat().st_mode):
                raise UnsafePath(f"Source link is forbidden: {relative}")
            if not (is_secret_path(relative) or is_staging_excluded(relative)):
                retained.append(dirname)
        dirnames[:] = retained
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(source).as_posix()
            if is_secret_path(relative) or is_staging_excluded(relative):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise UnsafePath(f"Source link is forbidden: {relative}")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise UnsafePath(f"Unsupported source file: {relative}")
            signature = (info.st_size, info.st_mtime_ns)
            if (
                previous.source_signatures.get(relative) == signature
                and relative in previous.hashes
            ):
                digest = previous.hashes[relative]
            else:
                digest = sha256_file(path)
            total += info.st_size
            if len(result) >= config.max_staged_files or total > config.max_staging_bytes:
                raise RuntimeError("Workspace exceeds executor staging limits during rebase.")
            result[relative] = HostFile(
                digest, info.st_size, stat.S_IMODE(info.st_mode), signature
            )
    return result


def rebase(
    config: ExecutorConfig,
    baseline: Baseline,
    *,
    keep_staged: Iterable[str] = (),
    take_host: Iterable[str] = (),
) -> tuple[Baseline, RebaseReport]:
    snapshot = baseline.snapshot
    keep = {normalize_relative(path) for path in keep_staged}
    take = {normalize_relative(path) for path in take_host}
    unknown = (keep | take) - set(baseline.conflicts)
    if unknown or keep & take:
        raise ExecutorToolError(
            INVALID_ARGUMENTS,
            "Resolve only current conflicts, each with exactly one choice: "
            + ", ".join(sorted(unknown | (keep & take))),
        )
    host = host_files(config, snapshot)
    staged = visible_files(config.work_root)
    hashes = dict(snapshot.hashes)
    sizes = dict(snapshot.sizes)
    modes = dict(snapshot.modes)
    signatures = dict(snapshot.source_signatures)
    conflicts = dict(baseline.conflicts)
    report = RebaseReport()

    def adopt(path: str) -> None:
        item = host.get(path)
        if item is None:
            for table in (hashes, sizes, modes, signatures):
                table.pop(path, None)
        else:
            hashes[path], sizes[path], modes[path] = item.sha256, item.size, item.mode
            signatures[path] = item.signature

    paths = sorted(set(host) | set(hashes) | set(conflicts), key=str.casefold)
    for path in paths:
        base = _entry(hashes.get(path), modes.get(path))
        now = host.get(path)
        host_entry = None if now is None else (now.sha256, now.mode)
        staged_value = staged.get(path)
        staged_entry = None if staged_value is None else (staged_value[0], staged_value[2])
        if path in take:
            _install(config, path, now)
            adopt(path)
            conflicts.pop(path, None)
            report.resolved.append(path)
        elif path in keep:
            adopt(path)
            conflicts.pop(path, None)
            report.resolved.append(path)
        elif host_entry == base:
            if now is not None:
                signatures[path] = now.signature
            if conflicts.pop(path, None) is not None:
                report.resolved.append(path)
        elif staged_entry == host_entry:
            adopt(path)
            conflicts.pop(path, None)
            report.absorbed.append(path)
        elif staged_entry == base:
            try:
                _install(config, path, now)
            except (UnsafePath, OSError):
                conflicts[path] = _conflict(path, base, host_entry, staged_entry)
                report.conflicts.append(path)
                continue
            adopt(path)
            conflicts.pop(path, None)
            report.absorbed.append(path)
        else:
            conflicts[path] = _conflict(path, base, host_entry, staged_entry)
            report.conflicts.append(path)
    changed = report.absorbed or report.resolved
    updated = Snapshot(
        str(uuid.uuid4()) if changed else snapshot.snapshot_id,
        hashes,
        sum(sizes.values()),
        sizes,
        modes,
        signatures,
    )
    if changed:
        visible_files(config.work_root)
    return Baseline(updated, conflicts), report


def _entry(digest: str | None, mode: int | None) -> _Entry:
    return None if digest is None else (digest, int(mode if mode is not None else 0o644))


def _conflict(path: str, base: _Entry, host: _Entry, staged: _Entry) -> Conflict:
    return Conflict(
        path,
        None if base is None else base[0],
        None if host is None else host[0],
        None if staged is None else staged[0],
    )


def _install(config: ExecutorConfig, path: str, host: HostFile | None) -> None:
    """Make the staged file match the host file (or remove it), never through a link."""
    target = safe_path(config.work_root, path, must_exist=False)
    if host is None:
        if target.is_symlink() or target.is_file():
            target.unlink()
        return
    source = config.source_root.joinpath(*path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_path(config.work_root, path, must_exist=False)
    descriptor, name = tempfile.mkstemp(prefix=".local-chat-rebase-", dir=target.parent)
    temporary = Path(name)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with (
            os.fdopen(os.open(source, flags), "rb") as reader,
            os.fdopen(descriptor, "wb") as writer,
        ):
            digest = copy_with_sha256(reader, writer)
        if digest != host.sha256:
            raise OSError(f"Host file changed during rebase: {path}")
        os.chmod(temporary, host.mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
