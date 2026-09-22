"""Allowlisted HTTPS egress proxy for the opt-in install-capable executor profile.

The executor never gets a route to the internet. In the opt-in profile it joins an
``internal`` Docker network whose only other member is this proxy, and package managers
reach registries through it with ``HTTPS_PROXY``. The proxy:

* accepts only ``CONNECT host:port`` (TLS stays end-to-end; no plain HTTP, no other methods);
* allows only exact allowlisted hostnames (or ``*.suffix`` entries) and allowlisted ports;
* never accepts IP literals, and refuses names that resolve to non-public addresses, so an
  allowlisted name cannot be used to reach the host, the gateway, or private networks;
* bounds concurrent tunnels, new tunnels per minute, idle time, tunnel lifetime, and bytes;
* writes one JSON audit line per decision (allowed with byte counts, or denied with reason).

It is standard-library only, so its image carries no third-party dependencies.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

DEFAULT_ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
DEFAULT_ALLOWED_PORTS = (443,)
DEFAULT_LISTEN = "0.0.0.0:3128"
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PROBE = "empty_connection"
_REQUEST_LINE = re.compile(r"^([A-Z]{3,10}) (\S{1,300}) HTTP/1\.[01]$")


class EgressDenied(Exception):
    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    allowed_ports: tuple[int, ...] = DEFAULT_ALLOWED_PORTS
    max_connections: int = 32
    max_connections_per_minute: int = 240
    header_timeout_seconds: float = 10.0
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 60.0
    max_tunnel_seconds: float = 900.0
    max_tunnel_bytes: int = 2_000_000_000
    max_header_bytes: int = 8_192

    def __post_init__(self) -> None:
        normalized = tuple(_normalize_entry(item) for item in self.allowed_hosts)
        if not normalized:
            raise ValueError("The egress allowlist must name at least one host")
        object.__setattr__(self, "allowed_hosts", normalized)
        if not self.allowed_ports or any(not 1 <= int(port) <= 65_535 for port in self.allowed_ports):
            raise ValueError("Allowed egress ports must be between 1 and 65535")
        if min(self.max_connections, self.max_connections_per_minute, self.max_header_bytes, self.max_tunnel_bytes) < 1:
            raise ValueError("Egress limits must be positive")

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> EgressPolicy:
        env = os.environ if environ is None else environ
        hosts = tuple(item for item in env.get("LOCAL_CHAT_EGRESS_ALLOWED_HOSTS", ",".join(DEFAULT_ALLOWED_HOSTS)).split(",") if item.strip())
        ports = tuple(int(item) for item in env.get("LOCAL_CHAT_EGRESS_ALLOWED_PORTS", "443").split(",") if item.strip())
        return cls(
            allowed_hosts=hosts,
            allowed_ports=ports,
            max_connections=int(env.get("LOCAL_CHAT_EGRESS_MAX_CONNECTIONS", "32")),
            max_connections_per_minute=int(env.get("LOCAL_CHAT_EGRESS_MAX_CONNECTIONS_PER_MINUTE", "240")),
        )

    def host_allowed(self, host: str) -> bool:
        for entry in self.allowed_hosts:
            if entry.startswith("*."):
                if host.endswith(entry[1:]) and host != entry[2:]:
                    return True
            elif host == entry:
                return True
        return False


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str
    method: str = ""
    host: str = ""
    port: int = 0


def evaluate(request_line: str, policy: EgressPolicy) -> Decision:
    """Decide one proxy request line without any I/O."""
    match = _REQUEST_LINE.match(request_line.strip())
    if not match:
        return Decision(False, "malformed_request")
    method, target = match.groups()
    if method != "CONNECT":
        return Decision(False, "method_not_allowed", method)
    host, separator, raw_port = target.rpartition(":")
    if not separator or not raw_port.isdigit():
        return Decision(False, "malformed_target", method)
    port = int(raw_port)
    host = host.strip("[]").rstrip(".").casefold()
    if _is_ip_literal(host):
        return Decision(False, "ip_literal", method, host, port)
    if not _valid_hostname(host):
        return Decision(False, "malformed_host", method, host[:255], port)
    if port not in policy.allowed_ports:
        return Decision(False, "port_not_allowed", method, host, port)
    if not policy.host_allowed(host):
        return Decision(False, "host_not_allowed", method, host, port)
    return Decision(True, "allowed", method, host, port)


async def resolve_public(host: str, port: int) -> list[str]:
    """Resolve ``host`` and refuse the answer unless every address is publicly routable."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise EgressDenied("resolution_failed", 502) from exc
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise EgressDenied("resolution_failed", 502)
    if any(not ipaddress.ip_address(address.split("%", 1)[0]).is_global for address in addresses):
        raise EgressDenied("non_public_address")
    return addresses


Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
AuditLog = Callable[[dict[str, object]], None]


def stdout_audit(entry: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


@dataclass
class EgressProxy:
    policy: EgressPolicy
    resolver: Resolver = resolve_public
    connector: Connector = asyncio.open_connection
    audit: AuditLog = stdout_audit
    clock: Callable[[], float] = time.monotonic
    active: int = 0
    _recent: deque[float] = field(default_factory=deque)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = self.clock()
        peer = writer.get_extra_info("peername")
        client = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        state = {"decision": Decision(False, "not_evaluated")}
        try:
            if self.active >= self.policy.max_connections:
                raise EgressDenied("too_many_connections", 503)
            now = self.clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) >= self.policy.max_connections_per_minute:
                raise EgressDenied("rate_limited", 429)
            self._recent.append(now)
            self.active += 1
            try:
                await self._tunnel(reader, writer, client, started, state)
            finally:
                self.active -= 1
        except EgressDenied as exc:
            if exc.reason != _PROBE:
                self._log(False, exc.reason, state["decision"], client, started)
                await _respond(writer, exc.status)
        except Exception as exc:  # never let one client crash the proxy
            self._log(False, f"error:{type(exc).__name__}", state["decision"], client, started)
            await _respond(writer, 502)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client: str, started: float, state: dict[str, Decision]) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.policy.header_timeout_seconds)
        except asyncio.LimitOverrunError as exc:
            raise EgressDenied("headers_too_large", 431) from exc
        except asyncio.IncompleteReadError as exc:
            # A connection closed before sending a byte (health checks, port probes) is not
            # an egress attempt, so it is neither answered nor audited.
            raise EgressDenied(_PROBE if not exc.partial else "malformed_request", 400) from exc
        except TimeoutError as exc:
            raise EgressDenied("malformed_request", 400) from exc
        if len(head) > self.policy.max_header_bytes:
            raise EgressDenied("headers_too_large", 431)
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        decision = evaluate(request_line, self.policy)
        state["decision"] = decision
        if not decision.allowed:
            status = 405 if decision.reason == "method_not_allowed" else 400 if decision.reason.startswith("malformed") else 403
            raise EgressDenied(decision.reason, status)
        addresses = await self.resolver(decision.host, decision.port)
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(self.connector(addresses[0], decision.port), timeout=self.policy.connect_timeout_seconds)
        except (OSError, TimeoutError) as exc:
            raise EgressDenied("upstream_unreachable", 502) from exc
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        counters = {"up": 0, "down": 0}
        deadline = started + self.policy.max_tunnel_seconds
        outcome = "closed"
        try:
            outcome = await self._pump(reader, upstream_writer, writer, upstream_reader, counters, deadline)
        finally:
            upstream_writer.close()
        self._log(True, "allowed", decision, client, started, bytes_up=counters["up"], bytes_down=counters["down"], outcome=outcome)

    async def _pump(self, client_reader, upstream_writer, client_writer, upstream_reader, counters, deadline) -> str:
        async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter, key: str) -> str:
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return "max_tunnel_seconds"
                try:
                    chunk = await asyncio.wait_for(source.read(65_536), timeout=min(self.policy.idle_timeout_seconds, remaining))
                except TimeoutError:
                    return "idle_timeout"
                if not chunk:
                    return "closed"
                counters[key] += len(chunk)
                if counters["up"] + counters["down"] > self.policy.max_tunnel_bytes:
                    return "max_tunnel_bytes"
                sink.write(chunk)
                await sink.drain()

        tasks = [asyncio.create_task(copy(client_reader, upstream_writer, "up")), asyncio.create_task(copy(upstream_reader, client_writer, "down"))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        results = [task.result() for task in done if not task.cancelled() and task.exception() is None]
        return next((item for item in results if item != "closed"), "closed")

    def _log(self, allowed: bool, reason: str, decision: Decision, client: str, started: float, **extra: object) -> None:
        entry: dict[str, object] = {
            "event": "egress.allowed" if allowed else "egress.denied",
            "reason": reason,
            "method": decision.method,
            "host": decision.host,
            "port": decision.port,
            "client": client,
            "timestamp": time.time(),
            "duration_seconds": round(self.clock() - started, 3),
            **extra,
        }
        self.audit(entry)

    async def serve(self, host: str, port: int) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port, limit=self.policy.max_header_bytes)


async def _respond(writer: asyncio.StreamWriter, status: int) -> None:
    reasons = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 429: "Too Many Requests", 431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}
    try:
        writer.write(f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii"))
        await writer.drain()
    except (ConnectionError, OSError, RuntimeError):
        pass


def _normalize_entry(value: str) -> str:
    entry = value.strip().rstrip(".").casefold()
    host = entry[2:] if entry.startswith("*.") else entry
    if _is_ip_literal(host) or not _valid_hostname(host) or "." not in host:
        raise ValueError(f"Invalid egress allowlist entry: {value!r}")
    return entry


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _valid_hostname(host: str) -> bool:
    return 0 < len(host) <= 253 and all(_HOST_LABEL.match(label) for label in host.split("."))


def main() -> None:
    policy = EgressPolicy.from_environment()
    listen = os.environ.get("LOCAL_CHAT_EGRESS_LISTEN", DEFAULT_LISTEN)
    host, _, port = listen.rpartition(":")

    async def run() -> None:
        server = await EgressProxy(policy).serve(host or "0.0.0.0", int(port))
        stdout_audit({"event": "egress.started", "listen": listen, "allowed_hosts": list(policy.allowed_hosts), "allowed_ports": list(policy.allowed_ports), "timestamp": time.time()})
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
