"""The workspace policy file, ``.euesto/policy.toml`` (harness plan 4.6, decision D6).

The desktop reads the policy from the **host** workspace, never from staging, and validates
it here. Validation fails closed: any unknown key, wrong type, or out-of-range value rejects
the whole file, and nothing from a rejected file takes effect. A policy (or a change to it)
takes effect only after the user confirms it in Settings; the agent can propose a change only
as an ordinary staged edit, which must be published first.

Example::

    version = 1

    [runner]
    image = "python"                 # auto, base, python, node, or polyglot

    [network]
    mode = "none"                    # none or registries (a per-session opt-in)
    allow_hosts = ["crates.io"]      # public registries from REGISTRY_ADDITIONS only

    [setup]
    command = "uv sync --frozen"     # run in the runner with registry access, after approval
    isolated = true                  # the setup runner sees only manifests and lockfiles

    [checks]
    test = "python -m pytest -q"
    lint = { command = "ruff check .", timeout_seconds = 120 }

    [secrets]
    patterns = ["*.vault"]           # added to the built-in secret names, never removing any

    [publication]
    never_publish = ["deploy/**"]
    verify_before_publish = ["test"]
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

POLICY_PATH = ".euesto/policy.toml"
POLICY_VERSION = 1
MAX_POLICY_BYTES = 64_000
RUNNER_IMAGES = ("auto", "base", "python", "node", "polyglot")
NETWORK_MODES = ("none", "registries")
# The registries the egress proxy always allows in the `registries` mode and during setup.
DEFAULT_REGISTRY_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
# The only hosts a workspace may add (5.2): public package registries. Broad hosts such as
# github.com are refused because they serve arbitrary user content (a tunnel for exfiltration
# and for fetching unreviewed code), which defeats a registry-only allowlist.
REGISTRY_ADDITIONS = (
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "proxy.golang.org",
    "sum.golang.org",
    "repo.maven.apache.org",
    "repo1.maven.org",
)
CHECK_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_CHECKS = 12
MAX_COMMAND_CHARS = 1_000
DEFAULT_CHECK_SECONDS = 300
MAX_CHECK_SECONDS = 900
DEFAULT_SETUP_SECONDS = 900
MAX_SETUP_SECONDS = 1_800
MAX_PATTERNS = 50
MAX_PATTERN_CHARS = 100

# Dependency manifests and lockfiles: what an isolated setup receives, and what triggers a
# new setup when it changes on the host (5.1).
SETUP_MANIFESTS = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "constraints.txt",
    "Pipfile",
    "Pipfile.lock",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    ".yarnrc.yml",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
)
# Directories setup installs into; they persist in the work volume and are never published.
SETUP_OUTPUT_DIRECTORIES = (".venv", "node_modules")


class PolicyError(ValueError):
    """A policy file that cannot take effect; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    command: str
    timeout_seconds: int = DEFAULT_CHECK_SECONDS


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    runner_image: str = "auto"
    network_mode: str = "none"
    allow_hosts: tuple[str, ...] = ()
    setup_command: str | None = None
    setup_isolated: bool = True
    setup_timeout_seconds: int = DEFAULT_SETUP_SECONDS
    checks: tuple[Check, ...] = ()
    secret_patterns: tuple[str, ...] = ()
    never_publish: tuple[str, ...] = ()
    verify_before_publish: tuple[str, ...] = field(default_factory=tuple)

    def to_config(self) -> dict[str, Any]:
        """The effective policy as stored in the gateway's workspace configuration."""
        return {
            "version": POLICY_VERSION,
            "runner_image": self.runner_image,
            "network": {"mode": self.network_mode, "allow_hosts": list(self.allow_hosts)},
            "setup": {
                "command": self.setup_command,
                "isolated": self.setup_isolated,
                "timeout_seconds": self.setup_timeout_seconds,
            },
            "checks": {
                check.name: {"command": check.command, "timeout_seconds": check.timeout_seconds}
                for check in self.checks
            },
            "secret_patterns": list(self.secret_patterns),
            "never_publish": list(self.never_publish),
            "verify_before_publish": list(self.verify_before_publish),
        }

    @classmethod
    def from_config(cls, value: object) -> WorkspacePolicy:
        """Rebuild a confirmed policy saved by ``to_config`` (revalidated, fail closed)."""
        if not isinstance(value, dict):
            return cls()
        network = value.get("network") if isinstance(value.get("network"), dict) else {}
        setup = value.get("setup") if isinstance(value.get("setup"), dict) else {}
        publication = {
            "never_publish": value.get("never_publish") or [],
            "verify_before_publish": value.get("verify_before_publish") or [],
        }
        document: dict[str, Any] = {
            "version": POLICY_VERSION,
            "runner": {"image": value.get("runner_image", "auto")},
            "network": {
                "mode": network.get("mode", "none"),
                "allow_hosts": network.get("allow_hosts") or [],
            },
            "checks": value.get("checks") or {},
            "secrets": {"patterns": value.get("secret_patterns") or []},
            "publication": publication,
        }
        if setup.get("command"):
            document["setup"] = {
                "command": setup["command"],
                "isolated": setup.get("isolated", True),
                "timeout_seconds": setup.get("timeout_seconds", DEFAULT_SETUP_SECONDS),
            }
        return validate_policy(document)

    @property
    def registry_hosts(self) -> tuple[str, ...]:
        return (*DEFAULT_REGISTRY_HOSTS, *self.allow_hosts)


@dataclass(frozen=True, slots=True)
class PolicyFile:
    """What the host workspace holds: no policy, a valid one, or an invalid one."""

    status: str
    digest: str | None = None
    policy: WorkspacePolicy | None = None
    errors: tuple[str, ...] = ()
    text: str = ""


def read_policy(workspace: Path) -> PolicyFile:
    """Read and validate ``.euesto/policy.toml`` from the host workspace."""
    directory = workspace / ".euesto"
    path = directory / "policy.toml"
    try:
        for item in (directory, path):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                return PolicyFile("invalid", errors=("the policy file must not be a link",))
        if not stat.S_ISREG(path.lstat().st_mode):
            return PolicyFile("invalid", errors=("the policy file must be a regular file",))
        raw = path.read_bytes()
    except FileNotFoundError:
        return PolicyFile("absent")
    except OSError as exc:
        return PolicyFile("invalid", errors=(f"the policy file cannot be read: {exc}",))
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_POLICY_BYTES:
        return PolicyFile("invalid", digest, errors=("the policy file is larger than 64 KB",))
    try:
        text = raw.decode("utf-8")
        policy = parse_policy(text)
    except UnicodeDecodeError:
        return PolicyFile("invalid", digest, errors=("the policy file is not UTF-8",))
    except PolicyError as exc:
        return PolicyFile("invalid", digest, errors=tuple(exc.errors), text=text)
    return PolicyFile("valid", digest, policy, text=text)


def parse_policy(text: str) -> WorkspacePolicy:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError([f"not valid TOML: {exc}"]) from exc
    return validate_policy(document)


def validate_policy(document: dict[str, Any]) -> WorkspacePolicy:
    errors: list[str] = []
    sections = {"version", "runner", "network", "setup", "checks", "secrets", "publication"}
    for key in sorted(set(document) - sections):
        errors.append(f"unknown key: {key}")
    if document.get("version") != POLICY_VERSION:
        errors.append(f"version must be {POLICY_VERSION}")

    runner = _table(document, "runner", {"image"}, errors)
    image = runner.get("image", "auto")
    if image not in RUNNER_IMAGES:
        errors.append(f"runner.image must be one of: {', '.join(RUNNER_IMAGES)}")

    network = _table(document, "network", {"mode", "allow_hosts"}, errors)
    mode = network.get("mode", "none")
    if mode not in NETWORK_MODES:
        errors.append("network.mode must be none or registries")
    hosts = _strings(network.get("allow_hosts", []), "network.allow_hosts", errors)
    for host in hosts:
        if host.casefold() not in REGISTRY_ADDITIONS:
            errors.append(
                f"network.allow_hosts: {host} is not an allowed public registry "
                f"(allowed additions: {', '.join(REGISTRY_ADDITIONS)})"
            )

    setup = _table(document, "setup", {"command", "isolated", "timeout_seconds"}, errors)
    command = setup.get("command")
    if "command" in setup and not _command(command):
        errors.append("setup.command must be a non-empty command of at most 1000 characters")
    isolated = setup.get("isolated", True)
    if not isinstance(isolated, bool):
        errors.append("setup.isolated must be true or false")
    setup_timeout = _seconds(
        setup.get("timeout_seconds", DEFAULT_SETUP_SECONDS),
        "setup.timeout_seconds",
        MAX_SETUP_SECONDS,
        errors,
    )

    checks: list[Check] = []
    raw_checks = document.get("checks", {})
    if not isinstance(raw_checks, dict):
        errors.append("checks must be a table")
        raw_checks = {}
    if len(raw_checks) > MAX_CHECKS:
        errors.append(f"at most {MAX_CHECKS} checks are allowed")
    for name, value in raw_checks.items():
        if not CHECK_NAME.match(name):
            errors.append(f"checks.{name}: names are lowercase identifiers")
            continue
        if isinstance(value, str):
            value = {"command": value}
        if not isinstance(value, dict) or set(value) - {"command", "timeout_seconds"}:
            errors.append(f"checks.{name} must be a command or {{command, timeout_seconds}}")
            continue
        if not _command(value.get("command")):
            errors.append(f"checks.{name}.command must be a non-empty command")
            continue
        timeout = _seconds(
            value.get("timeout_seconds", DEFAULT_CHECK_SECONDS),
            f"checks.{name}.timeout_seconds",
            MAX_CHECK_SECONDS,
            errors,
        )
        checks.append(Check(name, value["command"].strip(), timeout))

    secrets = _table(document, "secrets", {"patterns"}, errors)
    patterns: tuple[str, ...] = ()
    try:
        patterns = validate_secret_patterns(secrets.get("patterns", []))
    except ValueError as exc:
        errors.append(f"secrets.patterns: {exc}")

    publication = _table(
        document, "publication", {"never_publish", "verify_before_publish"}, errors
    )
    never: tuple[str, ...] = ()
    try:
        never = validate_publish_globs(publication.get("never_publish", []))
    except ValueError as exc:
        errors.append(f"publication.never_publish: {exc}")
    verify = _strings(
        publication.get("verify_before_publish", []),
        "publication.verify_before_publish",
        errors,
    )
    declared = {check.name for check in checks}
    for name in verify:
        if name not in declared:
            errors.append(f"publication.verify_before_publish: no check named {name}")

    if errors:
        raise PolicyError(errors)
    return WorkspacePolicy(
        runner_image=image,
        network_mode=mode,
        allow_hosts=tuple(dict.fromkeys(host.casefold() for host in hosts)),
        setup_command=command.strip() if isinstance(command, str) else None,
        setup_isolated=isolated,
        setup_timeout_seconds=setup_timeout,
        checks=tuple(checks),
        secret_patterns=patterns,
        never_publish=never,
        verify_before_publish=tuple(dict.fromkeys(verify)),
    )


def validate_secret_patterns(patterns: object) -> tuple[str, ...]:
    """Workspace-added secret name globs: each matches one path segment, never a path."""
    if not isinstance(patterns, list | tuple):
        raise ValueError("secret patterns must be a list of name globs")
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"at most {MAX_PATTERNS} secret patterns are allowed")
    cleaned: list[str] = []
    for pattern in patterns:
        if (
            not isinstance(pattern, str)
            or not pattern.strip()
            or len(pattern) > MAX_PATTERN_CHARS
            or "/" in pattern
            or "\\" in pattern
            or "\x00" in pattern
        ):
            raise ValueError(
                f"invalid secret pattern: {pattern!r} (a file or directory name glob)"
            )
        cleaned.append(pattern.strip().casefold())
    return tuple(dict.fromkeys(cleaned))


def validate_publish_globs(patterns: object) -> tuple[str, ...]:
    """``never_publish`` path globs: relative POSIX globs such as ``config/prod/**``."""
    if not isinstance(patterns, list | tuple):
        raise ValueError("never_publish must be a list of path globs")
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"at most {MAX_PATTERNS} never_publish patterns are allowed")
    cleaned: list[str] = []
    for pattern in patterns:
        if (
            not isinstance(pattern, str)
            or not pattern.strip()
            or len(pattern) > MAX_PATTERN_CHARS
            or pattern.startswith("/")
            or "\\" in pattern
            or "\x00" in pattern
            or ".." in PurePosixPath(pattern).parts
        ):
            raise ValueError(f"invalid never_publish pattern: {pattern!r}")
        cleaned.append(pattern.strip())
    return tuple(dict.fromkeys(cleaned))


def matches_publish_glob(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Whether ``path`` matches a ``never_publish`` glob, case-insensitively. ``dir/**`` and a
    bare directory name also cover everything beneath it."""
    folded = path.casefold()
    for pattern in patterns:
        glob = pattern.casefold().rstrip("/")
        if glob.endswith("/**"):
            prefix = glob[:-3]
            if folded == prefix or folded.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatchcase(folded, glob) or folded.startswith(glob + "/"):
            return True
    return False


def detect_setup_command(workspace: Path) -> str | None:
    """A setup command inferred from the host workspace's lockfiles, for the user to confirm."""
    try:
        names = {entry.name for entry in workspace.iterdir() if entry.is_file()}
    except OSError:
        return None
    commands: list[str] = []
    if "uv.lock" in names:
        commands.append("uv sync --frozen")
    elif "poetry.lock" in names:
        commands.append("POETRY_VIRTUALENVS_IN_PROJECT=true poetry install --no-root")
    elif "requirements.txt" in names:
        commands.append(
            "python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
        )
    if "pnpm-lock.yaml" in names:
        commands.append("pnpm install --frozen-lockfile")
    elif "yarn.lock" in names:
        commands.append("yarn install --frozen-lockfile")
    elif "package-lock.json" in names or "npm-shrinkwrap.json" in names:
        commands.append("npm ci")
    if "Cargo.lock" in names or "Cargo.toml" in names:
        commands.append("cargo fetch")
    if "go.mod" in names:
        commands.append("go mod download")
    return " && ".join(commands) or None


def setup_manifests(workspace: Path) -> list[str]:
    """The dependency manifests and lockfiles present at the workspace root."""
    present = []
    for name in SETUP_MANIFESTS:
        path = workspace / name
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                present.append(name)
        except OSError:
            continue
    return present


def manifests_digest(workspace: Path) -> str:
    """Identity of the dependency manifests; a change means setup should run again."""
    digest = hashlib.sha256()
    for name in setup_manifests(workspace):
        try:
            content = (workspace / name).read_bytes()
        except OSError:
            continue
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def _table(
    document: dict[str, Any], name: str, keys: set[str], errors: list[str]
) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        errors.append(f"{name} must be a table")
        return {}
    for key in sorted(set(value) - keys):
        errors.append(f"unknown key: {name}.{key}")
    return value


def _strings(value: object, name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        errors.append(f"{name} must be a list of strings")
        return []
    if len(value) > MAX_PATTERNS:
        errors.append(f"{name} has more than {MAX_PATTERNS} entries")
        return []
    return [item.strip() for item in value]


def _command(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_COMMAND_CHARS
        and "\x00" not in value
    )


def _seconds(value: object, name: str, maximum: int, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        errors.append(f"{name} must be a whole number of seconds from 1 to {maximum}")
        return min(DEFAULT_CHECK_SECONDS, maximum)
    return value
