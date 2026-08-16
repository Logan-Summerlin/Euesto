# Architecture

This document describes the implementation on the current branch. Older architecture notes and phase history are not normative.

## Components

- **Desktop** — PySide6/Qt Quick UI, local history/settings, runtime management, approvals, and the trusted publication broker. It is the only component allowed to publish changes into the selected host workspace.
- **Gateway** — a loopback-authenticated service in Docker. It owns OpenRouter access, the agent loop, sessions, budgets, journals, and provider-facing policy. It has no workspace mount.
- **Executor** — a separate Docker service reached through authenticated IPC. It exposes exactly seven local tools: `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls`. It has no network access, runs as a non-root user, and never publishes to the host.
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

**Chat** has no local workspace tools. **Plan** exposes only `read`, `grep`, `find`, and `ls`, against the read-only source. **Agent** exposes all seven tools, with mutations confined to staging. Publication is a separate desktop-authorized operation.

The public protocol is intentionally seven-tool-only. Checkpoints, staging, manifests, and mutation primitives are internal mechanisms rather than additional agent capabilities.

## Publication and recovery

The executor creates a manifest from its current staging baseline. The manifest records workspace identity, source snapshot identity, approval identity, operations, and staged content hashes. The desktop broker validates that the manifest is for the selected workspace and current baseline before applying it. Stale or conflicting manifests are rejected rather than silently rebased. See [PUBLICATION.md](PUBLICATION.md).

## Security boundary

The **executor cannot publish**. It is network-disabled, non-root, capability-restricted, and backed by a read-only source mount plus bounded writable staging. Bash is non-interactive and subject to command, stdin, output, timeout, process, and environment controls. The gateway does not receive a workspace mount. Host publication is unavailable to the executor by design.

## Current versus historical design

The current architecture is the seven-tool executor, ephemeral staging, and desktop publication broker described above. Earlier plans that referenced alternate public tool names, compatibility aliases, mandatory hashes, or direct executor publication are historical and must not be treated as current behavior.
