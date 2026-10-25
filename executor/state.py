"""Persistent executor state: workspace identity, publication baseline, and conflicts (2.4).

The state volume belongs to one workspace identity. It is mounted only into the executor (never
the runner or the gateway), so commands cannot forge a baseline or a checkpoint. Every write is
atomic, so a crash leaves either the previous or the next state, never a mixture.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import atomic_write_bytes
from .staging import Snapshot

STATE_SCHEMA = 1
IDENTITY_FILE = "identity.json"
BASELINE_FILE = "baseline.json"
CHECKPOINT_DIRECTORY = "checkpoints"
_HASH = re.compile(r"^[0-9a-f]{64}$")


class WorkspaceIdentityMismatch(RuntimeError):
    """The state volume belongs to a different workspace and must not be reused."""


@dataclass(frozen=True, slots=True)
class Conflict:
    """A file changed both on the host and in staging since the baseline."""

    path: str
    base_sha256: str | None
    host_sha256: str | None
    staged_sha256: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "base_sha256": self.base_sha256,
            "host_sha256": self.host_sha256,
            "staged_sha256": self.staged_sha256,
        }


@dataclass(slots=True)
class Baseline:
    snapshot: Snapshot
    conflicts: dict[str, Conflict] = field(default_factory=dict)


class StateStore:
    def __init__(self, root: Path, workspace_id: str) -> None:
        self.root = root
        self.workspace_id = workspace_id

    @property
    def checkpoints(self) -> Path:
        return self.root / CHECKPOINT_DIRECTORY

    def claim(self) -> bool:
        """Claim the volume for this workspace. True when it already held this workspace's
        state; raises when it belongs to another workspace."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / IDENTITY_FILE
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._write(
                IDENTITY_FILE, {"schema": STATE_SCHEMA, "workspace_id": self.workspace_id}
            )
            return False
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceIdentityMismatch("The state volume identity is unreadable.") from exc
        if not isinstance(identity, dict) or identity.get("workspace_id") != self.workspace_id:
            raise WorkspaceIdentityMismatch(
                "The staging volumes belong to another workspace; refusing to reuse them."
            )
        return True

    def load(self) -> Baseline | None:
        try:
            data = json.loads((self.root / BASELINE_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            return None
        try:
            files = data["files"]
            hashes = {str(path): str(item[0]) for path, item in files.items()}
            sizes = {str(path): int(item[1]) for path, item in files.items()}
            modes = {str(path): int(item[2]) for path, item in files.items()}
            signatures = {
                str(path): (int(value[0]), int(value[1]))
                for path, value in (data.get("source_signatures") or {}).items()
            }
            conflicts = {
                str(item["path"]): Conflict(
                    str(item["path"]),
                    _digest(item.get("base_sha256")),
                    _digest(item.get("host_sha256")),
                    _digest(item.get("staged_sha256")),
                )
                for item in data.get("conflicts") or []
            }
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            return None
        if not all(_HASH.fullmatch(value) for value in hashes.values()):
            return None
        snapshot = Snapshot(
            str(data.get("snapshot_id") or ""),
            hashes,
            sum(sizes.values()),
            sizes,
            modes,
            signatures,
        )
        return Baseline(snapshot, conflicts) if snapshot.snapshot_id else None

    def save(self, baseline: Baseline) -> None:
        snapshot = baseline.snapshot
        self._write(
            BASELINE_FILE,
            {
                "schema": STATE_SCHEMA,
                "workspace_id": self.workspace_id,
                "snapshot_id": snapshot.snapshot_id,
                "files": {
                    path: [digest, snapshot.sizes.get(path, 0), snapshot.modes.get(path, 0o644)]
                    for path, digest in sorted(snapshot.hashes.items())
                },
                "source_signatures": {
                    path: list(value)
                    for path, value in sorted(snapshot.source_signatures.items())
                },
                "conflicts": [
                    item.to_dict()
                    for _path, item in sorted(
                        baseline.conflicts.items(), key=lambda pair: pair[0]
                    )
                ],
            },
        )

    def forget_baseline(self) -> None:
        (self.root / BASELINE_FILE).unlink(missing_ok=True)

    def _write(self, name: str, value: dict) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        atomic_write_bytes(self.root / name, payload, prefix=".state-")


def _digest(value: object) -> str | None:
    return str(value) if isinstance(value, str) and _HASH.fullmatch(value) else None
