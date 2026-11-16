"""What an assignment on a shell line changes for the security check (review 7, H7-1).

The classifier (:func:`snowpea_core.jarvis.classify._is_code_variable`) says
which variables load code or redirect configuration for whatever runs next.
:func:`judge` takes that judgement as it is and lets an assignment through
only when it can say why the value is harmless:

* a value that runs nothing (``GIT_PAGER=cat``, ``EDITOR=true``);
* ``PATH`` made only of system folders, folders already on the daemon's own
  ``PATH`` and ``$PATH`` itself — nothing relative, nothing in the project, in a
  temporary folder or written by the agent;
* a module search path (``PYTHONPATH=src``) whose folders the agent did not
  write into;
* a compiler by its plain name (``CC=gcc``), ``MAKEFLAGS=-j8``, a system shell;
* a plain switch or number for a tool's own configuration family
  (``PIP_NO_CACHE_DIR=1``, ``NPM_CONFIG_LOGLEVEL=error``, ``CARGO_TERM_COLOR=always``).

The variables paths are resolved with — ``HOME``, ``PWD``, ``OLDPWD``,
``TMPDIR``, ``SNOWPEA_HOME``, ``CDPATH``, every ``XDG_*`` — are different: the
check expands ``~`` and ``$HOME`` with the daemon's values, so a line that
gives one of them another value cannot be judged at all.  Only an assignment
of the very value the check uses is let through.

Pure: text in, a reason out.  Nothing is run or expanded by a shell.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from snowpea_core.jarvis import classify as _c
from snowpea_core.security import written

#: Variables the check itself resolves paths with.
RESOLVE: frozenset[str] = frozenset(
    {"HOME", "PWD", "OLDPWD", "TMPDIR", "SNOWPEA_HOME", "CDPATH"}
)
#: Values of a code-carrying variable that run nothing.
HARMLESS_VALUES: frozenset[str] = frozenset({"", "cat", "less", "more", "true", ":", "false"})
#: Variables that list folders programs are found in.
_PROGRAM_PATHS: frozenset[str] = frozenset({"PATH"})
#: Variables that list folders modules or libraries are loaded from.
MODULE_PATHS: frozenset[str] = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "NODE_PATH", "PERL5LIB", "RUBYLIB", "GEM_PATH", "GEM_HOME",
     "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
)
#: Folders of the system's own programs.
SYSTEM_DIRS: frozenset[str] = frozenset(
    {"/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin",
     "/opt/homebrew/bin", "/opt/homebrew/sbin", "/opt/local/bin", "/snap/bin",
     "/usr/local/go/bin", "/usr/games", "/usr/local/games"}
)
_COMPILER_VARS: frozenset[str] = frozenset({"CC", "CXX", "LD", "RUSTC"})
_COMPILER = re.compile(
    r"(?:ccache\s+|sccache\s+)?(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+|tcc|icx|icpx|musl-gcc|"
    r"[a-z0-9_]+-(?:linux|w64|apple|unknown)-[a-z0-9_-]*(?:gcc|g\+\+|clang)|ld|ld\.lld|lld|"
    r"ld\.gold|gold|mold|rustc)(?:-[0-9.]+)?")
_MAKEFLAGS = re.compile(r"(?:\s*(?:-j\s*[0-9]*|-k|-s|-B|--no-print-directory|--silent|"
                        r"--keep-going|--jobs(?:=[0-9]+)?))+\s*")
_SHELLS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash",
     "/usr/bin/sh", "/usr/bin/bash", "/usr/bin/zsh", "/usr/bin/dash"}
)
#: A switch, a level or a number: it names neither code nor a path.
_SCALAR = re.compile(r"(?i)(?:0|1|true|false|yes|no|on|off|always|never|auto|none|error|warn|"
                     r"warning|info|notice|silent|verbose|debug|quiet|[0-9]{1,6})")
#: Configuration families of package tools: a scalar value is harmless.
_FAMILIES: tuple[str, ...] = ("NPM_CONFIG_", "YARN_", "PNPM_", "CARGO_", "PIP_", "UV_",
                              "POETRY_", "BUNDLE_", "GEM_", "GIT_")
#: ``NODE_OPTIONS`` flags that only tune the runtime.
_NODE_TUNING = re.compile(
    r"--(?:max-old-space-size|max-semi-space-size|stack-size|max-http-header-size|"
    r"unhandled-rejections|dns-result-order|title)=[\w.:-]+|--(?:no-warnings|no-deprecation|"
    r"trace-warnings|trace-deprecation|enable-source-maps|openssl-legacy-provider|"
    r"experimental-vm-modules|no-experimental-fetch|experimental-specifier-resolution=node|"
    r"preserve-symlinks|throw-deprecation|pending-deprecation|expose-gc)")


def is_resolve(name: str) -> bool:
    """``name`` is a variable the check resolves paths with."""
    upper = name.upper()
    return upper in RESOLVE or upper.startswith("XDG_")


#: Code-carrying variables the classifier's table does not hold.
_MORE_CODE: frozenset[str] = frozenset({"PYTHONWARNINGS", "PYTHONBREAKPOINT", "PERL5DB"})


def is_code(name: str) -> bool:
    """``name`` loads code or redirects configuration for what runs next."""
    upper = name.upper()
    return _c._is_code_variable(name) or upper in _MORE_CODE or upper.startswith("BASH_FUNC_")


def is_watched(name: str) -> bool:
    """An assignment of ``name`` is judged at all."""
    return is_resolve(name) or is_code(name)


def _folder(part: str, cwd: Path | None, user_home: Path) -> Path | None:
    """One entry of a path list as an absolute folder; ``None`` when it is not one."""
    text = part
    for token in ("${HOME}", "$HOME"):
        if text.startswith(token + "/") or text == token:
            text = str(user_home) + text[len(token):]
    if text == "~" or text.startswith("~/"):
        text = str(user_home) + text[1:]
    for token in ("${PWD}", "$PWD", "$(pwd)", "`pwd`"):
        if (text.startswith(token + "/") or text == token) and cwd is not None:
            text = str(cwd) + text[len(token):]
    if "$" in text or "`" in text or text.startswith("~"):
        return None
    path = Path(text)
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    return Path(os.path.normpath(str(path)))


def _daemon_path() -> set[str]:
    return {os.path.normpath(entry) for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and os.path.isabs(entry)}


def _under(path: Path, root: Path | None) -> bool:
    return root is not None and (path == root or root in path.parents)


def _program_path(value: str, cwd: Path | None, user_home: Path) -> bool:
    """``PATH=value`` only names folders programs already come from."""
    known = _daemon_path()
    tmp = Path(os.path.normpath(tempfile.gettempdir()))
    entries = value.split(":")
    if not value or any(not entry for entry in entries):
        return False  # an empty entry is the current folder
    for entry in entries:
        if entry in ("$PATH", "${PATH}"):
            continue
        if not entry.startswith(("/", "~", "$HOME", "${HOME}")):
            return False  # relative: wherever the line happens to be
        folder = _folder(entry, cwd, user_home)
        if folder is None:
            return False
        text = str(folder)
        if text not in SYSTEM_DIRS and text not in known:
            return False
        if text in SYSTEM_DIRS:
            continue
        # Already on the daemon's PATH, yet not a place the agent fills.
        if _under(folder, cwd) or _under(folder, tmp) or _under(folder, Path("/tmp")) \
                or written.under(folder):
            return False
    return True


def _module_path(value: str, cwd: Path | None, user_home: Path, name: str) -> bool:
    """A module or library search path whose folders the agent did not write into."""
    for entry in value.split(":"):
        if not entry or entry in (f"${name}", f"${{{name}}}"):
            continue
        folder = _folder(entry, cwd, user_home)
        if folder is None or written.under(folder):
            return False
    return True


def judge(name: str, value: str, dynamic: bool, *, cwd: Path | None, user_home: Path,
          home: Path | None = None) -> str | None:
    """Why the assignment ``name=value`` makes the line unreadable; ``None`` when it does not.

    ``dynamic``: the value holds an expansion (the text still shows it).
    """
    upper = name.upper()
    if is_resolve(upper):
        current = {"HOME": str(user_home), "TMPDIR": tempfile.gettempdir(),
                   "PWD": str(cwd) if cwd is not None else None,
                   "SNOWPEA_HOME": str(home) if home is not None else None}.get(upper)
        if not dynamic and current is not None and value and \
                os.path.normpath(value) == os.path.normpath(current):
            return None  # the value the check already uses
        return f"env-code:{upper}"
    if not is_code(name):
        return None
    if upper in _PROGRAM_PATHS:
        return None if _program_path(value, cwd, user_home) else f"env-code:{upper}"
    if upper in MODULE_PATHS:
        return None if _module_path(value, cwd, user_home, upper) else f"env-path:{upper}"
    if dynamic:
        return f"env-code:{upper}"
    if upper == "IFS":
        # Splitting only changes words that hold an expansion, and those are
        # already words the check does not know.
        return None
    if upper == "NODE_OPTIONS":
        return None if all(_NODE_TUNING.fullmatch(flag) for flag in value.split()) \
            else f"env-code:{upper}"
    if upper in _COMPILER_VARS:
        return None if _COMPILER.fullmatch(value.strip()) else f"env-code:{upper}"
    if upper == "MAKEFLAGS":
        return None if not value.strip() or _MAKEFLAGS.fullmatch(value) else f"env-code:{upper}"
    if upper == "SHELL":
        return None if value in _SHELLS else f"env-code:{upper}"
    if upper.startswith(_FAMILIES) and upper not in _c._CODE_VARIABLES and \
            _SCALAR.fullmatch(value):
        return None
    if value in HARMLESS_VALUES:
        return None
    return f"env-code:{upper}"


def references(text: str) -> set[str]:
    """Names of the resolve variables ``text`` assigns through ``${NAME:=value}``."""
    return {match.group(1).upper()
            for match in re.finditer(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*:?=", text)
            if is_watched(match.group(1))}


__all__ = ["HARMLESS_VALUES", "MODULE_PATHS", "RESOLVE", "SYSTEM_DIRS", "is_resolve",
           "is_watched", "judge", "references"]
