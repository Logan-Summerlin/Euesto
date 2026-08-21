# Euesto — Project Roadmap

This document is a status-oriented roadmap. It is not the authoritative architecture or tool specification. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/TOOLS.md](docs/TOOLS.md), [docs/LIMITS.md](docs/LIMITS.md), and [docs/PUBLICATION.md](docs/PUBLICATION.md) for current behavior.

## Completed

- Eight-tool model-facing executor API: `read`, `write`, `edit`, `bash`, `grep`, `find`, `ls`, and scoped read-only `investigate_repository` delegation.
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

- Keep schemas, dispatch, permissions, limits, tests, and documentation synchronized as the eight-tool API evolves.
- Maintain container and QML checks alongside the Python test/lint/compile checks.
- Continue release/runtime validation for Windows packaging and digest-pinned container images.

## Planned

- Improve observability and recovery UX without expanding executor authority.
- Add narrowly scoped usability improvements that preserve the current security and publication model.
- Keep resource defaults evidence-based as real workloads reveal bottlenecks.

## Deferred

- Broader plugin/MCP discovery and credential delegation.
- Independent multi-agent orchestration (scoped read-only investigation delegation is supported; executable custom tools remain declared-only).
- Unrestricted host tools or direct host shell access.
- Provider-independent cloud synchronization.

## Non-goals

The project does not seek to become an unrestricted remote-control agent, a browser-primary application, a document/RAG platform, a media-generation suite, or a multi-agent orchestration framework. Chat remains free of local workspace tools.

## Acceptance baseline

The current baseline is considered complete only when the eight-tool API remains stable, Plan is read-only, Agent mutations remain staged, the executor cannot publish or reach the network, source mounts remain read-only, failed mutations roll back, publication remains approved and hash-validated, effective limits are internally consistent, and the documented pytest/ruff/compile/QML/container checks pass.
