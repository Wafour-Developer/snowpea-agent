"""One place where the tool layer builds HTTP clients.

Everything that talks to the network — search providers, ``web_extract``, the
SSE MCP transport — goes through :func:`new_client`, so a test can replace the
transport once instead of patching each call site.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

#: Sent by every outbound request so operators can identify the traffic.
USER_AGENT = "snowpea-agent/0.1 (+https://github.com/snowpea/snowpea-agent)"

DEFAULT_TIMEOUT = 20.0


def new_client(*, timeout: float = DEFAULT_TIMEOUT, guard_ssrf: bool = False) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient``; ``guard_ssrf`` adds the vendored SSRF guard.

    The guard re-validates the destination at TCP connect, which closes the DNS
    rebinding window that a plain pre-flight check leaves open.
    """
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": True,
        "headers": {"User-Agent": USER_AGENT},
    }
    if guard_ssrf:
        try:
            from snowpea_core.vendor.hermes.tools import url_safety

            client = url_safety.create_ssrf_safe_async_client(**kwargs)
            if isinstance(client, httpx.AsyncClient):
                return client
        except Exception:  # noqa: BLE001 - fall back to an unguarded client
            pass
    return httpx.AsyncClient(**kwargs)


def is_safe_url(url: str) -> bool:
    """False for private, loopback, link-local and cloud-metadata targets."""
    from snowpea_core.vendor.hermes.tools import url_safety

    return bool(url_safety.is_safe_url(url))


_SCRIPT_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|div|section|article|li|tr|h[1-6]|br)\s*>|<br\s*/?>", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """A readability-lite HTML → text pass: no dependency, no JS, no DOM."""
    import html as html_mod

    text = _SCRIPT_RE.sub(" ", html)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, saying how much was dropped."""
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n… [truncated, {dropped} more characters]"


__all__ = [
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "html_to_text",
    "is_safe_url",
    "new_client",
    "truncate",
]
