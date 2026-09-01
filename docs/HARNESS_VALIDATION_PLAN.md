# Harness Validation Environment Plan

## Purpose

Make every documented Euesto validation check runnable from the coding harness and reproducible in CI, without provider credentials or unrestricted network access. The supported checks are:

- fast, slow, and Docker-marked pytest tiers;
- Ruff;
- Python compilation;
- QML linting and an optional offscreen QML smoke test;
- container policy, isolation, staging, and recovery checks;
- packaging checks where the host supports them.

The plan addresses the current failure mode where the harness does not contain `pytest` (`pytest: command not found`) and prevents a missing optional dependency from being confused with a failed validation.

## Current gaps

1. The repository declares Python development dependencies in `requirements-dev.txt`, but the harness image/session does not install them automatically.
2. QML linting is documented and used by GitHub Actions, but there is no repository-owned preflight command that verifies `pyside6-qmllint` availability, reports its version, or explains how to install it.
3. The validation commands are duplicated across README, contributor documentation, and workflows. They can drift and do not provide one local entry point.
4. The default pytest configuration intentionally excludes `slow` and `docker`; a complete validation run must explicitly execute all three tiers and handle pytest exit code 5 when a tier has no collected tests.
5. Container tests require Docker, Linux container features, and local fixture secrets/workspaces. They cannot be treated as ordinary unit tests or silently skipped in a purported full validation run.
6. Qt tests use offscreen/software-rendering environment variables in individual files and workflows rather than a single documented harness profile.
7. The executor’s developer-tool discovery list does not include `pyside6-qmllint`, so environment diagnostics cannot currently report QML tooling consistently.
8. The current dependency files use ranges for development dependencies. That is convenient for users but does not make a harness or CI run byte-for-byte reproducible.
9. The release workflow runs Docker-marked tests on Windows even though those tests require a Docker-capable environment; the supported behavior and failure reporting need to be made explicit.
10. There is no machine-readable validation result/report that distinguishes `passed`, `failed`, and `unavailable` checks without exposing environment secrets.

## Design constraints

- Unit and integration tests must remain credential-free and must not require provider network access.
- Container tests must continue to exercise the real security boundary: non-root execution, blocked egress, read-only source, writable staging, resource limits, and publication separation.
- Missing tools must be reported as `unavailable`, not silently skipped and not misreported as test failures.
- A command that is advertised as `full` must fail if a required check is unavailable, unless the caller explicitly selects an `allow-unavailable` policy.
- Preflight output must include versions and safe paths only; never dump the complete environment, tokens, headers, or arbitrary command output.
- Keep the existing public model-facing eight-tool vocabulary unchanged. Validation support is a harness/developer capability, not a new model-facing executor tool.

## Implementation phases

### Phase 1: Establish one validation entry point

Add `scripts/validate.py` (or an equivalent small module under `scripts/`) with these subcommands:

```text
python scripts/validate.py preflight
python scripts/validate.py fast
python scripts/validate.py slow
python scripts/validate.py docker
python scripts/validate.py qml
python scripts/validate.py all
```

Requirements:

- use `sys.executable -m pytest` and `python -m ruff` where possible, so the active virtual environment is unambiguous;
- run from the repository root and set `PYTHONPATH=.` consistently;
- set `QT_QPA_PLATFORM=offscreen` and `QT_QUICK_BACKEND=software` for Qt validation, without changing the application default;
- preserve subprocess exit codes and print the exact command being run;
- support `--json PATH` for a bounded machine-readable report;
- provide `--allow-unavailable` only for local diagnostic use;
- treat pytest exit code 5 as `unavailable`/`empty` for a selected tier, with a clear report, rather than an unexplained failure;
- never pass provider credentials or the caller’s unrestricted environment to subprocesses.

Refactor README, `docs/CONTRIBUTING.md`, `docs/TESTING.md`, and relevant workflow steps to call this entry point. Keep direct commands documented as implementation details where useful.

### Phase 2: Add safe preflight reporting

Implement a read-only preflight report that checks:

- Python executable, version, and platform;
- import availability and versions for PySide6, pytest, pytest-asyncio, pytest-timeout, Ruff, and Pillow;
- executable availability and version for `pyside6-qmllint`, Docker, Docker Compose, and PyInstaller;
- whether the required QML files exist;
- whether Docker is reachable without printing daemon configuration or credentials;
- whether the current directory is the repository root;
- whether Qt can initialize offscreen;
- whether the requested validation tier is supported on the current OS.

Use `shutil.which`, `importlib.metadata`, and bounded `subprocess.run(..., timeout=...)`. Return records shaped like:

```json
{
  "name": "pyside6-qmllint",
  "status": "available|unavailable|failed",
  "version": "6.x|unknown",
  "detail": "short safe explanation"
}
```

Add tests for available, missing, and failing commands using injected command runners; tests must not depend on the host having Docker or Qt.

### Phase 3: Make dependencies reproducible

Add a documented installation profile for the harness:

```text
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Then choose and implement one reproducibility strategy:

1. generate a hash-pinned development lock file from `requirements-dev.txt`, or
2. use a supported `pyproject.toml` dependency group plus a committed lock file.

The selected strategy must cover the complete Python test and QML toolchain. Verify that the chosen PySide6 version supplies `pyside6-qmllint`; if it does not, add the supported Qt tooling package or document the platform-specific package installation explicitly.

Add a CI/harness bootstrap script that:

- verifies Python 3.12;
- installs the locked dependencies;
- does not require provider credentials;
- does not use a broad user-site installation;
- emits dependency versions after installation.

Do not put development-only tools into the gateway or executor runtime images unless a security review finds a concrete need.

### Phase 4: Add a supported QML validation environment

Provide a Linux validation path matching CI, preferably a small developer/CI image or documented container profile containing:

- Python 3.12;
- the locked development dependencies;
- the system libraries needed by PySide6 offscreen operation, including the currently installed `libegl1` and any libraries discovered as required by the selected PySide6 wheel;
- a working `pyside6-qmllint` executable.

Add:

- a Dockerfile or Compose profile dedicated to validation, separate from the production gateway and executor images;
- a script that runs QML lint against the four primary components;
- an optional smoke test that loads `qml/Main.qml` offscreen and exits deterministically;
- clear `unavailable` reporting when the host lacks Qt rather than silently skipping the check.

Keep software rendering as a validation/diagnostic setting only. It must not become the normal desktop rendering default.

### Phase 5: Strengthen test-tier execution

Update pytest configuration and tests so all tiers are explicit and reliable:

- fast: `pytest -m "not slow and not docker"`;
- slow: `pytest -m "slow and not docker"`;
- container: `pytest -m docker`;
- complete non-container: `pytest -m "not docker"`.

Audit all tests for missing markers and classify them consistently. Add collection checks that fail if a test requiring Docker or a slow external process is unmarked. Ensure async tests use deterministic synchronization and do not depend on provider credentials or network access.

Add a test-count/collection report to the validation output. A full run must show the count for each tier and must not silently pass an empty required tier.

### Phase 6: Make Docker validation explicit and portable

Separate container validation from ordinary Python validation in CI and the local harness.

Requirements:

- preflight Docker and Compose before starting container tests;
- create temporary fixture secrets and a temporary workspace exactly as the existing workflow does;
- always tear down Compose profiles with a trap/finalizer;
- retain checks for non-root users, `network_mode: none`, blocked TCP/UDP/DNS egress, read-only source, writable staging, capability drops, no-new-privileges, resource limits, traversal/link rejection, and staging recovery;
- report Docker-unavailable distinctly from a container test failure;
- use a Linux runner for Docker tests;
- do not run Docker-marked tests in the Windows packaging job unless Docker Desktop availability is intentionally provisioned and tested;
- keep release packaging and security/container validation as separate jobs with clear dependencies.

Add a repository script for fixture setup and teardown so local and CI runs use the same safe setup rather than duplicated shell fragments.

### Phase 7: Align GitHub Actions with the local harness

Refactor `.github/workflows/test-containers.yml` to:

1. install the same locked development dependencies as the documented local harness;
2. run `preflight` and upload its bounded JSON report;
3. run fast, slow, Ruff, compile, and QML checks through the shared validation entry point;
4. run container checks in a dedicated Linux job;
5. upload JUnit/JSON results and logs with secrets filtered;
6. fail on unavailable required tools;
7. use cache keys derived from the lock file and Python/OS versions.

Update `.github/workflows/release.yml` so the Windows job consumes the shared non-container validation command and does not claim to validate Linux Docker isolation. Keep the packaged QML smoke test on Windows, where it is relevant.

Add a lightweight workflow or job that runs the preflight script on pull requests and reports missing optional tooling early.

### Phase 8: Add regression coverage for the harness itself

Add tests covering:

- missing `pytest`, Ruff, Docker, PyInstaller, and `pyside6-qmllint`;
- malformed or timed-out version commands;
- command exit-code propagation;
- pytest exit code 5 handling;
- safe JSON report content and absence of environment secrets;
- correct Qt environment variables for validation subprocesses;
- repository-root and required-file detection;
- lock-file/bootstrap consistency;
- all validation command construction and marker expressions;
- no-provider-credential operation;
- Docker fixture cleanup on success, failure, and cancellation.

Add a structural test that verifies the workflow invokes the shared validator and that documented commands do not drift from the validator’s command names.

## Documentation deliverables

Update:

- `README.md`: quick setup and one-command validation;
- `docs/CONTRIBUTING.md`: environment bootstrap, preflight interpretation, and tier commands;
- `docs/TESTING.md`: required versus optional checks and Docker/Qt prerequisites;
- `docs/TROUBLESHOOTING.md`: unavailable versus failed tool guidance;
- `docker/README.container.md`: validation image/profile and fixture workflow;
- `AGENTS.md`: the canonical validation entry point and the rule that missing dependencies must be reported explicitly;
- GitHub workflow comments and job names.

Keep `docs/HARNESS_QOL_PLAN.md` as the feature plan and use this document as the implementation plan for making its validation acceptance criteria executable.

## Acceptance criteria

The work is complete when, in a clean supported Python 3.12 environment:

1. `python scripts/validate.py preflight` reports every required tool and version without secrets.
2. `python scripts/validate.py all` runs fast, slow, QML, Ruff, compilation, and Docker checks, or reports Docker as an explicit unavailable tier when Docker is not installed.
3. `pytest`, Ruff, compileall, and QML lint are runnable after the documented bootstrap command.
4. QML lint uses the supported PySide6/Qt version and has a documented offscreen fallback/smoke path.
5. GitHub Actions uses the same validation commands and dependency source as local development.
6. Provider credentials and unrestricted network access are never required for unit, integration, lint, compile, or QML checks.
7. Container/security checks remain real checks and are not converted into silent skips.
8. Missing tools, failed tools, empty test tiers, and passed checks are distinguishable in terminal and JSON output.
9. New harness behavior has regression tests and all applicable GitHub Actions jobs pass.
