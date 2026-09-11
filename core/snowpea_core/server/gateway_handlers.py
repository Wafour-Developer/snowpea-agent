"""RPC handlers for ``gateway.bind|list|unbind`` (M5 contract §3).

Kept in their own module — and registered from ``build_dispatcher`` with one
line — so the gateway can land without touching ``session_handlers.py``, which
other M5 stories are editing at the same time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.config.credentials import CredentialError
from snowpea_core.gateway.base import GatewayError
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    GatewayBinding,
    GatewayBindParams,
    GatewayBindResult,
    GatewayListResult,
    GatewayUnbindParams,
    Ok,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.gateway")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("gateway.bind", "gateway.list", "gateway.unbind")


def _router(core: Core) -> object:
    router = getattr(core, "gateway", None)
    if router is None:
        raise RpcError(errors.NOT_IMPLEMENTED, "the gateway router is not running")
    return router


async def gateway_bind_handler(
    _conn: RpcConnection, params: GatewayBindParams, core: Core
) -> GatewayBindResult:
    """``gateway.bind`` — attach a platform account to an agent or session."""
    router = _router(core)
    try:
        binding = await router.bind(  # type: ignore[attr-defined]
            params.platform,
            params.credentialsRef,
            params.target,
            channel_id=params.channelId,
            user_id=params.userId,
        )
    except CredentialError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    except GatewayError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return GatewayBindResult(bindingId=binding.id)


async def gateway_list_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> GatewayListResult:
    """``gateway.list`` — live bindings; credentials stay behind their ref."""
    router = _router(core)
    return GatewayListResult(
        bindings=[
            GatewayBinding(
                bindingId=binding.id,
                platform=binding.platform,
                target=binding.describe_target(),
                credentialsRef=binding.credentials_ref,
                channelId=binding.channel_id,
                userId=binding.user_id,
                state=binding.state,  # type: ignore[arg-type]
            )
            for binding in router.list()  # type: ignore[attr-defined]
        ]
    )


async def gateway_unbind_handler(
    _conn: RpcConnection, params: GatewayUnbindParams, core: Core
) -> Ok:
    """``gateway.unbind`` — stop and forget one binding."""
    router = _router(core)
    removed = await router.unbind(params.bindingId)  # type: ignore[attr-defined]
    if not removed:
        raise RpcError(errors.NOT_FOUND, f"no gateway binding {params.bindingId}")
    return Ok(ok=True)


def register_gateway_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("gateway.bind", gateway_bind_handler)
    dispatcher.register("gateway.list", gateway_list_handler)
    dispatcher.register("gateway.unbind", gateway_unbind_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "gateway_bind_handler",
    "gateway_list_handler",
    "gateway_unbind_handler",
    "register_gateway_handlers",
]
