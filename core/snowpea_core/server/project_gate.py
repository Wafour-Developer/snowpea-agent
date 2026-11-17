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


#: ``details.reason`` of a project settings file that does not validate and so
#: is not written over (security review 8, H8-3).
INVALID_REASON = "project_settings_invalid"


def unsafe_write(exc: Exception, field: str = "workdir") -> RpcError:
    """``ProjectSettings.save``'s refusal as the RPC error a client switches on."""
    if getattr(exc, "reason", None) == "invalid":
        return RpcError(errors.INVALID_PARAMS, str(exc),
                        {"reason": INVALID_REASON, "field": field})
    return RpcError(errors.INVALID_PARAMS, NOT_A_PROJECT,
                    {"reason": REASON, "field": field, "what": "home"})


#: The refusal of a folder under the home whose first name is snowpea's own (0.2.90).
OWN_FOLDER = ("this folder is one of snowpea's own folders in its data folder ($SNOWPEA_HOME); "
              "a project cannot live there — choose a folder with another name, or one outside "
              "the data folder")


def admit_home_project(core: Any, conn: Any, workdir: Any, *, field: str = "workdir") -> bool:
    """0.2.90: a project folder inside snowpea's home (``SNOWPEA_HOME`` set to a
    folder the person also works in).

    A folder whose first name under the home is snowpea's own is refused up
    front (``invalid_params``, ``details.what: "own"``).  Any other folder under
    the home becomes a registered project — an ordinary place for the tools —
    when a PERSON's connection asks (``person_gate.is_person``); an agent's
    tool, Jarvis, a browser host or the connector never registers one.
    Returns True when ``workdir`` is (now) such a project.  The home itself is
    left to :func:`require_project`.
    """
    if not workdir:
        return False
    from snowpea_core.security import control_plane
    from snowpea_core.server.person_gate import is_person

    home = getattr(getattr(core, "paths", None), "home", None)
    if home is None:
        return False
    problem = control_plane.home_project_problem(workdir, home)
    if problem == "own":
        raise RpcError(errors.INVALID_PARAMS, OWN_FOLDER,
                       {"reason": REASON, "field": field, "what": "own"})
    if problem is not None:
        return False
    rel = control_plane._real_parts(workdir, home)
    if control_plane.person_project(rel, home) is not None:
        return True
    if not is_person(conn):
        return False
    control_plane.add_person_project(home, workdir)
    return True


def forget_home_project(core: Any, workdir: Any) -> None:
    """The registration of ``workdir`` ends with its workspace row (0.2.90)."""
    from snowpea_core.security import control_plane

    home = getattr(getattr(core, "paths", None), "home", None)
    if home is not None and workdir:
        try:
            control_plane.remove_person_project(home, workdir)
        except OSError:
            pass


__all__ = ["INVALID_REASON", "OWN_FOLDER", "REASON", "admit_home_project",
           "forget_home_project", "project_problem", "require_project", "unsafe_write"]
