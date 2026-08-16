# Euesto

Euesto is a local-first Windows desktop chatbot with a contained OpenRouter agent. The desktop, gateway, executor, staging, and publication broker are separated so the agent can do useful coding work without receiving unrestricted host authority.

## Quick start — installed users

The Windows release requires:

- 64-bit Windows 10 or 11;
- Docker Desktop using Linux containers;
- internet access for the first runtime image download and for OpenRouter requests; and
- an OpenRouter account/API key.

Users do **not** need Python, Git, PowerShell, a source checkout, `venv`, or `pip`. Install the Windows package, launch Euesto, enter the OpenRouter key in **Settings → Connection**, and choose a workspace. Runtime setup happens in the background and Plan/Agent mode is enabled only after the exact workspace/executor identity is ready.

A portable ZIP is also supported. Docker Desktop is the required external runtime and cannot be bundled with Euesto.

## Product behavior

- **Chat** is a normal model conversation with no local workspace tools.
- **Plan** can inspect one selected workspace with read-only tools.
- **Agent** can inspect and modify an ephemeral staging copy, then request publication through the trusted desktop broker.
- Local SQLite stores conversation/settings data; credentials use the Windows credential store where supported.
- The gateway is loopback-authenticated and can reach OpenRouter but has no workspace mount.
- The executor is network-disabled, non-root, and has a read-only source mount plus bounded writable staging.

## Modes and permissions

| Mode | Local tools | Mutation | Host publication |
|---|---|---|---|
| Chat | none | none | none |
| Plan | `read`, `grep`, `find`, `ls` | no | no |
| Agent | all seven | staging only | desktop broker, after approval/validation |

The seven-tool public API is `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls`. Mode restrictions are enforced in code, not only in prompts. Agent Auto mode can reduce repeated prompts but does not grant network, host-path, shell, or publication authority to the executor.

## Example: editing a file

An Agent turn can use `read` to inspect a file, `edit` to replace an exact string, or `write` to create/replace a UTF-8 file. The mutation is applied to staging, checkpointed first. The agent then reports staged changes; publication is a separate approved operation. A failed mutation rolls its checkpoint back.

For large files, use bounded/incremental reads and localized edits instead of assuming an entire file can be returned in one model response.

## Publication lifecycle

```text
workspace snapshot
  -> ephemeral staging
  -> checkpointed mutations
  -> manifest + approval
  -> path/hash validation
  -> desktop publication broker
  -> host workspace + recovery state
```

The executor cannot publish. Manifests are tied to a workspace and source snapshot; stale manifests are rejected. See [docs/PUBLICATION.md](docs/PUBLICATION.md).

## Security and privacy

Euesto is **local-first, not offline**. The desktop, gateway, executor, staging, history, and publication broker run locally, but prompts and any workspace context or command output selected for an agent request can leave the computer through OpenRouter. Provider routing defaults to denying data collection and can request ZDR where the selected provider supports it.

The executor cannot access the network or publish to the host. The gateway cannot read the workspace. The selected source mount is read-only. Bash runs inside the isolated executor and is bounded by command, stdin/output, time, process, and environment controls.

Local SQLite data is not application-encrypted. Protect the Windows account and disk. Treat workspace contents, prompts, logs, and recovery files as local user data.

## Resource limits

The default `coding` profile uses 1 MB read/write/Bash-output limits, 2 MB edit target/result limits, a 300-second Bash timeout, 500 search/list results, 64 MiB grep scan budget, 300,000 staged files, 2.5 GB staging/checkpoint budgets, and 8 GB configured work capacity. Hard ceilings are higher where practical; requested values never bypass configured or hard limits. See [docs/LIMITS.md](docs/LIMITS.md).

## Recovery and reset

Unpublished work can be discarded through the staging controls without changing the host workspace. If publication is interrupted, use the desktop recovery state rather than replaying an old manifest. Stale-manifest errors require reseeding from the current workspace. Preserve recovery data before deleting local application data.

## Developer workflow

Contributors need Python 3.12 and Docker Desktop for container checks:

```powershell
git clone https://github.com/Logan-Summerlin/Euesto.git
cd Euesto
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Run the applicable checks:

```powershell
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests do not require provider credentials. Container/security checks are part of the CI workflow when container-related changes apply.

## Repository map

```text
app.py              Desktop entry point
src/                Desktop/runtime/publication code
server/             Gateway and agent runtime
executor/            Seven-tool executor, staging, checkpoints, limits
shared/              Framework-neutral tool/protocol structures
qml/                 Qt Quick UI
docker/              Container images, compose, security checks
tests/               Unit, integration, security, and contract tests
docs/                Authoritative architecture/operator/contributor docs
PROJECT_PLAN.md      Status-oriented roadmap
AGENTS.md            Durable agent invariants
CHANGELOG.md         Release/change summary
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — current components, boundaries, and data flow.
- [Tools](docs/TOOLS.md) — authoritative seven-tool reference.
- [Limits](docs/LIMITS.md) — effective defaults, hard ceilings, and precedence.
- [Publication](docs/PUBLICATION.md) — staging, approval, publication, conflicts, and recovery.
- [Contributing](docs/CONTRIBUTING.md) — development and safe tool changes.
- [Troubleshooting](docs/TROUBLESHOOTING.md) — runtime, workspace, staging, and publication failures.
- [Project roadmap](PROJECT_PLAN.md) — completed, active, planned, deferred, and non-goal status.
- [Container guidance](docker/README.container.md) — container setup and verification.

## Non-goals

No unrestricted host shell, mandatory cloud sync, browser-primary UI, document/RAG pipeline, media-generation suite, plugin marketplace, arbitrary MCP authority, or multi-agent orchestration. Keep the single-agent core small and auditable.
