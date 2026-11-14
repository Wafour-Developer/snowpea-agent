"""The daemon serves the snowpea web build at /web/ (M18 4.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.transport_http import create_app


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    (root / "assets" / "main-abc.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(root))
    return root


async def _client(core: Any = None) -> TestClient:
    app = create_app(RpcDispatcher(core), None, core)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_index_assets_and_client_routes(dist: Path) -> None:
    client = await _client()
    try:
        index = await client.get("/web/")
        assert index.status == 200 and "id=app" in await index.text()
        assert index.headers["Cache-Control"] == "no-cache"
        asset = await client.get("/web/assets/main-abc.js")
        assert asset.status == 200 and "immutable" in asset.headers["Cache-Control"]
        assert asset.headers["Content-Type"].endswith("javascript")
        route = await client.get("/web/sessions/s-1")
        assert route.status == 200 and "id=app" in await route.text()
        escape = await client.get("/web/../secret.txt")
        assert "nope" not in await escape.text()
        moved = await client.get("/web?token=abc", allow_redirects=False)
        assert moved.status == 302 and moved.headers["Location"] == "/web/?token=abc"
    finally:
        await client.close()


async def test_a_build_without_the_web_ui_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.server import transport_http

    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(tmp_path / "missing"))
    monkeypatch.setattr(transport_http, "PACKAGED_WEB", tmp_path / "also-missing")
    client = await _client()
    try:
        response = await client.get("/web/")
        assert response.status == 404 and "build:web" in await response.text()
    finally:
        await client.close()
