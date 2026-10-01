"""Which urls core's own browser may open for a session.

Every session: cloud-metadata endpoints (169.254.169.254,
metadata.google.internal, the whole link-local range, …) are refused — the
floor of the vendored SSRF guard.

A session the Snowpea browser opened (``is_browser_session``) that has been
switched to core's own Chromium additionally may not reach private/LAN
(RFC 1918, CGNAT), loopback or link-local addresses, unless the user typed
that exact address (``localhost:5173``, ``192.168.0.10``) in the
conversation.  Its pages come from the open web, so a page or a prompt
injection must not steer the agent into the user's router or dev servers.
IDE and CLI sessions keep loopback and the LAN: building and checking a local
dev server is their everyday work.

Hostnames are resolved once for the check.  DNS rebinding (a name that
resolves differently when Chromium dials it) is out of scope here.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit

from snowpea_core.tools.browser_providers import is_browser_session
from snowpea_core.vendor.hermes.tools import url_safety

URL_BLOCKED = "url_blocked"

#: Parent links followed to find the conversation the user typed into.
_MAX_PARENT_HOPS = 8


def restricted(session: Any) -> bool:
    """True when private, loopback and link-local targets need the user's say-so."""
    return is_browser_session(session)


def _host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """``host`` as addresses: itself when literal, else what DNS answers (or nothing)."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []  # an unresolvable name fails in the browser, not here
    ips = []
    for *_rest, sockaddr in answers:
        try:
            ips.append(ipaddress.ip_address(str(sockaddr[0]).split("%")[0]))
        except ValueError:
            continue
    return ips


def _user_texts(session: Any, core: Any) -> list[str]:
    """What the user typed in this conversation, or in its top-level parent's.

    A subagent's own first message is its lead's brief, not the user's words,
    so a child session looks at the root session it was delegated from.
    """
    sessions = getattr(core, "sessions", None)
    current = session
    for _ in range(_MAX_PARENT_HOPS):
        parent_id = getattr(current, "parent_session_id", None)
        if not parent_id or sessions is None:
            break
        try:
            parent = sessions.get(parent_id)
        except Exception:  # noqa: BLE001 - an unknown parent ends the walk
            parent = None
        if parent is None:
            break
        current = parent
    history = getattr(current, "history", None)
    messages = getattr(history, "messages", None) or []
    texts: list[str] = []
    for message in messages:
        if getattr(message, "role", None) != "user":
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                str(block.get("text"))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
            )
    return texts


def user_named(url: str, session: Any, core: Any) -> bool:
    """True when the user typed this url's exact address (host, or host:port)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is not None:
        shown = f"[{host}]" if ":" in host else host
        addresses = [f"{shown}:{port}"]
    else:
        addresses = [host, f"[{host}]"] if ":" in host else [host]
    patterns = [
        re.compile(rf"(?<![\w.\-:\[]){re.escape(address)}(?![\w\-:]|\.\w)", re.IGNORECASE)
        for address in addresses
    ]
    return any(p.search(text) for text in _user_texts(session, core) for p in patterns)


def refusal(url: str, session: Any, core: Any) -> str | None:
    """Why core's own browser may not open ``url`` for this session, or ``None``."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return None
    if url_safety.is_always_blocked_url(url):
        return f"{URL_BLOCKED}: {host} is a cloud-metadata address; the browser never opens it"
    if not restricted(session):
        return None
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        private = True
    else:
        private = any(url_safety._is_blocked_ip(ip) for ip in _host_ips(host))  # noqa: SLF001
    if not private or user_named(url, session, core):
        return None
    return (
        f"{URL_BLOCKED}: {host} is a private, loopback or link-local address, which a "
        "browser session opens only when the user names that exact address (for example "
        f"`{parts.netloc}`) in the conversation. Ask the user if this is intended."
    )


__all__ = ["URL_BLOCKED", "refusal", "restricted", "user_named"]
