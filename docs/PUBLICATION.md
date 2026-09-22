# Publication and Recovery

Host publication is deliberately separate from agent execution. The executor can mutate only its ephemeral staging tree; the desktop publication broker is the only component that can write the selected host workspace.

## Lifecycle

1. **Workspace snapshot** — the executor records the selected workspace baseline, including file identity/hash information used for later comparisons.
2. **Staging** — Agent mode works against a writable staging copy while Plan reads the read-only source. Staging is scoped to the executor instance.
3. **Mutations** — `write`, `edit`, `patch`, and `bash` checkpoint before mutation (`patch` takes one checkpoint for all of its operations). Failed, cancelled, and timed-out mutations restore the checkpoint. Successful mutations remain unpublished.
4. **Review and manifest creation** — the agent (and the user) can inspect exactly what is pending with the read-only `status` tool, which compares staging with the publication baseline and returns bounded diffs. The executor then creates a manifest containing manifest/run/workspace identity, source snapshot identity, approval identity, publication batch identity, and path-bounded operations with staged hashes.
5. **Validation** — the desktop broker validates workspace identity, source baseline, paths, operation type, staged content hashes, modes, and publication invariants before touching the host.
6. **Approval** — the user/session authorization policy must authorize publication. Auto mode can remove repeated prompts but does not grant the executor host-write authority.
7. **Publication** — the trusted desktop broker applies the approved operations to the selected workspace and records recovery information.
8. **Recovery** — recovery copies/state allow the application to recover from an interrupted publication. Staging can also be discarded to abandon unpublished work.

## Binary files

Text files travel as UTF-8 `content`. Any changed file that is not valid UTF-8 (images, fonts, fixture databases, generated artifacts — including files written by `bash`) travels as `content_base64` instead. Exactly one of the two is present on every create/update; the staged hash is verified over the decoded bytes when the manifest is parsed and again after the broker writes the host file. Binary content is only copied, replaced, or deleted: no diff or line-based validation applies. Modes are published exactly as for text files.

## Large changesets: batched publication

The broker accepts at most 500 operations and 32,000,000 bytes per manifest. A larger changeset is split, in path order, into ordered **batches** that share one `publication_id` and carry `batch_index`/`batch_count`. Each batch is a complete manifest with the unchanged single-batch rules — exact path-set approval, per-file hash and mode validation, stale-baseline rejection — and each is approved separately (Auto sessions continue automatically).

1. Batch 1 is offered when the run completes. After it is published, the desktop sends the gateway a content-free **publication receipt** (paths, hashes, modes) and the executor advances the baseline for exactly those operations.
2. The desktop then asks for the next batch (`POST /v1/workspaces/{id}/staging/manifest` with the next `batch_index`). The executor recomputes what is still pending against the advanced baseline, so the next manifest never repeats published files.
3. Each batch is **all-or-nothing** on the host: if any operation fails, every file the batch already touched is restored from its recovery copy before the error is reported. Earlier batches stay published.
4. The desktop records every batch outcome (`published`, `failed`, `baseline_failed`) in a durable ledger under its recovery directory (`publications/<publication_id>.json`), and the failure message states how many batches are on the host and how many remain. Retrying re-runs the failed, already-reviewed batch; the remainder follows.

A single file larger than one batch cannot be published and is reported by name when the manifest is built.

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

The desktop broker (`src/workspace_broker.py`) enforces its own limits on top of manifest validation: at most 500 operations and 32,000,000 bytes of staged content (decoded bytes for binary files) per publication batch, exact path-set agreement with the user-approved path list, unique non-aliased relative paths, and rejection of host files whose current hash no longer matches the reviewed base hash. Workspaces must be ordinary directories nested below a drive root; drive roots, the user-profile root, and protected system/credential/cloud-sync directories are refused. Recovery copies are written outside the workspace before any host file is modified.
