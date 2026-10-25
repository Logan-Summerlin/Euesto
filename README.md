# Euesto

Euesto is a local-first Windows desktop chatbot with a contained OpenRouter agent. The desktop, gateway, executor, runner, staging, and publication broker are separate components, so the agent can do real coding work without unrestricted host authority.

## Install and run (Windows users)

Requirements:

- 64-bit Windows 10 or 11;
- Docker Desktop using Linux containers (it cannot be bundled with Euesto);
- internet access for the first runtime image download and for OpenRouter requests; and
- an OpenRouter API key.

You do not need Python, Git, PowerShell, or a source checkout. Install the Windows package (or the portable ZIP), enter the key in **Settings → Connection**, and choose a workspace. Runtime setup runs in the background; Plan and Agent modes are enabled once the workspace and executor identity are ready.

## Modes

| Mode | Local tools | Mutation | Host publication |
|---|---|---|---|
| Chat | none | none | none |
| Plan | `read`, `grep`, `find`, `ls`, `investigate_repository`, `update_plan` | none | none |
| Agent | all thirteen (web tools when enabled) | staging only | desktop broker, after approval and validation |

- **Agent** edits a persistent, disposable staging copy of the workspace. Unpublished staging survives restarts and is removed only by an explicit discard.
- **Approval tiers** (Agent): `prompt` asks for every change; `sandbox` runs edits and Bash without prompts while the runner is offline, and every publication is still reviewed; `auto` allows valid calls and stops for confirmation at risk-flagged publications. No tier grants network, host-path, or publication authority to the executor or runner.
- **`investigate_repository`** runs a bounded, read-only investigation on a separately configured model. It is capped at four calls per turn and debits the parent run's budget.
- **`web_search` and `fetch_url`** are off by default and can be enabled per workspace for Agent sessions. `fetch_url` asks before reading any URL that is not on its documentation allowlist.
- **Workspace policy** (`.euesto/policy.toml`) can declare the runner image, network mode, setup command, checks, extra secret names, and never-published paths. It applies only after you confirm the reviewed file in Settings.

The public tool contract is in [docs/TOOLS.md](docs/TOOLS.md).

## Security and privacy

Euesto is **local-first, not offline**. Prompts, plus any workspace context or command output an agent request includes, leave the computer through OpenRouter. Provider routing denies data collection by default and requests zero-data-retention where the provider supports it.

- The executor and the runner have no network and cannot publish to the host.
- The gateway can reach OpenRouter but cannot read the workspace.
- The source mount is read-only and mounted only in the executor. Secret-like files are never copied into staging.
- Bash runs in the isolated runner and is bounded by command, stdin and output size, time, processes, and environment.
- Dependency setup runs in an approved one-shot container that reaches only allowlisted registries through an audited proxy. The default keeps every container network-free ([docs/EGRESS.md](docs/EGRESS.md)).
- Local SQLite data is not application-encrypted. Protect the Windows account and disk, and treat workspace contents, prompts, logs, and recovery files as personal data.

## Publication

```text
workspace snapshot -> staging (rebased onto host edits on resume) -> checkpointed mutations
  -> manifest + approval -> path and hash validation -> desktop broker -> host workspace
```

The executor cannot publish. Manifests are bound to a workspace and a source snapshot; stale manifests are rejected, and the fix is to reseed from the current workspace. See [docs/PUBLICATION.md](docs/PUBLICATION.md).

## Resource limits

The default `coding` profile allows 1 MB for read, write, and Bash output; 2 MB for edit targets and results; a 300-second Bash timeout; 500 search/list results per call; a 64 MiB grep scan budget; 300,000 staged files; 2.5 GB of staging and checkpoints; and 8 GB of configured work capacity. Requested values never bypass configured or hard limits. Authoritative values are in [docs/LIMITS.md](docs/LIMITS.md).

## Recovery

Unpublished work can be discarded through the staging controls without touching the host workspace. If publication is interrupted, use the desktop recovery state; do not replay an old manifest. Preserve recovery data before deleting local application data. See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Developer setup

Contributors need Python 3.12 and, for container checks, Docker Desktop.

```powershell
git clone https://github.com/Logan-Summerlin/Euesto.git
cd Euesto
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
python scripts/validate.py fast
```

- Run the checks that apply to your change: `python scripts/validate.py <tier>` (`preflight`, `collect`, `fast`, `slow`, `docker`, `ruff`, `compile`, `qml`, `qml-smoke`, `all`). The commands are listed once in [docs/TESTING.md](docs/TESTING.md#required-checks).
- Unit tests need no provider credentials.
- Before contributing, read [AGENTS.md](AGENTS.md) (invariants and the repository map) and [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

## Documentation

| Document | Covers |
|---|---|
| [docs/README.md](docs/README.md) | Documentation index, reading order, and naming rules. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, ownership, data flow, and the security boundary. |
| [docs/TOOLS.md](docs/TOOLS.md) | The thirteen tools and their error codes. |
| [docs/LIMITS.md](docs/LIMITS.md) | Defaults, hard ceilings, and precedence. |
| [docs/PUBLICATION.md](docs/PUBLICATION.md) | Staging, approval, publication, conflicts, and recovery. |
| [docs/EGRESS.md](docs/EGRESS.md) | Network modes, dependency setup, and the registry proxy. |
| [docs/TESTING.md](docs/TESTING.md) | Test tiers, required checks, and CI. |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Development rules and safe tool changes. |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Runtime, workspace, staging, and publication failures. |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Completed, active, planned, deferred, and non-goal status. |
| [docker/README.container.md](docker/README.container.md) | Container setup and verification. |
| [CHANGELOG.md](CHANGELOG.md) | Release history. |

## Non-goals

No unrestricted host shell, mandatory cloud sync, browser-primary UI, document or RAG pipeline, media-generation suite, plugin marketplace, arbitrary MCP authority, or multi-agent orchestration. The single-agent core stays small and auditable.
