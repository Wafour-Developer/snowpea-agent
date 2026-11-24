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
It never runs a check itself, and it does not ask for one the person declined
or told it not to run (addendum 29, C3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
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

#: Calls that show the page itself: a snapshot of what rendered, a screenshot,
#: or a screenshot file opened with view_image.  Navigating alone is not a look.
BROWSER_LOOKS = frozenset({"browser_snapshot", "browser_screenshot", "view_image"})

#: The snowpea browser's ``repl`` host tool: code that snapshots or
#: screenshots a page is a look too (addendum 28, C1).
REPL_TOOL = "repl"
_REPL_LOOK = re.compile(
    r"(?<![\w.$])(?:snapshot|annotatedScreenshot|snapshotTabs|display)\s*\("
    r"|\.screenshot\s*\("
)

#: ``repl`` code that hands the tab to the person (a payment, a login).
_REQUEST_HUMAN = re.compile(r"(?<![\w.$])requestHuman\s*\(")

#: A to-do the model marked as not its to do now: blocked, or waiting on the
#: person (after a ``requestHuman`` hand-back, a payment only they can make).
_TODO_WAITS = re.compile(
    r"\(\s*(?:blocked|waiting)\b"
    r"|\bwaiting\s+(?:on|for)\s+(?:the\s+)?(?:person|user|you)\b"
    r"|\(\s*(?:차단|대기)\s*[:：]"
    r"|사용자\s*(?:대기|확인\s*대기)|사용자(?:가|께서)?\s*직접",
    re.IGNORECASE,
)


def todo_waits(content: str) -> bool:
    """True when a to-do says it is blocked or waits on the person."""
    return bool(_TODO_WAITS.search(content or ""))


#: Script files that are UI when they live in a web project (one with an
#: ``index.html`` or a bundler config between them and the working directory).
WEB_SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".ts", ".glsl", ".wgsl"})
WEB_ROOT_MARKERS = (
    "index.html", "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "next.config.js", "next.config.mjs", "svelte.config.js", "nuxt.config.ts",
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
    r"|node\s+\S+\.(?:m?js|cjs|ts)|deno\s+run|bun\s+(?:run\s+)?\S+\.(?:m?js|ts)"
    r")(?:\b|$)"
)


def _is_look(session: Session, name: str, args: dict[str, Any], result: Any) -> bool:
    """True when a successful call showed the rendered page.

    A built-in look, a host tool registered with ``look: true``, a host result
    whose meta says ``look: true`` or ``kind: "snapshot"``, or ``repl`` code
    that takes a snapshot or a screenshot.
    """
    if name in BROWSER_LOOKS:
        return True
    from snowpea_core.tools.host_tools import HOST_META_KEY, HOST_TOOLS

    host_meta = (getattr(result, "meta", None) or {}).get(HOST_META_KEY)
    if isinstance(host_meta, dict) and (
        host_meta.get("look") is True or host_meta.get("kind") == "snapshot"
    ):
        return True
    tool = HOST_TOOLS.for_session(session).get(name)
    if tool is not None and getattr(tool, "look", False):
        return True
    return name == REPL_TOOL and bool(_REPL_LOOK.search(str(args.get("code") or "")))


def has_repl(session: Session) -> bool:
    """True when the session's host (the snowpea browser) provides ``repl``."""
    from snowpea_core.tools.host_tools import HOST_TOOLS

    return REPL_TOOL in HOST_TOOLS.for_session(session)


#: The person asked for no checks this turn: "검사 명령은 돌리지 말고",
#: "테스트는 생략하고", "don't run the tests".  Only an instruction counts:
#: "테스트하지 않은 코드", "no tests yet" or "don't skip the tests" do not.
_CHECK_WORD_KO = r"(?:검사|테스트|빌드|체크|린트|타입\s*체크|tsc|pytest)"
_KO_PARTICLE = r"(?:\s*(?:명령|명령어|코드)?\s*(?:은|는|을|를|도)?\s*)"
_NO_CHECKS = re.compile(
    _CHECK_WORD_KO + _KO_PARTICLE
    + r"(?:(?:돌리|실행하|하|돌리진|실행하진)지?\s*(?:말|마)"
    r"|생략(?:하고|해|해줘|해주세요|하세요)|빼고\s|안\s*돌려도)"
    r"|(?<!n't )(?<!not )\b(?:don'?t|do\s+not|no\s+need\s+to)\s+(?:run|bother\s+running)"
    r"\s+(?:the\s+|any\s+)?(?:tests?|checks?|tsc|builds?|lint(?:er)?s?|type\s*-?checks?"
    r"|test\s+suite)\b"
    r"|(?<!n't )(?<!not )\bskip\s+(?:running\s+)?(?:the\s+|all\s+)?(?:tests|checks|build"
    r"|typecheck|test\s+suite)\s*(?:[.,;!]|$|and\s+just|for\s+now)",
    re.IGNORECASE,
)


def user_declines_checks(text: str) -> bool:
    """True when the person's own words say not to run checks or tests."""
    return bool(_NO_CHECKS.search(text or ""))


def is_check_command(command: str) -> bool:
    """True when ``command`` runs tests, a linter, a type checker or a build."""
    return bool(_CHECK.search(command or ""))


def _in_web_project(path: Path, workdir: Path | None) -> bool:
    """True when a web entry or bundler config sits between ``path`` and ``workdir``."""
    try:
        folder = path.resolve().parent
        stop = workdir.resolve() if workdir is not None else None
    except OSError:
        return False
    for _ in range(12):
        if any((folder / marker).is_file() for marker in WEB_ROOT_MARKERS):
            return True
        if folder == stop or folder.parent == folder:
            return False
        folder = folder.parent
    return False


def _kind(path: str, workdir: Path | None = None) -> str | None:
    """``"ui"``, ``"code"`` or ``None`` (nothing to verify) for an edited path."""
    pure = PurePath(path)
    suffix = pure.suffix.lower()
    if suffix in NON_CODE_SUFFIXES or (not suffix and pure.name.lower() in NON_CODE_NAMES):
        return None
    if suffix in UI_SUFFIXES:
        return "ui"
    if suffix in WEB_SCRIPT_SUFFIXES:
        full = Path(path) if Path(path).is_absolute() or workdir is None else workdir / path
        if _in_web_project(full, workdir):
            return "ui"
    return "code"


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
    #: :attr:`step` when the open-todos follow-up was sent (-1: never).
    todo_nudge_step: int = -1
    #: The browser's ``repl`` handed a tab to the person (``requestHuman``) this turn.
    handed_to_person: bool = False
    #: A question to the person timed out this turn; open to-dos then wait on them.
    question_unanswered: bool = False
    #: The turn was already told its deliverable sits in throwaway ``tmp/``.
    tmp_nudged: bool = False
    #: The person declined a check (a shell/execute_code approval) this turn.
    check_declined: bool = False
    #: ... or a look at the page.
    look_declined: bool = False
    #: The person's message said not to run checks.
    no_checks: bool = False
    closed: bool = False
    #: Unfinished-work continuations this turn (agent/unfinished_work.py), and
    #: :attr:`step` when the last one was sent: no tool call since means the
    #: model only restated its list, and the turn may end.
    unfinished_continues: int = 0
    unfinished_step: int = -1


def begin_turn(session: Session, text: str = "") -> None:
    session.verify_turn = TurnEvidence(no_checks=user_declines_checks(text))


def note_declined(
    session: Session, name: str, args: dict[str, Any], tag: str | None = None
) -> None:
    """The person declined this call: do not send the model back to retry it."""
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None:
        return
    command = str(args.get("command") or "")
    if name == "execute_code" or (
        (name == "shell" or tag == "exec") and is_check_command(command)
    ):
        # Declining ``npm install`` or ``git push`` is not declining a check.
        turn.check_declined = True
    elif name in BROWSER_LOOKS or name == REPL_TOOL:
        turn.look_declined = True


def observe(session: Session, name: str, args: dict[str, Any], result: Any) -> None:
    """Fold one finished tool call into the turn's evidence."""
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None:
        return
    turn.step += 1
    ok = bool(getattr(result, "ok", False))
    if name in EDIT_TOOLS and ok:
        path = str(getattr(result, "path", "") or args.get("path") or "")
        workdir = getattr(session, "workdir", None)
        kind = _kind(path, Path(workdir) if workdir else None) if path else None
        if kind == "ui":
            turn.ui_edits[path] = turn.step
        elif kind == "code":
            turn.code_edits[path] = turn.step
        return
    if name == "shell":
        # Files a heredoc, tee or sed -i wrote count as edits (1.35.0).
        wrote = (getattr(result, "meta", None) or {}).get("wrote") if ok else None
        workdir = getattr(session, "workdir", None)
        for path in wrote or ():
            kind = _kind(str(path), Path(workdir) if workdir else None)
            if kind == "ui":
                turn.ui_edits[str(path)] = turn.step
            elif kind == "code":
                turn.code_edits[str(path)] = turn.step
        command = str(args.get("command") or "")
        if is_check_command(command):
            turn.last_check = (turn.step, ok, command.strip()[:200])
        return
    if name == "execute_code":
        turn.last_check = (turn.step, ok, "execute_code (ad-hoc)")
        return
    if name == REPL_TOOL and _REQUEST_HUMAN.search(str(args.get("code") or "")):
        turn.handed_to_person = True
    if ok and _is_look(session, name, args, result):
        turn.last_look = turn.step
        return
    if name == "delegate_task" and ok and str(args.get("agent") or "") == "verifier":
        # Only the verifier's verdict counts, and UNVERIFIED is not a pass: an
        # executor's "7/7 PASS" over API tests is how a page that never
        # rendered was once reported complete.
        output = str(getattr(result, "output", "") or "")
        if re.search(r"\bPASS\b", output) and not re.search(r"\b(?:FAIL|UNVERIFIED)\b", output):
            turn.last_check = (turn.step, True, "delegate_task (verifier PASS)")
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


def _path_after(final_text: str, root: str) -> str | None:
    """The first path in ``final_text`` that starts with ``root``, if any."""
    start = final_text.find(root)
    if start < 0:
        return None
    end = start + len(root)
    stops = "`'\")>]"
    while end < len(final_text) and not final_text[end].isspace() and final_text[end] not in stops:
        end += 1
    return final_text[start:end].rstrip(".,;:")


def tmp_deliverable(session: Session, final_text: str) -> str | None:
    """The first path under the session's ``tmp/`` that ``final_text`` names, if any."""
    workspace = getattr(session, "workspace_dir", None)
    if not workspace or not final_text:
        return None
    return _path_after(final_text, str(workspace).rstrip("/\\") + "/tmp/")


def os_downloads_deliverable(
    session: Session, final_text: str, results_dir: str | None = None
) -> str | None:
    """The first path under the computer's ``~/Downloads`` that ``final_text`` names.

    A file the session itself keeps there (its results folder lies inside
    ``~/Downloads``) is fine.
    """
    if not final_text:
        return None
    home = str(Path("~").expanduser()).rstrip("/\\")
    for root in (f"{home}/Downloads/", "~/Downloads/"):
        path = _path_after(final_text, root)
        if path is None:
            continue
        full = home + path[1:] if path.startswith("~") else path
        if results_dir and (full + "/").startswith(str(results_dir).rstrip("/\\") + "/"):
            continue
        return path
    return None


def nudge(core: Core, session: Session, final_text: str = "") -> str | None:
    """The follow-up for a turn about to end unchecked or with its work misplaced.

    ``None`` lets the turn end.  ``final_text`` is the answer it is about to
    give: one that points the user at a file in the session's throwaway
    ``tmp/`` (R5, 1.37.0) or in the computer's ``~/Downloads`` (1.71.0) is
    sent back once to put the deliverable in the session's results folder.
    """
    unverified = _nudge_unverified(core, session, final_text)
    if unverified is not None:
        return unverified
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None or turn.tmp_nudged or turn.nudges >= MAX_NUDGES or not enabled(core, session):
        return None
    from snowpea_core.session.paths import resolve

    folders = resolve(session, core)
    path = tmp_deliverable(session, final_text)
    if path is not None:
        where = "in the session's scratch space (tmp/), which is throwaway"
    else:
        path = os_downloads_deliverable(session, final_text, folders.results_dir)
        if path is None:
            return None
        where = "in the computer's Downloads folder, outside this conversation's folders"
    turn.tmp_nudged = True
    turn.nudges += 1
    return (
        f"[system] Your answer points at {path}, {where} and not where the user looks. "
        f"Unless they named that place, move the file into {folders.results_dir} "
        f"(a downloaded file into {folders.downloads_dir}), check it is there, and give "
        "the new path."
    )


def _nudge_unverified(core: Core, session: Session, final_text: str = "") -> str | None:
    """The follow-up for a turn about to end unverified, or ``None`` to let it end."""
    turn: TurnEvidence | None = getattr(session, "verify_turn", None)
    if turn is None or turn.nudges >= MAX_NUDGES or not enabled(core, session):
        return None
    edits = {**turn.code_edits, **turn.ui_edits}
    if not edits:
        return _open_todos(session, turn, final_text)
    check = turn.last_check
    # Only code edits need a check; a UI-only turn is proved by looking at it.
    last_edit = max(turn.code_edits.values(), default=-1)
    code_ok = not turn.code_edits or (
        check is not None and check[0] > last_edit and check[1]
    )
    ui_last = max(turn.ui_edits.values(), default=-1)
    ui_ok = not turn.ui_edits or turn.last_look > ui_last or turn.look_declined
    declined = not code_ok and turn.check_declined
    if not code_ok and (turn.no_checks or turn.check_declined):
        # The person said no: asking again is how a 14 s turn took 3 minutes.
        code_ok = True
    if code_ok and ui_ok:
        if declined and not turn.closed:
            turn.closed = True
            turn.nudges += 1
            return (
                "[system] A check was not approved. Do not try another way to run it: "
                "end the turn now and say the work is unverified."
            )
        return _open_todos(session, turn, final_text)
    turn.nudges += 1
    shown = sorted(edits, key=edits.__getitem__)[-8:]
    lines = [
        "[system] You changed code in this turn but have no fresh evidence that it works.",
        "Changed: " + ", ".join(shown) + (" …" if len(edits) > len(shown) else ""),
    ]
    if not code_ok:
        if check is None:
            lines.append("No test, lint, typecheck or build ran after the edits.")
        elif check[0] < last_edit:
            lines.append(
                f"The last check (`{check[2]}`) ran before the latest edit, so it is stale."
            )
        else:
            lines.append(f"The last check (`{check[2]}`) failed.")
        lines.append(
            "Unless the user asked you not to run checks, run the project's relevant check "
            "now, read any failure, fix the cause and say what passed. With no suite, "
            "exercise the changed behaviour — execute_code (Python) or node in shell — and "
            "call it ad-hoc verification, not a green suite."
        )
    if not ui_ok:
        if turn.no_checks or turn.check_declined:
            lines.append("Do not run tests or other checks: the user said not to.")
        look = (
            "take snapshot(page) (or page.screenshot()) in repl"
            if has_repl(session)
            else "take a browser_screenshot (or snapshot)"
        )
        lines.append(
            "UI files changed too: start the dev server if needed, open the page in the "
            f"browser, {look}, read the console errors, "
            "and compare what you see with what was asked. Navigating alone is not a look."
        )
    lines.append(
        "If you cannot verify, say exactly what blocks it and call the work unverified; "
        "do not claim it is done."
    )
    return "\n".join(lines)


def _open_todos(session: Session, turn: TurnEvidence, final_text: str = "") -> str | None:
    """A turn ending with its own task list unfinished (opencode states the rule,
    nothing enforces it): once, ask to finish or say what blocks each item.

    An item marked blocked or waiting on the person is done for this turn; so
    is the whole list when the turn handed a tab to the person
    (``requestHuman``) and its answer ends waiting on them."""
    if turn.todo_nudged or turn.question_unanswered:
        return None
    if turn.handed_to_person and final_text:
        from snowpea_core.agent import unfinished_work

        if unfinished_work.waiting(final_text):
            return None
    todos = [t for t in (getattr(session, "todos", None) or []) if isinstance(t, dict)]
    open_items = [
        t
        for t in todos
        if t.get("status") in ("pending", "in_progress")
        and not todo_waits(str(t.get("content") or ""))
    ]
    if not open_items:
        return None
    turn.todo_nudged = True
    turn.todo_nudge_step = turn.step
    turn.nudges += 1
    listed = "; ".join(str(t.get("content") or t.get("id")) for t in open_items[:8])
    return (
        f"[system] Your task list still has {len(open_items)} open item(s): {listed}. "
        "Carry on with them now. If one cannot be done, set it back to pending with "
        '"(blocked: reason)" added, or "(waiting on the person: reason)" when only they can '
        "do it (a payment, a login), and say so, instead of ending as if it were done."

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
