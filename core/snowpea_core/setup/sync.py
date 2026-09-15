"""Await a coroutine from the wizard's synchronous code, loop or no loop.

The wizard is a terminal flow, so most of it runs with no event loop and
``asyncio.run`` is the natural bridge.  ``snowpea setup`` can also be reached
from inside a running loop (the TUI's ``/setup``, tests), where ``asyncio.run``
raises instead; a one-thread pool gives the coroutine its own loop there.
"""

from __future__ import annotations

import asyncio
from typing import Any


def run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


__all__ = ["run_sync"]
