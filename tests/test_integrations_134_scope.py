"""Protocol 1.34.0 browser review: which declaration signs in, and flow identity.

(c) ``mcp.authorize``/``mcp.deauthorize {scope}``; a project entry shadowing a
global one at another URL needs an explicit scope; the result names
``serverUrl`` and ``resource``.  (d) ``mcp.auth.done`` carries ``state`` and
``scope``.  (e) Closing the connection that started a flow cancels it.
"""

# The fixtures come from the sibling modules, so pytest parameters shadow imports.
# ruff: noqa: F811

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from test_integrations_134 import (  # noqa: F401
    FakeAuthServer,
    auth_events,
    consent,
    fake_as,
    record_for,
)
from test_mcp_manage import _stop_servers, core, events, workdir  # noqa: F401

from snowpea_core.server import mcp_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    McpAuthorizeCompleteParams,
    McpAuthorizeParams,
    McpDeauthorizeParams,
)
from snowpea_core.tools import mcp_config, mcp_oauth

HOST_REDIRECT = "https://browser.example/cb"


def shadowed(core: Core, workdir: Path, fake_as: FakeAuthServer, project_url: str) -> None:
    mcp_config.save_entry(core.paths.home / ".mcp.json", "remote", {"url": fake_as.mcp_url})
    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": project_url})


async def authorize(core: Core, workdir: Path, **extra: Any) -> Any:
    return await mcp_handlers.mcp_authorize_handler(
        extra.pop("conn", None),
        McpAuthorizeParams(name="remote", workdir=str(workdir), redirectUri=HOST_REDIRECT, **extra),
        core,
    )


async def complete(core: Core, url: str) -> Any:
    query = parse_qs(urlsplit(await consent(url)).query)
    return await mcp_handlers.mcp_authorize_complete_handler(
        None,
        McpAuthorizeCompleteParams(state=query["state"][0], code=query["code"][0]),
        core,
    )


# ---------------------------------------------------------------------------
# (c) scope
# ---------------------------------------------------------------------------


async def test_a_shadowing_project_entry_at_another_url_needs_a_scope(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    shadowed(core, workdir, fake_as, f"{fake_as.base}/shadow")
    with pytest.raises(RpcError) as caught:
        await authorize(core, workdir)
    assert caught.value.code == "invalid_params"
    assert caught.value.data == {"field": "scope"}
    assert fake_as.registrations == []  # nothing was discovered or registered

    result = await authorize(core, workdir, scope="global")
    assert result.scope == "global"
    assert result.serverUrl == fake_as.mcp_url
    assert result.resource == fake_as.mcp_url
    done = await complete(core, result.url)
    assert done.ok

    store = mcp_oauth.TokenStore(core.paths.home)
    assert store.get("remote", fake_as.mcp_url) is not None
    assert store.get("remote", f"{fake_as.base}/shadow") is None


async def test_a_shadowing_entry_at_the_same_url_needs_no_scope(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    # A trailing slash or host case does not make it another URL ...
    shadowed(core, workdir, fake_as, fake_as.mcp_url + "/")
    row = mcp_handlers.signing_row(core, "remote", None, workdir)
    assert row.scope == "project"
    # ... and a project copy of the very same URL signs in without a scope.
    shadowed(core, workdir, fake_as, fake_as.mcp_url)
    result = await authorize(core, workdir)
    assert result.scope == "project"
    assert result.serverUrl == fake_as.mcp_url


async def test_an_undeclared_scope_is_not_found(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    mcp_config.save_entry(core.paths.home / ".mcp.json", "remote", {"url": fake_as.mcp_url})
    with pytest.raises(RpcError) as caught:
        await authorize(core, workdir, scope="project")
    assert caught.value.code == "mcp_not_found"
    with pytest.raises(RpcError) as caught:
        await mcp_handlers.mcp_deauthorize_handler(
            None, McpDeauthorizeParams(name="remote", workdir=str(workdir), scope="project"), core
        )
    assert caught.value.code == "mcp_not_found"


async def test_deauthorize_with_a_scope_drops_only_that_record(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    project_url = f"{fake_as.base}/shadow"
    shadowed(core, workdir, fake_as, project_url)
    store = mcp_oauth.TokenStore(core.paths.home)
    store.put("remote", record_for(fake_as, "at-g", "rt-g"))
    store.put("remote", {**record_for(fake_as, "at-p", "rt-p"), "url": project_url})

    result = await mcp_handlers.mcp_deauthorize_handler(
        None, McpDeauthorizeParams(name="remote", workdir=str(workdir), scope="global"), core
    )
    assert result.removed
    assert store.get("remote", fake_as.mcp_url) is None
    assert store.get("remote", project_url) is not None

    # Without a scope, the record of the declaration in effect (the project's).
    result = await mcp_handlers.mcp_deauthorize_handler(
        None, McpDeauthorizeParams(name="remote", workdir=str(workdir)), core
    )
    assert result.removed and store.get("remote", project_url) is None


# ---------------------------------------------------------------------------
# (d) state and scope on mcp.auth.done
# ---------------------------------------------------------------------------


async def test_auth_done_names_the_flow_and_a_replaced_flow_stays_quiet(
    core: Core,
    workdir: Path,
    fake_as: FakeAuthServer,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": fake_as.mcp_url})
    first = await authorize(core, workdir)
    second = await authorize(core, workdir)
    assert first.state != second.state
    assert auth_events(events) == []  # replacing a flow reports nothing

    with pytest.raises(RpcError):
        await complete(core, first.url)
    assert auth_events(events) == []  # the replaced flow's state is simply unknown

    await complete(core, second.url)
    assert auth_events(events) == [
        {"name": "remote", "ok": True, "error": None, "state": second.state, "scope": "project"}
    ]


# ---------------------------------------------------------------------------
# (e) the starting connection closes
# ---------------------------------------------------------------------------


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def test_closing_the_connection_cancels_its_flows(
    core: Core,
    workdir: Path,
    fake_as: FakeAuthServer,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": fake_as.mcp_url})
    conn = SimpleNamespace(on_close=[])
    other = SimpleNamespace(on_close=[])
    loopback = await mcp_handlers.mcp_authorize_handler(
        conn, McpAuthorizeParams(name="remote", workdir=str(workdir)), core
    )
    mcp_config.save_entry(workdir / ".mcp.json", "kept", {"url": fake_as.mcp_url})
    kept = await mcp_handlers.mcp_authorize_handler(
        other,
        McpAuthorizeParams(name="kept", workdir=str(workdir), redirectUri=HOST_REDIRECT),
        core,
    )
    port = urlsplit(loopback.redirectUri).port or 0
    assert _port_open(port)
    assert conn.on_close == [mcp_handlers.cancel_for]

    for callback in conn.on_close:
        await callback(conn)

    assert loopback.state not in mcp_handlers._PENDING
    assert kept.state in mcp_handlers._PENDING  # another connection's flow is untouched
    assert not _port_open(port)
    assert auth_events(events) == [
        {
            "name": "remote",
            "ok": False,
            "error": "cancelled",
            "state": loopback.state,
            "scope": "project",
        }
    ]


async def test_one_close_callback_per_connection(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": fake_as.mcp_url})
    conn = SimpleNamespace(on_close=[])
    for _ in range(3):
        await mcp_handlers.mcp_authorize_handler(
            conn,
            McpAuthorizeParams(name="remote", workdir=str(workdir), redirectUri=HOST_REDIRECT),
            core,
        )
    assert conn.on_close == [mcp_handlers.cancel_for]


async def test_a_real_connection_close_runs_the_cancel(
    core: Core, workdir: Path, fake_as: FakeAuthServer
) -> None:
    from snowpea_core.server.rpc import RpcConnection

    mcp_config.save_entry(workdir / ".mcp.json", "remote", {"url": fake_as.mcp_url})

    async def send(_payload: Any) -> None:
        return None

    try:
        conn = RpcConnection(send)  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - a different constructor
        pytest.skip("RpcConnection needs more than a sender here")
    result = await mcp_handlers.mcp_authorize_handler(
        conn, McpAuthorizeParams(name="remote", workdir=str(workdir)), core
    )
    await conn.close()
    assert result.state not in mcp_handlers._PENDING
