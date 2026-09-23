"""Opt-in egress settings for the install-capable executor profile.

The executor itself never gains network authority: in the default profile it runs with
``network_mode: none``. Only when the operator starts the opt-in egress overlay
(``docker/compose.egress.yaml``) does the container receive ``LOCAL_CHAT_EGRESS_PROXY``,
the address of the allowlisted proxy on an internal-only network. Bash then gets the
standard proxy variables so pip/npm reach allowlisted registries through that proxy.
"""

from __future__ import annotations

import os
import re

_PROXY_URL = re.compile(r"^http://(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9][a-z0-9.-]{0,252}):\d{1,5}$")


def proxy_url(environ: dict[str, str] | None = None) -> str | None:
    """The validated proxy URL, or None when egress is not enabled (the default)."""
    value = (os.environ if environ is None else environ).get("LOCAL_CHAT_EGRESS_PROXY", "").strip()
    if not value:
        return None
    if not _PROXY_URL.fullmatch(value):
        raise ValueError("LOCAL_CHAT_EGRESS_PROXY must be an http://host:port proxy address")
    return value


def proxy_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Variables that route package managers through the allowlisted proxy."""
    url = proxy_url(environ)
    if url is None:
        return {}
    return {
        "HTTPS_PROXY": url,
        "https_proxy": url,
        "HTTP_PROXY": url,
        "http_proxy": url,
        "NO_PROXY": "",
        "no_proxy": "",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "npm_config_https_proxy": url,
        "npm_config_proxy": url,
    }


def egress_status(environ: dict[str, str] | None = None) -> dict[str, object]:
    """Runtime facts for the executor status endpoint."""
    env = os.environ if environ is None else environ
    try:
        url = proxy_url(env)
    except ValueError:
        return {"enabled": False, "mode": "misconfigured"}
    if url is None:
        return {"enabled": False, "mode": "none"}
    hosts = [item.strip() for item in env.get("LOCAL_CHAT_EGRESS_ALLOWED_HOSTS", "").split(",") if item.strip()]
    return {"enabled": True, "mode": "allowlisted_proxy", "proxy": url, "allowed_hosts": hosts}
