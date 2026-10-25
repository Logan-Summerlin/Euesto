# Architecture

This is the authoritative description of how the components fit together and where the security boundary sits. Per-tool behavior is in [TOOLS.md](TOOLS.md); numeric limits are in [LIMITS.md](LIMITS.md); publication rules are in [PUBLICATION.md](PUBLICATION.md); network modes are in [EGRESS.md](EGRESS.md).

## Ownership and dependency rules

| Area | Owner | Rule |
|---|---|---|
| Presentation | `qml/` | Qt Quick composition only. No security or persistence policy. |
| Desktop adapter and services | `src/` | Uses `shared/`. Reaches the gateway and the publication path through explicit clients. |
| Bootstrap | `app.py` | Wires the desktop application. No domain policy. |
| Gateway | `server/` | Owns provider access and the agent loop. Has no workspace mount. |
| Executor (control plane) | `executor/` | Owns file tools, staging, checkpoints, baselines, manifests. Does not import desktop or gateway internals. |
| Runner | `runner/` | Runs Bash. Standard library only; imports nothing from the project. |
| Protocol | `shared/` | Framework-neutral. Must not import `src/` or `server/`. |
| Egress proxy | `egress/` | Standard-library CONNECT proxy for the opt-in overlay. |

## Process topology

```text
Desktop (app.py, src/, qml/)
  |  loopback HTTP, bearer token
  v
Gateway (server/)  --HTTPS-->  OpenRouter
  |  authenticated Unix socket (executor IPC)
  v
Executor (executor/)  -- read-only source mount, writable staging, state volume
  |  Unix socket (runner IPC)
  v
Runner (runner/)      -- /work (staging, exec allowed), /cache, socket dir only
```

- **Desktop** owns the UI, local SQLite history and settings, runtime management, approvals, and the **desktop publication broker**. It is the only component that writes to the selected host workspace.
- **Gateway** owns provider calls, the agent loop, budgets, sessions, the journal, permission rules, skills, and the gateway-side tools (`investigate_repository`, `update_plan`, `web_search`, `fetch_url`). It never touches the workspace or the executor's state.
- **Executor** serves the nine executor tools (`read`, `write`, `edit`, `apply_patch`, `bash`, `grep`, `find`, `ls`, `status`). It runs file tools, `status`, checkpoints, baselines, and manifests itself, and forwards every `bash` command to the runner. It has no network, runs as a non-root user with small fixed limits, and **cannot publish**.
- **Runner** runs one command at a time in its own process group, with `/bin/bash -lc`, under a read-only root filesystem, all capabilities dropped, and `no-new-privileges`. It sees only staging, a package cache, and its socket directory: no source mount, state volume, secrets, executor socket, or network. Four digest-pinned images (`runner-base`, `runner-python`, `runner-node`, `runner-polyglot`) provide toolchains. The desktop picks one from the project files unless Settings or the confirmed workspace policy names one; `/v1/status` reports what is installed.

## Modes

- **Chat**: no workspace tools.
- **Plan**: read-only tools against the source snapshot, plus `investigate_repository` and `update_plan`.
- **Agent**: every tool, with mutations confined to staging. Consecutive read-only executor calls in a turn run concurrently; mutations and gateway tools run serialized in call order.

Each Agent run also carries an approval tier (`prompt`, `sandbox`, `auto`; see [TOOLS.md](TOOLS.md#permission-matching)), a network mode (`none` or `registries`; see [EGRESS.md](EGRESS.md)), and a budget profile. Auto never survives a resume.

## Staging, checkpoints, and drift

- Staging is the mutable copy used by Agent mode. It is per workspace and lives in named volumes (`euesto-{work,state,cache}-<key>`, where the key is the first 12 hex digits of the workspace identity). It survives `compose down` and app restarts and is removed only by an explicit discard. A state volume claimed by another workspace identity is refused.
- Every mutation is checkpointed first. Failed, cancelled, or timed-out mutations roll back. Successful mutations stay staged until published.
- When staging resumes, host files changed since the baseline are absorbed into staging (a three-way rebase). A file changed on both sides is a **conflict**: staging keeps its version, `status` reports it, and it is held back from publication until the user keeps the staged or the host version.
- Sandbox git: at seed time the control plane clones host history (200 commits by default) into the work volume's `.git`, with no remotes, hooks, or credential helpers, and records the seeded tree as `refs/euesto/base`. `.git` is never tool-visible, checkpointed, or published, and checkpoint rollback does not rewind it.

## Agent loop and context

The gateway owns one versioned system prompt per mode (`server/agent/prompts.py`). Messages follow a cache-stable layout: the static prompt; the session context (user preferences, workspace instructions, the workspace `AGENTS.md` as untrusted data, selected skills, and a deterministic workspace snapshot), rebuilt only at session start or after compaction; then history; then per-run notes with budgets. Volatile values never precede cached content.

- **Result bounds:** each tool result is bounded on its own; a shortened result emits `tool.result_bounded`. Results are lean plain text (`server/agent/results.py`).
- **Full output:** every Bash command's complete output is written to `.euesto-output/` in staging (never published) and named in the result. Test, lint, and compiler output leads with a structured summary.
- **Compaction:** runs only at a high-water mark derived from the profile's working-set cap, the request budget, and the remaining window. The model writes a structured handoff; a deterministic summary is the fallback. Journaled as `context.compacted`.
- **Retries and pauses:** retryable provider failures are retried with jittered backoff. A final failure pauses the run (`provider_error`) so it can be resumed.
- **Budget wind-down:** at 85% of any budget the agent is asked to wrap up. When a budget is exhausted, a tool-free summary turn runs from a reserved slice, and the run pauses (`budget` or `no_progress`). The user can continue with a fresh budget of the same profile.
- **Redaction:** every tool result is scanned for high-confidence secrets before it enters history (`server/agent/redaction.py`, patterns in `shared/secret_scan.py`).
- **Journal:** run state, events, sessions, permission rules, and resumable snapshots persist in the gateway's SQLite journal (`server/journal/store.py`).

## Investigation delegation

`investigate_repository` opens a bounded nested loop on a separately configured model. The loop may call only `read`, `grep`, `find`, `ls`, and `status` through the parent's executor session. Its cost is charged to the parent run. Up to four calls are accepted per turn. Nested activity is journaled as `subagent.*`. The delegation grants no mutation, command, staging, or publication authority.

## Publication

Publication is a separate, desktop-authorized operation. The executor produces a manifest from its staging baseline; the desktop broker compares it with the host, obtains approval, validates workspace identity, baseline, and hashes, applies only path-bounded changes, and keeps a recovery record. Stale or conflicting manifests are rejected, not rebased. Details: [PUBLICATION.md](PUBLICATION.md).

## Workspace policy

`.euesto/policy.toml` in the host workspace declares the runner image, network mode and registry additions, setup command, named checks, extra secret patterns, and never-published paths (`shared/policy.py`). The desktop reads it from the host only. It takes effect only after the user confirms the exact reviewed file, and an invalid file fails closed.

## Security boundary

- The executor cannot publish, reach the network, or run commands. The runner cannot publish, reach the network, or see the source. The gateway cannot read the workspace.
- The source is mounted read-only and only in the executor. Agent mutations happen in staging.
- Path handling rejects absolute, drive, and UNC paths, traversal, Windows aliases and reserved names, non-canonical Unicode, secret-like paths, symlinks, and hard-linked files. Secret-like files (for example `.env*`, keys, certificates, `.netrc`, `kubeconfig`, `*.tfstate`) are never copied into staging.
- A symlink a command creates in staging is an *unpublishable link*: never followed by the file tools, never published, and reported by `status`.
- Dependency setup runs in a separate one-shot container, attached only to the allowlisted registry proxy after the user approves the command. It sees the dependency manifests, not the source tree. The agent's runner stays offline unless the session opted into `registries`.
- The documentation tools run in the gateway, never reach the executor or runner, and return bounded, untrusted text.

## Container hardening (summary)

Gateway and executor run as non-root (UIDs 10001 and 10002), read-only where possible, with dropped capabilities, `no-new-privileges`, resource limits, and `network_mode: none` by default. The egress overlay (`docker/compose.egress.yaml`) is never loaded by default. Compose hardening is asserted in `tests/docker/` and by the Docker tier; see [TESTING.md](TESTING.md) and [docker/README.container.md](../docker/README.container.md).
