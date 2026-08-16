# Troubleshooting

## Docker failures

Confirm Docker Desktop is running with Linux containers and that the required runtime images are available. Re-run the runtime setup after stopping stale containers. If a container starts but the app does not enable Plan/Agent, verify gateway health and exact executor/workspace identity.

## Workspace failures

Use one selected project directory rather than a drive root, user profile, credential directory, cloud-sync root, or Docker data directory. Recreate the executor for the exact workspace when its identity no longer matches. Check available work-volume capacity when large staging/checkpoint operations fail.

## Credential failures

The desktop stores the OpenRouter key in the Windows credential store. An executor credential is separate and is generated for the local runtime. Re-enter the provider key in Settings when provider calls fail; do not copy executor tokens into the provider credential field.

## Gateway failures

Check that the loopback gateway is healthy and that the desktop is using the current session credential. Provider failures can be caused by an invalid key, unavailable model, timeout, rate limit, or provider-side error. Unit tests do not require provider credentials.

## Executor failures

Check `/v1/status` and its reported workspace identity, snapshot identity, tool list, and effective limits. Executor authentication requires the current credential plus a fresh nonce. A Plan request for `write`, `edit`, or `bash` is intentionally rejected.

## Staging failures

A failed mutation should roll back its checkpoint. If staging is inconsistent, discard staging and reseed from the current workspace. Resource failures can occur when staged bytes, checkpoint bytes, file count, or work-volume headroom are exhausted.

## Publication failures

Publication requires the correct workspace, current source snapshot, approval, valid paths, and hash-consistent staged content. Review the staged changes rather than retrying an old manifest blindly.

## Stale-manifest errors

A stale manifest means the publication baseline changed. Discard/reseed staging against the current workspace, repeat the needed agent work, and create a new manifest. Stale manifests are rejected by design.

## Resource-limit failures

The effective value is the minimum of the request, configured profile, and hard ceiling. See [LIMITS.md](LIMITS.md). A valid tool call can still fail if the shared staging/checkpoint resource model or Docker/container capacity is exhausted.

## Recovery/reset

To abandon unpublished Agent work, use the staging discard/recovery controls. To recover from a publication interruption, use the recovery state maintained by the desktop broker. Resetting local application data is a last resort because it can remove local history, settings, and credentials; preserve recovery data first when investigating a publication incident.
