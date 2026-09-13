"""LSP integration: diagnostics, definitions, references, symbols, rename.

Ported from opencode's ``packages/opencode/src/lsp/*`` (MIT, commit 95daf90);
see ``docs/design/m13-lsp-contract.md`` and
``docs/design/deviations/CORE-lsp.md``.  The daemon owns one
:class:`~snowpea_core.lsp.manager.LspManager`, which lazily starts one language
server per ``(server, project root)`` and shuts it down again when it goes
idle.

Nothing here may fail an edit.  A missing server, a crashed server and a slow
server all mean "no diagnostics this time"; ``write_file`` and ``edit_file``
succeed either way (contract §3).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.lsp import diagnostic, language, servers, tools
from snowpea_core.lsp.client import LspClient, LspError
from snowpea_core.lsp.diagnostic import report
from snowpea_core.lsp.manager import START_BUDGET_SEC, LspManager, manager_for
from snowpea_core.lsp.tools import TOOLS, refresh_state, register_lsp_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.tools.registry import ToolContext

log = logging.getLogger("snowpea.lsp")


def wire_lsp(core: object) -> None:
    """Attach the manager to ``Core`` and set the tool states (``wire_core``)."""
    manager_for(core)  # type: ignore[arg-type]
    refresh_state(core)


async def diagnostics_block(ctx: ToolContext, path: str) -> str:
    """The ``Diagnostics`` block ``write_file``/``edit_file`` append, or ``""``.

    Bounded by :data:`START_BUDGET_SEC` for starting a server and by the
    client's own diagnostics timeout for the answer, and it swallows
    everything: an edit is never failed, delayed past its budget, or made
    conditional on a language server being installed (contract §3, AC-43).
    """
    try:
        manager = manager_for(ctx.core)
        if not manager.enabled:
            return ""
        workdir = Path(ctx.session.workdir)
        file = Path(path)
        if not file.is_absolute():
            file = workdir / file
        if not file.is_file():
            return ""
        clients = await manager.touch_file(
            file,
            workdir,
            wait_for_diagnostics=True,
            budget=START_BUDGET_SEC,
            session_id=ctx.session.id,
        )
        if not clients:
            return ""
        found: list[dict[str, object]] = []
        resolved = file.resolve()
        for client in clients:
            found.extend(client.diagnostics_for(resolved))
        try:
            display = str(resolved.relative_to(workdir.resolve()))
        except ValueError:
            display = str(resolved)
        return report(display, found)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - diagnostics never fail an edit
        log.debug("could not collect diagnostics for %s", path, exc_info=True)
        return ""


__all__ = [
    "START_BUDGET_SEC",
    "TOOLS",
    "LspClient",
    "LspError",
    "LspManager",
    "diagnostic",
    "diagnostics_block",
    "language",
    "manager_for",
    "refresh_state",
    "register_lsp_tools",
    "report",
    "servers",
    "tools",
    "wire_lsp",
]
