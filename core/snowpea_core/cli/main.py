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
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import snowpea_core
from snowpea_core import __version__
from snowpea_core.cli import commands as cli_commands
from snowpea_core.cli.daemon_client import (
    DaemonClient,
    DaemonError,
    RpcCallError,
    ensure_daemon,
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
    parser.add_argument("--home", default=None, help="override SNOWPEA_HOME")
    parser.add_argument(
        "-c",
        "--prompt",
        dest="prompt",
        metavar="PROMPT",
        default=None,
        help="run one headless turn with PROMPT and exit",
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


def resolve_tui_command() -> list[str]:
    """Return the argv prefix that runs the TUI bundle.

    ``SNOWPEA_TUI_ENTRY`` wins; a ``.tsx`` entry runs through ``npx tsx``.  Then
    the packaged ``snowpea_core/tui/dist`` copy, then the repo checkout's
    ``tui/dist`` (development installs).
    """
    override = os.environ.get("SNOWPEA_TUI_ENTRY")
    if override:
        entry = Path(override).expanduser()
        if not entry.exists():
            raise TuiNotFound(f"SNOWPEA_TUI_ENTRY points at a missing file: {entry}")
        if entry.suffix == ".tsx":
            return ["npx", "tsx", str(entry)]
        return ["node", str(entry)]

    packaged = Path(snowpea_core.__file__).resolve().parent / "tui" / "dist" / TUI_BUNDLE
    if packaged.exists():
        return ["node", str(packaged)]
    repo = Path(__file__).resolve().parents[3] / "tui" / "dist" / TUI_BUNDLE
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
    try:
        return subprocess.call(command)  # noqa: S603 - argv built above
    except FileNotFoundError:
        _err(f"could not run the TUI: {command[0]} is not on PATH")
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_OK


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
    question = f"Allow {tool} {summary}? [y/N/a(lways for session)] "

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

    renderer: Renderer = JsonRenderer() if args.json else PlainRenderer()
    tracker = TurnTracker()
    interactive = sys.stdin.isatty() and not args.approve_none
    always_allow = False

    async def approval_handler(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal always_allow
        if always_allow:
            return {"decision": "allow", "scope": "session"}
        if not interactive:
            tracker.denied = True
            return {"decision": "deny", "scope": "once"}
        answer = await _ask_approval(params)
        if answer in ("a", "always"):
            always_allow = True
            return {"decision": "allow", "scope": "session"}
        if answer in ("y", "yes"):
            return {"decision": "allow", "scope": "once"}
        tracker.denied = True
        return {"decision": "deny", "scope": "once"}

    try:
        info = await ensure_daemon(home)
    except DaemonError as exc:
        _err(str(exc))
        return EXIT_NO_DAEMON

    client = DaemonClient(info, approval_handler=approval_handler)
    try:
        await client.connect()
    except DaemonError as exc:
        _err(str(exc))
        return EXIT_NO_DAEMON

    session_id: str | None = None
    exit_code = EXIT_AGENT_FAILED
    try:
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
        renderer.finish(exit_code, session_id, tracker.usage)


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


def main(argv: Sequence[str] | None = None) -> int:
    """``snowpea`` console script."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.version:
        print(f"snowpea {__version__}")
        return EXIT_OK

    home: str | None = args.home

    try:
        if args.subcommand:
            return asyncio.run(cli_commands.dispatch(args, home))
        if args.prompt is not None:
            return asyncio.run(run_headless(args, home))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_TIMEOUT
    return launch_tui(args, home)


if __name__ == "__main__":
    raise SystemExit(main())
