# Safe container setup and operation

This is the developer/operator guide for a source checkout. Users of the Windows installer do not
need Python, a virtual environment, or these Docker commands; the installed desktop runs the same
bounded topology and manages its runtime in the background.

This guide runs Local OpenRouter Chat v1.1 developer services with Docker Desktop on Windows. Chat uses only the
loopback gateway. Plan and Agent add a separate executor that has no network, sees one selected
workspace read-only, and writes only to an ephemeral in-container staging area.

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
git clone https://github.com/Logan-Summerlin/Local-Chatbot.git
cd Local-Chatbot
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
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
its staging copy and staged publication review, including `.git`, `.venv`, `venv`, `env`,
`node_modules`, Python bytecode caches, and common test/type-checker caches. It supports up
to 300,000 materialized regular files and 2,000,000,000 bytes (2 GB decimal). The ephemeral
staging filesystem is capped at 2 GB and the executor at 3 GB of memory; Docker Desktop must have
enough memory allocated for the selected project. Keep large datasets and generated artifacts
outside the selected workspace when they are not needed for the task.

Plan can list, read, and search bounded UTF-8 text. It cannot patch files or run commands under any
approval. Agent can request typed text edits and non-shell executables. Approving a command lets it
mutate any file in ephemeral staging; it does not permit a host write. Host publication requires a
second approval showing the exact manifest.

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
docker inspect local-openrouter-chat-gateway-1 --format "{{json .HostConfig.Binds}}"
```

Expected results:

- the executor network mode is `none`
- the only host workspace bind ends in `:/source:ro` and belongs to the executor
- the gateway has no workspace bind
- neither service mounts `/var/run/docker.sock`
- both services are non-root, read-only, capability-dropped, and no-new-privileges
- the executor eventually reports `Up (healthy)` after its staging socket is ready
- only the gateway publishes `127.0.0.1:8765`; the executor publishes no port

Do not add `privileged`, host networking, writable source mounts, devices, Docker socket mounts,
added capabilities, or unconfined security profiles to “fix” a launch problem.

## 6. Approval and recovery rules

- Read every executable, argument, working directory, timeout, and mutation warning.
- Shells, PowerShell, SSH, Docker, and other host-control programs are rejected by the executor.
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

This stops the containers and deletes both active session-token files. The executor's `/work`
staging area is tmpfs and disappears with the container. The gateway journal volume and desktop
conversation database remain. To remove the gateway journal after stopping:

```powershell
docker volume rm local-openrouter-chat_gateway-data
```

That deletion is irreversible and is not required for normal use.

## 8. Troubleshooting without weakening security

- **Agent unavailable:** restart with `-Workspace`, then select the identical canonical folder.
- **Executor exits during startup:** inspect `docker compose --profile agent logs executor`; the
  most common causes are a workspace exceeding 300,000 materialized files or 2 GB, an insufficient
  Docker Desktop memory allocation, or an unreadable source file. Remove unnecessary generated data
  or select a narrower project folder.
- **Workspace identity mismatch:** run `dev-down.ps1`; do not reuse the old executor.
- **Socket unavailable:** inspect `docker compose --profile agent ps` and executor logs; recreate
  the services instead of adding a network.
- **Permission denied:** approve the exact supported operation or change the task. Do not make a
  global wildcard rule.
- **File conflict:** review the live host edit, then start a new run from a fresh snapshot.
- **Docker fails to mount the folder:** enable file sharing for that specific projects directory;
  never broaden the selection to a drive or profile root.

Containerization limits the model-controlled process; it does not protect against a compromised
Docker daemon, Windows administrator, kernel, hypervisor, malicious image, or physical access to an
unlocked laptop. Keep the runtime and images patched and review release checksums.
