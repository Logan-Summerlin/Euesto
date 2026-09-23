# Euesto Coding-Harness Fix Plan (combined, verified against source)

> **Archived — implemented; not normative.** Formerly `docs/HARNESS_FIX_PLAN.md`. Every P0, P1, and P2 task is implemented (see the harness entries in `CHANGELOG.md`). Two items remain open and are carried in [`docs/ROADMAP.md`](../docs/ROADMAP.md): copy-on-write staging (P1-4 fix 3) and the P3 product-scope bullets. Current behavior is documented in `docs/TOOLS.md`, `docs/LIMITS.md`, `docs/PUBLICATION.md`, and `docs/EGRESS.md`; file and line citations below describe the tree as it was when the plan was written.

## 0. How to use this document

This plan lives at `archived-doc/harness-fix-plan.md` (it was the root-level `EUESTO_HARNESS_FIX_PLAN.md` until P2-5). The file and line citations below describe the tree as it was verified; each task's **Status** line records what changed since.

This merges two prior write-ups — `Coding-harness weaknesses.txt` (terse bug list) and
`Euesto_Critique_Analysis.md` (line-by-line critique review) — into one execution plan,
**re-verified directly against the repository** at:

- Repo: `Logan-Summerlin/Euesto`, branch `main`
- Commit: `8940f8ac6bae170009513cf5ad5c6695d6fff0f4` (2026-09-06) — this already includes the
  former `quality-fixes` branch (merged via PR #24) **and** a follow-up `fix-bash-rollback`
  commit (PR #25). If you are starting from an older checkout, diff against this commit first;
  several items below are already done there and re-doing them will just cause merge noise.

Every task cites the exact file(s)/line(s) as they exist at that commit, a **verified** current
snippet, why it matters, a concrete fix, and acceptance criteria. Tasks are grouped by tier
(P0 correctness contradictions → P1 high-cost/high-friction gaps → P2 structural/QoL →
P3 roadmap-scope) and end with a suggested execution order and a definition-of-done checklist.

**Ground rule for every task below (per `AGENTS.md` → "Change discipline"):** when a public
tool's behavior, schema, or limits change, update `shared/tools.py`, `server/openrouter/agent.py`,
the executor dispatcher/permissions, the relevant tests, **and** `docs/TOOLS.md` / `docs/LIMITS.md`
in the same change. Don't leave stale compatibility shims for old callers.

---

## 1. Already fixed — do not re-implement

Both source documents were written against an earlier state of the branch. These are now done;
confirmed by reading the current code:

- **Bash rollback-on-failure is no longer unconditional.** `executor/tools/bash.py` now takes a
  `rollback_on_failure: bool = True` argument, only rolls back when `returncode != 0 and
  rollback_on_failure`, and returns `rolled_back` / `rollback_reason` / `rollback_on_failure` in
  the tool result. The model-facing schema in `server/openrouter/agent.py` exposes
  `rollback_on_failure` on the `bash` tool. `archived-doc/harness-qol-plan.md` item 5 is marked
  `(implemented)`. **No further action needed here** beyond Task P2-6 below (docs/tests polish).
- **Checkpoint storage is already content-addressed.** `executor/checkpoints.py::create_checkpoint`
  writes into a shared `objects/<sha256>` store and skips re-copying a blob that's already there,
  with `_prune()` garbage-collecting unreferenced objects against a storage budget. The *storage
  cost* problem from the weaknesses doc is handled. **The full-tree re-hash cost is not** — see
  Task P1-4, which is still open and is the more expensive half of that original complaint.

---

## 2. Tier P0 — correctness contradictions (fix first; small, testable, unblocks other work)

### P0-1. The 512 KB protocol argument cap silently defeats the 1 MB+ write/edit/bash-stdin limits

**Files:** `shared/tools.py`, `executor/config.py`, `executor/tools/write.py`, `executor/tools/bash.py`

**Verified:**
```python
# shared/tools.py
MAX_TOOL_ARGUMENT_BYTES = 512_000
...
if len(repr(self.arguments).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
    raise ValueError("Tool arguments are too large")
```
```python
# executor/config.py — coding profile
max_write_bytes: int = 1_000_000        # hard ceiling 8_000_000
max_bash_stdin_bytes: int = 1_000_000   # hard ceiling 8_000_000
```
Every tool call is wrapped in a `ToolRequest` whose `arguments` dict is size-checked against
`MAX_TOOL_ARGUMENT_BYTES` **before** it ever reaches `write`/`edit`/`bash`. Since a `write` call's
`content` (or a `bash` call's `stdin`) lives inside `arguments`, no call carrying more than
~512 KB of payload can ever be dispatched — regardless of the `max_write_bytes` /
`max_bash_stdin_bytes` values the executor profile advertises (1 MB default, up to 8 MB on
`large-workspace`/hard ceiling). The protocol layer silently makes ~half of the documented write
capacity unreachable, and 100% of the "large-workspace" 2 MB write profile unreachable.

**Fix (pick one, ordered by preference):**
1. Raise `MAX_TOOL_ARGUMENT_BYTES` to track the largest `HARD_CEILING` that can appear in any
   argument-carrying field (`max_write_bytes`, `max_edit_result_bytes`, `max_bash_stdin_bytes`,
   `max_command_bytes`), plus a fixed envelope overhead (JSON/`repr` structure, other fields), and
   document the derivation inline as a comment so it can't silently drift again.
2. If keeping the protocol cap low is intentional (e.g. IPC framing reasons), add a chunked-write
   path (`write` accepts an `append`/`offset` mode, or a dedicated `write_chunk` operation that the
   executor assembles server-side under the *existing* checkpoint) instead of raising the cap.
3. At minimum, lower `max_write_bytes`/`max_bash_stdin_bytes`/`max_edit_result_bytes` defaults and
   hard ceilings to something actually reachable under 512 KB, and say so explicitly in
   `docs/LIMITS.md`, so the documented limit stops lying about achievable capacity.

Prefer option 1 or 2; option 3 is a regression in stated capability and should only be the choice
if neither is feasible.

**Acceptance criteria:**
- A `write` call with content sized exactly at the profile's `max_write_bytes` succeeds end-to-end
  (protocol → executor → staging) for every profile (`small`, `coding`, `large-workspace`).
- `docs/LIMITS.md` gains a row for `MAX_TOOL_ARGUMENT_BYTES` (or its replacement mechanism) showing
  it is *not* the binding constraint below the documented per-tool limits.
- Regression test: `tests/unit/executor/test_write.py` (or wherever write limits are tested) gets a
  case at exactly `max_write_bytes` for at least the `coding` and `large-workspace` profiles.

---

### P0-2. Investigation call budget resets once per *run*, not once per *turn*

**File:** `server/agent/runtime.py`

**Verified:**
```python
class ...:
    def __init__(...):
        self._investigation_calls: dict[str, int] = {}
        ...
    async def run(...):
        ...
        while True:                       # <- one iteration of this loop is one "turn"
            ...
            turn = await agent_turn(...)
            ...
        finally:
            self._investigation_calls.pop(run_id, None)   # <- only cleared when the WHOLE run ends
            self._investigation_call_budget.pop(run_id, None)
    async def _investigate_repository(self, run_id, ...):
        count = self._investigation_calls.get(run_id, 0)
        self._investigation_calls[run_id] = count + 1
        ...
        if count >= budget_limit:   # budget_limit is 4 by INVESTIGATION_HARD_CALL_CEILING
            ... "Investigation call budget exhausted (4 calls per turn)."
```
`_investigation_calls` is keyed only by `run_id` and is only removed in the `finally` of the outer
`run()` coroutine — which wraps the entire multi-turn `while True` agent loop, not a single turn.
`README.md`/`AGENTS.md`/`docs/TOOLS.md` all describe this as a **per-turn** budget ("up to four
calls are accepted per turn"). In the current code it is a **per-run, lifetime-of-the-session**
budget: after 4 total `investigate_repository` calls across *any number of turns*, every
subsequent call in that run returns `investigation.call_limit` forever, even on turn 50 of a long
session. The error message itself ("4 calls per turn") is then factually wrong about the state
that produced it.

**Fix:** Move the counter to per-turn scope. Concretely: reset (or use a fresh dict entry for)
`self._investigation_calls[run_id]` and `self._investigation_call_budget[run_id]` at the top of
each iteration of the `while True:` loop in `run()` (i.e., right after `budget.consume_iteration()`
or right before dispatching `turn.tool_calls`), not only in the outer `finally`.

**Acceptance criteria:**
- A session that calls `investigate_repository` 4 times in turn 1, then continues to turn 2, is
  able to call it up to 4 more times in turn 2.
- New regression test in the runtime test suite: run two turns, exhaust the budget in turn 1,
  assert turn 2 starts with a fresh budget.
- Update `docs/TOOLS.md` / `AGENTS.md` wording only if the semantics you land on differ from "four
  calls per turn" — otherwise the docs are already correct and just need the code to match them.

---

### P0-3. Investigation subagent wall-time is effectively unbounded

**File:** `server/agent/runtime.py`, inside `_investigate_repository`

**Verified:**
```python
child = RunBudget(
    min(parent_budget.remaining_iterations, INVESTIGATION_MAX_ITERATIONS),
    max(10, int(parent_budget.remaining_wall_seconds)),   # <- wall-time budget
    allowance,
    min(parent_budget.remaining_tool_calls, INVESTIGATION_MAX_TOOL_CALLS),
    "investigation",
)
```
The child's wall-clock budget is `max(10, parent_remaining_wall_seconds)` — i.e. it inherits
*almost all* of the parent's remaining wall-clock time, floored only at 10 seconds with **no
ceiling**. A parent run early in its 1,800 s (`coding` profile) budget can hand a "cheap read-only
investigation subagent" up to ~1,790 seconds, defeating the purpose of a bounded, cheap-model
delegation and letting one investigation call stall the entire run.

**Fix:** Cap the child wall-time independently, e.g.
`min(max(10, int(parent_budget.remaining_wall_seconds)), INVESTIGATION_MAX_WALL_SECONDS)` with a
new constant (the weaknesses doc suggests ~300 s as a reasonable ceiling — pick a value consistent
with `INVESTIGATION_MAX_ITERATIONS`/`INVESTIGATION_MAX_TOOL_CALLS`, which already exist as
independent caps in the same file and can be used as the pattern to follow).

**Acceptance criteria:**
- A parent run with e.g. 1,500 s remaining still produces a child `RunBudget` with wall time
  bounded at the new constant, not 1,500 s.
- Add a unit test in the runtime test suite asserting `child.max_wall_seconds <=
  INVESTIGATION_MAX_WALL_SECONDS` regardless of parent remaining wall time.
- Document the new ceiling next to the existing "four calls / 36 iterations / 36 tool calls" note
  in `docs/TOOLS.md`.

---

### P0-4. `max_search_seconds` is configured but never wired to `grep`, and `find`/`ls` have no time budget at all

**Files:** `executor/config.py`, `executor/app.py`, `executor/tools/search_text.py`, `executor/tools/find.py`, `executor/tools/ls.py`

**Verified:** `ExecutorConfig.max_search_seconds` exists (`30` default, `300` hard ceiling) and
`search_text()` accepts a `max_seconds: float = 30.0` parameter and genuinely enforces it
(`if time.monotonic() - started >= max_seconds: truncated = True; ...; break`). But the dispatcher
never passes it through:
```python
# executor/app.py
elif request.tool == "grep":
    requested_results = request.arguments.get("max_results")
    output, data = grep(root, request.arguments,
                         max_bytes=self.config.effective_limit("max_grep_scan_bytes"),
                         max_results=self.config.effective_limit("max_search_results", requested_results))
    # <- no max_seconds= argument passed; search_text always falls back to its 30.0 default,
    #    so per-request/per-profile overrides of max_search_seconds silently do nothing.
```
`find.py` and `ls.py` are worse: neither function has a time parameter or a `time.monotonic()`
check anywhere in the file. They are only bounded by result count (`max_results`) and, for `find`,
recursion depth (`max_depth`). A pathological directory shape (e.g. extremely wide directories
under a low `max_depth`, or a glob that matches nothing so every file must be visited before
returning "no matches") can run for the entire remaining wall-time budget with no independent cap.

**Fix:**
1. Pass `max_seconds=self.config.effective_limit("max_search_seconds")` through to `grep()` →
   `search_text()` in `executor/app.py` so profile/env overrides actually take effect.
2. Add the same bounded-loop pattern `search_text` already uses (`started = time.monotonic()`,
   check inside the walk loop, set `truncated=True`/`truncation_reason="time_budget"` and return
   partial results) to `find.py` and `ls.py`, threading a `max_seconds` parameter through from
   `app.py` using the same `max_search_seconds` config value (or a dedicated `max_walk_seconds` if
   you want `find`/`ls` to have an independently tunable budget — either is defensible, just pick
   one and document it).

**Acceptance criteria:**
- Setting `LOCAL_CHAT_MAX_SEARCH_SECONDS` to a small value (e.g. `1`) measurably truncates a
  `grep` call against a large tree within ~1s wall time (add a test with a synthetic large/slow
  tree or a monkeypatched clock).
- `find` and `ls` return `truncated: true` / a time-budget reason when they exceed the configured
  seconds, verified with a similar test.
- `docs/TOOLS.md` gains a "time budget" note for `find`/`ls` alongside the existing result-count
  documentation, matching the `grep` "Default: 500 results and a 64 MiB scan budget" pattern.

---

### P0-5. `ls` default result count silently disagrees with its own docs and with `find`

**Files:** `executor/tools/ls.py`, `docs/TOOLS.md`, `docs/LIMITS.md`

**Verified:**
```python
# executor/tools/ls.py
requested = arguments.get("max_results", 200)   # <- default 200 when caller omits max_results
```
```python
# executor/tools/find.py
requested = arguments.get("max_results", 500)   # <- default 500
```
```
docs/TOOLS.md:91:- **Default:** 500 results in `coding`.   (this is the `ls` section)
docs/LIMITS.md:18:| `ls` results | 500 | 2,000 | ...
```
The *config-level* cap (`max_ls_results = 500`) matches the docs, but the *tool's own argument
default* (used whenever a model call omits `max_results`) is 200, not 500 — so an agent that
doesn't explicitly pass `max_results` silently gets fewer results than the documentation promises,
with no truncation signal explaining why (the response will just look "complete" at 200 items,
even in a directory with 350 visible entries).

**Fix:** Change `ls.py`'s default from `200` to `500` to match `find.py` and the documented
default, **or** change the docs/config to `200` if `200` was the intended smaller default for
`ls` specifically — pick one canonical value and make code and docs agree. Given `find` already
uses 500 and the docs already say 500, changing the code default in `ls.py` is the lower-risk fix.

**Acceptance criteria:**
- `ls` with no `max_results` argument returns up to 500 entries (or whatever value you finalize),
  matching `docs/TOOLS.md`/`docs/LIMITS.md` exactly.
- Add/extend `tests/test_documentation_contract.py` (already exists per the doc-drift task below)
  to assert this default against the doc-stated value so it can't silently re-drift.

---

### P0-6. `grep`'s scan-size budget and output-clip budget share one config value

**Files:** `executor/tools/grep.py`, `executor/tools/search_text.py`, `executor/config.py`

**Verified:**
```python
# executor/tools/grep.py
def grep(root, arguments, *, max_bytes, max_results=500):
    output, data = search_text(root, arguments, max_bytes=max_bytes, max_results=max_results)
    encoded = output.encode("utf-8")
    if len(encoded) <= max_bytes:      # <- output is clipped against the SAME max_bytes
        return output, data
    ...
```
```python
# executor/tools/search_text.py
if size > max_bytes:      # <- per-file size is also skipped against the SAME max_bytes
    skipped_large += 1; continue
```
`max_bytes` (backed by `max_grep_scan_bytes`, 64 MB default) does two unrelated jobs: "skip a
candidate file if it's bigger than this" and "clip the final combined output string if it's bigger
than this." At the current default (64 MB) this rarely bites in practice, since per-match text is
already separately clipped to 500 characters and result *count* is bounded by `max_results` — but
it is still an architectural conflation with no independent `max_output_bytes`, and if anyone ever
lowers `max_grep_scan_bytes` in a future profile (e.g. a `small` profile tuned tighter than today's
16 MB), the two unrelated behaviors would change together in a way that's hard to reason about.

**Fix:** Add a distinct config field (e.g. `max_grep_output_bytes`, small, e.g. 500 KB–1 MB) and
pass it separately into `grep.py`'s output-clip step, leaving `max_grep_scan_bytes` solely
responsible for the per-file skip-if-too-large decision in `search_text.py`.

**Acceptance criteria:** lowering `max_grep_scan_bytes` alone no longer changes the output-clip
threshold in a test that asserts the two are independent.

---

### P0-7. The bare `"env"` staging-exclusion entry matches any real directory named `env/`

**File:** `executor/paths.py`

**Verified:**
```python
STAGING_EXCLUDED_PARTS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".tox", ".nox",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
})
def is_staging_excluded(value: str) -> bool:
    return any(part.casefold() in STAGING_EXCLUDED_PARTS for part in PurePosixPath(value).parts)
```
`is_staging_excluded` matches on **any path segment equal to `env`, anywhere in the tree,
case-insensitively** — not "a virtualenv-shaped directory at the repo root." A real, load-bearing
directory literally named `env/` (config-as-code, environment-loader patterns, ML experiment
configs) is silently dropped from staging, hidden from `find`/`grep`/`ls`, and excluded from the
publish manifest — with no error. This is the single highest-confidence, zero-tradeoff bug in
either source document: it has no offsetting security or performance benefit.

**Fix:** Either (a) drop the bare `"env"` entry and keep only `.venv`/`venv` (much less likely to
collide with a real source directory name), or (b) make the match require virtualenv-shaped
contents (e.g. a `pyvenv.cfg` file, or `bin/activate`/`Scripts/activate` sibling) rather than
matching on the bare directory name. Prefer (a) for simplicity unless there's a known real-world
case that needs (b)'s precision.

**Acceptance criteria:**
- A repository containing a top-level `env/` directory with ordinary source files stages,
  publishes, and appears in `find`/`grep`/`ls` output like any other directory.
- `.venv`/`venv` directories (and, if you keep bare-`env` detection via heuristic, a real
  virtualenv named `env/`) are still excluded.
- Add a regression case to `tests/test_staging.py` (per the weaknesses doc, this file "already has
  the scaffolding" for this) covering both a real `env/` source directory and a real virtualenv
  directory to confirm the fix discriminates correctly.

---

### P0-8. `read` doesn't apply the same staging-exclusion check as `find`/`grep`/`ls`

**File:** `executor/tools/read.py`, `executor/paths.py`

**Verified:** `read()` only calls `safe_path(root, relative, must_exist=True)`, which enforces
`is_secret_path` (blocks `.env`, `.ssh`, etc.) via `normalize_relative`, but never calls
`is_tool_excluded`/`is_staging_excluded` the way `find.py`/`search_text.py`/`ls.py` all do. In
**Plan mode**, `root = source_root` — the real, unfiltered host mount (not the filtered staging
copy) — so a model that already knows or guesses an exact path such as `.git/HEAD` could `read()`
it directly, even though it would never appear in any `find`/`grep`/`ls` listing. In practice this
is low-severity today (most of `.git`'s internals are zlib-compressed binary and fail the UTF-8
decode `read` enforces, and `bash` isn't available in Plan mode to make use of it), but it's a
real, verifiable inconsistency in how one exclusion set is applied across tools, and it will
become more exploitable if any future non-`.git` excluded directory (e.g. a build cache) ever
contains plausible UTF-8 text worth reading.

**Fix:** Make `read()` call `is_tool_excluded(relative)` (raising the same "not found"-style error
`find`/`grep`/`ls` produce for excluded paths) before it opens the file, for consistency across the
whole read-oriented tool surface.

**Acceptance criteria:** `read()` on any path under a `STAGING_EXCLUDED_PARTS` directory (in either
mode) fails the same way `find`/`grep`/`ls` already treat it — "not visible" — rather than
succeeding when the underlying bytes happen to be valid UTF-8.

---

## 3. Tier P1 — high-cost / high-friction gaps (real, worth doing soon)

### P1-1. Native staged status/diff tool (no raw Git exposure)

**Status: implemented** — read-only Agent tool `status` (`executor/tools/status.py`); see `docs/TOOLS.md`.

**Files (new/changed):** a new `executor/tools/status.py` (or extend `checkpoints.inspect_checkpoint`
into a model-facing operation), `executor/app.py` dispatcher, `server/openrouter/agent.py` schema,
`shared/tools.py`, `docs/TOOLS.md`

**Context:** `.git` is deliberately excluded from staging (`STAGING_EXCLUDED_PARTS` includes
`.git`; `seed_staging` never copies it), so the agent has no `git status`/`git diff`/`git log`.
The maintainer's own `archived-doc/harness-qol-plan.md` item 6 already specifies the right-shaped fix —
**harness-native status/diff independent of Git** — and the primitives already exist:
`executor/staging.py::workspace_changes()` already computes created/modified/deleted/
permission-changed paths against the snapshot baseline, `app.py::workspace_status()` already
surfaces a summarized version of this after every mutation, and
`executor/checkpoints.py::inspect_checkpoint()` already supports bounded, byte/line-limited textual
diffs (`_checkpoint_diffs`) against a *checkpoint* baseline. **Do not** add a real Git working
tree or `git_status`/`git_diff`/`git_log` tools — that reopens the large, stateful, history-bearing
directory the staging model exists to avoid, and makes mutations commit-capable, a materially
bigger authority grant than "edit files in a staging copy."

**Fix:** Add a model-facing `status` (and/or `diff`) tool, or extend `workspace_status` with an
optional bounded diff, built directly on `workspace_changes()` + `bounded_diff()` (already used for
edit/write results) comparing the **staging snapshot** against the **source baseline** (not just
against the last checkpoint) — i.e. "everything I've changed so far this session, ready to review
before publish." Bound it the same way `bounded_diff` already bounds edit diffs
(`MAX_DIFF_LINES`/`MAX_DIFF_BYTES`), and exclude secret/staging-metadata/checkpoint content the
same way `workspace_changes`/`visible_files` already do.

**Acceptance criteria (from `archived-doc/harness-qol-plan.md` item 6, already written):**
- Status is available for an empty and non-empty staging area.
- Diffs are bounded by bytes and lines.
- Secret, staging metadata, and checkpoint content are excluded.
- Output includes enough metadata to review a pending publication safely.
- New tool is added consistently everywhere `AGENTS.md`'s change-discipline rule requires:
  `shared/tools.py`, `server/openrouter/agent.py` schema, executor dispatch/permissions
  (`shared/permissions.py` — likely a `READ_TOOLS` entry, since status/diff shouldn't require
  mutation-level approval), tests, and `docs/TOOLS.md`/`docs/LIMITS.md`.

*(Optional, smaller follow-on, only if there's real demand: a narrow, read-only `git log`/
`git blame` against the **host** repository, not staging, exposed as its own small tool rather than
raw `git` in `bash`.)*

---

### P1-2. Structured multi-file patch operation + advisory shrink guard

**Status: implemented** — mutation tool `apply_patch` (`executor/tools/apply_patch.py`; first shipped as `patch`, then renamed to `apply_patch`, with `patch` now on the regression suite's removed-name list and no alias), advisory shrink guard for confirmed changes, and `edit` newline policy/diagnostics.

**Files:** new `executor/tools/patch.py` (or similar), `executor/mutations.py`, `executor/app.py`,
`server/openrouter/agent.py`, `shared/tools.py`, `docs/TOOLS.md`

**Context:** `executor/tools/edit.py` requires literal `old_str`/`new_str` matching with an
`expected_occurrences` count, and there is no multi-file patch/apply tool — a routine "update 4
files in one logical change" task currently requires 4+ independent `edit`/`write` calls, each its
own checkpoint/rollback cycle with no atomicity across the set. Separately,
`executor/mutations.py::guard_shrink` **hard-blocks** any whole-file edit on a file ≥200 bytes /
≥20 lines that would shrink it by more than half in both bytes and lines:
```python
if old_bytes >= 200 and old_lines >= 20 and new_bytes < old_bytes * SHRINK_RATIO and new_lines < old_lines * SHRINK_RATIO:
    raise ExecutorToolError("staging.shrink_warning", ...)
```
This is a reasonable guard against a hallucinated near-empty replacement, but it also fires on
entirely legitimate large deletions/rewrites (stripping a big dead-code block, replacing a stub
with a smaller real implementation), forcing several smaller edits purely to dodge the ratio. This
is on the maintainer's own roadmap as `archived-doc/harness-qol-plan.md` items 2 ("Improve exact-edit
diagnostics and newline handling") and 3 ("Provide an auditable structured patch operation").

**Fix:**
1. Implement `archived-doc/harness-qol-plan.md` item 3 as written: a single `apply_patch`-style tool
   taking a list of `{path, operation, old_str?, new_str?, content?}` entries, sharing the existing
   checkpoint/rollback/path-safety primitives (`create_mutation_checkpoint`/`rollback_mutation`
   from `executor/mutations.py`) so one failure across the batch rolls back the whole set
   atomically. Do not add an alias for the existing `edit`/`write` tools — this is additive.
2. Make `guard_shrink` **advisory rather than hard-blocking** when the match was already confirmed
   exact and intentional via `expected_occurrences`/hash checks (which happen first) — e.g. return
   a `shrink_warning: true` flag in the result data instead of raising, once occurrence/hash
   validation has already proven the replacement was deliberate and specific rather than an
   accidental near-total-wipe.
3. Address `archived-doc/harness-qol-plan.md` item 2 alongside this: make exact-edit matching robust to
   CRLF/LF differences per an explicit documented policy, and on a failed match report bounded
   diagnostics (zero matches vs. too many matches vs. hash conflict vs. malformed context, with a
   short escaped context preview) instead of a bare failure.

**Acceptance criteria (from the QoL plan, already written for item 3):**
- Patch operations are represented in structured request data and included in mutation results.
- Every changed path is normalized, validated, and included in the checkpoint.
- Partial patch application rolls back on failure, cancellation, or timeout.
- The operation does not create an alias or legacy public tool vocabulary.
- The public tool registry, permissions, documentation, and tests are updated together.
- For the shrink-guard change: a legitimate large deletion (confirmed by exact
  `old_str`/`new_str`/`expected_occurrences` match) succeeds with a warning flag instead of being
  rejected; an *unconfirmed* wholesale-replacement `write` over an existing large file still warns
  or blocks as today.

---

### P1-3. Publication ceiling blocks legitimate multi-file codemods

**Status: implemented** — ordered, separately approved, all-or-nothing batches with a durable ledger and content-free baseline receipts; see `docs/PUBLICATION.md`.

**File:** `src/workspace_broker.py`

**Verified:**
```python
MAX_PUBLISH_FILES = 500
MAX_PUBLISH_BYTES = 32_000_000
...
if set(paths) != {normalize_relative(item) for item in approved_paths}:
    raise BrokerError("Approved paths do not exactly match the manifest")
```
A single publication is capped at 500 files / 32 MB, with an **exact-match** requirement between
the manifest's paths and the approved-paths set, and (per the critique doc, consistent with this
code's "stale baseline" checks elsewhere) a stale-snapshot reject if staging has moved on since
approval was requested. A genuine repo-wide codemod (e.g. a rename or lint-fix touching 600+ files)
cannot be published in one shot today, with no batching path.

**Fix:** Add a batched multi-manifest publish: split a large changeset into ordered batches under
the existing per-batch 500-file/32 MB ceiling, with per-batch approval, per-batch hash validation,
and a defined recovery/resume story if a later batch fails after earlier batches already
published (e.g. persist which batches succeeded so a retry only re-attempts the remainder, and
surface that state to the human approver). Keep the existing single-batch ceiling and exact-match
semantics unchanged for one batch — this is additive orchestration on top, not a loosening of the
per-batch limits.

**Acceptance criteria:**
- A staged changeset with, say, 1,200 changed files across two batches publishes successfully with
  two sequential approvals, and a mid-sequence failure leaves a resumable, clearly reported state
  rather than an ambiguous partially-published workspace.
- Existing single-batch behavior and limits are unchanged and covered by existing tests.

---

### P1-4. Checkpoint/status cost is a full-tree SHA-256 hash on *every* mutating call, not just at session start

**Status: implemented (fixes 1 and 2)** — stat-signature digest cache in `visible_files()`, object-store checks without re-hashing, and post-mutation status reuses the checkpoint walk. Fix 3 (copy-on-write staging) remains future work.

**Files:** `executor/staging.py` (`visible_files`), `executor/checkpoints.py` (`create_checkpoint`),
`executor/app.py`

**Verified:**
```python
# executor/staging.py
def visible_files(root: Path) -> dict[str, tuple[str, int, int]]:
    for current, dirnames, filenames in os.walk(root, ...):
        ...
        for filename in sorted(filenames):
            ...
            result[relative] = (sha256_file(path), path.stat().st_size, stat.S_IMODE(mode))
    return result
```
`visible_files()` walks and SHA-256-hashes **every eligible file in the tree** on every call, with
no caching. It is called from **two** places on the hot path of every single mutating tool call:
1. `create_mutation_checkpoint()` → `create_checkpoint()` → `visible_files(work_root)`, invoked at
   the start of every `write`/`edit`/`bash` call (`executor/tools/write.py`, `edit.py`, `bash.py`
   each call `create_mutation_checkpoint` before doing anything).
2. `app.py`'s post-mutation `workspace_status()` → `workspace_changes()` → `visible_files()`,
   invoked again *after* every `write`/`edit`/`bash` call in Agent mode
   (`if request.mode == "agent" and request.tool in {"write", "edit", "bash"}: data["workspace_status"] = self.workspace_status()`).

So a "change 7 lines" task on a 300,000-file / 2.5 GB workspace pays a full-tree hash **twice per
mutating call**, not once per session. The content-addressed `objects/` store (already implemented
— see §1) removes the *storage* duplication cost but not this *hashing* cost, which dominates on
large repositories.

**Fix (incremental, does not require touching the security model):**
1. Maintain an in-memory (or on-disk, keyed by workspace) index of `{relative_path: (mtime_ns,
   size, sha256)}` populated once at `seed_staging()` time. On each subsequent `visible_files()`
   call, `os.walk` as today but only re-hash a file whose `(mtime_ns, size)` changed since the last
   observation; reuse the cached digest otherwise. This preserves exact correctness (mtime+size is
   already good enough for the tool's own existing trust model — checkpoints already trust
   `sha256_file` results the same way) while turning the common case (most of the tree untouched
   between one mutation and the next) into a cheap `stat()`-only walk.
2. As a smaller intermediate step if the full index is too large a change: skip calling
   `workspace_status()`'s independent `visible_files()` pass entirely and instead derive the
   post-mutation status from the *pre-mutation* checkpoint's manifest (already computed this call)
   plus the single file(s) the just-executed tool touched — avoiding the second full-tree walk
   without touching the checkpoint mechanism at all.
3. Longer-term (larger effort, sequence after 1/2): copy-on-write staging (reflink/hardlink where
   the filesystem supports it — Btrfs, XFS with reflink, most Linux filesystems the executor
   container runs on) so `seed_staging()` itself stops being an eager full copy, falling back to
   the current behavior where COW isn't available.

**Acceptance criteria:**
- A synthetic large-workspace benchmark (e.g. 50k small files) shows a `write` call's wall time
  dominated by the actual write, not by `visible_files()`, before vs. after — add a benchmark/perf
  test that fails if a single-file mutation's checkpoint step scales linearly with total repo file
  count once the cache is warm.
- Checkpoint/rollback/publish correctness is unchanged — all existing checkpoint tests still pass
  unmodified, since the caching is purely an optimization of `visible_files()`'s internals, not a
  change to what it returns.

---

### P1-5. Executor has no dependency-install or network path at all

**Status: implemented as an opt-in prototype** — `docs/EGRESS.md`, `egress/`, `docker/compose.egress.yaml`; the default profile keeps `network_mode: none`.

**Files:** `docker/compose.yaml`, `docker/Dockerfile.executor`

**Context:** `network_mode: none` on the executor container is confirmed, and the base image is
bare `python:3.12.13-slim-bookworm` with nothing beyond the pinned Python requirements — no git,
node, cargo, go, Java. `pip install`, `npm install`, `cargo fetch`, or any network-dependent test
are all impossible today, with no documented workaround. This is the most severe functional
ceiling in either source document: it rules out the majority of real-world multi-dependency
projects, not just adds friction.

**Fix (large effort — scope and prototype in parallel with the tasks above, don't block on it):**
Add a narrow, **allowlisted egress proxy** (PyPI, npm registry, and explicitly nothing else to
start) rather than open network access — the same shape as an `allowed_domains`/`blocked_domains`
model used by sandboxed tool runners generally. This preserves "the agent cannot exfiltrate your
code or call arbitrary APIs" while unblocking the single most common real-world need. Concretely:
1. Design doc first: which registries, how the proxy authenticates/rate-limits, how it's wired
   into `docker/compose.yaml` (a sidecar container the executor can reach only via the proxy,
   still with `network_mode: none` on the executor itself and only a controlled internal link to
   the proxy sidecar), and what's logged/auditable about outbound requests.
2. Prototype behind a feature flag / opt-in profile so the default posture stays fully
   network-isolated until this is validated.
3. Extend the base image (or a distinct "install-capable" profile image) with the minimum tool
   needed to *use* the proxy (pip/npm already present via Python), without adding unrelated
   toolchains (node/cargo/go/Java) unless there's a corresponding, separately-scoped need.

**Acceptance criteria:** a task requiring `pip install <allowlisted package>` succeeds inside the
executor under the new profile, while a request to an arbitrary non-allowlisted host is refused
and logged, and the default profile (no opt-in) is provably unchanged (`network_mode: none`,
no egress).

---

### P1-6. Binary/non-UTF-8 files have no path through the pipeline, even via `bash`

**Status: implemented** — `PublishOperation.content_base64`, carried byte-exact by the executor manifest and the broker.

**Files:** `executor/tools/read.py`, `executor/tools/write.py`, `shared/tools.py`
(`PublishOperation.content: str | None`), `src/workspace_broker.py`

**Verified:** `read`/`write` hard-require valid UTF-8 (raising `ValueError` on decode failure).
`PublishOperation.content` is typed `str | None` and hashed via `.encode("utf-8")`; the broker
writes to the host via `operation.content.encode("utf-8")` (confirmed unchanged at current HEAD —
still no `content_base64` variant anywhere). So it's not just that `read`/`write`/`edit` can't
touch binary files — **the publication data model itself cannot represent a binary file**, even if
something wrote one into staging via `bash`. Any repository with icons, fonts, fixture databases,
images, or generated binary artifacts has an entire class of routine changes (update an app icon,
regenerate a fixture, replace an asset) the agent cannot complete end-to-end.

**Fix:** Extend `PublishOperation` with a `content_base64: str | None` variant alongside the
existing text `content` field, gated behind an explicit allow-list of binary-safe operations
(copy/replace/delete only — no diff, no line-based validation, since none of that applies to binary
data). The broker's existing hash-validation and path-safety checks apply unchanged to the new
variant; only the content representation needs to grow a second, opt-in path. This is additive and
doesn't touch the existing text-file safety guarantees.

**Acceptance criteria:** a `bash`-written binary file (e.g. a small PNG) that appears in staging is
detected by `workspace_changes()`, included in the publish manifest via the new base64 path, and
lands on the host with byte-identical content and matching mode — with existing text-file
publication paths and their tests entirely unaffected.

---

### P1-7. Tool calls within a turn execute sequentially even when read-only and independent

**Status: implemented** — consecutive read-only calls run concurrently (`server/agent/runtime.py::tool_call_groups`); the executor serves them off its event loop.

**File:** `server/agent/runtime.py`

**Verified:**
```python
for raw_call in turn.tool_calls:
    budget.consume_tool_call()
    run_mutated = (await self._execute_tool_call(...)) or run_mutated
```
No `asyncio.gather` — a turn issuing five independent `grep`/`read`/`find`/`ls` calls runs them one
at a time, burning wall-clock budget for no benefit.

**Fix:** Parallelize strictly read-only tool calls (`read`, `grep`, `find`, `ls` — already a
first-class `READ_TOOLS` concept in the permission system) within a turn via `asyncio.gather`,
while keeping mutating calls (`write`, `edit`, `bash`) serialized in their original order to
preserve checkpoint-per-mutation ordering semantics. A mixed batch (some read, some mutating) can
run its read-only calls concurrently and its mutating calls serially, preserving overall
tool-call order in the returned messages so the model still sees results in a predictable sequence.

**Acceptance criteria:** a turn with N independent read-only calls completes in roughly
`max(latencies)` rather than `sum(latencies)`, verified with a test using artificially delayed
stub tools; a turn mixing reads and mutations still applies mutations in their original
call-order and preserves per-call checkpoint semantics.

---

## 4. Tier P2 — structural & quality-of-life (lower urgency, real value)

### P2-1. README states a stale investigation call limit ("two"), contradicting AGENTS.md/docs/TOOLS.md and the code

**Files:** `README.md`, `AGENTS.md`, `docs/TOOLS.md`, `docs/LIMITS.md`, `executor` code
(`INVESTIGATION_HARD_CALL_CEILING = 4`)

**Verified:** `README.md` line 37 still says *"capped at two calls per turn"*; `AGENTS.md` and
`docs/TOOLS.md` both say *"four calls per turn"*; the code constant is `4`. This is a direct,
checkable doc/code drift on a branch whose entire purpose was quality hardening.

**Fix:** One-line fix to `README.md` (two → four). More durably: add (or extend, since
`tests/test_documentation_contract.py` already exists and is exactly the right place) a CI check
that greps the call-limit constant out of `server/agent/runtime.py` and fails the build if any of
`README.md`/`AGENTS.md`/`docs/TOOLS.md`/`docs/LIMITS.md` disagree with it or each other. Extend the
same contract test to cover the `ls`/`find` default-count drift fixed in P0-5, so doc/code drift on
*any* documented numeric constant is caught going forward, not just this one instance.

**Acceptance criteria:** `README.md` matches the code; `tests/test_documentation_contract.py`
fails if any of the four files disagree on this constant (or the `ls`/`find` defaults) again.

---

### P2-2. `investigate_repository` starts cold and returns unstructured prose

**File:** `server/openrouter/agent.py` (tool schema), `server/agent/runtime.py` (`_investigate_repository`)

**Context:** The tool schema takes only `query: string` — no path hints, no visibility into what
the parent has already explored — and the subagent returns free prose the parent must trust rather
than inspect. `archived-doc/harness-qol-plan.md` item 7 already calls for a configurable investigation
budget (partially addressed by the existing `investigation_call_budget` request field, capped at
`INVESTIGATION_HARD_CALL_CEILING`) but doesn't yet address the cold-start/prose-trust gap.

**Fix:** Keep the feature (the four-call-per-turn — once P0-2 is fixed — budget-management idea is
sound) but improve its interface:
1. Let the parent optionally pass a short structured hint (files/directories already inspected) so
   the subagent doesn't re-walk them.
2. Have the subagent return a structured findings list (`{file, line, justification}` entries)
   rather than free prose, so the parent can decide whether to trust a claim or verify it directly
   with one more `read` call, rather than accepting an unverifiable summary.

**Acceptance criteria:** an investigation call given a hint list demonstrably skips re-reading
those paths (verifiable via tool-call logs in a test), and its result is a structured list the
parent can programmatically inspect rather than only a prose string.

---

### P2-3. Approval policy is a binary switch (`prompt` vs `auto`) with no risk-scoped middle tier

**File:** `shared/permissions.py`

**Verified:**
```python
return PermissionDecision.ALLOW_RUN if request.tool in READ_TOOLS else PermissionDecision.ASK
```
Under the default `"prompt"` policy every `write`/`edit`/`bash` call is an `ASK` unless a matching
rule exists. The existing mitigation (per-run "allow for this run" rules and persisted
"allow rule" entries in `permission_rules`, both already implemented in
`server/agent/runtime.py`/`server/journal/store.py`) makes this closer to "confirm once per new
kind of action" than "confirm every edit forever" — but the *policy* itself only has two positions.

**Fix:** Add a middle tier that auto-allows `write`/`edit` within staging (reversible, and already
gated behind publication approval regardless) while still prompting for `bash` (broader,
harder-to-preview effects). This matches risk to friction better than the current two-position
switch without touching the existing rule-persistence mechanics, which already work well.

**Acceptance criteria:** the new policy tier allows `write`/`edit` without a prompt while still
requiring approval for `bash`, and existing `prompt`/`auto` behavior is unchanged for
backward-compatible configs.

---

### P2-4. `DesktopBridge` is a single 2,000+ line class doing five jobs

**File:** `src/qml_backend.py` (currently 2,008 lines, `class DesktopBridge(QObject)`)

**Fix:** Split into conversation / generation / settings / runtime / staging-publication services,
keeping `DesktopBridge` as a thin QML adapter that composes them. **Do not** split
`runtime.py`/`checkpoints.py` by raw line count — those are cohesive by responsibility and
splitting them for size alone would hurt readability rather than help it. Do this one slice at a
time (e.g. extract staging-publication first, since it's the most self-contained), with tests
passing after each slice, rather than as one large rewrite.

**Acceptance criteria:** `DesktopBridge` becomes materially smaller and delegates to named service
objects with their own tests; QML-facing signatures/behavior are unchanged (verify via existing UI
tests / `scripts/qml_smoke.py`).

---

### P2-5. Root-level doc/plan sprawl and naming drift

**Status: implemented** — every living plan and the roadmap now live in `docs/` (`EUESTO_HARNESS_FIX_PLAN.md` → `archived-doc/harness-fix-plan.md`, `PROJECT_PLAN.md` → `docs/ROADMAP.md`) and are indexed in `docs/README.md` (Plans). `Euesto QoL Plan.md` was reconciled item by item (each is implemented, deliberately reverted, or continues as P2-7) and archived as `archived-doc/improvement-plan.md` with a disposition table; `Coding Harness Fixes_9_6_2026.txt` (the bash-rollback write-up, implemented per §1) was removed. The repository root keeps only `README.md`, `AGENTS.md`, and `CHANGELOG.md`, and `tests/structural/test_documentation_layout.py` enforces the root allowlist, `UPPER_SNAKE_CASE.md`/lowercase-hyphen naming with no spaces or dates, and that no document links to a removed location.

**Files (verified present at repo root):** `Coding Harness Fixes_9_6_2026.txt` (space + date in
filename), `Euesto QoL Plan.md` (≈19 KB, duplicating `archived-doc/harness-qol-plan.md`), plus the
already-existing `docs/` tree (`HARNESS_QOL_PLAN.md`, `HARNESS_VALIDATION_PLAN.md`,
`ORGANIZATION_PLAN.md`, etc.) and a single `archived-doc/` directory.

**Fix:** Consolidate root-level planning documents into `docs/`, delete or explicitly mark
superseded duplicates (`Euesto QoL Plan.md` vs `archived-doc/harness-qol-plan.md` — reconcile which is
authoritative and remove the other, don't maintain two copies of the same plan). Normalize file
naming going forward (no embedded spaces/dates in tracked planning docs — put dates in commit
history or a changelog entry instead).

**Acceptance criteria:** one authoritative location for each living plan document; no duplicate
plan content between root and `docs/`.

---

### P2-6. Test taxonomy migration left real coverage gaps

**Status: implemented** — `tests/unit/executor/test_bash.py` restores all 14 original assertions (the loss was an accidental truncation in commit `9b604c2`, not a deliberate trim) and absorbs the duplicate `test_bash_regressions.py`; it adds `rollback_on_failure` coverage (default rollback, opt-out retaining partial progress, opt-out never bypassing timeout or cancellation rollback, argument validation). Writing those tests exposed that an explicitly cancelled command with `rollback_on_failure: false` kept its changes; cancellation now always rolls back and reports `rollback_reason: "cancelled"`. Commands also get `/dev/null` stdin instead of inheriting the executor's, so TTY rejection holds regardless of how the service was started (tested with a pseudo-terminal). The QML transcript tests are restored as `tests/ui/test_transcript_qml.py` (marked `slow`, synchronized on observable conditions) and `archived-doc/organization-plan.md` §3a records the resolution.

**Files:** `archived-doc/organization-plan.md`, `tests/unit/executor/test_bash.py`, (deleted)
`tests/test_transcript_qml.py`

**Verified:** `archived-doc/organization-plan.md` itself documents that `tests/test_bash.py` (14 tests:
shell syntax, workspace-traversal rejection, timeout rollback, cancellation, restricted env vars,
TTY rejection, large-output truncation, event-retention bounds) was replaced by
`tests/unit/executor/test_bash.py`, which currently contains **3** tests (confirmed by counting
`def test_` in the file at current HEAD) — the other 11 do not appear relocated elsewhere.
`tests/test_transcript_qml.py` (257 lines of QML `Transcript` rendering tests via
`QQmlApplicationEngine`) was deleted with no replacement.

**Fix:** Per `archived-doc/organization-plan.md`'s own recommendation: restore the missing bash-tool
assertions into `tests/unit/executor/test_bash.py` (or a sibling file in the same directory) —
this is especially important now given the `rollback_on_failure` behavior added in the already-done
bash-rollback fix (§1) needs its own coverage alongside whatever the original 14 tests covered.
Either restore `tests/test_transcript_qml.py` under `tests/ui/` or confirm equivalent coverage
exists elsewhere and update the organization plan to say so explicitly. Keep marker-based CI
selection (no reintroducing file allowlists).

**Acceptance criteria:** `tests/unit/executor/test_bash.py` covers timeout rollback, cancellation,
restricted env vars, TTY rejection, large-output truncation, event-retention bounds, and the new
`rollback_on_failure` opt-out; QML transcript rendering has either restored coverage or a
documented, deliberate decision not to.

---

### P2-7. Error classification is substring matching on message text, not typed at the throw site

**File:** `executor/errors.py`

**Verified:** `classify_error()` still does string-content checks like
`if "shrink" in lowered`, `"utf-8" in str(exc).casefold()`, `"working directory" in lowered`,
`"exceeds" in lowered or "too large" in lowered or "limit" in lowered`, etc., to guess an error
code from a caught exception's message. This is fragile — any wording change in a raised message
anywhere in the executor silently changes (or breaks) the reported `error_code` for that failure
without a test necessarily catching it, since the two are coupled only by string content, not by
type.

**Fix:** Push `ExecutorToolError(code=...)` construction to each throw site instead of inferring
it centrally from message text. `guard_shrink` already does this correctly (raises
`ExecutorToolError("staging.shrink_warning", ...)` directly) — extend that pattern to every other
raise site currently relying on `classify_error`'s substring inference, and shrink
`classify_error` down to only the genuinely generic fallback cases (bare `PermissionError`,
`TimeoutError`, unclassified `OSError`) it can't reasonably know about otherwise.

**Acceptance criteria:** changing a raised error message's wording anywhere in the executor no
longer changes its reported `error_code`, verified by a test that checks code stability
independent of message text for each error path.

---

## 5. Tier P3 — legitimate, but product-scope decisions (roadmap, not a punch list)

These are real and correctly identified in both source documents, but each is an architecture/scope
decision rather than a bug fix. Sequence them on their own timeline, informed by user demand,
rather than treating them as backlog debt:

- **OpenRouter-only, no local models / direct provider access** (`server/openrouter/client.py`
  hardcodes `https://openrouter.ai/api/v1/chat/completions` as the only endpoint). If pursued, add
  a second provider adapter behind the same `AgentTurn`/tool-schema interface
  `server/openrouter/agent.py` already defines, rather than a generic multi-provider abstraction up
  front.
- **Windows + Docker Desktop requirement.** The README's Windows requirement is accurate; note for
  the record that "Windows Home can't run Docker Desktop" is **factually outdated** (WSL2 backend
  has supported Home since 2020) — the real adoption friction is Docker's *licensing* for large
  organizations, not Windows edition. If cross-platform support becomes a goal, Linux-native
  support is the lowest-incremental-risk next step, since the sandboxing logic is already
  Linux-container-based; only the desktop UI and credential-store integration are Windows-specific.
- **Hardcoded 90s non-streaming provider timeout** (`server/openrouter/agent.py`:
  `httpx.AsyncClient(timeout=90, ...)`, `"stream": False`). Move to streaming responses (OpenRouter
  supports this) so a long turn produces progress rather than a binary timeout; make the timeout
  adaptive/configurable per model or profile.
- **No symbol/reference navigation** (agent does everything via `grep`/`find`/`ls`). If pursued, a
  lightweight `ripgrep`/`ctags`-style index built once at staging time, exposed as one or two
  additional tools (`symbols(path)`, `references(symbol)`) — not full LSP integration, consistent
  with the project's minimalist tool-surface philosophy.
- **$2.00 default coding budget is tight for frontier models on large tasks**
  (`server/agent/budgets.py`). Rather than just raising the default, add a graceful wind-down path:
  when a run is about to exhaust budget mid-task, summarize staged progress and offer to continue
  in a fresh run with a new budget, instead of simply failing with `run.failed`.
- **Head+tail truncation rather than structured reduction for huge tool output.** If pursued, a
  lightweight structured-extraction layer for the highest-value cases specifically (test runner
  output, common compiler/linter error formats) — pull `{file, line, message}` tuples out before
  truncating — rather than a general-purpose log-parsing subsystem.
- **No agent-efficacy benchmark suite.** A small, fixed task suite (5–10 scenarios: fix a failing
  test, add a feature across N files, resolve a documented bug) run end-to-end against the actual
  harness, tracked over time on success rate / tool calls / rollback frequency. This is a
  measurement gap, not a code fix, but it's the right way to validate whether the P0/P1 fixes above
  actually move the needle — sequence it as a companion to those fixes, ideally starting once P0-1
  through P0-8 land, so it can measure their effect.

---

## 6. Suggested execution order

1. **P0-1 through P0-8** — small, independently testable, unblock large writes / search / find /
   ls / investigation / staging-visibility, and fix the one zero-tradeoff data-loss bug (`env`).
   Do these first and in any order; they don't depend on each other.
2. **P2-1** (README/doc drift + CI doc-contract check) — trivial, and gives you a safety net
   (the extended `tests/test_documentation_contract.py`) that several later tasks touch anyway.
3. **P1-1** (native status/diff) and **P1-2** (structured patch + advisory shrink guard) — both
   already scoped in the repo's own `archived-doc/harness-qol-plan.md`; biggest agent-UX win without
   expanding authority.
4. **P1-4** (checkpoint/status hashing cost) — do this before or alongside P1-1/P1-2, since a new
   status/diff tool and a new patch tool will both call into the same `visible_files()`/checkpoint
   machinery you're optimizing here; better to land the perf fix first so the new tools inherit it.
5. **P1-3** (batched publish) and **P1-6** (binary file support) — round out the mutation/publish
   path once status/diff/patch exist to exercise it.
6. **P1-5** (allowlisted egress proxy) — start design work in parallel with steps 1–5 rather than
   after; it's the highest-effort, highest-leverage item and shouldn't block on everything else.
7. **P1-7** (parallelize read-only tool calls) and **P2-2/P2-3** — lower-risk UX/perf polish, any
   order, as capacity allows.
8. **P2-4 through P2-7** — structural cleanup (DesktopBridge split, doc consolidation, test
   taxonomy restoration, typed error classification), one slice at a time, opportunistically.
9. **P3 items** — deliberate roadmap/scope decisions, revisited on their own timeline. Stand up the
   benchmark suite (last P3 bullet) once a meaningful batch of P0/P1 fixes has landed, so it has
   something to measure.

---

## 7. Definition of done (applies to every task above)

- `scripts/validate.py` runs clean.
- No provider credentials required for anything in the executor/staging/publication path.
- Container checks still verify: non-root execution, blocked egress (except the explicitly
  allowlisted proxy path from P1-5, if/when implemented), read-only source mount, staging-recovery
  correctness, and tool-mode boundaries (Plan vs. Agent).
- Docs, schemas, and tests are updated **together** in the same change, per `AGENTS.md` →
  "Change discipline" — not as a follow-up.
- Any new or changed public tool appears consistently in `shared/tools.py`,
  `server/openrouter/agent.py`, executor dispatch/permissions, tests, and
  `docs/TOOLS.md`/`docs/LIMITS.md`.
