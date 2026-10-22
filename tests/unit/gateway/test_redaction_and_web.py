"""Secret redaction (harness plan 4.5) and documentation tools (5.4) in the gateway."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from server.agent.redaction import Redactor
from server.agent.web import (
    MAX_PAGE_CHARS,
    UNTRUSTED_LABEL,
    WebToolError,
    WebTools,
    check_url,
    html_to_text,
)
from shared.permissions import PermissionDecision
from shared.secret_scan import find_secrets, redact_secrets
from shared.tools import ToolResult, mode_tools
from tests.unit.gateway.test_harness_loop import (
    ScriptedExecutor,
    call,
    final_turn,
    kinds,
    make_request,
    run,
    tool_contents,
    tools_turn,
)

# Fake tokens are assembled at runtime so no literal secret-shaped string is checked in.
ALNUM = "Ab3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zA" * 4
FAKE_SECRETS = {
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + ALNUM[:40] + "\n"
    "-----END RSA PRIVATE KEY-----",
    "aws_access_key": "AKIA" + "ABCDEFGHIJ234567",
    "aws_secret_key": "aws_secret_access_key = " + ALNUM[:40],
    "gcp_api_key": "AIza" + ALNUM[:35],
    "github_token": "ghp" + "_" + ALNUM[:36],
    "gitlab_token": "glpat" + "-" + ALNUM[:20],
    "anthropic_key": "sk-ant" + "-api03-" + ALNUM[:40],
    "openrouter_key": "sk-or" + "-v1-" + ALNUM[:64],
    "openai_key": "sk" + "-proj-" + ALNUM[:48],
    "stripe_key": "sk" + "_live_" + ALNUM[:24],
    "slack_token": "xoxb" + "-1234567890-" + ALNUM[:24],
    "slack_webhook": "https://hooks.slack.com/services/" + "T0ABC1234/B0ABC1234/" + ALNUM[:24],
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + ALNUM[:43],
    "azure_storage_key": "AccountKey=" + ALNUM[:86] + "==",
}


@pytest.mark.parametrize("kind", sorted(FAKE_SECRETS))
def test_each_secret_kind_is_redacted_without_keeping_the_value(kind: str) -> None:
    secret = FAKE_SECRETS[kind]
    text, counts = redact_secrets(f"before {secret} after")
    assert counts == {kind: 1}
    assert f"[REDACTED:{kind}]" in text and secret not in text
    assert text.startswith("before ") and text.endswith(" after")


def test_an_unterminated_private_key_is_still_redacted() -> None:
    text, counts = redact_secrets("-----BEGIN PRIVATE KEY-----\nMIIE" + ALNUM)
    assert counts == {"private_key": 1} and ALNUM not in text


@pytest.mark.parametrize(
    "benign",
    [
        "a" * 64,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "123e4567-e89b-12d3-a456-426614174000",
        "commit 9fceb02d0ae598e95dc970b74767f19372d61af8",
        "sk-short",
        "task-" + ALNUM,
        "SGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQgZW5jb2RlZCB0ZXh0Cg==",
        "AKIA lowercase akiaabcdefghij234567",
        "sk-" + "a" * 40,
    ],
)
def test_ordinary_hashes_uuids_and_base64_are_not_flagged(benign: str) -> None:
    assert find_secrets(benign) == {}


def test_unredacted_paths_apply_only_to_reads_of_exactly_those_files() -> None:
    redactor = Redactor.from_workspace_config({"unredacted_paths": ["config/.env", 7]})
    secret = FAKE_SECRETS["github_token"]
    assert redactor.redact("read", {"path": "config/.env"}, secret) == (secret, {})
    assert redactor.redact("read", {"path": "./config/.env"}, secret) == (secret, {})
    assert redactor.redact("read", {"path": "config/other.env"}, secret)[1] == {
        "github_token": 1
    }
    assert redactor.redact("grep", {"path": "config/.env"}, secret)[1] == {"github_token": 1}
    assert redactor.redact("bash", {"command": "cat config/.env"}, secret)[1] == {
        "github_token": 1
    }


class SecretExecutor(ScriptedExecutor):
    async def execute(self, request):
        result = await super().execute(request)
        if request.tool == "read":
            return ToolResult(
                request.request_id,
                True,
                output="token = " + FAKE_SECRETS["github_token"],
                data=result.data,
            )
        return result


def test_secrets_are_redacted_before_the_result_reaches_the_provider(monkeypatch) -> None:
    provider, events, saved, _ = run(
        monkeypatch,
        [tools_turn(call("r", "read", {"path": "a.py"})), final_turn()],
        executor=SecretExecutor(),
    )
    secret = FAKE_SECRETS["github_token"]
    sent = tool_contents(provider.payloads[-1])[0]
    assert "[REDACTED:github_token]" in sent
    assert all(secret not in body.decode() for body in provider.bodies)
    assert all(secret not in json.dumps(message) for message in saved["snapshot"][1])
    redacted = [payload for kind, payload in events if kind == "secret.redacted"]
    assert redacted == [{"request_id": "r", "tool": "read", "kinds": {"github_token": 1}}]
    assert secret not in json.dumps(redacted)


def test_an_unredacted_path_reaches_the_provider_unchanged(monkeypatch) -> None:
    provider, events, _saved, _ = run(
        monkeypatch,
        [tools_turn(call("r", "read", {"path": "a.py"})), final_turn()],
        executor=SecretExecutor(),
        request=make_request(workspace_config={"unredacted_paths": ["a.py"]}),
    )
    assert FAKE_SECRETS["github_token"] in tool_contents(provider.payloads[-1])[0]
    assert "secret.redacted" not in kinds(events)


def test_investigation_results_are_redacted(monkeypatch) -> None:
    from server.agent import investigation as investigation_module
    from server.openrouter.agent import AgentTurn

    nested: list[list[dict]] = []
    script = [
        AgentTurn(
            "",
            (call("n1", "read", {"path": "a.py"}),),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call("n1", "read", {"path": "a.py"})],
            },
            {"total_tokens": 5},
        ),
        AgentTurn("a.py holds a token.", (), {"role": "assistant", "content": "x"}, {}),
    ]

    async def nested_turn(model, messages, api_key, mode, *args, **kwargs):
        nested.append([dict(message) for message in messages])
        return script.pop(0)

    monkeypatch.setattr(investigation_module, "agent_turn", nested_turn)
    _provider, events, _saved, _ = run(
        monkeypatch,
        [
            tools_turn(call("i", "investigate_repository", {"query": "where is the token?"})),
            final_turn(),
        ],
        executor=SecretExecutor(),
    )
    secret = FAKE_SECRETS["github_token"]
    assert all(secret not in json.dumps(messages) for messages in nested)
    assert "[REDACTED:github_token]" in json.dumps(nested[-1])
    redacted = [payload for kind, payload in events if kind == "secret.redacted"]
    assert redacted and redacted[0]["kinds"] == {"github_token": 1}


# -- 5.4 documentation tools -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowlisted", "has_query"),
    [
        ("https://docs.python.org/3/library/json.html", True, False),
        ("https://requests.readthedocs.io/en/latest/", True, False),
        ("https://docs.python.org/3/search.html?q=json", True, True),
        ("https://example.com/guide", False, False),
        ("https://evil-docs.python.org.example.com/", False, False),
    ],
)
def test_the_allowlist_and_query_strings_decide_approval(url, allowlisted, has_query) -> None:
    check = check_url(url)
    assert (check.allowlisted, check.has_query) == (allowlisted, has_query)
    assert check.needs_approval is (not allowlisted or has_query)


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.python.org/",
        "ftp://docs.python.org/",
        "https://user:pass@docs.python.org/",
        "https://docs.python.org:8443/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "file:///etc/passwd",
        "",
        42,
    ],
)
def test_unfetchable_urls_are_refused(url) -> None:
    with pytest.raises(WebToolError) as caught:
        check_url(url)
    assert caught.value.code == "web.invalid_url"


def test_html_is_reduced_to_bounded_readable_text() -> None:
    html = (
        "<html><head><title>JSON  docs</title><style>p{}</style></head><body>"
        "<nav>Home | About</nav><script>steal()</script><h1>json</h1>"
        "<p>Encode &amp; decode.</p><ul><li>dumps</li><li>loads</li></ul>"
        "<footer>copyright</footer></body></html>"
    )
    text = html_to_text(html)
    assert text.startswith("# JSON docs")
    assert "Encode & decode." in text and "- dumps" in text
    assert "steal" not in text and "Home" not in text and "copyright" not in text
    long = html_to_text("<p>" + "word " * 20_000 + "</p>")
    assert len(long) < MAX_PAGE_CHARS + 100 and "truncated" in long


async def public(_host: str) -> list[str]:
    return ["93.184.216.34"]


def fetch(handler, url: str, **kwargs):
    tools = WebTools(transport=httpx.MockTransport(handler), resolver=public)
    return asyncio.run(tools.fetch(url, **kwargs))


def test_a_redirect_is_rechecked_and_stops_when_it_needs_approval() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/moved"})
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://example.com/x"})
        return httpx.Response(200, text="never")

    with pytest.raises(WebToolError) as caught:
        fetch(handler, "https://docs.python.org/start")
    assert caught.value.code == "web.redirect_needs_approval"
    assert seen == ["https://docs.python.org/start", "https://docs.python.org/moved"]
    with pytest.raises(WebToolError) as http_redirect:
        fetch(
            lambda _r: httpx.Response(302, headers={"location": "http://docs.python.org/"}),
            "https://docs.python.org/start",
        )
    assert http_redirect.value.code == "web.invalid_url"


def test_an_approved_redirect_target_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.python.org":
            return httpx.Response(302, headers={"location": "https://example.com/x"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="hello")

    page = fetch(handler, "https://docs.python.org/a", approved_url="https://example.com/x")
    assert page.url == "https://example.com/x" and page.text == "hello"
    assert page.model_text().startswith(UNTRUSTED_LABEL)


def test_fetches_are_bounded_in_size_type_and_redirects(monkeypatch) -> None:
    from server.agent import web as web_module

    monkeypatch.setattr(web_module, "MAX_FETCH_BYTES", 1_000)
    page = fetch(
        lambda _r: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="y" * 5_000
        ),
        "https://docs.python.org/big",
    )
    assert page.bytes == 1_000
    with pytest.raises(WebToolError) as binary:
        fetch(
            lambda _r: httpx.Response(
                200, headers={"content-type": "application/zip"}, content=b"PK"
            ),
            "https://docs.python.org/a.zip",
        )
    assert binary.value.code == "web.unsupported_content"
    with pytest.raises(WebToolError) as loop:
        fetch(
            lambda r: httpx.Response(302, headers={"location": r.url.path + "x"}),
            "https://docs.python.org/a",
        )
    assert loop.value.code == "web.too_many_redirects"


def test_fetches_time_out(monkeypatch) -> None:
    from server.agent import web as web_module

    monkeypatch.setattr(web_module, "FETCH_TIMEOUT_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(1)
        yield b"late"

    with pytest.raises(WebToolError) as caught:
        fetch(
            lambda _r: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=slow()
            ),
            "https://docs.python.org/slow",
        )
    assert caught.value.code == "web.timeout"


def test_private_addresses_are_refused() -> None:
    from server.agent.web import resolve_public

    async def scenario():
        return await resolve_public("localhost")

    with pytest.raises(WebToolError) as caught:
        asyncio.run(scenario())
    assert caught.value.code in {"web.private_address", "web.unreachable"}


def test_web_tools_are_off_by_default_and_agent_only() -> None:
    assert not {"web_search", "fetch_url"} & mode_tools("agent")
    assert {"web_search", "fetch_url"} <= mode_tools("agent", web=True)
    assert not {"web_search", "fetch_url"} & mode_tools("plan", web=True)


class FakeWeb:
    def __init__(self) -> None:
        self.fetched: list[tuple[str, str | None]] = []
        self.searched: list[str] = []

    async def fetch(self, url, *, approved_url=None):
        from server.agent.web import FetchedPage

        self.fetched.append((url, approved_url))
        return FetchedPage(url, 200, "text/plain", "page text", 9)

    async def search(self, api_key, model, query, max_results, provider_preferences=None):
        from server.agent.web import SearchResults

        self.searched.append(query)
        return SearchResults(
            query,
            ({"title": "Docs", "url": "https://docs.python.org/", "snippet": "s"},),
            {"cost": 0.01},
        )


class UrlApprovals:
    def __init__(self, decision) -> None:
        self.decision = decision
        self.asked = 0

    async def wait(self, *args, **kwargs):
        self.asked += 1
        return self.decision


def web_run(monkeypatch, turns, *, decision=PermissionDecision.ALLOW_ONCE, web_tools=True):
    from server.agent.runtime import AgentRuntime

    executor = ScriptedExecutor()
    web = FakeWeb()
    approvals = UrlApprovals(decision)
    runtime = AgentRuntime(executor, approvals, None, web=web)
    provider, events, _saved, _ = run(
        monkeypatch,
        turns,
        runtime=runtime,
        request=make_request(workspace_config={"web_tools": web_tools}),
    )
    return provider, events, web, approvals, executor


def test_allowlisted_fetches_run_without_asking_and_never_reach_the_executor(
    monkeypatch,
) -> None:
    provider, events, web, approvals, executor = web_run(
        monkeypatch,
        [
            tools_turn(
                call("f", "fetch_url", {"url": "https://docs.python.org/3/"}),
                call("s", "web_search", {"query": "json  dumps"}),
            ),
            final_turn(),
        ],
    )
    assert approvals.asked == 0 and executor.requests == []
    assert web.fetched == [("https://docs.python.org/3/", None)]
    assert web.searched == ["json dumps"]
    fetched, searched = tool_contents(provider.payloads[-1])
    assert fetched.startswith(UNTRUSTED_LABEL) and "page text" in fetched
    assert searched.startswith(UNTRUSTED_LABEL) and "<https://docs.python.org/>" in searched
    requested = [payload for kind, payload in events if kind == "web.requested"]
    assert {item["tool"] for item in requested} == {"fetch_url", "web_search"}
    assert {tool["function"]["name"] for tool in provider.payloads[0]["tools"]} >= {
        "web_search",
        "fetch_url",
    }


def test_other_urls_ask_every_time_even_under_auto(monkeypatch) -> None:
    url = "https://example.com/guide"
    _provider, events, web, approvals, _executor = web_run(
        monkeypatch,
        [
            tools_turn(call("f1", "fetch_url", {"url": url})),
            tools_turn(call("f2", "fetch_url", {"url": url})),
            final_turn(),
        ],
    )
    assert approvals.asked == 2
    assert web.fetched == [(url, url), (url, url)]
    asked = [payload for kind, payload in events if kind == "approval.required"]
    assert asked[0]["kind"] == "url" and asked[0]["url"] == url
    assert asked[0]["available_decisions"] == ["deny", "allow_once"]


def test_a_declined_url_is_not_fetched(monkeypatch) -> None:
    provider, _events, web, _approvals, _executor = web_run(
        monkeypatch,
        [
            tools_turn(call("f", "fetch_url", {"url": "https://example.com/"})),
            final_turn(),
        ],
        decision=PermissionDecision.DENY,
    )
    assert web.fetched == []
    assert "permission.denied" in tool_contents(provider.payloads[-1])[0]


def test_web_tools_are_unavailable_unless_the_workspace_opts_in(monkeypatch) -> None:
    provider, _events, web, _approvals, _executor = web_run(
        monkeypatch,
        [
            tools_turn(call("f", "fetch_url", {"url": "https://docs.python.org/"})),
            final_turn(),
        ],
        web_tools=False,
    )
    assert web.fetched == []
    assert "tool.unknown" in tool_contents(provider.payloads[-1])[0]
    assert not {"web_search", "fetch_url"} & {
        tool["function"]["name"] for tool in provider.payloads[0]["tools"]
    }
