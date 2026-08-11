# Local OpenRouter Chat

A local-first Windows desktop chatbot with a contained OpenRouter agent mode. The app is
text-only, inexpensive to run, and designed so one person can understand its security boundary.

## User prerequisites

For the installed Windows release, the user needs:

- 64-bit Windows 10 or Windows 11;
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) configured for Linux
  containers (the WSL 2 backend is recommended);
- an internet connection the first time the app starts so Docker can download the digest-pinned gateway
  and executor images, plus access to OpenRouter when chatting; and
- an OpenRouter account and API key.

The release installer handles Python, the Python packages, the virtual environment, Docker Compose
commands, local gateway/executor session credentials, and image setup. Release users do not need
Python, Git, PowerShell, a source checkout, `venv`, or `pip`. Docker Desktop is the one required
runtime program that cannot be bundled with this application.

After installation, launch **Local OpenRouter Chat**, store the OpenRouter API key once in Settings,
and choose a workspace. The app starts Docker Desktop if necessary, downloads the runtime in the
background, and enables Plan/Agent mode only after the executor reports ready for that exact folder.

## Current status

v1.1 provides the v1.0 chatbot plus a Windows release path with a PyInstaller executable, a
per-user installer, digest-pinned GHCR gateway/executor images, and an in-app runtime manager.
The runtime manager creates session credentials, starts Docker Desktop when needed, pulls images in
the background, and gates workspace modes on exact executor readiness.

The application provides:

- a PySide6 Qt Quick/QML desktop with stable exact-height transcripts, buffered responses,
  cancellation, queueing, steering,
  editing, branches, search, import/export, model controls, usage, themes, tray controls, and a
  global quick-chat hotkey;
- Chat, read-only Plan, and Agent with prompt approval or session-scoped Auto;
- a loopback-only gateway for OpenRouter calls, model catalog caching, sessions, budgets,
  replayable events, generation-to-message links, branch-scoped activity replay, compaction, skills,
  and prompt commands;
- a network-disabled executor with one read-only workspace mount and an ephemeral staging copy;
- compact live executor context plus bounded metadata listing, batch reads, globbed search, and
  hash-checked whole-file or exact-replacement edits;
- exact prompt-mode approvals or opt-in Auto authorization, with all host writes still validated by
  the desktop publication broker; and
- local SQLite history, API keys, and optional manual gateway credentials; the release session token
  is generated in the app-data directory for Docker Compose.

One prompt is rendered as one assistant turn: a collapsed tool-name list stays inside the turn,
followed by the buffered final answer and usage/cost metadata. Tool arguments, request IDs, outputs,
and per-event token/context details are never sent to QML. Request-scoped provider routing denies
data collection by default and can require ZDR endpoints. Hosted web tools have separate policies.

The v0.7 extensibility review is complete. Arbitrary MCP discovery/credentials and multi-agent
orchestration are deliberately outside v1.0 because they would add authority, billing, and recovery
systems to a product whose goal is a small auditable single-agent core. Skills and declared custom
capabilities remain bounded and visible.

## Modes

| Mode | Local access | Mutation |
|---|---|---|
| Chat | None; only explicitly enabled OpenRouter-hosted web/time tools | None |
| Plan | One selected workspace through bounded list/read/search tools | None |
| Agent | One selected workspace through prompt-approved or Auto-authorized tools | Ephemeral staging, then broker-validated publish |

Auto is an Agent-only session toggle. It removes per-tool and publication prompts for otherwise
valid calls, requires clean staging, and stops after failures or interrupted runs. It does not add a
shell, network access, host paths, broader mounts, or exemptions from command/resource/hash limits.

Mode limits are enforced in code, not only in prompts.

## Architecture

```text
PySide6 QML desktop + runtime manager + trusted publish broker
  └─ authenticated HTTP on 127.0.0.1
       └─ gateway container: OpenRouter, agent loop, journal; no workspace mount
            └─ permissioned Unix socket
                 └─ executor: no network, read-only source, ephemeral writable staging
```

The gateway can reach the model provider but cannot read the workspace. The executor can read the
selected workspace but cannot reach the network or write to the host. Only the desktop broker can
publish an approved, path-bounded, hash-checked manifest.

In the installed release, the runtime manager controls Docker Desktop and Compose from a worker
thread. It generates the gateway/executor session files, pulls the digest-pinned images, and waits
for gateway health plus the exact executor/workspace identity before enabling Plan or Agent.

Local-first does not mean offline: prompts and selected context, file excerpts, search results,
and command output sent to a model leave the computer through OpenRouter. Local SQLite files are
not application-encrypted; rely on the Windows account and disk encryption for at-rest protection.

## Install and run on Windows

Download the `LocalOpenRouterChat-Windows-x64-Setup.exe` installer from a GitHub release, run it,
and launch the app from the Start menu or desktop shortcut. On first launch, open **Settings →
Connection** and store the OpenRouter API key. Then select **Workspace**. Runtime setup continues
in the app while Docker Desktop starts and the images download; no terminal window or gateway token
copy/paste is required.

The portable ZIP from the same release is also supported: extract it, run
`LocalOpenRouterChat.exe`, and follow the same first-launch flow. The installer is the recommended
option because it creates shortcuts and can be removed without touching conversations, API keys, or
runtime data.

Release maintainers must set both GHCR runtime packages to **public** before distributing an
installer. The release workflow verifies anonymous pulls so end users never need to sign in to a
container registry.

## Developer/source setup

The commands below are for contributors who are running the repository itself. They are not needed
by users of the Windows release.

Requirements: Python 3.12, Docker Desktop using Linux containers, and an OpenRouter API key.

```powershell
git clone https://github.com/Logan-Summerlin/Local-Chatbot.git
cd Local-Chatbot
py -3.12 -m venv .venv
\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
./scripts/dev-up.ps1
python app.py
```

The developer desktop reads the active local gateway session token. The startup script also copies
it to the clipboard as a manual fallback for **Settings → Connection**. Store the OpenRouter key in
the same panel.

For Plan or Agent mode in the developer workflow, recreate the services for exactly one project:

```powershell
./scripts/dev-down.ps1
./scripts/dev-up.ps1 -Workspace "C:\Projects\my-project"
python app.py
```

Select the same folder in the app. Do not select a drive root, user profile, credential folder,
cloud-sync root, or Docker data directory. Read
[docker/README.container.md](docker/README.container.md) before trusting Agent mode.

## Local data

| Data | Location |
|---|---|
| Conversations and settings | `%LOCALAPPDATA%\LocalOpenRouterChat\chat.sqlite3` |
| API key and optional manually saved gateway token | Windows Credential Manager |
| Active local gateway session token | `%LOCALAPPDATA%\LocalOpenRouterChat\gateway-session` |
| Gateway journal and model cache | Docker volume `gateway-data` |
| Global skills | `%LOCALAPPDATA%\LocalOpenRouterChat\skills` |
| Workspace skills | `<workspace>\.local-chat\skills` |
| Publish recovery copies | `%LOCALAPPDATA%\LocalOpenRouterChat\recovery` |

## Development

```powershell
python -m pip install -r requirements-dev.txt
pytest
ruff check .
python -m compileall -q app.py src server shared executor tests scripts
pyside6-qmllint qml/Main.qml qml/Sidebar.qml qml/Transcript.qml qml/Composer.qml
```

Unit tests require no live OpenRouter key. Build the Windows package with:

```powershell
pyinstaller --noconfirm --clean build\chatbot.spec
```

## Documentation

- [AGENTS.md](AGENTS.md): current architecture, invariants, and contributor instructions.
- [PROJECT_PLAN.md](PROJECT_PLAN.md): completed phases, architecture decisions, and release gates.
- [docker/README.container.md](docker/README.container.md): safe container setup and verification.

Do not add overlapping roadmap documents; update these files.

## Non-goals

No cloud accounts, mandatory sync, browser-primary UI, document/RAG pipeline, media generation,
invisible automation, unrestricted host tools, plugin marketplace, arbitrary MCP, or multi-agent
orchestration. Keep Chat free of local tools and extensions behind the contained single-agent core.
