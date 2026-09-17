"""``snowpea`` console entry point.

Three shapes, all documented in ``docs/design/m1-core-contract.md`` §10 and
plan §3.6:

* ``snowpea`` — make sure a daemon is up, then hand the terminal to the Ink TUI.
* ``snowpea -c "<prompt>"`` — one headless turn with deterministic exit codes.
* ``snowpea <subcommand>`` — session-less lookups and daemon control.

Exit codes: ``0`` complete, ``1`` the agent ended in failure, ``2`` usage or
configuration error, ``3`` the daemon could not be reached, ``4`` denied by an
approval or by the mode, ``5`` ``--timeout`` elapsed (or the turn was
interrupted).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import snowpea_core
from snowpea_core import __version__
from snowpea_core import update as update_mod
from snowpea_core.cli import commands as cli_commands
from snowpea_core.cli.daemon_client import (
    DaemonClient,
    DaemonError,
    RpcCallError,
    ensure_daemon,
    pid_alive,
    read_daemon_json,
)
from snowpea_core.cli.render import (
    EXIT_AGENT_FAILED,
    EXIT_DENIED,
    EXIT_NO_DAEMON,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    JsonRenderer,
    PlainRenderer,
    Renderer,
    TurnTracker,
    format_args,
)

MODES = ("plan", "accept", "auto")
TUI_BUNDLE = "snowpea-tui.js"

#: The TUI exits with this after an in-place update, meaning "start me again".
#: 75 is EX_TEMPFAIL, which no other snowpea exit path uses.
TUI_RESTART_EXIT = 75
#: How long to wait for the old daemon to go away before re-execing.
RESTART_DRAIN_SEC = 15.0


class TuiNotFound(RuntimeError):
    """No TUI bundle could be located (→ exit code 2)."""


def _err(message: str) -> None:
    print(f"snowpea: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``snowpea`` parser (argparse exits 2 on a usage error)."""
    parser = argparse.ArgumentParser(
        prog="snowpea",
        description="Snowpea — a local-first coding agent.",
    )
    parser.add_argument("--version", "-V", action="store_true", help="print the version and exit")
    parser.add_argument("--mode", choices=MODES, default=None, help="permission mode")
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="run the TUI in the alternate-buffer full-screen layout instead of inline",
    )
    parser.add_argument("--home", default=None, help="override SNOWPEA_HOME")
    parser.add_argument(
        "-c",
        "--prompt",
        dest="prompt",
        metavar="PROMPT",
        default=None,
        help="run one headless turn with PROMPT and exit",
    )
    parser.add_argument(
        "--resume",
        dest="resume",
        metavar="SESSION_ID",
        default=None,
        help="continue a saved session instead of opening a new one (see `snowpea session list`)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON Lines instead of text")
    parser.add_argument("--cwd", default=None, help="working directory of the session")
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SEC", help="abort the turn after SEC"
    )
    parser.add_argument("--provider", default=None, help="provider vendor for this session")
    parser.add_argument(
        "--approve-none",
        action="store_true",
        help="deny every approval request instead of prompting",
    )
    cli_commands.add_subparsers(parser)
    return parser


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------


def packaged_tui_bundle(package_root: Path | None = None) -> Path:
    """Where the wheel carries the bundle (M8 contract §1, ``hatch_build.py``)."""
    root = package_root or Path(snowpea_core.__file__).resolve().parent
    return root / "tui" / "dist" / TUI_BUNDLE


def repo_tui_bundle(repo_root: Path | None = None) -> Path:
    """Where ``npm -w tui run build`` leaves it in a checkout."""
    root = repo_root or Path(__file__).resolve().parents[3]
    return root / "tui" / "dist" / TUI_BUNDLE


def resolve_tui_command(
    *, package_root: Path | None = None, repo_root: Path | None = None
) -> list[str]:
    """Return the argv prefix that runs the TUI bundle.

    ``SNOWPEA_TUI_ENTRY`` wins; a ``.tsx`` entry runs through ``npx tsx``.  Then
    the packaged ``snowpea_core/tui/dist`` copy, then the repo checkout's
    ``tui/dist`` (development installs).  The two roots are arguments only so
    that ``tests/test_installer.py`` can pin them; nothing passes them in.
    """
    override = os.environ.get("SNOWPEA_TUI_ENTRY")
    if override:
        entry = Path(override).expanduser()
        if not entry.exists():
            raise TuiNotFound(f"SNOWPEA_TUI_ENTRY points at a missing file: {entry}")
        if entry.suffix == ".tsx":
            return ["npx", "tsx", str(entry)]
        return ["node", str(entry)]

    packaged = packaged_tui_bundle(package_root)
    repo = repo_tui_bundle(repo_root)
    if packaged.exists() and repo.exists():
        # Editable installs ship an older copy under ``snowpea_core/tui/dist``.
        # Prefer whichever bundle was built most recently so reconnect and other
        # TUI fixes in the checkout actually run.
        if repo.stat().st_mtime > packaged.stat().st_mtime:
            return ["node", str(repo)]
        return ["node", str(packaged)]
    if packaged.exists():
        return ["node", str(packaged)]
    if repo.exists():
        return ["node", str(repo)]
    raise TuiNotFound(
        f"the TUI bundle ({TUI_BUNDLE}) was not found; build it with "
        "`npm run build --prefix tui` or set SNOWPEA_TUI_ENTRY"
    )


def launch_tui(args: argparse.Namespace, home: str | None) -> int:
    """Ensure a daemon, then exec the TUI and pass its exit code through."""
    try:
        command = resolve_tui_command()
    except TuiNotFound as exc:
        _err(str(exc))
        return EXIT_USAGE
    try:
        info = asyncio.run(ensure_daemon(home))
    except DaemonError as exc:
        _err(str(exc))
        return EXIT_NO_DAEMON
    workdir = Path(args.cwd or Path.cwd()).expanduser().resolve()
    command += ["--port", str(info.port), "--token", info.token, "--cwd", str(workdir)]
    if args.mode:
        command += ["--mode", args.mode]
    if getattr(args, "fullscreen", False):
        command.append("--fullscreen")
    try:
        code = subprocess.call(command)  # noqa: S603 - argv built above
    except FileNotFoundError:
        _err(f"could not run the TUI: {command[0]} is not on PATH")
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_OK
    if code == TUI_RESTART_EXIT:
        return relaunch(home)
    return code


def wait_for_daemon_exit(home: str | None, timeout: float = RESTART_DRAIN_SEC) -> None:
    """Give the daemon the TUI just shut down time to release its port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_daemon_json(home)
        if info is None or not pid_alive(info.pid):
            return
        time.sleep(0.2)


def relaunch(home: str | None = None) -> int:
    """Replace this process with a fresh ``snowpea`` after an update.

    The TUI asks for this with exit code :data:`TUI_RESTART_EXIT` once the
    upgrade finished and the daemon is on its way out; re-execing is what makes
    the newly installed version the one the user is talking to.
    """
    wait_for_daemon_exit(home)
    argv0 = sys.argv[0] or "snowpea"
    executable = shutil.which(argv0)
    if not executable and os.path.exists(argv0):
        executable = argv0
    if not executable:
        executable = shutil.which("snowpea")
    if not executable:
        _err("could not find the snowpea executable to restart; run `snowpea` again")
        return EXIT_USAGE
    try:
        os.execv(executable, [executable, *sys.argv[1:]])
    except OSError as exc:  # pragma: no cover - exec almost never returns
        _err(f"could not restart snowpea: {exc}")
        return EXIT_USAGE
    return EXIT_OK  # pragma: no cover - unreachable after a successful execv


# ---------------------------------------------------------------------------
# headless
# ---------------------------------------------------------------------------


def _exit_code_for_rpc_error(exc: RpcCallError) -> int:
    if exc.code in ("not_implemented", "invalid_params", "not_found", "protocol_incompatible"):
        return EXIT_USAGE
    if exc.code in ("mode_denied", "approval_denied", "approval_timeout"):
        return EXIT_DENIED
    return EXIT_AGENT_FAILED


async def _ask_approval(params: dict[str, Any]) -> str:
    """Prompt the user on stderr and read one answer from stdin."""
    tool = params.get("tool", "?")
    summary = format_args(params.get("args") or {})
    note = str(params.get("note") or "").strip()
    warning = f"!! {note}\n" if note else ""
    question = f"{warning}Allow {tool} {summary}? [y/N/a(lways for session)] "

    def _read() -> str:
        sys.stderr.write(question)
        sys.stderr.flush()
        try:
            return sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            return ""

    return (await asyncio.to_thread(_read)).strip().lower()


async def run_headless(args: argparse.Namespace, home: str | None) -> int:
    """One ``session.prompt`` turn rendered to stdout (plan §3.6)."""
    prompt = args.prompt or ""
    if not prompt.strip():
        _err("-c needs a non-empty prompt")
        return EXIT_USAGE
    if args.timeout is not None and args.timeout <= 0:
        _err("--timeout must be greater than zero")
        return EXIT_USAGE
    workdir = Path(args.cwd or Path.cwd()).expanduser()
    if not workdir.is_dir():
        _err(f"--cwd is not a directory: {workdir}")
        return EXIT_USAGE
    workdir = workdir.resolve()
    if getattr(args, "resume", None) and (args.mode or args.provider):
        # The saved session keeps its own mode, provider and workdir.
        _err("--mode/--provider are ignored with --resume; the saved session keeps its own")

    renderer: Renderer = JsonRenderer() if args.json else PlainRenderer()
    tracker = TurnTracker()
    interactive = sys.stdin.isatty() and not args.approve_none
    always_allow = False

    async def approval_handler(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal always_allow
        if always_allow:
            return {"decision": "allow", "scope": "session"}
        if not interactive:
            tracker.note_denied_request(params.get("sessionId"))
            return {"decision": "deny", "scope": "once"}
        answer = await _ask_approval(params)
        if answer in ("a", "always"):
            always_allow = True
            return {"decision": "allow", "scope": "session"}
        if answer in ("y", "yes"):
            return {"decision": "allow", "scope": "once"}
        tracker.note_denied_request(params.get("sessionId"))
        return {"decision": "deny", "scope": "once"}

    async def question_handler(params: dict[str, Any]) -> dict[str, Any]:
        """Decline every ``ask_user`` question: headless has no picker.

        Answering at once, and saying so on stderr, is kinder than letting the
        question sit out its timeout in a script nobody is watching.  The tool
        reports an empty answer as declined, so the agent knows it was refused
        rather than agreed with.
        """
        asked = [
            str(item.get("question") or "").strip() for item in (params.get("questions") or [])
        ]
        for question in asked:
            _err(f"question needs an interactive client, declined: {question}")
        return {"answers": []}

    try:
        info = await ensure_daemon(home)
    except DaemonError as exc:
        _err(str(exc))
        return EXIT_NO_DAEMON

    client = DaemonClient(
        info, approval_handler=approval_handler, question_handler=question_handler
    )
    try:
        await client.connect()
    except DaemonError as exc:
        _err(str(exc))
        return EXIT_NO_DAEMON

    session_id: str | None = None
    exit_code = EXIT_AGENT_FAILED
    resume_id = str(getattr(args, "resume", None) or "").strip()
    try:
        if resume_id:
            # ``-c "…" --resume <id>`` is the headless half of the TUI's
            # /resume: the saved history is reloaded and this prompt continues
            # it, instead of talking to a brand new session (GAP-15).
            # The replayed events are deliberately discarded: a headless run
            # renders *this* turn, not the history it is continuing.
            try:
                resumed = await client.call("session.resume", {"sessionId": resume_id})
            except RpcCallError as exc:
                _err(f"session.resume failed ({exc.code}): {exc.message}")
                return _exit_code_for_rpc_error(exc)
            session_id = str(resumed.get("sessionId", "") or resume_id)
        else:
            create_params: dict[str, Any] = {
                "workdir": str(workdir),
                "originSurface": "cli",
            }
            if args.mode:
                create_params["mode"] = args.mode
            if args.provider:
                create_params["provider"] = args.provider
            try:
                created = await client.call("session.create", create_params)
            except RpcCallError as exc:
                _err(f"session.create failed ({exc.code}): {exc.message}")
                return _exit_code_for_rpc_error(exc)
            session_id = str(created.get("sessionId", ""))
        tracker.root_session_id = session_id

        consumer = asyncio.ensure_future(_consume(client, session_id, renderer, tracker))
        try:
            try:
                # Slash commands are routed by the core, not parsed here (§9).
                await client.call("session.prompt", {"sessionId": session_id, "text": prompt})
            except RpcCallError as exc:
                _err(f"session.prompt failed ({exc.code}): {exc.message}")
                return _exit_code_for_rpc_error(exc)
            await asyncio.wait_for(consumer, timeout=args.timeout)
            exit_code = tracker.exit_code()
        except TimeoutError:
            with contextlib.suppress(DaemonError, RpcCallError):
                await client.call("session.interrupt", {"sessionId": session_id}, timeout=10.0)
            exit_code = EXIT_TIMEOUT
        except DaemonError as exc:
            _err(str(exc))
            exit_code = EXIT_AGENT_FAILED
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        return exit_code
    finally:
        if session_id:
            with contextlib.suppress(DaemonError, RpcCallError, Exception):
                await client.call("session.close", {"sessionId": session_id}, timeout=10.0)
        await client.close()
        renderer.finish(exit_code, session_id, tracker.usage, tracker.context)


async def _consume(
    client: DaemonClient, session_id: str, renderer: Renderer, tracker: TurnTracker
) -> None:
    """Render ``session.event`` notifications until ``turn.done``."""
    async for frame in client.notifications():
        if frame.get("method") != "session.event":
            continue
        event = frame.get("params") or {}
        if event.get("sessionId") not in (None, session_id):
            continue
        renderer.event(event)
        tracker.event(event)
        if tracker.done:
            return
    raise DaemonError("the daemon closed the connection before the turn finished")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def split_passthrough(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Take the child argv after a bare ``--`` out of an ``mcp`` command line.

    ``snowpea mcp add notes --global -- python -m server`` cannot be parsed by
    argparse as written: a trailing ``nargs="*"`` positional stops collecting
    once an optional flag has been seen, and the ``--`` then reads as an
    unrecognised argument.  Splitting first keeps the documented
    ``-- <command> [args…]`` form working wherever the flags sit, and the tail
    is never parsed — it is the child's argv verbatim.
    """
    items = list(argv)
    if not items or items[0] != "mcp" or "--" not in items:
        return items, []
    index = items.index("--")
    return items[:index], items[index + 1 :]


def parse_argv(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse ``snowpea``'s arguments, honouring the ``mcp`` ``--`` passthrough."""
    head, tail = split_passthrough(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(head)
    if tail:
        args.rest = tail
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """``snowpea`` console script."""
    args = parse_argv(argv)

    if args.version:
        # Cache-only: `--version` never waits on the network.
        print(f"snowpea {__version__}{update_mod.version_suffix(args.home)}")
        return EXIT_OK

    home: str | None = args.home

    try:
        if args.subcommand == "init":
            # Reuses the headless path `-c` uses: a session in --cwd (or the
            # current directory), one turn, deterministic exit codes. The
            # subcommand is just `/init [--force]` typed for you.
            args.prompt = "/init" + (" --force" if getattr(args, "force", False) else "")
            return asyncio.run(run_headless(args, home))
        if args.subcommand:
            return asyncio.run(cli_commands.dispatch(args, home))
        if args.prompt is not None:
            return asyncio.run(run_headless(args, home))
        if getattr(args, "resume", None):
            _err("--resume needs a prompt: snowpea -c \"…\" --resume <sessionId>")
            _err("inside the TUI, use /resume to pick a saved session")
            return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_TIMEOUT
    return launch_tui(args, home)


if __name__ == "__main__":
    raise SystemExit(main())
