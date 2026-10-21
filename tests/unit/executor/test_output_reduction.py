"""Fixture output for each reduction parser, including truncated and interleaved output (1.6)."""

from __future__ import annotations

import pytest

from executor.reduction import MAX_FAILURES, MAX_REDUCTION_CHARS, reduce_output

PYTEST = """\
============================= test session starts ==============================
collected 4 items

tests/test_a.py .F..                                                     [100%]

=================================== FAILURES ===================================
___________________________________ test_x ____________________________________

    def test_x():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_x - assert 1 == 2
========================= 1 failed, 3 passed in 0.12s ==========================
"""

UNITTEST = """\
.F.
======================================================================
FAIL: test_sum (tests.test_math.MathTest.test_sum)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/work/tests/test_math.py", line 9, in test_sum
    self.assertEqual(sum([1, 2]), 4)
AssertionError: 3 != 4

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=1)
"""

JEST = """\
 FAIL  src/sum.test.js
  ● math › adds numbers

    expect(received).toBe(expected) // Object.is equality

    Expected: 4
    Received: 3

      3 | test('adds numbers', () => {
    > 4 |   expect(sum(1, 2)).toBe(4);
        |                     ^
      at Object.<anonymous> (src/sum.test.js:4:21)

Test Suites: 1 failed, 2 passed, 3 total
Tests:       1 failed, 7 passed, 8 total
Snapshots:   0 total
"""

VITEST = """\
 ❯ src/sum.test.ts (2 tests | 1 failed) 4ms
   × math > adds numbers
     → expected 3 to be 4

 FAIL  src/sum.test.ts > math > adds numbers
AssertionError: expected 3 to be 4
 ❯ src/sum.test.ts:5:22

 Test Files  1 failed (1)
      Tests  1 failed | 1 passed (2)
"""

GO = """\
=== RUN   TestAdd
    add_test.go:9: Add(1, 2) = 3; want 4
--- FAIL: TestAdd (0.00s)
=== RUN   TestSub
--- PASS: TestSub (0.00s)
FAIL
FAIL\texample.com/calc\t0.002s
ok  \texample.com/util\t0.001s
"""

CARGO = """\
running 2 tests
test tests::adds ... FAILED
test tests::subs ... ok

failures:

---- tests::adds stdout ----
thread 'tests::adds' panicked at src/lib.rs:10:9:
assertion `left == right` failed
  left: 3
 right: 4

failures:
    tests::adds

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""

TSC = """\
src/app.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'.
src/util.ts:10:5 - error TS2304: Cannot find name 'foo'.

Found 2 errors in 2 files.
"""

ESLINT = """\

/work/src/app.js
  1:10  error    'x' is defined but never used  no-unused-vars
  4:1   warning  Unexpected console statement    no-console

✖ 2 problems (1 error, 1 warning)
"""

RUFF = """\
src/app.py:1:8: F401 [*] `os` imported but unused
src/app.py:5:1: E711 Comparison to `None` should be `cond is None`
Found 2 errors.
[*] 1 fixable with the `--fix` option.
"""

RUFF_FULL = """\
F401 [*] `os` imported but unused
 --> src/app.py:1:8
  |
1 | import os
  |        ^^
  |
Found 1 error.
"""

MYPY = """\
src/app.py:3: error: Incompatible types in assignment (expression has type "str", variable has type "int")  [assignment]
src/app.py:9: note: Revealed type is "builtins.int"
Found 1 error in 1 file (checked 4 source files)
"""

GCC = """\
main.c: In function 'main':
main.c:4:5: error: 'y' undeclared (first use in this function)
main.c:6:9: warning: unused variable 'z' [-Wunused-variable]
"""

CLANG = """\
main.cpp:3:10: fatal error: 'missing.h' file not found
#include "missing.h"
         ^~~~~~~~~~~
1 error generated.
"""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            PYTEST,
            [
                "pytest: 1 failed, 3 passed",
                "FAILED tests/test_a.py::test_x (tests/test_a.py:12) - assert 1 == 2",
            ],
        ),
        (
            UNITTEST,
            [
                "unittest: ran 3 tests, FAILED (failures=1)",
                "FAIL test_sum (tests.test_math.MathTest.test_sum) "
                "(/work/tests/test_math.py:9) - AssertionError: 3 != 4",
            ],
        ),
        (
            JEST,
            [
                "jest: tests 1 failed, 7 passed, 8 total; suites 1 failed, 2 passed, 3 total",
                "math › adds numbers (src/sum.test.js:4) - "
                "expect(received).toBe(expected) // Object.is equality",
            ],
        ),
        (
            VITEST,
            [
                "vitest: tests 1 failed | 1 passed (2); files 1 failed (1)",
                "src/sum.test.ts > math > adds numbers (src/sum.test.ts:5) - "
                "AssertionError: expected 3 to be 4",
            ],
        ),
        (
            GO,
            [
                "go test: 1 packages ok, 1 failed; 1 failing tests, 1 passed",
                "--- FAIL TestAdd (add_test.go:9) - Add(1, 2) = 3; want 4",
            ],
        ),
        (
            CARGO,
            [
                "cargo test: 1 passed, 1 failed, 0 ignored",
                "FAILED tests::adds (src/lib.rs:10:9) - assertion `left == right` failed",
            ],
        ),
        (
            TSC,
            [
                "tsc: Found 2 errors in 2 files",
                "src/app.ts:3 TS2322 Type 'string' is not assignable to type 'number'.",
                "src/util.ts:10 TS2304 Cannot find name 'foo'.",
            ],
        ),
        (
            ESLINT,
            [
                "eslint: 2 problems (1 error, 1 warning)",
                "/work/src/app.js:1 'x' is defined but never used (no-unused-vars)",
            ],
        ),
        (
            RUFF,
            [
                "ruff: Found 2 errors",
                "src/app.py:1 F401 [*] `os` imported but unused",
                "src/app.py:5 E711 Comparison to `None` should be `cond is None`",
            ],
        ),
        (RUFF_FULL, ["ruff: Found 1 error", "src/app.py:1 F401 [*] `os` imported but unused"]),
        (
            MYPY,
            [
                "mypy: Found 1 error in 1 file (checked 4 source files)",
                'src/app.py:3 Incompatible types in assignment (expression has type "str", '
                'variable has type "int") [assignment]',
            ],
        ),
        (
            GCC,
            [
                "gcc/clang: 1 errors, 1 warnings",
                "main.c:4 error: 'y' undeclared (first use in this function)",
            ],
        ),
        (
            CLANG,
            [
                "clang: 1 errors, 0 warnings",
                "main.cpp:3 fatal error: 'missing.h' file not found",
            ],
        ),
    ],
)
def test_each_parser_reduces_its_fixture(output: str, expected: list[str]) -> None:
    assert reduce_output("cmd", output).splitlines() == expected


def test_truncated_pytest_output_still_lists_failures() -> None:
    truncated = "FAILED tests/test_a.py::test_x - assert 1 == 2\n... [output truncated] ..."
    assert reduce_output("pytest", truncated).splitlines() == [
        "pytest: summary missing (output truncated?)",
        "FAILED tests/test_a.py::test_x - assert 1 == 2",
    ]


def test_interleaved_tools_each_contribute_a_section() -> None:
    combined = RUFF.replace("Found 2 errors.\n", "") + PYTEST + "Found 2 errors.\n"
    lines = reduce_output("make check", combined).splitlines()
    assert lines[0] == "pytest: 1 failed, 3 passed"
    assert "ruff: Found 2 errors" in lines


def test_ansi_colors_are_ignored() -> None:
    colored = (
        "\x1b[31mFAILED\x1b[0m tests/t.py::test_a - boom\n\x1b[1m1 failed\x1b[0m in 0.01s\n"
    )
    assert reduce_output("pytest", colored).splitlines()[0] == "pytest: 1 failed"


def test_failures_and_total_size_are_bounded() -> None:
    many = "".join(f"FAILED tests/t.py::test_{index} - {'x' * 400}\n" for index in range(500))
    text = reduce_output("pytest", many + "500 failed in 1s\n")
    lines = text.splitlines()
    assert lines[-1] == f"… {500 - MAX_FAILURES} more"
    assert len(lines) == MAX_FAILURES + 2
    assert len(text) <= MAX_REDUCTION_CHARS


def test_unrecognized_output_reduces_to_nothing() -> None:
    assert reduce_output("ls", "README.md\nsrc\n") is None
    assert reduce_output("echo", "") is None
