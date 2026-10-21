"""Git history in the sandbox (plan 2.6).

At seed time, before the runner has ever touched the work volume, the executor bare-clones the
host repository from the read-only ``/source`` into ``/work/.git`` with a fixed, credential-free
configuration, then replaces the clone's configuration with a minimal one: no remotes, no
credential helpers, hooks disabled.

The seeded tree is recorded as the local ``refs/euesto/base`` commit, and the index points at
that tree, so in the runner ``git diff euesto/base`` shows exactly the agent's changes,
``git diff HEAD`` adds the user's uncommitted work, and ``git log``/``git blame`` show history.

After seeding, the runner may have modified anything under ``.git``, so the executor never runs
``git`` there again: it updates the base commit and the index by writing git's file formats
itself, opening every directory without following links. ``.git`` stays excluded from the file
tools, checkpoints, and publication, and checkpoint rollback does not rewind git state.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import struct
import subprocess
import time
import zlib
from pathlib import Path

from .staging import Snapshot

GIT = "git"
BASE_REF = "refs/euesto/base"
STATE_FILE = "git_base.json"
CLONE_TIMEOUT_SECONDS = 600
CLEAN_CONFIG = (
    "[core]\n"
    "\trepositoryformatversion = 0\n"
    "\tfilemode = true\n"
    "\tbare = false\n"
    "\tlogallrefupdates = false\n"
    "\thooksPath = /dev/null\n"
    "\tfsmonitor = false\n"
)
SAFE_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
SAFE_OPTIONS = (
    "-c",
    "protocol.file.allow=always",
    "-c",
    "safe.directory=*",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
)
_AUTHOR = b"Euesto <euesto@localhost>"
# Harness-owned paths in staging that git should not report as untracked work.
LOCAL_EXCLUDES = b".euesto-output/\n"


def seed_repository(
    source_root: Path, work_root: Path, state_root: Path, snapshot: Snapshot, depth: int
) -> dict[str, object]:
    """Clone host history into fresh staging and record the base commit. Never raises."""
    (state_root / STATE_FILE).unlink(missing_ok=True)
    if not (source_root / ".git").exists():
        return _status(False, depth, "the workspace is not a git repository")
    if shutil.which(GIT, path=SAFE_ENVIRONMENT["PATH"]) is None:
        return _status(False, depth, "git is not installed in the executor image")
    git_dir = work_root / ".git"
    try:
        if os.path.lexists(git_dir):
            return _status(False, depth, "staging already holds a .git entry")
        arguments = [
            GIT,
            *SAFE_OPTIONS,
            "clone",
            "--bare",
            "--quiet",
            "--no-hardlinks",
            "--single-branch",
            "--no-tags",
        ]
        if depth:
            arguments += ["--depth", str(depth)]
        arguments += [f"file://{source_root.resolve()}", str(git_dir)]
        subprocess.run(
            arguments,
            env=SAFE_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CLONE_TIMEOUT_SECONDS,
            check=True,
        )
        if _git_output(git_dir, "rev-parse", "--show-object-format") != "sha1":
            raise RuntimeError("only SHA-1 repositories are supported")
        head = _git_output(git_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") or None
        blobs = _hash_base_files(git_dir, work_root, snapshot)
        # Replace the clone's configuration: no remote URL, fetch refspec, or branch tracking
        # from the host survives, and nothing in it can run a program.
        _write_bytes(work_root, (".git", "config"), CLEAN_CONFIG.encode("ascii"))
        _write_bytes(work_root, (".git", "info", "exclude"), LOCAL_EXCLUDES)
        for name in ("hooks", "FETCH_HEAD", "description"):
            target = git_dir / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        _remove(git_dir)
        reason = (
            exc.stderr.decode("utf-8", "replace")[-300:]
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else str(exc)[:300]
        )
        return _status(False, depth, f"clone failed: {reason.strip() or type(exc).__name__}")
    state = {"head": head, "files": {}, "commit": None}
    try:
        state = _write_base(work_root, snapshot, blobs, head)
    except (OSError, ValueError) as exc:
        _remove(git_dir)
        return _status(False, depth, f"base commit failed: {exc}")
    _save_state(state_root, {**state, "depth": depth})
    return _status(True, depth, None, state)


def update_base(work_root: Path, state_root: Path, snapshot: Snapshot) -> dict[str, object]:
    """Move ``refs/euesto/base`` (and the index) to a new publication baseline natively."""
    state = _load_state(state_root)
    if state is None:
        return {"available": False}
    depth = int(state.get("depth") or 0)
    known: dict[str, list] = dict(state.get("files") or {})
    blobs: dict[str, str] = {}
    try:
        for path, digest in snapshot.hashes.items():
            previous = known.get(path)
            if previous and previous[0] == digest:
                blobs[path] = previous[2]
                continue
            content = _baseline_content(work_root, path, digest)
            if content is None:
                raise ValueError(f"baseline content unavailable for {path}")
            blobs[path] = _write_object(work_root, b"blob", content)
        new_state = _write_base(work_root, snapshot, blobs, state.get("head"))
    except (OSError, ValueError) as exc:
        return _status(False, depth, f"base update failed: {exc}")
    _save_state(state_root, {**new_state, "depth": depth})
    return _status(True, depth, None, new_state)


def repository_status(state_root: Path, depth: int) -> dict[str, object]:
    state = _load_state(state_root)
    if state is None:
        return _status(False, depth, "not seeded")
    return _status(True, int(state.get("depth") or depth), None, state)


# -- seeding helpers (git runs only here, before the runner has seen the volume) ---------------


def _git_output(git_dir: Path, *arguments: str, stdin: bytes | None = None) -> str:
    completed = subprocess.run(
        [GIT, *SAFE_OPTIONS, f"--git-dir={git_dir}", *arguments],
        env=SAFE_ENVIRONMENT,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 and arguments[:1] != ("rev-parse",):
        raise RuntimeError(completed.stderr.decode("utf-8", "replace")[-300:])
    return completed.stdout.decode("utf-8", "replace").strip()


def _hash_base_files(git_dir: Path, work_root: Path, snapshot: Snapshot) -> dict[str, str]:
    """Write a blob for every baseline file; objects already in the clone are not rewritten."""
    paths = sorted(snapshot.hashes)
    if not paths:
        return {}
    listing = "".join(str(work_root.joinpath(*path.split("/"))) + "\n" for path in paths)
    output = _git_output(
        git_dir, "hash-object", "-w", "--no-filters", "--stdin-paths", stdin=listing.encode()
    )
    digests = output.splitlines()
    if len(digests) != len(paths):
        raise RuntimeError("git hash-object returned an unexpected number of objects")
    return dict(zip(paths, digests, strict=True))


# -- native git formats --------------------------------------------------------------------------


def _write_base(
    work_root: Path, snapshot: Snapshot, blobs: dict[str, str], head: object
) -> dict[str, object]:
    entries = {
        path: (_git_mode(snapshot.modes.get(path, 0o644)), blobs[path]) for path in blobs
    }
    tree = _write_tree(work_root, entries)
    stamp = f"{int(time.time())} +0000".encode()
    lines = [b"tree " + tree.encode()]
    if isinstance(head, str) and len(head) == 40:
        lines.append(b"parent " + head.encode())
    lines += [
        b"author " + _AUTHOR + b" " + stamp,
        b"committer " + _AUTHOR + b" " + stamp,
        b"",
        b"Euesto staging baseline",
        b"",
    ]
    commit = _write_object(work_root, b"commit", b"\n".join(lines))
    _write_bytes(work_root, (".git", *BASE_REF.split("/")), commit.encode() + b"\n")
    _write_index(work_root, snapshot, entries)
    return {
        "head": head if isinstance(head, str) else None,
        "commit": commit,
        "files": {
            path: [snapshot.hashes[path], snapshot.modes.get(path, 0o644), blobs[path]]
            for path in sorted(blobs)
        },
    }


def _git_mode(mode: int) -> bytes:
    return b"100755" if mode & 0o111 else b"100644"


def _write_tree(work_root: Path, entries: dict[str, tuple[bytes, str]]) -> str:
    root: dict[str, object] = {}
    for path, value in entries.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[parts[-1]] = value

    def write(node: dict[str, object]) -> str:
        items = []
        for name, value in node.items():
            if isinstance(value, dict):
                items.append((name.encode() + b"/", b"40000", name.encode(), write(value)))
            else:
                mode, digest = value  # type: ignore[misc]
                items.append((name.encode(), mode, name.encode(), digest))
        body = b"".join(
            mode + b" " + name + b"\0" + bytes.fromhex(digest)
            for _key, mode, name, digest in sorted(items)
        )
        return _write_object(work_root, b"tree", body)

    return write(root)


def _write_object(work_root: Path, kind: bytes, body: bytes) -> str:
    raw = kind + b" " + str(len(body)).encode() + b"\0" + body
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
    parts = (".git", "objects", digest[:2])
    directory = _open_directory(work_root, parts, create=True)
    try:
        try:
            os.stat(digest[2:], dir_fd=directory, follow_symlinks=False)
            return digest
        except FileNotFoundError:
            pass
        _replace_in(directory, digest[2:], zlib.compress(raw), 0o444)
    finally:
        os.close(directory)
    return digest


def _write_index(
    work_root: Path, snapshot: Snapshot, entries: dict[str, tuple[bytes, str]]
) -> None:
    """A version-2 index of the base tree; stat data is filled only for files that still match
    the base, so git re-checks everything else by content."""
    records = []
    for path in sorted(entries, key=lambda item: item.encode()):
        mode, digest = entries[path]
        encoded = path.encode()
        info = _matching_stat(work_root, path, snapshot.sizes.get(path))
        fields = (
            (
                int(info.st_ctime) & 0xFFFFFFFF,
                info.st_ctime_ns % 1_000_000_000,
                int(info.st_mtime) & 0xFFFFFFFF,
                info.st_mtime_ns % 1_000_000_000,
                info.st_dev & 0xFFFFFFFF,
                info.st_ino & 0xFFFFFFFF,
                int(mode, 8),
                info.st_uid & 0xFFFFFFFF,
                info.st_gid & 0xFFFFFFFF,
                info.st_size & 0xFFFFFFFF,
            )
            if info is not None
            else (0, 0, 0, 0, 0, 0, int(mode, 8), 0, 0, 0)
        )
        entry = struct.pack(">10I", *fields) + bytes.fromhex(digest)
        entry += struct.pack(">H", min(len(encoded), 0xFFF)) + encoded
        entry += b"\0" * (8 - (len(entry) % 8))
        records.append(entry)
    body = b"DIRC" + struct.pack(">II", 2, len(records)) + b"".join(records)
    body += hashlib.sha1(body, usedforsecurity=False).digest()
    _write_bytes(work_root, (".git", "index"), body)


def _matching_stat(work_root: Path, path: str, size: int | None) -> os.stat_result | None:
    try:
        info = work_root.joinpath(*path.split("/")).lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size != size:
        return None
    return info


def _baseline_content(work_root: Path, path: str, digest: str) -> bytes | None:
    """The bytes of a baseline file, from staging when it still matches."""
    try:
        directory = _open_directory(work_root, tuple(path.split("/")[:-1]), create=False)
    except OSError:
        return None
    try:
        descriptor = os.open(
            path.rsplit("/", 1)[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except OSError:
        return None
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as handle:
        content = handle.read()
    return content if hashlib.sha256(content).hexdigest() == digest else None


# -- link-safe file access -----------------------------------------------------------------------


def _open_directory(work_root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    """Open ``work_root/parts`` one component at a time, refusing links at every step."""
    descriptor = os.open(work_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise OSError(f"unsafe git path component: {part!r}")
            if create:
                try:
                    os.mkdir(part, 0o770, dir_fd=descriptor)
                except FileExistsError:
                    pass
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = following
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_bytes(work_root: Path, parts: tuple[str, ...], content: bytes) -> None:
    directory = _open_directory(work_root, parts[:-1], create=True)
    try:
        _replace_in(directory, parts[-1], content, 0o660)
    finally:
        os.close(directory)


def _replace_in(directory: int, name: str, content: bytes, mode: int) -> None:
    temporary = f".euesto-{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, mode, dir_fd=directory)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except OSError:
            pass
        raise


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        path.unlink()


# -- persisted state (on the state volume, never in staging) ------------------------------------


def _save_state(state_root: Path, state: dict[str, object]) -> None:
    from .atomic_io import atomic_write_bytes

    state_root.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        state_root / STATE_FILE,
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode(),
        prefix=".state-",
    )


def _load_state(state_root: Path) -> dict[str, object] | None:
    try:
        value = json.loads((state_root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("commit") else None


def _status(
    available: bool,
    depth: int,
    reason: str | None,
    state: dict[str, object] | None = None,
) -> dict[str, object]:
    status: dict[str, object] = {
        "available": available,
        "depth": depth or "full",
        "base_ref": "euesto/base" if available else None,
    }
    if state is not None:
        status["head"] = state.get("head")
    if reason:
        status["reason"] = reason
    return status
