"""``snowpea web tunnel status|start|stop|install|token`` (M37, design §2.10).

``status``, ``start``, ``stop`` and ``install`` ask the daemon (``web.tunnel.*``);
``token`` writes the provider's token into ``credentials.json`` straight from stdin, so it
works with the daemon stopped and the token never goes through a command line, a log
or an environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import DaemonError, RpcCallError
from snowpea_core.cli.render import EXIT_NO_DAEMON, EXIT_OK, EXIT_USAGE
from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths

TOKEN_PROVIDERS = ("cloudflare", "ngrok")
PROVIDERS = ("cloudflare-quick", "cloudflare", "ngrok")


def _fail(message: str, code: int) -> int:
    print(f"snowpea: {message}", file=sys.stderr)
    return code


def print_status(status: dict[str, Any]) -> None:
    state = str(status.get("state") or "off")
    provider = str(status.get("provider") or "off")
    print(f"tunnel:    {state} ({provider})")
    if status.get("url"):
        print(f"address:   {status['url']}")
    if status.get("error"):
        print(f"problem:   {status['error']}")
    if provider in PROVIDERS:
        print(f"program:   {status.get('binary') or 'not found'}")
        if not status.get("binary"):
            if status.get("installable"):
                print("           run `snowpea web tunnel install` to download it")
            elif status.get("installCommand"):
                print(f"           install it with: {status['installCommand']}")
        if status.get("tokenSet") is not None:
            print(f"token:     {'set' if status['tokenSet'] else 'not set'}")


async def tunnel_command(
    home: Path | str | None, action: str, *, provider: str | None = None, save: bool = False,
    as_json: bool = False,
) -> int:
    """Route ``snowpea web tunnel <action>``."""
    if action == "token":
        return token_command(home, provider)
    from snowpea_core.cli.commands import _call  # the one RPC round trip helper

    methods = {"status": "web.tunnel.status", "start": "web.tunnel.start",
               "stop": "web.tunnel.stop", "install": "web.tunnel.install"}
    method = methods.get(action)
    if method is None:
        return _fail("usage: snowpea web tunnel status|start|stop|install|token", EXIT_USAGE)
    params: dict[str, Any] = {}
    if provider and action in {"start", "install"}:
        params["provider"] = provider
    if save and action in {"start", "stop"}:
        params["save"] = True
    try:
        result = await _call(home, method, params, timeout=180.0 if action != "status" else None)
    except RpcCallError as exc:
        return _fail(f"web tunnel {action}: {exc.message}", EXIT_USAGE)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    if as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    print_status(result)
    if result.get("installedPath"):
        print(f"installed: {result['installedPath']}")
    return EXIT_OK


def token_command(home: Path | str | None, provider: str | None) -> int:
    """``snowpea web tunnel token --provider ngrok|cloudflare``: one line of stdin."""
    if provider not in TOKEN_PROVIDERS:
        return _fail("usage: snowpea web tunnel token --provider ngrok|cloudflare "
                     "(the token is read from stdin)", EXIT_USAGE)
    from snowpea_core.server.tunnel import TOKEN_KEYS

    token = sys.stdin.readline().strip()
    if not token:
        return _fail("no token on stdin", EXIT_USAGE)
    if any(ch.isspace() for ch in token):
        return _fail("a token has no spaces", EXIT_USAGE)
    CredentialStore(Paths.create(home)).set(TOKEN_KEYS[provider], token)
    print(f"{provider} tunnel token stored")
    return EXIT_OK


__all__ = ["print_status", "token_command", "tunnel_command"]
