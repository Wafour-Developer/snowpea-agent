"""Tool descriptions for the six load-bearing tools.

A tool description is prompt text the model reads on every turn, and one-liners
spend that budget saying nothing.  These say what the tool is *not* for as well,
which is where the tool-selection wins are: the model reaching for ``shell`` to
cat a file is the single most common waste of a turn.

They live here rather than inline in ``tools/*.py`` so that prompt review sees
every change to them in one place.  ``tools/*.py`` imports them by name.
"""

from __future__ import annotations

READ_FILE = (
    "Read a UTF-8 text file relative to the session working directory. Use this "
    "rather than cat, head or tail in shell. Long files are truncated; the result "
    "says so when it happens."
)

WRITE_FILE = (
    "Create a file, or replace an existing file's contents entirely. Use edit_file "
    "for a targeted change; this tool destroys everything the file currently holds. "
    "Prefer it over an echo or heredoc in shell."
)

EDIT_FILE = (
    "Replace an exact string in a file. Read the file first: the old string must "
    "match byte for byte, including indentation, and must appear exactly once unless "
    "replaceAll is true. Include a line or two of surrounding context to make the "
    "match unique. If it fails, re-read the file rather than retrying the same text."
)

SHELL = (
    "Run a shell command in the session working directory. Do not use it to read "
    "files (use read_file), to search (use grep or glob), or to edit (use edit_file). "
    "Reserve it for builds, installs, git, tests, package managers and scripts. The "
    "working directory and exported environment persist between calls, so activate a "
    "virtualenv once rather than before every command. Set `timeout` generously for "
    "long builds; a foreground command still returns the moment it finishes. Use "
    "`background: true` for servers and daemons, then check readiness with a separate "
    "command rather than sleeping."
)

GREP = (
    "Search file contents by regular expression. Use this rather than grep, rg or "
    "find in shell. Prefer it over reading whole files when you are looking for one "
    "symbol."
)

GLOB = (
    "Find files by path pattern, newest first. Use this rather than find or ls in "
    "shell when you are looking for files by name or extension. Pair it with grep: "
    "glob narrows to the files, grep finds the line."
)

DELEGATE_TASK = (
    "Hand a self-contained task to a subagent and return its report. Call it several "
    "times in one turn to run them in parallel. The child sees nothing of this "
    "conversation, so put everything it needs in the task, including any required "
    "output language. Its reply is a self-report, not a verified fact: for anything "
    "with an external effect, verify the result yourself before telling the user it "
    "worked."
)

__all__ = [
    "DELEGATE_TASK",
    "EDIT_FILE",
    "GLOB",
    "GREP",
    "READ_FILE",
    "SHELL",
    "WRITE_FILE",
]
