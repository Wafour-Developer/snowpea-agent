"""A child's environment without the frozen core's PyInstaller state.

A frozen (PyInstaller onedir) core runs with variables its bootloader set for
itself: ``_MEIPASS2``, ``_PYI_*`` (``_PYI_APPLICATION_HOME_DIR``,
``_PYI_ARCHIVE_FILE``, ``_PYI_PARENT_PROCESS_LEVEL`` …) and library paths
into its own bundle (``LD_LIBRARY_PATH``, ``DYLD_*``).  A child that is the
same binary again — a plugin hook run through ``snowpea-core --run-hook`` —
takes those as "I am a worker of that parent" and can crash (SIGSEGV on mac
arm64, 0.2.91); any other program can load the bundle's libraries in place of
its own.  :func:`frozen_child_env` drops all of that; outside a frozen core it
changes nothing.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

#: Variables whose path entries pointing into the bundle are removed.
_PATH_VARS: tuple[str, ...] = ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")

#: PyInstaller (6.9+): a spawned copy of the executable starts as a new,
#: independent program instead of a worker of the process that spawned it.
RESET_VAR = "PYINSTALLER_RESET_ENVIRONMENT"


def _inside(entry: str, bundle: str) -> bool:
    try:
        real = os.path.realpath(entry)
    except (OSError, ValueError):
        return False
    return real == bundle or real.startswith(bundle.rstrip(os.sep) + os.sep)


def frozen_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) fit for a child of a frozen core.

    When frozen: ``_MEIPASS2`` and every ``_PYI_*`` go; ``LD_LIBRARY_PATH`` is
    restored from ``LD_LIBRARY_PATH_ORIG`` when PyInstaller saved it, and
    otherwise, like ``DYLD_*``, ``PYTHONHOME`` and ``PYTHONPATH``, keeps only
    the entries outside ``sys._MEIPASS`` (dropped when none is left), so a
    value the user set before the core started survives; and
    ``PYINSTALLER_RESET_ENVIRONMENT=1`` is set.  Not frozen: an unchanged copy.
    """
    env = dict(os.environ if base is None else base)
    if not getattr(sys, "frozen", False):
        return env
    for name in list(env):
        if name == "_MEIPASS2" or name.startswith("_PYI_"):
            del env[name]
    bundle_dir = getattr(sys, "_MEIPASS", None)
    bundle = os.path.realpath(bundle_dir) if bundle_dir else None
    names = [*_PATH_VARS, *(n for n in env if n.startswith("DYLD_") and not n.endswith("_ORIG"))]
    for name in names:
        original = env.pop(f"{name}_ORIG", None)
        if original is not None:
            env[name] = original
            continue
        value = env.get(name)
        if value is None or bundle is None:
            continue
        kept = [part for part in value.split(os.pathsep) if part and not _inside(part, bundle)]
        if kept:
            env[name] = os.pathsep.join(kept)
        else:
            del env[name]
    env[RESET_VAR] = "1"
    return env


__all__ = ["RESET_VAR", "frozen_child_env"]
