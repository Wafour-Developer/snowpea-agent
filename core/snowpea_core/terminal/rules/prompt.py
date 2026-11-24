"""The command a permission prompt on screen asks about, read strictly (M37 §2.11).

Jarvis answers a terminal's permission prompt only for a command it could
classify.  The screen rules' ``extract`` is good enough for a status line, but
the command itself can steer a loose regex (security review H3): a command
whose body contains "Do you want to proceed?" ends a lazy match early, and the
last ``$`` line on a Codex screen can belong to the command's own output.

So this reads the prompt from the bottom up, anchored on its option list:

* the option list is the last one on screen (``1. Yes …``);
* the question line sits directly above it;
* the header (Claude Code: "Bash command"; Codex: the run question) must be
  visible above the question, inside the same prompt;
* the command is what lies between them — refused when it holds a prompt
  phrase, a second ``$`` command line (Codex), or a sign of truncation
  (``…``, ``+N lines``, ``ctrl+… to expand``).

:func:`prompt_command` returns ``(command, None)`` or ``(None, why)``; anything
unexpected is a refusal: the prompt goes to the person.
"""

from __future__ import annotations

import re
import textwrap

_BOX = "│┃|╭╮╰╯─━"
_PROMPT_PHRASES = (
    "do you want to proceed", "bash command", "esc to cancel", "allow command",
    "press enter to confirm", "would you like to run", "don't ask again",
    "tell claude what to do", "tell codex what to do", "❯", "›",
)
_OPTION_ONE = re.compile(r"(?i)^\s*[❯›>]?\s*1\.\s*yes\b")
_TRUNCATED = re.compile(r"(?i)(…|\.\.\.\s*$|\+\s*\d+\s+lines?\b|ctrl\+\w+\s+to\s+expand)")
_CODEX_QUESTIONS = ("would you like to run the following command?", "allow command?")
MAX_COMMAND = 4000


def _clean(line: str) -> str:
    return line.strip().strip(_BOX).rstrip()


def _options_index(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if _OPTION_ONE.match(_clean(lines[index])):
            return index
    return None


def _above(lines: list[str], index: int) -> int | None:
    """The nearest non-blank line above ``index``."""
    for above in range(index - 1, -1, -1):
        if _clean(lines[above]):
            return above
    return None


def _unbox(line: str) -> str:
    """A row without a prompt box's side borders (indentation kept for dedent)."""
    row = line.rstrip()
    if row.endswith(("│", "┃")):
        row = row[:-1].rstrip()
    stripped = row.lstrip()
    if stripped.startswith(("│", "┃")):
        row = " " * (len(row) - len(stripped)) + " " + stripped[1:]
    return row


def _body(lines: list[str]) -> str:
    rows = [_unbox(line) for line in lines]
    while rows and not rows[0].strip():
        rows.pop(0)
    while rows and not rows[-1].strip():
        rows.pop()
    return textwrap.dedent("\n".join(rows)).strip()


def _refusal(command: str) -> str | None:
    if not command:
        return "the prompt shows no command"
    if len(command) > MAX_COMMAND:
        return "the command is too long to read"
    lower = command.lower()
    if any(phrase in lower for phrase in _PROMPT_PHRASES):
        return "the command text contains prompt wording"
    if any(_TRUNCATED.search(line) for line in command.split("\n")):
        return "the command looks cut off on screen"
    return None


def _claude(lines: list[str]) -> tuple[str | None, str | None]:
    options = _options_index(lines)
    if options is None:
        return None, "no option list on screen"
    question = _above(lines, options)
    if question is None or _clean(lines[question]).lower() != "do you want to proceed?":
        return None, "the question is not directly above the options"
    header = None
    for index in range(question - 1, -1, -1):
        if _clean(lines[index]).lower() == "bash command":
            header = index
            break
    if header is None:
        return None, "the prompt's header is not visible"
    command = _body(lines[header + 1:question])
    why = _refusal(command)
    return (None, why) if why else (command, None)


def _codex(lines: list[str]) -> tuple[str | None, str | None]:
    options = _options_index(lines)
    if options is None:
        return None, "no option list on screen"
    question = None
    for index in range(options - 1, -1, -1):
        if _clean(lines[index]).lower() in _CODEX_QUESTIONS:
            question = index
            break
    if question is None:
        return None, "the prompt's question is not visible"
    block = lines[question + 1:options]
    starts = [i for i, line in enumerate(block) if _clean(line).startswith("$ ")]
    if len(starts) != 1:
        return None, ("the prompt shows no command" if not starts
                      else "the prompt shows more than one command line")
    first = starts[0]
    if any(_clean(line) for line in block[:first]):
        return None, "unexpected text between the question and the command"
    rows = [_clean(block[first])[2:]]
    for line in block[first + 1:]:
        if not _clean(line):
            break
        rows.append(line)
    if any(_clean(line) for line in block[first + len(rows):]):
        return None, "unexpected text between the command and the options"
    command = _body(rows)
    why = _refusal(command)
    return (None, why) if why else (command, None)


def prompt_command(cli: str, screen: str) -> tuple[str | None, str | None]:
    """``(command, None)`` read from the permission prompt on ``screen``, or ``(None, why)``."""
    lines = str(screen or "").replace("\r\n", "\n").split("\n")
    if cli == "claude":
        return _claude(lines)
    if cli == "codex":
        return _codex(lines)
    return None, f"snowpea does not read {cli}'s permission prompts"


def same_command(first: str | None, second: str | None) -> bool:
    """Equal once whitespace is ignored (a screen wraps long commands)."""
    if not first or not second:
        return False
    return re.sub(r"\s+", "", first) == re.sub(r"\s+", "", second)


__all__ = ["prompt_command", "same_command"]
