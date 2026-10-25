# Contributing

## Development setup

Use Python 3.12. Install development dependencies and create a local virtual environment. Docker Desktop using Linux containers is required for container integration checks. Provider credentials are not required for unit tests.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
```

`requirements-dev.lock` pins the validation toolchain; `python scripts/bootstrap.py` installs it into the active Python 3.12 interpreter and prints the installed versions.

## Required checks

The canonical validation entry point is `python scripts/validate.py`. Run `python scripts/validate.py preflight --json validation-report.json` to distinguish unavailable tools from failed checks (add `--require fast,docker` to make it a gate that fails), then `collect`, `fast`, `slow`, `docker`, `ruff`, `compile`, `qml`, `qml-smoke`, or `all` for explicit tiers. The individual commands are listed once, in [TESTING.md](TESTING.md#required-checks); run the ones that apply to the change.

Python code is formatted with `ruff format` (run `ruff format .` before committing); the `ruff` tier fails on unformatted files. Container changes should also be checked with the repository's container workflow/scripts. QML checks apply when QML or QML-facing interfaces change.

## Tool/schema synchronization

The thirteen public tools are defined in three places that must agree:

1. `shared/tools.py` — public names, request/result protocol, tool sets (`EXECUTOR_TOOLS`, `PLAN_TOOLS`, `READ_TOOLS`, `MUTATION_TOOLS`, `STAGED_EDIT_TOOLS`, `PARALLEL_SAFE_TOOLS`), and publish data structures;
2. `server/openrouter/agent.py` — model-facing JSON schemas and Plan/Agent tool selection;
3. `executor/app.py` — dispatch to the real implementations.

The nested investigation loop lives in `server/agent/investigation.py`; its Plan-tool allowlist is enforced in code there. `server/agent/runtime.py` runs the parent loop, and `server/agent/tool_calls.py` holds the tool-call parsing and result bounding both share. Permissions are enforced by `executor/permissions.py`; resource defaults and hard ceilings are in `executor/config.py`. Update tests when any schema, mode rule, limit, result field, or dispatch behavior changes.

## Safely modifying tools

- Keep the public vocabulary exactly the thirteen registered tools unless the project plan explicitly changes it.
- Do not add legacy aliases or compatibility wrappers for removed public tools.
- Keep Plan read-only in code, not just in prompts.
- Keep Agent mutations in staging; never give the executor host publication authority.
- Preserve path containment, link rejection, UTF-8 semantics, checkpoints, rollback, and hash validation.
- Keep Bash non-interactive, network-disabled, non-root, and bounded.
- Prefer localized edits and incremental reads for large files.
- If a limit changes, update the executor config, model schema where relevant, tests, and `docs/LIMITS.md` together.
- If a publication invariant changes, update `docs/PUBLICATION.md` and its tests.

## Runner images

`docker/Dockerfile.runner` pins every base image by digest. Release CI builds the four runner targets with an SBOM and provenance, scans them for HIGH and CRITICAL vulnerabilities, and records their digests in `release-images.json`. Rebuild cadence: refresh the pinned digests and toolchain versions at least monthly and in every release, and immediately when a scan reports a fixable HIGH or CRITICAL vulnerability.

The `Scan runtime images` workflow (`.github/workflows/vulnerability-scan.yml`) runs on every branch push, on every pull request, on `v*` release tags, in the weekly drift window, and by hand. It builds and publishes nothing, so it is safe on any of them: it scans the references of the newest *published* release manifest — the newest successful `Build Windows release` run that uploaded one, which can be older than the schema this checkout accepts (schema 2 published no egress proxy, schema 3 does) — and reports a notice instead of failing when no release has published a manifest yet. The image keys each schema declares therefore live in `scripts/release_image_refs.py`: an older manifest is scanned for the images it does publish, and a manifest whose `schema_version` is newer than that script knows fails the scan by name instead of silently leaving an image unscanned. Add a schema's new key there — and to `src/runtime_manager.py` — when the manifest grows.

## Testing without credentials

Unit tests should use fake provider responses or dependency injection. No test should require a live OpenRouter key. Container security tests should verify non-root execution, blocked egress, mount modes, resource limits, and executor/publication separation.

## Documentation

Authoritative references: `docs/ARCHITECTURE.md`, `docs/TOOLS.md`, `docs/LIMITS.md`, `docs/PUBLICATION.md`, `docs/EGRESS.md`, and `docs/TESTING.md`. `AGENTS.md` holds only durable invariants, `docs/ROADMAP.md` holds status, and living plans live in `docs/` (indexed in `docs/README.md`). Completed or superseded plans move to `archived-doc/`.
