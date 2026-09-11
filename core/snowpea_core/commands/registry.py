"""Slash-command registry — M1 stub (US-005 fills it in)."""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import CommandInfo


class CommandRegistry:
    """Name -> command lookup plus ``/foo bar`` parsing. Stub: empty."""

    def __init__(self) -> None:
        self._commands: dict[str, Any] = {}

    def register(self, command: Any) -> None:
        self._commands[getattr(command, "name", str(command))] = command

    def list(self, session: Any | None = None) -> list[CommandInfo]:
        return []

    def parse(self, text: str) -> tuple[str, str] | None:
        if not text.startswith("/"):
            return None
        name, _, args = text[1:].partition(" ")
        if not name:
            return None
        return name, args.strip()


__all__ = ["CommandRegistry"]
