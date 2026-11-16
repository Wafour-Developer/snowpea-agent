"""What interpreter code and script files would do, for the security lists (review 5, H4;
review 6, H-3 and M-1).

Pure functions, never anything executed.  Two questions:

* Inline code (``python -c``, ``node -e``, ``perl -e``, ``ruby -e``, ``php -r``,
  code fed by a here-document) and ``execute_code`` bodies: :func:`analyse`
  finds the deleting, writing and process-spawning primitives.  A primitive
  with a LITERAL target becomes what it stands for — ``shutil.rmtree('/')`` is
  judged as ``rm -rf /``, ``os.system("…")`` as that shell line — so the
  presets, the custom patterns and the protected paths judge it like any
  command.  A primitive whose target is not a literal makes the code
  ``opaque``: it asks the person in every mode.
* A script FILE a line runs (``bash x.sh``, ``python x.py``, ``./x``,
  ``source x``): :func:`read_script` reads it (size-capped) so the caller can
  judge its text — a shell script line by line with the shell matcher, other
  languages with :func:`analyse`.

Python is read as a syntax tree (:mod:`snowpea_core.security.pycode`).  Node,
Perl, Ruby and PHP keep a table of calls per language, with the uncertainty
flipped (review 6): "no finding" means allowed only when nothing uncertain was
seen.  A call the tables match in the text but not once strings are blanked,
next to a construct the blanking does not understand (a regex literal, a
comment, a here-document, a template expression, an unclosed quote), is
opaque; so is — in JavaScript — a bracketed lookup on ``fs`` / ``child_process``
/ ``process``, an alias or a renamed destructuring of one of their calls, and
``require`` / ``import()`` / ``eval`` / ``Function`` of anything but a literal.
A language with no table cannot be read at all.

The limit, stated honestly: a static check cannot see what a program computes
at run time (a path built from a network answer, code fetched later, a
compiled binary).  Only OS-level confinement is a guarantee; the security lists
stop the plain and the plainly disguised cases and ask about the rest.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from snowpea_core.security import written

#: Interpreter family (``jarvis.classify._family``) -> the language its code is in.
LANGUAGES: dict[str, str] = {
    "python": "python", "pypy": "python",
    "node": "node", "nodejs": "node", "deno": "node", "bun": "node", "tsx": "node",
    "ts-node": "node",
    "perl": "perl", "ruby": "ruby", "php": "php",
}

#: Largest script file read whole; a bigger one is "unreadable".
MAX_SCRIPT_BYTES = 256 * 1024
#: Most primitives judged in one piece of code.
MAX_FINDINGS = 64
#: A script changed this recently may be the agent's own even when no record
#: says so (a file a program wrote).  One criterion of two: every path the
#: agent is known to have written counts whatever its age (review 6, M-8).
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


# Python is not in these tables: it is read as a syntax tree
# (:mod:`snowpea_core.security.pycode`, security review 6, H-3).

_NODE_FS = r"""(?:['"`](?:node:)?fs(?:/promises)?['"`]|\bfs\.|\bfsp?\b)"""
_NODE_CP = r"""(?:['"`](?:node:)?child_process['"`]|\bchild_process\b)"""
_NODE_VM = r"""(?:['"`](?:node:)?vm['"`]|\bvm\.)"""
#: Node: ``fs`` deletes and writes, ``child_process`` spawns, ``eval``.
NODE: tuple[_Rule, ...] = (
    _r(r"(?:\.|\b)(?:rmSync|rm|rmdirSync|rmdir)\s*\(", "delete-node", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:unlinkSync|unlink)\s*\(", "delete", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:renameSync|rename)\s*\(", "move", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:cpSync|cp|copyFileSync|copyFile)\s*\(", "copy", needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:symlinkSync|linkSync)\s*\(|\.\s*(?:symlink|link)\s*\(", "link",
       needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:writeFileSync|writeFile|appendFileSync|appendFile|truncateSync|truncate|"
       r"chmodSync|chmod|chownSync|chown|lchownSync|lchmodSync|utimesSync|utimes|"
       r"createWriteStream|mkdirSync|mkdir|mkdtempSync|mkdtemp)\s*\(", "write", needs=_NODE_FS),
    _r(r"(?:\.|\b)openSync\s*\(|\bfsp?(?:\s*\.\s*promises)?\s*\.\s*open\s*\(", "open-node",
       needs=_NODE_FS),
    _r(r"(?:\.|\b)(?:execSync|exec)\s*\(", "spawn", "shell", needs=_NODE_CP),
    _r(r"(?:\.|\b)(?:execFileSync|execFile|spawnSync|spawn|fork)\s*\(", "spawn", "exec",
       needs=_NODE_CP),
    _r(r"(?<![\w.$])eval\s*\(", "code"),
    # Code built from a string by anything but ``eval('literal')`` (review 7, H7-6).
    _r(r"(?<![\w.$])Function\s*\(", "opaque-eval"),
    _r(r"(?:\.|\b)(?:runInNewContext|runInThisContext|runInContext|compileFunction|Script|"
       r"SourceTextModule)\s*\(", "opaque-eval", needs=_NODE_VM),
    _r(r"\bWorker\b[\s)]*\(", "worker"),
    _r(r"\bprocess\.(?:binding|dlopen|mainModule)\b", "opaque-call"),
)
#: The names the ``fs`` / ``child_process`` rules above call.  Any other use of
#: one — an alias, a renamed destructuring, a bracketed lookup — cannot be
#: followed by a table and is opaque (security review 6, H-3).
_NODE_NAMES = (
    "rmSync", "rm", "rmdirSync", "rmdir", "unlinkSync", "unlink", "renameSync", "rename",
    "cpSync", "cp", "copyFileSync", "copyFile", "symlinkSync", "linkSync", "writeFileSync",
    "writeFile", "appendFileSync", "appendFile", "truncateSync", "truncate", "chmodSync",
    "chmod", "chownSync", "chown", "execSync", "exec", "execFileSync", "execFile", "spawnSync",
    "spawn", "fork", "createWriteStream", "mkdirSync", "mkdtempSync", "openSync",
    "utimesSync", "lchownSync", "lchmodSync",
)
_NODE_NAME = re.compile(r"(?<![\w$])(" + "|".join(_NODE_NAMES) + r")(?![\w$])")
_NODE_DANGEROUS_MODULE = r"(?:node:)?(?:fs|fs/promises|child_process|os|process|module|vm)"
#: ``const x = require('fs')`` / ``import x from 'fs'`` / ``import * as x from 'fs'``.
_NODE_MODULE_ALIAS = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['\"`]"
    + _NODE_DANGEROUS_MODULE + r"['\"`]\s*\)(?!\s*[.\[])"
    r"|import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s+['\"`]"
    + _NODE_DANGEROUS_MODULE + r"['\"`]")
#: ``const {a, b: c} = …`` / ``import {a, b as c} from 'x'``: a destructuring pattern.
_NODE_DESTRUCTURE = re.compile(
    r"(?:const|let|var)\s*\{([^{}]*)\}\s*=(?!=)|import\s*\{([^{}]*)\}\s*from\b")
_NODE_REFLECT = re.compile(
    r"\b(?:globalThis|global|module|process)\s*\[|\bReflect\s*\.|\bprocess\.(?:binding|dlopen|"
    r"mainModule)\b|\bmodule\.constructor\b|\.constructor\s*\.\s*constructor\b|"
    r"\b_load\s*\(")
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
    "node": NODE, "perl": PERL, "ruby": RUBY, "php": PHP,
}
#: Languages :func:`analyse` can read at all.
ANALYSED: frozenset[str] = frozenset({"python", *RULES})

#: Constructs the string masking of the table languages does not understand: a
#: quote inside one of them can hide a call from the tables (review 6, H-3).
_RISKY: dict[str, re.Pattern[str]] = {
    # A regex literal or a comment (``/``), a template expression.
    "node": re.compile(r"/|\$\{"),
    # Regex and quote-like operators, comments, here-documents, ``$'`` and ``$"``.
    "perl": re.compile(r"[/#]|<<|\$['\"`]|\b(?:q|qq|qw|qx|m|s|tr|y)\s*[^\w\s,;=)]|^=\w+",
                       re.MULTILINE),
    "ruby": re.compile(r"[/#]|<<|\$['\"`]|%[qQwWiIrxs]?[\[({<|!/]|\?['\"`]|^=begin",
                       re.MULTILINE),
    "php": re.compile(r"[/#]|<<<"),
}


@dataclass
class Finding:
    """What one piece of code would do, as far as it can be read."""

    #: Commands it stands for (``["rm", "-rf", "/"]``), judged like any command.
    commands: list[list[str]] = field(default_factory=list)
    #: Commands it spawns from a list of words (``subprocess.run([...])``):
    #: ``(word, computed)`` each, walked like a shell command whose computed
    #: words are ``$X``.
    argv: list[list[tuple[str, bool]]] = field(default_factory=list)
    #: Shell lines it hands to a shell (``os.system("…")``), parsed as lines.
    lines: list[str] = field(default_factory=list)
    #: ``(path, destructive, recursive)`` it writes, deletes or moves.
    writes: list[tuple[str, bool, bool]] = field(default_factory=list)
    #: Literal paths it opens to read (judged against the secret files).
    reads: list[str] = field(default_factory=list)
    #: Path-like literals it names at all (judged against snowpea's control files).
    mentions: list[str] = field(default_factory=list)
    #: Of :attr:`mentions`, those whose every use is a recognised read of exactly
    #: that expression (security review 7, H7-3).  Only the syntax tree can say.
    read_only: set[str] = field(default_factory=set)
    #: Files it makes a link to (``os.symlink(src, …)``): judged as ``ln``'s source.
    links: list[str] = field(default_factory=list)
    #: The new names those links get (security review 8, H8-2: one made inside
    #: snowpea's home asks the person).
    linked: list[str] = field(default_factory=list)
    #: Modules it imports by name (python) — one the project holds is read too.
    imports: list[str] = field(default_factory=list)
    #: Files it loads by a relative path (node ``require('./x')``): read too.
    requires: list[str] = field(default_factory=list)
    #: Why part of it cannot be seen (``code:delete:non-literal`` …).
    opaque: list[str] = field(default_factory=list)
    #: Why it always asks a person (``snowpea-code``): never switched off.
    control: list[str] = field(default_factory=list)

    def merge(self, other: Finding) -> None:
        self.commands.extend(other.commands)
        self.argv.extend(other.argv)
        self.lines.extend(other.lines)
        self.writes.extend(other.writes)
        self.reads.extend(other.reads)
        self.links.extend(other.links)
        self.linked.extend(other.linked)
        for item in other.mentions:
            if item not in self.mentions:
                self.mentions.append(item)
                if item in other.read_only:
                    self.read_only.add(item)
            elif item not in other.read_only:
                self.read_only.discard(item)
        for item in other.imports:
            if item not in self.imports:
                self.imports.append(item)
        for item in other.requires:
            if item not in self.requires:
                self.requires.append(item)
        for why in other.opaque:
            if why not in self.opaque:
                self.opaque.append(why)
        for why in other.control:
            self._control(why)

    def _hide(self, why: str) -> None:
        if why not in self.opaque:
            self.opaque.append(why)

    def _control(self, why: str) -> None:
        if why not in self.control:
            self.control.append(why)


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


#: Most arguments of one call read (a spawn with more is not judged by its head).
MAX_ARGS = 16
_NODE_PATH_JOIN = re.compile(
    r"(?:path|require\s*\(\s*['\"](?:node:)?path['\"]\s*\))\s*\.\s*(?:join|resolve)\s*(?=\()")


def _folded_at(code: str, index: int, language: str) -> tuple[str | None, int]:
    """A constant string starting at ``index``.  JavaScript also folds what is
    plainly constant (security review 7, M7-4): ``'a' + 'b'``, a template literal
    without ``${}``, ``path.join``/``path.resolve`` of literals."""
    value, end = _string_at(code, index, language)
    if language != "node":
        return value, end
    if value is None:
        start = _skip_space(code, index)
        joined = _NODE_PATH_JOIN.match(code, start)
        if joined is None:
            return None, index
        items, after = _list_at(code, joined.end(), language)
        if not items:
            return None, index
        value, end = posixpath.join(*items), after
    while True:
        plus = _skip_space(code, end)
        if code[plus:plus + 1] != "+" or code[plus:plus + 2] in ("++", "+="):
            return value, end
        more, after = _folded_at(code, plus + 1, language)
        if more is None:
            return value, end  # the caller sees the ``+`` that follows: computed
        return value + more, after


def _args(code: str, index: int, language: str) -> list[str | list[str] | None]:
    """The call's arguments from ``index`` (just after the name and an optional ``(``):
    each a string, a list of strings, or ``None`` (not a literal).  At most four."""
    out: list[str | list[str] | None] = []
    end = _call_end(code, index)
    j = index
    while j < end and len(out) < MAX_ARGS:
        j = _skip_space(code, j)
        if j >= end or code[j] in ");":
            break
        item: str | list[str] | None
        item, after = _folded_at(code, j, language)
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


def analyse(code: str, language: str, *, depth: int = 0, path: str | None = None) -> Finding:
    """What ``code`` in ``language`` would delete, move, write or run.  Never raises.

    Python is read as a syntax tree (:mod:`snowpea_core.security.pycode`);
    ``path`` is the script file the code came from (``__file__``).  The other
    languages go through their tables; a language with no table cannot be
    read at all (``code:language``).  Code past the size or findings bound is
    opaque rather than half judged.
    """
    found = Finding()
    try:
        if len(code) > MAX_SCRIPT_BYTES:
            found._hide("code:too-large")
        elif language == "python":
            from snowpea_core.security.pycode import analyse_python

            analyse_python(code, found, depth=depth, path=path)
            if len(found.commands) + len(found.argv) + len(found.lines) + len(
                    found.writes) > MAX_FINDINGS:
                found._hide("code:too-many")
        elif language in RULES:
            _analyse(code, language, depth, found)
        else:
            found._hide("code:language")
    except Exception:  # noqa: BLE001 - a bug here makes the code unseeable, never allowed
        found._hide("code:internal")
    return found


def _mask(text: str, language: str) -> tuple[str, bool]:
    """``text`` with the inside of every string literal blanked (same length), so a
    word inside a string (``print("unlink me")``) is never taken for a call; and
    whether every string closed on its own line (a quote that did not is either a
    multi-line string or a quote the masking misread)."""
    out = list(text)
    index = 0
    balanced = True
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
            # A JS template literal may run over several lines; the others end on theirs.
            multiline = char == "`"
            while j < len(text) and text[j] != char and (multiline or text[j] != "\n"):
                if text[j] == "\\":
                    out[j] = " "
                    j += 1
                    if j < len(text):
                        out[j] = " " if text[j] != "\n" else "\n"
                    j += 1
                    continue
                out[j] = " " if text[j] != "\n" else "\n"
                j += 1
            if j >= len(text) or text[j] != char:
                balanced = False
            index = j + 1
            continue
        index += 1
    return "".join(out), balanced


def _analyse(code: str, language: str, depth: int, found: Finding) -> None:
    rules = RULES[language]
    text = _strip_comments(code, language)
    masked, balanced = _mask(text, language)
    # Something the tables match in the text as written but not once its strings
    # are blanked sits inside what the masking took for a string.  That is a
    # word in a real string — unless the masking misread a quote (in a regex, a
    # comment, a here-document, a template expression): then it may be a call.
    hidden = False
    if language in ("perl", "ruby", "php"):
        hidden = _backticks(text, masked, language, found)
    seen = 0
    for rule in rules:
        if rule.needs is not None and not rule.needs.search(text):
            continue
        matches = list(rule.pattern.finditer(masked))
        if len(rule.pattern.findall(text)) > len(matches):
            hidden = True
        for match in matches:
            seen += 1
            if seen > MAX_FINDINGS:
                found._hide("code:too-many")
                return
            args = _args(text, match.end(), language)
            _judge(rule, args, language, depth, found, text, match)
    if hidden and (not balanced or _RISKY[language].search(text)):
        found._hide("code:unparsed")
    if language == "node":
        _node_extras(text, masked, found)
    for literal in _PATH_LITERAL.finditer(text):
        value = literal.group(2)
        if len(found.mentions) < MAX_FINDINGS * 4 and value not in found.mentions:
            found.mentions.append(value)


_RUNTIME_API = re.compile(r"(?<![\w$.])(?:Deno|Bun)\s*(?:\?\.|\.|\[)\s*(?!version\b|env\b|argv\b"
                          r"|main\b|inspect\b|sleep\b|nanoseconds\b|revision\b)")
#: Path-like string literals (for the control-plane check of what code names).
_PATH_LITERAL = re.compile(r"(['\"`])([^'\"`\n\\]*(?:/|snowpea)[^'\"`\n\\]*)\1")


def _node_extras(text: str, masked: str, found: Finding) -> None:
    """What no table of calls can follow in JavaScript (security review 6, H-3)."""
    # ``require(x)`` / ``import(x)`` with anything but one literal.
    for match in re.finditer(r"(?<![\w$.])(?:require|import)\s*\(", masked):
        value, end = _string_at(text, match.end(), "node")
        if value is None or text[_skip_space(text, end):][:1] != ")":
            found._hide("code:require:non-literal")
        elif value.startswith(("./", "../", "/")) and not value.endswith(".json") \
                and value not in found.requires:
            found.requires.append(value)  # the project's own file: read like a script
    for match in re.finditer(r"(?:^|[;\n])\s*import\s+(?:[^'\"`;\n]*?\sfrom\s+)?(['\"])(\.{0,2}/"
                             r"[^'\"\n]*)\1", text):
        if not match.group(2).endswith(".json") and match.group(2) not in found.requires:
            found.requires.append(match.group(2))
    if _NODE_REFLECT.search(masked):
        found._hide("code:reflect")
    if _RUNTIME_API.search(masked):
        # Deno's and Bun's own file and process calls: no table reads them
        # (security review 7, H7-2).
        found._hide("code:runtime-api")
    # A bracketed lookup on a module that carries dangerous calls.
    aliases = {"process"}
    for match in _NODE_MODULE_ALIAS.finditer(text):
        aliases.add(match.group(1) or match.group(2))
    lookup = re.compile(r"(?<![\w$.])(?:" + "|".join(re.escape(a) for a in sorted(aliases))
                        + r")\s*(?:\?\.)?\s*\[(?!\s*\d+\s*\])")
    direct = re.search(r"\brequire\s*\(\s*['\"`]" + _NODE_DANGEROUS_MODULE
                       + r"['\"`]\s*\)\s*(?:\?\.)?\s*\[", text)
    if direct or lookup.search(masked):
        found._hide("code:computed-member")
    reach = re.search(_NODE_FS, text) or re.search(_NODE_CP, text)
    if not reach:
        return
    if re.search(r"\[\s*(['\"`])(?:" + "|".join(_NODE_NAMES) + r")\1\s*\]", text):
        found._hide("code:computed-member")
    # Every other use of a dangerous name must be a call, or a destructuring
    # that keeps the name; an alias or a rename cannot be followed.
    plain: list[tuple[int, int]] = []
    renamed: list[tuple[int, int]] = []
    for match in _NODE_DESTRUCTURE.finditer(masked):
        group = 1 if match.group(1) is not None else 2
        body = match.group(group)
        keeps = ":" not in body and not re.search(r"\bas\b", body) and "..." not in body
        (plain if keeps else renamed).append(match.span(group))
    for match in _NODE_NAME.finditer(masked):
        at = match.start()
        if any(start <= at < end for start, end in renamed):
            found._hide("code:alias")  # ``const {rmSync: r} = require('fs')``
            return
        after = masked[match.end():match.end() + 8].lstrip()
        if after.startswith("(") or any(start <= at < end for start, end in plain):
            continue
        if after.startswith(":") and masked[max(0, at - 1):at] != ".":
            continue  # an option key (``{recursive: true}``), not the function
        found._hide("code:alias")  # ``const r = fs.rmSync``, ``run(fs.rm)``, ``.bind``…
        return


_BACKTICK = re.compile(r"`([^`]*)`|\bqx\s*([{(\[/|!])")
_CLOSE = {"{": "}", "(": ")", "[": "]", "/": "/", "|": "|", "!": "!"}


def _backticks(text: str, masked: str, language: str, found: Finding) -> bool:
    """Backticks and ``qx{}``/``%x{}`` outside string literals: a shell line.

    True when a backtick sits inside what the masking took for a string."""
    hidden = text.count("`") > masked.count("`")
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
    return hidden


def _judge(rule: _Rule, args: list[str | list[str] | None], language: str, depth: int,
           found: Finding, text: str, match: re.Match[str]) -> None:
    action = rule.action
    first = args[0] if args else None
    if action == "opaque-call":
        found._hide("code:native-binding")
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
    # Security review 6, M-1: a write or a copy whose target is not a literal
    # cannot be judged against any path.
    if action == "write":
        if language == "php" and "fopen" in match.group(0):
            mode = args[1] if len(args) > 1 else None
            if isinstance(mode, str) and not re.search(r"[wax+c]", mode):
                return  # opened to read
        target = first
        if language in ("perl", "ruby") and re.search(r"chmod|chown", match.group(0)):
            # ``chmod 0755, "x"``: the mode (and owner) come first, then the files.
            target = next((a for a in args[1:] if isinstance(a, str)), None)
        if isinstance(target, str):
            found.writes.append((target, False, False))
        else:
            found._hide("code:write:non-literal")
        return
    if action == "copy":
        if len(args) > 1 and isinstance(args[1], str):
            found.writes.append((args[1], False, True))
        else:
            found._hide("code:copy:non-literal")
        return
    if action == "link":
        # ``symlinkSync(target, path)``: ``path`` is written, ``target`` is linked to.
        if isinstance(first, str):
            found.links.append(first)
        if len(args) > 1 and isinstance(args[1], str):
            found.writes.append((args[1], False, False))
            found.linked.append(args[1])
        if not isinstance(first, str) or len(args) < 2 or not isinstance(args[1], str):
            found._hide("code:copy:non-literal")
        return
    if action == "open-node":
        flags = args[1] if len(args) > 1 else "r"
        if isinstance(flags, str) and flags in ("r", "rs", "sr"):
            if isinstance(first, str):
                found.reads.append(first)
            return
        if isinstance(first, str):
            found.writes.append((first, False, False))
        else:
            found._hide("code:write:non-literal")
        return
    if action == "opaque-eval":
        found._hide("code:eval:constructed")
        return
    if action == "worker":
        options = text[match.end():_call_end(text, match.end())]
        if re.search(r"\beval\s*:\s*(?!false\b)", options):
            found._hide("code:eval:worker")
        elif isinstance(first, str):
            if first.startswith(("./", "../", "/")) and first not in found.requires:
                found.requires.append(first)
        elif "worker_threads" in text or "Worker" in text[:match.start()]:
            found._hide("code:require:non-literal")


#: The word a computed argument of a spawned command is shown as.
UNKNOWN_WORD = "$__computed"


def _spawn(form: str, args: list[str | list[str] | None], found: Finding) -> None:
    """A spawn becomes the words it runs, walked like a shell command (security
    review 7, H7-4): the program decides what the call is, whatever the language."""
    first = args[0] if args else None
    if first is None or len(args) >= MAX_ARGS:
        found._hide("code:spawn:non-literal")
        return
    if isinstance(first, list):
        if first:
            found.argv.append([(word, False) for word in first])
        return
    if form == "exec":
        # A program, then its argv (``os.execv("/bin/rm", ["rm", "-rf", "/"])``,
        # ``spawn("rm", ["-rf", "/"])``).  Later positional arguments are options.
        rest = args[1:2]
        words: list[tuple[str, bool]] = [(first, False)]
        for item in rest:
            if isinstance(item, list):
                tail = item[1:] if item and Path(item[0]).name == Path(first).name else item
                words.extend((word, False) for word in tail)
            elif isinstance(item, str):
                words.append((item, False))
            else:
                # The program is known, its arguments are not: both are said.
                words.append((UNKNOWN_WORD, True))
                found._hide("code:spawn:non-literal")
        found.argv.append(words)
        return
    if form != "shell" and len(args) > 1:
        # ``system("rm", "-rf", "/")`` (perl, ruby): the words themselves.
        found.argv.append([(item, False) if isinstance(item, str) else (UNKNOWN_WORD, True)
                           for item in args])
        if any(not isinstance(item, str) for item in args):
            found._hide("code:spawn:non-literal")
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


def note_write(path: str | os.PathLike[str], session_id: str | None = None) -> None:
    """Remember that the agent wrote ``path`` (its script is then the agent's own).

    Kept for the whole session and across daemon restarts
    (:mod:`snowpea_core.security.written`; security review 6, M-8)."""
    written.note([os.fspath(path)], session_id)


def is_recent(path: str | os.PathLike[str]) -> bool:
    """``path`` is the agent's own: it wrote it (any time), or it changed just now."""
    try:
        real = os.path.realpath(os.fspath(path))
        info = os.stat(real)
    except (OSError, ValueError, TypeError):
        return True
    return (time.time() - max(info.st_mtime, info.st_ctime) < RECENT_SEC) or written.contains(real)


_SHEBANG = re.compile(r"^#!\s*(\S+)(?:\s+(\S+))?")
_SUFFIXES: dict[str, str] = {
    ".sh": "shell", ".bash": "shell", ".py": "python", ".js": "node", ".mjs": "node",
    ".cjs": "node", ".pl": "perl", ".rb": "ruby", ".php": "php",
    # Languages nothing here can read: a recent script in one is opaque.
    ".lua": "lua", ".r": "r", ".awk": "awk", ".sed": "sed", ".tcl": "tcl", ".jl": "julia",
    # TypeScript and JSX: the JavaScript tables match calls, and a type or a tag
    # hides none of them (security review 7, H7-2: bun and deno run these files).
    ".ts": "node", ".mts": "node", ".cts": "node", ".tsx": "node", ".jsx": "node",
    ".groovy": "groovy", ".swift": "swift",
    ".scala": "scala", ".ps1": "powershell", ".fish": "fish", ".applescript": "applescript",
}


def language_of(path: str, text: str | None, default: str) -> str:
    """The script's language: its ``#!`` line, else its suffix, else ``default``."""
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
            if program:
                return program  # an interpreter with no table (lua, awk, make…)
    suffix = Path(path).suffix.lower()
    return _SUFFIXES.get(suffix, default)


def read_script(raw: str, cwd: Path | None, *, default: str = "shell") -> Script | None:
    """The script ``raw`` names, read now (``None``: no file can be named from it).

    ``raw`` is a word as the shell passes it on: a ``$`` or a backtick in it is
    that character (security review 6, H-2 — such a name used to be skipped).
    """
    if not raw:
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
        written.contains(real)
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
    "is_recent",
    "language_of",
    "note_write",
    "read_script",
]
