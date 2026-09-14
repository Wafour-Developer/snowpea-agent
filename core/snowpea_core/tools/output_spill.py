"""Head/tail trimming for long tool output (M15 §A4).

A ten-thousand-line ``shell`` result is paid for on every later turn of the
conversation, and the model almost always needs the first and the last few
dozen lines.  :func:`spill` keeps those, writes the whole thing to
``$SNOWPEA_HOME/cache/tool-output/<id>.txt`` and leaves one pointer line in the
middle telling the agent exactly how to read the rest back.

Ported in shape from Hermes' delegation-report spill (MIT); see
``docs/design/deviations/CORE-policies.md``.  Exported so delegation (M15 §C5)
reuses the same helper rather than growing a second one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from snowpea_core.config.paths import resolve_home

log = logging.getLogger("snowpea.tools.spill")

#: Default split of a trimmed result between its head and its tail.
DEFAULT_HEAD_LINES = 300
DEFAULT_TAIL_LINES = 100

#: Spill files older than this are swept on the next write.
MAX_SPILL_FILES = 200


@dataclass(frozen=True)
class Spilled:
    """What :func:`spill` produced."""

    text: str
    #: Where the full output was written; ``None`` when nothing was trimmed.
    path: Path | None = None
    #: Lines replaced by the pointer line; ``0`` when nothing was trimmed.
    omitted: int = 0

    @property
    def trimmed(self) -> bool:
        return self.path is not None


def spill_dir(home: Path | str | None = None) -> Path:
    """``$SNOWPEA_HOME/cache/tool-output`` (created on first use)."""
    return resolve_home(home) / "cache" / "tool-output"


def _sweep(directory: Path) -> None:
    """Keep the directory bounded; a full disk must never fail a tool call."""
    try:
        files = sorted(directory.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    except OSError:  # pragma: no cover - racing sweep
        return
    for stale in files[: max(0, len(files) - MAX_SPILL_FILES)]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover
            pass


def spill(
    text: str,
    *,
    head_lines: int = DEFAULT_HEAD_LINES,
    tail_lines: int = DEFAULT_TAIL_LINES,
    kind: str = "output",
    home: Path | str | None = None,
) -> Spilled:
    """Trim ``text`` to a head and a tail, pointing at the full copy on disk.

    Returns the text unchanged when it already fits, or when the full copy
    could not be written — a pointer to a file that does not exist would be
    worse than a long result.
    """
    lines = text.splitlines()
    keep = max(1, head_lines) + max(0, tail_lines)
    if len(lines) <= keep:
        return Spilled(text=text)

    directory = spill_dir(home)
    target = directory / f"{kind}-{uuid.uuid4().hex[:12]}.txt"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _sweep(directory)
    except OSError as exc:  # pragma: no cover - disk full / read-only home
        log.debug("could not spill %s output: %s", kind, exc)
        return Spilled(text=text)

    head = lines[: max(1, head_lines)]
    tail = lines[len(lines) - max(0, tail_lines) :] if tail_lines > 0 else []
    omitted = len(lines) - len(head) - len(tail)
    pointer = (
        f"[… {omitted} lines omitted — "
        f'read_file("{target}", offset={len(head) + 1}, limit={omitted})]'
    )
    return Spilled(text="\n".join([*head, pointer, *tail]), path=target, omitted=omitted)


#: Tools whose results the agent loop runs through :func:`spill`.
SPILLED_TOOLS: frozenset[str] = frozenset({"shell", "grep", "glob", "list_dir"})


def max_result_lines(core: object) -> int:
    """``tools.maxResultLines`` (default 400), never below 20."""
    tools = getattr(getattr(core, "settings", None), "tools", None)
    try:
        value = int(getattr(tools, "maxResultLines", 400))
    except (TypeError, ValueError):
        value = 400
    return max(20, value)


__all__ = [
    "DEFAULT_HEAD_LINES",
    "DEFAULT_TAIL_LINES",
    "MAX_SPILL_FILES",
    "SPILLED_TOOLS",
    "Spilled",
    "max_result_lines",
    "spill",
    "spill_dir",
]
