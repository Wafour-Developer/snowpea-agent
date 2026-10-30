"""Protocol 1.34.0: the snowpea-browser "Integrations" requests.

1. MCP OAuth for http/sse servers: discovery (401 → RFC 9728 → RFC 8414),
   dynamic client registration, PKCE S256, the code exchange with a resource
   indicator, refresh with rotation, tokens keyed by name and URL, the loopback
   and the host-completed flows, ``needs_auth`` at connect, and no token in any
   RPC result, event or log line.  The authorization and resource server is a
   local aiohttp app; nothing leaves 127.0.0.1.
2. ``settings.skills.disabled``.
3. ``${VAR}`` expansion in MCP args, env and headers.
4. ``mcp.catalog`` rows carry ``auth``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
import pytest_asyncio
from aiohttp import web
from test_mcp_manage import (  # noqa: F401
    FIXTURE_ECHO,
    _stop_servers,
    core,
    echo_params,
    events,
    workdir,
)

from snowpea_core.agent import loop as agent_loop
from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import mcp_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import ERROR_CODES, RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    EVENTS,
    METHODS,
    PROTOCOL_VERSION,
    McpAddParams,
    McpAuthorizeCompleteParams,
    McpAuthorizeParams,
    McpDeauthorizeParams,
    McpEntryFields,
    McpListParams,
    McpUpdateParams,
)
from snowpea_core.session.session import Session
from snowpea_core.skills.loader import SkillLoader
from snowpea_core.tools import mcp_catalog, mcp_client, mcp_config, mcp_oauth, skills_tools
from snowpea_core.tools.registry import ToolContext


def test_version_methods_events_capabilities_and_error_code() -> None:
    assert PROTOCOL_VERSION == "1.34.0"
    for method in ("mcp.authorize", "mcp.authorize.complete", "mcp.deauthorize"):
        assert method in METHODS
    assert "mcp.auth.done" in EVENTS
    assert {"mcpOAuth", "skillToggle", "mcpVarExpansion"} <= set(CAPABILITIES)
    assert "mcp_auth_failed" in ERROR_CODES


# ---------------------------------------------------------------------------
# the fake authorization + resource server
# ---------------------------------------------------------------------------


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class FakeAuthServer:
    """An OAuth 2.1 AS and an MCP resource that answers 401 without a valid token."""

    def __init__(self) -> None:
        self.base = ""
        self.dcr = True
        #: Knobs for the hardening tests (H2): what the metadata claims.
        self.prm_resource: str | None = None
        self.issuer_claim: str | None = None
        self.iss_supported = False
        self.send_iss: str | None = None
        self.registrations: list[dict[str, Any]] = []
        self.authorize_queries: list[dict[str, str]] = []
        self.token_forms: list[dict[str, str]] = []
        self.codes: dict[str, dict[str, str]] = {}
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        self.seen_auth: list[str | None] = []
        self.counter = 0
        self.runner: web.AppRunner | None = None

    @property
    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    def _issue(self) -> dict[str, Any]:
        self.counter += 1
        access, refresh = f"at-{self.counter}-secret", f"rt-{self.counter}-secret"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    async def mcp(self, request: web.Request) -> web.Response:
        header = request.headers.get("Authorization")
        self.seen_auth.append(header)
        if header and header.removeprefix("Bearer ") in self.valid_access:
            return web.json_response({"ok": True})
        return web.Response(
            status=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer resource_metadata="{self.base}/.well-known/'
                    f'oauth-protected-resource/mcp", scope="read write"'
                )
            },
        )

    async def prm(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "resource": self.prm_resource or self.mcp_url,
                "authorization_servers": [self.base],
                "scopes_supported": ["read", "write"],
            }
        )

    async def asm(self, _request: web.Request) -> web.Response:
        body: dict[str, Any] = {
            "issuer": self.issuer_claim or self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "code_challenge_methods_supported": ["S256"],
        }
        if self.dcr:
            body["registration_endpoint"] = f"{self.base}/register"
        if self.iss_supported:
            body["authorization_response_iss_parameter_supported"] = True
        return web.json_response(body)

    async def register(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.registrations.append(body)
        return web.json_response({"client_id": f"client-{len(self.registrations)}"}, status=201)

    async def authorize(self, request: web.Request) -> web.Response:
        query = dict(request.query)
        self.authorize_queries.append(query)
        code = f"code-{len(self.authorize_queries)}"
        self.codes[code] = query
        location = f"{query['redirect_uri']}?code={code}&state={query['state']}"
        if self.send_iss is not None:
            location += f"&iss={self.send_iss}"
        raise web.HTTPFound(location)

    async def token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_forms.append({k: str(v) for k, v in form.items()})
        if form.get("grant_type") == "authorization_code":
            issued = self.codes.pop(str(form.get("code")), None)
            if (
                issued is None
                or s256(str(form.get("code_verifier"))) != issued["code_challenge"]
                or form.get("redirect_uri") != issued["redirect_uri"]
                or form.get("client_id") != issued["client_id"]
                or form.get("resource") != issued["resource"]
            ):
                return web.json_response({"error": "invalid_grant"}, status=400)
            return web.json_response(self._issue())
        if form.get("grant_type") == "refresh_token":
            token = str(form.get("refresh_token"))
            if token not in self.valid_refresh:
                return web.json_response({"error": "invalid_grant"}, status=400)
            self.valid_refresh.discard(token)  # rotation: the old one is spent
            return web.json_response(self._issue())
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/mcp", self.mcp)
        app.router.add_get("/.well-known/oauth-protected-resource/mcp", self.prm)
        app.router.add_get("/.well-known/oauth-authorization-server", self.asm)
        app.router.add_post("/register", self.register)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/token", self.token)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@pytest_asyncio.fixture
async def fake_as() -> AsyncIterator[FakeAuthServer]:
    server = FakeAuthServer()
    await server.start()
    yield server
    await mcp_handlers.close_pending()
    await server.close()


def declare(workdir: Path, url: str, **extra: Any) -> None:  # noqa: F811
    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": url, **extra})


async def consent(url: str) -> str:
    """Play the user's browser: open the consent URL, return where it redirects."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    assert response.status_code == 302
    return response.headers["location"]


def auth_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:  # noqa: F811
    return [params for method, params in events if method == "mcp.auth.done"]


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 1. OAuth
# ---------------------------------------------------------------------------


async def test_loopback_flow_discovers_registers_and_stores_tokens(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
    events: list[tuple[str, dict[str, Any]]],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    declare(workdir, fake_as.mcp_url)
    result = await mcp_handlers.mcp_authorize_handler(
        None, McpAuthorizeParams(name="remote", workdir=str(workdir)), core
    )
    assert result.redirectUri.startswith("http://127.0.0.1:")
    assert result.redirectUri.endswith("/callback")
    query = parse_qs(urlsplit(result.url).query)
    assert result.url.startswith(f"{fake_as.base}/authorize?")
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [fake_as.mcp_url]
    assert query["scope"] == ["read write"]  # from WWW-Authenticate
    assert query["state"] == [result.state]
    assert query["client_id"] == ["client-1"]
    assert fake_as.registrations[0]["redirect_uris"] == [result.redirectUri]
    assert fake_as.registrations[0]["token_endpoint_auth_method"] == "none"

    location = await consent(result.url)
    async with httpx.AsyncClient() as client:
        page = await client.get(location)
    assert page.status_code == 200
    await wait_for(lambda: bool(auth_events(events)))
    assert auth_events(events) == [{"name": "remote", "ok": True, "error": None}]

    # The exchange carried the verifier and the resource indicator.
    exchange = fake_as.token_forms[-1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["resource"] == fake_as.mcp_url
    path = core.paths.home / mcp_oauth.OAUTH_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = mcp_oauth.TokenStore(core.paths.home).get("remote", fake_as.mcp_url)
    assert stored is not None and stored["access_token"] == "at-1-secret"

    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    row = next(item for item in listing.servers if item.name == "remote")
    assert row.auth.state == "ok" and row.auth.required

    # Nothing secret anywhere a client or the log could see it.
    wire = json.dumps([result.model_dump(mode="json"), listing.model_dump(mode="json"), events])
    # The test's own "browser" requests (httpx/httpcore lines for the consent
    # page and the redirect) carry the code by design; everything else must not.
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not record.name.startswith("httpcore")
        and "/callback?" not in record.getMessage()
        and "/authorize?" not in record.getMessage()
    )
    for secret in ("at-1-secret", "rt-1-secret", exchange["code_verifier"], exchange["code"]):
        assert secret not in wire
        assert secret not in logged


async def test_host_completed_flow_uses_the_given_redirect(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
    events: list[tuple[str, dict[str, Any]]],  # noqa: F811
) -> None:
    declare(workdir, fake_as.mcp_url)
    redirect = "https://browser.example/oauth/callback"
    result = await mcp_handlers.mcp_authorize_handler(
        None,
        McpAuthorizeParams(name="remote", workdir=str(workdir), redirectUri=redirect, scope="read"),
        core,
    )
    assert result.redirectUri == redirect
    assert parse_qs(urlsplit(result.url).query)["scope"] == ["read"]
    location = await consent(result.url)
    params = parse_qs(urlsplit(location).query)
    done = await mcp_handlers.mcp_authorize_complete_handler(
        None, McpAuthorizeCompleteParams(state=params["state"][0], code=params["code"][0]), core
    )
    assert done.ok and done.name == "remote"
    assert "secret" not in json.dumps(done.model_dump(mode="json"))
    assert auth_events(events)[-1]["ok"] is True
    # A state is good for one completion only.
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_authorize_complete_handler(
            None, McpAuthorizeCompleteParams(state=params["state"][0], code="x"), core
        )
    assert caught.value.code == "mcp_auth_failed"
    assert caught.value.data == {"stage": "state"}


async def test_the_exchange_fails_on_a_wrong_verifier(fake_as: FakeAuthServer) -> None:
    redirect = "https://browser.example/cb"
    async with mcp_oauth.new_client() as http:
        found = await mcp_oauth.discover(http, fake_as.mcp_url)
        client = await mcp_oauth.register(http, found, redirect, found.scope)
    verifier = "v" * 43
    url = mcp_oauth.authorize_url(
        found,
        client_id=client["client_id"],
        redirect_uri=redirect,
        challenge_s256=s256(verifier),
        state="st",
        scope=found.scope,
    )
    code = parse_qs(urlsplit(await consent(url)).query)["code"][0]
    record = {
        "url": fake_as.mcp_url,
        "resource": found.resource,
        "token_endpoint": found.token_endpoint,
        "redirect_uri": redirect,
        **client,
    }
    with pytest.raises(mcp_oauth.OAuthError) as caught:
        await mcp_oauth.exchange(record, code, "w" * 43)
    assert caught.value.stage == "exchange"
    assert "invalid_grant" in str(caught.value)


async def test_an_error_redirect_reports_failure(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
    events: list[tuple[str, dict[str, Any]]],  # noqa: F811
) -> None:
    declare(workdir, fake_as.mcp_url)
    result = await mcp_handlers.mcp_authorize_handler(
        None,
        McpAuthorizeParams(name="remote", workdir=str(workdir), redirectUri="https://h.example/"),
        core,
    )
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_authorize_complete_handler(
            None, McpAuthorizeCompleteParams(state=result.state, error="access_denied"), core
        )
    assert caught.value.code == "mcp_auth_failed"
    assert auth_events(events)[-1]["ok"] is False
    assert "access_denied" in auth_events(events)[-1]["error"]


async def test_without_registration_a_configured_client_id_is_used(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
) -> None:
    fake_as.dcr = False
    declare(workdir, fake_as.mcp_url)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_authorize_handler(
            None, McpAuthorizeParams(name="remote", workdir=str(workdir)), core
        )
    assert caught.value.code == "mcp_auth_failed"
    assert caught.value.data == {"stage": "client"}

    await mcp_handlers.mcp_update_handler(
        None,
        McpUpdateParams(
            name="remote",
            workdir=str(workdir),
            patch=McpEntryFields(oauth={"clientId": "registered-app"}),  # type: ignore[arg-type]
        ),
        core,
    )
    result = await mcp_handlers.mcp_authorize_handler(
        None, McpAuthorizeParams(name="remote", workdir=str(workdir)), core
    )
    assert parse_qs(urlsplit(result.url).query)["client_id"] == ["registered-app"]
    assert fake_as.registrations == []
    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    assert listing.servers[0].auth.clientId == "registered-app"


async def test_a_stdio_server_cannot_sign_in(core: Core, workdir: Path) -> None:  # noqa: F811
    await mcp_handlers.mcp_add_handler(None, echo_params("echo", workdir, test=False), core)
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_authorize_handler(
            None, McpAuthorizeParams(name="echo", workdir=str(workdir)), core
        )
    assert caught.value.code == "mcp_invalid"


def record_for(fake_as: FakeAuthServer, access: str, refresh: str, **extra: Any) -> dict[str, Any]:
    return {
        "url": fake_as.mcp_url,
        "resource": fake_as.mcp_url,
        "token_endpoint": f"{fake_as.base}/token",
        "client_id": "client-1",
        "redirect_uri": "https://h.example/",
        "access_token": access,
        "refresh_token": refresh,
        **extra,
    }


async def test_bearer_auth_refreshes_once_and_keeps_the_rotated_token(
    tmp_path: Path, fake_as: FakeAuthServer
) -> None:
    fake_as.valid_refresh.add("rt-old")
    store = mcp_oauth.TokenStore(tmp_path)
    store.put("remote", record_for(fake_as, "at-stale", "rt-old"))
    auth = mcp_oauth.BearerAuth(store, "remote", fake_as.mcp_url)
    async with httpx2.AsyncClient(auth=auth) as client:
        response = await client.post(fake_as.mcp_url, json={})
    assert response.status_code == 200
    assert fake_as.seen_auth == ["Bearer at-stale", "Bearer at-1-secret"]
    saved = store.get("remote", fake_as.mcp_url)
    assert saved is not None
    assert saved["access_token"] == "at-1-secret"
    assert saved["refresh_token"] == "rt-1-secret"  # rotated
    assert not auth.unauthorized

    # The spent refresh token no longer works: once refused, needs_auth.
    fake_as.valid_access.clear()
    fake_as.valid_refresh.clear()
    async with httpx2.AsyncClient(auth=auth) as client:
        response = await client.post(fake_as.mcp_url, json={})
    assert response.status_code == 401
    assert auth.unauthorized


async def test_an_expired_token_is_refreshed_before_it_is_sent(
    tmp_path: Path, fake_as: FakeAuthServer
) -> None:
    fake_as.valid_refresh.add("rt-old")
    store = mcp_oauth.TokenStore(tmp_path)
    store.put("remote", record_for(fake_as, "at-old", "rt-old", expires_at=1.0))
    auth = mcp_oauth.BearerAuth(store, "remote", fake_as.mcp_url)
    async with httpx2.AsyncClient(auth=auth) as client:
        response = await client.post(fake_as.mcp_url, json={})
    assert response.status_code == 200
    assert fake_as.seen_auth == ["Bearer at-1-secret"]


async def test_a_url_change_invalidates_the_tokens(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
) -> None:
    declare(workdir, fake_as.mcp_url)
    mcp_oauth.TokenStore(core.paths.home).put("remote", record_for(fake_as, "at-x", "rt-x"))
    store = mcp_oauth.TokenStore(core.paths.home)
    assert store.get("remote", fake_as.mcp_url) is not None
    assert store.get("remote", fake_as.mcp_url + "/other") is None
    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    assert listing.servers[0].auth.state == "ok"

    await mcp_handlers.mcp_update_handler(
        None,
        McpUpdateParams(
            name="remote",
            workdir=str(workdir),
            patch=McpEntryFields(url=f"{fake_as.base}/elsewhere"),
        ),
        core,
    )
    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    assert listing.servers[0].auth.state != "ok"


async def test_a_401_at_connect_surfaces_needs_auth_and_still_saves(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
) -> None:
    result = await mcp_handlers.mcp_add_handler(
        None,
        McpAddParams(name="remote", scope="project", workdir=str(workdir), url=fake_as.mcp_url),
        core,
    )
    assert result.ok
    assert result.auth.state == "needs_auth" and result.auth.required
    assert "remote" in mcp_config.read_servers(workdir / ".mcp.json")
    assert result.error and mcp_client.NEEDS_AUTH_TEXT in result.error

    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    assert listing.servers[0].state == "error"
    assert listing.servers[0].auth.state == "needs_auth"


async def test_a_stored_token_is_sent_on_connect_and_deauthorize_drops_it(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
) -> None:
    declare(workdir, fake_as.mcp_url)
    mcp_oauth.TokenStore(core.paths.home).put("remote", record_for(fake_as, "at-sent", "rt-x"))
    mcp_client.MANAGER.home = core.paths.home
    row = mcp_handlers.declared(core, workdir)["remote"]
    await mcp_handlers.probe(row.config)
    assert "Bearer at-sent" in fake_as.seen_auth

    result = await mcp_handlers.mcp_deauthorize_handler(
        None, McpDeauthorizeParams(name="remote", workdir=str(workdir)), core
    )
    assert result.removed
    assert mcp_oauth.TokenStore(core.paths.home).get("remote", fake_as.mcp_url) is None
    again = await mcp_handlers.mcp_deauthorize_handler(
        None, McpDeauthorizeParams(name="remote", workdir=str(workdir)), core
    )
    assert not again.removed


async def test_a_configured_authorization_header_wins_over_a_token(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    fake_as: FakeAuthServer,
) -> None:
    declare(workdir, fake_as.mcp_url, headers={"Authorization": "Bearer from-entry"})
    mcp_oauth.TokenStore(core.paths.home).put("remote", record_for(fake_as, "at-sent", "rt-x"))
    mcp_client.MANAGER.home = core.paths.home
    await mcp_handlers.probe(mcp_handlers.declared(core, workdir)["remote"].config)
    assert fake_as.seen_auth and set(fake_as.seen_auth) == {"Bearer from-entry"}


# ---------------------------------------------------------------------------
# 3. ${VAR} expansion
# ---------------------------------------------------------------------------


def allow(core: Core, *names: str) -> None:  # noqa: F811
    settings = core.settings.model_copy(deep=True)
    settings.mcp.allowVars = list(names)
    core.settings = settings


def test_expansion_precedence_escape_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "credentials.json").write_text(
        json.dumps({"TOKEN": "from-credentials", "OBJ": {"token": "x"}}), encoding="utf-8"
    )
    monkeypatch.setenv("TOKEN", "from-env")
    monkeypatch.setenv("ONLY_ENV", "env-value")
    monkeypatch.setenv("OBJ", "env-obj")
    config = mcp_client.McpServerConfig(
        name="x",
        source="global",
        url="https://example.invalid/mcp",
        args=["--a=${ONLY_ENV}", "$${TOKEN}", "${MISSING:-fallback}"],
        headers={"Authorization": "Bearer ${TOKEN}", "X-Obj": "${OBJ}"},
    )
    resolved = mcp_client.resolve_config(config, tmp_path)
    assert resolved.headers == {"Authorization": "Bearer from-credentials", "X-Obj": "env-obj"}
    assert resolved.args == ["--a=env-value", "${TOKEN}", "fallback"]
    assert config.headers["Authorization"] == "Bearer ${TOKEN}"  # the entry is untouched


def test_an_unresolved_variable_names_itself_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    monkeypatch.setenv("SET_ONE", "do-not-print-me")
    config = mcp_client.McpServerConfig(
        name="x", command="srv", env={"A": "${SET_ONE}", "B": "${NOPE_NOT_SET}"}, source="global"
    )
    with pytest.raises(mcp_client.McpError) as caught:
        mcp_client.resolve_config(config, tmp_path)
    assert "${NOPE_NOT_SET}" in str(caught.value)
    assert "do-not-print-me" not in str(caught.value)


async def test_a_stdio_server_starts_with_expanded_args_and_lists_header_keys_only(
    core: Core,  # noqa: F811
    workdir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECHO_SCRIPT", str(FIXTURE_ECHO))
    mcp_config.save_entry(
        workdir / ".mcp.json",
        "echo",
        {"command": sys.executable, "args": ["${ECHO_SCRIPT}"]},
    )
    result = await mcp_handlers.mcp_test_handler(
        None, mcp_handlers.McpTestParams(name="echo", workdir=str(workdir)), core
    )
    # A project entry may not expand a name the user did not allow (H1) ...
    assert not result.ok
    assert result.error == (
        "echo: ${ECHO_SCRIPT} is not allowed in a project entry; add it to settings.mcp.allowVars"
    )
    # ... and may once it is in settings.mcp.allowVars.
    allow(core, "ECHO_SCRIPT")
    result = await mcp_handlers.mcp_test_handler(
        None, mcp_handlers.McpTestParams(name="echo", workdir=str(workdir)), core
    )
    assert result.ok, result.error

    mcp_config.save_entry(
        workdir / ".mcp.json",
        "web",
        {
            "url": "https://example.invalid/mcp",
            "headers": {"Authorization": "Bearer ${ECHO_SCRIPT}"},
        },
    )
    listing = await mcp_handlers.mcp_list_handler(None, McpListParams(workdir=str(workdir)), core)
    echo_row = next(row for row in listing.servers if row.name == "echo")
    assert echo_row.args == ["${ECHO_SCRIPT}"]  # listed as written, never expanded
    web_row = next(row for row in listing.servers if row.name == "web")
    assert web_row.headerKeys == ["Authorization"]

    monkeypatch.delenv("ECHO_SCRIPT")
    failed = await mcp_handlers.mcp_test_handler(
        None, mcp_handlers.McpTestParams(name="echo", workdir=str(workdir)), core
    )
    assert not failed.ok and "${ECHO_SCRIPT}" in (failed.error or "")


# ---------------------------------------------------------------------------
# 4. the catalog
# ---------------------------------------------------------------------------


async def test_catalog_rows_say_how_they_authenticate(core: Core) -> None:  # noqa: F811
    result = await mcp_handlers.mcp_catalog_handler(None, mcp_handlers.Empty(), core)
    rows = {row.id: row for row in result.entries}
    assert rows["linear"].auth == "oauth"
    assert rows["linear"].entry == {"url": "https://mcp.linear.app/mcp"}
    assert rows["github"].auth == "token"
    assert rows["github"].entry["url"] == "https://api.githubcopilot.com/mcp/"
    assert "${GITHUB_PERSONAL_ACCESS_TOKEN}" in rows["github"].entry["headers"]["Authorization"]
    for row in result.entries:
        assert row.auth in ("oauth", "token", "none")
        for need in row.needs:
            assert f"${{{need}}}" in json.dumps(row.entry)
        for arg in row.entry.get("args", []):
            assert "@latest" not in arg
    assert "@modelcontextprotocol/server-github" not in json.dumps(mcp_catalog.CATALOG)


# ---------------------------------------------------------------------------
# 2. settings.skills.disabled
# ---------------------------------------------------------------------------


def write_skill(root: Path, name: str, body: str = "Do the thing.", extra: str = "") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: about {name}\n{extra}---\n\n{body}\n", encoding="utf-8"
    )


@pytest.fixture
def loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillLoader:
    home = tmp_path / "home"
    builtins = tmp_path / "builtins"
    builtins.mkdir()
    write_skill(home / "skills", "deploy")
    write_skill(
        home / "skills", "routine", extra="autoInject: {keywords: [routine]}\n", body="Routine."
    )
    monkeypatch.setattr("snowpea_core.skills.loader.builtin_root", lambda: builtins)
    fake = SimpleNamespace(
        paths=Paths(home=home),
        commands=CommandRegistry(),
        sessions=SimpleNamespace(list=lambda: [SimpleNamespace(workdir=str(tmp_path))]),
        hub=None,
        settings=Settings(),
    )
    built = SkillLoader(fake)  # type: ignore[arg-type]
    built.load_sync()
    fake.skills = built
    return built


def disable(loader: SkillLoader, *names: str) -> None:
    settings = loader.core.settings.model_copy(deep=True)
    settings.skills.disabled = list(names)
    loader.core.settings = settings


async def test_an_empty_disabled_list_changes_nothing(loader: SkillLoader) -> None:
    before = loader.index_groups()
    commands = sorted(info.name for info in loader.core.commands.list())
    disable(loader)
    await loader.apply_enabled()
    assert loader.index_groups() == before
    assert sorted(info.name for info in loader.core.commands.list()) == commands
    assert all(info.enabled for info in loader.list())


async def test_a_disabled_skill_leaves_index_commands_and_injection(
    loader: SkillLoader, tmp_path: Path
) -> None:
    assert "deploy" in json.dumps(loader.index_groups())
    disable(loader, "deploy", "routine")
    await loader.apply_enabled()
    assert "deploy" not in json.dumps(loader.index_groups())
    assert "deploy" not in {info.name for info in loader.core.commands.list()}
    rows = {info.name: info for info in loader.list()}
    assert rows["deploy"].enabled is False and rows["routine"].enabled is False

    session = Session(id="s-off", workdir=tmp_path)
    assert await agent_loop._auto_inject_skills(loader.core, session, "a routine task") == []

    ctx = ToolContext(
        session=SimpleNamespace(id="s", workdir=str(tmp_path), skill_views={}),
        core=loader.core,  # type: ignore[arg-type]
        backend=None,  # type: ignore[arg-type]
    )
    viewed = await skills_tools.skill_view(ctx, {"name": "deploy"})
    assert not viewed.ok and "turned off" in (viewed.error or "")
    listed = await skills_tools.skill_list(ctx, {})
    assert "deploy [global] (disabled)" in listed.output
    # Still loaded, so skill.read and skill.remove find it.
    assert "deploy" in loader.skills

    disable(loader)
    await loader.apply_enabled()
    assert "deploy" in {info.name for info in loader.core.commands.list()}
    session = Session(id="s-on", workdir=tmp_path)
    assert await agent_loop._auto_inject_skills(loader.core, session, "a routine task") == [
        "routine"
    ]


async def test_skill_list_output_is_byte_identical_with_nothing_disabled(
    loader: SkillLoader, tmp_path: Path
) -> None:
    ctx = ToolContext(
        session=SimpleNamespace(id="s", workdir=str(tmp_path), skill_views={}),
        core=loader.core,  # type: ignore[arg-type]
        backend=None,  # type: ignore[arg-type]
    )
    listed = await skills_tools.skill_list(ctx, {})
    assert "- deploy [global] — about deploy" in listed.output
    assert "(disabled)" not in listed.output


async def test_a_settings_change_applies_the_toggle(core: Core, monkeypatch: Any) -> None:  # noqa: F811
    calls: list[str] = []

    async def applied() -> None:
        calls.append("applied")

    core.skills = SimpleNamespace(apply_enabled=applied)  # type: ignore[assignment]
    await core.notify_settings_changed(["skills"])
    await core.notify_settings_changed(["mcp"])
    assert calls == ["applied"]


def test_the_setting_round_trips() -> None:
    settings = Settings.model_validate({"skills": {"disabled": ["a", "plug:b"]}})
    assert settings.skills.disabled == ["a", "plug:b"]
    assert Settings().skills.disabled == []
