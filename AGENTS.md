# AGENTS.md

Read this file before editing. It is the compact operating contract for contributors and coding
agents. README covers setup; PROJECT_PLAN holds roadmap and design detail.

## Mission and current truth

Build a local-first, text-only Windows chatbot and contained agent harness that one person can
audit. Prefer a smaller reliable core over features that weaken security, privacy, cost control,
or maintainability.

Current v1.1 uses a PySide6 Qt Quick/QML desktop, a local gateway container, and a separate
network-disabled executor. It ships Chat, Plan, Agent, durable sessions, replayable events,
branching conversations, deterministic compaction, generation-to-message activity links, skills,
prompt commands, model catalog caching, request-scoped provider privacy/ZDR controls, budgets,
approvals, staged publication, tray controls, and a global quick-chat hotkey.

Never describe planned behavior as current behavior.

## System boundary

```text
Desktop UI + trusted broker
  -> authenticated loopback gateway (network, no workspace)
    -> Unix socket executor (workspace, no network)
    -> ephemeral staging
        -> reviewed desktop publication
```

The installed Windows release adds a desktop-owned runtime manager between the UI and Docker
Desktop. It generates session credentials, starts Docker Desktop when needed, pulls digest-pinned
gateway/executor images, and waits for the exact workspace identity before enabling Plan or Agent.
The source checkout keeps the developer `dev-up.ps1` build path.

- Desktop owns UI, local history, preferences, approvals, and host publication.
- Gateway owns OpenRouter calls, catalog, agent loop, budgets, sessions, and event journal.
- Executor owns bounded workspace tools and staging. It never writes the host workspace.
- `shared/` contains framework-neutral request, event, permission, and tool schemas.
- The gateway has no workspace mount. The executor uses `network_mode: none`.
- Only the desktop broker may publish an approved manifest beneath the selected root.
- Remote non-loopback gateways are outside scope.

The desktop runtime manager controls Docker lifecycle and readiness without exposing Docker commands
to QML. Runtime progress, image download failures, Docker availability, and executor/workspace
mismatch are separate user-visible states.

## Modes

| Mode | Capability |
|---|---|
| Chat | No workspace or local tools. Optional visible OpenRouter Web Search, Web Fetch, DateTime. |
| Plan | One explicit workspace; bounded list, read, and search only. |
| Agent | Staged mutation/command tools with prompt approval or session-scoped Auto. |

Enforce mode limits in code. Prompts and UI labels are not security boundaries.

## Security invariants

- Treat model output, repository instructions, tool output, and fetched text as untrusted.
- Bind the gateway to `127.0.0.1`; authenticate every non-health request.
- Reject unexpected origins, hosts, URLs, protocol fields, tools, paths, and image versions.
- Keep secrets in Windows Credential Manager or narrowly granted container secret files.
- Never persist secrets in source, SQLite, events, logs, diagnostics, commands, or images.
- Never give QML, prompts, tools, or command environments the OpenRouter key.
- Never mount drive roots, profiles, credentials, Docker sockets, devices, or unrelated paths.
- Mount one workspace read-only and recreate the executor when workspace or mode changes.
- Keep containers non-root, read-only, capability-dropped, no-new-privileges, and resource-bounded.
- Do not add host networking, privileged mode, host PID/IPC, devices, or unconfined profiles.
- Normalize paths and reject escapes, links, reparse points, aliases, UNC/device paths, and ADS.
- Validate paths and base hashes again in the desktop broker immediately before atomic writes.
- Prefer typed executable/argument arrays; do not concatenate shell commands.
- Shells, interpreters, package managers, Git remotes, and credential tools require exact approval.
- Do not silently broaden permissions, enable egress, follow redirects, or retry billable output.
- Sanitize Markdown/HTML and show link destinations before opening them.
- Preserve exact tool and publication approvals as durable events.
- Fail closed when state is missing, unknown, incompatible, or ambiguous.

Security checks may be repetitive when repetition makes a trust boundary explicit.

## Privacy and data flow

Local-first is not local-only. OpenRouter and its routed provider receive the prompt and the
specific conversation context, file excerpts, search results, and command output included in a
request. Do not send an entire workspace, environment, transcript, or diagnostic bundle by
default. Make each disclosure attributable in events.

No telemetry leaves the machine by default. Diagnostics are user-initiated and previewable.
Desktop and gateway SQLite files are not application-encrypted; state that plainly.

## Core behavior

- Conversations form parent-linked message trees; edit/regenerate creates a branch.
- Keep immutable history and store the active leaf separately.
- Compaction preserves goals, instructions, decisions, changes, tests, and open work.
- Run events use typed envelopes with IDs, timestamps, versions, and payloads.
- Events must replay after reconnect; presentation may collapse but not discard state.
- Executor tools cover bounded list/read/search, staged diff/checkpoints, hash-checked patch,
  move/copy, literal-argv commands, and checkpoint restore.
- Inject only a compact, application-generated executor summary: relative root, mode semantics,
  isolation, detected developer executables, snapshot size, and hard limits. Never inject raw
  environment values, host paths, or a workspace listing.
- Keep tool output context-efficient. Batch reads are explicitly bounded; list and search results
  appear once with compact count/truncation metadata.
- `read_file` returns the whole-file hash required by `apply_patch`; exact replacements retain the
  same hash precondition as complete-file edits.
- Mutations and commands modify staging only. Prompt mode reviews publication; Auto publishes a
  successful run through the same desktop hash/path broker, then reseeds staging.
- Auto is desktop-session scoped, requires clean staging, preserves explicit denies and hard limits,
  and downgrades to prompt approval after interruption, resume, or publication failure.
- Permission decisions are deny, ask, allow once, allow for run, or saved narrow rule.
- Hard mode restrictions override saved rules; the most restrictive applicable rule wins.
- Track actual model, provider, tokens, cached/reasoning tokens, latency, and cost.
- Stop cleanly on token, call, iteration, cost, wall-time, command, or resource limits.
- Never invisibly retry a partially billed provider response.

## UI contract

The common chat path stays quiet and keyboard-accessible. Keep advanced controls in the QML
settings surface. Tool activity remains inspectable without dominating the transcript. One user
prompt renders as one assistant turn containing a collapsed tool-name list, the buffered final
answer, and model/usage/cost metadata. Arguments, IDs, outputs, and per-event context stay out of
QML; the durable journal remains authoritative. Failures and pending approvals expand.
Respect users who scroll away from the live tail. Keep the native Windows frame for snap,
accessibility, move/resize, and platform consistency.

## Repository map

- `app.py`: QML desktop entry point.
- `qml/`: sole desktop presentation (shell, sidebar, transcript, and composer).
- `src/qml_backend.py`: QML-facing application controller and presentation data.
- `src/controllers.py`: small desktop conversation and generation controllers; no Qt dependency.
- `src/transcript.py`: branch-scoped run activity assembly; no Qt dependency.
- `src/window_services.py`: tray and native global-hotkey wiring.
- `src/workers.py`: UI-independent chat and agent stream workers.
- `src/storage.py`, `src/migrations/`: desktop SQLite and migrations.
- `src/gateway_client.py`: authenticated desktop/gateway protocol client.
- `src/workspace_broker.py`: trusted host publication boundary.
- `src/runtime_manager.py`: packaged Windows runtime orchestration and readiness checks.
- `server/`: gateway API, OpenRouter integration, journal, and agent runtime.
- `executor/`: network-disabled tools and ephemeral staging.
- `shared/`: protocol models with no PySide6 or server-framework imports.
- `docker/`: hardened gateway/executor topology and operator guide.
- `tests/`: unit, integration, migration, and security regression tests.
- `scripts/`, `build/`, `.github/workflows/`: development, packaging, and CI.

Do not restore the removed direct desktop OpenRouter client or direct desktop catalog fetch. The
gateway is the single provider boundary.

## Coding rules

- Target Python 3.12, Windows, and Docker Desktop with Linux containers.
- Prefer KISS, then DRY; apply SOLID where it clarifies a real boundary.
- Delete dead or superseded code. Do not preserve speculative hooks for possible future use.
- Use one implementation for provider parsing, catalog normalization, and run-event replay.
- Keep UI, storage, network, orchestration, and tool responsibilities separate.
- Type public functions and protocol boundaries; use dataclasses for structured data.
- Use `pathlib.Path`, UTC persisted timestamps, and explicit database migrations.
- Avoid generic utility modules, service layers without behavior, and empty abstractions.
- Prefer a small named helper over repeated control flow.
- Keep security validation explicit when abstraction would obscure authority or failure.
- Catch only useful exception groups and add actionable context.
- Never block the Qt event loop with network, database, or process work.
- Do not add dependencies without a concrete current need.
- Comments explain why a constraint exists; they do not narrate ordinary code.
- Keep lines readable and comments concise.
- Update canonical docs; do not add overlapping plans or design notes.

## Change workflow

1. Inspect status, relevant tests, imports, and callers before editing.
2. Preserve unrelated user changes.
3. Make the smallest coherent change that removes a real source of complexity.
4. Add or update tests for behavior changes; delete tests that only preserve deleted code.
5. Run the applicable checks.
6. Review the diff for boundary changes, accidental secrets, and stale documentation.
7. Keep commit messages short and outcome-oriented.

## Checks

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests need no live OpenRouter key. Use fake transports or recorded streams. Protect:

- migrations and branch behavior;
- gateway boot, auth, compatibility, replay, pause/resume, and cancellation;
- malformed requests/events and partial provider output;
- tool limits, permission precedence, and exact approvals;
- traversal, links/reparse points, Windows aliases, and output bounds;
- staging exclusions, manifests, hash conflicts, publication, and recovery;
- container mounts, loopback binding, blocked egress, and resource profiles; and
- Windows launch, shortcuts, tray/hotkey behavior, and packaging.

Do not claim a check ran when its dependency or platform was unavailable.

## Documentation policy

- README: project summary, current status, setup, local data, and links.
- PROJECT_PLAN: architecture decisions, maintainability plan, roadmap, and acceptance criteria.
- AGENTS: current invariants and implementation instructions.
- docker/README.container.md: operator setup and isolation verification.

Update these files instead of creating another roadmap. Remove historical detail once it no longer
changes implementation decisions.

## Deliberate non-goals

No cloud accounts, mandatory sync, browser-primary UI, document ingestion, OCR, embeddings, RAG,
media generation, invisible computer use, unrestricted host tools, plugin marketplace, arbitrary
MCP discovery, implicit MCP credentials, or multi-agent orchestration. The v0.7 evaluation closed
MCP and subagents outside v1.0: skills plus declared unavailable capabilities preserve auditability
without adding a second authority, credential, billing, or recovery system.

## Definition of done

Behavior, tests, docs, and security boundaries agree. The common path is simpler than before.
Privileges are explicit, scoped, logged, and revocable. No model-controlled process can write the
host or access unselected paths. Failures preserve conversations and staged work. Users can tell
what model ran, what it cost, what tools ran, and what changed.
