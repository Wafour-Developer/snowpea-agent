"""JSON-RPC 2.0 dispatch over a bidirectional connection.

A :class:`RpcConnection` is one client (one WebSocket).  Requests flow both
ways: the client calls methods from :data:`snowpea_core.server.protocol.METHODS`
and the server calls ``approval.request`` back.  Batching is not supported.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import METHODS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.rpc")

Handler = Callable[["RpcConnection", Any, "Core"], Awaitable[Any]]
Sender = Callable[[dict[str, Any]], Awaitable[None]]

#: The only method a client may call before ``system.hello`` succeeds.
PUBLIC_METHODS: frozenset[str] = frozenset({"system.hello"})

DEFAULT_CALL_TIMEOUT = 60.0


class RpcConnection:
    """One connected client; carries auth state and pending server->client calls."""

    def __init__(self, sender: Sender, *, peer: str = "") -> None:
        self._sender = sender
        self.peer = peer
        self.surface_id: str = uuid.uuid4().hex
        self.authenticated: bool = False
        self.client_version: str | None = None
        self.closed: bool = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()

    # -- outgoing -----------------------------------------------------
    async def send_json(self, obj: dict[str, Any]) -> None:
        """Serialise and write one JSON-RPC frame."""
        if self.closed:
            return
        await self._sender(obj)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget server->client notification."""
        await self.send_json({"jsonrpc": "2.0", "method": method, "params": params})

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = DEFAULT_CALL_TIMEOUT
    ) -> dict[str, Any]:
        """Server->client request; resolves when the client answers."""
        request_id = f"s{next(self._ids)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self.send_json(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    # -- incoming responses -------------------------------------------
    def handle_response(self, message: dict[str, Any]) -> None:
        key = str(message.get("id"))
        future = self._pending.get(key)
        if future is None or future.done():
            log.debug("dropping response for unknown id %s", key)
            return
        if "error" in message and message["error"] is not None:
            err = message["error"] or {}
            data = err.get("data") or {}
            code = data.get("code") if isinstance(data, dict) else None
            future.set_exception(
                RpcError(code or errors.INTERNAL, str(err.get("message", "remote error")))
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {"result": result})

    # -- lifecycle -----------------------------------------------------
    def spawn(self, coro: Awaitable[None]) -> None:
        """Run a handler concurrently so the read loop keeps draining."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancel in-flight handlers and fail outstanding server->client calls."""
        self.closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RpcError(errors.INTERNAL, "connection closed"))
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()


class RpcDispatcher:
    """Routes incoming frames to registered handlers."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self._handlers: dict[str, Handler] = {}
        self._params: dict[str, type[BaseModel]] = {}

    def register(
        self, name: str, handler: Handler, params_model: type[BaseModel] | None = None
    ) -> None:
        """Register ``name``; params are validated with the protocol model."""
        model = params_model
        if model is None:
            method = METHODS.get(name)
            if method is None:
                raise KeyError(f"{name} is not in protocol.METHODS; pass params_model explicitly")
            model = method.params
        self._handlers[name] = handler
        self._params[name] = model

    def has(self, name: str) -> bool:
        return name in self._handlers

    def method_names(self) -> list[str]:
        return sorted(self._handlers)

    async def handle_message(self, conn: RpcConnection, raw: str | bytes) -> None:
        """Parse one frame and route it (request, notification or response)."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            await conn.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": errors.PARSE_ERROR, "message": "invalid JSON"},
                }
            )
            return
        if isinstance(message, list):
            await conn.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": errors.INVALID_REQUEST,
                        "message": "batch requests are not supported",
                    },
                }
            )
            return
        if not isinstance(message, dict):
            await conn.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": errors.INVALID_REQUEST, "message": "expected an object"},
                }
            )
            return
        if "method" not in message:
            conn.handle_response(message)
            return
        conn.spawn(self._dispatch(conn, message))

    async def _dispatch(self, conn: RpcConnection, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        try:
            result = await self._invoke(conn, message)
        except RpcError as exc:
            if request_id is not None:
                await conn.send_json(
                    {"jsonrpc": "2.0", "id": request_id, "error": exc.to_jsonrpc()}
                )
            return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - handlers must not kill the loop
            log.exception("handler for %s failed", method)
            if request_id is not None:
                err = RpcError(errors.INTERNAL, f"{type(exc).__name__}: {exc}")
                await conn.send_json(
                    {"jsonrpc": "2.0", "id": request_id, "error": err.to_jsonrpc()}
                )
            return
        if request_id is not None:
            await conn.send_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _invoke(self, conn: RpcConnection, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        if not isinstance(method, str):
            raise RpcError(errors.INVALID_PARAMS, "method must be a string")
        if not conn.authenticated and method not in PUBLIC_METHODS:
            raise RpcError(errors.UNAUTHORIZED, f"{method} requires system.hello first")
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError(errors.NOT_FOUND, f"unknown method: {method}")
        raw_params = message.get("params")
        if raw_params is None:
            raw_params = {}
        if not isinstance(raw_params, dict):
            raise RpcError(errors.INVALID_PARAMS, "params must be an object")
        model = self._params[method]
        try:
            params = model.model_validate(raw_params)
        except ValidationError as exc:
            raise RpcError(
                errors.INVALID_PARAMS, "invalid params", exc.errors(include_url=False)
            ) from exc
        result = await handler(conn, params, self.core)
        return _as_dict(result)


def _as_dict(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return {"result": result}


__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "PUBLIC_METHODS",
    "Handler",
    "RpcConnection",
    "RpcDispatcher",
]
