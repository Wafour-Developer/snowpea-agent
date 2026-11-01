"""Files a ``shell`` command changed, so they count as edits (1.35.0).

A heredoc into a file, ``tee``, ``sed -i`` or a script that writes one is how a
small model edits when ``patch`` feels heavy.  Those edits used to skip the
language server's ``Diagnostics`` block and the verify-on-stop gate, which only
watched the edit tools.  Rather than parse every way a shell can write, this
looks at what the command *names* and what actually changed: a file the
command text mentions, inside the working directory, whose mtime moved while
the command ran.  A false positive costs one diagnostics lookup; a false
negative is the old behaviour.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.tools.registry import ToolContext, ToolResult

log = logging.getLogger(__name__)

#: Words of a command that can name a file: they hold a dot or a slash.
_WORD = re.compile(r"""[^\s'"`=:;|&<>(){}\[\],$*?!#]+""")

#: Most files one command may report, and how many get a diagnostics lookup.
MAX_FILES = 8
MAX_DIAGNOSED = 3

#: Directories whose files a command touches without editing anything.
_SKIP_PARTS = frozenset({".git", "node_modules", "__pycache__", ".venv", "dist", "build"})

#: Slack for coarse filesystem timestamps.
MTIME_SLACK_SEC = 1.0


def changed_files(command: str, workdir: Path | str, started: float) -> list[str]:
    """Workdir-relative files ``command`` names that changed since ``started``."""
    root = Path(workdir).resolve()
    found: list[str] = []
    for word in dict.fromkeys(_WORD.findall(command or "")):
        if "." not in word and "/" not in word:
            continue
        if word.startswith("~"):
            continue
        try:
            path = Path(word)
            full = path if path.is_absolute() else root / path
            full = full.resolve()
            rel = full.relative_to(root)
            if not full.is_file() or _SKIP_PARTS & set(rel.parts):
                continue
            if full.stat().st_mtime < started - MTIME_SLACK_SEC:
                continue
        except (OSError, ValueError):
            continue
        text = rel.as_posix()
        if text not in found:
            found.append(text)
        if len(found) >= MAX_FILES:
            break
    return found


async def annotate(
    ctx: ToolContext, args: dict[str, Any], result: ToolResult, started: float
) -> ToolResult:
    """Record the files a successful shell command wrote and append their diagnostics."""
    if not result.ok:
        return result
    try:
        from snowpea_core.exec.local import LocalBackend

        if not isinstance(ctx.session.backend, LocalBackend):
            return result
        files = changed_files(str(args.get("command") or ""), ctx.session.workdir, started)
        if not files:
            return result
        result.meta = {**(result.meta or {}), "wrote": files}
        from snowpea_core import lsp

        blocks = [
            block
            for block in [await lsp.diagnostics_block(ctx, name) for name in files[:MAX_DIAGNOSED]]
            if block
        ]
        if blocks:
            tail = "\n\n".join(blocks)
            result.output = f"{result.output}\n\n{tail}" if result.output else tail
    except Exception:  # noqa: BLE001 - a lookup must never fail the command
        log.debug("could not look at the files a shell command wrote", exc_info=True)
    return result


__all__ = ["annotate", "changed_files"]
