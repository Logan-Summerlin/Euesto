# Coding Harness Quality-of-Life Plan

## Purpose

This document records improvements to the local coding harness identified during repository investigation and implementation work. The goals are to make edits more auditable, reduce avoidable tool friction, and improve confidence when validating changes in a staged workspace.

These improvements must preserve the existing security model: plan-mode tools remain read-only, agent mutations remain staged, executor boundaries remain unchanged, and publication remains explicitly approved and hash-validated.

## Prioritized improvements

### 1. Make file reads tolerant of oversized line ranges

If a requested `end_line` exceeds the file length, return the available range and include a non-fatal warning rather than failing the entire read. Preserve strict errors for invalid start lines, malformed arguments, and byte-limit violations.

Acceptance criteria:

- `read(file, 1, 999999)` returns the file when the file is shorter than 999999 lines.
- The response reports the actual returned range and whether clipping occurred.
- Existing byte and path safety limits remain authoritative.

### 2. Improve exact-edit diagnostics and newline handling

Make exact edits robust to normal newline representation while retaining exact-content safety. On a failed match, report useful bounded diagnostics, such as the number of matches found and a short escaped context preview.

Acceptance criteria:

- CRLF/LF differences are handled according to an explicit documented policy.
- A failed edit never silently applies a fuzzy or ambiguous replacement.
- The error identifies whether the failure was zero matches, too many matches, hash conflict, or malformed context.
- Matching remains path-bounded and hash-validated.

### 3. Provide an auditable structured patch operation

Add a harness-native way to apply a bounded multi-file patch, instead of requiring a Bash/Python script for routine source changes. It should use the same staging, checkpoint, path, size, and rollback primitives as existing mutations.

Acceptance criteria:

- Patch operations are represented in structured request data and included in mutation results.
- Every changed path is normalized, validated, and included in the checkpoint.
- Partial patch application rolls back on failure, cancellation, or timeout.
- The operation does not create an alias or legacy public tool vocabulary.
- The public tool registry, permissions, documentation, and tests are updated together if this becomes a model-facing tool.

### 4. Add an environment and validation preflight report

Expose a read-only preflight command or report showing available validation tools and versions, including Python, pytest, Ruff, Qt/QML linting, and relevant package metadata.

Acceptance criteria:

- Missing tools are reported clearly before validation starts.
- The report distinguishes unavailable tools from failed checks.
- It does not expose secrets or unrestricted environment variables.
- It works without provider credentials.

### 5. Clarify mutation transaction and rollback status

Separate command-level failure, mutation-transaction rollback, and current staged-workspace state in tool responses.

Acceptance criteria:

- A failed command states exactly which transaction was rolled back.
- The response states whether prior staged changes remain present.
- Status output is internally consistent with the staged workspace.
- Regression tests cover failed Bash commands, failed writes, and unavailable validation commands.

### 6. Add native staged status and diff inspection

Provide harness-native status and diff inspection, independent of the presence of Git. These should summarize created, modified, deleted, and permission-changed files and expose bounded textual diffs.

Acceptance criteria:

- Status is available for an empty and non-empty staging area.
- Diffs are bounded by bytes and lines.
- Secret, staging metadata, and checkpoint content are excluded.
- Output includes enough metadata to review a pending publication safely.

### 7. Make delegated repository investigation more interactive

Support a configurable investigation-call budget while retaining a hard safety ceiling. The default should remain conservative; a session may request a higher budget when a broad task benefits from an initial survey followed by focused follow-ups.

The requested initial enhancement is to permit **up to four investigation subagent calls per turn**. This requires executor/runtime policy changes, not merely a documentation change, because the current limit is enforced by the harness outside the repository tool implementation.

Acceptance criteria:

- The effective budget is the minimum of the session request, configured budget, and hard ceiling.
- The hard ceiling is four calls per turn for the first rollout.
- Calls remain read-only and restricted to the Plan tool set.
- Nested investigation cannot mutate files, run commands, publish, or create independent sessions.
- Call accounting is per parent turn and resets at the documented boundary.
- Exceeding the budget returns a clear bounded error.
- Telemetry records count and outcome without recording sensitive repository contents.

### 8. Add QML-aware validation support

Provide a standard QML validation path in the harness, with a useful fallback when `pyside6-qmllint` is unavailable. Ideally this should include a Qt-enabled validation environment or container matching the supported runtime.

Acceptance criteria:

- QML syntax and import errors are reported separately from Python failures.
- The validator uses the project’s supported Qt/PySide version.
- Missing Qt tooling is reported as unavailable rather than silently skipped.
- A minimal smoke test loads the primary QML components where the environment supports it.

## Plan for four investigation calls per turn

### Current constraint

The repository’s durable guidance currently describes `investigate_repository` as capped at two calls per turn, and the local executor context enforces that cap. Repository changes alone cannot raise the runtime-enforced limit.

### Proposed rollout

1. **Specify the contract**
   - Define “turn” as one parent assistant request from initial tool use through the final response.
   - Define that only top-level `investigate_repository` calls count.
   - Keep nested investigation read-only and limited to `read`, `grep`, `find`, and `ls`.
   - Return remaining budget and a stable error when the fourth-call budget is exhausted.

2. **Update policy/configuration**
   - Change the harness call-budget constant from two to four.
   - Keep the value configurable only within a hard ceiling of four.
   - Make the effective value visible in session diagnostics.
   - Update the runtime facts and durable repository guidance so they do not contradict one another.

3. **Preserve isolation and accounting**
   - Reuse the parent executor session and its workspace view.
   - Do not permit concurrent staging, independent agents, mutation, command execution, or publication through investigation.
   - Count failed, timed-out, and rejected investigation calls consistently so retries cannot bypass the limit.

4. **Add regression coverage**
   - One, two, three, and four calls succeed when otherwise valid.
   - The fifth call is rejected without repository access.
   - A failed call still consumes its allocated attempt according to the documented policy.
   - Nested tool attempts outside the Plan tool set are rejected.
   - The counter resets for a new parent turn.
   - No mutation or publication state changes after any investigation call.

5. **Roll out safely**
   - Implement the runtime change behind a small, auditable configuration change.
   - Run the full test suite and security/container checks.
   - Update `AGENTS.md`, authoritative tool documentation, and troubleshooting guidance together.
   - Monitor call counts, latency, and token/resource usage before considering a larger budget.

## Suggested implementation order

1. Native status/diff and clearer rollback reporting.
2. Read-range tolerance and edit diagnostics.
3. Preflight reporting and QML validation.
4. Structured patch support, only after its public contract is reviewed.
5. Four-call investigation budget, with runtime policy, documentation, and regression tests updated together.

## Validation checklist

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

When a validation dependency is unavailable, the harness should report that fact explicitly and continue with checks that can run safely.
