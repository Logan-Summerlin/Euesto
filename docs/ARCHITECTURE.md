# Architecture

This document describes the implementation on the current branch. Older architecture notes and phase history are not normative.

## Components

- **Desktop** — PySide6/Qt Quick UI (`app.py`, `src/`, `qml/`), local SQLite history/settings, runtime management, approvals, and the trusted publication broker. It is the only component allowed to publish changes into the selected host workspace.
- **Gateway** — a loopback-authenticated service in Docker (`server/`). It owns OpenRouter access, the agent loop, sessions, budget profiles, journals, permission rules, skills, and provider-facing policy. It has no workspace mount.
- **Executor** — a separate Docker service reached through authenticated Unix-socket IPC. It exposes exactly eight local tools: `read`, `write`, `edit`, `bash`, `grep`, `find`, `ls`, and `investigate_repository`. It has no network access, runs as a non-root user, and never publishes to the host.
- **Workspace mount** — the selected host workspace is presented to the executor as a read-only source snapshot. Agent mutations happen in an ephemeral writable staging area.
- **Staging/checkpoints** — staging is the mutable working copy for Agent mode. Mutations checkpoint first; failed, cancelled, or timed-out mutations are rolled back. Successful mutations remain staged until publication.
- **Publication broker** — the desktop compares the staged state with the source snapshot, obtains approval, validates the manifest and hashes, applies path-bounded changes to the host, and retains recovery information.

## Data flow

```text
User
  -> Desktop UI
  -> local authenticated gateway
  -> OpenRouter (prompts + selected context/tool output)
  -> Gateway agent loop
  -> authenticated executor IPC
  -> source snapshot / staging
  -> ToolResult
  -> Gateway
  -> Desktop

Agent publication:
  staged changes
    -> manifest + approval
    -> desktop publication broker
    -> hash/path validation
    -> host workspace
    -> recovery record
```

The gateway may communicate with the model provider, but cannot read the workspace. The executor can read the selected workspace but cannot access the network or host-write path. This separation is the primary security boundary.

## Modes

**Chat** has no local workspace tools. **Plan** exposes only `read`, `grep`, `find`, and `ls`, against the read-only source. **Agent** exposes all eight tools, with mutations confined to staging. Publication is a separate desktop-authorized operation.

Agent runs additionally carry an approval policy (`prompt` or `auto`) and a budget profile. Auto mode auto-allows otherwise-valid tool calls but does not grant network, host-path, shell, or publication authority to the executor.

## Investigation delegation

`investigate_repository` is an eighth public tool available in Agent mode. It opens a bounded nested agent loop that uses a separately configured investigation model and may call only the Plan tool set through the parent's existing executor session. Its cost debits the parent run's budget; at most two calls are accepted per turn; nested activity is journaled as `subagent.*` events for replay and audit. The delegation grants no mutation, command, staging, or publication authority.

## Budgets and journaling

Agent runs are bounded by iteration, tool-call, wall-time, and cost budgets (`server/agent/budgets.py`). The standard `coding` profile allows 600 iterations, 900 tool calls, 1,800 seconds, and $2.00; larger profiles exist and require explicit user approval when they exceed twice the standard limits on tool calls, wall time, or cost. Run state, events, permission rules, sessions, and resumable snapshots persist in the gateway's SQLite journal (`server/journal/store.py`), which supports pause/resume and recovery of interrupted runs.

## Skills and extensions

Markdown skill files with frontmatter can be discovered globally (`%LOCALAPPDATA%/LocalOpenRouterChat/skills`) or per workspace (`.local-chat/skills`; workspace skills shadow global ones with the same name). Selected skills are rendered as system context and may declare required tools; a skill requiring an unavailable tool is rejected. Workspace configuration may also declare custom capabilities, which are surfaced as declared-but-unavailable entries so they cannot grant executable authority.

## Publication and recovery

The executor creates a manifest from its current staging baseline. The manifest records workspace identity, source snapshot identity, approval identity, operations, and staged content hashes. The desktop broker validates that the manifest is for the selected workspace and current baseline before applying it. Stale or conflicting manifests are rejected rather than silently rebased. Broker-side bounds: at most 500 operations and 32 MB of content per publication. See [PUBLICATION.md](PUBLICATION.md).

## Security boundary

The **executor cannot publish**. It is network-disabled, non-root, capability-restricted, and backed by a read-only source mount plus bounded writable staging. Bash is non-interactive and subject to command, stdin, output, timeout, process, and environment controls (a fixed base environment with user-supplied variables filtered by name and size). The gateway does not receive a workspace mount. Host publication is unavailable to the executor by design.

Path handling rejects absolute/drive/UNC paths, traversal segments, Windows aliases and reserved DOS names, non-canonical Unicode, secret-like paths (`.env*`, `.ssh`, credentials, and similar), symlinks, and hard-linked files. Staging excludes common metadata, dependency, and cache directories (`.git`, `.venv`, `node_modules`, bytecode/test/type-checker caches, and similar).

## Current versus historical design

The current architecture is the eight-tool executor, ephemeral staging, scoped investigation delegation, and desktop publication broker described above. Earlier plans that referenced alternate public tool names, compatibility aliases, mandatory hashes, or direct executor publication are historical and must not be treated as current behavior.
