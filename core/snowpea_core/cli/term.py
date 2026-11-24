"""``snowpea term ls | attach | read | send`` — terminal sessions from a shell.

Design m37 §2.9 / §2.13: thin clients of the daemon's ``terminal.*`` methods.
``attach`` puts this TTY in raw mode, shows the terminal's screen and output,
sends what the person types as human input and resizes the remote PTY with the
window.  ``Ctrl+\\`` detaches; the TTY is restored on every way out.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from snowpea_core.cli.daemon_client import (
    DaemonClient,
    DaemonError,
    RpcCallError,
    ensure_daemon,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3

#: Ctrl+\ — the detach key (FS, 0x1c).
DETACH_BYTE = b"\x1c"
#: Bytes of terminal.data acknowledged at a time.
ACK_BATCH = 16 * 1024


class _Rpc(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = ...) -> dict[str, Any]: ...


def _fail(message: str, code: int) -> int:
    print(f"snowpea: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def add_parser(sub: Any) -> None:
    term = sub.add_parser("term", help="terminal sessions: list, attach, read and send")
    actions = term.add_subparsers(dest="action", metavar="<action>")
    ls = actions.add_parser("ls", help="list terminals")
    ls.add_argument("--all", dest="term_all", action="store_true", help="include ended ones")
    ls.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    attach = actions.add_parser("attach", help="attach this terminal to one (Ctrl+\\ detaches)")
    attach.add_argument("terminal_id")
    attach.add_argument(
        "--observe", action="store_true", help="watch only: never resize, never write"
    )
    read = actions.add_parser("read", help="print a terminal's screen as plain text")
    read.add_argument("terminal_id")
    read.add_argument("--lines", type=int, default=60, help="history lines to include (default 60)")
    read.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    send = actions.add_parser("send", help="type text into a terminal and press Enter")
    send.add_argument("terminal_id")
    send.add_argument("text", nargs="+", help="the text to send")
    send.add_argument("--no-submit", action="store_true", help="do not press Enter afterwards")
    send.add_argument("--force", action="store_true", help="send into a blocked terminal")


# ---------------------------------------------------------------------------
# ls / read / send
# ---------------------------------------------------------------------------


def format_row(info: dict[str, Any]) -> str:
    status = info.get("status") or {}
    state = str(status.get("state", "?"))
    if status.get("kind"):
        state += f"/{status['kind']}"
    if not info.get("alive", True):
        state = "exited" if info.get("exitCode") in (None, 0) else f"exited({info.get('exitCode')})"
    title = str(info.get("title") or "").replace("\n", " ")
    return (
        f"{info.get('terminalId', ''):<14} {info.get('profile', ''):<8} {state:<18} "
        f"{info.get('cwd', '')}" + (f"  {title}" if title else "")
    )


async def _with_client(home: Path | str | None, work: Any) -> int:
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    try:
        return await work(client)
    except RpcCallError as exc:
        return _fail(f"{exc.code}: {exc.message}", EXIT_USAGE)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    finally:
        await client.close()


async def term_ls(client: _Rpc, *, all_: bool, as_json: bool) -> int:
    result = await client.call("terminal.list", {} if all_ else {"aliveOnly": True})
    rows = [r for r in (result.get("terminals") or []) if isinstance(r, dict)]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not rows:
        print("no terminals")
        return EXIT_OK
    for row in rows:
        print(format_row(row))
    return EXIT_OK


async def term_read(client: _Rpc, terminal_id: str, *, lines: int, as_json: bool) -> int:
    result = await client.call("terminal.read", {"terminalId": terminal_id, "lines": lines})
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("text", ""))
    return EXIT_OK


async def term_send(
    client: _Rpc, terminal_id: str, text: str, *, submit: bool, force: bool
) -> int:
    result = await client.call(
        "terminal.send",
        {"terminalId": terminal_id, "text": text, "submit": submit, "force": force},
    )
    if result.get("sent", True) is False:
        return _fail(f"not sent: {result.get('reason') or 'refused'}", EXIT_USAGE)
    if result.get("accepted") is False:
        print("sent (the program did not start working within 5 s)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def raw_mode(fd: int) -> Iterator[None]:
    """Put ``fd`` in raw mode; the old attributes come back on every exit path."""
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


def window_size(fd: int) -> tuple[int, int]:
    size = os.get_terminal_size(fd)
    return size.columns, size.lines


class _Attach:
    """The state of one attach: output, acks and the detach reason."""

    def __init__(self, client: Any, terminal_id: str, write_out: Callable[[bytes], None]) -> None:
        self.client = client
        self.terminal_id = terminal_id
        self.write_out = write_out
        self.pending_ack = 0
        self.reason = "detached"
        self.exit_code: int | None = None
        self.done = asyncio.Event()

    async def handle(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        params = frame.get("params") or {}
        if params.get("terminalId") != self.terminal_id:
            return
        if method == "terminal.data":
            raw = base64.b64decode(params.get("data") or "")
            self.write_out(raw)
            self.pending_ack += len(raw)
            if self.pending_ack >= ACK_BATCH:
                count, self.pending_ack = self.pending_ack, 0
                await self.client.call(
                    "terminal.ack", {"terminalId": self.terminal_id, "bytes": count}
                )
        elif method == "terminal.exit":
            self.reason = "exited"
            self.exit_code = params.get("exitCode")
            self.done.set()
        elif method == "terminal.closed":
            self.reason = "closed"
            self.done.set()

    async def send_input(self, data: bytes) -> bool:
        """Forward typed bytes; ``False`` when Ctrl+\\ asked to detach."""
        if DETACH_BYTE in data:
            data = data.split(DETACH_BYTE, 1)[0]
            detach = True
        else:
            detach = False
        if data:
            await self.client.call(
                "terminal.write",
                {
                    "terminalId": self.terminal_id,
                    "data": base64.b64encode(data).decode("ascii"),
                    "human": True,
                },
            )
        if detach:
            self.done.set()
        return not detach


async def attach_session(
    client: Any,
    terminal_id: str,
    *,
    observe: bool,
    write_out: Callable[[bytes], None],
    read_input: Callable[[], bytes],
    add_input_reader: Callable[[Callable[[], None]], Callable[[], None]],
    size: Callable[[], tuple[int, int]],
    on_winch: Callable[[Callable[[], None]], Callable[[], None]],
) -> tuple[str, int | None]:
    """Run an attach until detach, exit or close; returns ``(reason, exitCode)``.

    The TTY itself (raw mode, signals) is injected so this can be driven by a
    test; :func:`term_attach` supplies the real ones.
    """
    cols, rows = size()
    result = await client.call(
        "terminal.attach",
        {
            "terminalId": terminal_id,
            "mode": "observe" if observe else "interactive",
            "cols": cols,
            "rows": rows,
        },
    )
    state = _Attach(client, terminal_id, write_out)
    repaint = result.get("repaint") or ""
    if repaint:
        write_out(base64.b64decode(repaint))

    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task[Any]] = set()

    def spawn(coro: Any) -> None:
        task = loop.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def on_input() -> None:
        data = read_input()
        if not data:  # EOF on stdin
            state.done.set()
            return
        if not observe:
            spawn(state.send_input(data))
        elif DETACH_BYTE in data:
            state.done.set()

    def resized() -> None:
        if observe:
            return
        c, r = size()
        spawn(client.call("terminal.resize", {"terminalId": terminal_id, "cols": c, "rows": r}))

    async def pump() -> None:
        async for frame in client.notifications():
            await state.handle(frame)
        state.reason = "connection closed"
        state.done.set()

    remove_input = add_input_reader(on_input)
    remove_winch = on_winch(resized)
    pump_task = loop.create_task(pump())
    try:
        await state.done.wait()
    finally:
        remove_input()
        remove_winch()
        pump_task.cancel()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(pump_task, *tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await client.call("terminal.detach", {"terminalId": terminal_id})
    return state.reason, state.exit_code


async def term_attach(client: DaemonClient, terminal_id: str, *, observe: bool) -> int:
    """The real-TTY wrapper around :func:`attach_session` (POSIX)."""
    if sys.platform == "win32":
        return _fail(
            "`snowpea term attach` is not supported on Windows yet; "
            "use `snowpea term read|send`, or open the terminal in the app",
            EXIT_USAGE,
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _fail("`snowpea term attach` needs an interactive terminal", EXIT_USAGE)
    in_fd, out_fd = sys.stdin.fileno(), sys.stdout.fileno()
    loop = asyncio.get_running_loop()

    def write_out(data: bytes) -> None:
        os.write(out_fd, data)

    def add_input_reader(callback: Callable[[], None]) -> Callable[[], None]:
        loop.add_reader(in_fd, callback)
        return lambda: loop.remove_reader(in_fd)

    def on_winch(callback: Callable[[], None]) -> Callable[[], None]:
        loop.add_signal_handler(signal.SIGWINCH, callback)
        return lambda: loop.remove_signal_handler(signal.SIGWINCH)

    print(f"attached to {terminal_id}; Ctrl+\\ detaches", file=sys.stderr)
    with raw_mode(in_fd):
        reason, code = await attach_session(
            client,
            terminal_id,
            observe=observe,
            write_out=write_out,
            read_input=lambda: os.read(in_fd, 4096),
            add_input_reader=add_input_reader,
            size=lambda: window_size(out_fd),
            on_winch=on_winch,
        )
    print(f"\r\n[{reason}]" + (f" exit code {code}" if code is not None else ""), file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def run(args: Any, home: Path | str | None) -> int:
    action = getattr(args, "action", None)
    if action == "ls":
        return await _with_client(
            home,
            lambda c: term_ls(
                c,
                all_=bool(getattr(args, "term_all", False)),
                as_json=bool(getattr(args, "sub_json", False)),
            ),
        )
    if action == "read":
        return await _with_client(
            home,
            lambda c: term_read(
                c,
                args.terminal_id,
                lines=args.lines,
                as_json=bool(getattr(args, "sub_json", False)),
            ),
        )
    if action == "send":
        return await _with_client(
            home,
            lambda c: term_send(
                c,
                args.terminal_id,
                " ".join(args.text),
                submit=not args.no_submit,
                force=args.force,
            ),
        )
    if action == "attach":
        return await _with_client(
            home, lambda c: term_attach(c, args.terminal_id, observe=args.observe)
        )
    return _fail("usage: snowpea term ls | attach <id> | read <id> | send <id> <text>", EXIT_USAGE)
