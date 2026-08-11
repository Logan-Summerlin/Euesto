# Local OpenRouter Chat — Project Plan

This is the canonical design and roadmap. It records decisions that still affect implementation;
historical release narration and detailed coding rules belong nowhere else. Read AGENTS.md first.

## 1. Status and product scope

v1.1 is a Windows-oriented PySide6 Qt Quick/QML desktop chatbot backed by a local Docker gateway and a separate
network-disabled executor. It supports:

- local branching conversations, search, import/export, usage, model controls, and prompt presets;
- Chat, read-only Plan, and Agent with prompt approval or session-scoped Auto;
- OpenRouter streaming and model catalog access through the gateway only;
- durable run events, internal agent sessions, pause/resume, compaction, skills, and commands;
- one selected workspace, ephemeral staging, explicit authorization, and broker-validated publication; and
- bounded cost, iteration, tool, command, process, file, output, and container resources; and
- request-scoped provider data-collection denial plus optional zero-data-retention enforcement.

The QML migration, durable run-to-message association, branch-scoped activity replay, Windows
packaging integration, and provider privacy controls are complete. The v0.7 evaluation rejected
arbitrary MCP discovery/credentials and multi-agent orchestration for v1.0: both would add new
authority, billing, permission, and recovery systems without improving the small auditable core.

The v1.1 release path removes Python, virtual-environment, pip, Compose, and gateway-token setup
from the end-user workflow. A PyInstaller executable and per-user installer launch a desktop-owned
runtime manager. It creates session credentials, starts Docker Desktop when needed, pulls
digest-pinned gateway and executor images, and enables workspace modes only after the exact
executor/workspace identity is healthy. The source checkout retains the developer build script.

The product remains text-only. It handles prompts, Markdown, code, terminal output, diffs, and
ordinary links. It does not ingest documents, build knowledge bases, run OCR/RAG, process media,
or provide invisible computer control.

## 2. Architectural decision

```text
Windows desktop
  QML UI | Python controllers | local SQLite | credential vault | trusted publish broker
       |
       | authenticated HTTP/SSE on numeric loopback
       v
Gateway container
  OpenRouter | catalog | agent runtime | budgets | durable journal
  no workspace mount
       |
       | authenticated Unix-domain socket
       v
Executor container
  network_mode: none | read-only /source | ephemeral writable /work
       |
       | typed publish manifest, never a host mount
       v
Desktop broker
  path + scope + hash validation | atomic write | recovery copy
```

This split is the central security property:

- the component with network access cannot read the workspace;
- the component that reads the workspace cannot access the network or host writes; and
- the component that writes the host is not model-reachable and accepts only reviewed manifests.

Remote gateways and writable workspace mounts are out of scope.

## 3. Capability modes

### Chat

- No workspace identity or local tools.
- Optional Web Search, Web Fetch, and DateTime are OpenRouter-hosted and individually visible.
- Only the selected conversation context crosses the gateway.

### Plan

- Requires one explicitly selected workspace and matching executor identity.
- Permits bounded `list_files`, `read_file`, and `search_text`.
- Cannot patch or execute commands under any permission rule.

### Agent

- Adds bounded patch, command, move/copy, staging inspection, and checkpoint tools.
- Prompt mode requires exact approval; Auto authorizes otherwise-valid tools for the desktop session.
- Prompt publication is reviewed. Auto publication requires clean staging, uses the same desktop
  validation, reseeds after success, and stops on failure or resume.
- Supports steering, queueing, cancellation, budgets, checkpoints, and safe-boundary pause/resume.

Mode restrictions are enforced by shared schemas, gateway policy, and executor policy.

## 4. Current subsystem responsibilities

### Desktop

- Render conversations, branches, streams, tool activity, approvals, settings, and usage.
- Store conversations and preferences in local SQLite.
- Store API and gateway credentials through Windows Credential Manager.
- Connect only to a numeric loopback gateway and validate protocol compatibility.
- Select and canonicalize one workspace.
- Orchestrate Docker Desktop, digest-pinned runtime image pulls, session credentials, and readiness
  from the packaged desktop without exposing process control to QML.
- Present separate Docker, gateway, image download, executor, and workspace mismatch states.
- Publish approved manifests through `WorkspaceBroker`.
- Remain usable for local history when Docker is stopped.

### Gateway

- Authenticate every non-health request.
- Own all OpenRouter traffic and normalize provider streams.
- Cache the model catalog and expose it to the desktop.
- Run the bounded single-agent loop and persist typed run events.
- Preserve internal assistant tool calls/results between prompts.
- Inject a small live executor-capability summary without host paths, raw environment values, or
  workspace contents.
- Enforce context, cost, iteration, tool-call, and wall-time limits.
- Coordinate approvals and permission rules.
- Never mount or infer a host workspace path.

### Executor

- Verify workspace identity and typed tool requests.
- Seed an ephemeral staging copy from the read-only source.
- Reject traversal, links, reparse points, device paths, aliases, and unsafe commands.
- Bound bytes, files, output, time, processes, memory, and CPU.
- Support metadata listings, bounded explicit batch reads, hash-returning reads, glob-filtered
  search, and hash-checked whole-file or exact-replacement edits.
- Avoid duplicating list and search payloads in model context.
- Return tool results and publish manifests; never publish directly.

### Shared protocol

`shared/` defines versions, chat/agent requests, event envelopes, gateway status, permission
rules, tools, results, and publish manifests. It imports no PySide6, FastAPI, or executor code.

## 5. Data and protocol

### Desktop SQLite

The current schema stores:

- conversations with active leaf, pin/archive state, system prompt, model, and preset snapshot;
- parent-linked messages with provider, usage, timing, and cost metadata;
- model catalog, favorites, recents, and aliases;
- settings, prompt presets, custom commands, compaction records, workspace config, generation-run
  links, and run events.

Editing a user message or regenerating an assistant response creates a sibling branch. Existing
history is not overwritten. Migrations must upgrade old flat transcripts losslessly.

### Gateway journal

The gateway stores run metadata, ordered events, snapshots, agent sessions, catalog cache,
workspace configuration, and saved permission rules. Terminal and interrupted runs are pruned by
bounded retention rules. Secrets and full authorization headers never enter the journal.

### Event envelope

Each event contains a schema version, integer event ID, run ID, type, timestamp, and typed payload.
Families cover run lifecycle, model streaming, tools, approvals, usage, budgets, compaction,
checkpoints, pause/resume, cancellation, failure, and completion.

Desktop reconnect uses `Last-Event-ID`. Presentation may summarize activity but must preserve the
underlying semantic state.

### Errors

Protocol errors use a stable code, user-safe message, and retryable flag. Unknown fields, events,
tools, modes, or versions fail closed. A partially emitted billable provider stream is not silently
retried from the beginning.

## 6. Security and privacy requirements

Mandatory invariants:

- gateway port published on `127.0.0.1`, never broad interfaces by default;
- high-entropy desktop/gateway and gateway/executor session credentials;
- no secrets in source, Compose environment values, logs, SQLite, events, or tool environments;
- provider routing denies training/data collection by default and can require ZDR per request;
- one explicit read-only workspace mount, no Docker socket, devices, profiles, or credential roots;
- non-root containers, read-only roots, dropped capabilities, no-new-privileges, default seccomp;
- no executor network and no gateway workspace mount;
- typed command arrays and hard rejection of shell/host-control programs;
- canonical path validation in executor and desktop broker;
- base-hash conflict checks, atomic replacement, and recovery copies;
- exact, inspectable approvals and revocable saved rules; and
- sanitized Markdown/HTML with visible link destinations.

Local-first is not local-only. Model requests may transmit prompts, selected history, file excerpts,
search results, and command output to OpenRouter and its routed provider. Send the minimum needed
context and make disclosure attributable. Local SQLite files are not application-encrypted.

The container boundary does not defend against a compromised Docker daemon, administrator,
kernel, hypervisor, malicious image, or unlocked machine. Keep the runtime patched.

## 7. Maintainability and simplification

The simplification pass is complete. Future changes should preserve the existing backend, workspace,
storage, credential, permission, and agent-runtime behavior while keeping the desktop presentation
easy to maintain by a less-capable LLM.

### Principles

- **KISS:** prefer the smallest complete implementation; keep security checks explicit and readable.
- **SOLID:** keep UI, persistence, network, orchestration, executor, and protocol responsibilities
  separate; add a boundary only when it owns concrete behavior and can be tested independently.
- **DRY:** use one provider boundary, one catalog normalizer, one event replay path, and one request
  preparation/persistence path.
- **YAGNI:** remove superseded callers, compatibility helpers, duplicate tests, unused settings,
  schemas, and speculative extension points in the same change.
- **Security over compression:** never shorten path, permission, container, secret, approval, or
  publication validation merely to reduce line count.
- **Maintainability:** type public/protocol functions, document constraints only, and avoid generic
  utility, service, ORM, or MVVM layers without current behavior.

### Current boundaries

- `server/` owns OpenRouter, agent runtime, budgets, sessions, and the gateway journal.
- `executor/` owns bounded workspace tools and ephemeral staging; it has no network or host writes.
- `src/` owns desktop UI, storage, gateway access, controllers, and trusted publication.
- `shared/` owns framework-neutral schemas.
- The gateway is the only provider boundary; the desktop broker is the only host publication boundary.

### Change checklist

1. Search callers, tests, schemas, settings, scripts, and docs before changing a path.
2. Make one cohesive, behavior-preserving change and remove replaced code in the same change.
3. Add focused tests while preserving security regression coverage.
4. Run the relevant test, lint, compile, container, and Windows checks.
5. Inspect the diff for stale docs, secrets, permission widening, and unnecessary abstractions.
6. Record meaningful line-count changes, but never trade readable safety checks for a target.

The completed pass extracted desktop generation/conversation coordination, durable run-to-message links,
branch-scoped activity replay, and platform wiring without changing gateway or executor authority.
Further simplification is maintenance work, not another roadmap phase.

## 8. Completed QML presentation migration

QML is the sole v1.0 presentation. `DesktopBridge` exposes presentation data and user intents;
existing controllers, workers, storage, gateway access, approval logic, Markdown sanitization,
and the trusted publication broker retain their authority.

### Required transcript model

One user prompt maps to one assistant turn:

```text
User message
Assistant turn
  collapsed tool-name summary
  final answer
  model, usage, cost
```

The QML presentation buffers the final answer and shows only tool names in a collapsed list.
Arguments, request IDs, outputs, model-turn events, and per-event token/context data remain outside
QML. Failed or approval-blocked activity stays expanded. Reopening a conversation reconstructs the
same tool-name association from SQLite while the gateway journal preserves exact events.

### Completed sequence

1. Extracted UI-independent workers, conversation/generation controllers, tray, and hotkey services.
2. Added `generation_runs` plus pure-Python branch-scoped transcript assembly.
3. Replaced the shell, sidebar, transcript, composer, model browser, settings, and approvals with QML.
4. Nested bounded tool-name activity in its assistant turn; failures and approvals expand.
5. Updated screenshot tooling, PyInstaller data, QML lint, and packaged Windows launch smoke tests.
6. Removed `main_window.py`, `chat_view.py`, Widgets dialogs/components, and their obsolete tests.
7. Retained the native frame for Windows Snap, accessibility, move/resize, and platform consistency.

### QML acceptance scenario

A run with at least five model/tool iterations and fifteen tool calls produces one top-level
assistant turn. Tool names may update as calls begin; response text appears once on completion.
Activity stays collapsed unless attention is required. Branches and reopened conversations show
only their associated tool calls. The transcript uses exact-height rows and incremental model
updates so scrolling away from the live tail is respected while agent activity continues.

## 9. Completed phases and v1.0 decisions

| Phase | Result |
|---|---|
| v0.5 maintenance | Controllers extracted; durable run links, replay, migrations, and container tests complete. |
| v0.6 presentation | QML parity complete; Widgets presentation removed. |
| v0.7 extensibility | Skills and declared capability discovery retained. Arbitrary MCP and subagents rejected for v1.0 because their credentials, permissions, billing, concurrency, and recovery would create a second authority system. |
| v1.0 release | QML default/only UI, provider privacy/ZDR routing, protocol/version update, packaging assets, Windows launch smoke test, and canonical docs complete. |
| v1.1 release path | PyInstaller executable, Inno Setup installer, digest-pinned GHCR images, automatic runtime setup, and workspace-gated Plan/Agent activation. |

The roadmap is closed. Later features require a new plan with explicit authority and recovery
models; they are not implicit unfinished v1.0 work.

## 10. Testing and release gates

Run:

```text
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Required coverage:

- old-schema migrations, branches, import/export, catalog cache, and usage;
- provider payloads/streams, malformed responses, partial output, and cost normalization;
- gateway auth, version negotiation, replay, cancellation, pause/resume, and recovery;
- agent context, sessions, budgets, skills, permissions, and final reporting;
- traversal, links/reparse points, command policy, output/file/process limits;
- staging exclusions, manifests, hash conflicts, broker writes, and recovery;
- effective Compose config, loopback binding, executor egress, mounts, and container health; and
- Windows UI launch, shortcuts, tray/hotkey, QML load, and PyInstaller packaging as applicable.

Unit tests use fake transports and no live API key. Live provider tests are explicit opt-in and
never run on ordinary pull requests.

A release requires passing tests, successful database upgrade from supported schemas, a clean
Windows package smoke test, container profile inspection, dependency review, and documentation
that matches shipped behavior.

## 11. Repository map

```text
app.py                    QML desktop entry point
qml/
  Main.qml                Native-frame application shell, models, settings, approvals
  Sidebar.qml             Searchable conversation navigation
  Transcript.qml          Exact-height semantic turns and bounded tool-name activity
  Composer.qml            Prompt, queue/steer, hosted-tool controls
src/
  qml_backend.py          QML-facing application controller and presentation data
  transcript_model.py     Incremental Qt transcript rows
  controllers.py          Conversation/generation behavior without presentation
  transcript.py           Pure semantic transcript assembly
  workers.py              Chat and agent stream QThreads
  window_services.py      Tray and Windows global-hotkey services
  storage.py              Desktop SQLite repositories
  migrations/             Explicit desktop schema upgrades
  gateway_client.py       Authenticated loopback protocol client
  model_catalog.py        Desktop cache/presentation facade
  workspace_broker.py     Trusted staged-change publisher
server/
  app.py                  HTTP routes and middleware assembly
  service.py              Gateway application service
  agent/                  Runtime, context, budgets, approvals
  openrouter/             Sole provider client and catalog normalizer
  journal/                Durable events, sessions, rules, catalog
  executor/               Unix-socket executor client
  extensions/             Skills and declared capability discovery
executor/
  app.py                  Authenticated executor service
  staging.py              Read-only source to ephemeral work copy
  paths.py                Canonical path policy
  tools/                  Five bounded executor tools
shared/                   Framework-neutral protocol models
docker/                   Images, Compose profile, operator guide
tests/                    Unit, integration, migration, security tests
scripts/                  Windows lifecycle and deterministic utilities
build/                    PyInstaller metadata
.github/workflows/        Windows release and container validation
```

## 12. Agent workspace tooling follow-up

Implementation status (2026-08-10): Phases A–E and all P0/P1/P2 delivery items are complete in the
shared protocol, gateway/runtime, isolated executor, desktop review surface, tests, and this plan.
The phase details below remain as the acceptance traceability record for the implementation.

Feedback from tool-using models is useful evidence of friction, but it is not itself a requirement.
Accept a request only when it improves task completion without widening executor authority or sending
large, low-value payloads into a 100k-token model context.

### Assessment

| Feedback | Decision | Reason |
|---|---|---|
| Show whether staging is empty, what snapshot it represents, how long it survives, and how publication works | Accept | The current summary says staging is ephemeral but does not define its lifetime or clearly distinguish source, initial staging, current staging, and published host state. |
| Add a staged status/diff view | Accept, highest priority | Agents and users currently have to infer changes from edit results or build a full publish manifest. A bounded base-to-staging view improves review, reporting, and recovery. |
| Make returned checkpoint IDs restorable | Accept, highest priority | Current checkpoints contain hashes only and no restore operation. Returning an unusable ID implies a recovery contract that does not exist. |
| Separate command stdout and stderr and identify truncation per stream | Accept | Combining streams loses diagnostic ordering and makes failures harder to interpret. Exit code, elapsed time, and cancellation already exist. |
| Standardize errors and limit/truncation metadata | Accept | Most executor failures currently collapse to `tool.invalid`; callers need stable distinctions such as missing path, hash conflict, invalid UTF-8, output truncation, permission denial, and timeout. |
| Stream command output, permit longer builds, and support bounded read-only parallel calls | Accept later, with bounds | These can reduce latency and help long tests, but require cancellation, event replay, combined output budgets, and deterministic result ordering first. |
| Add file cursors, search context lines, selected file metadata, and move/copy operations | Accept selectively | Targeted navigation is better than raising payload limits. Move/copy may be useful, while empty directories and broad metadata search do not survive publication meaningfully. |
| Add executable versions and storage headroom to the capability summary | Accept selectively | A small, allowlisted inventory can prevent failed probes. Raw environment variables, process lists, package inventories, and host paths remain unnecessary or sensitive. |
| Add a capability manifest, explicit no-network/no-GPU/no-shell state, per-call read/search/timeout controls, file sizes, case/glob search, and create/delete/nested-file edits | Already addressed | The executor status and tool schemas now expose these facts and controls. `apply_patch` creates parents, creates/deletes text files, and supports hash-checked whole-file or exact-replacement edits. |
| Add an opt-in shell, arbitrary environment variables, background processes, or unrestricted system inspection | Reject | These bypass typed command policy, complicate approval and cancellation, and widen access without being necessary for the auditable local-agent core. |
| Raise ordinary read/output limits to 1–5 MB or search archives and binary contents | Reject | A 1 MB result can consume roughly 250k tokens before surrounding context. Prefer search, line/byte cursors, summaries, and explicit bounded slices. |
| Reveal hidden instruction precedence/content or platform channel mechanics | Reject | Repository instructions are untrusted input, and hidden application/provider instructions are not workspace capabilities. The app should state effective task constraints without disclosing protected context. |

### Context budget

All phases preserve the existing minimum-disclosure rule:

- generated workspace context stays at or below 1,600 characters and contains no listing, host path,
  raw environment value, or package inventory;
- the serialized local tool schema set stays below 4,500 characters;
- ordinary reads default to 64 KB combined and retain the 256 KB hard ceiling;
- status, diff, search, command, and error results report explicit counts and truncation instead of
  repeating the same payload in `output` and structured data; and
- the gateway applies a combined per-turn tool-result budget before adding results to model context.
  Oversized results remain inspectable locally and are represented to the model by a bounded tail or
  summary plus a cursor.

### Phase A — make state and failures unambiguous

Status: Complete. The capability block is versioned, the result envelope carries bounded-result
metadata, stable executor error codes are mapped centrally, and patch writes use checkpoint rollback
with the documented validate-before-write atomicity boundary.

1. Version the executor capability block and add compact fields for `workspace_empty`, current mode,
   source snapshot identity, staging lifetime, and whether unpublished changes exist. State that
   staging survives prompts only while the same executor instance remains healthy and is discarded
   when the workspace/mode runtime is recreated.
2. Define one result-envelope convention for `returned`, `total_known`, `limit`, `truncated`, and
   `next_cursor`. Omit unknown totals rather than guessing them.
3. Replace the broad `tool.invalid` catch with stable codes for invalid arguments, missing/not-file
   paths, invalid UTF-8, size limits, hash/match conflicts, permission denial, timeout, cancellation,
   and internal I/O failure. Messages remain user-safe and exclude absolute paths.
4. Document mutation atomicity accurately: validate a patch set before writing, but do not call a
   multi-file change atomic until rollback or transactional replacement exists.

Acceptance: an empty workspace requires no discovery call; every bounded result says whether more
data exists; the UI and model can distinguish a retryable stale hash from an invalid request; and
the injected context remains within the budgets above.

### Phase B — add bounded status, diff, and publication review

Status: Complete. Workspace inspection, bounded UTF-8 diffs, manifest comparison, and desktop review
and discard actions now use the same ordered change primitive; publication remains user-approved.

1. Add an `inspect_workspace` read tool that compares the initial snapshot with current staging and
   returns sorted create/update/delete entries with base/staged hashes and sizes. It never returns
   unchanged paths or `.local-chat-*` metadata.
2. Add an optional bounded unified diff for explicitly selected UTF-8 files. Enforce per-file and
   combined line/byte ceilings, report omitted files/hunks, and return a cursor instead of silently
   truncating. Binary or invalid-UTF-8 files receive metadata only.
3. Build the publish manifest from the same comparison primitive so agent status, user review, and
   broker publication cannot disagree about what changed.
4. Expose clear desktop actions for review, publish, and discard. The model may identify candidate
   paths, but only the user approves host publication.

Acceptance: status and publish manifests describe the same ordered operations; a diff cannot exceed
its result budget; no host path or unchanged file leaks into model context; and UI review shows every
operation before publication.

### Phase C — make recovery real

Status: Complete. Checkpoints are bounded content-addressed snapshots with inspection, preview,
restore, pruning, restore events, and an explicit discard/reseed action.

1. Replace hash-only checkpoint manifests with bounded, content-addressed staging snapshots, or stop
   returning `checkpoint_id` until restoration exists.
2. Add exact-ID checkpoint inspection and restore operations. Restoration first previews affected
   paths, requires the same mutation approval boundary, prepares all content before replacement, and
   records durable checkpoint/restore events.
3. Bound checkpoints by count and total bytes; prune oldest unreferenced snapshots deterministically.
   Never copy executor metadata, links, devices, or paths outside staging.
4. Add a separate user action to discard all staging and reseed from the read-only source snapshot.

Acceptance: a failed multi-step edit or command can restore byte-for-byte staging state; restore
failures leave the prior staging tree usable; checkpoint retention is bounded; and replay explains
which checkpoint was created, restored, or pruned.

### Phase D — improve command diagnostics without adding a shell

Status: Complete. Commands now preserve bounded stdout/stderr streams, stream ring-buffer events
with replay cursors, retain process-group cancellation, and keep direct argv and isolation rules.

1. Return bounded stdout and stderr fields separately with exit code, elapsed time, per-stream byte
   counts, and per-stream truncation. Preserve one combined display rendering only in the UI.
2. Stream incremental command events into bounded ring buffers with sequence IDs, replay, tail
   cursors, and the existing request-ID cancellation path. Slow consumers must not create unbounded
   memory or model context.
3. After streaming and cancellation tests pass, consider increasing the approved per-call timeout
   ceiling for known test/build executables while retaining the run wall-time and process limits.
4. Keep direct argv execution, the environment allowlist, denied shells/host-control programs,
   network isolation, and no-background-process rule.

Acceptance: diagnostics preserve stream identity; cancellation terminates the process group; output
memory and model disclosure remain bounded; and no command path gains shell parsing or egress.

### Phase E — add targeted navigation only where evidence supports it

Status: Complete. Bounded file cursors, search context and metadata, hash-checked move/copy, bounded
read-only concurrency, and compact executable/headroom capability facts are implemented.

1. Add line or byte cursors so an agent can continue through a large UTF-8 file without increasing
   the 256 KB ceiling. Hash metadata continues to describe the complete file.
2. Add bounded search context lines and optional file-kind/encoding metadata. Do not search archive
   members or binary contents automatically.
3. Evaluate typed `move_file`/`copy_file` operations using the same path, size, hash, approval, and
   checkpoint rules. Do not add empty-directory publication because manifests publish files only.
4. Evaluate up to four concurrent read-only calls in the gateway. Preserve request/result order and
   enforce one combined byte budget; mutation and command calls remain serialized.
5. Add allowlisted executable version and staging storage-headroom fields only if telemetry from
   failed local runs shows they avoid meaningful probing. Keep the generated summary within budget.

Acceptance: later file slices and search context are reachable without megabyte responses; any new
operation preserves current traversal and approval tests; and concurrency cannot reorder mutations,
evade budgets, or duplicate model-visible content.

### Delivery order and release gates

- **P0:** Complete — Phase A and Phase B correct the contracts and make staging reviewable.
- **P1:** Complete — Phase C and separate stdout/stderr provide deterministic recovery and command
  diagnostics.
- **P2:** Complete — command streaming, bounded read-only concurrency, and the evidence-backed Phase E
  navigation items are delivered without expanding executor authority.

Each phase updates shared schemas, gateway/runtime handling, desktop review surfaces, executor code,
and canonical documentation together. Required tests cover token/character budgets, empty and large
workspaces, truncation cursors, error codes, diff/manifest equivalence, restore failures, replay,
cancellation, traversal/link rejection, approvals, publication conflicts, and executor isolation.
The existing full unit, lint, compile, QML, Compose, gateway, and executor CI gates remain mandatory.

## 13. Resolved decisions

- Unassociated legacy events render in one collapsed legacy activity row.
- QML renders the existing sanitized Markdown HTML; QML never receives unsanitized executable HTML.
- The app keeps native Windows chrome instead of a frameless replacement.
- Provider data collection is denied by default; users may additionally require ZDR per request.
- Hosted web tools disclose that their retention policies are separate from inference ZDR.
- Arbitrary MCP and subagents are outside v1.0 after the v0.7 authority/maintenance evaluation.

## 14. Definition of done

A change is done when implementation, protocol, tests, docs, and security claims agree. It reduces
or clearly contains complexity, preserves local history, keeps secrets out of model-controlled
paths, leaves no writable model-to-host mount, and makes cost, tools, approvals, and changes
understandable to the user.
