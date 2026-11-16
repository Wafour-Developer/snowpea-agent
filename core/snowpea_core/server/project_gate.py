"""snowpea's own home is never a project (security review 7, H-2).

``settings.set {scope: "project", workdir: <the parent of $SNOWPEA_HOME>}``
used to write a project document over the global ``settings.json``: with the
default home, the user's home directory's ``.snowpea`` IS snowpea's home, so
"project" settings, skills, agents, commands and trust pins of that folder are
the global ones, reached without the global scope's rules.

ONE helper for every RPC that takes a project folder, :func:`require_project`.
What a method touches decides how much is refused:

``dot=True``
    The method reads or writes ``<workdir>/.snowpea/…`` as the PROJECT scope
    (settings, allowlist rules, skills/agents/commands, team guides, trust
    pins, security-list additions): refused for the home, a folder inside it,
    and a folder whose ``.snowpea`` is the home.
``dot=False``
    The method only works IN the folder (a session, a job, an external CLI,
    the project's ``.mcp.json``, which is ``<workdir>/.mcp.json``): refused for
    the home and folders inside it.  The user's home directory stays an
    ordinary place to work in.

The refusal is ``invalid_params`` with ``details {reason: "snowpea_home",
field: "workdir"}``, for every caller: it is a bug whoever asks.  Underneath,
``ProjectSettings.load``/``save`` never treat the global file as a project's
(``config/project.py``), so a caller that forgot this check still cannot.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.config.project import NOT_A_PROJECT, home_problem
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

REASON = "snowpea_home"


def project_problem(core: Any, workdir: Any, *, dot: bool = True) -> str | None:
    """``"home"``/``"parent"`` when ``workdir`` may not be used (see the module doc)."""
    if not workdir:
        return None
    home = getattr(getattr(core, "paths", None), "home", None)
    problem = home_problem(workdir, home) if home is not None else home_problem(workdir)
    if problem == "parent" and not dot:
        return None
    return problem


def require_project(core: Any, workdir: Any, *, dot: bool = True, field: str = "workdir") -> None:
    """Raise ``invalid_params`` when ``workdir`` is snowpea's own home (module doc)."""
    problem = project_problem(core, workdir, dot=dot)
    if problem is not None:
        raise RpcError(errors.INVALID_PARAMS, NOT_A_PROJECT,
                       {"reason": REASON, "field": field, "what": problem})


__all__ = ["REASON", "project_problem", "require_project"]
