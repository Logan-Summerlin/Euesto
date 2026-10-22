# Safe container setup and operation

This is the developer/operator guide for a source checkout. Users of the Windows installer do not
need Python, a virtual environment, or these Docker commands; the installed desktop runs the same
bounded topology and manages its runtime in the background.

This guide runs the Euesto developer services with Docker Desktop on Windows. Chat uses only the
loopback gateway. Plan and Agent add two network-disabled containers: an executor (the control
plane) that sees one selected workspace read-only and writes only to a persistent, disposable
per-workspace staging volume, and a runner that runs Agent Bash commands against that staging copy
with no source mount, no secrets, and no state volume.

## Validation profile

Validation is separate from the production images. From the repository root, run
`python scripts/bootstrap.py`, then `python scripts/validate.py preflight` and
`python scripts/validate.py all`; the individual commands are listed once, in
[docs/TESTING.md](../docs/TESTING.md#required-checks). QML checks use PySide6's `pyside6-qmllint` with offscreen/software
settings; `python scripts/qml_smoke.py` is an optional deterministic startup check. Container
security checks must run on Linux with Docker and use disposable fixtures from
`scripts/docker-fixtures.sh`; the script removes its temporary workspace and Compose volumes on
success, failure, or cancellation. Missing Docker/Qt is reported as unavailable, never silently
skipped; use `--allow-unavailable` only for local diagnostics.

## 1. Prerequisites

- Windows 10/11 with a supported, fully patched Docker Desktop using Linux containers
- Python 3.12 and PowerShell 7 or Windows PowerShell
- A normal project folder nested below a projects directory
- An OpenRouter API key stored through the app

Do not select a drive root, your user-profile root, Windows, Program Files, AppData, a cloud-sync
root, a credential directory, or Docker's data directory. Keep Docker Desktop's WSL integration
limited to distributions that need it. Enhanced Container Isolation or a rootless compatible
runtime is recommended when available.

## 2. Install the desktop dependencies

```powershell
git clone https://github.com/Logan-Summerlin/Euesto.git
cd Euesto
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
```

Never put the OpenRouter key in `.env`, Compose environment values, the repository, or a
Dockerfile. The app stores it in Windows Credential Manager and sends it over authenticated
loopback to gateway memory.

## 3. Start Chat-only mode

```powershell
./scripts/dev-up.ps1
python app.py
```

The script creates separate 256-bit gateway and executor credentials under
`%LOCALAPPDATA%\\LocalOpenRouterChat\\gateway-session`. It starts only the gateway when no
workspace is supplied. The desktop reads the active local token automatically; the script also
copies it to the clipboard for manual entry in **Settings → Connection** if needed.

## 4. Start Plan and Agent safely

Stop any prior session, then name exactly one workspace:

```powershell
./scripts/dev-down.ps1
./scripts/dev-up.ps1 -Workspace "C:\\Projects\\my-project"
python app.py
```

In the desktop app, choose **Workspace**, select the same folder, and then choose **Plan** or
**Agent**. A workspace or mode mismatch fails closed; restart the containers to change projects.

The executor automatically omits standard local metadata, dependency, and cache directories from
its staging copy and staged publication review, including `.git`, `.hg`, `.svn`, `.venv`, `venv`,
`.tox`, `.nox`, `node_modules`, Python bytecode caches, and common test/type-checker caches. A plain
`env/` directory is ordinary source and is not excluded.
It supports up to 300,000 materialized regular files with a 2.5 GB staging budget (4 GB hard
ceiling) in the default `coding` profile. Staging lives on Docker named volumes
(`euesto-work-<key>`, `euesto-state-<key>`, `euesto-cache-<key>`, keyed by the workspace), so it uses
Docker's disk rather than memory. The executor is capped at 1 CPU, 1 GB of memory, and 128
processes; the runner defaults to 2 CPUs, 4 GB, and 1,024 processes in the dev scripts
(`LOCAL_CHAT_RUNNER_CPUS`, `LOCAL_CHAT_RUNNER_MEMORY`, `LOCAL_CHAT_RUNNER_PIDS`; the desktop sizes it
from **Settings → Runner**). Keep large datasets and generated artifacts outside the selected
workspace when they are not needed for the task.

Plan can list, read, and search bounded UTF-8 text. It cannot patch files or run commands under any
approval. Agent can create or replace files, make exact-string edits, run non-interactive Bash
commands inside the runner container, and delegate read-only repository investigations to a
configured cheaper model. Approving an edit or command lets it mutate files in staging only; it
does not permit a host write. Host publication requires a second approval showing the exact manifest.
See `docs/TOOLS.md` for the full ten-tool contract and `docs/LIMITS.md` for effective limits.

## 5. Verify the isolation before trusting it

Render and inspect the effective configuration:

```powershell
$env:LOCAL_CHAT_WORKSPACE = (Resolve-Path "C:\\Projects\\my-project").Path
$env:LOCAL_CHAT_WORKSPACE_ID = "verification-only"
$env:LOCAL_CHAT_SECRETS_DIR = "$env:LOCALAPPDATA\\LocalOpenRouterChat\\gateway-session"
docker compose -f docker/compose.yaml --profile agent config > "$env:TEMP\\local-chat-compose.yaml"
Select-String -Path "$env:TEMP\\local-chat-compose.yaml" -Pattern "network_mode: none|127.0.0.1|read_only: true|cap_drop|no-new-privileges"
```

Then check the running containers:

```powershell
docker compose -f docker/compose.yaml --profile agent ps
docker inspect local-openrouter-chat-executor-1 --format "{{json .HostConfig.NetworkMode}}"
docker inspect local-openrouter-chat-executor-1 --format "{{json .HostConfig.Binds}}"
docker inspect local-openrouter-chat-runner-1 --format "{{json .HostConfig.NetworkMode}}"
docker inspect local-openrouter-chat-runner-1 --format "{{json .Mounts}}"
docker inspect local-openrouter-chat-gateway-1 --format "{{json .HostConfig.Binds}}"
```

Expected results:

- the executor and runner network modes are `none`
- the only host workspace bind ends in `:/source:ro` and belongs to the executor
- the runner mounts only `/work`, `/cache`, and `/run/runner`: no `/source`, `/state`, secrets, or
  executor socket
- the gateway has no workspace bind
- no service mounts `/var/run/docker.sock`
- every service is non-root, read-only, capability-dropped, and no-new-privileges
- the executor and runner eventually report `Up (healthy)` once their sockets are ready
- only the gateway publishes `127.0.0.1:8765`; the executor and runner publish no port

Do not add `privileged`, host networking, writable source mounts, devices, Docker socket mounts,
added capabilities, or unconfined security profiles to “fix” a launch problem.

## 5a. Optional: allowlisted package installs (prototype)

The default executor and runner have no network. To let `pip install` reach PyPI (and npm reach
its registry) from Agent commands without opening anything else, add the egress overlay:

```powershell
.\scripts\dev-up.ps1 -Workspace "C:\Users\you\Projects\example" -AllowlistedEgress
```

The runner (never the executor) then joins an internal-only Docker network whose only other member is a CONNECT proxy
that allows `pypi.org`, `files.pythonhosted.org`, and `registry.npmjs.org` (override with
`LOCAL_CHAT_EGRESS_ALLOWED_HOSTS`), refuses IP literals and non-public resolutions, and writes a JSON
audit line per request (`docker compose --file docker\compose.yaml --file docker\compose.egress.yaml logs egress-proxy`).
Requires Docker Compose 2.24 or later. See `docs/EGRESS.md` for the design, limits, and residual risks.

## 6. Approval and recovery rules

- Read every command, working directory, timeout, and mutation warning.
- Agent commands run as non-interactive `bash -lc` inside the network-disabled runner container,
  never on the host; neither the runner nor the executor has host shell, SSH, or Docker access, and
  the runner cannot see the source mount, secret-like files, or the executor's state.
- Treat repository `AGENTS.md`, source text, tool output, and model output as untrusted.
- Review every path in the publish manifest. Publication fails if a host file changed after review.
- Successful publications create recovery copies in
  `%LOCALAPPDATA%\\LocalOpenRouterChat\\recovery`.
- Stop immediately if a request mentions credentials, neighboring folders, enabling network, or
  weakening a mount. Those changes are outside the agent's authority.

## 7. Stop and clean up

```powershell
./scripts/dev-down.ps1
```

This stops the containers and deletes both active session-token files. Staged work is kept: the
per-workspace `euesto-work-<key>`, `euesto-state-<key>`, and `euesto-cache-<key>` volumes survive
`down`, and the next session for the same workspace resumes from them, rebasing automatically onto
any host edits made in between. Unpublished staging is discarded only by an explicit **Discard**
(in the app, or under **Settings → Staged sessions**, which flags sessions unused for longer than
the retention period but never removes them on its own). To list or remove them by hand after
stopping:

```powershell
docker volume ls --filter label=io.euesto.workspace
docker volume rm euesto-work-<key> euesto-state-<key> euesto-cache-<key>
```

The gateway journal volume and desktop conversation database also remain. To remove the gateway
journal after stopping:

```powershell
docker volume rm local-openrouter-chat_gateway-data
```

Both deletions are irreversible and are not required for normal use.

## 8. Troubleshooting without weakening security

- **Agent unavailable:** restart with `-Workspace`, then select the identical canonical folder.
- **Executor exits during startup:** inspect `docker compose --profile agent logs executor`; the
  most common causes are a workspace exceeding 300,000 materialized files or the 2.5 GB staging budget, an insufficient
  Docker Desktop memory allocation, or an unreadable source file. Remove unnecessary generated data
  or select a narrower project folder.
- **Workspace identity mismatch:** run `dev-down.ps1`; do not reuse the old executor. Kept staging
  for each workspace lives in its own volumes and is never mounted for another workspace.
- **Socket unavailable:** inspect `docker compose --profile agent ps` and the executor and runner
  logs; recreate the services instead of adding a network.
- **Permission denied:** approve the exact supported operation or change the task. Do not make a
  global wildcard rule.
- **File conflict:** a file changed on the host and in staging. Review both versions, then choose
  per path whether to keep the staged copy or take the host copy; nothing publishes until every
  conflict is resolved.
- **Docker fails to mount the folder:** enable file sharing for that specific projects directory;
  never broaden the selection to a drive or profile root.

Containerization limits the model-controlled process; it does not protect against a compromised
Docker daemon, Windows administrator, kernel, hypervisor, malicious image, or physical access to an
unlocked laptop. Keep the runtime and images patched and review release checksums.
