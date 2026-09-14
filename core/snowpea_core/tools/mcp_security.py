"""Shape checks an MCP entry must pass before it is saved (M14 §1b).

An MCP server is an arbitrary child process that the model can then call, so
adding one is closer to installing a plugin than to setting a preference.  The
substance of the check is hermes-agent's ``validate_mcp_server_entry``, vendored
verbatim at ``vendor/hermes/mcp/mcp_security.py`` (MIT, see ``NOTICE`` and
``docs/vendoring-map.md``): a known indicator of compromise anywhere in
command, args or env values; a shell interpreter whose inline script reaches
the network; a shell interpreter writing an OS persistence surface.

:func:`findings` adds the two things that check cannot see because hermes has
no remote-server concept — a plain-http endpoint, and a shell interpreter used
as the server command at all — and returns everything as advisory text.
``mcp.add`` refuses an entry that produces any finding; ``force`` accepts them.

No finding ever includes an ``env`` or ``header`` value: the vendored check is
given the entry (it scans env values for IOCs) but only its own message text is
returned, and these messages name keys, never values.
"""

from __future__ import annotations

import os
from typing import Any

from snowpea_core.vendor.hermes.mcp.mcp_security import (
    _SHELL_INTERPRETERS as SHELL_INTERPRETERS,
)
from snowpea_core.vendor.hermes.mcp.mcp_security import (
    validate_mcp_server_entry,
)

#: Endpoints where plain http is a local loopback rather than the open network.
LOCAL_PREFIXES: tuple[str, ...] = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "http://0.0.0.0",
)


#: Phrases that only appear in a tool description when someone is addressing
#: the model rather than describing a tool (M15 §E).  A server's descriptions
#: are read as prompt text on every turn, so a match is worth a log line — but
#: never a block: the phrases are ordinary English and a legitimate server that
#: documents prompt handling would otherwise stop working.
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "system prompt",
    "</system>",
    "<|im_start|>",
    "you are now",
)

#: Characters with no business in a description: bidi overrides, zero-width
#: joiners and the tag block, all of which hide text from a human reviewer.
_HIDDEN_RANGES: tuple[tuple[int, int], ...] = (
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0xE0000, 0xE007F),
)


def description_findings(text: str, label: str = "tool") -> list[str]:
    """Injection markers in a server-supplied description; advisory only.

    Never blocks: the caller logs what this returns and registers the tool
    anyway.  A server that really is hostile is a server the user chose to
    install, and silently dropping its tools would look like a broken server
    rather than a warning.
    """
    body = str(text or "")
    lowered = body.lower()
    found = [
        f"{label}: description contains {marker!r}, which reads as an instruction to the model"
        for marker in INJECTION_MARKERS
        if marker in lowered
    ]
    hidden = sorted(
        {
            f"U+{ord(char):04X}"
            for char in body
            if any(low <= ord(char) <= high for low, high in _HIDDEN_RANGES)
        }
    )
    if hidden:
        found.append(f"{label}: description contains hidden characters ({', '.join(hidden)})")
    return found


def _basename(command: str) -> str:
    return os.path.basename(command.replace("\\", "/")).lower()


def findings(entry: dict[str, Any], name: str = "server") -> list[str]:
    """Everything questionable about ``entry``; empty means it looks ordinary."""
    found = [str(issue) for issue in validate_mcp_server_entry(name, entry)]

    command = str(entry.get("command") or "")
    if command and _basename(command) in SHELL_INTERPRETERS and not found:
        # The vendored check only objects to a shell interpreter that also
        # reaches the network or writes a persistence surface.  A server whose
        # command *is* a shell is still not a server: its argv is a program.
        found.append(
            f"'{_basename(command)}' is a shell interpreter, not a server binary; "
            "declare the real executable and its arguments instead"
        )

    url = str(entry.get("url") or "")
    if url.startswith("http://") and not url.startswith(LOCAL_PREFIXES):
        found.append("the url is plain http, so its headers travel in clear text")
    return found


__all__ = [
    "INJECTION_MARKERS",
    "LOCAL_PREFIXES",
    "SHELL_INTERPRETERS",
    "description_findings",
    "findings",
    "validate_mcp_server_entry",
]
