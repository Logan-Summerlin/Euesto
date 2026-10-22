# Package Egress: Setup Phase and Network Modes

The default executor and runner have **no network**: `docker/compose.yaml` runs both with `network_mode: none`, and the executor keeps it in every profile. Package registries are reachable in exactly two approved situations, both through the same allowlisting proxy:

- **The setup phase** (harness plan 5.1): a one-shot setup container installs a workspace's dependencies after the user approves the exact command. The agent's runner stays offline throughout.
- **The `registries` network mode** (5.3): a per-session opt-in that attaches the runner itself, for installing packages mid-run. It is offered only when the confirmed workspace policy sets `network.mode = "registries"`; in it Bash asks before every command unless a saved rule matches, and the Sandbox tier does not skip those prompts.

Neither ever gives a container a route to anything except the allowlisted registries.

## Threat model and goals

- The agent must not be able to exfiltrate workspace content or call arbitrary APIs.
- The runner must still be unable to reach the host, the gateway, the local network, or cloud metadata endpoints, and the executor stays without any network.
- The common real-world need — installing dependencies from PyPI (and npm) — should work.
- Every outbound decision must be auditable.
- The default posture must stay provably unchanged unless the operator opts in.

## Topology

```text
runner ──(egress-internal: internal network, no gateway/NAT)──► egress-proxy ──(egress-external)──► allowlisted registries
   no DNS upstream, fixed IP 172.31.250.10                 listens only on 172.31.250.2:3128
```

- `docker/compose.egress.yaml` is an **overlay**. Only when it is added with `--file` (for a setup run, or a session in the `registries` mode) does the runner drop `network_mode: none` (via Compose `!reset`, Docker Compose 2.24+) and join `egress-internal`. That network is `internal: true`: it has no gateway and no NAT, so the only reachable peer is the proxy. The executor only gains the proxy address as an environment value it passes into each command.
- The runner's embedded-DNS upstream is pointed at `127.0.0.1` (nothing listens there), so it cannot resolve — or tunnel data through — external names. It reaches the proxy by fixed address; with `HTTPS_PROXY` set, clients send `CONNECT host:443` and the proxy does the resolution.
- The proxy (`egress/proxy.py`, `docker/Dockerfile.egress-proxy`) is the only container on both networks. It binds only its internal address, publishes no ports, runs as UID 10003 with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and small PID/memory/CPU limits. It is standard-library Python with no third-party packages.
- The setup container (`docker/compose.setup.yaml`) joins `egress-internal` at the fixed address 172.31.250.20 with the same DNS restriction.
- The gateway is not on either egress network and is unaffected. `web_search` and `fetch_url` (`docs/TOOLS.md`) run in the gateway with their own checks and never use this proxy.

## Proxy policy

| Control | Behavior |
|---|---|
| Methods | Only `CONNECT host:port`. TLS stays end-to-end; plain HTTP and every other method are refused (405). |
| Hosts | Exact hostnames from `LOCAL_CHAT_EGRESS_ALLOWED_HOSTS` (default `pypi.org,files.pythonhosted.org,registry.npmjs.org`, plus the workspace policy's `network.allow_hosts`); `*.suffix` entries allow subdomains only. Matching is case-insensitive and ignores a trailing dot. |
| IP literals | Always refused, so the allowlist cannot be bypassed by address. |
| Resolution | Every resolved address must be publicly routable (`ipaddress.is_global`); names resolving to loopback, private, link-local, or reserved ranges are refused, so an allowlisted name cannot be pointed at internal services. |
| Ports | `LOCAL_CHAT_EGRESS_ALLOWED_PORTS` (default `443`). |
| Rate / concurrency | At most 32 concurrent tunnels (503) and 240 new tunnels per minute (429). |
| Tunnel bounds | 10 s to send headers (8 KiB max), 10 s upstream connect, 60 s idle timeout, 900 s lifetime, 2,000,000,000 bytes per tunnel. |
| Authentication | By topology: only the runner and the setup container share the internal network with the proxy, and the targets are public registries, so no credential is issued (a credential in the runner would be readable by the agent anyway). |

## Setup phase

Each workspace has a setup command: declared in the policy file (`[setup] command`), or detected from the lockfiles at the workspace root and confirmed by the user the first time it runs (`detect_setup_command` in `shared/policy.py`): `uv sync --frozen --no-install-project`, `poetry install --no-root` with an in-project environment, `pip install -r requirements.txt` into `.venv`, `pnpm install --frozen-lockfile`, `yarn install --frozen-lockfile`, `npm ci`, `cargo fetch`, and `go mod download`, joined with `&&` when a workspace has several ecosystems.

- **When it runs:** the desktop offers it when the workspace runtime first becomes ready, and again whenever a dependency manifest or lockfile in the host workspace has changed since the last successful setup (including after a publication that changed one). Every run shows the command, the mode, and the reachable registries, and runs only after approval. A declined offer is not repeated until the manifests change; *Install dependencies* in Settings → Policy runs it on demand.
- **How it runs:** the desktop adds `docker/compose.egress.yaml`, `docker/compose.setup.yaml`, and a generated mount overlay, then runs `docker compose … run --rm setup`. The setup container uses the workspace's runner image and identity (UID 10002), a read-only root filesystem, all capabilities dropped, `no-new-privileges`, the runner's resource limits, and `runner/setup.py` as its entry point: one `bash --noprofile --norc -o pipefail -c` command in its own process group, with a fixed environment (`PATH`, `HOME=/tmp/setup-home`, `CI=1`, package caches on `/cache`, `UV_PROJECT_ENVIRONMENT=/work/.venv`, in-project Poetry environments) plus the proxy variables. It holds no secrets and mounts neither `/source`, the state volume, nor any socket. The default timeout is 900 seconds (policy maximum 1,800); the command is at most 1,000 characters.
- **Isolated setup (the default):** `/work` is a private tmpfs holding only the host's dependency manifests and lockfiles (read-only binds) and the staging volume's `.venv` and `node_modules` directories (Compose volume subpaths, so it needs a recent Docker Engine and Compose release). Install scripts never see the source tree while the network is open.
- **Full setup:** projects that need their source during install (editable installs, workspace-local packages) can choose *Full setup…*, which mounts the whole staging volume; the confirmation warns that install scripts can then read the code. A failed isolated setup suggests it.
- **Afterwards:** dependencies persist in `.venv` and `node_modules` on the work volume and in the `cache-<id>` volume; both directories are excluded from publication and checkpoints. The proxy's audit lines are appended to the audit log, and the proxy is stopped and removed unless the session is in the `registries` mode. The agent's runner was never attached, so the agent loop stays offline.

## Network modes

`AgentRunRequest.network_mode` is `none` (the default) or `registries`. The desktop offers the *Registries* switch in Agent mode only when the confirmed workspace policy allows it, asks for confirmation, and restarts the runtime with the egress overlay; turning it off, or confirming a policy that no longer allows it, restarts the runtime without it. The gateway refuses a `registries` run unless the executor reports egress enabled, and it derives the network state for approvals from the executor's status rather than the request, so a runner that is attached is always treated as networked. In `registries` mode `executor/egress.py` adds `HTTPS_PROXY`/`HTTP_PROXY` (and lower-case and npm equivalents) to the fixed base environment of each command, `/v1/status` reports `environment.egress = {"enabled": true, "mode": "allowlisted_proxy", ...}`, the run notes name the reachable registries, every Bash command asks unless a saved rule matches, and the approval prompt states that the command can reach the registries.

Install into a virtual environment inside staging, for example `python -m venv .venv && .venv/bin/pip install -r requirements.txt`, or `npm install` with the `runner-node` or `runner-polyglot` image. `/work` allows exec in every profile, so console scripts and compiled extensions of installed packages run; the runner remains non-root with all capabilities dropped and `no-new-privileges`.

## Workspace allowlist additions

The default allowlist is `pypi.org`, `files.pythonhosted.org`, and `registry.npmjs.org`. A confirmed workspace policy may add only public package registries from a fixed list (`REGISTRY_ADDITIONS` in `shared/policy.py`): `crates.io`, `static.crates.io`, `index.crates.io`, `proxy.golang.org`, `sum.golang.org`, `repo.maven.apache.org`, and `repo1.maven.org`. Any other host makes the policy invalid, and an invalid policy never takes effect.

Broad hosts such as `github.com`, `raw.githubusercontent.com`, `gitlab.com`, or object-storage domains are refused on purpose: they serve arbitrary user-controlled content, so allowing one would give an install script (or the agent in `registries` mode) a channel to fetch unreviewed code and to send workspace content anywhere an attacker can write, which defeats a registry-only allowlist. Dependencies that only exist on such hosts should be vendored into the workspace or installed by the user on the host.

## Audit log

The proxy writes one JSON line per decision to stdout, retained by Docker's `json-file` driver (5 × 5 MB):

```json
{"event":"egress.allowed","reason":"allowed","method":"CONNECT","host":"pypi.org","port":443,"client":"172.31.250.10","bytes_up":1834,"bytes_down":90211,"duration_seconds":0.84,"outcome":"closed","timestamp":1790000000.0}
{"event":"egress.denied","reason":"host_not_allowed","method":"CONNECT","host":"example.com","port":443,"client":"172.31.250.10","duration_seconds":0.0,"timestamp":1790000001.0}
```

Denial reasons: `method_not_allowed`, `host_not_allowed`, `port_not_allowed`, `ip_literal`, `malformed_request`/`malformed_target`/`malformed_host`, `headers_too_large`, `non_public_address`, `resolution_failed`, `upstream_unreachable`, `rate_limited`, `too_many_connections`.

The desktop collects these lines after every setup run and appends them to `egress-audit.jsonl` in its data directory: only the known fields, deduplicated, keeping the newest 2,000 entries. Settings → Policy → *Network audit log* shows the most recent entries first. Developers can also run `docker compose --file docker/compose.yaml --file docker/compose.egress.yaml logs egress-proxy`.

## Release images and developer runtimes

Each release builds, signs, and publishes the proxy image (`docker/Dockerfile.egress-proxy`) with the other images, verifies the pull by digest, and pins it as `egress_proxy` in the release manifest (schema version 3, `docker/release-images.example.json`); an installed build refuses a manifest without a pinned proxy. Installed and developer desktop builds run the setup phase and the `registries` mode the same way; there is no environment switch that attaches the runner outside an approved session.

For script-managed developer runtimes, `./scripts/dev-up.ps1 -Workspace <path> -AllowlistedEgress` (or `docker compose --file docker/compose.yaml --file docker/compose.egress.yaml --profile agent up --detach --build`) starts the runtime with the overlay, which the gateway then treats as the `registries` mode.

## Residual risks

- A CONNECT tunnel is opaque TLS, so the proxy sees host and port, not URLs. Content served by an allowlisted registry (for example, a malicious package) is not inspected; installing packages is itself a code-execution decision the user approves through Bash.
- Registries behind shared CDNs could, in principle, be abused for domain fronting (a different `Host` inside TLS than the CONNECT target). Keep the allowlist minimal, and review the audit log for unexpected volumes.
- Package managers may send telemetry or metadata to allowlisted hosts; that traffic is limited to those hosts.

## Verification

- `tests/unit/egress/test_egress_proxy.py` — policy decisions, public-address resolution, a live tunnel through the proxy, audit entries for allowed/denied requests, rate limiting, Bash environment, the overlay only for the `registries` mode, and a check that `docker/compose.yaml` still renders the executor and runner with `network_mode: none` and no egress service, and that the overlay attaches only the runner.
- `tests/unit/desktop/test_setup_phase.py` — setup settings and policy validation, the setup container's Compose wiring (no `/source`, state, or socket), isolated and full mounts, the setup command and environment, and audit parsing and bounding.
- `tests/unit/desktop/test_policy_service.py` — the setup offer at first start and after a lockfile change, and the policy confirmation that gates the `registries` mode.
- `tests/docker/test_egress_profile.py` (container tier) — with the overlay, `pip install six` succeeds inside the runner through the proxy, `https://example.com` is refused and logged, a direct connection fails, isolated setup installs a package without seeing a source file while the agent's runner stays offline, and the default profile renders the executor and runner with `network_mode: none`.
