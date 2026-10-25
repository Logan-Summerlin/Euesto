# Euesto — Project Roadmap

This document is a status-oriented roadmap. It is not the authoritative architecture or tool specification. See [ARCHITECTURE.md](ARCHITECTURE.md), [TOOLS.md](TOOLS.md), [LIMITS.md](LIMITS.md), and [PUBLICATION.md](PUBLICATION.md) for current behavior. Open work items, including those carried from archived plans, are tracked below; new living plans are indexed in [README.md](README.md#plans).

## Completed

- Thirteen-tool model-facing API: the nine executor tools (`read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, `status`), scoped read-only `investigate_repository` delegation, gateway-local `update_plan`, and the opt-in documentation tools `web_search` and `fetch_url`.
- Plan/Agent capability separation with Plan mutation denial enforced in code.
- Incremental file inspection and localized editing for larger files.
- Bounded Bash execution, output, stdin, command time, process cleanup, and rollback.
- Persistent, disposable per-workspace staging and checkpointed mutations, with host-drift rebase and per-file conflict resolution.
- Desktop-only, approved, hash-validated publication.
- Workspace containment and executor/container security boundaries.
- Runtime profiles and hard ceilings for file, search, staging, checkpoint, command, and output resources.
- Agent budget profiles, approval policies, journal persistence, pause/resume, and run recovery.
- Markdown skills (global and workspace scopes) and declared-only custom capabilities.
- Configurable investigation model with a bounded, budget-debited nested loop.
- Regression, security, and integration coverage for the executor/publication boundary.
- Repository documentation rebuilt into separate user, operator, contributor, and agent-facing references.
- Agentic harness Phases 0–2 ([AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md)):
  - a gateway-owned static prompt, per-result bounds, provider retries with resumable pause, steering, narration, a live run meter, and a staged-work guard on close and workspace change;
  - cache-stable context, per-turn usage and cache accounting, planned compaction with a model-written handoff, lean results, full command output as artifacts, structured test and compiler output reduction, and output-token discipline;
  - a separate runner container with digest-pinned toolchain images, an executable `/work`, durable per-workspace staging, sandbox git history, and runner resource settings.
- Agentic harness Phases 3–5 ([AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md)):
  - long runs: streamed turns with idle and ceiling timeouts, the session plan, budget wind-down with *Continue with another budget*, and loop resilience for smaller models;
  - risk-proportional approvals: the Sandbox tier, command rules that match only simple commands, publication risk flags, partial publication from a review pane, secret redaction, declared checks, and the host-side workspace policy;
  - controlled connectivity: an approved one-shot dependency setup container, the allowlisted registry proxy with a pinned release image and audit log, the `registries` network mode for the runner, and the documentation tools.

## Active

- Keep schemas, dispatch, permissions, limits, tests, and documentation synchronized as the thirteen-tool API evolves.
- Maintain container and QML checks alongside the Python test/lint/compile checks.
- Continue release/runtime validation for Windows packaging and digest-pinned container images.
- Record a green Linux and Docker CI run for the validation remediation (`archived-doc/ci-validation-remediation-plan.md`, §7 and §12); the code and workflow invariants are already pinned by tests.

## Planned

- Improve observability and recovery UX without expanding executor authority.
- Add narrowly scoped usability improvements that preserve the current security and publication model.
- Keep resource defaults evidence-based as real workloads reveal bottlenecks.
- Turn the harness into a capable, long-running software-engineering agent without weakening the boundary: see [AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md) (Phases 0–5 implemented, 6–7 proposed, Phase 6 covering missions and background processes; it states which open items below it covers).

### Open items carried from archived plans

From [the harness fix plan](../archived-doc/harness-fix-plan.md):

- **Copy-on-write staging** (P1-4 fix 3): with persistent staging (harness plan 2.4, 2.5) the full copy happens once per workspace and later sessions copy only drifted files. Seeding with reflinks where the filesystem supports them remains open as an optimization.
- **P3 product-scope decisions**, each sequenced on its own timeline and informed by demand:
  - a second provider adapter (local models or direct provider access) behind the existing agent-turn and tool-schema interface, rather than a generic multi-provider layer;
  - Linux-native desktop support, the lowest-risk step if cross-platform support becomes a goal (sandboxing is already Linux-container based);
  - ~~streaming provider responses with a configurable, per-model or per-profile timeout instead of the agent turn's fixed 90-second non-streaming request~~: done in harness plan 3.1;
  - lightweight symbol and reference navigation (for example `symbols(path)` and `references(symbol)` from an index built at staging time), not full LSP integration;
  - ~~a graceful budget wind-down that summarizes staged progress and offers to continue in a fresh run instead of failing with `run.failed`~~: done in harness plan 3.3;
  - ~~structured reduction of large tool output for the highest-value formats (test runners, compilers, linters) before head+tail truncation~~: done in harness plan 1.6;
  - ~~a small agent-efficacy benchmark suite~~: declined; loop and prompt changes are validated by the credential-free test suite, including scripted-provider scenario tests (see [AGENTIC_HARNESS_PLAN.md](AGENTIC_HARNESS_PLAN.md)).

From [the code-simplification plan](../archived-doc/simplification-plan.md), findings that need a product or security decision:

- ~~**Saved bash rules are too broad**~~: done in harness plan 0.5. Saved Bash rules store the tokenized executable and argument prefix, a migration disabled broad legacy rules with a one-time notice, and a Bash allow rule without an executable never matches.
- ~~**The auto-policy clean-staging preflight is unreachable over HTTP**~~: done in harness plan 0.5; the check tests `approval_policy == "auto"`.
- ~~**`read` clamps to 256,000 bytes**~~: done in harness plan 0.5; `read` honors the profile's `max_read_bytes` up to the hard ceiling, with a 32,000-byte per-call default.
- ~~**Starlette/uvicorn versions drift**~~: done in harness plan 0.5; the dev lock matches the gateway image, and a structural test keeps them equal.

From [the harness validation plan](../archived-doc/harness-validation-plan.md):

- a Qt preflight check that reports whether Qt can initialize offscreen, and a dedicated Qt-enabled validation image or Compose profile;
- collection checks that fail on unmarked Docker or slow tests, and per-tier test counts in the validation output;
- JUnit result upload from CI;
- a structural test that the workflows invoke the shared validator and that documented commands match its tier names.

From [the organization plan](../archived-doc/organization-plan.md):

- **Product naming:** choose one canonical product name for user-facing text and record the legacy `LocalOpenRouterChat`/"Local OpenRouter Chat" runtime identifiers (keyring service, export marker, window title, installer and executable names) once. Renaming them needs a migration decision because the keyring service name protects stored secrets.

## Deferred

- Broader plugin/MCP discovery and credential delegation.
- Independent multi-agent orchestration (scoped read-only investigation delegation is supported; executable custom tools remain declared-only).
- Unrestricted host tools or direct host shell access.
- Provider-independent cloud synchronization.

## Non-goals

The project does not seek to become an unrestricted remote-control agent, a browser-primary application, a document/RAG platform, a media-generation suite, or a multi-agent orchestration framework. Chat remains free of local workspace tools.

## Acceptance baseline

The current baseline is considered complete only when the thirteen-tool API remains stable, Plan is read-only, Agent mutations remain staged, the executor and runner cannot publish, the executor cannot reach the network, and the runner reaches only the allowlisted proxy under the opt-in egress overlay, source mounts remain read-only, failed mutations roll back, publication remains approved and hash-validated, effective limits are internally consistent, and the documented pytest/ruff/compile/QML/container checks pass.
