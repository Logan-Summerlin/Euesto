# Euesto Improvement Plan

## Purpose

This plan consolidates the current repository review with the observed transcript rendering problem into a prioritized set of improvements. The goal is to improve reliability, scalability, usability, and maintainability without weakening Euesto's existing security model.

The work should generally proceed from **diagnosis and low-risk fixes**, through **executor and permission robustness**, then **larger architectural/UI improvements**, followed by a final repository-wide validation pass.

---

## Guiding principles

- Preserve the current security boundaries: source workspaces remain protected, mutations remain staged, and publication remains explicitly approved and hash-validated.
- Prefer small, measurable fixes before broad refactors.
- Reuse existing primitives instead of introducing duplicate implementations.
- Keep limits, schemas, permissions, implementation, documentation, and tests synchronized.
- Treat regressions in long-running agent sessions and large workspaces as first-class concerns.
- Do not assume that a visual workaround proves the root cause; include diagnostic steps where practical.
- Every behavior change should have regression coverage before the work is considered complete.

---

# Phase 1 — Diagnose and fix the disappearing transcript text

## 1. Reproduce and characterize the rendering failure

Before making a broad rendering refactor, establish a reliable reproduction scenario.

### Investigate

- Test long conversations containing many messages, tool calls, code blocks, headings, tables, and mixed fonts.
- Pay particular attention to content that has been scrolled off-screen and then returned to the viewport.
- Determine whether the missing text occurs while:
  - the underlying model data is unchanged,
  - the delegate still exists,
  - the `TextEdit` still contains the expected HTML,
  - the item's geometry remains correct.
- Test on the Windows configuration where the problem was observed.

### Diagnostic goal

Distinguish between:

1. a QML/model/delegate lifecycle problem,
2. a layout or clipping problem,
3. a Qt Quick scene graph rendering problem,
4. GPU/RHI glyph-cache pressure or eviction.

---

## 2. Test the suspected GPU/RHI rendering mechanism

Run the application with a software scene graph backend:

```text
QSG_RHI_BACKEND=software
```

Compare the reproduction behavior with the normal rendering backend.

### Interpretation

- If the disappearing text continues under software rendering, investigate the QML, model, layout, or delegate lifecycle first.
- If the problem disappears under software rendering while remaining reproducible under the normal GPU backend, treat GPU-backed text rendering or glyph-cache behavior as a strong candidate for the root cause.

This diagnostic mode should remain a diagnostic tool rather than becoming the default application configuration, because full software rendering may unnecessarily reduce overall UI performance.

---

## 3. Try native text rendering as the lowest-risk permanent mitigation

The current transcript renders message content through `TextEdit.RichText`. Introduce `NativeRendering` where supported by the relevant Qt Quick text elements.

Start with the high-value transcript text elements:

- `messageBody` in `Transcript.qml`,
- transcript labels that participate heavily in scrolling,
- other persistent or repeated text elements where the same rendering failure can occur.

Then review the rest of the application for consistency:

- `Composer.qml`,
- `Sidebar.qml`,
- `Main.qml`,
- other repeated or long-lived text-heavy components.

### Desired approach

Avoid scattering unexplained one-off rendering settings throughout the QML tree.

Instead:

1. identify the text-heavy components that need the behavior;
2. apply a consistent rendering policy;
3. document why the policy exists;
4. add a regression-oriented test or smoke test where practical.

If native rendering eliminates the disappearing-text problem without unacceptable visual or performance regressions, adopt it as the preferred mitigation.

---

# Phase 2 — Improve transcript scalability and delegate lifecycle

## 4. Replace the full-history `Repeater` with a virtualized view

The current transcript implementation keeps delegates for the conversation history alive through a `Repeater` inside a `Flickable` and `Column`.

For long agent sessions, this means that message cards, activity rows, and rich-text documents can remain instantiated even when far outside the viewport.

Evaluate replacing this structure with a `ListView` or another virtualized Qt Quick view.

### Requirements

The replacement should preserve:

- incremental updates from `TranscriptListModel`,
- streaming message updates,
- tail-following behavior,
- manual scrolling behavior,
- conversation switching,
- expanded/collapsed activity state,
- message editing,
- regeneration,
- correct rich-text sizing,
- stable scroll position when new content arrives.

### Configuration to investigate

- `reuseItems: true`,
- an evidence-based `cacheBuffer`,
- delayed or lazy loading for expanded tool activity,
- delegate reuse behavior,
- explicit handling of rich-text document size changes.

### Important caution

Virtualization itself can expose lifecycle assumptions that the current always-instantiated `Repeater` hides. The migration should therefore be accompanied by focused tests for delegate reuse, scrolling, conversation replacement, and streaming updates.

---

## 5. Reduce unnecessary rich-text and font pressure

The transcript's Markdown rendering should be reviewed for unnecessary variation in:

- font families,
- font sizes,
- inline style blocks,
- repeated document-level styling.

Where possible:

- centralize shared Markdown styling,
- minimize per-message style duplication,
- avoid creating distinct font/style combinations unless they are semantically necessary.

The goal is not to reduce Markdown fidelity arbitrarily, but to reduce unnecessary rendering and memory pressure in a transcript that may contain hundreds of messages.

---

# Phase 3 — Executor reliability and filesystem consistency

## 6. Make all file mutation paths consistently atomic

Review the executor's write paths and consolidate them around a shared atomic-write primitive.

The review identified that `write.py` performs a direct text write while other mutation/checkpoint paths already use temporary files followed by replacement. The direct path should be brought into alignment with the repository's existing atomicity standard.

### Requirements

- write to a temporary file in the target filesystem,
- flush and synchronize where appropriate,
- atomically replace the destination,
- clean up temporary files after failures,
- preserve the expected newline and encoding behavior,
- add interruption/failure regression coverage where practical.

Prefer extracting or reusing one shared primitive rather than maintaining multiple subtly different atomic-write implementations.

---

## 7. Add total scan and time budgets to filesystem search tools

Review `grep`, `find`, `ls`, and related filesystem traversal paths.

The current configuration includes limits related to scan size, but the supplied review found that the search implementation may treat the byte limit as a per-file threshold rather than a cumulative operation budget.

Add explicit limits for:

- cumulative bytes scanned,
- elapsed wall-clock time,
- optionally maximum visited files/directories where useful.

### Behavior

When a limit is reached:

- stop cleanly,
- return partial results,
- expose `truncated: true`,
- report the reason for truncation,
- avoid turning a large non-matching search into an unbounded executor operation.

The behavior should be consistent with the existing bounded-execution philosophy used elsewhere in the project.

---

## 8. Apply staging exclusions consistently to read-oriented tools

Ensure `find`, `ls`, and text search use the same staging exclusion policy as workspace seeding, checkpointing, and publication visibility.

This prevents directories such as dependency caches or generated environments created inside staging from unexpectedly dominating later agent searches.

The exclusion policy should ideally be centralized so future tools cannot accidentally diverge.

Add tests covering:

- excluded directories present before a tool call,
- excluded directories created by a previous `bash` command,
- explicit user-requested paths near exclusion boundaries,
- interaction with secret-path filtering.

---

## 9. Improve large publish operations

The current publish manifest has a finite file-count ceiling, which can block legitimate large codemods or repository-wide renames.

Do not simply remove the limit.

Instead, design a bounded multi-manifest or batched publication mechanism that preserves:

- explicit approval,
- path validation,
- per-file hash verification,
- atomicity guarantees appropriate to the publication model,
- clear recovery if a later batch fails.

Document the operational semantics before implementation.

---

## 10. Document hard-link limitations

The executor's hard-link protections are security-conscious but can produce surprising failures for legitimate workspaces.

Add documentation covering:

- why hard-linked files are rejected,
- what the user will observe,
- supported workarounds,
- whether the restriction applies to reading, mutation, or both.

Ensure the resulting tool error is clear enough for both users and agents to understand what happened.

---

# Phase 4 — Permission and runtime robustness

## 11. Normalize paths before permission matching

Permission matching should use the same conceptual path representation as executor path validation.

The review identified string-based prefix matching that can diverge from normalized filesystem semantics.

Create or reuse a canonical normalization step before permission evaluation.

Requirements:

- reject invalid paths consistently,
- ensure permission scope and executor scope describe the same object,
- preserve the existing sandbox boundary,
- add tests for:
  - `..` components,
  - mixed separators,
  - trailing slashes,
  - prefix collisions,
  - Windows-style paths.

---

## 12. Define deterministic precedence among matching permission rules

Where multiple `ALLOW` rules match, define an explicit tie-breaking policy rather than depending on tuple or insertion order.

Recommended options to evaluate:

- most specific scope wins,
- longest normalized path prefix wins,
- most recently created rule wins.

Choose one policy, document it, and add regression tests for overlapping rules.

The existing restrictive ordering should remain intact:

```text
DENY > ASK > ALLOW
```

The new rule should only resolve ambiguity within otherwise equivalent decisions.

---

## 13. Prevent indefinitely stalled approval waits

Approval waits should respect the run's wall-clock budget or a separately defined approval timeout.

The current review found that an unanswered approval can leave a run waiting indefinitely while the elapsed budget continues to advance.

Implement bounded waiting with behavior such as:

- calculate the remaining wall-clock budget,
- use a timeout-aware approval wait,
- cancel or pause cleanly when the deadline expires,
- emit a structured event such as `approval.timeout`,
- preserve journal/recovery semantics.

Add tests for:

- approval received before timeout,
- approval timeout,
- run budget exhausted while waiting,
- cancellation during approval wait,
- recovery behavior after timeout.

---

## 14. Replace fragile error-message classification with structured errors

Where possible, errors should originate with explicit error codes rather than being inferred later from message substrings.

Introduce or expand typed/structured executor errors so producers can specify:

- error code,
- human-readable message,
- retryability,
- relevant structured metadata.

Keep string classification only as a fallback for unexpected exceptions.

Add regression tests ensuring that wording changes do not silently alter an error's classification.

---

# Phase 5 — UI safety and observability

## 15. Add link destination visibility before external navigation

The repository's security expectations include showing users where links lead, but the current transcript opens activated links directly.

Implement one of the following:

### Preferred

A confirmation dialog showing the exact destination before opening an external URL.

### Minimum

A visible hover or status preview containing the actual destination.

The implementation should work correctly for:

- `http`,
- `https`,
- `mailto`,
- sanitized Markdown links whose visible text differs from their destination.

Add tests or component-level checks for destination display and activation behavior.

---

## 16. Make rollback events highly visible in the transcript

When a `bash` operation rolls back staging because of a nonzero exit code, the transcript should clearly communicate that the operation's changes were discarded.

The existing structured result may already expose `rolled_back`, but the UI should render that state distinctly rather than relying only on generic attention styling.

Add a dedicated visual representation such as:

- a rollback badge,
- a clear activity title,
- an explanation in expanded tool details.

The user should be able to understand why expected changes disappeared without inspecting raw structured tool output.

---

# Phase 6 — Security and robustness regression hardening

## 17. Expand path-redaction tests

Add regression coverage for absolute-path redaction and platform-specific path formats.

Include:

- Windows drive paths,
- UNC paths,
- mixed slash/backslash paths,
- embedded paths inside larger error strings,
- multiple paths in one error,
- paths emitted by nested exceptions.

This complements the existing path-containment and validation testing.

---

## 18. Review shared invariants and remove implementation drift

For each improvement, compare:

- `AGENTS.md`,
- architecture documentation,
- tool documentation,
- limits documentation,
- permission documentation,
- implementation,
- tests.

Where documentation claims an invariant that the implementation does not enforce, either:

1. implement the invariant, or
2. revise the documentation so it accurately describes the supported behavior.

Avoid leaving aspirational security or UX guarantees written as if they are already enforced.

---

# Phase 7 — Test suite and GitHub Actions stabilization

## 19. Update tests as part of every implementation step

Each improvement above must include corresponding tests rather than postponing test work until after the code changes.

At minimum, add or update coverage for:

### Transcript rendering and lifecycle

- long conversation rendering,
- scrolling away from and back to old messages,
- conversation switching,
- delegate reuse,
- streaming updates,
- rich-text sizing,
- activity expansion,
- link destination handling.

### Executor

- atomic writes,
- interrupted/failed writes where testable,
- cumulative scan limits,
- search timeouts,
- truncation reporting,
- staging exclusions created during execution,
- large publication batching if implemented.

### Permissions and runtime

- normalized path matching,
- overlapping rule precedence,
- approval timeout,
- cancellation,
- budget exhaustion while waiting,
- structured error classification.

### Security

- hard-link behavior,
- path redaction including UNC paths,
- path normalization edge cases,
- link handling.

---

## 20. Run the complete repository validation suite before considering the work complete

The final implementation step must be a clean validation pass against the same checks expected by GitHub Actions.

Before committing any future implementation work:

1. Run the relevant targeted tests for each modified subsystem.
2. Run the full Python test suite.
3. Run Ruff or the repository's configured lint checks.
4. Run Python compilation/static validation required by the project.
5. Run the QML/component checks.
6. Run container or executor integration checks required by the repository.
7. Review failures as compatibility problems rather than merely updating expected values until they pass.
8. Update tests where behavior intentionally changed.
9. Re-run the entire suite from a clean state.
10. Confirm that the exact checks used by GitHub Actions pass locally or in an equivalent environment.

No implementation phase should be considered complete until the test suite and validation commands are updated to reflect intentional behavior changes and **GitHub Actions can run without failing on the resulting commit**.

---

# Suggested implementation order

1. Reproduce the disappearing-text problem.
2. Test `QSG_RHI_BACKEND=software` as a diagnostic.
3. Apply and evaluate `NativeRendering` for the transcript's high-risk text paths.
4. Add regression coverage for transcript rendering behavior.
5. Improve transcript virtualization with a carefully tested `ListView` migration if performance/memory pressure remains significant.
6. Fix atomic writes.
7. Add cumulative search and traversal budgets.
8. Unify staging exclusions.
9. Normalize permission matching and define deterministic rule precedence.
10. Add bounded approval waits.
11. Replace fragile string-based error classification with structured errors.
12. Improve link destination safety and rollback visibility.
13. Address publish batching and hard-link documentation.
14. Expand security regression coverage.
15. Synchronize documentation with the final implementation.
16. Update and run the complete test suite and all GitHub Actions-equivalent checks until clean.

---

# Definition of done

The improvement effort is complete only when:

- the disappearing-text issue has either been fixed or its cause has been conclusively narrowed and mitigated;
- transcript performance remains stable with long conversations;
- text rendering behavior is consistent across supported Windows configurations;
- filesystem mutations follow a consistent atomicity strategy;
- search and traversal operations have explicit resource bounds;
- staging exclusions are applied consistently;
- permission matching is normalized and deterministic;
- approval waits cannot stall runs indefinitely;
- executor errors use stable structured classifications where practical;
- external link destinations are visible before navigation;
- rollback behavior is obvious in the UI;
- security edge cases have regression coverage;
- documentation matches actual behavior;
- targeted tests and the full repository suite pass;
- Ruff, compilation/static checks, QML checks, container checks, and all other configured GitHub Actions checks pass;
- the test suite has been updated for intentional behavior changes so future commits do not cause GitHub Actions failures.