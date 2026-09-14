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
    "conversation, so put everything it needs in the task; the brief itself may be "
    "English for precision, and the output language is appended for you. Pass title "
    "as a one-line description of the delegation in the user's language — it is what "
    "the user sees while it runs. Its reply is a self-report, not a verified fact: "
    "for anything with an external effect, verify the result yourself before telling "
    "the user it worked. Relay what it found in your own words; never paste its "
    "report. The result starts with status / reason / roundsUsed: reason 'budget' "
    "or 'timeout' means the child stopped early, so the report is partial — read it, "
    "take what is done, and delegate only what is left, never the same task again."
)

LSP_DIAGNOSTICS = (
    "Ask the language server what is wrong with a file: type errors, undefined names, "
    "unused imports, each with a line and column. Use it after an edit whose "
    "Diagnostics block was empty because the server was still starting, or before you "
    "hand work back, rather than running the whole test suite to find a typo. Omit "
    "path for everything the servers have seen so far."
)

LSP_DEFINITION = (
    "Jump to where a symbol is defined, given a file and the zero-based line and "
    "character of a use of it. Use this rather than grepping for the name: it follows "
    "imports and re-exports, and it cannot be fooled by a comment or a string that "
    "happens to spell the same word."
)

LSP_REFERENCES = (
    "List every use of the symbol at a position, across the whole project. Run it "
    "before you rename anything, change a function's signature, or delete something "
    "you believe is dead. grep finds the spelling; this finds the symbol."
)

LSP_SYMBOLS = (
    "Outline one file: its classes, functions and methods, with the line each starts "
    "on. Use it to find your way around a long file before reading it, rather than "
    "pulling the whole thing into context."
)

LSP_WORKSPACE_SYMBOLS = (
    "Find a class, function or constant by name anywhere in the project when you do "
    "not know which file holds it. Reach for it before glob and grep when you are "
    "looking for a definition rather than for text."
)

ASK_USER = (
    "Ask the user a question and wait for the answer. Use it whenever the answer is a "
    "choice out of a closed set — a library, a strategy, a scope — instead of writing "
    "\"(a) … (b) … (c) …\" into the transcript and hoping they type a letter back: the "
    "client draws the options as a list the user moves through with the arrow keys. Give "
    "2-8 options, each with a label of a few words and one line saying what choosing it "
    "costs or buys; put the one you recommend first and mark it \"(추천)\" / "
    "\"(recommended)\". Put the reason the answer matters in the question itself. Leave "
    "options out for a genuinely open question. Set multiSelect when several answers can "
    "be true at once. A free-text \"Other\" row is added for you. Several related questions "
    "belong in one call: the client shows them as tabs the user can walk back through and "
    "change their mind in before confirming, and you get every answer at once. The tool "
    "blocks, so ask only what you cannot work out yourself, and read the result: a declined "
    "or timed-out question is not agreement."
)

SET_MODE = (
    "Ask the user to leave plan mode, and switch to what they pick. Call it the moment a "
    "plan is finished with mode=\"accept\" (or \"auto\" when nothing needs watching) instead "
    "of writing \"exit plan mode to start\" into your reply: the client draws a picker with "
    "the mode you asked for first, and the user answers with one keypress. If they choose a "
    "new mode it takes effect immediately, so keep going in the same turn and start "
    "implementing the plan. If they stay in plan mode, or answer nothing, leave the plan as "
    "your reply and change nothing. Never ask the user in prose to switch modes themselves."
)

QUEUE_COMMAND = (
    "Queue a slash command the user has just chosen, e.g. \"/ralph fix the flaky test\". "
    "It does not run inline: it starts as its own turn the moment this one ends, with its "
    "own history. Only queue what the user picked — never a command you decided on "
    "yourself — and say in your reply what you queued."
)

LSP_HOVER = (
    "The type and documentation the language server has for the symbol at a position: "
    "signature, inferred type, docstring. Use it to confirm what a function actually "
    "takes before you call it, instead of inferring it from nearby call sites."
)

LSP_RENAME = (
    "Rename the symbol at a position everywhere it is used, through the language "
    "server, and write the result to disk. Prefer it over a find-and-replace, which "
    "cannot tell a symbol from a string that spells it the same way. Read the list of "
    "changed files it returns. If the server declines, do the rename by hand with "
    "lsp_references as the checklist."
)

__all__ = [
    "ASK_USER",
    "DELEGATE_TASK",
    "EDIT_FILE",
    "GLOB",
    "GREP",
    "LSP_DEFINITION",
    "LSP_DIAGNOSTICS",
    "LSP_HOVER",
    "LSP_REFERENCES",
    "LSP_RENAME",
    "LSP_SYMBOLS",
    "LSP_WORKSPACE_SYMBOLS",
    "QUEUE_COMMAND",
    "READ_FILE",
    "SET_MODE",
    "SHELL",
    "WRITE_FILE",
]
