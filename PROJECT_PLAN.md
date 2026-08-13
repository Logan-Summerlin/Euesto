# Local OpenRouter Chat — Project Plan

This is the canonical current architecture and roadmap. Historical implementation details that no longer affect behavior should not be preserved here.

## Status

Euesto is a Windows-oriented PySide6 Qt Quick/QML desktop backed by a local gateway and a separate network-disabled executor. Chat has no workspace tools. Plan and Agent use the same seven-tool executor vocabulary with different capability sets.

## Architecture

```text
MODEL
  |
  | read write edit bash grep find ls
  v
Gateway AgentRuntime
  |
  v
ExecutorService
  |
  +-- safe paths
  +-- staging
  +-- sandboxed bash
  +-- checkpoints
  |
  v
Desktop publish broker
```

The gateway has no workspace mount. The executor has no network and uses a read-only source mount plus ephemeral writable staging. The executor never publishes to the host.

## Agent tooling contract

### Plan

```text
read
grep
find
ls
```

Plan is read-only and operates against the selected source workspace.

### Agent

```text
read
write
edit
bash
grep
find
ls
```

Agent mutations operate only on ephemeral staging. Publication remains a separate desktop-authorized operation.

## Tool behavior

- `read`: bounded UTF-8 text reads with optional line ranges and hashes.
- `write`: create or replace one UTF-8 file. An expected SHA-256 may be supplied but is optional.
- `edit`: exact string replacement with occurrence validation. An expected SHA-256 may be supplied but is optional.
- `bash`: non-interactive `/bin/bash -lc` execution with bounded stdin/output/environment/time and process-group cleanup.
- `grep`: bounded content search.
- `find`: recursive bounded path discovery.
- `ls`: bounded immediate directory listing.

The model-facing JSON schemas must match these actual argument shapes exactly.

## Safety machinery

Reusable mutation logic lives below the tool layer. It includes SHA-256 hashing, checkpoint creation/rollback, suspicious whole-file shrink detection, bounded diffs, and mutation validation. These are implementation primitives, not agent tools.

Bash uses the same checkpoint/staging layer. A failed, cancelled, or timed-out command rolls staged filesystem changes back before returning control to the agent.

## Security requirements

- Workspace containment and path canonicalization.
- Symlink and hard-link rejection where mutation safety requires it.
- UTF-8-only text operations.
- Bounded files, bytes, output, results, processes, and execution time.
- Non-root executor user.
- No network access from the executor.
- Read-only executor root and source mount.
- Ephemeral writable staging.
- Dropped capabilities and `no-new-privileges`.
- No interactive TTY.
- Restricted command environment.
- Process-group cancellation and cleanup.
- Checkpoint rollback for failed mutations.
- Hash-checked host publication through the trusted desktop broker.

## Implementation phases

| Phase | Purpose |
|---|---|
| 1 | Establish the seven-tool protocol and registry. |
| 2 | Implement `read`, `grep`, `find`, `ls`, `write`, and `edit`. |
| 3 | Implement the real `bash` executor. |
| 4 | Make the seven tools the complete model-facing interface. |
| 5 | Delete superseded tool modules, compatibility code, tests, and stale documentation. |

Phase 5 is a hard cutover. There is one agent API; the old public vocabulary is not deprecated or supported.

## Definition of done

- Only the seven canonical tools are model-facing.
- No compatibility aliases or alternate tool profiles exist.
- Superseded executor modules are deleted.
- Useful staging/checkpoint infrastructure remains internal and reusable.
- Tests assert the new API rather than the removed API.
- Security tests cover filesystem containment, links, hashes, UTF-8, Bash isolation, timeout, cancellation, environment restrictions, process cleanup, and rollback.
- Repository documentation describes only the current seven-tool architecture.
- `pytest`, `ruff check .`, Python compilation, and applicable container/QML checks pass.

## Repository map

```text
executor/
  app.py
  checkpoints.py
  config.py
  errors.py
  mutations.py
  paths.py
  permissions.py
  staging.py
  tools/
    __init__.py
    read.py
    write.py
    edit.py
    bash.py
    grep.py
    find.py
    ls.py
shared/
  tools.py
server/
  agent/runtime.py
  openrouter/agent.py
```
