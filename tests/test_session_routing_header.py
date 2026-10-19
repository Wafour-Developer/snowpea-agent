"""``X-Session-ID`` on requests to a self-hosted OpenAI-compatible server.

``llm.snowpea.ai`` runs vllm-router with ``consistent_hash`` on this header.
Without it consecutive rounds of one turn landed on whichever worker the
router picked, so the ~100k-token prefix was recomputed every round (20-60 s
before the first token instead of about 2 s).  A local-style vendor now gets
the session id on every model call; a hosted vendor never does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _support import make_daemon
from aiohttp import web

from snowpea_core.providers import routing
from snowpea_core.providers.openai_compat import OpenAICompatProvider
from snowpea_core.providers.presets import PRESETS


def test_a_local_vendor_sends_the_session_and_a_hosted_one_does_not() -> None:
    local = OpenAICompatProvider(PRESETS["local"], model="m", base_url="http://127.0.0.1:9/v1")
    hosted = OpenAICompatProvider(
        PRESETS["openai"], api_key="sk-test", model="gpt", base_url="https://api.test/v1"
    )
    assert routing.SESSION_HEADER not in local._client().headers  # noqa: SLF001
    with routing.for_session("s-abc"):
        assert local._client().headers[routing.SESSION_HEADER] == "s-abc"  # noqa: SLF001
        assert routing.SESSION_HEADER not in hosted._client().headers  # noqa: SLF001
    assert routing.current_session() is None


@pytest.mark.asyncio
async def test_every_round_of_a_turn_carries_its_session(tmp_path: Path) -> None:
    seen: list[tuple[str | None, int]] = []

    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m"}]})

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        seen.append((request.headers.get(routing.SESSION_HEADER), len(body["messages"])))
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        first_round = not any(m["role"] == "tool" for m in body["messages"])
        if first_round:
            delta: dict[str, Any] = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta, finish = {"content": "done"}, "stop"
        for chunk in (
            {"choices": [{"index": 0, "delta": delta}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        ):
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]  # noqa: SLF001
    daemon = await make_daemon(
        tmp_path / "home",
        settings={
            "providers": {
                "default": "local",
                "local": {"base_url": f"http://127.0.0.1:{port}/v1", "model": "m"},
            },
            "memory": {"enabled": False},
        },
    )
    try:
        core = daemon.core
        assert core is not None
        workdir = tmp_path / "project"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto", provider="local", model="m")
        from snowpea_core.agent import loop

        await loop.run_turn(core, session, "list the folder")
        assert len(seen) >= 2
        assert {header for header, _ in seen} == {session.id}
    finally:
        await daemon.stop()
        await runner.cleanup()
