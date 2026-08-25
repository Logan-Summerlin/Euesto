# Repository Organization and Simplicity Plan

## Purpose

This plan compares the current repository with the ten-point organization checklist and defines the smallest useful adjustments. The repository is already strongly organized around its security/runtime boundaries; the plan therefore favors incremental refactoring over a broad directory reshuffle. It also applies **YAGNI** (do not add structure or abstractions without a demonstrated need) and **KISS** (prefer direct, local, predictable code).

## Assessment at a glance

| # | Criterion | Current result | Adjustment needed |
|---|---|---|---|
| 1 | One obvious home per major concept | **Meets, with a few boundary ambiguities** | Document ownership rules; reduce desktop bridge concentration. |
| 2 | Extremely simple top level | **Mostly meets** | Remove/relocate local runtime artifacts; decide whether the historical archive is worth retaining. |
| 3 | Production/tests/docs/tooling separated | **Meets; test taxonomy work already underway** | Continue the in-progress `tests/unit/<domain>/`, `tests/docker/`, `tests/ui/` reorganization defined in `docs/TESTING.md`; verify no coverage was lost in the move (see 3a). |
| 4 | Architecture discoverable from tree | **Meets strongly** | Clarify the `qml/`/`src/` split and the desktop entry-point relationship. |
| 5 | Boring, predictable naming | **Mostly meets** | Normalize `ARCHIVED DOC`; resolve the Euesto/LocalOpenRouterChat naming transition. |
| 6 | Focused files/modules | **Needs adjustment** | Decompose `src/qml_backend.py`; review `storage.py` and possibly the agent runtime. |
| 7 | Explicit, low-coupling dependencies | **Mostly meets** | Preserve layer direction; introduce narrow desktop-facing services during the bridge refactor. |
| 8 | Documentation for navigation | **Mostly meets** | Add a small `docs/README.md` index — now including the new `docs/TESTING.md` — and a contributor "where to change what" map. |
| 9 | Docs close to reality | **Mostly meets** | Correct the stale executor docstring and add lightweight consistency checks. |
| 10 | Optimized for the next developer | **Mostly meets** | Provide a short onboarding path (with the updated tiered test commands), ownership map, and executable validation commands. |

## Evidence and proposed adjustments

### 1. Give every major concept one obvious home

**What is working:** The major runtime concepts have clear homes: `src/` for desktop code, `server/` for the gateway, `executor/` for the sandbox, `shared/` for framework-neutral protocol structures, `qml/` for Qt Quick views, `tests/` for tests, `docs/` for authoritative documentation, and `scripts/` for developer helpers. This matches the documented boundary `Desktop -> Gateway -> Executor`.

**Problem:** `src/qml_backend.py` is a 2,008-line `DesktopBridge` that currently owns QML properties and actions for conversations, settings, runtime management, generation, approvals, staging/publication, import/export, and context/usage operations. The concept names have homes, but their implementation is concentrated in one façade, making the "obvious home" less useful in practice.

**Plan:** Keep `DesktopBridge` as the thin QML adapter and move cohesive operations behind small desktop services/controllers, for example conversation, generation, settings, runtime, and staging/publication coordination. Do not create a generic `utils` or framework layer. Each extracted service should have one responsibility, explicit constructor dependencies, and tests for behavior that moved.

### 2. Keep the top-level directory extremely simple

**What is working:** The root contains sensible entry points and categories: `app.py`, `src/`, `server/`, `executor/`, `shared/`, `qml/`, `tests/`, `docs/`, `scripts/`, packaging/container directories, configuration, and README/project metadata.

**Problems:** The workspace currently contains `.local-chat-snapshot.json` and `.local-chat-checkpoints/`, which are runtime/user artifacts rather than source organization. They should never be committed and should be ignored/documented as local state. `ARCHIVED DOC/` is also a root category with only historical material; it is explicitly non-normative, but it adds navigation surface.

**Plan:**
1. Confirm local snapshot/checkpoint state is ignored and absent from version control; if this repository is distributed as a source tree, add an explicit local-state note to the contributor/troubleshooting documentation.
2. Keep only the minimal historical archive, or move it outside the active source tree if design rationale is no longer needed. Do not create new archive folders for superseded plans.
3. Keep root metadata files as-is unless a packaging/build convention requires a change; moving `app.py` solely for aesthetic reasons would add churn without improving the architecture.

### 3. Separate production code, tests, documentation, and tooling

**Result: meets, and the branch has already started the deeper reorganization this section anticipated.** Production code is separated from the ~40 Python test files; documentation is in `docs/` plus intentionally visible root project files; scripts are in `scripts/`; CI is in `.github/`; packaging is in `build/` and `installer/`; container assets are in `docker/`.

**What changed on this branch:** A new `docs/TESTING.md` defines a real test taxonomy — unit, contract, integration, security, and structural — and the reorganization commit began placing tests accordingly: `tests/unit/executor/`, `tests/unit/gateway/`, `tests/unit/publication/`, `tests/docker/`, and `tests/ui/` now exist. The rollout is partial — most files are still flat at `tests/` root, and `integration/`/`security/` subfolders don't exist yet — but the taxonomy and the reason for it (a documented, reviewed test tier system tied to pytest markers, not an aesthetic reshuffle) are exactly the "repeated shared fixtures or a real test taxonomy" justification the original guidance asked for.

**Plan (superseding the previous "do not add test subpackages" guidance):**
1. Treat `docs/TESTING.md`'s taxonomy as authoritative going forward. New tests written during Phase 2/3 desktop-bridge extraction should land under `tests/unit/<domain>/` (matching the `executor`/`gateway`/`publication` pattern already established) rather than at `tests/` root.
2. Finish the partial rollout opportunistically — as files are touched for other reasons, move them into the matching tier directory — rather than doing a single disruptive mass move.
3. Keep using pytest markers (`slow`, `docker`) as the mechanism for tier separation rather than curated file lists in CI; `docs/TESTING.md` already states CI must use marker expressions, not file allowlists — preserve that rule.
4. Still avoid `conftest.py` proliferation, an `experiments/` directory, or additional taxonomy tiers beyond the five already documented unless a concrete need appears.

### 3a. Verify no test coverage was lost during the reorganization

**Problem:** The reorganization commit did not purely move files — in at least two cases it reduced coverage:

- `tests/test_bash.py` (14 tests covering shell syntax, workspace-traversal rejection, timeout rollback, cancellation, restricted environment variables, TTY rejection, large-output truncation, and event-retention bounds) was replaced by `tests/unit/executor/test_bash.py`, which keeps only 3 of the original tests. The remaining 11 do not appear relocated elsewhere in the suite, and the file's last test has no assertions.
- `tests/test_transcript_qml.py` (257 lines of QML `Transcript` rendering tests run through `QQmlApplicationEngine`) was deleted outright with no replacement.

**Why this matters for the rest of the plan:** Phase 2 below depends on the existing test suite to characterize `DesktopBridge`/QML behavior before extracting code from it, and the executor's bash tool is explicitly the kind of security-sensitive surface this plan says not to touch without care. Starting the extraction work on a suite with less coverage than the plan assumed increases the risk of an unnoticed regression.

**Plan:**
1. Before beginning Phase 2, confirm with whoever authored `fix-test-suite` whether the dropped bash and transcript-QML assertions were intentionally cut (e.g., because they were slow/flaky) or lost in the reorganization.
2. If unintentional, restore the missing bash-tool assertions into `tests/unit/executor/test_bash.py` (or a sibling file in the same directory), and either restore `tests/test_transcript_qml.py` under `tests/ui/` or confirm equivalent coverage exists.
3. If intentional — for example, the QML rendering tests require a full Qt Quick runtime and were judged too slow/environment-dependent for the fast tier — mark them with the `slow` marker and relocate them under `tests/ui/` rather than deleting them, consistent with how `tests/docker/test_container_policy.py` was preserved and tagged instead of removed.

### 4. Make the architecture discoverable from the directory structure

**Result: meets strongly.** The tree communicates the desktop/gateway/executor split, and `docs/ARCHITECTURE.md` explains it.

**Minor ambiguity:** QML views are in `qml/`, while their Python bridge is in `src/qml_backend.py`; a newcomer may not know which UI behavior belongs in QML, the bridge, or a desktop service.

**Plan:** Add a short ownership table to `docs/ARCHITECTURE.md` or the new documentation index:

- `qml/`: presentation and Qt Quick composition;
- `src/`: desktop-side state, controllers, persistence, runtime, and publication interfaces;
- `app.py`: process/bootstrap wiring only;
- `server/`: gateway/provider/agent behavior;
- `executor/`: bounded workspace operations only;
- `shared/`: protocol types and registries, with no application-specific orchestration.

Avoid renaming directories merely to make the tree look more layered; the existing runtime boundaries are clearer than a speculative `api/services/models` hierarchy.

### 5. Prefer boring, predictable naming

**What is working:** Python packages and most modules use lowercase, descriptive names (`gateway_client.py`, `runtime_manager.py`, `checkpoints.py`). The public tool vocabulary is centralized and consistent. As a positive sign that this norm is already taking hold, `fix-test-suite` renamed several vaguely numbered test files — `test_phase5_resources.py` → `test_budget_resources.py`, `test_phase6_regression_matrix.py` → `test_executor_regression.py`, `test_executor_phase2_contract.py` → `test_executor_dispatch_limits.py`, `test_tooling_phase2.py` → `test_tool_edge_cases.py`, `test_tooling_phase5.py` → `test_tooling_contract.py` — to names that describe what they verify instead of when they were written.

**Problems:** `ARCHIVED DOC/` uses capitals and a space. The product is called `Euesto` in user-facing documentation while runtime identifiers still use `LocalOpenRouterChat`; this is understandable during a rename but is a discoverability hazard. The distinction between `qml/` and `qml_backend.py` is also not obvious from names alone.

**Plan:**
1. Rename the archive to a predictable lowercase name such as `archived-doc/` only if the archive remains in the repository; update all links and CI/package references in the same change.
2. Choose one canonical product name for user-facing docs and record the legacy runtime identifier once in a naming note. Do not perform a risky global rename of storage paths, installer identifiers, or compatibility-sensitive values without a dedicated migration decision.
3. Use domain names (`conversation`, `generation`, `settings`, `publication`) for extracted modules; avoid `helpers.py`, `common.py`, `manager.py`, or unexplained abbreviations. Apply the same "describe what it verifies" standard already used for the renamed test files above to any new modules extracted from `DesktopBridge`.

### 6. Keep files and modules focused

**Highest-priority issue:** `src/qml_backend.py` is approximately 80 KB/2,008 lines and contains a very large `DesktopBridge` with dozens of slots and properties across unrelated workflows. This is the clearest violation of the focused-module and KISS goals.

**Secondary candidates:** `src/storage.py` is about 839 lines; `server/service.py` about 551 lines (now with a small cancellation-robustness fix from `fix-test-suite` that waits briefly for an in-flight task before force-cancelling it — unrelated to this plan, no action needed); `server/agent/runtime.py` about 390 lines; and `executor/checkpoints.py` about 373 lines. Size alone is not proof of a design problem: `runtime.py` and `checkpoints.py` each represent cohesive sequential/security-sensitive workflows. Inspect cohesion and dependency direction before splitting them.

**Plan, in order:**
1. Inventory `DesktopBridge` methods by responsibility and write characterization tests around public QML behavior. (Confirm the coverage gap in 3a is resolved first, since it directly affects this step.)
2. Extract one vertical slice at a time, starting with the least security-sensitive concern (conversation/history or settings), then runtime/generation, then staging/publication coordination.
3. Keep signal/property translation in the bridge; keep business rules in services that do not import QML types.
4. Update `app.py`, QML-facing tests, and imports after each slice; delete forwarding code that is no longer needed.
5. Reassess `storage.py` and `server/service.py` after the bridge work. Split only if a named responsibility has independent invariants or tests. Do not split `server/agent/runtime.py` merely to reduce line count.

### 7. Make dependencies explicit and minimize coupling

**What is working:** `shared/` is a framework-neutral boundary; the executor does not depend on desktop internals; the gateway reaches the executor through `server/executor/client.py`; and the repository documents tool/schema synchronization. These are strong architectural choices.

**Risks:** The size/import surface of `DesktopBridge` makes it a likely coupling hub. Root `app.py` also performs bootstrap/subclass wiring and hardcodes an investigation-model default, which should remain configuration rather than domain logic.

**Plan:**
- Record intended dependency direction in the architecture doc and enforce it with a small import-boundary test or static check.
- Give extracted services explicit collaborators via constructors or narrow protocols; do not use module-level singletons or a service locator.
- Keep QML/PySide imports at the UI adapter boundary.
- Keep provider, executor, and publication security decisions in their current owning layers; the desktop façade should coordinate, not reimplement them.
- Add tests for forbidden imports (`executor`/`shared` must not depend on `src` or `server` internals) only if the current test suite does not already cover the rule.

### 8. Write documentation for navigation

**What is working:** `README.md` has quick start, modes, checks, a repository map, and links to architecture, tools, limits, publication, contributing, troubleshooting, roadmap, and container guidance. `AGENTS.md` and `docs/CONTRIBUTING.md` reinforce the boundaries. `fix-test-suite` adds `docs/TESTING.md`, which documents the test tiers, async test synchronization policy, and the "one regression test per bug fix" review rule.

**Gap:** `docs/` has no index page, so someone browsing that directory does not get an immediate "start here" route. The contributor guide explains tool changes well but could better explain ordinary code ownership.

**Plan:** Add `docs/README.md` with a short reading order:

1. `ARCHITECTURE.md` for boundaries;
2. `CONTRIBUTING.md` for setup and checks;
3. `TESTING.md` for test tiers, markers, and the regression-test policy;
4. `TOOLS.md`, `LIMITS.md`, and `PUBLICATION.md` for security-sensitive changes;
5. `TROUBLESHOOTING.md` for operational issues.

Add a "Where to make a change" table, not a second architecture narrative. Link it from the root README and keep examples copy/pasteable.

### 9. Keep documentation and code close to reality

**What is working:** The authoritative docs describe the current eight-tool API, staging, publication, limits, and security model. `PROJECT_PLAN.md` is labeled as a status roadmap and the archive is labeled non-normative.

**Known discrepancy:** `executor/tools/__init__.py` says `Canonical seven-tool executor surface`, while the current public vocabulary is eight tools. The eighth tool is gateway-delegated rather than implemented in that package, so the package docstring should say what it actually exports rather than claim a count that conflicts with repository docs.

**Plan:** Change that docstring to a precise description of the executor-implemented tool exports, and add a lightweight documentation/protocol consistency test or check for tool count/names where practical. Avoid generated documentation infrastructure unless the project begins exposing a library API; for this application, small executable checks are more YAGNI/KISS-aligned.

### 10. Optimize for the next developer

**Result: mostly meets.** A newcomer can find installation, tests, architecture, security constraints, and container instructions. The main friction points are the oversized desktop bridge, archive naming, local runtime artifacts, and the missing docs index.

**Plan:** Make the following the canonical onboarding path in the README/index:

1. Read the one-paragraph product and boundary summary.
2. Create the Python 3.12 environment and install `requirements-dev.txt` (now includes `pytest-asyncio` and `pytest-timeout` alongside `pytest` and `ruff`).
3. Run the tiered checks — `pytest` (fast tier), `pytest -m "slow and not docker"` (slow tier), `pytest -m docker` (container tier) — and `ruff check .`; use compile/QML/container checks when relevant.
4. Read `docs/ARCHITECTURE.md` and the ownership table.
5. Make a small change in the owning boundary and run the applicable checks.

Keep the commands authoritative in one place where possible and link to it elsewhere. Add a small "first change" example only if it can remain accurate without duplicating implementation details.

## YAGNI/KISS guardrails

- Do not reorganize the entire tree into generic layers (`models`, `services`, `utils`) without a concrete ownership problem.
- Do not add a plugin system, compatibility aliases, alternate tool names, or speculative abstractions; the repository's existing non-goals explicitly reject these.
- Do not split cohesive security-sensitive workflows just to satisfy a line-count target.
- Prefer one narrow service extraction and one focused test change at a time.
- Delete obsolete forwarding modules and stale documentation after migration; do not leave parallel implementations.
- Keep public behavior unchanged during organizational refactors unless a separate, reviewed behavior change is intended.
- When relocating tests as part of the ongoing `tests/unit/<domain>/` rollout, move and, if needed, re-tag with markers — don't drop assertions in the process (see 3a).

## Phased implementation order

### Phase 1 — low-risk hygiene

- Add `docs/README.md` and the ownership/navigation table, including `docs/TESTING.md` in the reading order.
- Correct the stale executor docstring.
- Confirm `.local-chat-snapshot.json` and `.local-chat-checkpoints/` are local-only and ignored.
- Decide whether to retain and, if retained, normalize the archive directory name.
- Decide and document canonical product naming without changing runtime identifiers yet.
- Resolve the 3a coverage gap: confirm the dropped bash-tool and transcript-QML tests were an intentional trim, and restore or re-tag/relocate them if not.

### Phase 2 — desktop bridge characterization

- Map `DesktopBridge` methods and imports by responsibility.
- Add or update tests for conversations, settings, generation, runtime, approval, and publication behavior before moving code — placing new tests under `tests/unit/<domain>/` per the branch's established taxonomy.
- Define narrow service interfaces and dependency direction.

### Phase 3 — incremental extraction

- Extract one cohesive desktop workflow at a time.
- Keep the bridge as a thin QML adapter and preserve signals, slots, property names, and user-visible behavior.
- Run the applicable tiered test/lint/compile/QML suite (`pytest`, `pytest -m "slow and not docker"`, `pytest -m docker`, `ruff check .`, compile check, `qmllint`) after each extraction.
- Reassess `storage.py` and `server/service.py` only after observing actual cohesion/coupling problems.

### Phase 4 — enforce and maintain

- Add import-boundary and tool/documentation consistency checks where they provide durable value.
- Update the repository map and docs whenever ownership changes.
- Remove obsolete modules, links, and archive entries rather than accumulating historical alternatives.
- Continue moving remaining flat `tests/*.py` files into `tests/unit/<domain>/` (or `integration/`/`security/`, once those tiers are created) opportunistically as files are touched.

## Completion criteria

The repository will match the checklist substantially when:

- every major runtime concern has a named owning directory/module;
- no committed local snapshot/checkpoint artifacts remain in the root;
- `DesktopBridge` is an adapter rather than a multi-domain business-logic container;
- QML, desktop services, gateway, executor, and shared protocol dependencies are explicit and one-directional;
- naming and product terminology are documented and consistent;
- `docs/README.md` gives a new contributor a direct reading path, including `docs/TESTING.md`;
- the stale seven-tool wording is removed and tool/documentation consistency is checked;
- the test suite is organized under `docs/TESTING.md`'s taxonomy with no unresolved coverage loss from the reorganization (3a);
- all three test tiers (fast, slow, docker) and required checks pass without provider credentials; and
- no new abstraction, directory, compatibility layer, or framework was introduced without a concrete maintenance benefit.

## Validation commands

Run the applicable repository checks after each phase:

```text
pytest                           # fast tier: not slow and not docker
pytest -m "slow and not docker"  # slow tier
pytest -m docker                 # container/configuration tier
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Container/security changes must continue to validate non-root execution, blocked egress, mount modes, resource limits, traversal/link rejection, staging recovery, and exact tool-mode boundaries.
