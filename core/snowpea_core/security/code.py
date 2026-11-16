"""What interpreter code and script files would do, for the security lists (review 5, H4).

Pure functions, a table per language, never anything executed.  Two questions:

* Inline code (``python -c``, ``node -e``, ``perl -e``, ``ruby -e``, ``php -r``)
  and ``execute_code`` bodies: :func:`analyse` finds the destructive and
  process-spawning primitives of that language.  A primitive with a LITERAL
  target becomes what it stands for — ``shutil.rmtree('/')`` is judged as
  ``rm -rf /``, ``os.system("…")`` as that shell line — so the presets, the
  custom patterns and the protected paths judge it like any command.  A
  destructive or spawning primitive whose argument is not a literal (a
  variable, a concatenation, an f-string) makes the code ``opaque``: it asks
  the person in every mode.  Code with none of these is not touched (reading,
  printing, arithmetic stay allowed).
* A script FILE a line runs (``bash x.sh``, ``python x.py``, ``./x``,
  ``source x``): :func:`read_script` reads it (size-capped) so the caller can
  judge its text — a shell script line by line with the shell matcher, other
  languages with :func:`analyse`.

The limit, stated honestly: a static check cannot see what a program computes
at run time (a path built from a network answer, code fetched later, a
compiled binary).  Only OS-level confinement is a guarantee; the security lists
stop the plain and the plainly disguised cases and ask about the rest.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Interpreter family (``jarvis.classify._family``) -> the language its code is in.
LANGUAGES: dict[str, str] = {
    "python": "python", "pypy": "python",
    "node": "node", "nodejs": "node", "deno": "node", "bun": "node",
    "perl": "perl", "ruby": "ruby", "php": "php",
}

#: Largest script file read whole; a bigger one is "unreadable".
MAX_SCRIPT_BYTES = 256 * 1024
#: Most primitives judged in one piece of code.
MAX_FINDINGS = 64
#: A script changed this recently (or written by a tool call this daemon
#: judged) may be the agent's own: unreadable means opaque.
RECENT_SEC = 600.0

#: What a primitive does: ``delete-tree`` (rm -rf), ``delete`` (rm / rmdir),
#: ``move`` (mv), ``write`` (a file is written or its mode changed), ``spawn``
#: (a shell line or argv), ``code`` (a string run as code of the same language).
Action = str


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    action: Action
    #: For ``spawn``: ``shell`` (the argument is a shell line), ``argv`` (a list
    #: of words or a program then a list), ``auto`` (either).
    form: str = "auto"
    #: Matched only when the code mentions this module (``re`` search).
    needs: re.Pattern[str] | None = None


def _r(pattern: str, action: Action, form: str = "auto", needs: str | None = None) -> _Rule:
    return _Rule(re.compile(pattern), action, form, re.compile(needs) if needs else None)


_PY_OS = r"(?<![\w.])(?:os|_os|posix|nt)\."
#: Python: the standard library's deleting, moving, writing and spawning calls.
PYTHON: tuple[_Rule, ...] = (
    _r(r"(?<![\w.])shutil\.rmtree\s*\(", "delete-tree"),
    _r(_PY_OS + r"(?:remove|unlink|rmdir|removedirs)\s*\(", "delete"),
    _r(r"(?<![\w.])shutil\.move\s*\(", "move"),
    _r(_PY_OS + r"(?:rename|renames|replace)\s*\(", "move"),
    _r(_PY_OS + r"(?:chmod|lchmod|chown|lchown|truncate)\s*\(", "write"),
    _r(r"(?<![\w.])shutil\.chown\s*\(", "write"),
    _r(r"(?<![\w.])shutil\.(?:copyfile|copy|copy2|copytree)\s*\(", "copy"),
    _r(_PY_OS + r"(?:system|popen)\s*\(", "spawn", "shell"),
    _r(_PY_OS + r"(?:exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?)\s*\(", "spawn", "exec"),
    _r(r"(?<![\w.])subprocess\.(?:run|call|check_call|check_output|Popen|getoutput|"
       r"getstatusoutput)\s*\(", "spawn"),
    _r(r"(?<![\w.])(?:pty\.spawn|commands\.getoutput|asyncio\.create_subprocess_(?:shell|exec))"
       r"\s*\(", "spawn"),
    _r(r"(?<![\w.])(?:exec|eval|compile)\s*\(", "code"),
    _r(r"(?<![\w.])__import__\s*\(", "code"),
    _r(r"(?<![\w.])open\s*\(", "open"),
)
#: ``Path("x").unlink()`` and friends (``pathlib``), judged by their receiver.
_PY_PATHLIB = re.compile(
    r"\b(?:Path|PurePath|PosixPath|WindowsPath)\s*\(\s*(?P<arg>[^()]*)\)\s*\."
    r"(?P<name>unlink|rmdir|write_text|write_bytes|rename|replace|chmod|touch|mkdir|"
    r"symlink_to|hardlink_to)\s*\("
)
_PY_RECEIVER_DELETE = re.compile(r"\.(?:unlink|rmdir)\s*\(")
#: Names a ``from os import …`` brings in bare.
_PY_FROM_IMPORT = re.compile(r"from\s+(os|shutil|subprocess|pty)\s+import\s+([\w\s,()]+)")
_PY_IMPORT_AS = re.compile(r"import\s+(os|shutil|subprocess)\s+as\s+(\w+)")
_PY_BARE: dict[str, tuple[Action, str]] = {
    "rmtree": ("delete-tree", "auto"), "remove": ("delete", "auto"),
    "unlink": ("delete", "auto"), "rmdir": ("delete", "auto"),
    "removedirs": ("delete", "auto"), "move": ("move", "auto"), "rename": ("move", "auto"),
    "replace": ("move", "auto"), "renames": ("move", "auto"), "system": ("spawn", "shell"),
    "popen": ("spawn", "shell"), "run": ("spawn", "auto"), "call": ("spawn", "auto"),
    "check_call": ("spawn", "auto"), "check_output": ("spawn", "auto"),
    "Popen": ("spawn", "auto"), "getoutput": ("spawn", "shell"), "spawn": ("spawn", "auto"),
    "chmod": ("write", "auto"), "chown": ("write", "auto"),
}

_NODE_FS = r"""(?:['"](?:node:)?fs(?:/promises)?['"]|\bfs\.|\bfsp?\b)"""
_NODE_CP = r"""(?:['"](?:node:)?child_process['"]|\bchild_process\b)"""
#: Node: ``fs`` deletes and writes, ``child_process`` spawns, ``eval``.
NODE: tuple[_Rule, ...] = (
    _r(r"(?:\.|\b)(?:rmSync|rm|rmdirSync|rmdir)\s*\(", "delete-node", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:unlinkSync|unlink)\s*\(", "delete", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:renameSync|rename)\s*\(", "move", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:cpSync|cp|copyFileSync|copyFile|symlinkSync|linkSync)\s*\(", "copy",
       needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:writeFileSync|writeFile|appendFileSync|appendFile|truncateSync|truncate|"
       r"chmodSync|chmod|chownSync|chown)\s*\(", "write", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:execSync|exec)\s*\(", "spawn", "shell", needs=_NODE_CP),
    _r(r"(?:\.|\b)(?:execFileSync|execFile|spawnSync|spawn|fork)\s*\(", "spawn", "exec",
       needs=_NODE_CP),
    _r(r"(?<![\w.$])(?:eval|Function)\s*\(", "code"),
    _r(r"\bprocess\.binding\s*\(", "opaque-call"),
)
#: Perl: unlink/rmdir/rmtree, system/exec/backticks/qx, open for writing, rename.
PERL: tuple[_Rule, ...] = (
    _r(r"\b(?:rmtree|remove_tree)\b\s*\(?", "delete-tree"),
    _r(r"\b(?:unlink|rmdir)\b\s*\(?", "delete"),
    _r(r"\brename\b\s*\(?", "move"),
    _r(r"\b(?:chmod|chown|truncate)\b\s*\(?", "write"),
    _r(r"\b(?:system|exec)\b\s*\(?", "spawn"),
    _r(r"\beval\b\s*\(?", "code"),
)
#: Ruby: FileUtils, File/Dir deletes, system/exec/spawn/backticks/%x, IO.popen, Open3.
RUBY: tuple[_Rule, ...] = (
    _r(r"\bFileUtils\.(?:rm_rf|rm_r|remove_dir|remove_entry|remove_entry_secure|rmtree)\b\s*\(?",
       "delete-tree"),
    _r(r"\bFileUtils\.(?:rm|rm_f|remove|remove_file|rmdir|safe_unlink)\b\s*\(?", "delete"),
    _r(r"\b(?:File|Dir)\.(?:delete|unlink|rmdir)\b\s*\(?", "delete"),
    _r(r"\b(?:FileUtils\.(?:mv|move)|File\.rename)\b\s*\(?", "move"),
    _r(r"\b(?:File\.(?:write|chmod|chown)|FileUtils\.(?:chmod|chmod_R|chown|chown_R|touch))\b"
       r"\s*\(?", "write"),
    _r(r"(?<![\w.])(?:Kernel\.)?(?:system|exec|spawn)\b\s*\(?", "spawn"),
    _r(r"\b(?:IO\.popen|Open3\.\w+|Process\.spawn)\s*\(?", "spawn"),
    _r(r"(?<![\w.])(?:Kernel\.)?(?:eval|instance_eval|class_eval)\b\s*\(?", "code"),
)
#: PHP: unlink/rmdir/rename, the exec family, backticks, file_put_contents.
PHP: tuple[_Rule, ...] = (
    _r(r"\b(?:unlink|rmdir)\s*\(", "delete"),
    _r(r"\brename\s*\(", "move"),
    _r(r"\b(?:file_put_contents|chmod|chown|fopen)\s*\(", "write"),
    _r(r"\b(?:system|exec|shell_exec|passthru|popen|proc_open|pcntl_exec)\s*\(", "spawn",
       "shell"),
    _r(r"\b(?:eval|assert|create_function)\s*\(", "code"),
)

RULES: dict[str, tuple[_Rule, ...]] = {
    "python": PYTHON, "node": NODE, "perl": PERL, "ruby": RUBY, "php": PHP,
}


@dataclass
class Finding:
    """What one piece of code would do, as far as the table sees."""

    #: Commands it stands for (``["rm", "-rf", "/"]``), judged like any command.
    commands: list[list[str]] = field(default_factory=list)
    #: Shell lines it hands to a shell (``os.system("…")``), parsed as lines.
    lines: list[str] = field(default_factory=list)
    #: ``(path, destructive, recursive)`` it writes, deletes or moves.
    writes: list[tuple[str, bool, bool]] = field(default_factory=list)
    #: Why part of it cannot be seen (``code:delete:non-literal`` …).
    opaque: list[str] = field(default_factory=list)

    def merge(self, other: Finding) -> None:
        self.commands.extend(other.commands)
        self.lines.extend(other.lines)
        self.writes.extend(other.writes)
        for why in other.opaque:
            if why not in self.opaque:
                self.opaque.append(why)

    def _hide(self, why: str) -> None:
        if why not in self.opaque:
            self.opaque.append(why)


# ---------------------------------------------------------------------------
# literals
# ---------------------------------------------------------------------------

_INTERPOLATES: dict[str, re.Pattern[str]] = {
    # The double-quoted strings of these languages interpolate.
    "perl": re.compile(r"[$@]"),
    "ruby": re.compile(r"#\{"),
    "php": re.compile(r"\$"),
}


def _skip_space(code: str, index: int) -> int:
    while index < len(code) and code[index] in " \t\r\n":
        index += 1
    return index


def _string_at(code: str, index: int, language: str) -> tuple[str | None, int]:
    """A string literal starting at ``index``: ``(value, end)``; ``(None, index)`` when not."""
    index = _skip_space(code, index)
    start = index
    prefix = ""
    while index < len(code) and code[index].isalpha() and len(prefix) < 3:
        prefix += code[index]
        index += 1
    if prefix and (language != "python" or not re.fullmatch(r"(?i)[rbu]{1,2}|f|rf|fr|br|rb",
                                                             prefix)):
        return None, start
    if prefix.lower().count("f"):
        return None, start  # an f-string: computed
    if index >= len(code) or code[index] not in "\"'`":
        return None, start
    quote = code[index]
    if language == "python" and code[index:index + 3] in ('"""', "'''"):
        end = code.find(code[index:index + 3], index + 3)
        if end < 0:
            return None, start
        return code[index + 3:end], end + 3
    out: list[str] = []
    j = index + 1
    while j < len(code):
        char = code[j]
        if char == "\\" and j + 1 < len(code):
            out.append(code[j + 1])
            j += 2
            continue
        if char == quote:
            text = "".join(out)
            if quote == "`" and "${" in text:
                return None, start  # a JS template with expressions
            if quote == '"' and language in _INTERPOLATES and _INTERPOLATES[language].search(text):
                return None, start
            if quote == "`" and language != "node":
                return None, start  # backticks: a command in perl/ruby/php, not a string
            return text, j + 1
        out.append(char)
        j += 1
    return None, start


def _list_at(code: str, index: int, language: str) -> tuple[list[str] | None, int]:
    """A list of string literals (``["rm", "-rf", "/"]``, perl/ruby lists without brackets)."""
    index = _skip_space(code, index)
    if index >= len(code) or code[index] not in "[(":
        return None, index
    close = "]" if code[index] == "[" else ")"
    items: list[str] = []
    j = _skip_space(code, index + 1)
    while j < len(code):
        if code[j] == close:
            return items, j + 1
        value, end = _string_at(code, j, language)
        if value is None:
            return None, index
        items.append(value)
        j = _skip_space(code, end)
        if j < len(code) and code[j] == ",":
            j = _skip_space(code, j + 1)
    return None, index


def _call_end(code: str, index: int) -> int:
    """The index after the call's closing parenthesis (``index`` just after ``(``)."""
    depth = 0
    j = index
    quote = ""
    while j < len(code):
        char = code[j]
        if quote:
            if char == "\\":
                j += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return j + 1
            depth -= 1
        elif char in ";\n" and depth == 0 and code[index - 1:index] != "(":
            return j
        j += 1
    return len(code)


def _args(code: str, index: int, language: str) -> list[str | list[str] | None]:
    """The call's arguments from ``index`` (just after the name and an optional ``(``):
    each a string, a list of strings, or ``None`` (not a literal).  At most four."""
    out: list[str | list[str] | None] = []
    end = _call_end(code, index)
    j = index
    while j < end and len(out) < 4:
        j = _skip_space(code, j)
        if j >= end or code[j] in ");":
            break
        item: str | list[str] | None
        item, after = _string_at(code, j, language)
        if item is None:
            items, after_list = _list_at(code, j, language)
            if items is not None:
                item, after = items, after_list
        if item is None:
            out.append(None)
            j = _next_comma(code, j, end)
        else:
            k = _skip_space(code, after)
            if k < end and code[k] not in ",);\n":
                # ``"a" + x``, ``"a" % x``, ``"a".format(…)``: the argument is computed.
                out.append(None)
                j = _next_comma(code, k, end)
            else:
                out.append(item)
                j = k
        if j < end and code[j] == ",":
            j += 1
            continue
        break
    return out


def _next_comma(code: str, index: int, end: int) -> int:
    depth = 0
    quote = ""
    j = index
    while j < end:
        char = code[j]
        if quote:
            if char == "\\":
                j += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif char == "," and depth == 0:
            return j
        j += 1
    return end


# ---------------------------------------------------------------------------
# the analysis
# ---------------------------------------------------------------------------


def _strip_comments(code: str, language: str) -> str:
    """Drop whole-line comments (``#`` / ``//``) so a commented-out call is not a call."""
    marks = ("//",) if language == "node" else ("#",) if language != "php" else ("#", "//")
    kept = []
    for line in code.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(marks) and not stripped.startswith("#!"):
            kept.append("")
            continue
        kept.append(line)
    return "\n".join(kept)


def _python_rules(code: str) -> list[_Rule]:
    rules = list(PYTHON)
    for module, names in _PY_FROM_IMPORT.findall(code):
        for name in re.findall(r"\w+", names):
            if name in _PY_BARE and name != "as":
                action, form = _PY_BARE[name]
                if module in ("os", "shutil", "subprocess", "pty"):
                    rules.append(_r(rf"(?<![\w.]){re.escape(name)}\s*\(", action, form))
    for module, alias in _PY_IMPORT_AS.findall(code):
        del module
        rules.append(_r(rf"(?<![\w.]){re.escape(alias)}\.(?:rmtree|remove|unlink|rmdir|"
                        r"removedirs)\s*\(", "delete-tree"))
        rules.append(_r(rf"(?<![\w.]){re.escape(alias)}\.(?:system|popen|run|call|check_call|"
                        r"check_output|Popen|getoutput|exec\w*|spawn\w*)\s*\(", "spawn"))
    return rules


def analyse(code: str, language: str, *, depth: int = 0) -> Finding:
    """What ``code`` in ``language`` would delete, move, write or run.  Never raises."""
    found = Finding()
    try:
        _analyse(code, language, depth, found)
    except Exception:  # noqa: BLE001 - a bug here makes the code unseeable, never allowed
        found._hide("code:internal")
    return found


def _mask(text: str, language: str) -> str:
    """``text`` with the inside of every string literal blanked (same length), so a
    word inside a string (``print("unlink me")``) is never taken for a call."""
    out = list(text)
    index = 0
    quotes = "\"'" if language != "node" else "\"'`"
    while index < len(text):
        char = text[index]
        if char in quotes:
            triple = text[index:index + 3]
            if language == "python" and triple in ('"""', "'''"):
                end = text.find(triple, index + 3)
                end = len(text) if end < 0 else end
                for k in range(index + 3, end):
                    out[k] = " " if text[k] != "\n" else "\n"
                index = end + 3
                continue
            j = index + 1
            while j < len(text) and text[j] != char and text[j] != "\n":
                if text[j] == "\\":
                    out[j] = " "
                    j += 1
                    if j < len(text):
                        out[j] = " "
                    j += 1
                    continue
                out[j] = " "
                j += 1
            index = j + 1
            continue
        index += 1
    return "".join(out)


def _analyse(code: str, language: str, depth: int, found: Finding) -> None:
    rules = RULES.get(language)
    if rules is None:
        return
    text = _strip_comments(code, language)
    masked = _mask(text, language)
    if language == "python":
        rules = tuple(_python_rules(masked))
        _pathlib(text, found)
    if language in ("perl", "ruby", "php"):
        _backticks(text, masked, language, found)
    seen = 0
    for rule in rules:
        if rule.needs is not None and not rule.needs.search(text):
            continue
        for match in rule.pattern.finditer(masked):
            seen += 1
            if seen > MAX_FINDINGS:
                found._hide("code:too-many")
                return
            if language == "python" and rule.action == "code" and "__import__" in match.group(0):
                _python_import(text, match.end(), found)
                continue
            args = _args(text, match.end(), language)
            _judge(rule, args, language, depth, found, text, match)


def _python_import(text: str, index: int, found: Finding) -> None:
    """``__import__("os").system(…)``: the module named by a literal, its call judged."""
    args = _args(text, index, "python")
    module = args[0] if args and isinstance(args[0], str) else None
    if module is None:
        found._hide("code:import:non-literal")
        return
    tail = text[_call_end(text, index):]
    call = re.match(r"\s*\.\s*(\w+)\s*\(", tail)
    if call is None:
        return
    synthetic = f"{module}.{call.group(1)}({tail[call.end():]}"
    sub = Finding()
    _analyse(synthetic, "python", 1, sub)
    found.merge(sub)


def _pathlib(text: str, found: Finding) -> None:
    for match in _PY_PATHLIB.finditer(text):
        value, _end = _string_at(match.group("arg"), 0, "python")
        name = match.group("name")
        if value is None:
            if name in ("unlink", "rmdir", "rename", "replace"):
                found._hide(f"code:{name}:non-literal")
            continue
        if name in ("unlink",):
            found.commands.append(["rm", value])
            found.writes.append((value, True, False))
        elif name == "rmdir":
            found.commands.append(["rmdir", value])
            found.writes.append((value, True, False))
        elif name in ("rename", "replace"):
            found.writes.append((value, True, False))
        else:
            found.writes.append((value, False, False))
    if ("pathlib" in text or "Path(" in text) and not _PY_PATHLIB.search(text):
        for match in _PY_RECEIVER_DELETE.finditer(text):
            before = text[max(0, match.start() - 12):match.start()]
            if not re.search(r"(?:os|shutil)$", before):
                found._hide("code:unlink:non-literal")


_BACKTICK = re.compile(r"`([^`]*)`|\bqx\s*([{(\[/|!])")
_CLOSE = {"{": "}", "(": ")", "[": "]", "/": "/", "|": "|", "!": "!"}


def _backticks(text: str, masked: str, language: str, found: Finding) -> None:
    """Backticks and ``qx{}``/``%x{}`` outside string literals: a shell line."""
    for match in _BACKTICK.finditer(masked):
        match = _BACKTICK.match(text, match.start()) or match
        if match.group(1) is not None:
            body = match.group(1)
        else:
            close = _CLOSE[match.group(2)]
            end = text.find(close, match.end())
            body = text[match.end():end] if end > 0 else ""
        if _INTERPOLATES.get(language, re.compile(r"\$")).search(body) or not body.strip():
            found._hide("code:backticks:non-literal")
            continue
        found.lines.append(body)
    if language == "ruby":
        for match in re.finditer(r"%x\s*([{(\[|!])", text):
            close = _CLOSE[match.group(1)]
            end = text.find(close, match.end())
            body = text[match.end():end] if end > 0 else ""
            if "#{" in body or not body.strip():
                found._hide("code:backticks:non-literal")
            else:
                found.lines.append(body)


def _judge(rule: _Rule, args: list[str | list[str] | None], language: str, depth: int,
           found: Finding, text: str, match: re.Match[str]) -> None:
    action = rule.action
    first = args[0] if args else None
    if action == "opaque-call":
        found._hide("code:native-binding")
        return
    if action == "open":
        _python_open(args, text, match, found)
        return
    if action == "code":
        if isinstance(first, str) and depth < 3:
            found.merge(analyse(first, language, depth=depth + 1))
        elif not args and language == "perl":
            return  # ``eval { … }`` blocks are code already read
        else:
            found._hide("code:eval:non-literal")
        return
    if action == "spawn":
        _spawn(rule.form, args, found)
        return
    if action == "delete-node":
        options = text[match.end():_call_end(text, match.end())]
        action = "delete-tree" if re.search(r"\brecursive\s*:\s*true", options) else "delete"
    if action in ("delete", "delete-tree", "move"):
        targets = [a for a in args if isinstance(a, str)]
        if language == "perl" and isinstance(first, list):
            targets = list(first)  # unlink("a", "b") parsed as a list
        if not targets or (first is None and action != "move") or (
            action == "move" and first is None
        ):
            found._hide(f"code:{action}:non-literal")
            return
        if action == "move":
            found.commands.append(["mv", *(t for t in targets[:2])])
            found.writes.append((targets[0], True, True))
            if len(targets) > 1:
                found.writes.append((targets[1], False, True))
            return
        for target in targets if language in ("perl", "ruby", "php") else targets[:1]:
            if action == "delete-tree":
                found.commands.append(["rm", "-rf", target])
                found.writes.append((target, True, True))
            else:
                found.commands.append(["rm", target])
                found.writes.append((target, True, False))
        return
    if action == "write":
        for target in args[:1]:
            if isinstance(target, str):
                found.writes.append((target, False, False))
        return
    if action == "copy" and len(args) > 1 and isinstance(args[1], str):
        found.writes.append((args[1], False, True))


def _python_open(args: list[str | list[str] | None], text: str, match: re.Match[str],
                 found: Finding) -> None:
    """``open(path, "w")`` (or ``mode="a"``): a write of a literal path."""
    if not args:
        return
    mode = args[1] if len(args) > 1 and isinstance(args[1], str) else None
    if mode is None:
        keyword = re.search(r"mode\s*=\s*['\"]([^'\"]*)['\"]",
                            text[match.end():_call_end(text, match.end())])
        mode = keyword.group(1) if keyword else "r"
    if not re.search(r"[wax+]", mode):
        return
    if isinstance(args[0], str):
        found.writes.append((args[0], False, False))


def _spawn(form: str, args: list[str | list[str] | None], found: Finding) -> None:
    first = args[0] if args else None
    if first is None:
        found._hide("code:spawn:non-literal")
        return
    if isinstance(first, list):
        if first:
            found.commands.append(list(first))
        return
    if form == "exec":
        # A program, then its argv (``os.execv("/bin/rm", ["rm", "-rf", "/"])``,
        # ``spawn("rm", ["-rf", "/"])``).  Later positional arguments are options.
        rest = args[1:2]
        if any(item is None for item in rest):
            found._hide("code:spawn:non-literal")
            return
        words = [first]
        for item in rest:
            if isinstance(item, list):
                words.extend(item[1:] if item and Path(item[0]).name == Path(first).name
                             else item)
            elif isinstance(item, str):
                words.append(item)
        found.commands.append(words)
        return
    if len(args) > 1 and all(isinstance(item, str) for item in args):
        # ``system("rm", "-rf", "/")`` (perl, ruby, php): the words themselves.
        found.commands.append([item for item in args if isinstance(item, str)])
        return
    # A string is a shell line (os.system, subprocess(…, shell=True), exec) or a
    # program name; either way it is judged as a line.
    found.lines.append(first)


# ---------------------------------------------------------------------------
# script files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Script:
    """A script a line runs, as read now."""

    path: str
    #: The text, or ``None`` when it cannot be read (missing, too big, binary).
    text: str | None
    #: ``shell`` or a :data:`LANGUAGES` value.
    language: str
    #: ``sha256:<hex>`` of the bytes, or ``missing`` / ``unreadable`` / ``too-large``.
    digest: str
    #: Changed recently, or written through a tool call this daemon judged.
    recent: bool


_RECENT_WRITES: dict[str, float] = {}
_RECENT_LOCK = threading.Lock()


def note_write(path: str | os.PathLike[str]) -> None:
    """Remember that a tool call was let write ``path`` (its script is then the agent's)."""
    try:
        key = os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return
    with _RECENT_LOCK:
        _RECENT_WRITES[key] = time.time()
        if len(_RECENT_WRITES) > 4096:
            for old in sorted(_RECENT_WRITES, key=_RECENT_WRITES.__getitem__)[:1024]:
                _RECENT_WRITES.pop(old, None)


def _written_by_tools(real: str) -> bool:
    with _RECENT_LOCK:
        return real in _RECENT_WRITES


_SHEBANG = re.compile(r"^#!\s*(\S+)(?:\s+(\S+))?")


def language_of(path: str, text: str | None, default: str) -> str:
    """The script's language: its ``#!`` line, else ``default``."""
    if text:
        match = _SHEBANG.match(text)
        if match:
            program = Path(match.group(1)).name
            if program == "env" and match.group(2):
                program = match.group(2)
            program = re.sub(r"[0-9.]+$", "", program)
            if program in ("sh", "bash", "zsh", "dash", "ksh", "ash", "mksh"):
                return "shell"
            if program in LANGUAGES:
                return LANGUAGES[program]
    suffix = Path(path).suffix.lower()
    return {".sh": "shell", ".bash": "shell", ".py": "python", ".js": "node", ".mjs": "node",
            ".cjs": "node", ".pl": "perl", ".rb": "ruby", ".php": "php"}.get(suffix, default)


def read_script(raw: str, cwd: Path | None, *, default: str = "shell") -> Script | None:
    """The script ``raw`` names, read now (``None``: the word is not a file path at all)."""
    if not raw or "$" in raw or "`" in raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
    except OSError:
        return Script(str(path), None, language_of(str(path), None, default), "missing", True)
    recent = (time.time() - max(info.st_mtime, info.st_ctime) < RECENT_SEC) or \
        _written_by_tools(real)
    if not stat.S_ISREG(info.st_mode):
        return Script(str(path), None, default, "unreadable", recent)
    if info.st_size > MAX_SCRIPT_BYTES:
        return Script(str(path), None, language_of(str(path), None, default), "too-large",
                      recent)
    try:
        with open(real, "rb") as handle:
            data = handle.read(MAX_SCRIPT_BYTES + 1)
    except OSError:
        return Script(str(path), None, language_of(str(path), None, default), "unreadable",
                      recent)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if b"\0" in data[:8192]:
        return Script(str(path), None, default, digest, recent)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Script(str(path), None, default, digest, recent)
    return Script(str(path), text, language_of(str(path), text, default), digest, recent)


def digest_now(path: str) -> str:
    """The digest :func:`read_script` would record for ``path`` now."""
    script = read_script(path, None)
    return script.digest if script is not None else "missing"


def argv_text(words: list[str]) -> str:
    """Words as one shell line (for messages)."""
    return " ".join(shlex.quote(w) for w in words)


__all__ = [
    "LANGUAGES",
    "MAX_SCRIPT_BYTES",
    "RECENT_SEC",
    "Finding",
    "Script",
    "analyse",
    "argv_text",
    "digest_now",
    "language_of",
    "note_write",
    "read_script",
]
