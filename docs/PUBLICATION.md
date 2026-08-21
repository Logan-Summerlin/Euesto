# Publication and Recovery

Host publication is deliberately separate from agent execution. The executor can mutate only its ephemeral staging tree; the desktop publication broker is the only component that can write the selected host workspace.

## Lifecycle

1. **Workspace snapshot** — the executor records the selected workspace baseline, including file identity/hash information used for later comparisons.
2. **Staging** — Agent mode works against a writable staging copy while Plan reads the read-only source. Staging is scoped to the executor instance.
3. **Mutations** — `write`, `edit`, and `bash` checkpoint before mutation. Failed, cancelled, and timed-out mutations restore the checkpoint. Successful mutations remain unpublished.
4. **Manifest creation** — the executor creates a manifest containing manifest/run/workspace identity, source snapshot identity, approval identity, and path-bounded operations with staged hashes.
5. **Validation** — the desktop broker validates workspace identity, source baseline, paths, operation type, staged content hashes, modes, and publication invariants before touching the host.
6. **Approval** — the user/session authorization policy must authorize publication. Auto mode can remove repeated prompts but does not grant the executor host-write authority.
7. **Publication** — the trusted desktop broker applies the approved operations to the selected workspace and records recovery information.
8. **Recovery** — recovery copies/state allow the application to recover from an interrupted publication. Staging can also be discarded to abandon unpublished work.

## Stale manifests

A manifest is stale when its workspace identity or source snapshot no longer matches the executor's current baseline. Stale manifests are rejected. The system does not silently merge an old manifest into a changed workspace.

## Publication conflicts

A host file changed after the agent's snapshot, or a supplied optimistic concurrency hash no longer matches, is a conflict condition rather than permission to overwrite blindly. The safe response is to review the current workspace, discard/reseed staging when appropriate, and generate a new manifest from the current baseline.

## Important invariants

- The executor cannot publish.
- The gateway cannot write the workspace.
- Agent mutations remain staged until publication.
- Publication is path-bounded and hash-validated.
- A failed mutation is rolled back before control returns to the agent.
- A stale publication baseline is rejected.
- Discarding staging removes unpublished changes without changing the host workspace.

## Broker bounds

The desktop broker (`src/workspace_broker.py`) enforces its own limits on top of manifest validation: at most 500 operations and 32,000,000 bytes of staged content per publication, exact path-set agreement with the user-approved path list, unique non-aliased relative paths, and rejection of host files whose current hash no longer matches the reviewed base hash. Workspaces must be ordinary directories nested below a drive root; drive roots, the user-profile root, and protected system/credential/cloud-sync directories are refused. Recovery copies are written outside the workspace before any host file is modified.
