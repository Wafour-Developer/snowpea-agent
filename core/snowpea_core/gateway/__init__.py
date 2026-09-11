"""Chat-platform gateway (M5 contract §3).

``GatewayRouter`` binds a credentialed platform account to a snowpea target and
carries messages both ways; the adapters below speak one platform each.
"""

from snowpea_core.gateway.base import (
    Button,
    GatewayError,
    InboundMessage,
    PlatformAdapter,
    approval_buttons,
    approval_callback,
    parse_approval_callback,
)
from snowpea_core.gateway.router import Binding, GatewayRouter, build_adapter

__all__ = [
    "Binding",
    "Button",
    "GatewayError",
    "GatewayRouter",
    "InboundMessage",
    "PlatformAdapter",
    "approval_buttons",
    "approval_callback",
    "build_adapter",
    "parse_approval_callback",
]
