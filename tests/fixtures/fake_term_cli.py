#!/usr/bin/env python3
"""A stand-in interactive CLI for the real-tmux bridge test.

``fake_term_cli.py <out> <pidfile>``: writes its pid, puts its terminal in raw
mode with bracketed paste on (as Claude Code does), and appends every submitted
input to ``<out>`` as one JSON string per line: the text of a bracketed paste
(CR turned back into newlines) up to the Enter that follows it.  Runs until it
is killed.
"""

from __future__ import annotations

import json
import os
import sys
import termios
import tty

START, END = b"\x1b[200~", b"\x1b[201~"


def main() -> int:
    out, pidfile = sys.argv[1], sys.argv[2]
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        sys.stdout.write("\x1b[?2004hfake cli ready\r\n> ")
        sys.stdout.flush()
        with open(pidfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        pending = b""
        line = b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return 0
            pending += chunk
            while pending:
                if pending.startswith(START):
                    end = pending.find(END)
                    if end < 0:
                        break
                    line += pending[len(START):end].replace(b"\r", b"\n")
                    pending = pending[end + len(END):]
                    continue
                byte, pending = pending[:1], pending[1:]
                if byte in (b"\r", b"\n"):
                    with open(out, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(line.decode("utf-8", "replace")) + "\n")
                    line = b""
                else:
                    line += byte
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
