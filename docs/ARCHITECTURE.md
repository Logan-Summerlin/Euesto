# Architecture

This document describes the implementation on the current branch. Older architecture notes and phase history are not normative.

## Ownership and dependency direction

| Area | Owner | Dependency rule |
|---|---|---|
| Presentation | `qml/` | Qt Quick composition; no security or persistence policy. |
| Desktop adapter and services | `src/` | May use `shared/`; coordinates gateway and publication through explicit clients. |
| Bootstrap | `app.py` | Wires the desktop application; contains no domain policy. |
| Gateway | `server/` | Owns provider and agent behavior; has no workspace mount. |
| Executor (control plane) | `executor/` | Owns file tools, staging, checkpoints, baselines, and manifests; does not import desktop or gateway internals. |
| Runner | `runner/` | Runs Bash commands; standard library only, imports nothing from the project. |
| Protocol | `shared/` | Framework-neutral; must not import `src/` or `server/`. |

## Components

- **Desktop** — PySide6/Qt Quick UI (`app.py`, `src/`, `qml/`), local SQLite history/settings, runtime management, approvals, and the trusted publication broker. It is the only component allowed to publish changes into the selected host workspace.
- **Gateway** — a loopback-authenticated service in Docker (`server/`). It owns OpenRouter access, the agent loop, sessions, budget profiles, journals, permission rules, skills, and provider-facing policy. It has no workspace mount.
- **Executor (control plane)** — a separate Docker service reached through authenticated Unix-socket IPC. It serves the nine executor tools `read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, and `status`; the gateway serves the other four model-facing tools itself (`investigate_repository`, a nested read-only loop over the executor's read tools; `update_plan`; and the opt-in documentation tools `web_search` and `fetch_url`), for thirteen in all. It runs the file tools, `status`, checkpoints, baselines, and manifests itself, and hands every `bash` command to the runner. It mounts the read-only source, staging, and its state volume, has no network, runs as a non-root user with small fixed limits (1 CPU, 1 GB, 128 PIDs), and never publishes to the host.
- **Runner** — the workbench where commands run (`runner/`, `docker/Dockerfile.runner`). A stdlib-only command server listens on a Unix socket shared only with the executor and runs one command at a time in its own process group. The runner mounts only staging (`/work`, exec allowed), a package cache (`/cache`), and its socket directory: no source mount, no state volume, no secrets, no executor socket, and no network (in a session that opted into the `registries` network mode, the egress overlay attaches the runner, never the executor, to the allowlisted proxy; dependency setup runs in a separate one-shot setup container, `docs/EGRESS.md`). It runs as UID 10002 with a read-only root filesystem, all capabilities dropped, and `no-new-privileges`, with CPU, memory, and PID limits the user sets within ceilings (`docs/LIMITS.md`). Four digest-pinned images (`runner-base`, `runner-python`, `runner-node`, `runner-polyglot`) carry the toolchains; the desktop picks one from the project's files unless Settings or the confirmed workspace policy names one, and `/v1/status` reports the executables actually installed.
  The QML adapter `src/qml_backend.py` (`DesktopBridge`) only exposes signals, properties, and slots to QML; behavior lives in six services under `src/desktop/`: `RuntimeService` (selected workspace, local runtime, gateway token and health), `SettingsService` (preferences, model catalog, commands, presets, skills, workspace configuration, permission rules), `ConversationService` (conversation list, transcript, import/export, context), `GenerationService` (Chat/Plan/Agent runs, session approval tier, tool approvals, queued input), `StagingPublicationService` (staged review/discard, the review pane with partial publication and declared checks, and batched publication), and `PolicyService` (the workspace policy file and its confirmation, the dependency setup phase, and the egress audit log). Services reach QML only through the bridge's signals and confirmations; the bridge's QML surface is pinned by `tests/ui/test_desktop_bridge_surface.py`.
- **Workspace mount** — the selected host workspace is presented to the executor as a read-only source snapshot. Agent mutations happen in persistent, disposable staging.
- **Staging/checkpoints** — staging is the mutable working copy for Agent mode. Mutations checkpoint first; failed, cancelled, or timed-out mutations are rolled back. Successful mutations remain staged until publication. Staging, executor state (workspace identity, publication baseline, conflicts, checkpoints), and the package cache live in per-workspace named volumes (`euesto-{work,state,cache}-<key>`, where the key is the first 12 hex digits of the workspace identity). They survive `compose down` and app restarts and are removed only by *Discard* or by removing the staged session in Settings. A state volume claimed by another workspace identity is refused.
- **Host drift and conflicts** — when staging resumes, files changed on the host since the baseline are absorbed into staging and the baseline (a three-way rebase). A file changed both on the host and in staging is a *conflict*: staging keeps its version, `status` lists it with base, host, and staged hashes, and it is held back from publication until the user keeps the staged or takes the host version.
- **Sandbox git** — at seed time the control plane clones host history (200 commits by default; full history is a setting) into the work volume's `.git` with no remotes, hooks, or credential helpers, and records the seeded tree as `refs/euesto/base`. In the runner, `git log`, `git blame`, and `git diff HEAD` work, and `git diff refs/euesto/base` shows exactly the agent's changes. `.git` stays excluded from the file tools, checkpoints, and publication, and checkpoint rollback does not rewind git state.
- **Publication broker** — the desktop compares the staged state with the source snapshot, obtains approval, validates the manifest and hashes, applies path-bounded changes to the host, and retains recovery information.

## Data flow

```text
User
  -> Desktop UI
  -> local authenticated gateway
  -> OpenRouter (prompts + selected context/tool output)
  -> Gateway agent loop
  -> authenticated executor IPC
  -> source snapshot / staging (file tools)
     or runner socket -> command in the runner (bash)
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

The gateway may communicate with the model provider, but cannot read the workspace. The executor can read the selected workspace but cannot access the network or host-write path, and runs no commands. The runner runs commands against staging only. This separation is the primary security boundary.

## Modes

**Chat** has no local workspace tools. **Plan** exposes only `read`, `grep`, `find`, `ls`, `investigate_repository`, and `update_plan`, against the read-only source. **Agent** exposes all thirteen tools (`web_search` and `fetch_url` only when the workspace enables them), with mutations confined to staging; consecutive read-only executor calls in one turn run concurrently while mutations and gateway tools stay serialized. Publication is a separate desktop-authorized operation.

Agent runs additionally carry an approval policy (`prompt`, `sandbox`, or `auto`; see `docs/TOOLS.md`), a network mode (`none` or `registries`; see `docs/EGRESS.md`), and a budget profile. Prompted approvals use the remaining wall-clock budget as their deadline; expiry is journaled as `approval.timeout` and cannot leave a run waiting indefinitely. The `sandbox` tier runs `write`, `edit`, `apply_patch`, and `bash` without prompts while the runner has no network, because every effect stays in the checkpointed staged copy with no network, credentials, or `/source`; every publication batch is still reviewed. Auto mode auto-allows otherwise-valid tool calls but does not grant network, host-path, or publication authority to the executor, and stops for confirmation at risk-flagged publication batches. With registry access, Bash asks under every tier unless a saved rule matches. Auto never survives a resume; Sandbox becomes the default for new Agent sessions once the user has read its explanation.

## Agent loop and context

The gateway owns one versioned system prompt per mode (`server/agent/prompts.py`, recorded in `run.started` as `prompt_version`). It is short, imperative, written for low-cost models, contains no dynamic values, and is bounded to 4,800 bytes, so it stays cached across turns, runs, and sessions. Messages follow a cache-stable layout: the static prompt; then the **session context**, rebuilt only when a session starts or after compaction (the conversation's own system prompt as a user preference, user-authored workspace instructions up to 32,000 characters, the workspace's `AGENTS.md` as untrusted content, selected skills, and a bounded, deterministic workspace snapshot of layout, manifests, and toolchain facts); then the history; and finally per-run **run notes** (budgets, network mode). Volatile values never sit ahead of cached content.

- **Result bounds.** Each model-facing tool result is bounded on its own (24,000 bytes by default, 32,000 for `read`, never more than 10% of the working set), and there is no cumulative per-run allowance. A shortened result emits `tool.result_bounded` with its original size. Results are lean plain text: hashes the harness already knows are filled in for the next edit, an unchanged re-read returns a one-line note, and errors carry a short hint.
- **Full output as artifacts.** Every command's complete output is written to `.euesto-output/` in staging (never published), and the result names the log so the agent can search it from `bash` (`grep`, `tail`); the file tools never show it. Test, lint, and compiler output (pytest, unittest, Jest, Vitest, Go, Cargo, tsc, ESLint, Ruff, mypy, gcc/clang) leads with a structured reduction: counts and the first failures with file and line.
- **Compaction.** Context is compacted only at a high-water mark (the smaller of 60% of the model's window and the profile's working-set cap, 96,000 tokens in `coding`), back down to a third of it. The model writes a structured handoff (objective, decisions, files touched, commands, plan state, next steps); a deterministic summary is the fallback. Compaction is journaled as `context.compacted`.
- **Usage and caching.** Every turn journals `usage.turn` with prompt, cached, cache-write, completion, and reasoning tokens, cost, and a cache-miss cause; unexpected misses are journaled as `cache.unexpected_miss`. Providers that need explicit cache breakpoints get them.
- **Retries.** Provider timeouts, connection errors, HTTP 408/409/425/429, 5xx, and error bodies returned with a 200 status are retried up to three attempts with jittered backoff (`model.retry`). A final retryable failure pauses the run (`paused`, reason `provider_error`) so it can be resumed rather than restarted.
- **Steering.** `POST /v1/runs/{id}/messages` accepts guidance of up to 8,000 characters for an active run, journaled as `input.steered`. The loop appends it as a user message before the next model turn, never during a tool call; terminal runs reject it.
- **Output discipline.** Each turn is capped at the profile's `max_tokens` (16,000 in `coding`); a turn cut off by the limit gets a short note to write large files in parts. Compaction turns use low reasoning effort.
- **Streaming.** Agent turns are streamed: tool calls are assembled from the streamed deltas, an idle timeout (120 seconds between chunks) and a per-turn ceiling (600 seconds) replace a fixed request timeout, and text is journaled as bounded `model.stream` events so the desktop shows it live.
- **Plan.** `update_plan` keeps a gateway-local plan of up to 20 steps per session. It is journaled as `plan.updated`, shown as a checklist above the composer, survives resume, and is carried into every compaction handoff and wind-down summary.
- **Budget wind-down.** At 85% of any budget the loop adds one note asking the agent to wrap up. When a budget is exhausted (or after four turns without progress), a tool-free summary turn funded from a reserved slice of the budget runs, and the run pauses with reason `budget` (or `no_progress`). The desktop offers *Continue with another budget*, which resumes with a fresh budget of the same profile, its plan, and the handoff.
- **Loop resilience.** Malformed arguments, schema violations, and unknown tools get one-line corrective results; a call that fails the same way three times gets a note to change approach; an empty or reasoning-only reply gets one re-prompt (`loop.note`).
- **Secret redaction.** Every tool result is scanned for high-confidence secret formats before it enters the history, so a secret never reaches the provider; matches become `[REDACTED:<kind>]` and `secret.redacted` records kinds and counts only.

The desktop shows the model's narration between tool calls (`model.narration`), steering, retries, and compactions in the transcript, and a live meter of cost, cache hit rate, context against the working set, tool calls, and elapsed time.

## Investigation delegation

`investigate_repository` is a public tool available in Plan and Agent mode. It opens a bounded nested agent loop that uses a separately configured investigation model and may call only `read`, `grep`, `find`, `ls`, and `status` through the parent's existing executor session. Its cost debits the parent run's budget; up to four calls are accepted per turn; nested activity is journaled as `subagent.*` events for replay and audit. The delegation grants no mutation, command, staging, or publication authority.

## Budgets and journaling

Agent runs are bounded by iteration, tool-call, wall-time, and cost budgets (`server/agent/budgets.py`). The standard `coding` profile allows 600 iterations, 900 tool calls, 1,800 seconds, and $2.00; larger profiles exist and require explicit user approval when they exceed twice the standard limits on tool calls, wall time, or cost. Run state, events, permission rules, sessions, and resumable snapshots persist in the gateway's SQLite journal (`server/journal/store.py`), which supports pause/resume and recovery of interrupted runs.

## Skills and extensions

Markdown skill files with frontmatter can be discovered globally (`%LOCALAPPDATA%/LocalOpenRouterChat/skills`) or per workspace (`.local-chat/skills`; workspace skills shadow global ones with the same name). Selected skills are rendered as system context and may declare required tools; a skill requiring an unavailable tool is rejected. Workspace configuration may also declare custom capabilities, which are surfaced as declared-but-unavailable entries so they cannot grant executable authority.

## Publication and recovery

The executor creates a manifest from its current staging baseline. Because staging persists, changes can outlive the run that made them; the desktop can publish them later through the same reviewed manifest (`POST /v1/workspaces/{id}/staging/publication`). Closing the app or switching workspace with unpublished changes offers *Review and publish*, *Discard*, *Keep for later*, or *Cancel*; staged work is never removed silently. The review pane shows every staged change with its diff, risk flags, and declared check results, and can publish a selected subset; unselected changes stay staged. The manifest records workspace identity, source snapshot identity, approval identity, operations, and staged content hashes. The desktop broker validates that the manifest is for the selected workspace and current baseline before applying it. Stale or conflicting manifests are rejected rather than silently rebased. Broker-side bounds apply per batch: at most 500 operations and 32,000,000 bytes of content in each separately approved, all-or-nothing batch, so a larger changeset publishes as several batches. See [PUBLICATION.md](PUBLICATION.md).

## Workspace policy

`.euesto/policy.toml` in the host workspace declares the runner image, network mode and registry additions, setup command, named checks, extra secret patterns, and never-published paths (`shared/policy.py`, `docs/PUBLICATION.md`). The desktop reads it from the host only, never from staging; it takes effect only after the user confirms exactly the reviewed file, any change needs confirming again, and an invalid file fails closed. The confirmed policy is passed to the gateway in the workspace configuration and to the runtime as Compose environment (secret patterns for the executor, never-publish globs for the executor and the broker, the allowlist for the proxy).

## Security boundary

The **executor cannot publish**. It is network-disabled, non-root, capability-restricted, and backed by a read-only source mount plus bounded writable staging. Bash is non-interactive and subject to command, stdin, output, timeout, process, and environment controls (a fixed base environment with user-supplied variables filtered by name and size). Bash runs in the runner, which has no source mount, no state volume, no secrets, and no executor credential, so a command can read only staging. The gateway does not receive a workspace mount. Host publication is unavailable to the executor and the runner by design. Dependency setup runs in a separate one-shot container with the runner's image and restrictions, attached only to the allowlisted registry proxy after the user approves the command; in isolated setup it sees only the dependency manifests, never the source tree, and the agent's runner stays offline. The documentation tools run in the gateway, never touch the executor or runner, and return untrusted, bounded text.

Path handling rejects absolute/drive/UNC paths, traversal segments, Windows aliases and reserved DOS names, non-canonical Unicode, secret-like paths (`.env*`, `.ssh`, credentials, key stores and certificates such as `*.pem` and `*.key`, `.netrc`, `kubeconfig`, `*.tfstate`, `secrets.*`, and patterns the confirmed workspace policy adds), symlinks, and hard-linked files. Secret-like files are never copied into staging, so neither the file tools nor Bash can read them. A symlink a command creates in staging is recorded as an *unpublishable link*: the file tools still refuse to traverse it, `status` lists it under `unpublishable_links`, checkpoints capture and restore it, and manifests never include it. Staging excludes common metadata, dependency, and cache directories (`.git`, `.venv`, `venv`, `node_modules`, bytecode/test/type-checker caches, and similar), and every read-oriented tool (`read`, `grep`, `find`, `ls`) treats those paths as absent in both modes. A plain `env/` directory is ordinary source and is not excluded.

## Current versus historical design

The current architecture is the thirteen-tool surface, the control-plane executor and separate runner, persistent and disposable staging, scoped investigation delegation, and desktop publication broker described above. Earlier plans that referenced alternate public tool names, compatibility aliases, mandatory hashes, or direct executor publication are historical and must not be treated as current behavior.
