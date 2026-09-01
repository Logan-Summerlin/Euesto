# Contributing

## Development setup

Use Python 3.12. Install development dependencies and create a local virtual environment. Docker Desktop using Linux containers is required for container integration checks. Provider credentials are not required for unit tests.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

## Required checks

The canonical validation entry point is `python scripts/validate.py`. Bootstrap with `python -m venv .venv` and `python -m pip install -r requirements-dev.lock`, then run `python scripts/validate.py preflight --json validation-report.json` to distinguish unavailable tools from failed checks. Use `fast`, `slow`, `qml`, `docker`, or `all` for explicit tiers.

Run the repository checks that apply to the change:

```powershell
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Container changes should also be checked with the repository's container workflow/scripts. QML checks apply when QML or QML-facing interfaces change.

## Tool/schema synchronization

The eight public tools are defined in three places that must agree:

1. `shared/tools.py` — public names, request/result protocol, permission sets (`PLAN_TOOLS`, `MUTATION_TOOLS`, `READ_TOOLS`, `INVESTIGATION_TOOLS`), and publish data structures;
2. `server/openrouter/agent.py` — model-facing JSON schemas and Plan/Agent tool selection;
3. `executor/app.py` — dispatch to the real implementations.

The nested investigation loop lives in `server/agent/runtime.py`; its Plan-tool allowlist is enforced in code there. Permissions are enforced by `executor/permissions.py`; resource defaults and hard ceilings are in `executor/config.py`. Update tests when any schema, mode rule, limit, result field, or dispatch behavior changes.

## Safely modifying tools

- Keep the public vocabulary exactly the eight registered tools unless the project plan explicitly changes it.
- Do not add legacy aliases or compatibility wrappers for removed public tools.
- Keep Plan read-only in code, not just in prompts.
- Keep Agent mutations in staging; never give the executor host publication authority.
- Preserve path containment, link rejection, UTF-8 semantics, checkpoints, rollback, and hash validation.
- Keep Bash non-interactive, network-disabled, non-root, and bounded.
- Prefer localized edits and incremental reads for large files.
- If a limit changes, update the executor config, model schema where relevant, tests, and `docs/LIMITS.md` together.
- If a publication invariant changes, update `docs/PUBLICATION.md` and its tests.

## Testing without credentials

Unit tests should use fake provider responses or dependency injection. No test should require a live OpenRouter key. Container security tests should verify non-root execution, blocked egress, mount modes, resource limits, and executor/publication separation.

## Documentation

Use `docs/ARCHITECTURE.md`, `docs/TOOLS.md`, `docs/LIMITS.md`, and `docs/PUBLICATION.md` as authoritative references. `PROJECT_PLAN.md` is a status roadmap, and `AGENTS.md` contains only durable agent invariants.
