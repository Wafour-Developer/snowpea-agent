"""OAuth for remote MCP servers (1.34.0, capability ``mcpOAuth``).

The MCP authorization spec, client side: OAuth 2.1 with PKCE S256.  A server
that wants a token answers ``401`` with ``WWW-Authenticate: Bearer
resource_metadata=...``; that names its RFC 9728 protected resource metadata,
which names an authorization server, whose RFC 8414 metadata (or OpenID
configuration) gives the endpoints.  The client registers itself with RFC 7591
dynamic client registration when the server offers it, else uses the entry's
``oauth.clientId``.  Every authorization and token request carries the RFC
8707 ``resource`` indicator.

Tokens live in ``$SNOWPEA_HOME/mcp-oauth.json`` (0600, written like
``credentials.json``, fsynced), keyed by server name and canonical resource
URL: a record whose URL is not the entry's any more is never used, so changing
the URL invalidates it, and a shadowing entry of the same name cannot overwrite
another URL's record.  Discovery only uses https (or loopback) URLs, refuses
internal addresses the server is not on, follows same-origin metadata
redirects only, and checks the resource, the issuer and RFC 9207 ``iss``.
Nothing in this module logs or returns a token, a code, a
verifier or a client secret; errors name the step and the HTTP status only.

:class:`BearerAuth` is what the transports use: it adds ``Authorization:
Bearer`` from the store, refreshes once when the server answers 401 (keeping a
rotated refresh token), and remembers that the server still refused so the
server can surface ``needs_auth`` instead of a generic error.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
import httpx2

log = logging.getLogger("snowpea.tools.mcp.oauth")

#: Beside ``credentials.json`` in ``$SNOWPEA_HOME``, with the same mode.
OAUTH_FILE = "mcp-oauth.json"
FILE_MODE = 0o600
#: Every metadata, registration and token request is capped at this.
HTTP_TIMEOUT = 15.0
#: An access token this close to its expiry is refreshed before it is sent.
EXPIRY_SKEW = 30.0
#: Name a dynamically registered client gives itself.
CLIENT_NAME = "Snowpea"
#: What the MCP spec says goes with a metadata request.
PROTOCOL_HEADER = "MCP-Protocol-Version"
PROTOCOL_VALUE = "2025-06-18"


class OAuthError(RuntimeError):
    """One step of the flow failed; ``stage`` says which, the text never holds a secret."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


# ---------------------------------------------------------------------------
# the token file
# ---------------------------------------------------------------------------


def canonical_resource(url: str) -> str:
    """The RFC 8707 form of a server URL: lower-case scheme and host, no fragment."""
    parts = urlsplit(url)
    return urlunsplit(
        parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), fragment="")
    )


def _same_resource(a: str, b: str) -> bool:
    return canonical_resource(a).rstrip("/") == canonical_resource(b).rstrip("/")


def record_key(name: str, url: str) -> str:
    """``<name>|<canonical resource>``: a server name never holds ``|`` (``NAME_RE``).

    Keying by the resource too means a project entry that shadows a global one
    under the same name, but points elsewhere, never overwrites its tokens.
    """
    return f"{name}|{canonical_resource(url).rstrip('/')}"


class TokenStore:
    """``$SNOWPEA_HOME/mcp-oauth.json``: ``{"servers": {"<name>|<resource>": record}}``.

    The first release keyed records by bare name; :meth:`load` reads that
    shape too and files each record under its new key.
    """

    def __init__(self, home: Path | str) -> None:
        self.home = Path(home).expanduser()
        self.path = self.home / OAUTH_FILE

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s; treating it as empty", self.path)
            return {}
        servers = raw.get("servers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, record in servers.items():
            if not isinstance(record, dict):
                continue
            key = str(key)
            if "|" not in key:  # the 1.34.0-draft shape: keyed by name alone
                if not record.get("url"):
                    continue
                key = record_key(key, str(record["url"]))
            out[key] = record
        return out

    def _save(self, servers: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps({"servers": servers}, indent=2, ensure_ascii=False) + "\n"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, FILE_MODE)
        tmp.replace(self.path)
        os.chmod(self.path, FILE_MODE)

    def get(self, name: str, url: str | None) -> dict[str, Any] | None:
        """The record for ``name`` at ``url``, only while it was issued for that URL."""
        if not url:
            return None
        record = self.load().get(record_key(name, url))
        if record is None or not _same_resource(str(record.get("url") or ""), url):
            return None
        if not record.get("access_token"):
            return None
        return record

    def put(self, name: str, record: dict[str, Any]) -> None:
        servers = self.load()
        servers[record_key(name, str(record["url"]))] = record
        self._save(servers)

    def drop(self, name: str, url: str | None = None) -> bool:
        """Forget ``name`` at ``url``, or every record of ``name`` without one."""
        servers = self.load()
        if url:
            doomed = [record_key(name, url)]
        else:
            doomed = [key for key in servers if key.partition("|")[0] == name]
        doomed = [key for key in doomed if key in servers]
        if not doomed:
            return False
        for key in doomed:
            servers.pop(key)
        self._save(servers)
        return True


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


@dataclass
class Discovery:
    """What the server and its authorization server published."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    #: From ``WWW-Authenticate``, else the resource's ``scopes_supported``.
    scope: str | None = None
    auth_methods: tuple[str, ...] = ()
    #: The server advertised ``authorization_response_iss_parameter_supported``
    #: (RFC 9207): the redirect must then carry ``iss`` equal to :attr:`issuer`.
    iss_required: bool = False


def www_auth_field(header: str | None, field_name: str) -> str | None:
    """One ``key="value"`` (or bare ``key=value``) from a ``WWW-Authenticate`` header."""
    if not header:
        return None
    match = re.search(rf'{field_name}=(?:"([^"]*)"|([^\s,]+))', header)
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    ip = _host_ip(host)
    return ip is not None and ip.is_loopback


def _internal(host: str) -> bool:
    """A loopback, private, link-local or otherwise non-public IP literal (or localhost)."""
    if host == "localhost":
        return True
    ip = _host_ip(host)
    if ip is None:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def checked_url(url: Any, what: str, server_url: str, stage: str = "metadata") -> str:
    """``url`` when discovery may use it, else :class:`OAuthError`.

    It must be https, or http on loopback; and a loopback, private or
    link-local IP literal is allowed only when the MCP server itself is on
    that very host, so a hostile server cannot point the daemon at another
    machine on the user's network or at the daemon's own neighbours.
    """
    text = str(url or "")
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("https", "http"):
        raise OAuthError(stage, f"the {what} is not an https URL")
    if parts.scheme == "http" and not _loopback(text):
        raise OAuthError(stage, f"the {what} is not an https URL")
    if _internal(host) and host != (urlsplit(server_url).hostname or "").lower():
        raise OAuthError(stage, f"the {what} points at a private or loopback address")
    return text


def resource_metadata_urls(hint: str | None, server_url: str) -> list[str]:
    """RFC 9728 candidates: the header's hint, then path-aware, then root well-known."""
    urls = [hint] if hint else []
    parts = urlsplit(server_url)
    base = f"{parts.scheme}://{parts.netloc}"
    if parts.path and parts.path != "/":
        urls.append(urljoin(base, f"/.well-known/oauth-protected-resource{parts.path}"))
    urls.append(urljoin(base, "/.well-known/oauth-protected-resource"))
    return urls


def server_metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 candidates for ``issuer``, with the OpenID Connect fallbacks."""
    parts = urlsplit(issuer)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if path:
        return [
            urljoin(base, f"/.well-known/oauth-authorization-server{path}"),
            urljoin(base, f"/.well-known/openid-configuration{path}"),
            urljoin(base, f"{path}/.well-known/openid-configuration"),
        ]
    return [
        urljoin(base, "/.well-known/oauth-authorization-server"),
        urljoin(base, "/.well-known/openid-configuration"),
    ]


#: Same-origin redirects a metadata GET follows before it gives up.
MAX_REDIRECTS = 5


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    """A metadata document; redirects are followed only within ``url``'s origin."""
    target = url
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            response = await client.get(
                target, headers={PROTOCOL_HEADER: PROTOCOL_VALUE}, follow_redirects=False
            )
        except httpx.HTTPError:
            return None
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return None
            following = urljoin(target, location)
            if _origin(following) != _origin(url):
                return None
            target = following
            continue
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


async def challenge(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> str | None:
    """The ``WWW-Authenticate`` a bare ``initialize`` POST gets, when it is refused."""
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VALUE,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1"},
        },
    }
    sent = {k: v for k, v in (headers or {}).items() if k.lower() != "authorization"}
    sent["Accept"] = "application/json, text/event-stream"
    try:
        async with client.stream(
            "POST", url, json=body, headers=sent, follow_redirects=False
        ) as response:
            if response.status_code in (401, 403):
                return response.headers.get("WWW-Authenticate") or ""
    except httpx.HTTPError:
        return None
    return None


def _same_issuer(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


async def discover(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> Discovery:
    """Walk ``401`` → protected resource metadata → authorization server metadata.

    Every URL on the way is checked by :func:`checked_url`; the resource the
    metadata names must be ``url`` itself, and the authorization server's
    ``issuer`` the one it was fetched for.
    """
    header = await challenge(client, url, headers)
    hint = www_auth_field(header, "resource_metadata")
    if hint:
        hint = checked_url(hint, "resource_metadata URL", url, "discovery")
    scope = www_auth_field(header, "scope")
    resource_meta: dict[str, Any] | None = None
    for candidate in resource_metadata_urls(hint, url):
        resource_meta = await _get_json(client, checked_url(candidate, "metadata URL", url))
        if resource_meta is not None:
            break
    if resource_meta is None:
        raise OAuthError(
            "discovery",
            "the server publishes no OAuth protected resource metadata (RFC 9728); "
            "it does not support sign-in, or wants a token in a header instead",
        )
    resource = str(resource_meta.get("resource") or "")
    if not resource or not _same_resource(resource, url):
        raise OAuthError(
            "metadata", "the protected resource metadata is for another resource than this server"
        )
    servers = resource_meta.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise OAuthError("discovery", "the resource metadata names no authorization server")
    issuer = checked_url(servers[0], "authorization server", url)
    server_meta: dict[str, Any] | None = None
    for candidate in server_metadata_urls(issuer):
        server_meta = await _get_json(client, checked_url(candidate, "metadata URL", url))
        if server_meta is not None:
            break
    if server_meta is None:
        raise OAuthError(
            "discovery", f"no authorization server metadata (RFC 8414) found for {issuer}"
        )
    if not _same_issuer(str(server_meta.get("issuer") or ""), issuer):
        raise OAuthError(
            "metadata", "the authorization server metadata names another issuer than its own"
        )
    methods = server_meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise OAuthError("metadata", "the authorization server does not support PKCE S256")
    if scope is None:
        supported = resource_meta.get("scopes_supported")
        if isinstance(supported, list) and supported:
            scope = " ".join(str(item) for item in supported)
    registration = server_meta.get("registration_endpoint")
    auth_methods = server_meta.get("token_endpoint_auth_methods_supported")
    return Discovery(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=checked_url(
            server_meta.get("authorization_endpoint"), "authorization endpoint", url
        ),
        token_endpoint=checked_url(server_meta.get("token_endpoint"), "token endpoint", url),
        registration_endpoint=checked_url(registration, "registration endpoint", url)
        if registration
        else None,
        scope=scope,
        auth_methods=tuple(str(m) for m in auth_methods) if isinstance(auth_methods, list) else (),
        iss_required=server_meta.get("authorization_response_iss_parameter_supported") is True,
    )


def check_iss(record: dict[str, Any], iss: str | None) -> None:
    """RFC 9207: the redirect's ``iss`` must be the issuer, and present when required."""
    expected = str(record.get("issuer") or "")
    if iss is None:
        if record.get("iss_required"):
            raise OAuthError("callback", "the redirect carried no iss, which this server requires")
        return
    if not _same_issuer(iss, expected):
        raise OAuthError("callback", "the redirect's iss is not this authorization server")


async def register(
    client: httpx.AsyncClient, found: Discovery, redirect_uri: str, scope: str | None
) -> dict[str, Any]:
    """RFC 7591: a public client for ``redirect_uri``; ``{client_id, client_secret?, method}``."""
    assert found.registration_endpoint is not None
    body: dict[str, Any] = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        body["scope"] = scope
    try:
        response = await client.post(found.registration_endpoint, json=body, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise OAuthError(
            "registration", f"client registration failed: {type(exc).__name__}"
        ) from None
    if response.status_code not in (200, 201):
        raise OAuthError(
            "registration", f"client registration was refused (HTTP {response.status_code})"
        )
    try:
        info = response.json()
    except ValueError:
        info = None
    if not isinstance(info, dict) or not info.get("client_id"):
        raise OAuthError("registration", "client registration returned no client_id")
    return {
        "client_id": str(info["client_id"]),
        "client_secret": str(info["client_secret"]) if info.get("client_secret") else None,
        "auth_method": str(info.get("token_endpoint_auth_method") or "none"),
    }


def authorize_url(
    found: Discovery,
    *,
    client_id: str,
    redirect_uri: str,
    challenge_s256: str,
    state: str,
    scope: str | None,
) -> str:
    """The consent URL the user opens."""
    query: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge_s256,
        "code_challenge_method": "S256",
        "state": state,
        "resource": found.resource,
    }
    if scope:
        query["scope"] = scope
    sep = "&" if "?" in found.authorization_endpoint else "?"
    return f"{found.authorization_endpoint}{sep}{urlencode(query)}"


async def _token_request(
    client: httpx.AsyncClient, record: dict[str, Any], form: dict[str, str], stage: str
) -> dict[str, Any]:
    """POST the token endpoint with the client's credentials; the parsed answer."""
    form = {**form, "client_id": str(record["client_id"])}
    if record.get("resource"):
        form["resource"] = str(record["resource"])
    auth: httpx.BasicAuth | None = None
    secret = record.get("client_secret")
    if secret:
        if record.get("auth_method") == "client_secret_basic":
            auth = httpx.BasicAuth(str(record["client_id"]), str(secret))
        else:
            form["client_secret"] = str(secret)
    try:
        response = await client.post(
            str(record["token_endpoint"]),
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(stage, f"the token request failed: {type(exc).__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, dict) or "error" in body:
        code = body.get("error") if isinstance(body, dict) else None
        detail = f" ({code})" if isinstance(code, str) and code.isascii() and len(code) < 64 else ""
        raise OAuthError(
            stage, f"the token endpoint refused the request (HTTP {response.status_code}){detail}"
        )
    if not body.get("access_token"):
        raise OAuthError(stage, "the token endpoint returned no access_token")
    token_type = str(body.get("token_type") or "Bearer")
    if token_type.lower() != "bearer":
        raise OAuthError(stage, "the token endpoint returned a non-Bearer token")
    return body


def _apply_tokens(record: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out["access_token"] = str(body["access_token"])
    # Rotation: a new refresh token replaces the old one; none keeps it.
    if body.get("refresh_token"):
        out["refresh_token"] = str(body["refresh_token"])
    if body.get("scope"):
        out["scope"] = str(body["scope"])
    try:
        expires_in = float(body["expires_in"]) if body.get("expires_in") is not None else None
    except (TypeError, ValueError):
        expires_in = None
    out["expires_at"] = time.time() + expires_in if expires_in else None
    out["obtained_at"] = time.time()
    return out


async def exchange(
    record: dict[str, Any], code: str, verifier: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Authorization code → the record with tokens filled in."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": str(record["redirect_uri"]),
        "code_verifier": verifier,
    }
    async with _client(client) as http:
        body = await _token_request(http, record, form, "exchange")
    return _apply_tokens(record, body)


async def refresh(
    record: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """A refresh grant; the record with the new access (and rotated refresh) token."""
    if not record.get("refresh_token"):
        raise OAuthError("refresh", "no refresh token is stored")
    form = {"grant_type": "refresh_token", "refresh_token": str(record["refresh_token"])}
    async with _client(client) as http:
        body = await _token_request(http, record, form, "refresh")
    return _apply_tokens(record, body)


@contextlib.asynccontextmanager
async def _client(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    """``client`` as is, or a fresh one that is closed afterwards."""
    if client is not None:
        yield client
        return
    async with new_client() as own:
        yield own


def new_client() -> httpx.AsyncClient:
    """Redirects are never followed by default; :func:`_get_json` follows same-origin ones."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# the transports' side
# ---------------------------------------------------------------------------


#: ``(home, name)`` -> ``(loop, lock)``: one refresh at a time per server across
#: every connection and probe, so a rotated refresh token is never spent twice.
_REFRESH_LOCKS: dict[tuple[str, str], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def refresh_lock(home: Path | str | None, name: str) -> asyncio.Lock:
    """The process-wide refresh lock for ``name`` under ``home``.

    A lock belongs to one event loop; a new loop (a restarted daemon in the
    same process, a test) gets a fresh one.
    """
    key = (str(home or ""), name)
    loop = asyncio.get_running_loop()
    held = _REFRESH_LOCKS.get(key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _REFRESH_LOCKS[key] = held
    return held[1]


class BearerAuth(httpx2.Auth):
    """``Authorization: Bearer`` from the store; one refresh on a 401.

    ``unauthorized`` is True once the server refused a request even after
    the refresh (or with nothing to refresh): the server then reports
    ``needs_auth``.  With no stored record the request goes out untouched, so
    a server that never asks for OAuth sees exactly what it saw before.
    """

    def __init__(self, store: TokenStore, name: str, url: str) -> None:
        self.store = store
        self.name = name
        self.url = url
        self.unauthorized = False

    def _record(self) -> dict[str, Any] | None:
        try:
            return self.store.get(self.name, self.url)
        except OSError:
            return None

    async def _refreshed(self, used: str | None) -> dict[str, Any] | None:
        """A fresh record, refreshing unless another request already did."""
        async with refresh_lock(getattr(self.store, "home", None), self.name):
            record = self._record()
            if record is None:
                return None
            if used is not None and record.get("access_token") != used:
                return record  # a concurrent request refreshed it meanwhile
            try:
                updated = await refresh(record)
            except OAuthError as exc:
                log.info("mcp server %s: token refresh failed: %s", self.name, exc)
                return None
            self.store.put(self.name, updated)
            return updated

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        record = self._record()
        if record is not None:
            expires = record.get("expires_at")
            if expires and time.time() > float(expires) - EXPIRY_SKEW:
                record = await self._refreshed(record.get("access_token")) or record
            request.headers["Authorization"] = f"Bearer {record['access_token']}"
        response = yield request
        if response.status_code != 401:
            if response.status_code < 400:
                self.unauthorized = False
            return
        if record is not None and record.get("refresh_token"):
            fresh = await self._refreshed(record.get("access_token"))
            if fresh is not None:
                request.headers["Authorization"] = f"Bearer {fresh['access_token']}"
                response = yield request
                if response.status_code != 401:
                    self.unauthorized = False
                    return
        self.unauthorized = True


__all__ = [
    "OAUTH_FILE",
    "BearerAuth",
    "Discovery",
    "OAuthError",
    "TokenStore",
    "authorize_url",
    "canonical_resource",
    "challenge",
    "discover",
    "exchange",
    "new_client",
    "new_state",
    "check_iss",
    "checked_url",
    "record_key",
    "refresh",
    "refresh_lock",
    "register",
    "resource_metadata_urls",
    "server_metadata_urls",
    "www_auth_field",
]
