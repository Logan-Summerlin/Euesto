# Publication and Recovery

Host publication is deliberately separate from agent execution. The executor and the runner can mutate only the workspace's persistent, disposable staging tree; the desktop publication broker is the only component that can write the selected host workspace.

## Lifecycle

1. **Workspace snapshot** — the executor records the selected workspace baseline, including file identity/hash information used for later comparisons.
2. **Staging** — Agent mode works against a writable staging copy while Plan reads the read-only source. Staging belongs to one workspace identity and persists across runs, restarts, and app sessions in that workspace's volumes (see "Persistent staging" below).
3. **Mutations** — `write`, `edit`, `apply_patch`, and `bash` checkpoint before mutation (`apply_patch` takes one checkpoint for all of its operations). Failed, cancelled, and timed-out mutations restore the checkpoint. Successful mutations remain unpublished.
4. **Review and manifest creation** — the agent (and the user) can inspect exactly what is pending with the read-only `status` tool, which compares staging with the publication baseline and returns bounded diffs. The executor then creates a manifest containing manifest/run/workspace identity, source snapshot identity, approval identity, publication batch identity, and path-bounded operations with staged hashes.
5. **Validation** — the desktop broker validates workspace identity, source baseline, paths, operation type, staged content hashes, modes, and publication invariants before touching the host.
6. **Approval** — the user/session authorization policy must authorize publication. Auto mode can remove repeated prompts but does not grant the executor host-write authority, and it stops for explicit confirmation at any batch with a host-executable, dependency, or workspace-policy change (see "Risk flags" below); the `sandbox` tier never auto-publishes, so every batch is still confirmed. The prompt lists flagged files first and warns when a file would write a recognizable secret into the workspace.
7. **Publication** — the trusted desktop broker applies the approved operations to the selected workspace and records recovery information.
8. **Recovery** — recovery copies/state allow the application to recover from an interrupted publication. Staging can also be discarded to abandon unpublished work.

Symlinks a command creates in staging are *unpublishable links*: `status` lists them, publication review mentions them, and manifests never include them.

## Persistent staging

Staging, the executor's state (workspace identity, publication baseline, open conflicts, checkpoints), and the package cache are per-workspace named volumes. `compose down` keeps them; only *Discard* (reseed staging from the workspace) or removing the staged session in Settings deletes them. On start, the executor claims its state volume: a volume holding another workspace's identity is refused, a matching one resumes with its baseline, and a missing baseline or missing work volume reseeds.

**Host drift.** When staging resumes, or when the user chooses *Bring in host changes*, the executor compares the host with the baseline and staging (`POST /v1/workspaces/{id}/staging/rebase`):

| Host since baseline | Staging since baseline | Result |
|---|---|---|
| changed | unchanged | Staging and the baseline take the host version (absorbed). |
| unchanged | changed | Staging keeps its version; it publishes as usual. |
| changed | changed identically | Absorbed into the baseline; nothing to publish. |
| changed | changed differently | **Conflict**: staging keeps its version and the baseline is unchanged. |

A conflict is listed by `status` and in the desktop with base, host, and staged hashes. It blocks publication of that file only: manifests leave it out, and the broker would reject its base hash anyway. The user resolves it per file by keeping the staged version (the host version becomes the base, so the staged file publishes over it after review) or taking the host version (staging and the baseline take the host file). The rebase body names resolutions as `{"keep_staged": [...], "take_host": [...]}`; any other field, or a path that is not a current conflict, is rejected.

**Publishing kept work.** Because staging persists, changes can outlive the run that made them. *Publish staged changes* (and *Review and publish* when leaving) asks the gateway for a manifest of what is pending (`POST /v1/workspaces/{id}/staging/publication`, refused while a run is active) and goes through the same approval, hash validation, batching, and baseline advance as a publication at the end of a run.

**Leaving a workspace.** Before the window closes or the workspace changes, the desktop inspects staging. With unpublished changes it offers *Review and publish*, *Discard*, *Keep for later* (the volumes keep the work for the next session with this workspace), or *Cancel*; dismissing the dialog cancels. Staged work is never deleted silently.

## Review pane and partial publication

*Review and select…* in Settings → Agent opens the review pane (`POST /v1/workspaces/{id}/staging/review`): every staged change with its risk flags, a bounded unified diff per file, a checkbox per file (all selected at first), the declared checks with their results, and the paths the workspace policy never publishes. Flagged files are listed first. *Publish selected* asks for a manifest of exactly the selected paths (`POST …/staging/publication` and `…/staging/manifest` accept an optional `paths` list); unselected changes stay staged and publish later. Broker validation is unchanged: exact path-set approval, hashes, and the baseline, which advances only for the published files. A refreshed review keeps the user's choices and selects only files that are new since the last review. Later batches of a partial publication stay within the same selection.

## Risk flags

Each manifest operation is classified before review (`shared/publication_risk.py`):

| Flag | Files |
|---|---|
| runs on the host (`host_executable`) | CI workflows and pipelines (`.github/workflows`, `.gitlab-ci.yml`, `.circleci`, `azure-pipelines.yml`, `Jenkinsfile`, …); git hook directories (`.husky`, `.githooks`); editor task and run configurations (`.vscode/tasks.json`, `.vscode/launch.json`, `.idea/runConfigurations`, `.devcontainer`); task runners and build backends (`Makefile`, `justfile`, `noxfile.py`, `tox.ini`, `setup.py`, `build.rs`, Gradle and Maven wrappers and build files, …); `conftest.py`, `.pre-commit-config.yaml`, `.envrc`, Dockerfiles and Compose files; a change to the `scripts` of `package.json` (or the build system and scripts of `pyproject.toml`; when the host file cannot be read, any change to either counts); and any file that gains an executable bit. |
| dependency | Dependency manifests and lockfiles (`package.json`, `requirements*.txt`, `pyproject.toml`, `uv.lock`, `poetry.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.toml`/`Cargo.lock`, `go.mod`/`go.sum`, Gradle and Maven manifests, …). |
| workspace policy (`policy`) | Anything under `.euesto/`. |
| binary | Content carried as `content_base64`. |
| large deletion | A deletion in a batch that deletes 20 or more files. |

Auto never publishes a batch with a host-executable, dependency, or policy flag without explicit confirmation: the desktop falls back to the reviewed confirmation prompt for that batch and resumes Auto afterwards. The same module scans text content for the high-confidence secret formats that redaction uses (`docs/TOOLS.md`) and the prompt warns, by kind and count only, before a secret is written into the workspace.

## Workspace policy

`.euesto/policy.toml` in the **host** workspace declares the runner image, the network mode and registry additions (`docs/EGRESS.md`), the setup command, named checks, extra secret patterns, and publication rules:

```toml
version = 1

[runner]
image = "python"                 # auto, base, python, node, or polyglot

[network]
mode = "none"                    # or "registries" to allow the per-session opt-in
allow_hosts = ["crates.io"]      # public registries from a fixed list only

[setup]
command = "uv sync --frozen --no-install-project"
isolated = true

[checks]
test = "python -m pytest -q"
lint = { command = "ruff check .", timeout_seconds = 120 }

[secrets]
patterns = ["*.vault"]

[publication]
never_publish = ["deploy/**"]
verify_before_publish = ["test"]
```

- **Host only:** the desktop reads the file from the host workspace, never from staging. An agent can propose a change only as an ordinary staged edit (flagged `policy`, so Auto stops for it), and it does nothing until it is published *and* confirmed.
- **Confirmation:** Settings → Policy shows the file's status (`absent`, `unconfirmed`, `confirmed`, `changed`, `invalid`, `removed`), the policy in effect, and the pending one. *Confirm policy* applies exactly the reviewed file (re-read and compared by digest); any later change, including removal, needs confirming again ("Policy changed, review"), and until then the previously confirmed policy stays in effect. A change to the runner image, registry additions, secret patterns, or never-publish globs restarts the runtime.
- **Fail closed:** an invalid file (unknown keys, wrong types, hosts outside the registry list, bad globs, a required check that is not declared) never takes effect and lists its errors.
- **Never publish:** `publication.never_publish` globs are enforced by the desktop broker itself, which refuses a manifest containing a matching path, and by the executor, which leaves matching paths out of manifests and lists them in the review. A workspace may add secret patterns but never remove the built-in secret names.

## Declared checks and verification before publication

The policy's `[checks]` table names check commands such as `test`, `lint`, and `typecheck`; the agent's workspace snapshot lists them. *Run* in the review pane runs one in the runner against the current staging (`POST /v1/workspaces/{id}/staging/checks`), like Bash with a checkpoint that is always restored, so a check never changes what would be published. Each result is tied to the fingerprint of the staged snapshot it ran against; any later mutation, or a changed check command, marks it stale. `publication.verify_before_publish` names the checks that must have passed against the current snapshot before an **Auto** publication: the publication worker re-reads the review and refuses the batch otherwise. A user-confirmed publication shows which required checks are not current but lets the user decide.

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

A host file changed after the agent's snapshot, or a supplied optimistic concurrency hash no longer matches, is a conflict condition rather than permission to overwrite blindly. The safe response is to bring in host changes (which absorbs what it can and lists true conflicts), resolve each conflict, and generate a new manifest from the current baseline; discarding and reseeding staging remains available.

## Important invariants

- The executor cannot publish.
- The gateway cannot write the workspace.
- Agent mutations remain staged until publication.
- Publication is path-bounded and hash-validated.
- A failed mutation is rolled back before control returns to the agent.
- A stale publication baseline is rejected.
- Discarding staging removes unpublished changes without changing the host workspace.
- Staging persists until it is published or explicitly discarded; it is never removed silently.
- A file changed on both sides is never published until the user resolves the conflict.
- A path the confirmed workspace policy never publishes is refused by the broker.
- Auto never publishes a host-executable, dependency, or policy change without explicit confirmation.

## Broker bounds

The desktop broker (`src/workspace_broker.py`) enforces its own limits on top of manifest validation: at most 500 operations and 32,000,000 bytes of staged content (decoded bytes for binary files) per publication batch, exact path-set agreement with the user-approved path list, unique non-aliased relative paths, and rejection of host files whose current hash no longer matches the reviewed base hash. Workspaces must be ordinary directories nested below a drive root; drive roots, the user-profile root, and protected system/credential/cloud-sync directories are refused. Recovery copies are written outside the workspace before any host file is modified.
