"""The judgement: is this call blocked by the security lists?  Pure, never raises.

:func:`check_call` judges one tool call (snowpea's own, an external CLI's
bridged request, a Jarvis or worker call) and :func:`check` the
``security.check`` RPC's ``{command?, path?}``.  Both return a
:class:`Verdict`:

``blocked``
    A blocked command runs somewhere in the line, or the call writes (or, for
    a level ``all`` entry, reads) a protected path.  ``by`` names what
    (``commands:<preset>``, ``paths:<preset>``, ``custom``, or ``security``
    for an attempt to edit the lists themselves) and ``why`` says it in the
    person's language.
``opaque``
    Not blocked as far as can be seen, but part of the line cannot be seen
    (a program only the shell knows, ``eval`` of a variable, a pipe into a
    shell, a path only the shell knows) while a list is set: never allowed
    without the person (``reason``: :data:`UNCHECKED_REASON`).

Matching is on the parsed line (:mod:`snowpea_core.security.shell`).  Paths
are resolved against the call's folder (``cwd``/``workdir`` argument, else the
project); ``~``, ``$HOME``, ``$PWD``, ``$SNOWPEA_HOME`` and ``$TMPDIR`` are
expanded; symlinks are followed on both sides; a glob or a path with an
unknown tail is judged by every protected path it could reach.  A protected
path is never exempt for being inside the project.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import posixpath
import re
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from snowpea_core.jarvis import classify as _c
from snowpea_core.security import catalog, control_plane
from snowpea_core.security import patterns as pl
from snowpea_core.security import shell as sh
from snowpea_core.security.lists import Lists
from snowpea_core.util import pathcase

log = logging.getLogger("snowpea.security")

#: ``reason`` of a request that is put to the person because it cannot be checked.
UNCHECKED_REASON = "cannot be checked against your block list"
#: ``by`` of an attempt to change the security lists through a tool.
BY_SETTINGS = "security"

_MESSAGES: dict[str, dict[str, str]] = {
    "preset_command": {
        "en": "blocked command ({title}): {command}",
        "ko": "차단된 명령({title}): {command}",
    },
    "custom_command": {
        "en": "matches your blocked pattern “{pattern}”: {command}",
        "ko": "차단 패턴 “{pattern}”에 해당합니다: {command}",
    },
    "preset_path_write": {
        "en": "{path} is protected from changes ({title})",
        "ko": "{path} 은(는) 변경이 금지된 경로입니다({title})",
    },
    "preset_path_all": {
        "en": "{path} is protected from any access ({title})",
        "ko": "{path} 은(는) 접근이 금지된 경로입니다({title})",
    },
    "custom_path_write": {
        "en": "{path} is in your protected folders (no changes)",
        "ko": "{path} 은(는) 보호 폴더입니다(변경 금지)",
    },
    "custom_path_all": {
        "en": "{path} is in your protected folders (no access)",
        "ko": "{path} 은(는) 보호 폴더입니다(접근 금지)",
    },
    "ancestor": {
        "en": "{target} holds {path}, which is protected ({title})",
        "ko": "{target} 안에 보호된 경로 {path} 이(가) 있습니다({title})",
    },
    "settings": {
        "en": "the security lists can only be changed by you in Settings, never by a tool",
        "ko": "보안 목록은 설정 화면에서 직접만 바꿀 수 있고, 도구로는 바꿀 수 없습니다",
    },
    "unchecked": {
        "en": "snowpea cannot tell what this will touch before it runs, so it asks you",
        "ko": "snowpea가 실행 전에 무엇을 건드릴지 알 수 없어 확인합니다",
    },
    "custom_title": {"en": "your list", "ko": "내 목록"},
    "control_write": {
        "en": "{path} is one of snowpea's own control files ({what}); no tool changes it",
        "ko": "{path} 은(는) snowpea 자체 제어 파일입니다({what}). 도구로는 바꿀 수 없습니다",
    },
    "control_stray": {
        "en": "{path} is inside snowpea's data folder ($SNOWPEA_HOME). Register the folder "
              "as a project (open it in snowpea), or move it out of the data folder",
        "ko": "{path} 은(는) snowpea 데이터 폴더($SNOWPEA_HOME) 안에 있습니다. 이 폴더를 "
              "프로젝트로 등록하거나(snowpea 에서 열기) 데이터 폴더 밖으로 옮기세요",
    },
    "control_read": {
        "en": "{path} holds snowpea's own secrets ({what}); no tool reads it",
        "ko": "{path} 에는 snowpea 자체 비밀 정보가 있습니다({what}). 도구로는 읽을 수 없습니다",
    },
    "control_ask": {
        "en": "this changes snowpea's own files ({what}): only you can allow it",
        "ko": "snowpea 자체 파일({what})을 바꿉니다. 직접 허용해야 합니다",
    },
    "control_cli": {
        "en": "this runs snowpea's own CLI, which acts as you: only you can allow it",
        "ko": "snowpea 자체 CLI 를 실행합니다(사용자 본인으로 동작). 직접 허용해야 합니다",
    },
    "control_code": {
        "en": "this code uses snowpea's own modules, which act as you: only you can allow it",
        "ko": "snowpea 자체 모듈을 쓰는 코드입니다(사용자 본인으로 동작). 직접 허용해야 합니다",
    },
    "control_mention": {
        "en": "this code names one of snowpea's own control files ({what}): only you can "
              "allow it",
        "ko": "snowpea 자체 제어 파일({what})을 가리키는 코드입니다. 직접 허용해야 합니다",
    },
    "control_operand": {
        "en": "this hands one of snowpea's own control files ({what}) to a program that may "
              "change it: only you can allow it",
        "ko": "snowpea 자체 제어 파일({what})을 바꿀 수 있는 프로그램에 넘깁니다. "
              "직접 허용해야 합니다",
    },
    "unknown_code": {
        "en": "can't tell what this code changes (a target or a call it computes while it runs)",
        "ko": "이 코드가 무엇을 바꾸는지 알 수 없습니다(실행 중에 계산되는 대상이나 호출)",
    },
}

#: ``reason`` of a request put to the person because it acts on snowpea's own
#: controls (its CLI, its own files): asked in every mode, never by Jarvis,
#: never remembered, and no setting switches it off (security review 6, H-1).
CONTROL_REASON = "acts on snowpea's own controls"
#: ``reason`` when only code the check cannot resolve is in the way (review 6,
#: H-3 / M-1): what ``security.unknownCode: "allow"`` switches off.
UNKNOWN_CODE_REASON = "cannot tell what this code changes"
#: Every ``reason`` a verdict (and ``ApprovalRequest.reason``) may carry.
REASONS: tuple[str, ...] = (UNCHECKED_REASON, CONTROL_REASON, UNKNOWN_CODE_REASON)
#: ``security.unknownCode`` (m21, owner decision 2026-11-17): ``ask`` asks the
#: person whenever snowpea cannot tell what a call will touch; ``auto`` (the
#: default) lets such a call through in a session in auto mode and asks in plan
#: and accept; ``allow`` lets it through in every mode.  Only the reasons in
#: :data:`UNRESOLVED_REASONS` are ever let through this way.
UNKNOWN_CODE_VALUES: tuple[str, ...] = ("ask", "auto", "allow")
UNKNOWN_CODE_DEFAULT = "auto"


def message(key: str, locale: str | None, **values: Any) -> str:
    return catalog.pick(_MESSAGES[key], locale).format(**values)


#: ``uncheckedWhy`` texts (m21): one sentence per reason family, ``{x}`` the
#: name, path or program concerned.  The first family whose prefix matches wins.
_WHY_TEXTS: tuple[tuple[str, dict[str, str]], ...] = (
    ("unknown-path", {"en": "writes to `{x}` — the path is not known before it runs",
                      "ko": "`{x}`에 씀 — 실행 전에는 경로를 알 수 없음"}),
    ("unknown-read", {"en": "reads `{x}` — the path is not known before it runs",
                      "ko": "`{x}`를 읽음 — 실행 전에는 경로를 알 수 없음"}),
    ("dynamic-program", {"en": "the program is named by a variable (`{x}`)",
                         "ko": "프로그램 이름이 변수(`{x}`)로 정해짐"}),
    ("brace-program", {"en": "the program is spelled by a brace expansion (`{x}`)",
                       "ko": "프로그램 이름이 중괄호 확장(`{x}`)으로 정해짐"}),
    ("substitution", {"en": "the program comes out of a command substitution",
                      "ko": "프로그램 이름이 명령 치환의 결과로 정해짐"}),
    ("snowpea-cli", {"en": "a command that controls snowpea ({x})",
                     "ko": "snowpea를 제어하는 명령({x})"}),
    ("snowpea-code", {"en": "code that uses snowpea's own modules ({x})",
                      "ko": "snowpea 자체 모듈을 쓰는 코드({x})"}),
    ("control-plane", {"en": "touches snowpea's own files ({x})",
                       "ko": "snowpea 자체 파일을 건드림({x})"}),
    ("env-code:", {"en": "sets `{x}`, which a program it runs may read as code or settings",
                   "ko": "`{x}` 값을 정함 — 실행되는 프로그램이 코드나 설정으로 읽을 수 있음"}),
    ("env-path:", {"en": "sets `{x}`, where programs it runs look for code",
                   "ko": "`{x}` 값을 정함 — 실행되는 프로그램이 코드를 찾는 위치"}),
    ("script-written", {"en": "runs a script this same line writes first",
                        "ko": "같은 줄에서 먼저 쓴 스크립트를 실행함"}),
    ("script-", {"en": "runs `{x}`, which snowpea cannot read",
                 "ko": "snowpea가 읽을 수 없는 `{x}`를 실행함"}),
    ("code:", {"en": "code that computes a target or a call while it runs",
               "ko": "실행 중에 대상이나 호출을 계산하는 코드"}),
    ("eval", {"en": "runs text built while it runs (eval)",
              "ko": "실행 중에 만든 문자열을 실행함(eval)"}),
    ("source", {"en": "reads in a file it cannot see (source)",
                "ko": "볼 수 없는 파일을 불러옴(source)"}),
    ("shell-", {"en": "hands a shell commands snowpea cannot read ahead",
                "ko": "snowpea가 미리 읽을 수 없는 명령을 셸에 넘김"}),
    ("hard-linked", {"en": "writes a file that has another name too (`{x}`)",
                     "ko": "다른 이름도 가진 파일에 씀(`{x}`)"}),
    ("", {"en": "what this touches is not known before it runs",
          "ko": "실행 전에는 무엇을 건드릴지 알 수 없음"}),
)


def why_text(reason: str, subject: str, locale: str | None) -> str:
    """The plain sentence for one ``uncheckedWhy`` entry."""
    for head, texts in _WHY_TEXTS:
        if reason == head or reason.startswith(head if head.endswith((":", "-")) else
                                               head + ":") or not head:
            return catalog.pick(texts, locale).format(x=subject)
    return ""


def _shown_file(path: str, ctx: Context) -> str:
    """A script's path as the person reads it: relative to the project when inside it."""
    if ctx.workdir is not None:
        for root in (str(ctx.workdir), os.path.realpath(str(ctx.workdir))):
            if path.startswith(root.rstrip("/") + "/"):
                return path[len(root.rstrip("/")) + 1:]
    return path


def _unchecked_why(reasons: Iterable[str], view: sh.ShellView,
                   unknown: dict[str, sh.Effect], ctx: Context) -> tuple[dict[str, Any], ...]:
    """``uncheckedWhy`` for the reasons a verdict asks about (m21)."""
    out: list[dict[str, Any]] = []
    for reason in dict.fromkeys(reasons):
        where: dict[str, Any] | None = None
        subject = ""
        effect = unknown.get(reason)
        if effect is not None:
            where = effect.origin
            subject = effect.raw
        if where is None:
            where = view.details.get(reason)
        if where is None:
            where = {"part": _clip(view.texts[0]) if view.texts else ""}
        part = str(where.get("part") or "")
        subject = subject or str(where.get("subject") or "")
        if not subject:
            if reason.startswith(("env-code:", "env-path:")):
                subject = reason.split(":", 1)[1]
            elif reason.startswith("script-") and where.get("file") is None:
                subject = part.split()[0] if part.split() else ""
            elif reason.startswith("script-"):
                subject = str(where["file"])
            else:
                subject = part.split()[0] if part.split() else part
        if reason.startswith(("snowpea-", "control-plane")):
            head = part.split()
            subject = " ".join(head[:3]) + (" …" if len(head) > 3 else "")
        entry: dict[str, Any] = {"part": _clip(part)}
        if where.get("file"):
            entry["file"] = _shown_file(str(where["file"]), ctx)
            if isinstance(where.get("line"), int):
                entry["line"] = where["line"]
        entry["reason"] = reason
        entry["text"] = why_text(reason, subject, ctx.locale)
        out.append(entry)
    return tuple(out)


@dataclass(frozen=True)
class Verdict:
    blocked: bool = False
    #: ``commands:<id>``, ``paths:<id>``, ``custom`` or ``security``.
    by: str | None = None
    why: str | None = None
    #: Not blocked, but not all of it could be seen while a list is set.
    opaque: bool = False
    reason: str | None = None
    #: What matched: the command text or the protected path.
    match: str | None = None
    #: ``command`` | ``path`` | ``settings`` for a block.
    kind: str | None = None
    #: Why part of the line could not be seen (``dynamic-program``, ``unknown-path``…).
    unseen: tuple[str, ...] = ()
    #: For a protected path hit: the entry's level.
    level: str | None = None
    #: ``(path, digest)`` of every script file the call runs, as judged (H4): an
    #: answer to a question about it holds only while they are unchanged.
    scripts: tuple[tuple[str, str], ...] = ()
    #: Opaque only because snowpea cannot tell what the call will touch (every
    #: reason in :data:`UNRESOLVED_REASONS`, nothing on snowpea's controls):
    #: ``security.unknownCode: "auto"`` lets it through in a session in auto mode
    #: (:func:`asks_in`).  False for every other verdict.
    relaxable: bool = False
    #: m21 script grants: opaque ONLY because of what the content of the script
    #: files in :attr:`scripts` does (each reason in :data:`UNRESOLVED_REASONS`,
    #: none raised by the line itself, nothing on snowpea's controls): a person's
    #: allow may be remembered for exactly these files as they are now.
    script_grant: bool = False
    #: For an opaque verdict (m21, ``ApprovalRequest.uncheckedWhy``): what could
    #: not be checked, part by part — ``{"part", "file"?, "line"?, "reason", "text"}``.
    unchecked_why: tuple[dict[str, Any], ...] = field(default=(), compare=False, hash=False)

    def asks_in(self, mode: str | None) -> bool:
        """The verdict puts the call to the person in a session in ``mode``."""
        return self.opaque and not (self.relaxable and mode == "auto")

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"blocked": self.blocked, "by": self.by, "why": self.why,
                               "opaque": self.opaque}
        if self.reason:
            out["reason"] = self.reason
        if self.match:
            out["match"] = self.match
        if self.level:
            out["level"] = self.level
        return out

    @property
    def refusal(self) -> str:
        """What the model reads instead of a result."""
        return f"blocked by the security list: {self.by} — {self.why}"


ALLOWED = Verdict()


@dataclass
class Context:
    """Where a call runs and what the presets that depend on the machine stand for."""

    workdir: Path | None = None
    user_home: Path = field(default_factory=lambda: Path(os.path.expanduser("~")))
    home: Path | None = None
    projects: tuple[str, ...] = ()
    #: Mounted data disks; ``None`` reads them from the machine when needed.
    mounts: tuple[Path, ...] | None = None
    #: A virtual environment is active for the daemon (``VIRTUAL_ENV``/``CONDA_PREFIX``).
    env_venv: bool | None = None
    locale: str | None = None
    #: ``security.unknownCode``: ``ask`` (default) puts code the check cannot
    #: resolve to the person; ``allow`` lets it through (the control-plane rule
    #: and every blocked finding still apply).
    unknown_code: str = "ask"
    #: The call goes to a tool a connected host registered and runs itself (a
    #: browser's ``repl``), as the engine dispatches it — set by the caller from
    #: its registry, never from the tool's name or arguments.  Such a tool's
    #: ``code`` is the host's (the page's JavaScript) and is not read as local
    #: code unless the tool is tagged ``exec``; its paths are judged as usual.
    host_tool: bool = False

    def __post_init__(self) -> None:
        if self.env_venv is None:
            self.env_venv = bool(os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX"))

    # -- used by the catalog's predicates ---------------------------------------
    def variables(self, cwd: Path | None) -> dict[str, str]:
        out = {"HOME": str(self.user_home), "TMPDIR": tempfile.gettempdir()}
        if cwd is not None:
            out["PWD"] = str(cwd)
        if self.home is not None:
            out["SNOWPEA_HOME"] = str(self.home)
        return out

    def expand(self, word: str, cwd: Path | None) -> str | None:
        """``word`` as an absolute, normalised path (wildcards kept); ``None`` when unknown."""
        text = word.strip()
        if not text:
            return None
        if text.startswith("file://"):
            text = text[len("file://"):]
        variables = self.variables(cwd)

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            return variables.get(name, match.group(0))

        text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", substitute,
                      text)
        if text == "~" or text.startswith("~/"):
            text = str(self.user_home) + text[1:]
        elif text.startswith("~"):
            head, _, rest = text[1:].partition("/")
            if not re.fullmatch(r"[A-Za-z0-9._-]+", head):
                return None
            text = str(self.user_home.parent / head / rest)
        if "$" in text or "`" in text:
            return None
        if not text.startswith("/"):
            if cwd is None:
                return None
            text = str(cwd / text)
        normal = posixpath.normpath(text)
        if normal.startswith("//"):
            normal = "/" + normal.lstrip("/")
        return normal

    def critical_roots(self) -> list[Path]:
        """``/``, the home, and the parent of the project and of every known project:
        deleting one of them (or anything above) is catastrophic."""
        roots = [Path("/"), _real(self.user_home), Path(os.path.normpath(self.user_home))]
        for raw in (str(self.workdir) if self.workdir else None, *self.projects):
            if raw:
                for form in (_real(Path(raw)), Path(os.path.normpath(raw))):
                    if form.parent != form:
                        roots.append(form.parent)
        return list(dict.fromkeys(roots))

    def venv_active(self, cwd: Path | None) -> bool:
        if self.env_venv:
            return True
        for base in (cwd, self.workdir):
            if base is None:
                continue
            for name in (".venv", "venv", "env", ".env"):
                try:
                    if (base / name / "pyvenv.cfg").is_file():
                        return True
                except OSError:
                    continue
        return False


def _real(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.normpath(str(path.expanduser())))


# ---------------------------------------------------------------------------
# protected paths
# ---------------------------------------------------------------------------


def path_entries(lists: Lists, ctx: Context) -> list[catalog.PathEntry]:
    """Every protected path the lists stand for now, with symlinked forms twice."""
    out: list[catalog.PathEntry] = []
    for preset_id in lists.path_presets:
        try:
            out.extend(catalog.path_entries(
                preset_id, user_home=ctx.user_home, home=ctx.home, workdir=ctx.workdir,
                projects=ctx.projects, mounts=ctx.mounts,
            ))
        except Exception:  # noqa: BLE001 - a preset that cannot resolve protects what it can
            log.warning("security preset %s could not resolve", preset_id, exc_info=True)
    for raw, level in lists.paths:
        expanded = ctx.expand(raw, None)
        if expanded is None:
            continue
        written = Path(expanded)
        real = _real(written)
        for form in dict.fromkeys((real, written)):
            out.append(catalog.PathEntry(form, level, "tree", "custom",
                                         _MESSAGES["custom_title"], shown=raw))
    extra: list[catalog.PathEntry] = []
    for entry in out:
        if entry.by != "custom":
            real = _real(entry.path)
            if real != entry.path:
                extra.append(catalog.PathEntry(real, entry.level, entry.scope, entry.by,
                                               entry.title, entry.exempt, entry.shown))
    return out + extra


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _glob_parts(text: str) -> list[str]:
    return [part for part in text.split("/") if part]


def _part_match(want: str, got: str) -> bool:
    return want == "**" or fnmatch.fnmatchcase(got, want)


def _relations(pattern: list[str], open_end: bool, target: list[str]) -> set[str]:
    """Which of ``equal``/``under``/``ancestor`` a path the glob ``pattern`` stands for may
    have to ``target`` (both as parts).  ``open_end``: anything may follow the pattern."""
    found: set[str] = set()

    def walk(i: int, j: int) -> None:
        if len(found) == 3:
            return
        if i == len(pattern):
            if j == len(target):
                found.add("equal")
                if open_end:
                    found.add("under")
            else:
                found.add("ancestor")
                if open_end:
                    found.update(("equal", "under"))
            return
        if j == len(target):
            found.add("under")
            return
        if pattern[i] == "**":
            found.update(("equal", "under", "ancestor"))
            return
        if fnmatch.fnmatchcase(target[j], pattern[i]):
            walk(i + 1, j + 1)

    walk(0, 0)
    return found


@dataclass
class _Shape:
    """A path an effect may land on: concrete, or a pattern of parts."""

    concrete: tuple[Path, ...] = ()
    pattern: list[str] | None = None
    open_end: bool = False
    base: Path | None = None


def _shape(effect_raw: str, cwd: Path | None, ctx: Context) -> _Shape | None:
    """Where ``effect_raw`` lands; ``None`` when nothing at all is known."""
    expanded = ctx.expand(effect_raw, cwd)
    open_end = False
    if expanded is None:
        # A literal folder before the unknown part: it lands somewhere below it.
        text = effect_raw.strip()
        cut = min((i for i in (text.find("$"), text.find("`")) if i >= 0), default=-1)
        if cut <= 0:
            return None
        head = text[:cut]
        if "/" not in head:
            return None
        head = head.rsplit("/", 1)[0] or "/"
        expanded = ctx.expand(head, cwd)
        if expanded is None:
            return None
        open_end = True
    if open_end or re.search(r"[*?\[]", expanded):
        parts = _glob_parts(expanded)
        literal: list[str] = []
        for part in parts:
            if re.search(r"[*?\[]", part):
                break
            literal.append(part)
        base = _real(Path("/" + "/".join(literal)))
        rest = parts[len(literal):]
        pattern = _glob_parts(str(base)) + rest
        return _Shape(pattern=pattern, open_end=open_end, base=base)
    written = Path(expanded)
    return _Shape(concrete=tuple(dict.fromkeys((written, _real(written)))))


#: Case-insensitive file systems by default (security review 5, M5): macOS and
#: Windows compare names without case (and macOS without NFC/NFD differences).
_FOLD_CASE = sys.platform in ("darwin", "win32")


def _fold_text(text: str) -> str:
    return pathcase.fold_text(text)


def _fold(path: Path) -> Path:
    return Path(_fold_text(str(path)))


def _key(path: Path) -> Path:
    """``path`` as compared: folded where the file system ignores case."""
    return _fold(path) if _FOLD_CASE else path


def _folded(entry: catalog.PathEntry, shape: _Shape) -> tuple[catalog.PathEntry, _Shape]:
    entry = replace(entry, path=_fold(entry.path),
                    exempt=tuple(_fold(e) for e in entry.exempt))
    shape = _Shape(
        concrete=tuple(_fold(p) for p in shape.concrete),
        pattern=[_fold_text(p) for p in shape.pattern] if shape.pattern is not None else None,
        open_end=shape.open_end,
        base=_fold(shape.base) if shape.base is not None else None,
    )
    return entry, shape


def _effect_hits(effect: sh.Effect, entry: catalog.PathEntry, shape: _Shape) -> str | None:
    """``"self"`` (on or below the entry) / ``"ancestor"`` when ``effect`` breaks ``entry``."""
    if _FOLD_CASE:
        entry, shape = _folded(entry, shape)
    write = effect.kind == "write"
    if not write and entry.level != "all":
        return None
    reaching = effect.destructive or (write and effect.recursive)
    if shape.pattern is not None:
        relations = _relations(shape.pattern, shape.open_end, _glob_parts(str(entry.path)))
        exempt = shape.base is not None and any(_under(shape.base, e) for e in entry.exempt)
        if relations & {"equal", "under"} and not exempt:
            if entry.scope == "tree" or ("equal" in relations and (effect.destructive
                                                                 or not write)):
                return "self"
        if "ancestor" in relations:
            if write and (reaching or entry.scope == "tree" and shape.open_end):
                return "ancestor"
            if not write and effect.recursive:
                return "ancestor"
        return None
    for path in shape.concrete:
        if _under(path, entry.path):
            if any(_under(path, e) for e in entry.exempt):
                continue
            if entry.scope == "self":
                if path == entry.path and (effect.destructive or not write):
                    return "self"
                continue
            return "self"
        if _under(entry.path, path):
            if write and reaching:
                return "ancestor"
            if not write and effect.recursive:
                return "ancestor"
    return None


def _hard_linked(shape: _Shape) -> bool:
    """An existing regular file the effect writes has another name (``st_nlink > 1``)."""
    for path in shape.concrete[:1]:
        try:
            info = os.lstat(path)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return True
    return False


def _path_verdict(effects: Iterable[sh.Effect], entries: list[catalog.PathEntry],
                  ctx: Context, where: dict[str, sh.Effect] | None = None,
                  ) -> tuple[Verdict | None, list[str]]:
    """The first protected path an effect breaks, and why any effect could not be judged.
    ``where``: filled with the first effect behind each such reason (m21)."""
    unseen: list[str] = []
    if not entries:
        return None, unseen
    any_all = any(e.level == "all" for e in entries)
    for effect in effects:
        if effect.kind == "read" and not any_all:
            continue
        shape = _shape(effect.raw, effect.cwd, ctx)
        if shape is None:
            reason = "unknown-path" if effect.kind == "write" else "unknown-read"
            if reason not in unseen:
                unseen.append(reason)
                if where is not None:
                    where.setdefault(reason, effect)
            continue
        if effect.kind == "write" and _hard_linked(shape) and "hard-linked" not in unseen:
            # Security review 5, M6: a file with more than one name may be a
            # protected file under another name; which one cannot be seen.
            unseen.append("hard-linked")
            if where is not None:
                where.setdefault("hard-linked", effect)
        for entry in entries:
            hit = _effect_hits(effect, entry, shape)
            if hit is None:
                continue
            shown = entry.shown or str(entry.path)
            title = catalog.pick(entry.title, ctx.locale)
            if hit == "ancestor":
                target = effect.raw
                why = message("ancestor", ctx.locale, target=target, path=shown, title=title)
            else:
                key = ("custom_path_" if entry.by == "custom" else "preset_path_") + (
                    "all" if entry.level == "all" else "write")
                why = message(key, ctx.locale, path=shown, title=title)
            return Verdict(blocked=True, by=entry.by, why=why, match=shown, kind="path",
                           level=entry.level), unseen
    return None, unseen


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


@dataclass
class _Compiled:
    presets: list[catalog.CommandPreset]
    custom: list[pl.Pattern]


def _compile(lists: Lists) -> _Compiled:
    presets = [p for p in (catalog.command_preset(i) for i in lists.command_presets) if p]
    custom: list[pl.Pattern] = []
    for source in lists.patterns:
        try:
            custom.append(pl.compile_pattern(source))
        except pl.PatternError:
            continue
    return _Compiled(presets, custom)


def _subject(cmd: sh.Command, ctx: Context) -> pl.Subject:
    return pl.Subject(words=list(cmd.words), cwd=cmd.cwd, user_home=ctx.user_home,
                      extra_paths={f"${k}": v for k, v in ctx.variables(cmd.cwd).items()
                                   if k != "HOME"})


def _command_verdict(view: sh.ShellView, compiled: _Compiled, ctx: Context) -> Verdict | None:
    commands = [*view.commands, *view.literal_commands]
    for preset in compiled.presets:
        title = catalog.pick(preset.title, ctx.locale)
        by = f"commands:{preset.id}"
        for predicate in preset.line_predicates:
            try:
                hit = predicate(view, ctx)
            except Exception:  # noqa: BLE001 - a broken rule blocks rather than allows
                log.warning("security rule failed", exc_info=True)
                hit = True
            if hit:
                shown = view.texts[0] if view.texts else ""
                return Verdict(True, by, message("preset_command", ctx.locale, title=title,
                                                 command=_clip(shown)), match=_clip(shown),
                               kind="command")
        for cmd in commands:
            subject = _subject(cmd, ctx)
            hit = any(pl.matches(p, subject) for p in preset.compiled)
            if not hit:
                for predicate in preset.predicates:
                    try:
                        hit = bool(predicate(cmd, view, ctx))
                    except Exception:  # noqa: BLE001
                        log.warning("security rule failed", exc_info=True)
                        hit = True
                    if hit:
                        break
            if hit:
                return Verdict(True, by, message("preset_command", ctx.locale, title=title,
                                                 command=_clip(cmd.text)),
                               match=_clip(cmd.text), kind="command")
    for pattern in compiled.custom:
        for cmd in commands:
            if pl.matches(pattern, _subject(cmd, ctx)):
                return Verdict(True, "custom", message("custom_command", ctx.locale,
                                                       pattern=pattern.source,
                                                       command=_clip(cmd.text)),
                               match=pattern.source, kind="command")
    return None


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def check_command(command: str, lists: Lists, ctx: Context, cwd: Path | None = None) -> Verdict:
    """Judge one shell line run in ``cwd`` (default: the project).

    Even with empty lists the line is read: the control-plane rule is always on.
    """
    view = sh.view(command, cwd=str(cwd or ctx.workdir) if (cwd or ctx.workdir) else None,
                   home=str(ctx.home) if ctx.home else None, user_home=str(ctx.user_home),
                   projects=tuple(ctx.projects))
    return _judge_view(view, lists, ctx)


# -- the control plane (security review 6, H-1) -------------------------------------

_HOME_ITSELF = control_plane.ControlEntry("", "write", "snowpea's own folder")


def _control_hit(shape: _Shape, effect: sh.Effect,
                 home: Path) -> tuple[str, control_plane.ControlEntry, str] | None:
    """``(verdict, entry, shown path)`` when ``effect`` acts on a control file of ``home``."""
    write = effect.kind == "write"
    fold = _FOLD_CASE
    key = _fold(home) if fold else home
    settings_parts = control_plane.SETTINGS_PARTS
    if shape.pattern is None:
        access = "read" if not write else ("delete" if effect.destructive else "write")
        for path in shape.concrete:
            # Security review 9, F2: the file a linked settings.json points at.
            if control_plane.is_settings_target(path, home, fold=fold):
                hit = control_plane.judge_parts(settings_parts, access,
                                                recursive=effect.recursive, fold=fold)
                if hit is not None:
                    return hit[0], hit[1], control_plane.shown(home, settings_parts)
            folded = _fold(path) if fold else path
            if _under(folded, key):
                parts = path.parts[len(home.parts):]
                hit = control_plane.judge_parts(parts, access, recursive=effect.recursive,
                                                fold=fold, home=home)
                if hit is not None:
                    return hit[0], hit[1], control_plane.shown(home, parts)
            elif _under(key, folded) and write and effect.program != "git":
                # An ancestor of the home: deleting or moving it takes the control
                # files along; a recursive copy or extraction onto it may land on them.
                if effect.destructive:
                    return "blocked", _HOME_ITSELF, "$SNOWPEA_HOME"
                if effect.recursive:
                    return "ask", _HOME_ITSELF, "$SNOWPEA_HOME"
        return None
    pattern = [_fold_text(p) for p in shape.pattern] if fold else shape.pattern
    linked = control_plane.settings_target(home)
    if linked is not None and write:
        relations = _relations(pattern, shape.open_end,
                               _glob_parts(str(_fold(linked) if fold else linked)))
        entry = control_plane.entry_for(settings_parts, fold=fold)
        if entry is not None and ("equal" in relations or (
                "ancestor" in relations and effect.destructive and effect.program != "git")):
            return "blocked", entry, control_plane.shown(home, settings_parts)
    inside = shape.base is not None and _under(_fold(shape.base) if fold else shape.base, key)
    home_parts = _glob_parts(str(key))
    for entry in control_plane.CONTROL:
        if not write and entry.level != "all":
            continue
        for name in entry.names or (entry.rel,):
            target = home_parts + [_fold_text(p) if fold else p for p in name.split("/")]
            relations = _relations(pattern, shape.open_end, target)
            soft = "ask" if entry.level == "ask" else "blocked"
            shown = control_plane.shown(home, tuple(name.split("/")))
            if relations & {"equal", "under"}:
                return soft, entry, shown
            if "ancestor" not in relations:
                continue
            if write and effect.destructive and effect.program != "git":
                return soft, entry, shown
            if effect.recursive and inside:
                return soft, entry, shown
            if write and effect.recursive and effect.program != "git":
                return "ask", entry, shown
    return None


def _control_verdict(effects: Iterable[sh.Effect], ctx: Context, *,
                     tool: bool = False) -> tuple[Verdict | None, list[tuple[str, str]]]:
    """A block for an act on snowpea's control plane, and what must ask a person
    (``(unseen reason, why)``).  Always on: no list, preset or setting removes it."""
    asks: list[tuple[str, str]] = []
    homes = control_plane.homes(ctx.home, ctx.user_home)
    for effect in effects:
        shape = _shape(effect.raw, effect.cwd, ctx)
        if shape is None:
            continue
        external = _external_secret(shape, effect.kind == "write", ctx)
        if external is not None:
            return external, asks
        if effect.kind == "write":
            for path in shape.concrete:
                # Security review 8, H8-2: a link made anywhere in the home is a
                # second name for something there (or a way out of it).
                if effect.program == sh.LINK and any(_under(path, home) for home in homes):
                    asks.append(("control-plane:link", message(
                        "control_ask", ctx.locale, what="a link inside snowpea's own folder")))
                # M8-6: snowpea's own program files (the package, its Python, the launcher).
                if control_plane.install_root(path, tuple(ctx.projects or ()), ctx.workdir):
                    asks.append(("control-plane:install", message(
                        "control_ask", ctx.locale, what="snowpea's own program files")))
        for home in homes:
            hit = _control_hit(shape, effect, home)
            if hit is None:
                operand = _operand_hit(shape, effect, home)
                if operand is not None:
                    asks.append((f"control-plane:{operand.rel or 'home'}",
                                 message("control_operand", ctx.locale, what=operand.what)))
                    break
                continue
            verdict, entry, shown = hit
            write = effect.kind == "write"
            if tool and write and (entry.tool_edit or verdict == "ask"):
                # A file tool's write under the home already asks the person
                # (``config``); settings.json's lists are judged by _settings_tool.
                continue
            if verdict == "blocked":
                key = "control_write" if write else "control_read"
                if entry.stray:
                    # 0.2.90: a folder of the person's inside the home — say
                    # how to make it an ordinary place to work in.
                    key = "control_stray"
                return Verdict(True, BY_SETTINGS,
                               message(key, ctx.locale, path=shown, what=entry.what),
                               match=shown, kind="control", level=entry.level), asks
            asks.append((f"control-plane:{entry.rel or 'home'}",
                         message("control_ask", ctx.locale, what=entry.what)))
            break
    return None, asks


def _external_secret(shape: _Shape, write: bool, ctx: Context) -> Verdict | None:
    """A block when the path is the login of an external CLI in the user's home
    (security review 7, L7-2): one table with snowpea's own secrets."""
    for path in shape.concrete:
        entry = control_plane.external_secret(path, ctx.user_home)
        if entry is not None:
            shown = "~/" + entry.rel
            return Verdict(True, BY_SETTINGS,
                           message("control_write" if write else "control_read", ctx.locale,
                                   path=shown, what=entry.what),
                           match=shown, kind="control", level="all")
    return None


def _operand_hit(shape: _Shape, effect: sh.Effect,
                 home: Path) -> control_plane.ControlEntry | None:
    """A write-level control file named as an operand of a program that is not
    known to only read (security review 7, H7-5): ``sqlite3 <store> "INSERT …"``,
    ``ln -s <store> x``, ``X=<store>``.  What that program does with the file is
    not known here, so a person decides."""
    if effect.kind != "read" or effect.program in sh.READ_ONLY_PROGRAMS:
        return None
    hit = _control_hit(shape, sh.Effect(effect.raw, effect.cwd, "write"), home)
    if hit is None or hit[1].level not in ("write", "all") or hit[1] is _HOME_ITSELF:
        return None
    return hit[1]


_SHELL_META = re.compile(r"[;(){}<>|&\n]")
_GLOB = re.compile(r"[*?\[\]]")


def _mention_paths(raw: str) -> list[str]:
    """The paths one string literal of code names.  A literal that reads as a
    command line or a piece of code is not skipped: every path-like word in it
    counts (security review 7, H7-4)."""
    if not _SHELL_META.search(raw):
        return [raw]
    words = re.split(r"[\s;(){}<>|&=,'\"`]+", raw)
    return [word for word in words
            if word and ("/" in word or word.startswith("~")) and len(word) > 1]


def _mention_verdict(view: sh.ShellView, ctx: Context) -> tuple[Verdict | None,
                                                               list[tuple[str, str]]]:
    """Code that names a control file in a string: a secret is never read; another
    control file asks, unless EVERY use of that literal is a recognised read of
    exactly that expression (security review 7, H7-3)."""
    asks: list[tuple[str, str]] = []
    if not view.mentions:
        return None, asks
    homes = control_plane.homes(ctx.home, ctx.user_home)
    impure = {raw for raw, _cwd, read_only in view.mentions if not read_only}
    for literal, cwd, _read_only in view.mentions:
        for raw in _mention_paths(literal):
            # A pattern is judged where it is used; an unknown expansion names nothing.
            if _GLOB.search(raw) or ctx.expand(raw, cwd) is None:
                continue
            shape = _shape(raw, cwd, ctx)
            if shape is None:
                continue
            external = _external_secret(shape, False, ctx)
            if external is not None:
                return external, asks
            for home in homes:
                secret = _control_hit(shape, sh.Effect(raw, cwd, "read"), home)
                if secret is not None:
                    return Verdict(True, BY_SETTINGS,
                                   message("control_read", ctx.locale, path=secret[2],
                                           what=secret[1].what),
                                   match=secret[2], kind="control", level="all"), asks
                if literal not in impure:
                    continue
                hit = _control_hit(shape, sh.Effect(raw, cwd, "write"), home)
                if hit is not None:
                    asks.append((f"control-plane:{hit[1].rel or 'home'}",
                                 message("control_mention", ctx.locale, what=hit[1].what)))
                    break
    return None, asks


#: Opaque reasons that hold whatever the lists say (security review 7, M7-1): each
#: is a part of the line nobody read, where an act on snowpea's own controls may
#: sit.  With empty lists everything else that is opaque is let through.
_ALWAYS_UNSEEN: tuple[str, ...] = (
    "dynamic-program", "brace-program", "eval", "shell-stdin", "shell-body", "shell-c",
    "shell-string", "foreign-shell", "script-dynamic", "internal", "parse:", "env-code",
    "inline-code", "inline-dynamic", "interpreter-stdin", "nesting", "function", "trap",
    "source", "ssh-option", "deferred-run", "code:internal",
    # Security review 8, H8-3: git told to run a program, a repository whose hooks
    # or configuration the agent wrote, a write where git or PATH finds programs.
    "git-program", "git-hook", "git-control", "path-write",
)


def _always(why: str) -> bool:
    return any(why == head or why.startswith(head if head.endswith(":") else head + ":")
               for head in _ALWAYS_UNSEEN)


#: Reasons whose only meaning is "snowpea cannot tell before it runs what this
#: will touch" (m21): what ``security.unknownCode`` ``auto`` (auto mode) and
#: ``allow`` (every mode) stop asking about.  The call is then judged by what
#: the reader DID see — a blocked command, a protected path, snowpea's own
#: control files still stop it.  A name ending in ``:`` or ``-`` covers the
#: family (``code:write:non-literal``, ``script-missing``).  Anything not listed
#: (a reason added later included) keeps asking.
UNRESOLVED_REASONS: tuple[str, ...] = (
    # a path or a program only the shell knows
    "unknown-path", "unknown-read", "hard-linked", "dynamic-program", "brace-program",
    "substitution", "no-command", "stdin-args", "runs-commands", "test-subscript",
    # code nobody can read ahead: inline, piped, in a script, an interpreter's
    "code:", "eval", "shell-stdin", "shell-body", "shell-c", "shell-string",
    "foreign-shell", "remote-shell", "ssh-option", "script-", "inline-code",
    "inline-dynamic", "interpreter-stdin", "preload", "trap", "source", "encoded-payload",
    "npm-script", "make-file", "make-recipe", "deno-task", "awk-program", "awk-program-file",
    "awk-redirect", "sed-program", "sed-program-file", "db-shell", "db-script",
    "editor-commands", "pager-commands", "curl-config", "case", "select", "coproc",
    # a variable read by what runs next as code or configuration (not one the
    # check resolves paths with: see below)
    "env-code:", "env-path:",
)
#: Never let through by the switch, though their name matches above: the
#: reader itself failed (``code:internal``); a variable whose value the reader
#: resolves paths with (``HOME``, ``PWD``, ``TMPDIR``, ``SNOWPEA_HOME``,
#: ``XDG_*``) or whose name it cannot read — what it saw is then not what runs;
#: a script the same line writes before it runs it.  (``env-code:PATH`` too:
#: :func:`unresolved` — a PATH the reader cannot vouch for changes which program
#: each name on the line is.)
_NEVER_RELAXED: frozenset[str] = frozenset({"code:internal", "env-code:dynamic",
                                            "script-written"})
#: Unseen parts that can change the shell's own state (a builtin named by a
#: variable can be ``export HOME=…``): let through only when nothing in that
#: shell depends on that state (``~``, ``$HOME``, a relative path).
_STATEFUL_REASONS: frozenset[str] = sh.STATEFUL_REASONS


#: snowpea named as a program, a module, its home or its variables — not as a
#: folder name in a path (the owner's own checkout is ``…/snowpea/…``).
_SNOWPEA_WORD = re.compile(
    r"(?<![\w/.-])snowpea(?:[-_]\w+)?(?![\w/.-])|\.snowpea(?![\w-])|snowpea_core|SNOWPEA_",
    re.IGNORECASE)
#: Reasons where the PROGRAM that runs is not known (m21): its operands are
#: judged as the worst any program could do to them before it may run unasked.
_PROGRAM_UNKNOWN: frozenset[str] = frozenset({"dynamic-program", "brace-program", "eval",
                                              "substitution", "no-command"})


def _worst_case(view: sh.ShellView) -> list[sh.Effect]:
    """The literal operands of every program only the shell knows (``$X -rf ~``;
    the parser splits an unquoted ``` `echo rm` -rf / ``` into a pseudo-command
    whose first word is an option), as deletions of everything below them."""
    out: list[sh.Effect] = []
    pseudo = [cmd for cmd in [*view.commands, *view.literal_commands]
              if cmd.words and cmd.words[0].startswith("-")]
    for cmd in [*view.unknown_programs, *pseudo]:
        start = 0 if cmd in pseudo else 1
        for word, dyn in zip(cmd.words[start:], cmd.dynamic[start:], strict=False):
            if dyn or not word or (word.startswith("-") and "=" not in word):
                continue
            raw = word.split("=", 1)[1] if word.startswith("-") else word
            out.append(sh.Effect(raw, cmd.cwd, "write", destructive=True, recursive=True,
                                 program="dynamic"))
    return out


#: ``$NAME`` the reader resolves paths with: a path holding one depends on the
#: shell's state; any other ``$NAME`` is unknown already (``unknown-path``).
_STATE_NAMES = re.compile(r"\$\{?(?:HOME|PWD|OLDPWD|TMPDIR|SNOWPEA_HOME|XDG_[A-Z_]+)\b")


def _state_dependent(effects: Iterable[sh.Effect], shells: set[int]) -> bool:
    """A path the reader resolved through the shell's state (``~``, ``$HOME``, the
    current folder) in a shell where an unseen part may change that state."""
    for effect in effects:
        if effect.shell not in shells:
            continue
        raw = effect.raw.strip()
        if raw.startswith("~") or _STATE_NAMES.search(raw):
            return True
        if not raw.startswith(("/", "$", "`")):
            return True  # relative: the current folder
    return False


def unresolved(why: str, *, stateful: bool = False) -> bool:
    """``why`` only says the call's targets cannot be known ahead (:data:`UNRESOLVED_REASONS`).
    ``stateful``: the line has a path that depends on the shell's state."""
    from snowpea_core.security import envvars

    if why in _NEVER_RELAXED or why.startswith(("control-plane", "snowpea-")):
        return False
    if why.startswith("env-code:"):
        name = why.split(":", 1)[1]
        # The names the reader resolves paths and programs with: with another
        # value, what it saw (``~/x``, ``git``) is not what runs.
        if envvars.is_resolve(name) or name.upper() == "PATH":
            return False
    if stateful and why in _STATEFUL_REASONS:
        return False
    return any(why == head or (head.endswith((":", "-")) and why.startswith(head))
               for head in UNRESOLVED_REASONS)


def _judge_view(view: sh.ShellView, lists: Lists, ctx: Context,
                extra_effects: Iterable[sh.Effect] = (), *, tool: bool = False) -> Verdict:
    compiled = _compile(lists)
    if compiled.presets or compiled.custom:
        verdict = _command_verdict(view, compiled, ctx)
        if verdict is not None:
            return verdict
    effects = [*view.effects, *extra_effects]
    # A shell write to a settings file cannot be read; a file tool's content is
    # judged by _settings_tool (the lists compared before and after).
    settings_hit = _settings_effects(view.effects, ctx)
    if settings_hit is not None:
        return settings_hit
    unseen: list[str] = []
    entries = path_entries(lists, ctx) if not lists.paths_empty else []
    unknown: dict[str, sh.Effect] = {}
    verdict, path_unseen = _path_verdict(effects, entries, ctx, unknown)
    if verdict is not None:
        return verdict
    # snowpea's own control files: always on, whatever the lists hold (a path the
    # person's own lists protect too is reported as theirs, above).
    verdict, asks = _control_verdict(effects, ctx, tool=tool)
    if verdict is not None:
        return verdict
    verdict, named = _mention_verdict(view, ctx)
    if verdict is not None:
        return verdict
    asks.extend(named)
    for why in view.control:
        asks.append((why, message("control_code" if why == "snowpea-code" else "control_cli",
                                  ctx.locale)))
    if not lists.commands_empty:
        unseen.extend(view.opaque)
    elif not lists.empty:
        unseen.extend(why for why in view.opaque if why.startswith("code:") or _always(why))
    else:
        unseen.extend(why for why in view.opaque if _always(why))
    if entries:
        unseen.extend(path_unseen)
    # ``security.unknownCode`` (m21): what only says "cannot tell what this will
    # touch" (UNRESOLVED_REASONS) is let through under ``allow`` and, in a session
    # in auto mode, under ``auto`` (Verdict.relaxable).  Everything blocked above
    # stayed blocked; snowpea's own controls (asks), writes where git or PATH
    # finds programs (_program_places) and an unjudged call still ask below.
    stateful = _state_dependent(effects, view.stateful_shells)
    # A line (or a script it runs) that names snowpea anywhere — its CLI in a
    # variable's value, its module or its folder in a string — and has parts
    # snowpea cannot read is never let through unasked: what cannot be read may
    # be an act on snowpea's own controls.
    names_snowpea = any(_SNOWPEA_WORD.search(text) for text in view.texts)
    relax = set() if names_snowpea else {
        why for why in unseen if unresolved(why, stateful=stateful)}
    if relax & _PROGRAM_UNKNOWN:
        # ``$X -rf /``: the program is not known, what it is given is.  Every
        # literal word and path of the line, judged as the worst any program could
        # do to it (delete it with all below), must reach nothing protected and
        # nothing of snowpea's own, or the unknown program is not let through.
        worst = _worst_case(view)
        hit, _ = _path_verdict(worst, path_entries(lists, ctx), ctx)
        control, control_asks = _control_verdict(worst, ctx)
        if hit is not None or control is not None or control_asks:
            relax = set()
    if ctx.unknown_code == "allow":
        unseen = [why for why in unseen if why not in relax]
    unseen.extend(_program_places(effects, ctx))
    scripts = tuple(dict.fromkeys(view.scripts))
    _note_writes(effects, ctx)
    if asks:
        reasons = tuple(dict.fromkeys([why for why, _ in asks] + unseen))
        return Verdict(opaque=True, reason=CONTROL_REASON, why=asks[0][1], unseen=reasons,
                       scripts=scripts,
                       unchecked_why=_unchecked_why(reasons, view, unknown, ctx))
    if unseen:
        only_code = all(why.startswith("code:") for why in unseen)
        relaxable = ctx.unknown_code == "auto" and all(why in relax for why in unseen)
        # m21 script grants: every reason came from inside a script file the
        # reader hashed, none from the line itself, and each is one the switch
        # could let through (so never script-written, control-plane, snowpea).
        script_grant = bool(scripts) and _script_only(view, unseen, relax, entries,
                                                      extra_effects, ctx)
        reasons = tuple(dict.fromkeys(unseen))
        return Verdict(opaque=True,
                       reason=UNKNOWN_CODE_REASON if only_code else UNCHECKED_REASON,
                       why=message("unknown_code" if only_code else "unchecked", ctx.locale),
                       unseen=reasons, scripts=scripts,
                       relaxable=relaxable, script_grant=script_grant,
                       unchecked_why=_unchecked_why(reasons, view, unknown, ctx))
    return Verdict(scripts=scripts) if scripts else ALLOWED


def _script_only(view: sh.ShellView, unseen: list[str], relax: set[str],
                 entries: list[catalog.PathEntry], extra: Iterable[sh.Effect],
                 ctx: Context) -> bool:
    """m21 script grants: every reason in ``unseen`` is one the switch could let
    through and was raised only by the content of a script file the line runs."""
    in_file = {i for start, end in view.script_spans for i in range(start, end)}
    own = [effect for i, effect in enumerate(view.effects) if i not in in_file]
    _, own_unseen = _path_verdict([*own, *extra], entries, ctx)
    path_reasons = {why for why in unseen if why not in view.opaque}
    if own_unseen or (path_reasons and not in_file):
        return False
    return all(why in relax and _grantable(why) and why not in view.own_opaque
               and (why in view.script_opaque or why in path_reasons)
               for why in unseen)


#: m21 script grants: the reasons a grant may cover say only WHERE a script
#: writes or reads is not known.  Never that code or a program comes from
#: somewhere the grant does not hash (an eval, a pipe into a shell, a computed
#: program, a variable read as code, a module path): what such a script runs
#: could change while its own bytes do not.
_GRANTABLE: tuple[str, ...] = ("unknown-path", "unknown-read", "hard-linked", "test-subscript",
                               "code:write:", "code:copy:", "code:move:", "code:chdir")


def _grantable(why: str) -> bool:
    from snowpea_core.security import envvars

    if why.startswith("env-code:"):
        name = why.split(":", 1)[1]
        return name.upper() not in ("PATH", "DYNAMIC") and not envvars.is_code(name) \
            and not envvars.is_resolve(name)
    return any(why == head or (head.endswith(":") and why.startswith(head))
               for head in _GRANTABLE)


def _git_control(path: Path) -> bool:
    """``path`` is a repository's hooks folder (or in it) or its configuration."""
    parts = path.parts
    for index, part in enumerate(parts):
        if part.lower() != ".git":
            continue
        rest = tuple(p.lower() for p in parts[index + 1:])
        if rest[:1] == ("hooks",) or rest in (("config",), ("config.worktree",),
                                              ("info", "attributes")):
            return True
    return False


def _program_places(effects: Iterable[sh.Effect], ctx: Context) -> list[str]:
    """Security review 8, H8-3: writes into a repository's hooks or configuration
    (``git-control``) and into a folder on the daemon's ``PATH`` outside the
    project (``path-write``): what is put there runs later, unread."""
    found: list[str] = []
    owners = [Path(os.path.normpath(os.fspath(p))) for p in (*ctx.projects, ctx.workdir) if p]
    dirs = {Path(os.path.normpath(entry))
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and os.path.isabs(entry)}
    for effect in effects:
        if effect.kind != "write":
            continue
        shape = _shape(effect.raw, effect.cwd, ctx)
        if shape is None:
            continue
        for path in shape.concrete:
            if _git_control(path) and "git-control" not in found:
                found.append("git-control")
            folder = path if path in dirs else path.parent
            if folder in dirs and not any(_under(folder, owner) for owner in owners) \
                    and "path-write" not in found:
                found.append("path-write")
    return found


def _note_writes(effects: Iterable[sh.Effect], ctx: Context) -> None:
    """Files a call that is let through writes: a script among them is the agent's own
    (H4 b: then what cannot be read of it is opaque, however old its timestamp)."""
    from snowpea_core.security.code import note_write

    for effect in effects:
        if effect.kind == "write" and not effect.dynamic:
            expanded = ctx.expand(effect.raw, effect.cwd)
            if expanded is not None and not re.search(r"[*?\[]", expanded):
                note_write(expanded)


def scripts_changed(verdict: Verdict) -> list[str]:
    """The scripts ``verdict`` judged whose content is not what it judged any more."""
    from snowpea_core.security.code import digest_now

    return [path for path, digest in verdict.scripts if digest_now(path) != digest]


def check_path(path: str, lists: Lists, ctx: Context, *, write: bool = True) -> Verdict:
    """Judge a write (or, ``write=False``, a read) of ``path``: the "try it" box."""
    effect = sh.Effect(raw=path, cwd=ctx.workdir, kind="write" if write else "read",
                       destructive=write, recursive=True)
    control, _asks = _control_verdict([effect], ctx)
    if control is not None:
        return control
    if lists.paths_empty:
        return ALLOWED
    verdict, unseen = _path_verdict([effect], path_entries(lists, ctx), ctx)
    if verdict is not None:
        return verdict
    if unseen:
        return Verdict(opaque=True, reason=UNCHECKED_REASON, why=message("unchecked", ctx.locale),
                       unseen=tuple(unseen))
    return ALLOWED


# -- tool calls ------------------------------------------------------------------

_DESTROY_WORDS = re.compile(r"(^|[_.\-])(delete|remove|rm|trash|unlink|rmdir|purge|wipe|erase|"
                            r"move|rename|mv)([_.\-]|$)", re.IGNORECASE)
_WRITE_WORDS = re.compile(r"(^|[_.\-])(write|edit|create|copy|save|put|mkdir|append|patch|"
                          r"upload|replace|insert|chmod|chown|touch|truncate|extract|unzip)"
                          r"([_.\-]|$)", re.IGNORECASE)
_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$|"
                           r"^(?:\+\+\+|---) (?:[ab]/)?(\S.*?)(?:\t.*)?$", re.MULTILINE)
_SETTINGS_KEYS = re.compile(r"security|blockedCommands|protectedPaths", re.IGNORECASE)


def _tool_cwd(args: Mapping[str, Any], ctx: Context) -> Path | None:
    raw = args.get("cwd") or args.get("workdir") or args.get("dir")
    if isinstance(raw, str) and raw.strip():
        expanded = ctx.expand(raw, ctx.workdir)
        return Path(expanded) if expanded else None
    return ctx.workdir


def _patch_paths(text: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for match in _PATCH_HEADER.finditer(text):
        if match.group(2):
            out.append((match.group(2).strip(), match.group(1) == "Delete"))
        elif match.group(3):
            out.append((match.group(3).strip(), False))
        elif match.group(4) and match.group(4).strip() != "/dev/null":
            out.append((match.group(4).strip(), False))
    return out


def tool_effects(tool: str, args: Mapping[str, Any], tags: Iterable[str],
                 ctx: Context) -> list[sh.Effect]:
    """The paths a non-shell tool call writes or reads."""
    lower = (tool or "").lower()
    base = lower.split("__")[-1] if lower.startswith(_c.MCP_PREFIX) else lower
    tagset = set(tags)
    cwd = _tool_cwd(args, ctx)
    destroys = bool(_DESTROY_WORDS.search(base))
    writes = (
        bool(tagset & {"write", "outside", "config"})
        or base in _c.WRITE_TOOL_NAMES
        or destroys
        or bool(_WRITE_WORDS.search(base))
    )
    recursive = base in _c.RECURSIVE_READ_TOOL_NAMES
    out: list[sh.Effect] = []
    paths = _c._call_paths(args)
    if recursive and not writes and not paths and cwd is not None:
        paths = [str(cwd)]
    for raw in paths:
        out.append(sh.Effect(raw=raw, cwd=cwd, kind="write" if writes else "read",
                             destructive=destroys, recursive=recursive or destroys,
                             program="tool"))
    for key in ("input", "patch", "diff"):
        value = args.get(key)
        if isinstance(value, str) and ("*** " in value or "+++ " in value):
            for raw, deleted in _patch_paths(value):
                out.append(sh.Effect(raw=raw, cwd=cwd, kind="write", destructive=deleted))
    return out


def _settings_files(ctx: Context) -> list[Path]:
    found: list[Path] = []
    if ctx.home is not None:
        found.append(_real(ctx.home / "settings.json"))
    for root in (str(ctx.workdir) if ctx.workdir else None, *ctx.projects):
        if root:
            found.append(_real(Path(root) / ".snowpea" / "settings.json"))
    return list(dict.fromkeys(found))


def _settings_effects(effects: Iterable[sh.Effect], ctx: Context) -> Verdict | None:
    """A shell write that lands on a snowpea settings file may rewrite the lists."""
    files = _settings_files(ctx)
    if not files:
        return None
    for effect in effects:
        if effect.kind != "write":
            continue
        shape = _shape(effect.raw, effect.cwd, ctx)
        if shape is None:
            continue
        for target in files:
            # The file itself, or its own folder (``.snowpea/``, the home) deleted,
            # moved or rewritten as a tree; deleting a whole project is not judged here.
            if shape.pattern is not None:
                relations = _relations(shape.pattern, shape.open_end, _glob_parts(str(target)))
                last = shape.pattern[-1] if shape.pattern else ""
                named = shape.open_end or last == "**" or fnmatch.fnmatchcase(target.name, last)
                hit = ("equal" in relations and named) or (
                    "ancestor" in relations and (effect.destructive or effect.recursive)
                    and len(shape.pattern) >= len(_glob_parts(str(target))) - 1)
            else:
                # Security review 6, M-7: the same file under another case is the
                # same file on macOS and Windows.
                same, parent = _key(target), _key(target.parent)
                hit = any(_key(p) == same or (_key(p) == parent and (effect.destructive
                                                                     or effect.recursive))
                          for p in shape.concrete)
            if hit:
                return Verdict(True, BY_SETTINGS, message("settings", ctx.locale),
                               match=str(target), kind="settings")
    return None


def _security_of(text: str) -> Any:
    """The file's ``security`` section (``None`` when absent or empty; ``...`` unreadable)."""
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return ...
    if not isinstance(data, dict):
        return ...
    return data.get("security") or None


def _settings_tool(tool: str, args: Mapping[str, Any], ctx: Context) -> Verdict | None:
    """``settings_set`` touching ``security``; a file tool editing a settings file's lists."""
    lower = (tool or "").lower()
    if lower in ("settings_set", "settings.set", "settings_update"):
        patch = args.get("patch")
        text = json.dumps(patch) if not isinstance(patch, str) else patch
        if (isinstance(patch, Mapping) and "security" in patch) or (
            not isinstance(patch, Mapping) and _SETTINGS_KEYS.search(text or "")
        ) or any(isinstance(k, str) and k.startswith("security") for k in args):
            return Verdict(True, BY_SETTINGS, message("settings", ctx.locale), kind="settings")
        return None
    files = _settings_files(ctx)
    if not files:
        return None
    cwd = _tool_cwd(args, ctx)
    for raw in _c._call_paths(args):
        expanded = ctx.expand(raw, cwd)
        if expanded is None:
            continue
        target = _real(Path(expanded))
        if _key(target) not in {_key(known) for known in files}:
            continue
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            current = "{}"
        after_text = _new_content(lower, args, current, target, ctx, cwd)
        before = _security_of(current)
        after = _security_of(after_text) if after_text is not None else ...
        # The file read back: an unparseable result loads as the defaults, which
        # would drop the person's own entries.
        if after is ... or (before is not ... and before != after) or (
            before is ... and after is not None
        ):
            return Verdict(True, BY_SETTINGS, message("settings", ctx.locale),
                           match=str(target), kind="settings")
    return None


def _new_content(lower: str, args: Mapping[str, Any], current: str, target: Path,
                 ctx: Context, cwd: Path | None) -> str | None:
    """What a file tool leaves in ``target``; ``None`` when that cannot be known."""
    if _DESTROY_WORDS.search(lower):
        return None
    for key in ("content", "contents", "text", "file_text"):
        if isinstance(args.get(key), str) and not any(
            k in args for k in ("old_string", "edits", "oldText", "old_str")
        ):
            return str(args[key])
    edits: list[Mapping[str, Any]] = []
    if any(k in args for k in ("old_string", "old", "oldText", "old_str")):
        edits.append(args)
    raw_edits = args.get("edits")
    if isinstance(raw_edits, list):
        edits.extend(e for e in raw_edits if isinstance(e, Mapping))
    if not edits:
        return None
    text = current
    top = args.get("path") or args.get("file_path")
    for edit in edits:
        where = edit.get("path") or edit.get("file_path") or top
        if isinstance(where, str):
            expanded = ctx.expand(where, cwd)
            if expanded is not None and _real(Path(expanded)) != target:
                continue
        old = next((edit[k] for k in ("old_string", "old", "oldText", "old_str")
                    if isinstance(edit.get(k), str)), None)
        new = next((edit[k] for k in ("new_string", "new", "newText", "new_str")
                    if isinstance(edit.get(k), str)), None)
        if old is None or new is None or (old and old not in text):
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def check_call(
    tool: str,
    args: Mapping[str, Any] | None,
    lists: Lists,
    ctx: Context,
    *,
    tags: Iterable[str] = (),
) -> Verdict:
    """Judge one tool call against ``lists``.  Pure apart from resolving paths; never raises."""
    try:
        return _check_call(tool, dict(args or {}), lists, ctx, tuple(tags))
    except Exception:  # noqa: BLE001 - a bug here must not let the call through unchecked
        log.exception("security check failed for %s", tool)
        # Security review 7, M7-1: a call nobody judged is never allowed, with or
        # without lists — the control-plane rule was not applied to it either.
        return Verdict(opaque=True, reason=UNCHECKED_REASON, why=message("unchecked", ctx.locale),
                       unseen=("internal",))


def _check_call(tool: str, args: dict[str, Any], lists: Lists, ctx: Context,
                tags: tuple[str, ...]) -> Verdict:
    lower = (tool or "").lower()
    base = lower.split("__")[-1] if lower.startswith(_c.MCP_PREFIX) else lower
    # A tool known to only read (its tags say so, or its name is a read tool's)
    # does not edit the lists: showing settings.json stays an ordinary read.
    reads_only = bool(tags) and set(tags) <= {"read", "secret"} or base in _c.READ_TOOL_NAMES
    settings = None if reads_only else _settings_tool(tool, args, ctx)
    if settings is not None:
        return settings
    # Even with empty lists a shell line is read: one that rewrites a settings
    # file may change the lists themselves.
    command = _c._command_text(args)
    is_shell = lower in _c.SHELL_TOOL_NAMES or lower.startswith("process_start") or (
        command is not None and "exec" in tags
    )
    extra = tool_effects(tool, args, tags, ctx) if not is_shell else []
    if is_shell and command is not None:
        cwd = _tool_cwd(args, ctx)
        view = sh.view(command, cwd=str(cwd) if cwd else None,
                       home=str(ctx.home) if ctx.home else None, user_home=str(ctx.user_home),
                       projects=tuple(ctx.projects))
        return _judge_view(view, lists, ctx)
    code = args.get("code") if isinstance(args.get("code"), str) else None
    if ctx.host_tool and "exec" not in tags:
        # The host runs it (a browser page's script), not this machine: the
        # host judges it.  Its path-like arguments are still judged below.
        code = None
    elif code is None and lower in _c.CODE_TOOL_NAMES:
        code = next((v for v in args.values() if isinstance(v, str)), None)
    view = sh.ShellView()
    if code:
        language = args.get("language") or args.get("lang") or "python"
        view = sh.code_view(code, cwd=str(ctx.workdir) if ctx.workdir else None,
                            home=str(ctx.home) if ctx.home else None,
                            user_home=str(ctx.user_home),
                            language=language if isinstance(language, str) else "python")
    # Even with empty lists: the control-plane rule judges every tool's paths.
    return _judge_view(view, lists, ctx, extra_effects=extra, tool=not code)


__all__ = [
    "ALLOWED",
    "BY_SETTINGS",
    "CONTROL_REASON",
    "REASONS",
    "UNCHECKED_REASON",
    "UNKNOWN_CODE_REASON",
    "UNKNOWN_CODE_VALUES",
    "Context",
    "Verdict",
    "check_call",
    "check_command",
    "check_path",
    "message",
    "path_entries",
    "tool_effects",
]
