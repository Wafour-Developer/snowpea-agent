"""Rendering diagnostics for tool output (M13 contract §2, §3).

Ported from opencode's ``packages/opencode/src/lsp/diagnostic.ts`` (MIT, commit
95daf90).  Two deliberate differences from upstream, both from the M13
contract: the line carries the diagnostic ``code`` when the server sent one,
and warnings are rendered alongside errors rather than dropped — the agent is
the reader here, and "unused import" is exactly the kind of thing it should
clean up in the edit it is already making.

# MIT License
#
# Copyright (c) 2025 opencode
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""

from __future__ import annotations

from typing import Any

#: Lines rendered for one file before the "… N more" tail (contract §2).
MAX_PER_FILE = 20

#: LSP ``DiagnosticSeverity`` -> the word the agent reads.
SEVERITY: dict[int, str] = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}

#: Severities that reach the ``Diagnostics`` block appended to an edit.
REPORTED_SEVERITIES: frozenset[int] = frozenset({1, 2})

Diagnostic = dict[str, Any]


def severity_of(diagnostic: Diagnostic) -> int:
    """``severity`` as an int; a server that omits it means "error" (LSP §3.17)."""
    raw = diagnostic.get("severity")
    return raw if isinstance(raw, int) and raw in SEVERITY else 1


def _position(diagnostic: Diagnostic) -> tuple[int, int]:
    """1-based ``(line, column)`` of the diagnostic's start."""
    start = (diagnostic.get("range") or {}).get("start") or {}
    line = start.get("line")
    character = start.get("character")
    return (
        (line if isinstance(line, int) else 0) + 1,
        (character if isinstance(character, int) else 0) + 1,
    )


def pretty(diagnostic: Diagnostic) -> str:
    """One diagnostic as ``ERROR [12:5] message (code)``."""
    line, column = _position(diagnostic)
    text = str(diagnostic.get("message", "")).strip().replace("\n", " ")
    code = diagnostic.get("code")
    suffix = f" ({code})" if code not in (None, "") else ""
    return f"{SEVERITY[severity_of(diagnostic)]} [{line}:{column}] {text}{suffix}"


def counts(diagnostics: list[Diagnostic]) -> tuple[int, int]:
    """``(errors, warnings)`` in ``diagnostics``."""
    errors = sum(1 for item in diagnostics if severity_of(item) == 1)
    warnings = sum(1 for item in diagnostics if severity_of(item) == 2)
    return errors, warnings


def report(file: str, diagnostics: list[Diagnostic]) -> str:
    """The ``Diagnostics`` block for one file, or ``""`` when it is clean.

    Errors sort before warnings so a truncated block never hides an error
    behind twenty unused imports.
    """
    reported = [item for item in diagnostics if severity_of(item) in REPORTED_SEVERITIES]
    if not reported:
        return ""
    reported.sort(key=lambda item: (severity_of(item), _position(item)))
    shown = reported[:MAX_PER_FILE]
    lines = [pretty(item) for item in shown]
    more = len(reported) - len(shown)
    if more > 0:
        lines.append(f"… {more} more")
    body = "\n".join(lines)
    return f'<diagnostics file="{file}">\n{body}\n</diagnostics>'


__all__ = [
    "MAX_PER_FILE",
    "REPORTED_SEVERITIES",
    "SEVERITY",
    "Diagnostic",
    "counts",
    "pretty",
    "report",
    "severity_of",
]
