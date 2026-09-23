from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from egress.proxy import EgressDenied, EgressPolicy, EgressProxy, evaluate, resolve_public
from executor.egress import egress_status, proxy_environment
from executor.tools.bash import BashRunner
from src.runtime_manager import RuntimeErrorMessage, compose_base_args, egress_overlays

ROOT = Path(__file__).resolve().parents[3]


def test_policy_allows_only_connect_to_allowlisted_hosts_and_ports() -> None:
    policy = EgressPolicy(("pypi.org", "files.pythonhosted.org", "*.npmjs.org"))
    assert evaluate("CONNECT pypi.org:443 HTTP/1.1", policy).allowed
    assert evaluate("CONNECT PyPI.org.:443 HTTP/1.1", policy).host == "pypi.org"
    assert evaluate("CONNECT registry.npmjs.org:443 HTTP/1.1", policy).allowed
    cases = {
        "CONNECT npmjs.org:443 HTTP/1.1": "host_not_allowed",
        "CONNECT pypi.org.evil.example:443 HTTP/1.1": "host_not_allowed",
        "CONNECT evilpypi.org:443 HTTP/1.1": "host_not_allowed",
        "CONNECT pypi.org:80 HTTP/1.1": "port_not_allowed",
        "CONNECT 151.101.0.223:443 HTTP/1.1": "ip_literal",
        "CONNECT [2a04:4e42::223]:443 HTTP/1.1": "ip_literal",
        "GET http://pypi.org/simple/ HTTP/1.1": "method_not_allowed",
        "CONNECT pypi.org HTTP/1.1": "malformed_target",
        "CONNECT pypi_org:443 HTTP/1.1": "malformed_host",
        "garbage": "malformed_request",
    }
    for line, reason in cases.items():
        decision = evaluate(line, policy)
        assert not decision.allowed and decision.reason == reason, line


def test_policy_rejects_unsafe_allowlist_entries() -> None:
    for entry in ("10.0.0.1", "localhost", "*.", "bad host", ""):
        with pytest.raises(ValueError):
            EgressPolicy((entry,))
    policy = EgressPolicy.from_environment({"LOCAL_CHAT_EGRESS_ALLOWED_HOSTS": "pypi.org, files.pythonhosted.org", "LOCAL_CHAT_EGRESS_ALLOWED_PORTS": "443"})
    assert policy.allowed_hosts == ("pypi.org", "files.pythonhosted.org")


def test_default_resolver_refuses_non_public_addresses() -> None:
    with pytest.raises(EgressDenied) as raised:
        asyncio.run(resolve_public("localhost", 443))
    assert raised.value.reason in {"non_public_address", "resolution_failed"}


async def _echo_server() -> tuple[asyncio.base_events.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(1024):
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _request(port: int, request: bytes, payload: bytes = b"") -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    body = b""
    if payload and head.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
        body = await asyncio.wait_for(reader.readexactly(len(payload)), timeout=5)
    writer.close()
    return head, body


def test_proxy_tunnels_allowlisted_hosts_and_audits_every_decision() -> None:
    audit: list[dict] = []

    async def scenario() -> None:
        upstream, upstream_port = await _echo_server()
        resolved: list[tuple[str, int]] = []

        async def fake_resolver(host: str, port: int) -> list[str]:
            resolved.append((host, port))
            return ["127.0.0.1"]

        policy = EgressPolicy(("pypi.org",), (upstream_port,), max_connections_per_minute=6)
        proxy = EgressProxy(policy, resolver=fake_resolver, audit=audit.append)
        server = await proxy.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server, upstream:
            head, body = await _request(port, f"CONNECT pypi.org:{upstream_port} HTTP/1.1\r\nHost: pypi.org\r\n\r\n".encode(), b"wheel bytes")
            assert head.startswith(b"HTTP/1.1 200") and body == b"WHEEL BYTES"
            # A bare connect-and-close (a health probe) is not an egress attempt and is not audited.
            probe_reader, probe_writer = await asyncio.open_connection("127.0.0.1", port)
            probe_writer.close()
            assert await probe_reader.read() == b""
            head, _ = await _request(port, f"CONNECT example.com:{upstream_port} HTTP/1.1\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 403")
            head, _ = await _request(port, b"GET http://pypi.org/ HTTP/1.1\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 405")
            head, _ = await _request(port, f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 403")
            head, _ = await _request(port, f"CONNECT pypi.org:{upstream_port} HTTP/1.1\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 200")
            head, _ = await _request(port, f"CONNECT pypi.org:{upstream_port} HTTP/1.1\r\n\r\n".encode())
            assert head.startswith(b"HTTP/1.1 429")
            await asyncio.sleep(0.2)
        assert resolved == [("pypi.org", upstream_port), ("pypi.org", upstream_port)]

    asyncio.run(scenario())
    events = [(entry["event"], entry["reason"], entry["host"]) for entry in audit]
    assert events.count(("egress.allowed", "allowed", "pypi.org")) == 2
    assert ("egress.denied", "host_not_allowed", "example.com") in events
    assert ("egress.denied", "method_not_allowed", "") in events
    assert ("egress.denied", "ip_literal", "127.0.0.1") in events
    assert ("egress.denied", "rate_limited", "") in events
    assert len(events) == 6
    allowed = next(entry for entry in audit if entry["event"] == "egress.allowed")
    assert allowed["bytes_up"] == len(b"wheel bytes") and allowed["bytes_down"] == len(b"wheel bytes")


def test_executor_proxy_environment_is_empty_by_default() -> None:
    assert proxy_environment({}) == {}
    assert egress_status({}) == {"enabled": False, "mode": "none"}
    env = proxy_environment({"LOCAL_CHAT_EGRESS_PROXY": "http://172.31.250.2:3128"})
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://172.31.250.2:3128"
    assert egress_status({"LOCAL_CHAT_EGRESS_PROXY": "http://172.31.250.2:3128", "LOCAL_CHAT_EGRESS_ALLOWED_HOSTS": "pypi.org"})["allowed_hosts"] == ["pypi.org"]
    with pytest.raises(ValueError):
        proxy_environment({"LOCAL_CHAT_EGRESS_PROXY": "socks5://evil:1080"})


def test_bash_gets_proxy_variables_only_when_the_profile_is_enabled(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_CHAT_EGRESS_PROXY", raising=False)
    assert "HTTPS_PROXY" not in BashRunner._environment({})
    monkeypatch.setenv("LOCAL_CHAT_EGRESS_PROXY", "http://172.31.250.2:3128")
    environment = BashRunner._environment({})
    assert environment["HTTPS_PROXY"] == "http://172.31.250.2:3128"
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def _block(text: str, header: str) -> str:
    """The indented block under one top-level service/network key (two-space YAML indent)."""
    lines = text.splitlines()
    start = lines.index(header)
    indent = len(header) - len(header.lstrip())
    block = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return "\n".join(block)


def test_default_compose_profile_is_unchanged_and_the_overlay_is_isolated() -> None:
    base = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    executor = _block(base, "  executor:")
    assert "network_mode: none" in executor
    assert "networks:" not in executor and "egress" not in base.casefold()

    overlay = (ROOT / "docker" / "compose.egress.yaml").read_text(encoding="utf-8")
    overlay_executor = _block(overlay, "  executor:")
    proxy = _block(overlay, "  egress-proxy:")
    internal = _block(overlay, "  egress-internal:")
    assert "internal: true" in internal
    assert "network_mode: !reset null" in overlay_executor
    assert "egress-internal:" in overlay_executor and "egress-external" not in overlay_executor
    assert "dns:\n      - 127.0.0.1" in overlay_executor
    assert "tmpfs: !override" in overlay_executor and "nosuid,nodev,exec" in overlay_executor
    assert ",exec" not in base and "noexec" in _block(base, "  executor:")
    assert 'profiles: ["agent"]' in proxy and "ports:" not in proxy
    assert "read_only: true" in proxy and "cap_drop:\n      - ALL" in proxy and "no-new-privileges:true" in proxy
    assert 'user: "10003:10003"' in proxy
    assert "LOCAL_CHAT_EGRESS_LISTEN: 172.31.250.2:3128" in proxy and "ipv4_address: 172.31.250.2" in proxy
    assert "LOCAL_CHAT_EGRESS_PROXY: http://172.31.250.2:3128" in overlay_executor
    assert "/var/run/docker.sock" not in overlay and "privileged" not in overlay
    dockerfile = (ROOT / "docker" / "Dockerfile.egress-proxy").read_text(encoding="utf-8")
    assert "USER 10003:10003" in dockerfile and "pip install" not in dockerfile


def test_runtime_manager_adds_the_overlay_only_on_explicit_opt_in(tmp_path: Path) -> None:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "compose.egress.yaml").write_text("services: {}\n", encoding="utf-8")
    assert egress_overlays(tmp_path, prebuilt=False, environ={}) == ()
    assert egress_overlays(tmp_path, prebuilt=False, environ={"LOCAL_CHAT_EXECUTOR_EGRESS": "none"}) == ()
    overlays = egress_overlays(tmp_path, prebuilt=False, environ={"LOCAL_CHAT_EXECUTOR_EGRESS": "allowlisted"})
    assert compose_base_args(tmp_path / "docker" / "compose.yaml", overlays=overlays)[-2:] == ["--file", str(overlays[0])]
    with pytest.raises(RuntimeErrorMessage, match="release images"):
        egress_overlays(tmp_path, prebuilt=True, environ={"LOCAL_CHAT_EXECUTOR_EGRESS": "allowlisted"})
    with pytest.raises(RuntimeErrorMessage, match="allowlisted"):
        egress_overlays(tmp_path, prebuilt=False, environ={"LOCAL_CHAT_EXECUTOR_EGRESS": "open"})
