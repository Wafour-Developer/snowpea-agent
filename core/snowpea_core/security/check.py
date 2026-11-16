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
        "en": "cannot be checked against your block list",
        "ko": "차단 목록과 대조할 수 없습니다",
    },
    "custom_title": {"en": "your list", "ko": "내 목록"},
    "control_write": {
        "en": "{path} is one of snowpea's own control files ({what}); no tool changes it",
        "ko": "{path} 은(는) snowpea 자체 제어 파일입니다({what}). 도구로는 바꿀 수 없습니다",
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
UNKNOWN_CODE_VALUES: tuple[str, ...] = ("ask", "allow")


def message(key: str, locale: str | None, **values: Any) -> str:
    return catalog.pick(_MESSAGES[key], locale).format(**values)


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
                  ctx: Context) -> tuple[Verdict | None, list[str]]:
    """The first protected path an effect breaks, and why any effect could not be judged."""
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
            continue
        if effect.kind == "write" and _hard_linked(shape) and "hard-linked" not in unseen:
            # Security review 5, M6: a file with more than one name may be a
            # protected file under another name; which one cannot be seen.
            unseen.append("hard-linked")
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
                   home=str(ctx.home) if ctx.home else None, user_home=str(ctx.user_home))
    return _judge_view(view, lists, ctx)


# -- the control plane (security review 6, H-1) -------------------------------------

_HOME_ITSELF = control_plane.ControlEntry("", "write", "snowpea's own folder")


def _control_hit(shape: _Shape, effect: sh.Effect,
                 home: Path) -> tuple[str, control_plane.ControlEntry, str] | None:
    """``(verdict, entry, shown path)`` when ``effect`` acts on a control file of ``home``."""
    write = effect.kind == "write"
    fold = _FOLD_CASE
    key = _fold(home) if fold else home
    if shape.pattern is None:
        access = "read" if not write else ("delete" if effect.destructive else "write")
        for path in shape.concrete:
            folded = _fold(path) if fold else path
            if _under(folded, key):
                parts = path.parts[len(home.parts):]
                hit = control_plane.judge_parts(parts, access, recursive=effect.recursive,
                                                fold=fold)
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
        for home in homes:
            hit = _control_hit(shape, effect, home)
            if hit is None:
                continue
            verdict, entry, shown = hit
            write = effect.kind == "write"
            if tool and write and (entry.tool_edit or verdict == "ask"):
                # A file tool's write under the home already asks the person
                # (``config``); settings.json's lists are judged by _settings_tool.
                continue
            if verdict == "blocked":
                key = "control_write" if write else "control_read"
                return Verdict(True, BY_SETTINGS,
                               message(key, ctx.locale, path=shown, what=entry.what),
                               match=shown, kind="control", level=entry.level), asks
            asks.append((f"control-plane:{entry.rel or 'home'}",
                         message("control_ask", ctx.locale, what=entry.what)))
            break
    return None, asks


def _mention_verdict(view: sh.ShellView, ctx: Context) -> tuple[Verdict | None,
                                                               list[tuple[str, str]]]:
    """Code that names a control file in a string: a secret is never read; another
    control file asks, unless the code plainly only opens it to read."""
    asks: list[tuple[str, str]] = []
    if not view.mentions:
        return None, asks
    homes = control_plane.homes(ctx.home, ctx.user_home)
    read_only = {effect.raw for effect in view.effects
                 if effect.kind == "read" and effect.program == "code"}
    for raw, cwd in view.mentions:
        # Only a literal that reads as one path: not a pattern (judged where it is
        # used), not a piece of code or a command line, not an unknown expansion.
        if re.search(r"[*?\[\];(){}<>|&\n]", raw) or ctx.expand(raw, cwd) is None:
            continue
        shape = _shape(raw, cwd, ctx)
        if shape is None:
            continue
        for home in homes:
            secret = _control_hit(shape, sh.Effect(raw, cwd, "read"), home)
            if secret is not None:
                return Verdict(True, BY_SETTINGS,
                               message("control_read", ctx.locale, path=secret[2],
                                       what=secret[1].what),
                               match=secret[2], kind="control", level="all"), asks
            if raw in read_only:
                continue
            hit = _control_hit(shape, sh.Effect(raw, cwd, "write"), home)
            if hit is not None:
                asks.append((f"control-plane:{hit[1].rel or 'home'}",
                             message("control_mention", ctx.locale, what=hit[1].what)))
                break
    return None, asks


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
    verdict, path_unseen = _path_verdict(effects, entries, ctx)
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
        unseen.extend(why for why in view.opaque if why.startswith("code:"))
    if ctx.unknown_code == "allow":
        # ``security.unknownCode: "allow"``: code the check cannot resolve is let
        # through.  Everything blocked above stayed blocked, and what acts on
        # snowpea's own controls still asks below.
        unseen = [why for why in unseen if not why.startswith("code:")]
    if entries:
        unseen.extend(path_unseen)
    scripts = tuple(dict.fromkeys(view.scripts))
    _note_writes(effects, ctx)
    if asks:
        reasons = tuple(dict.fromkeys([why for why, _ in asks] + unseen))
        return Verdict(opaque=True, reason=CONTROL_REASON, why=asks[0][1], unseen=reasons,
                       scripts=scripts)
    if unseen:
        only_code = all(why.startswith("code:") for why in unseen)
        return Verdict(opaque=True,
                       reason=UNKNOWN_CODE_REASON if only_code else UNCHECKED_REASON,
                       why=message("unknown_code" if only_code else "unchecked", ctx.locale),
                       unseen=tuple(dict.fromkeys(unseen)), scripts=scripts)
    return Verdict(scripts=scripts) if scripts else ALLOWED


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
                             destructive=destroys, recursive=recursive or destroys))
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
        if lists.empty:
            return ALLOWED
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
                       home=str(ctx.home) if ctx.home else None, user_home=str(ctx.user_home))
        return _judge_view(view, lists, ctx)
    code = args.get("code") if isinstance(args.get("code"), str) else None
    if code is None and lower in _c.CODE_TOOL_NAMES:
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
