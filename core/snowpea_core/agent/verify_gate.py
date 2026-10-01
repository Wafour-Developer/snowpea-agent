"""Verify-on-stop: a turn that edited code does not end on an unchecked claim.

Ported from Hermes (``agent/verification_stop.py`` + the evidence ledger in
``agent/verification_evidence.py``), narrowed to one turn:

* every successful edit (``patch``, ``write_file`` …) of a file with runtime
  behaviour is noted, documentation and prose are not;
* a later ``shell`` command that is a test, lint, typecheck or build — or an
  ``execute_code`` run, Hermes's "ad-hoc verification" — is evidence, passing
  or failing, and an edit after it makes it stale;
* an edit to a UI file (``.vue``, ``.tsx``, ``.html``, ``.css`` …) also needs a
  look in the browser after it: a passing API test does not show a page
  renders (this part is snowpea's; Hermes leaves UI to skills);
* a verifier delegation that reports PASS counts for both.

When the model ends the turn without that evidence, the loop appends a
bounded follow-up (at most :data:`MAX_NUDGES` per turn) instead of finishing.
It never runs a check itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

#: Follow-ups one turn may get; a model that cannot verify says why instead.
MAX_NUDGES = 2

#: Tools whose successful result means a file changed.
EDIT_TOOLS = frozenset({"patch", "write_file", "edit_file", "multi_edit", "apply_patch"})

#: Edits with nothing to run: documentation, prose and plain data.
NON_CODE_SUFFIXES = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".adoc", ".org", ".log",
     ".csv", ".tsv"}
)
NON_CODE_NAMES = frozenset(
    {"license", "licence", "notice", "authors", "contributors", "changelog", "codeowners"}
)

#: Edits a person looks at: verified in a browser, not only by a test.
UI_SUFFIXES = frozenset(
    {".vue", ".svelte", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss", ".sass",
     ".less", ".astro"}
)

#: Browser tools whose successful call is a look at the page.
BROWSER_LOOKS = frozenset(
    {"browser_navigate", "browser_snapshot", "browser_screenshot", "browser_vision",
     "browser_console", "browser_click", "browser_type"}
)

#: A shell command that checks the code: tests, linters, type checkers, builds.
_CHECK = re.compile(
    r"(?:^|[\s;&|(/])(?:"
    r"pytest|py\.test|tox|nox|mypy|pyright|ruff|flake8|pylint|unittest"
    r"|tsc|vue-tsc|eslint|biome|vitest|jest|mocha|playwright|cypress"
    r"|cargo\s+(?:test|build|check|clippy)|go\s+(?:test|build|vet)"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|typecheck|type-check|check|e2e)\b"
    r"|npx\s+(?:tsc|vitest|jest|playwright|eslint)|make\s+(?:test|check|build|lint)"
    r"|gradle\w*\s+(?:test|build|check)|mvn\w*\s+(?:test|verify|package)"
    r"|dotnet\s+(?:test|build)|swift\s+(?:test|build)|deno\s+(?:test|check)"
    r"|node\s+--test|phpunit|rspec|ctest|vite\s+build|next\s+build"
    r")(?:\b|$)"
)


def is_check_command(command: str) -> bool:
    """True when ``command`` runs tests, a linter, a type checker or a build."""
    return bool(_CHECK.search(command or ""))


def _kind(path: str) -> str | None:
    """``"ui"``, ``"code"`` or ``None`` (nothing to verify) for an edited path."""
    pure = PurePath(path)
    suffix = pure.suffix.lower()
    if suffix in NON_CODE_SUFFIXES or (not suffix and pure.name.lower() in NON_CODE_NAMES):
        return None
    return "ui" if suffix in UI_SUFFIXES else "code"


@dataclass
class TurnEvidence:
    """What one turn changed and what it ran since, in tool-call order."""

    step: int = 0
    #: path -> step of its last edit
    code_edits: dict[str, int] = field(default_factory=dict)
    ui_edits: dict[str, int] = field(default_factory=dict)
    #: (step, ok, command) of the last check
    last_check: tuple[int, bool, str] | None = None
    last_look: int = -1
    nudges: int = 0
    todo_nudged: bool = False


def begin_turn(session: Session) -> None:
    session.verify_turn = TurnEvidence()


def observe(session: Session, name: str, args: dict[str, Any], result: Any) -> None:
    """Fold one finished tool call into the turn's evidence."""
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None:
        return
    turn.step += 1
    ok = bool(getattr(result, "ok", False))
    if name in EDIT_TOOLS and ok:
        path = str(getattr(result, "path", "") or args.get("path") or "")
        kind = _kind(path) if path else None
        if kind == "ui":
            turn.ui_edits[path] = turn.step
        elif kind == "code":
            turn.code_edits[path] = turn.step
        return
    if name == "shell":
        command = str(args.get("command") or "")
        if is_check_command(command):
            turn.last_check = (turn.step, ok, command.strip()[:200])
        return
    if name == "execute_code":
        turn.last_check = (turn.step, ok, "execute_code (ad-hoc)")
        return
    if name in BROWSER_LOOKS and ok:
        turn.last_look = turn.step
        return
    if name in ("delegate_task", "subagent_wait") and ok:
        output = str(getattr(result, "output", "") or "")
        if re.search(r"\bPASS\b", output) and not re.search(r"\bFAIL\b", output):
            turn.last_check = (turn.step, True, f"{name} (verifier PASS)")
            turn.last_look = turn.step


def enabled(core: Core, session: Session) -> bool:
    """``agent.verifyOnStop``: ``on``, ``off`` or ``auto`` (not on messaging channels)."""
    agent = getattr(getattr(core, "settings", None), "agent", None)
    value = str(getattr(agent, "verifyOnStop", "auto") or "auto").strip().lower()
    if value in ("off", "false", "0", "no"):
        return False
    if value in ("on", "true", "1", "yes"):
        return True
    surface = str(getattr(session, "origin_surface", None) or "")
    return not surface.startswith("gateway")


def nudge(core: Core, session: Session) -> str | None:
    """The follow-up for a turn about to end unverified, or ``None`` to let it end."""
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None or turn.nudges >= MAX_NUDGES or not enabled(core, session):
        return None
    edits = {**turn.code_edits, **turn.ui_edits}
    if not edits:
        return _open_todos(session, turn)
    last_edit = max(edits.values())
    check = turn.last_check
    code_ok = check is not None and check[0] > last_edit and check[1]
    ui_last = max(turn.ui_edits.values(), default=-1)
    ui_ok = not turn.ui_edits or turn.last_look > ui_last
    if code_ok and ui_ok:
        return _open_todos(session, turn)
    turn.nudges += 1
    shown = sorted(edits, key=edits.__getitem__)[-8:]
    lines = [
        "[system] You changed code in this turn but have no fresh evidence that it works.",
        "Changed: " + ", ".join(shown) + (" …" if len(edits) > len(shown) else ""),
    ]
    if check is None:
        lines.append("No test, lint, typecheck or build ran after the edits.")
    elif check[0] < last_edit:
        lines.append(f"The last check (`{check[2]}`) ran before the latest edit, so it is stale.")
    elif not check[1]:
        lines.append(f"The last check (`{check[2]}`) failed.")
    lines.append(
        "Run the project's relevant check now, read any failure, fix the cause and say "
        "what passed. With no suite, verify the changed behaviour with execute_code and "
        "call it ad-hoc verification, not a green suite."
    )
    if not ui_ok:
        lines.append(
            "UI files changed too: open the page in the browser, read the console for "
            "errors, take a snapshot or screenshot and compare it with what was asked."
        )
    lines.append(
        "If you cannot verify, say exactly what blocks it and call the work unverified; "
        "do not claim it is done."
    )
    return "\n".join(lines)


def _open_todos(session: Session, turn: TurnEvidence) -> str | None:
    """A turn ending with its own task list unfinished (opencode states the rule,
    nothing enforces it): once, ask to finish or say what blocks each item."""
    if turn.todo_nudged:
        return None
    todos = [t for t in (getattr(session, "todos", None) or []) if isinstance(t, dict)]
    open_items = [t for t in todos if t.get("status") in ("pending", "in_progress")]
    if not open_items:
        return None
    turn.todo_nudged = True
    turn.nudges += 1
    listed = "; ".join(str(t.get("content") or t.get("id")) for t in open_items[:8])
    return (
        f"[system] Your task list still has {len(open_items)} open item(s): {listed}. "
        "Carry on with them now. If one cannot be done in this turn, say what blocks it "
        "and replace it with a revised item, instead of ending as if it were done."
    )


__all__ = [
    "MAX_NUDGES",
    "TurnEvidence",
    "begin_turn",
    "enabled",
    "is_check_command",
    "nudge",
    "observe",
]
