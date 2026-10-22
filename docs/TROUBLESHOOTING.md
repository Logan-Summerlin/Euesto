# Troubleshooting

## Validation: unavailable versus failed

Run `python scripts/validate.py preflight --json validation-report.json`. **Unavailable** means a
required executable or dependency is absent (or a pytest tier collected no tests); install the
locked development dependencies or use `--allow-unavailable` only for diagnostics. **Failed** means
the tool ran and returned an error, timed out, or the environment violated a prerequisite and must
be investigated. A full validation run does not silently convert either result into a pass.

## Docker failures

Confirm Docker Desktop is running with Linux containers and that the required runtime images are available. Re-run the runtime setup after stopping stale containers. If a container starts but the app does not enable Plan/Agent, verify gateway health and exact executor/workspace identity.

## Workspace failures

Use one selected project directory rather than a drive root, user profile, credential directory, cloud-sync root, or Docker data directory. Recreate the executor for the exact workspace when its identity no longer matches. Check available work-volume capacity when large staging/checkpoint operations fail.

## Credential failures

The desktop stores the OpenRouter key in the Windows credential store. An executor credential is separate and is generated for the local runtime. Re-enter the provider key in Settings when provider calls fail; do not copy executor tokens into the provider credential field.

## Gateway failures

Check that the loopback gateway is healthy and that the desktop is using the current session credential. Provider failures can be caused by an invalid key, unavailable model, timeout, rate limit, or provider-side error. Unit tests do not require provider credentials.

## Executor failures

Check `/v1/status` and its reported workspace identity, snapshot identity, tool list, and effective limits. Executor authentication requires the current credential plus a fresh nonce. A Plan request for `write`, `edit`, `apply_patch`, `bash`, or `status` is intentionally rejected.

## Runner failures

Bash commands run in the runner container, not the executor. If every `bash` call fails while the file tools work, check that the runner is `Up (healthy)` and read its logs (`docker compose --profile agent logs runner`). A missing executable usually means the selected runner image lacks that toolchain: choose another image under **Settings → Runner** (`/v1/status` reports what is installed). Builds killed for memory or processes need higher runner limits in the same place, within the documented ceilings; never add a network, capabilities, or a source mount to the runner.

## Staging failures

A failed mutation should roll back its checkpoint. Staging persists per workspace across restarts and is rebased onto host edits automatically when a session resumes. A file changed both on the host and in staging is a conflict: `status` lists it with its base, host, and staged hashes, and it stays unpublishable until you keep the staged copy or take the host copy. If staging is inconsistent, discard staging and reseed from the current workspace. Resource failures can occur when staged bytes, checkpoint bytes, file count, or work-volume free space are exhausted. Symbolic links that commands create in staging are listed as unpublishable links and never block other tools or publication.

## Hard-linked files

Files with more than one hard link are rejected because a hard link lets a write through one path change content reachable through another, which path containment and hash validation cannot see. Staging seeding fails with `Unsupported source file: <path>` when the selected workspace contains one; `read`, `write`, `edit`, and `apply_patch` report that the target must be a regular, non-hard-linked file. The restriction applies to reading and mutation alike. To work around it, replace the link with an independent copy in the workspace (for example, copy the file to a temporary name and move it back over the original), or select a workspace that does not contain hard-linked files.

## Publication failures

Publication requires the correct workspace, current source snapshot, approval, valid paths, and hash-consistent staged content. Review the staged changes rather than retrying an old manifest blindly.

## Stale-manifest errors

A stale manifest means the publication baseline changed. Bring in the host changes (the rebase action, which resolves conflicts per file) and create a new manifest; discard and reseed only when the staged work is no longer wanted. Stale manifests are rejected by design.

## Investigation failures

- `investigation.call_limit`: more than four `investigate_repository` calls were attempted in one turn. Continue with the direct tools instead.
- `investigation.tool_not_permitted`: the nested loop tried a non-Plan tool; enforcement is in code, so this indicates the investigator model attempted an out-of-scope call.
- `investigation.failed`: the nested loop errored before gathering any evidence (for example an invalid key, unknown model ID, or provider outage that persisted through retries). The message includes the provider's error text. The parent is told to fall back to direct `read`/`grep`/`find`/`ls` use. A failed call still counts toward the four-call cap.
- Truncated or thin summaries: exploration stopped (`stop_reason` in the result) and the harness forced synthesis. `provider_error` means a provider failure cut exploration short; check the `provider_error` text. Re-run with a narrower query or investigate directly.
- Investigation model does not change: pick or type the model in Settings → Connection and press **Save investigation model**; the status bar confirms the saved ID.

## Resource-limit failures

The effective value is the minimum of the request, configured profile, and hard ceiling. See [LIMITS.md](LIMITS.md). A valid tool call can still fail if the shared staging/checkpoint resource model or Docker/container capacity is exhausted.

## Recovery/reset

To abandon unpublished Agent work, use the staging discard/recovery controls; kept staging for workspaces you no longer use is listed, with its size and age, under **Settings → Staged sessions**. To recover from a publication interruption, use the recovery state maintained by the desktop broker. Resetting local application data is a last resort because it can remove local history, settings, and credentials; preserve recovery data first when investigating a publication incident.

## Disappearing transcript text (Windows)

Euesto supports an opt-in scene-graph diagnostic. Start the application with
`EUESTO_RENDER_DIAGNOSTIC=software`; this sets `QSG_RHI_BACKEND=software` before
Qt initializes. Compare the same long, scrolled transcript with and without
this setting. If the issue only occurs on the normal backend, GPU/RHI glyph
cache pressure is a likely contributor. The software backend is diagnostic
only and is not the default because it can reduce UI performance.

Transcript rich text uses Qt native text rendering as the low-risk mitigation.
When reporting a remaining issue, record whether the delegate exists, its
HTML is populated, its geometry is non-zero, and whether software rendering
changes the result.
