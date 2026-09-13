"""Pick up ``settings.json`` edits without restarting the daemon.

``Daemon.start`` reads ``$SNOWPEA_HOME/settings.json`` once and hands the
resulting :class:`~snowpea_core.config.settings.Settings` to collaborators that
keep a reference to it (the provider registry, the session manager, the
approval queue, the allowlist) or to one of its sections (the scheduler, the
memory services).  Anything that edited the file afterwards -- ``snowpea
setup`` in another process, a hand edit -- was therefore invisible until the
daemon was restarted, which is how a freshly configured model still went out
as the old one (CORE-settings-reload).

This module is the whole of the fix:

* :func:`stamp` is the cheap "did the file change?" check (``os.stat``) that
  ``session.create`` and ``session.prompt`` run before they touch settings;
* :func:`changed_keys` names the top-level sections that actually differ, so a
  no-op write neither rebinds nor notifies;
* :func:`rebind` installs a fresh document on every collaborator that captured
  the old one.

Collaborators that already read ``core.settings`` at call time (the web-search
and browser tool providers, the MCP permission table, the media tools, the
agent loop, ``/ralph``, teams) need nothing here; reading lazily is strictly
better than being rebound, and new code should prefer it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from snowpea_core.config.settings import Settings

log = logging.getLogger("snowpea.config.hot_reload")

#: :func:`stamp` for a file that does not exist.
MISSING: tuple[int, int] = (-1, -1)


def stamp(path: Path | str) -> tuple[int, int]:
    """``(mtime_ns, size)`` for ``path``; :data:`MISSING` when it is absent.

    One ``os.stat`` per prompt is cheap enough to run unconditionally, and the
    pair catches both a rewrite that keeps the size and one that keeps the
    timestamp.
    """
    try:
        info = os.stat(path)
    except OSError:
        return MISSING
    return (info.st_mtime_ns, info.st_size)


def changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Top-level keys whose value differs between two settings documents."""
    return sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )


def rebind(core: Any, settings: Settings) -> None:
    """Install ``settings`` on ``core`` and on everything that captured the old one.

    Never raises: a collaborator that is not wired yet (a test double, a core
    built before ``wire_core``) is skipped rather than failing the reload.
    """
    core.settings = settings
    paths = getattr(core, "paths", None)

    sessions = getattr(core, "sessions", None)
    if sessions is not None:
        sessions.settings = settings

    approvals = getattr(core, "approvals", None)
    if approvals is not None:
        approvals.settings = settings

    providers = getattr(core, "providers", None)
    if providers is not None:
        providers.bind(settings, paths)
        mark = getattr(core, "mark_settings_saved", None)
        if mark is not None:
            providers.on_saved = mark

    allowlist = getattr(core, "allowlist", None)
    if allowlist is not None:
        if paths is not None:
            allowlist.bind(paths, settings)
        else:  # pragma: no cover - a core without paths is a test double
            allowlist.settings = settings

    scheduler = getattr(core, "scheduler", None)
    if scheduler is not None:
        scheduler.settings = settings.scheduler

    memory = getattr(core, "memory", None)
    if memory is not None:
        memory.settings = settings.memory
        retrieval = getattr(memory, "retrieval", None)
        if retrieval is not None:
            retrieval.top_k = settings.memory.top_k

    # A changed base_url or api_key must not be answered from the listing the
    # previous endpoint gave us.
    from snowpea_core.providers import models as model_discovery

    model_discovery.cache_clear()

    # Turning a voice backend on or off in settings.json flips the two audio
    # tools between active and inactive; everything else about audio is read at
    # call time and needs nothing here (CORE-multimodal).
    tools = getattr(core, "tools", None)
    if tools is not None:
        from snowpea_core.tools import audio_tools

        try:
            audio_tools.refresh_state(core)
        except Exception:  # noqa: BLE001 - a reload must never fail on this
            log.debug("could not refresh the audio tool state", exc_info=True)

        # Turning ``lsp.enabled`` off must take the seven ``lsp_*`` tools out
        # of the model's list on the next turn, not at the next restart (M13 §3).
        from snowpea_core.lsp import tools as lsp_tools

        try:
            lsp_tools.refresh_state(core)
        except Exception:  # noqa: BLE001 - a reload must never fail on this
            log.debug("could not refresh the lsp tool state", exc_info=True)

    # A changed ``skills.registry.url`` must retarget skill.search immediately,
    # not after the next daemon restart.
    if getattr(core, "skills", None) is not None:
        from snowpea_core.skills import registry_client

        try:
            registry_client.configure_client(settings)
        except Exception:  # noqa: BLE001 - a reload must never fail on this
            log.debug("could not reconfigure the skill registry client", exc_info=True)


__all__ = ["MISSING", "changed_keys", "rebind", "stamp"]
