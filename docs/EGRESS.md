# Allowlisted Package Egress (opt-in prototype)

The default executor has **no network**: `docker/compose.yaml` runs it with `network_mode: none`, and nothing in this document changes that default. This page describes an **opt-in** profile that lets `pip install` / `npm install` reach an explicit allowlist of package registries through a proxy, without giving the executor a route to anything else.

## Threat model and goals

- The agent must not be able to exfiltrate workspace content or call arbitrary APIs.
- The executor must still be unable to reach the host, the gateway, the local network, or cloud metadata endpoints.
- The common real-world need — installing dependencies from PyPI (and npm) — should work.
- Every outbound decision must be auditable.
- The default posture must stay provably unchanged unless the operator opts in.

## Topology

```text
executor ──(egress-internal: internal network, no gateway/NAT)──► egress-proxy ──(egress-external)──► allowlisted registries
   no DNS upstream, fixed IP 172.31.250.10                   listens only on 172.31.250.2:3128
```

- `docker/compose.egress.yaml` is an **overlay**. Only when it is added with `--file` does the executor drop `network_mode: none` (via Compose `!reset`, Docker Compose 2.24+) and join `egress-internal`. That network is `internal: true`: it has no gateway and no NAT, so the only reachable peer is the proxy.
- The executor's embedded-DNS upstream is pointed at `127.0.0.1` (nothing listens there), so it cannot resolve — or tunnel data through — external names. It reaches the proxy by fixed address; with `HTTPS_PROXY` set, clients send `CONNECT host:443` and the proxy does the resolution.
- The proxy (`egress/proxy.py`, `docker/Dockerfile.egress-proxy`) is the only container on both networks. It binds only its internal address, publishes no ports, runs as UID 10003 with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and small PID/memory/CPU limits. It is standard-library Python with no third-party packages.
- The gateway is not on either egress network and is unaffected.

## Proxy policy

| Control | Behavior |
|---|---|
| Methods | Only `CONNECT host:port`. TLS stays end-to-end; plain HTTP and every other method are refused (405). |
| Hosts | Exact hostnames from `LOCAL_CHAT_EGRESS_ALLOWED_HOSTS` (default `pypi.org,files.pythonhosted.org,registry.npmjs.org`); `*.suffix` entries allow subdomains only. Matching is case-insensitive and ignores a trailing dot. |
| IP literals | Always refused, so the allowlist cannot be bypassed by address. |
| Resolution | Every resolved address must be publicly routable (`ipaddress.is_global`); names resolving to loopback, private, link-local, or reserved ranges are refused, so an allowlisted name cannot be pointed at internal services. |
| Ports | `LOCAL_CHAT_EGRESS_ALLOWED_PORTS` (default `443`). |
| Rate / concurrency | At most 32 concurrent tunnels (503) and 240 new tunnels per minute (429). |
| Tunnel bounds | 10 s to send headers (8 KiB max), 10 s upstream connect, 60 s idle timeout, 900 s lifetime, 2,000,000,000 bytes per tunnel. |
| Authentication | By topology: only the executor shares the internal network with the proxy, and the targets are public registries, so no credential is issued (a credential in the executor would be readable by the agent anyway). |

## Audit log

The proxy writes one JSON line per decision to stdout, retained by Docker's `json-file` driver (5 × 5 MB):

```json
{"event":"egress.allowed","reason":"allowed","method":"CONNECT","host":"pypi.org","port":443,"client":"172.31.250.10","bytes_up":1834,"bytes_down":90211,"duration_seconds":0.84,"outcome":"closed","timestamp":1790000000.0}
{"event":"egress.denied","reason":"host_not_allowed","method":"CONNECT","host":"example.com","port":443,"client":"172.31.250.10","duration_seconds":0.0,"timestamp":1790000001.0}
```

Denial reasons: `method_not_allowed`, `host_not_allowed`, `port_not_allowed`, `ip_literal`, `malformed_request`/`malformed_target`/`malformed_host`, `headers_too_large`, `non_public_address`, `resolution_failed`, `upstream_unreachable`, `rate_limited`, `too_many_connections`. Inspect with `docker compose --file docker/compose.yaml --file docker/compose.egress.yaml logs egress-proxy`.

## Executor integration

When the overlay sets `LOCAL_CHAT_EGRESS_PROXY`, `executor/egress.py` adds `HTTPS_PROXY`/`HTTP_PROXY` (and lower-case and npm equivalents) to Bash's fixed base environment, and `/v1/status` reports `environment.egress = {"enabled": true, "mode": "allowlisted_proxy", ...}`. The agent's runtime context tells the model which registries are reachable. Without the overlay the variables are absent and status reports `{"enabled": false, "mode": "none"}`.

Install into a virtual environment inside staging, for example `python -m venv .venv && .venv/bin/pip install -r requirements.txt`. `.venv` is excluded from staging publication and checkpoints. The default profile mounts `/work` `noexec` (Docker's tmpfs default); this profile alone remounts it with `exec` so console scripts and compiled extensions of installed packages can run. The process remains non-root with all capabilities dropped, `no-new-privileges`, and `nosuid`, and it could already run arbitrary interpreted code through Bash, so `exec` adds native code from allowlisted registries, not a new privilege. No extra toolchains (Node, Cargo, Go, Java) are added to the executor image; npm is allowlisted for images or future profiles that carry Node.

## Enabling it

- Developer script: `./scripts/dev-up.ps1 -Workspace <path> -AllowlistedEgress`.
- Desktop (developer bundles): set `LOCAL_CHAT_EXECUTOR_EGRESS=allowlisted` before launching; release bundles refuse it until a pinned proxy image is published.
- Manually: `docker compose --file docker/compose.yaml --file docker/compose.egress.yaml --profile agent up --detach --build`.

Any other value of `LOCAL_CHAT_EXECUTOR_EGRESS` fails closed; unset, `none`, or `off` keeps the default no-network profile.

## Residual risks

- A CONNECT tunnel is opaque TLS, so the proxy sees host and port, not URLs. Content served by an allowlisted registry (for example, a malicious package) is not inspected; installing packages is itself a code-execution decision the user approves through Bash.
- Registries behind shared CDNs could, in principle, be abused for domain fronting (a different `Host` inside TLS than the CONNECT target). Keep the allowlist minimal, and review the audit log for unexpected volumes.
- Package managers may send telemetry or metadata to allowlisted hosts; that traffic is limited to those hosts.

## Verification

- `tests/unit/egress/test_egress_proxy.py` — policy decisions, public-address resolution, a live tunnel through the proxy, audit entries for allowed/denied requests, rate limiting, Bash environment, runtime-manager opt-in, and a check that `docker/compose.yaml` still renders the executor with `network_mode: none` and no egress service.
- `tests/docker/test_egress_profile.py` (container tier) — with the overlay, `pip install six` succeeds inside the executor through the proxy, `https://example.com` is refused and logged, a direct connection fails, and the default profile renders with `network_mode: none`.
