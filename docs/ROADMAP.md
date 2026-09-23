# Euesto — Project Roadmap

This document is a status-oriented roadmap. It is not the authoritative architecture or tool specification. See [ARCHITECTURE.md](ARCHITECTURE.md), [TOOLS.md](TOOLS.md), [LIMITS.md](LIMITS.md), and [PUBLICATION.md](PUBLICATION.md) for current behavior. Open work items are tracked in the plans listed in [README.md](README.md#plans).

## Completed

- Ten-tool model-facing API: `read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, `status`, and scoped read-only `investigate_repository` delegation.
- Plan/Agent capability separation with Plan mutation denial enforced in code.
- Incremental file inspection and localized editing for larger files.
- Bounded Bash execution, output, stdin, command time, process cleanup, and rollback.
- Ephemeral staging and checkpointed mutations.
- Desktop-only, approved, hash-validated publication.
- Workspace containment and executor/container security boundaries.
- Runtime profiles and hard ceilings for file, search, staging, checkpoint, command, and output resources.
- Agent budget profiles, approval policies, journal persistence, pause/resume, and run recovery.
- Markdown skills (global and workspace scopes) and declared-only custom capabilities.
- Configurable investigation model with a bounded, budget-debited nested loop.
- Regression, security, and integration coverage for the executor/publication boundary.
- Repository documentation rebuilt into separate user, operator, contributor, and agent-facing references.

## Active

- Keep schemas, dispatch, permissions, limits, tests, and documentation synchronized as the ten-tool API evolves.
- Maintain container and QML checks alongside the Python test/lint/compile checks.
- Continue release/runtime validation for Windows packaging and digest-pinned container images.

## Planned

- Improve observability and recovery UX without expanding executor authority.
- Add narrowly scoped usability improvements that preserve the current security and publication model.
- Keep resource defaults evidence-based as real workloads reveal bottlenecks.

### Open items carried from archived plans

From [the harness fix plan](../archived-doc/harness-fix-plan.md):

- **Copy-on-write staging** (P1-4 fix 3): seed staging with reflinks where the filesystem supports them instead of an eager full copy, falling back to today's copy elsewhere.
- **P3 product-scope decisions**, each sequenced on its own timeline and informed by demand:
  - a second provider adapter (local models or direct provider access) behind the existing agent-turn and tool-schema interface, rather than a generic multi-provider layer;
  - Linux-native desktop support, the lowest-risk step if cross-platform support becomes a goal (sandboxing is already Linux-container based);
  - streaming provider responses with a configurable, per-model or per-profile timeout instead of the agent turn's fixed 90-second non-streaming request;
  - lightweight symbol and reference navigation (for example `symbols(path)` and `references(symbol)` from an index built at staging time), not full LSP integration;
  - a graceful budget wind-down that summarizes staged progress and offers to continue in a fresh run instead of failing with `run.failed`;
  - structured reduction of large tool output for the highest-value formats (test runners, compilers, linters) before head+tail truncation;
  - a small agent-efficacy benchmark suite tracked over time (success rate, tool calls, rollback frequency).

From [the code-simplification plan](../archived-doc/simplification-plan.md), findings that need a product or security decision:

- **Saved bash rules are too broad:** `JournalStore.rule_for_request` keys on `executable`/`arguments` fields no tool sends, so one saved bash rule allows every bash command in that workspace and mode; the per-run rule tokenizes `command` correctly. Unifying them tightens existing saved rules.
- **`rule_used` is never invoked,** so `PermissionRule.last_used_at` is never set.
- **The auto-policy clean-staging preflight is unreachable over HTTP,** because `AgentRunRequest.from_dict` always fills `investigation_model_id`.
- **Undeclared tool arguments:** `find`/`ls` accept `cursor` and `read` accepts `offset`, but the model-facing schemas do not declare them.
- **`read` clamps to 256,000 bytes** regardless of a larger configured `max_read_bytes`.
- **Starlette/uvicorn versions drift** between `requirements-dev.lock` (0.45.3/0.34.0) and the gateway image (0.52.1).
- **Staged rewrites drop file modes:** `write`, `edit`, and `apply_patch` replace files with mode `0600`, so status reports a permission change on every rewrite and publication applies that mode on POSIX hosts.
- **Three usage formatters** produce different UI strings.

From [the organization plan](../archived-doc/organization-plan.md):

- **Product naming:** choose one canonical product name for user-facing text and record the legacy `LocalOpenRouterChat`/"Local OpenRouter Chat" runtime identifiers (keyring service, export marker, window title, installer and executable names) once. Renaming them needs a migration decision because the keyring service name protects stored secrets.
- **Flat test relocation:** move the remaining flat `tests/*.py` files into `tests/unit/<domain>/` (or `integration/`/`security/` once those tiers exist) as they are touched, without dropping assertions.

## Deferred

- Broader plugin/MCP discovery and credential delegation.
- Independent multi-agent orchestration (scoped read-only investigation delegation is supported; executable custom tools remain declared-only).
- Unrestricted host tools or direct host shell access.
- Provider-independent cloud synchronization.

## Non-goals

The project does not seek to become an unrestricted remote-control agent, a browser-primary application, a document/RAG platform, a media-generation suite, or a multi-agent orchestration framework. Chat remains free of local workspace tools.

## Acceptance baseline

The current baseline is considered complete only when the ten-tool API remains stable, Plan is read-only, Agent mutations remain staged, the executor cannot publish or reach the network (outside the opt-in allowlisted-egress overlay), source mounts remain read-only, failed mutations roll back, publication remains approved and hash-validated, effective limits are internally consistent, and the documented pytest/ruff/compile/QML/container checks pass.
