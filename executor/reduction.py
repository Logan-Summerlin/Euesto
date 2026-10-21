"""Structured reduction of test-runner and compiler output (plan item 1.6).

Pure, bounded parsers run on a command's full captured output before it is excerpted. Each
recognized tool contributes one section: a totals line, then its first failures with file,
line, and message. A failing test run shrinks from tens of kilobytes to a few hundred bytes;
the raw log stays available as an artifact. Output that no parser recognizes reduces to None.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 16_000_000
MAX_FAILURES = 10
MAX_LINE_CHARS = 240
MAX_REDUCTION_CHARS = 3_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
C_EXTENSIONS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm")
PYTHON_EXTENSIONS = (".py", ".pyi")


@dataclass(slots=True)
class Section:
    tool: str
    totals: str
    failures: list[str] = field(default_factory=list)

    def render(self, budget: int) -> str:
        """Totals, then whole failure lines while they fit ``budget`` characters."""
        lines = [_clip(f"{self.tool}: {self.totals}")]
        used = len(lines[0])
        shown = 0
        for item in self.failures[:MAX_FAILURES]:
            line = _clip(item)
            if used + 1 + len(line) + len(" … 9999 more") > budget:
                break
            lines.append(line)
            used += 1 + len(line)
            shown += 1
        if len(self.failures) > shown:
            lines.append(f"… {len(self.failures) - shown} more")
        return "\n".join(lines)


Parser = Callable[[list[str]], Section | None]


def reduce_output(command: str, output: str) -> str | None:
    """Return the reduction of ``output``, or None when no parser recognizes it."""
    del command  # detection is by output shape, so wrappers (make, npm test, tox) still work
    lines = _ANSI.sub("", output[:MAX_INPUT_CHARS]).splitlines()
    sections = [section for parser in PARSERS if (section := parser(lines)) is not None]
    if not sections:
        return None
    rendered: list[str] = []
    remaining = MAX_REDUCTION_CHARS
    for index, section in enumerate(sections):
        # Leave room for the totals line of every later section.
        reserve = sum(len(item.tool) + len(item.totals) + 4 for item in sections[index + 1 :])
        text = section.render(max(0, remaining - reserve))
        rendered.append(text)
        remaining -= len(text) + 1
    return "\n".join(rendered)[:MAX_REDUCTION_CHARS]


def _clip(line: str) -> str:
    line = line.strip()
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# pytest ---------------------------------------------------------------------------------------

_PYTEST_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected|"
    r"rerun)(?:, )?)+)(?: in [\d.]+s)?(?: \(\S+\))?\s*=*$"
)
_PYTEST_FAILURE = re.compile(r"^(FAILED|ERROR) (\S+::\S+|\S+\.py)(?: - (.*))?$")
_PYTEST_LOCATION = re.compile(r"^(\S+\.py):(\d+): (\w*(?:Error|Exception|Failed)\w*)$")


def parse_pytest(lines: list[str]) -> Section | None:
    totals = None
    failures: list[str] = []
    locations: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if match := _PYTEST_SUMMARY.match(stripped):
            if any(word in match.group(1) for word in ("passed", "failed", "error")):
                totals = match.group(1).rstrip(", ")
        elif match := _PYTEST_FAILURE.match(stripped):
            kind, test, message = match.groups()
            location = locations.get(test.split("::", 1)[0])
            entry = f"{kind} {test}"
            if location:
                entry += f" ({location})"
            if message:
                entry += f" - {message}"
            failures.append(entry)
        elif match := _PYTEST_LOCATION.match(stripped):
            locations[match.group(1)] = f"{match.group(1)}:{match.group(2)}"
    if totals is None and not failures:
        return None
    return Section("pytest", totals or "summary missing (output truncated?)", _unique(failures))


# unittest -------------------------------------------------------------------------------------

_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s$")
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)(?: \((.*)\))?$")
_UNITTEST_HEADER = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)")
_UNITTEST_FILE = re.compile(r'^\s*File "([^"]+)", line (\d+)')


def parse_unittest(lines: list[str]) -> Section | None:
    ran = None
    result = None
    failures: list[str] = []
    block: list[str] | None = None

    def close() -> None:
        assert block is not None
        header, body = block[0], block[1:]
        location = None
        for line in body:
            if match := _UNITTEST_FILE.match(line):
                location = f"{match.group(1)}:{match.group(2)}"
        messages = [line.strip() for line in body if line.strip() and not line[:1].isspace()]
        messages = [line for line in messages if not line.startswith("Traceback")]
        entry = header + (f" ({location})" if location else "")
        failures.append(entry + (f" - {messages[-1]}" if messages else ""))

    for line in lines:
        stripped = line.strip()
        if match := _UNITTEST_HEADER.match(stripped):
            if block is not None:
                close()
            block = [f"{match.group(1)} {match.group(2)} ({match.group(3)})"]
            continue
        if block is not None:
            separator = stripped.startswith(("-----", "=====")) and set(stripped) <= {"-", "="}
            if separator and len(block) > 1:
                close()
                block = None
            elif not separator:
                block.append(line)
        if match := _UNITTEST_RAN.match(stripped):
            ran = int(match.group(1))
        elif ran is not None and (match := _UNITTEST_RESULT.match(stripped)):
            result = match.group(0)
    if block is not None:
        close()
    if ran is None:
        return None
    return Section(
        "unittest", f"ran {ran} tests, {result or 'result missing'}", _unique(failures)
    )


# Jest and Vitest ------------------------------------------------------------------------------

_JEST_TESTS = re.compile(r"^Tests:\s+(.+total)$")
_JEST_SUITES = re.compile(r"^Test Suites:\s+(.+total)$")
_JEST_FAILURE = re.compile(r"^●\s+(.+)$")
_JS_LOCATION = re.compile(r"\(?((?:\.{0,2}/)?[\w./@-]+\.(?:[cm]?[jt]sx?)):(\d+):(\d+)\)?")
_VITEST_TESTS = re.compile(r"^Tests\s+(.+)$")
_VITEST_FILES = re.compile(r"^Test Files\s+(.+)$")
_VITEST_FAILURE = re.compile(r"^(?:FAIL|×|✗|❯)\s+(\S+\.(?:[cm]?[jt]sx?)\s+>\s+.+)$")


def parse_jest(lines: list[str]) -> Section | None:
    totals = suites = None
    failures: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if match := _JEST_TESTS.match(stripped):
            totals = re.sub(r"\s+", " ", match.group(1))
        elif match := _JEST_SUITES.match(stripped):
            suites = re.sub(r"\s+", " ", match.group(1))
        elif (match := _JEST_FAILURE.match(stripped)) and "Console" not in stripped:
            failures.append(match.group(1) + _js_detail(lines, index))
    if totals is None:
        return None
    summary = f"tests {totals}" + (f"; suites {suites}" if suites else "")
    return Section("jest", summary, _unique(failures))


def parse_vitest(lines: list[str]) -> Section | None:
    totals = files = None
    failures: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if match := _VITEST_TESTS.match(stripped):
            if re.search(r"\d+ (?:passed|failed)", match.group(1)):
                totals = re.sub(r"\s+", " ", match.group(1))
        elif match := _VITEST_FILES.match(stripped):
            files = re.sub(r"\s+", " ", match.group(1))
        elif match := _VITEST_FAILURE.match(stripped):
            failures.append(match.group(1) + _js_detail(lines, index))
    if totals is None:
        return None
    summary = f"tests {totals}" + (f"; files {files}" if files else "")
    return Section("vitest", summary, _unique(failures))


def _js_detail(lines: list[str], index: int) -> str:
    """The first message line and source location after a failure header."""
    message = location = None
    for line in lines[index + 1 : index + 40]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("●", "FAIL", "×", "✗", "Tests:", "Test Files")):
            break
        if message is None and not stripped.startswith(("at ", ">", "|")):
            message = stripped
        if location is None and (match := _JS_LOCATION.search(stripped)):
            if "node_modules" not in match.group(1):
                location = f"{match.group(1)}:{match.group(2)}"
        if message and location:
            break
    detail = f" ({location})" if location else ""
    return detail + (f" - {message}" if message else "")


# go test --------------------------------------------------------------------------------------

_GO_FAIL = re.compile(r"^--- FAIL: (\S+) \(")
_GO_PACKAGE = re.compile(r"^(ok|FAIL|---)\s+(\S+)\s+(?:[\d.]+s|\[)")
_GO_MESSAGE = re.compile(r"^\s+(\S+\.go:\d+): (.*)$")
_GO_PASS = re.compile(r"^--- PASS: ")


def parse_go_test(lines: list[str]) -> Section | None:
    failed_tests: list[str] = []
    packages = {"ok": 0, "FAIL": 0}
    passed = 0
    # `go test -v` logs a test's messages before its verdict; plain `go test` after it.
    recent: str | None = None
    awaiting: int | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("=== RUN"):
            recent = None
        elif match := _GO_FAIL.match(stripped):
            failed_tests.append(f"--- FAIL {match.group(1)}" + (recent or ""))
            awaiting = None if recent else len(failed_tests) - 1
            recent = None
        elif _GO_PASS.match(stripped):
            passed += 1
            recent = None
        elif match := _GO_MESSAGE.match(line):
            detail = f" ({match.group(1)}) - {match.group(2)}"
            if awaiting is not None:
                failed_tests[awaiting] += detail
                awaiting = None
            elif recent is None:
                recent = detail
        elif match := _GO_PACKAGE.match(line):
            if match.group(1) in packages:
                packages[match.group(1)] += 1
    if not failed_tests and not any(packages.values()):
        return None
    totals = f"{packages['ok']} packages ok, {packages['FAIL']} failed"
    totals += f"; {len(failed_tests)} failing tests"
    if passed:
        totals += f", {passed} passed"
    return Section("go test", totals, _unique(failed_tests))


# cargo test -----------------------------------------------------------------------------------

_CARGO_RESULT = re.compile(
    r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)
_CARGO_FAILED = re.compile(r"^test (\S+) \.\.\. FAILED$")
_CARGO_PANIC = re.compile(r"^thread '([^']+)' panicked at (\S+:\d+:\d+):?(.*)$")


def parse_cargo_test(lines: list[str]) -> Section | None:
    passed = failed = ignored = 0
    seen = False
    failed_tests: list[str] = []
    panics: dict[str, str] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if match := _CARGO_RESULT.match(stripped):
            seen = True
            passed += int(match.group(2))
            failed += int(match.group(3))
            ignored += int(match.group(4))
        elif match := _CARGO_FAILED.match(stripped):
            failed_tests.append(match.group(1))
        elif match := _CARGO_PANIC.match(stripped):
            message = match.group(3).strip()
            if not message and index + 1 < len(lines):
                message = lines[index + 1].strip()
            panics[match.group(1)] = f"({match.group(2)}) - {message}"
    if not seen and not failed_tests:
        return None
    failures = [f"FAILED {name} {panics.get(name, '')}".rstrip() for name in failed_tests]
    totals = (
        f"{passed} passed, {failed} failed, {ignored} ignored" if seen else "result missing"
    )
    return Section("cargo test", totals, _unique(failures))


# tsc ------------------------------------------------------------------------------------------

_TSC = re.compile(r"^(.+?)(?:\((\d+),(\d+)\)|:(\d+):(\d+)) ?[:-] error (TS\d+): (.*)$")
_TSC_FOUND = re.compile(r"^Found (\d+) errors?(?: in (\d+) files?)?")


def parse_tsc(lines: list[str]) -> Section | None:
    failures: list[str] = []
    found = None
    for line in lines:
        stripped = line.strip()
        if match := _TSC.match(stripped):
            path, line_a, _col_a, line_b, _col_b, code, message = match.groups()
            failures.append(f"{path}:{line_a or line_b} {code} {message}")
        elif match := _TSC_FOUND.match(stripped):
            found = match.group(0).rstrip(".")
    if not failures:  # "Found N errors" alone is also how ruff and mypy summarize
        return None
    return Section("tsc", found or f"{len(failures)} errors", _unique(failures))


# ESLint ---------------------------------------------------------------------------------------

_ESLINT_PROBLEM = re.compile(r"^(\d+):(\d+)\s+(error|warning)\s+(.+?)(?:\s{2,}(\S+))?$")
_ESLINT_TOTAL = re.compile(r"^[✖x] (\d+ problems? \(.*\))")
_ESLINT_FILE = re.compile(r"^(/|\.{0,2}/|[A-Za-z]:\\|[\w@-])[\w./\\@-]*\.(?:[cm]?[jt]sx?|vue)$")


def parse_eslint(lines: list[str]) -> Section | None:
    failures: list[str] = []
    current = None
    total = None
    for line in lines:
        stripped = line.strip()
        if _ESLINT_FILE.match(stripped):
            current = stripped
        elif current and line[:1].isspace() and (match := _ESLINT_PROBLEM.match(stripped)):
            row, _column, level, message, rule = match.groups()
            if level == "error":
                failures.append(f"{current}:{row} {message}" + (f" ({rule})" if rule else ""))
        elif match := _ESLINT_TOTAL.match(stripped):
            total = match.group(1)
    if total is None:
        return None
    return Section("eslint", total, _unique(failures))


# Ruff -----------------------------------------------------------------------------------------

_RUFF_CONCISE = re.compile(r"^(.+?\.pyi?):(\d+):(\d+): ([A-Z]{1,4}\d{2,4}) (.*)$")
_RUFF_FULL_CODE = re.compile(r"^([A-Z]{1,4}\d{2,4}) (.+)$")
_RUFF_ARROW = re.compile(r"^-->\s+(.+?\.pyi?):(\d+):(\d+)$")
_RUFF_FOUND = re.compile(r"^Found (\d+) errors?\.?")
_RUFF_FORMAT = re.compile(r"^(?:Would reformat|reformatted): (.+)$")
_RUFF_FORMAT_TOTAL = re.compile(r"^(\d+) files? (?:would be reformatted|reformatted)")


def parse_ruff(lines: list[str]) -> Section | None:
    failures: list[str] = []
    found = None
    pending = None
    for line in lines:
        stripped = line.strip()
        if match := _RUFF_CONCISE.match(stripped):
            path, row, _column, code, message = match.groups()
            failures.append(f"{path}:{row} {code} {message}")
        elif match := _RUFF_FULL_CODE.match(stripped):
            pending = f"{match.group(1)} {match.group(2)}"
        elif (match := _RUFF_ARROW.match(stripped)) and pending:
            failures.append(f"{match.group(1)}:{match.group(2)} {pending}")
            pending = None
        elif match := _RUFF_FORMAT.match(stripped):
            failures.append(f"would reformat {match.group(1)}")
        elif match := _RUFF_FOUND.match(stripped):
            found = match.group(0).rstrip(".")
        elif match := _RUFF_FORMAT_TOTAL.match(stripped):
            found = match.group(0)
        elif stripped == "All checks passed!":
            found = "all checks passed"
    # A bare "Found N errors" is also tsc's and mypy's summary; require ruff's own lines.
    if found is None or (not failures and found != "all checks passed"):
        return None
    return Section("ruff", found, _unique(failures))


# mypy -----------------------------------------------------------------------------------------

_MYPY = re.compile(r"^(.+?\.pyi?):(\d+)(?::\d+)?: error: (.*?)(?:\s+\[([\w-]+)\])?$")
_MYPY_TOTAL = re.compile(r"^(Found \d+ errors? in \d+ files?.*|Success: no issues found.*)$")


def parse_mypy(lines: list[str]) -> Section | None:
    failures: list[str] = []
    total = None
    for line in lines:
        stripped = line.strip()
        if match := _MYPY.match(stripped):
            path, row, message, code = match.groups()
            failures.append(f"{path}:{row} {message}" + (f" [{code}]" if code else ""))
        elif match := _MYPY_TOTAL.match(stripped):
            total = match.group(1)
    # "Found N errors in M files" is also tsc's summary; require mypy's own lines.
    if total is None or not (failures or total.startswith("Success")):
        return None
    return Section("mypy", total, _unique(failures))


# gcc and clang --------------------------------------------------------------------------------

_CC = re.compile(r"^(.+?):(\d+):(\d+): (fatal error|error|warning): (.*)$")
_CLANG_TOTAL = re.compile(
    r"^(\d+) (?:errors?|warnings?)(?: and \d+ (?:errors?|warnings?))? generated\.$"
)


def parse_c_compiler(lines: list[str]) -> Section | None:
    errors: list[str] = []
    warnings = 0
    clang = False
    for line in lines:
        stripped = line.strip()
        if (match := _CC.match(stripped)) and match.group(1).endswith(C_EXTENSIONS):
            path, row, _column, level, message = match.groups()
            if level == "warning":
                warnings += 1
            else:
                errors.append(f"{path}:{row} {level}: {message}")
        elif _CLANG_TOTAL.match(stripped):
            clang = True
    if not errors and not warnings:
        return None
    tool = "clang" if clang else "gcc/clang"
    return Section(tool, f"{len(errors)} errors, {warnings} warnings", _unique(errors))


PARSERS: tuple[Parser, ...] = (
    parse_pytest,
    parse_unittest,
    parse_jest,
    parse_vitest,
    parse_go_test,
    parse_cargo_test,
    parse_tsc,
    parse_eslint,
    parse_ruff,
    parse_mypy,
    parse_c_compiler,
)
