"""Error codes are chosen at the throw site and never inferred from message wording."""
from __future__ import annotations

import ast
import asyncio
import errno
import re
from pathlib import Path

import pytest

from executor import errors
from executor.app import ExecutorService
from executor.checkpoints import CheckpointError
from executor.config import ExecutorConfig
from executor.errors import ERROR_CODES, ExecutorToolError, classify_error
from executor.paths import UnsafePath
from executor.permissions import enforce_capability
from shared.tools import ToolRequest

ROOT = Path(__file__).resolve().parents[3]

# (tool, mode, arguments, expected code). Each row is one executor error path.
SCENARIOS = [
    ("read", "agent", {"path": "missing.txt"}, "path.missing"),
    ("read", "plan", {"path": "dir"}, "path.invalid_type"),
    ("read", "agent", {"path": "a.txt", "bogus": 1}, "request.invalid_arguments"),
    ("read", "agent", {"path": "../outside.txt"}, "path.unsafe"),
    ("read", "agent", {"path": ".env"}, "path.unsafe"),
    ("read", "agent", {"path": "bin.dat"}, "file.invalid_utf8"),
    ("read", "agent", {"path": "a.txt", "start_line": "one"}, "request.invalid_arguments"),
    ("read", "agent", {"path": "a.txt", "max_bytes": 0}, "request.invalid_arguments"),
    ("write", "agent", {"path": "dir", "content": "x"}, "path.invalid_type"),
    ("write", "agent", {"path": "new/deep.txt", "content": "x"}, "path.missing"),
    ("write", "agent", {"path": "a.txt", "content": "x", "expected_sha256": "0" * 64}, "staging.conflict"),
    ("write", "agent", {"path": "big.txt", "content": "x" * 200}, "limit.exceeded"),
    ("edit", "agent", {"path": "a.txt", "old_str": "absent", "new_str": "x"}, "edit.no_match"),
    ("edit", "agent", {"path": "a.txt", "old_str": "l", "new_str": "L"}, "edit.too_many_matches"),
    ("edit", "agent", {"path": "a.txt", "old_str": "", "new_str": "x"}, "edit.malformed_context"),
    ("edit", "agent", {"path": "dir", "old_str": "x", "new_str": "y"}, "path.invalid_type"),
    ("patch", "agent", {"operations": []}, "patch.malformed"),
    ("patch", "agent", {"operations": [{"operation": "delete", "path": "missing.txt"}]}, "path.missing"),
    ("bash", "agent", {"command": ""}, "command.invalid_arguments"),
    ("bash", "agent", {"command": "true", "working_directory": 5}, "working_directory.invalid"),
    ("bash", "agent", {"command": "true", "working_directory": "a.txt"}, "working_directory.invalid"),
    ("bash", "agent", {"command": "true", "env": {"PATH": "/tmp"}}, "command.invalid_arguments"),
    ("bash", "agent", {"command": "true", "env": {"OK": "\x00"}}, "command.invalid_arguments"),
    ("bash", "agent", {"command": "true", "timeout_seconds": "slow"}, "command.invalid_arguments"),
    ("bash", "agent", {"command": "cat", "stdin": "x" * 200}, "limit.exceeded"),
    ("grep", "agent", {"query": ""}, "request.invalid_arguments"),
    ("find", "agent", {"path": "a.txt"}, "path.invalid_type"),
    ("ls", "agent", {"path": "a.txt"}, "path.invalid_type"),
    ("ls", "agent", {"cursor": "!!!"}, "request.invalid_arguments"),
    ("status", "agent", {"max_results": 0}, "request.invalid_arguments"),
    ("status", "agent", {"paths": ["node_modules"]}, "path.missing"),
    ("status", "agent", {"paths": ["../outside"]}, "path.invalid"),
]


def _service(tmp_path: Path) -> ExecutorService:
    source = tmp_path / "source"
    (source / "dir").mkdir(parents=True)
    (source / "a.txt").write_text("hello world\n", encoding="utf-8")
    (source / "bin.dat").write_bytes(b"\x00\xff\x00binary")
    (source / "dir" / "b.txt").write_text("b\n", encoding="utf-8")
    config = ExecutorConfig(
        source_root=source, work_root=tmp_path / "work", socket_path=tmp_path / "executor.sock",
        token="t" * 43, workspace_id="workspace", max_write_bytes=100, max_bash_stdin_bytes=100,
    )
    return ExecutorService(config)


def _codes(tmp_path: Path) -> list[tuple[str, str]]:
    service = _service(tmp_path)
    observed = []
    for index, (tool, mode, arguments, _expected) in enumerate(SCENARIOS):
        result = asyncio.run(service.execute(ToolRequest(f"r{index}", "run", tool, mode, arguments)))
        assert not result.ok, (tool, arguments)
        observed.append((result.error_code, result.output))
    return observed


def test_every_error_path_reports_its_documented_code(tmp_path: Path) -> None:
    codes = [code for code, _output in _codes(tmp_path)]
    assert codes == [expected for *_rest, expected in SCENARIOS]
    assert set(codes) <= ERROR_CODES


def test_codes_are_independent_of_message_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reword every message the executor raises (and the sanitized text of foreign
    exceptions); every error path must still report exactly the same code."""
    original = ExecutorToolError.__init__

    def reworded(self, code, message, *args, **kwargs):
        original(self, code, "reworded: " + message[::-1], *args, **kwargs)

    baseline = _codes(tmp_path / "baseline")
    monkeypatch.setattr(ExecutorToolError, "__init__", reworded)
    monkeypatch.setattr(errors, "safe_message", lambda value: "reworded")
    changed = _codes(tmp_path / "reworded")
    assert [code for code, _ in changed] == [code for code, _ in baseline]
    # The wording really did change on the typed paths.
    assert all(output.startswith("reworded") for _code, output in changed)


def test_classify_error_never_reads_message_text() -> None:
    # Words the old substring classifier keyed on no longer affect foreign exceptions.
    for text in ("file not found", "exceeds the limit", "not valid utf-8", "working directory", "stdin command", "hash conflict", "shrink", "is a directory"):
        assert classify_error(ValueError(text)).code == "request.invalid_arguments"
        assert classify_error(RuntimeError(text)).code == "tool.internal"
    assert classify_error(ExecutorToolError("path.missing", "totally unrelated words")).code == "path.missing"
    assert classify_error(FileNotFoundError(errno.ENOENT, "whatever")).code == "path.missing"
    assert classify_error(IsADirectoryError(errno.EISDIR, "whatever")).code == "path.invalid_type"
    assert classify_error(OSError(errno.ENOSPC, "whatever")).code == "limit.exceeded"
    assert classify_error(OSError(errno.EIO, "file not found")).code == "io.internal"
    assert classify_error(PermissionError("x")).code == "permission.denied"
    assert classify_error(TimeoutError("x")).code == "tool.timeout"
    assert classify_error(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")).code == "file.invalid_utf8"
    assert classify_error(KeyError("x")).code == "tool.internal"


def test_typed_exception_families_carry_codes() -> None:
    assert UnsafePath("anything at all").code == "path.unsafe"
    assert isinstance(UnsafePath("x"), ValueError)
    assert CheckpointError("checkpoint.corrupt", "x").code == "checkpoint.corrupt"
    plan_write = ToolRequest.__new__(ToolRequest)
    for name, value in {"request_id": "r", "run_id": "run", "tool": "write", "mode": "plan", "arguments": {}}.items():
        object.__setattr__(plan_write, name, value)
    with pytest.raises(ExecutorToolError) as raised:
        enforce_capability(plan_write)
    assert raised.value.code == "permission.denied"


# Raise sites that deliberately stay untyped: staging seeding and publication bookkeeping
# report through their own HTTP endpoints, and configuration validation runs at startup.
UNTYPED_MODULES = {"executor/config.py", "executor/egress.py"}
UNTYPED_STAGING_FUNCTIONS = {"seed_staging", "advance_published_staging"}
TYPED_EXCEPTIONS = {"ExecutorToolError", "UnsafePath", "CheckpointError"}
# Exceptions whose type alone determines the code in classify_error.
TYPE_CLASSIFIED = {"TimeoutError", "PermissionError", "FileNotFoundError"}


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owners: dict[ast.AST, str] = {}
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                owners.setdefault(node, function.name)
    return owners


def test_every_tool_path_raise_names_a_documented_code() -> None:
    constants = {name for name in dir(errors) if name.isupper() and getattr(errors, name) in ERROR_CODES}
    problems = []
    for path in sorted((ROOT / "executor").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call) or not isinstance(node.exc.func, ast.Name):
                continue
            name = node.exc.func.id
            if name in TYPED_EXCEPTIONS:
                first = node.exc.args[0] if node.exc.args else None
                if name in {"ExecutorToolError", "CheckpointError"}:
                    # The code is a named constant (or forwarded from an already-typed cause).
                    named = isinstance(first, ast.Name) and first.id in constants
                    forwarded = isinstance(first, ast.Name) and first.id == "code" or isinstance(first, ast.Attribute) and first.attr == "code"
                    if not (named or forwarded):
                        problems.append(f"{relative}:{node.lineno} {name} without a named code")
                continue
            if name.endswith("Error") and name not in TYPE_CLASSIFIED and relative not in UNTYPED_MODULES:
                if relative == "executor/staging.py" and owners.get(node) in UNTYPED_STAGING_FUNCTIONS:
                    continue
                problems.append(f"{relative}:{node.lineno} raises untyped {name}")
    assert problems == []


def test_error_codes_are_documented() -> None:
    docs = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    section = re.search(r"^## Error codes\n(.*?)(?=^## |\Z)", docs, re.S | re.M)
    assert section, "docs/TOOLS.md must have an 'Error codes' section"
    documented = set(re.findall(r"^\| `([a-z0-9_]+\.[a-z0-9_]+)` \|", section.group(1), re.M))
    assert documented == ERROR_CODES
