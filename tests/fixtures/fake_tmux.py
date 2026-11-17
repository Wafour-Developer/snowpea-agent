#!/usr/bin/env python3
"""A fake ``tmux`` for the tmux-bridge unit tests: no terminal, no server.

It plays a world described in ``$FAKE_TMUX_WORLD`` (JSON) and appends every
call (argv and stdin) to ``$FAKE_TMUX_LOG`` (JSON lines)::

    {"panes": [{"pane": "%3", "tty": "/dev/pts/3", "pid": 100, "session": "work",
                "window": "1", "command": "claude", "dead": false}],
     "screens": {"%3": ["screen 0", "screen 1"]},   # capture-pane shows screens[at]
     "at": {"%3": 0},                                 # BTab moves it one on
     "on_key": {"Down": 1, "Enter": 1, "literal": 0},  # instead: per key, the move
     "fail": ["paste-buffer"]}                        # subcommands that exit 1

Supported: ``-V``, ``[-S path | -L name] list-panes -a -F <fmt>`` (the columns of
``snowpea_core.external.tmux.LIST_FORMAT``, whatever the format says),
``capture-pane -p -t <pane>``, ``load-buffer``, ``paste-buffer``, ``delete-buffer``,
``send-keys``.  Nothing is ever executed.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    args = sys.argv[1:]
    if args == ["-V"]:
        print("tmux 3.4")
        return 0
    data = sys.stdin.buffer.read() if "load-buffer" in args else b""
    log = os.environ.get("FAKE_TMUX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": args, "stdin": data.decode("utf-8", "replace")})
                         + "\n")
    while args and args[0] in ("-S", "-L", "-f"):
        args = args[2:]
    if not args:
        return 1
    path = os.environ.get("FAKE_TMUX_WORLD")
    world = json.load(open(path, encoding="utf-8")) if path else {}
    command = args[0]
    if command in world.get("fail", []):
        print(f"fake tmux: {command} failed", file=sys.stderr)
        return 1
    if command == "list-panes":
        for pane in world.get("panes", []):
            print("\t".join([pane["pane"], pane["tty"], str(pane["pid"]), pane.get("session", "s"),
                             str(pane.get("window", "0")), pane.get("command", "cli"),
                             "1" if pane.get("dead") else "0"]))
        return 0
    if command == "capture-pane":
        target = args[args.index("-t") + 1]
        screens = world.get("screens", {}).get(target)
        if screens is None:
            return 1
        at = world.get("at", {}).get(target, 0)
        sys.stdout.write(screens[min(at, len(screens) - 1)])
        return 0
    if command == "send-keys":
        target = args[args.index("-t") + 1]
        key = "literal" if "-l" in args else args[-1]
        moves = world.get("on_key")
        step = moves.get(key, 0) if isinstance(moves, dict) else (1 if key == "BTab" else 0)
        if step and path:
            world.setdefault("at", {})[target] = max(0, world.get("at", {}).get(target, 0) + step)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(world, handle)
        return 0
    if command in ("load-buffer", "paste-buffer", "delete-buffer"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
