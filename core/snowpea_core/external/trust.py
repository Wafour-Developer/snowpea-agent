"""Projects the person trusted for external CLIs (security review M4).

An external CLI started in a folder loads that folder's own configuration —
Claude Code's project settings and hooks, Cursor's workspace (snowpea passes
``--trust``), Codex's and agy's project files — and so runs code the repository
ships.  Only the person decides that a project may do so: the first external
session there is opened by a person with ``trust: true`` (a one-time
confirmation in the client); after that Jarvis, a browser host or the
connector may open sessions there too.

Kept in snowpea's one trusted-projects record (security review 5, C1:
:mod:`snowpea_core.trust.projects`, ``$SNOWPEA_HOME/trust/projects.json``,
0600) as the ``externalCli`` scope, never in the project's own
``.snowpea/settings.json``, which the repository itself could ship pre-set.
The record this used to be (``$SNOWPEA_HOME/jarvis/external-trust.json``) is
still read: its projects keep their external-CLI trust.  Keyed by the project
root's real path.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from snowpea_core.trust.projects import store_for

log = logging.getLogger("snowpea.external.trust")

#: The legacy file name (read, folded into the one record).
FILE_NAME = "external-trust.json"
SCOPE = "externalCli"


class ProjectTrust:
    """``trusted(root)`` / ``trust(root, by)``: the ``externalCli`` scope of project trust."""

    def __init__(self, home: Path) -> None:
        self.store = store_for(home)

    def trusted(self, root: str | os.PathLike[str] | None) -> bool:
        if not root:
            return False
        return self.store.active(root, SCOPE)

    def trust(self, root: str | os.PathLike[str], *, by: str) -> None:
        """Mark ``root`` trusted for external CLIs (only ever for a local person's request)."""
        self.store.set(root, [SCOPE], by=by)


__all__ = ["ProjectTrust"]
