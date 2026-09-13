#!/usr/bin/env python3
"""A minimal language server, for the LSP tests.

Real language servers are slow, optional and platform-dependent, so the LSP
tests drive this instead: it speaks the same ``Content-Length`` framing and the
same handful of methods, over stdio, with no dependencies.

It serves a toy language where a line reading ``ERROR <text>`` is an error and
``WARN <text>`` is a warning, published after every ``didOpen``/``didChange``.
``definition``, ``references``, ``hover``, ``documentSymbol``,
``workspace/symbol``, ``prepareRename`` and ``rename`` all answer from the same
plain-text scan, which is enough to check that the client, the manager and the
tools carry LSP shapes through correctly.

Environment knobs the tests use:

``FAKE_LSP_CRASH_AFTER``
    Exit abruptly after this many ``initialize`` requests have been served
    (``1`` means: come up once, then die on the next request), for AC-45.
``FAKE_LSP_NO_PUBLISH``
    Never push ``publishDiagnostics``; answer ``textDocument/diagnostic``
    instead, so the pull path is exercised.
``FAKE_LSP_SLOW_INITIALIZE``
    Sleep this many seconds before answering ``initialize``, for the
    three-second start budget.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

DOCUMENTS: dict[str, str] = {}
SERVED = 0


def read_message() -> dict[str, Any] | None:
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        header = line.strip()
        if not header:
            break
        name, _, value = header.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def send(message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    sys.stdout.buffer.flush()


def reply(request_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def path_to_uri(path: Path) -> str:
    return "file://" + pathname2url(str(path))


def diagnostics_for(text: str) -> list[dict[str, Any]]:
    found = []
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        for prefix, severity, code in (("ERROR", 1, "fake-error"), ("WARN", 2, "fake-warn")):
            if stripped.startswith(prefix):
                found.append(
                    {
                        "range": {
                            "start": {"line": number, "character": line.index(prefix)},
                            "end": {"line": number, "character": len(line)},
                        },
                        "severity": severity,
                        "code": code,
                        "source": "fake",
                        "message": stripped[len(prefix) :].strip() or prefix.lower(),
                    }
                )
    return found


def publish(uri: str, text: str) -> None:
    if os.environ.get("FAKE_LSP_NO_PUBLISH"):
        return
    send(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diagnostics_for(text)},
        }
    )


def word_at(text: str, line: int, character: int) -> str:
    lines = text.splitlines()
    if line >= len(lines):
        return ""
    row = lines[line]
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", row):
        if match.start() <= character < match.end():
            return match.group(0)
    return ""


def occurrences(text: str, word: str) -> list[tuple[int, int]]:
    out = []
    for number, row in enumerate(text.splitlines()):
        for match in re.finditer(rf"\b{re.escape(word)}\b", row):
            out.append((number, match.start()))
    return out


def location(uri: str, line: int, character: int, length: int) -> dict[str, Any]:
    return {
        "uri": uri,
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + length},
        },
    }


def definitions(word: str) -> list[dict[str, Any]]:
    """The first line that reads ``def <word>`` in any open document."""
    out = []
    for uri, text in DOCUMENTS.items():
        for number, row in enumerate(text.splitlines()):
            match = re.match(rf"\s*def\s+{re.escape(word)}\b", row)
            if match:
                out.append(location(uri, number, row.index(word), len(word)))
    return out


def references(word: str) -> list[dict[str, Any]]:
    out = []
    for uri, text in DOCUMENTS.items():
        for line, character in occurrences(text, word):
            out.append(location(uri, line, character, len(word)))
    return out


def symbols(uri: str) -> list[dict[str, Any]]:
    out = []
    for number, row in enumerate(DOCUMENTS.get(uri, "").splitlines()):
        match = re.match(r"\s*def\s+([A-Za-z_][A-Za-z0-9_]*)", row)
        if match:
            name = match.group(1)
            out.append(
                {
                    "name": name,
                    "kind": 12,
                    "range": {
                        "start": {"line": number, "character": 0},
                        "end": {"line": number, "character": len(row)},
                    },
                    "selectionRange": {
                        "start": {"line": number, "character": row.index(name)},
                        "end": {"line": number, "character": row.index(name) + len(name)},
                    },
                }
            )
    return out


def handle(message: dict[str, Any]) -> None:
    global SERVED
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")

    if method == "initialize":
        SERVED += 1
        slow = float(os.environ.get("FAKE_LSP_SLOW_INITIALIZE", "0") or 0)
        if slow:
            time.sleep(slow)
        reply(
            request_id,
            {
                "capabilities": {
                    "textDocumentSync": 1,
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "documentSymbolProvider": True,
                    "workspaceSymbolProvider": True,
                    "renameProvider": {"prepareProvider": True},
                    **(
                        {"diagnosticProvider": {"interFileDependencies": False}}
                        if os.environ.get("FAKE_LSP_NO_PUBLISH")
                        else {}
                    ),
                },
                "serverInfo": {"name": "fake", "version": "1"},
            },
        )
        return

    crash_after = int(os.environ.get("FAKE_LSP_CRASH_AFTER", "0") or 0)
    if crash_after and method not in ("initialized", "shutdown", "exit"):
        os._exit(9)

    if method in ("textDocument/didOpen", "textDocument/didChange"):
        document = params.get("textDocument") or {}
        uri = str(document.get("uri", ""))
        if method == "textDocument/didOpen":
            text = str(document.get("text", ""))
        else:
            changes = params.get("contentChanges") or [{}]
            text = str(changes[-1].get("text", ""))
        DOCUMENTS[uri] = text
        publish(uri, text)
        return

    if method == "textDocument/didClose":
        DOCUMENTS.pop(str((params.get("textDocument") or {}).get("uri", "")), None)
        return

    if request_id is None:
        return

    uri = str((params.get("textDocument") or {}).get("uri", ""))
    text = DOCUMENTS.get(uri, "")
    position = params.get("position") or {}
    word = word_at(text, int(position.get("line", 0)), int(position.get("character", 0)))

    if method == "textDocument/diagnostic":
        reply(request_id, {"kind": "full", "items": diagnostics_for(text)})
    elif method == "textDocument/definition":
        reply(request_id, definitions(word))
    elif method == "textDocument/references":
        reply(request_id, references(word))
    elif method == "textDocument/hover":
        reply(
            request_id,
            {"contents": {"kind": "markdown", "value": f"`{word}`: fake symbol"}} if word else None,
        )
    elif method == "textDocument/documentSymbol":
        reply(request_id, symbols(uri))
    elif method == "workspace/symbol":
        query = str(params.get("query", ""))
        out = []
        for document_uri in DOCUMENTS:
            for symbol in symbols(document_uri):
                if query.lower() in symbol["name"].lower():
                    out.append(
                        {
                            "name": symbol["name"],
                            "kind": symbol["kind"],
                            "location": {"uri": document_uri, "range": symbol["range"]},
                        }
                    )
        reply(request_id, out)
    elif method == "textDocument/prepareRename":
        reply(request_id, {"placeholder": word} if word else None)
    elif method == "textDocument/rename":
        new_name = str(params.get("newName", ""))
        changes: dict[str, list[dict[str, Any]]] = {}
        if word:
            for document_uri, document in DOCUMENTS.items():
                edits = [
                    {
                        "range": {
                            "start": {"line": line, "character": character},
                            "end": {"line": line, "character": character + len(word)},
                        },
                        "newText": new_name,
                    }
                    for line, character in occurrences(document, word)
                ]
                if edits:
                    changes[document_uri] = edits
        reply(request_id, {"changes": changes} if changes else None)
    elif method == "shutdown":
        reply(request_id, None)
    else:
        reply(request_id, None)


def main() -> None:
    while True:
        try:
            message = read_message()
        except (ValueError, OSError):
            return
        if message is None:
            return
        method = message.get("method")
        handle(message)
        if method == "exit":
            return


if __name__ == "__main__":
    main()
