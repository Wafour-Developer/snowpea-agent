"""Which files plan mode may write (M2 contract §9).

Plan mode refuses writes, and it has to keep refusing them: the whole point of
the mode is that the user reads the plan before any code moves.  But the plan
itself is a file.  A planner that cannot write ``docs/design/x.md`` or
``.snowpea/plans/x.md`` has to paste its work into the chat and hope somebody
copies it out, which is how plans get lost.

So the ``write`` deny has exactly one carve-out: documents.  A markdown or text
file, anything under the project's ``docs/`` or ``.snowpea/plans/``, and
``$SNOWPEA_HOME/plans/``.  Everything else — source, configuration, data — stays
denied with the usual message.

Two properties make this safe to state as a rule rather than a hook:

* it is keyed on the **resolved** path, so ``../../src/a.py`` and a symlink out
  of the workdir are judged where they land, not where they are spelled;
* it never touches the ``config`` tag.  A write to a settings file is re-tagged
  ``config`` before the policy sees it (``tools/config_guard.py``), and
  ``config`` is ``deny`` in plan mode with no exception at all.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from snowpea_core.config.paths import resolve_home
from snowpea_core.config.settings import DEFAULT_PLAN_WRITABLE_GLOBS

#: Directory under ``$SNOWPEA_HOME`` that holds plans rather than settings.
#: It is writable in plan mode and, unlike the rest of that home, is not
#: configuration (``config_guard.HOME_WORKING_DIRS``).
HOME_PLANS_DIR = "plans"

#: What the refusal adds so the model knows a different path would work.
REFUSAL_NOTE = "plan mode: only markdown/plan files may be written"


@lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern[str]:
    """A glob compiled the way a user expects, which ``fnmatch`` does not do.

    ``**/`` spans whole segments including none, ``*`` and ``?`` stop at a
    separator.  ``fnmatch`` lets ``*`` cross ``/`` and has no ``**`` at all, so
    ``**/*.md`` would match nothing sensible through it.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def _resolve(path: str, workdir: Any) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() and workdir:
            candidate = Path(workdir) / candidate
        return Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _under(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def writable_globs(settings: Any = None) -> tuple[str, ...]:
    """``modes.plan.writableGlobs``, or the built-in list."""
    plan = getattr(getattr(settings, "modes", None), "plan", None)
    configured = getattr(plan, "writableGlobs", None)
    if isinstance(configured, (list, tuple)):
        globs = tuple(str(item).strip() for item in configured if str(item).strip())
        if globs:
            return globs
    return tuple(DEFAULT_PLAN_WRITABLE_GLOBS)


def is_plan_writable(
    path: str, *, workdir: Any = None, home: Any = None, settings: Any = None
) -> bool:
    """True when plan mode may write ``path`` without asking.

    Inside the workdir the configured globs decide.  Outside it only
    ``$SNOWPEA_HOME/plans/`` qualifies: a plan the user keeps across projects is
    still a plan, and nothing else outside the project is any of plan mode's
    business.
    """
    if not str(path).strip():
        return False
    resolved = _resolve(str(path), workdir)
    if resolved is None:
        return False

    try:
        snowpea_home = Path(home).expanduser().resolve() if home else resolve_home()
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - unresolvable $HOME
        snowpea_home = None
    if snowpea_home is not None and _under(resolved, snowpea_home / HOME_PLANS_DIR):
        return True

    if not workdir:
        return False
    try:
        root = Path(workdir).expanduser().resolve()
        relative = resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        # Outside the workdir entirely; the home-plans case above was its only
        # way in.  A plan-mode write never escapes the project.
        return False
    return any(_regex(pattern).match(relative) for pattern in writable_globs(settings))


__all__ = [
    "HOME_PLANS_DIR",
    "REFUSAL_NOTE",
    "is_plan_writable",
    "writable_globs",
]
