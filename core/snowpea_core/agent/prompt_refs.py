"""Resolve ``@path`` references and bare image paths in user prompts."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from snowpea_core.attachments import pending
from snowpea_core.attachments.model import (
    Attachment,
    AttachmentError,
    is_image,
    is_text,
    sniff_mime,
)
from snowpea_core.attachments.store import AttachmentStore
from snowpea_core.tools import file_state
from snowpea_core.tools.fs import format_numbered_lines

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.protocol import Attachment as WireAttachment
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.agent.prompt_refs")

Scope = Literal["any", "workdir"]

#: Image extensions recognised for ``@`` refs and bare-path auto-attach.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

#: Characters of one referenced text file inlined into the prompt.
MAX_INLINE_PER_FILE = 60_000
#: Characters of all inlined text across one prompt.
MAX_INLINE_PER_PROMPT = 200_000
#: Directory entries shown for an ``@dir`` reference.
MAX_DIR_ENTRIES = 200
#: Files larger than this are not read into the prompt (non-images).
MAX_READ_BYTES = 2_000_000
#: Bytes read to sniff MIME before committing to a full read.
SNIFF_BYTES = 8192

#: Characters stripped from the end of a path candidate during trailing trim.
_TRIM_CHARS = ".,;:)!?]}>'\""
#: Wrapper characters stripped from bare-path tokens.
_WRAPPER_CHARS = "'\"()<>[]"
#: ``@`` reference: not after ``\`` or a word character.
_REF_RE = re.compile(
    r'(?<![\w\\])@(?:"([^"]+)"(?:\:(\d+)(?:-(\d+))?)?|([^\s@]+))'
)
_RANGE_RE = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")
_SECRET_DIR_PARTS = frozenset({".ssh", ".gnupg", ".aws"})


@dataclass(frozen=True)
class PromptRef:
    """One ``@`` token found in a prompt."""

    raw: str
    path: str
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class PreparedPrompt:
    """Typed prompt text, model-facing text, and resolved reference metadata."""

    text: str
    model_text: str
    refs: list[dict[str, Any]] = field(default_factory=list)


def _display_path(resolved: Path, workdir: Path) -> str:
    try:
        if resolved.is_relative_to(workdir):
            return str(resolved.relative_to(workdir))
    except ValueError:
        pass
    return str(resolved)


def _ref_entry(
    resolved: Path | None,
    workdir: Path,
    *,
    kind: str,
    lines: int | None = None,
    truncated: bool = False,
    raw: str | None = None,
) -> dict[str, Any]:
    if resolved is not None:
        path = _display_path(resolved, workdir)
    elif raw:
        path = raw
    else:
        path = ""
    entry: dict[str, Any] = {"path": path, "kind": kind, "truncated": truncated}
    if lines is not None:
        entry["lines"] = lines
    return entry


def is_secret_pattern(resolved: Path) -> bool:
    """True when the path itself says "credential": .ssh/.gnupg/.aws, .env*, keys."""
    parts = resolved.parts
    if any(part in _SECRET_DIR_PARTS for part in parts):
        return True
    name = resolved.name
    lower = name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if lower.endswith(".pem") or lower.endswith(".key"):
        return True
    return name.startswith("id_rsa") or name.startswith("id_ed25519")


def _is_secret_path(resolved: Path, home: Path) -> bool:
    """True when ``resolved`` must never be inlined or attached."""
    if is_secret_pattern(resolved):
        return True
    home_resolved = home.resolve()
    try:
        resolved.relative_to(home_resolved)
    except ValueError:
        return False
    attachments = home_resolved / "attachments"
    try:
        resolved.relative_to(attachments)
        return False
    except ValueError:
        return True


def _in_workdir(resolved: Path, workdir: Path) -> bool:
    try:
        resolved.relative_to(workdir.resolve())
        return True
    except ValueError:
        return False


def _trimmable_char(ch: str) -> bool:
    return ch in _TRIM_CHARS or ord(ch) > 127


def _strip_wrappers(token: str) -> str:
    text = token.strip()
    if text.startswith("file://"):
        return text[7:]
    while len(text) >= 2 and text[0] in _WRAPPER_CHARS and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text.strip(_WRAPPER_CHARS)


def _strip_trailing_ascii(path: str) -> str:
    while path and path[-1] in _TRIM_CHARS and ord(path[-1]) <= 127:
        path = path[:-1]
    return path


def _split_line_range(path: str) -> tuple[str, int | None, int | None]:
    cleaned = _strip_trailing_ascii(path)
    ranged = _RANGE_RE.match(cleaned)
    if ranged is None:
        return cleaned, None, None
    start = int(ranged.group(2))
    end = int(ranged.group(3)) if ranged.group(3) else start
    return ranged.group(1), start, end


def _resolve_existing(
    raw: str, workdir: Path
) -> tuple[Path | None, str, int | None, int | None]:
    """Trailing-trim ``raw`` until a path exists; return path, trimmed text, line range."""
    candidate = raw.strip()
    if candidate.startswith("file://"):
        candidate = candidate[7:]
    original = candidate
    while candidate:
        path_part, start, end = _split_line_range(candidate)
        resolved = resolve_path(workdir, path_part)
        if resolved is not None and resolved.exists():
            return resolved, path_part, start, end
        if not _trimmable_char(candidate[-1]):
            break
        candidate = candidate[:-1]
    path_part, start, end = _split_line_range(original)
    return None, path_part, start, end


def find_refs(text: str) -> list[PromptRef]:
    """Every ``@`` reference in ``text``, in document order."""
    refs: list[PromptRef] = []
    for match in _REF_RE.finditer(text):
        if match.group(1) is not None:
            path = match.group(1)
            start = int(match.group(2)) if match.group(2) else None
            end = int(match.group(3)) if match.group(3) else start
        else:
            path, start, end = _split_line_range(match.group(4) or "")
        if not path:
            continue
        refs.append(PromptRef(raw=match.group(0), path=path, line_start=start, line_end=end))
    return refs


def _bare_tokens(text: str) -> list[str]:
    """Whitespace tokens that keep quoted segments intact."""
    return re.findall(r'"[^"]+"|\'[^\']+\'|\S+', text)


def find_bare_image_paths(text: str, workdir: Path) -> list[str]:
    """Image paths written without ``@``, deduplicated in first-seen order."""
    seen: set[str] = set()
    paths: list[str] = []
    for token in _bare_tokens(text):
        candidate = _strip_wrappers(token)
        if not candidate:
            continue
        resolved, path_part, _, _ = _resolve_existing(candidate, workdir)
        if resolved is None or not resolved.is_file():
            continue
        if resolved.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            paths.append(path_part)
    return paths


def resolve_path(workdir: Path, raw: str) -> Path | None:
    """Resolve ``raw`` relative to ``workdir``, or as an absolute/``~/`` path."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("file://"):
        text = text[7:]
    expanded = Path(text).expanduser()
    try:
        if expanded.is_absolute():
            return expanded.resolve()
        return (workdir / expanded).resolve()
    except OSError:
        return None


def _auto_attach_enabled(core: Core) -> bool:
    agent = getattr(getattr(core, "settings", None), "agent", None)
    return bool(getattr(agent, "autoAttachImages", True))


def _inline_text_block(path: str, body: str, *, truncated: bool, note: str = "") -> str:
    suffix = note or (
        f"\n… [truncated; use read_file for the rest of {path}]" if truncated else ""
    )
    return f"[file: {path}]\n{body}{suffix}"


def _truncate_body(body: str, budget: int, *, path: str) -> tuple[str, bool, str]:
    """Cut at the last newline at or before ``budget``; return body, truncated, note."""
    if len(body) <= budget:
        return body, False, ""
    cut = body.rfind("\n", 0, budget)
    if cut <= 0:
        cut = budget
    clipped = body[:cut]
    lines_before = clipped.count("\n") + (1 if clipped else 0)
    total_lines = body.count("\n") + (1 if body else 0)
    note = (
        f"\n… [truncated at line {lines_before} of {total_lines}; "
        f"use read_file path={path} offset={lines_before + 1} for the rest]"
    )
    return clipped, True, note


def _read_text_file(
    resolved: Path,
    ref: PromptRef | None,
    *,
    budget: int,
    display_path: str,
) -> tuple[str, bool, int | None]:
    """``(numbered body, complete, line_count)`` capped at ``budget`` characters."""
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"[binary file: {resolved.name}]", True, None
    lines = content.splitlines(keepends=True)
    total_lines = len(lines) if lines else 0
    line_count: int | None = None
    start = ref.line_start if ref and ref.line_start else 1
    if ref and ref.line_start:
        end = ref.line_end or ref.line_start
        window = lines[start - 1 : end]
        body = format_numbered_lines("".join(window), start_line=start)
        complete = start == 1 and end >= len(lines)
        line_count = end - start + 1
    else:
        body = format_numbered_lines(content, start_line=1)
        complete = True
        line_count = total_lines or None
    truncated = False
    note = ""
    if len(body) > budget:
        body, truncated, note = _truncate_body(body, budget, path=display_path)
        complete = False
    if note:
        body = body + note
    return body, complete and not truncated, line_count


def _list_directory(resolved: Path) -> str:
    entries: list[str] = []
    try:
        children = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f"[directory: {resolved.name}, unreadable: {exc}]"
    for child in children[:MAX_DIR_ENTRIES]:
        if child.is_dir():
            entries.append(f"{child.name}/")
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append(f"{child.name} ({size} bytes)")
    extra = ""
    if len(children) > MAX_DIR_ENTRIES:
        extra = f"\n… [{len(children) - MAX_DIR_ENTRIES} more entries]"
    header = f"[directory: {resolved}]"
    return header + ("\n" + "\n".join(entries) if entries else " (empty)") + extra


def resolve_prompt_refs(
    core: Core,
    session: Session,
    text: str,
    *,
    scope: Scope = "any",
) -> tuple[list[Attachment], str, list[dict[str, Any]]]:
    """Turn ``@`` refs (and optional bare images) into attachments and inline text."""
    workdir = Path(session.workdir).resolve()
    home = core.paths.home
    attachments: list[Attachment] = []
    inline_parts: list[str] = []
    refs_meta: list[dict[str, Any]] = []
    inline_budget = MAX_INLINE_PER_PROMPT
    seen_paths: set[str] = set()

    def _consume_path(raw: str, ref: PromptRef | None) -> None:
        nonlocal inline_budget
        resolved, path_part, line_start, line_end = _resolve_existing(raw, workdir)
        key = path_part.strip()
        if key in seen_paths:
            return
        if resolved is None:
            return
        if ref is None and (line_start is not None or line_end is not None):
            ref = PromptRef(
                raw=raw, path=path_part, line_start=line_start, line_end=line_end
            )
        elif ref is not None and (line_start is not None or line_end is not None):
            ref = PromptRef(
                raw=ref.raw,
                path=path_part,
                line_start=line_start,
                line_end=line_end,
            )
        if scope == "workdir" and not _in_workdir(resolved, workdir):
            refs_meta.append(_ref_entry(resolved, workdir, kind="refused", raw=raw))
            seen_paths.add(key)
            return
        if _is_secret_path(resolved, home):
            refs_meta.append(_ref_entry(resolved, workdir, kind="refused", raw=raw))
            seen_paths.add(key)
            return
        seen_paths.add(key)
        display = _display_path(resolved, workdir)
        try:
            if resolved.is_dir():
                block = _list_directory(resolved)
                if inline_budget <= 0:
                    return
                clipped = block[:inline_budget]
                inline_parts.append(
                    _inline_text_block(
                        display, clipped, truncated=len(block) > len(clipped)
                    )
                )
                inline_budget -= len(clipped)
                refs_meta.append(
                    _ref_entry(resolved, workdir, kind="dir", lines=None, truncated=False)
                )
                return
            suffix = resolved.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                try:
                    attachments.append(Attachment.from_path(resolved))
                    refs_meta.append(_ref_entry(resolved, workdir, kind="image"))
                except AttachmentError as exc:
                    log.info("skipping @ref image %s: %s", raw, exc)
                return
            try:
                st = resolved.stat()
            except OSError as exc:
                log.debug("could not stat @ref %s: %s", raw, exc)
                return
            if st.st_size > MAX_READ_BYTES and suffix not in IMAGE_SUFFIXES:
                marker = (
                    f"[file too large to inline: {resolved.name}, {st.st_size} bytes — "
                    "use read_file with a line range]"
                )
                inline_parts.append(marker)
                refs_meta.append(
                    _ref_entry(resolved, workdir, kind="binary", lines=None, truncated=False)
                )
                return
            with open(resolved, "rb") as fh:
                head = fh.read(SNIFF_BYTES)
            mime = sniff_mime(head, resolved.name)
            if is_image(mime):
                try:
                    attachments.append(Attachment.from_path(resolved, mime=mime))
                    refs_meta.append(_ref_entry(resolved, workdir, kind="image"))
                except AttachmentError as exc:
                    log.info("skipping @ref image %s: %s", raw, exc)
                return
            if is_text(mime) or mime == "application/json":
                per_file = min(MAX_INLINE_PER_FILE, inline_budget)
                if per_file <= 0:
                    return
                body, complete, line_count = _read_text_file(
                    resolved, ref, budget=per_file, display_path=display
                )
                inline_parts.append(
                    _inline_text_block(display, body, truncated=not complete)
                )
                inline_budget -= len(body)
                refs_meta.append(
                    _ref_entry(
                        resolved,
                        workdir,
                        kind="file",
                        lines=line_count,
                        truncated=not complete,
                    )
                )
                if complete:
                    file_state.note_read(core, session, str(resolved), body, complete=True)
                return
            inline_parts.append(
                f"[binary file: {resolved.name}, {st.st_size} bytes, {mime}]"
            )
            refs_meta.append(_ref_entry(resolved, workdir, kind="binary"))
        except OSError as exc:
            log.debug("could not resolve @ref %s: %s", raw, exc)

    for ref in find_refs(text):
        _consume_path(ref.path, ref)

    if _auto_attach_enabled(core):
        for raw in find_bare_image_paths(text, workdir):
            _consume_path(raw, None)

    inline_text = "\n\n".join(inline_parts)
    return attachments, inline_text, refs_meta


def prepare_prompt(
    core: Core,
    session: Session,
    text: str,
    wire_attachments: Sequence[WireAttachment] | None,
    *,
    accept_wire: Any,
    scope: Scope = "any",
) -> PreparedPrompt:
    """Single entry for every prompt: wire attachments, ``@`` refs, bare images.

    ``accept_wire`` is ``session_handlers._accept_attachments`` — passed in to
    avoid a circular import while keeping one stash site.
    """
    stored, inline_wire = accept_wire(core, session.id, wire_attachments)
    ref_attachments, inline_refs, refs = resolve_prompt_refs(
        core, session, text, scope=scope
    )
    if ref_attachments:
        store = AttachmentStore(core.paths.attachments_dir)
        saved = [store.save(session.id, item) for item in ref_attachments]
        pending.stash(session.id, stored + saved)
    typed = "\n\n".join(part for part in (text, inline_wire) if part) if inline_wire else text
    if inline_refs:
        model_text = f"{typed}\n\n{inline_refs}" if typed else inline_refs
    else:
        model_text = typed
    return PreparedPrompt(text=typed, model_text=model_text, refs=refs)


__all__ = [
    "IMAGE_SUFFIXES",
    "PreparedPrompt",
    "PromptRef",
    "Scope",
    "find_bare_image_paths",
    "find_refs",
    "prepare_prompt",
    "resolve_path",
    "resolve_prompt_refs",
]
