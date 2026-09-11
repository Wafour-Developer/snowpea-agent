#!/usr/bin/env python3
"""Generate the protocol artifacts from ``snowpea_core.server.protocol``.

Single source of truth: ``core/snowpea_core/server/protocol.py``.
Generated artifacts (never hand-edit):

* ``sdk/src/protocol.ts``  — TypeScript bindings for the SDK and the TUI.
* ``docs/protocol.md``     — human-readable protocol reference.

Usage::

    uv run python scripts/gen_protocol.py              # write both artifacts
    uv run python scripts/gen_protocol.py --check      # fail if stale (CI)
    uv run python scripts/gen_protocol.py --diff A B   # compare two git refs
    uv run python scripts/gen_protocol.py --schema f   # generate from a JSON dump

Exit codes: ``0`` ok, ``1`` stale/different, ``2`` usage or import error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "core"
TS_OUT = REPO_ROOT / "sdk" / "src" / "protocol.ts"
MD_OUT = REPO_ROOT / "docs" / "protocol.md"
PROTOCOL_PY = "core/snowpea_core/server/protocol.py"

GENERATED_BANNER_TS = (
    "// GENERATED — do not edit.\n"
    "// Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py.\n"
    "// Re-run `uv run python scripts/gen_protocol.py` after changing the protocol.\n"
)
GENERATED_BANNER_MD = (
    "<!-- GENERATED — do not edit. "
    "Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py. -->\n"
)

DEFAULT_TRANSPORT: dict[str, Any] = {
    "ws": "/ws",
    "http": {"health": "/health", "version": "/version", "schema": "/protocol.json"},
}

DEFAULT_ERROR_CODES = [
    "approval_denied",
    "approval_timeout",
    "internal",
    "invalid_params",
    "login_unsupported",
    "mode_denied",
    "not_found",
    "not_implemented",
    "protocol_incompatible",
    "tool_inactive",
    "unauthorized",
]

MAX_INLINE_DEPTH = 12


# --------------------------------------------------------------------------
# schema acquisition + normalisation
# --------------------------------------------------------------------------


class SchemaUnavailable(RuntimeError):
    """``snowpea_core.server.protocol.dump_schema`` is not importable yet."""


def load_schema(schema_file: Path | None = None) -> dict[str, Any]:
    if schema_file is not None:
        return normalize(json.loads(schema_file.read_text(encoding="utf-8")))
    if str(CORE_DIR) not in sys.path:
        sys.path.insert(0, str(CORE_DIR))
    try:
        from snowpea_core.server import protocol as protocol_mod  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - import-time failure path
        raise SchemaUnavailable(
            f"cannot import snowpea_core.server.protocol ({exc}); "
            f"is {PROTOCOL_PY} written yet?"
        ) from exc
    dump = getattr(protocol_mod, "dump_schema", None)
    if dump is None:
        raise SchemaUnavailable(
            f"snowpea_core.server.protocol has no dump_schema(); expected it in {PROTOCOL_PY}"
        )
    return normalize(dump())


def normalize(raw: Any) -> dict[str, Any]:
    """Accept the tolerated shapes of ``dump_schema()`` and return one canonical dict."""
    if not isinstance(raw, dict):
        raise SchemaUnavailable(f"dump_schema() returned {type(raw).__name__}, expected dict")

    def pick(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in raw and raw[name] is not None:
                return raw[name]
        return default

    protocol_version = str(pick("protocolVersion", "protocol_version", "version", default="0.0.0"))
    server_version = str(pick("serverVersion", "server_version", default=protocol_version))

    methods_raw = pick("methods", default={})
    methods: dict[str, dict[str, Any]] = {}
    if isinstance(methods_raw, list):
        for entry in methods_raw:
            name = entry.get("name")
            if name:
                methods[str(name)] = dict(entry)
    elif isinstance(methods_raw, dict):
        for name, entry in methods_raw.items():
            methods[str(name)] = dict(entry) if isinstance(entry, dict) else {}
    for name, entry in methods.items():
        entry.pop("name", None)
        entry.setdefault("direction", "s2c" if name.endswith(".request") else "c2s")
        entry["params"] = _as_schema(entry.get("params"))
        entry["result"] = _as_schema(entry.get("result"))
        entry["summary"] = str(entry.get("summary") or entry.get("description") or "")

    events_raw = pick("events", "notifications", default={})
    events: dict[str, dict[str, Any]] = {}
    if isinstance(events_raw, list):
        for entry in events_raw:
            name = entry.get("name")
            if name:
                events[str(name)] = _as_schema(entry.get("payload") or entry.get("schema") or entry)
    elif isinstance(events_raw, dict):
        for name, entry in events_raw.items():
            if isinstance(entry, dict) and ("payload" in entry or "schema" in entry):
                entry = entry.get("payload") or entry.get("schema")
            events[str(name)] = _as_schema(entry)

    kinds_raw = pick("sessionEventKinds", "session_event_kinds", "eventKinds", default={})
    kinds: dict[str, dict[str, Any]] = {}
    if isinstance(kinds_raw, dict):
        for name, entry in kinds_raw.items():
            kinds[str(name)] = _as_schema(entry)
    elif isinstance(kinds_raw, list):
        for entry in kinds_raw:
            if isinstance(entry, str):
                kinds[entry] = {"type": "object", "additionalProperties": True}
            elif isinstance(entry, dict) and entry.get("kind"):
                kinds[str(entry["kind"])] = _as_schema(entry.get("payload") or entry)

    codes = pick("errorCodes", "error_codes", default=None)
    if isinstance(codes, dict):
        codes = list(codes)
    error_codes = sorted({str(c) for c in codes}) if codes else list(DEFAULT_ERROR_CODES)

    transport = pick("transport", default=None)
    if not isinstance(transport, dict):
        transport = {}
    merged_transport = {
        "ws": str(transport.get("ws") or DEFAULT_TRANSPORT["ws"]),
        "http": {**DEFAULT_TRANSPORT["http"], **(transport.get("http") or {})},
    }

    capabilities = pick("capabilities", default=[])
    capabilities = sorted({str(c) for c in capabilities}) if capabilities else []

    return {
        "protocolVersion": protocol_version,
        "serverVersion": server_version,
        "methods": methods,
        "events": events,
        "sessionEventKinds": kinds,
        "errorCodes": error_codes,
        "transport": merged_transport,
        "capabilities": capabilities,
    }


def _as_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"type": "object", "additionalProperties": True}


# --------------------------------------------------------------------------
# JSON Schema → TypeScript
# --------------------------------------------------------------------------


def _resolve(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Follow local ``$ref`` pointers (``#/$defs/X``, ``#/definitions/X``)."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < MAX_INLINE_DEPTH:
        ref = schema["$ref"]
        seen += 1
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        node: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        extra = {k: v for k, v in schema.items() if k != "$ref"}
        schema = {**node, **extra} if isinstance(node, dict) else {}
    return schema if isinstance(schema, dict) else {}


def _lit(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


def ts_type(schema: Any, root: dict[str, Any], depth: int = 0, indent: str = "") -> str:
    if depth > MAX_INLINE_DEPTH:
        return "unknown"
    if schema is True or schema == {}:
        return "unknown"
    if schema is False:
        return "never"
    if not isinstance(schema, dict):
        return "unknown"

    schema = _resolve(schema, root, depth)
    if not schema:
        return "unknown"

    if "const" in schema:
        return _lit(schema["const"])
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return " | ".join(_lit(v) for v in schema["enum"])

    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            parts: list[str] = []
            for variant in variants:
                rendered = ts_type(variant, root, depth + 1, indent)
                if rendered not in parts:
                    parts.append(rendered)
            if len(parts) == 1:
                return parts[0]
            return " | ".join(parts)

    all_of = schema.get("allOf")
    if isinstance(all_of, list) and all_of:
        parts = [ts_type(v, root, depth + 1, indent) for v in all_of]
        uniq = [p for i, p in enumerate(parts) if p not in parts[:i]]
        return " & ".join(uniq) if len(uniq) > 1 else uniq[0]

    kind = schema.get("type")
    if isinstance(kind, list):
        parts = []
        for one in kind:
            rendered = ts_type({**schema, "type": one}, root, depth + 1, indent)
            if rendered not in parts:
                parts.append(rendered)
        return " | ".join(parts)

    if kind == "array":
        items = schema.get("items")
        if isinstance(items, list):
            return "[" + ", ".join(ts_type(i, root, depth + 1, indent) for i in items) + "]"
        inner = ts_type(items, root, depth + 1, indent) if items is not None else "unknown"
        return f"({inner})[]" if re.search(r"[ |&]", inner) else f"{inner}[]"

    if kind == "object" or "properties" in schema or "additionalProperties" in schema:
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            return render_object(schema, root, depth, indent)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return f"Record<string, {ts_type(extra, root, depth + 1, indent)}>"
        return "Record<string, unknown>"

    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(str(kind), "unknown")


def prop_lines(schema: dict[str, Any], root: dict[str, Any], depth: int, indent: str) -> list[str]:
    schema = _resolve(schema, root, depth)
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    required = set(schema.get("required") or [])
    inner = indent + "  "
    lines: list[str] = []
    for name in sorted(props):
        sub = props[name]
        rendered = ts_type(sub, root, depth + 1, inner)
        doc = ""
        if isinstance(sub, dict):
            resolved = _resolve(sub, root, depth)
            doc = str(resolved.get("description") or sub.get("description") or "").strip()
        if doc:
            lines.append(f"{inner}/** {' '.join(doc.split())} */")
        key = name if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) else json.dumps(name)
        opt = "" if name in required else "?"
        lines.append(f"{inner}{key}{opt}: {rendered};")
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict) and extra:
        lines.append(f"{inner}[key: string]: {ts_type(extra, root, depth + 1, inner)};")
    return lines


def render_object(schema: dict[str, Any], root: dict[str, Any], depth: int, indent: str) -> str:
    lines = prop_lines(schema, root, depth, indent)
    if not lines:
        return "Record<string, never>"
    return "{\n" + "\n".join(lines) + f"\n{indent}}}"


def declare(name: str, schema: dict[str, Any], doc: str = "") -> str:
    root = schema if isinstance(schema, dict) else {}
    resolved = _resolve(root, root)
    props = resolved.get("properties")
    head = f"/** {' '.join(doc.split())} */\n" if doc.strip() else ""
    if isinstance(props, dict) and props:
        body = "\n".join(prop_lines(resolved, root, 0, ""))
        return f"{head}export interface {name} {{\n{body}\n}}\n"
    rendered = ts_type(root, root, 0, "")
    if rendered == "Record<string, never>":
        return f"{head}export type {name} = Record<string, never>;\n"
    return f"{head}export type {name} = {rendered};\n"


def type_name(rpc_name: str, suffix: str) -> str:
    parts = re.split(r"[._\-]+", rpc_name)
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return f"{camel}{suffix}"


# --------------------------------------------------------------------------
# TypeScript emitter
# --------------------------------------------------------------------------


def render_ts(schema: dict[str, Any]) -> str:
    methods: dict[str, Any] = schema["methods"]
    events: dict[str, Any] = schema["events"]
    kinds: dict[str, Any] = schema["sessionEventKinds"]
    out: list[str] = [GENERATED_BANNER_TS, ""]

    out.append(f'export const PROTOCOL_VERSION = {json.dumps(schema["protocolVersion"])};')
    out.append(f'export const WS_PATH = {json.dumps(schema["transport"]["ws"])};')
    http = schema["transport"]["http"]
    http_body = "\n".join(f"  {k}: {json.dumps(v)}," for k, v in sorted(http.items()))
    out.append(f"export const HTTP_ENDPOINTS = {{\n{http_body}\n}} as const;")
    out.append("")

    codes = schema["errorCodes"]
    out.append("/** Values of JSON-RPC `error.data.code`. */")
    out.append("export type ErrorCode =\n" + "\n".join(f"  | {_lit(c)}" for c in codes) + ";")
    out.append(
        "export const ERROR_CODES: readonly ErrorCode[] = [\n"
        + "\n".join(f"  {_lit(c)}," for c in codes)
        + "\n];"
    )
    out.append("")

    out.append("// ---------------------------------------------------------------------------")
    out.append("// Method params / results")
    out.append("// ---------------------------------------------------------------------------")
    out.append("")
    for name in sorted(methods):
        entry = methods[name]
        doc = entry.get("summary") or ""
        out.append(declare(type_name(name, "Params"), entry["params"], f"`{name}` params. {doc}"))
        out.append(declare(type_name(name, "Result"), entry["result"], f"`{name}` result."))

    out.append("// ---------------------------------------------------------------------------")
    out.append("// Event payloads")
    out.append("// ---------------------------------------------------------------------------")
    out.append("")
    for name in sorted(events):
        out.append(declare(type_name(name, "Payload"), events[name], f"`{name}` notification payload."))

    if kinds:
        out.append(
            "// ---------------------------------------------------------------------------"
        )
        out.append("// session.event payloads by kind")
        out.append(
            "// ---------------------------------------------------------------------------"
        )
        out.append("")
        for name in sorted(kinds):
            out.append(
                declare(
                    type_name(name, "EventPayload"),
                    kinds[name],
                    f"Payload of `session.event` with kind `{name}`.",
                )
            )
        body = "\n".join(
            f"  {json.dumps(k)}: {type_name(k, 'EventPayload')};" for k in sorted(kinds)
        )
        out.append("/** Maps every `session.event` kind to its payload type. */")
        out.append(f"export interface SessionEventKindMap {{\n{body}\n}}\n")
        out.append("export type SessionEventKind = keyof SessionEventKindMap;")
        out.append(
            "export const SESSION_EVENT_KINDS: readonly SessionEventKind[] = [\n"
            + "\n".join(f"  {_lit(k)}," for k in sorted(kinds))
            + "\n];"
        )
        out.append("")

    out.append("// ---------------------------------------------------------------------------")
    out.append("// Maps")
    out.append("// ---------------------------------------------------------------------------")
    out.append("")
    map_body = []
    for name in sorted(methods):
        map_body.append(
            f"  {json.dumps(name)}: {{ params: {type_name(name, 'Params')};"
            f" result: {type_name(name, 'Result')} }};"
        )
    out.append("/** Every JSON-RPC method, with its params and result types. */")
    out.append("export interface MethodMap {\n" + "\n".join(map_body) + "\n}\n")
    out.append("export type MethodName = keyof MethodMap;")
    out.append("export type MethodParams<M extends MethodName> = MethodMap[M][\"params\"];")
    out.append("export type MethodResult<M extends MethodName> = MethodMap[M][\"result\"];")
    out.append("")

    c2s = [n for n in sorted(methods) if methods[n].get("direction") != "s2c"]
    s2c = [n for n in sorted(methods) if methods[n].get("direction") == "s2c"]
    out.append("/** Methods the client calls on the server. */")
    out.append(
        "export type ClientMethod =\n" + "\n".join(f"  | {_lit(n)}" for n in c2s) + ";"
        if c2s
        else "export type ClientMethod = never;"
    )
    out.append("/** Methods the server calls on the client (bidirectional JSON-RPC). */")
    out.append(
        "export type ServerMethod =\n" + "\n".join(f"  | {_lit(n)}" for n in s2c) + ";"
        if s2c
        else "export type ServerMethod = never;"
    )
    out.append("")

    ev_body = "\n".join(f"  {json.dumps(n)}: {type_name(n, 'Payload')};" for n in sorted(events))
    out.append("/** Every server→client notification, with its payload type. */")
    out.append(
        f"export interface EventMap {{\n{ev_body}\n}}\n" if events else "export interface EventMap {}\n"
    )
    out.append("export type EventName = keyof EventMap;")
    out.append(
        "export const EVENT_NAMES: readonly EventName[] = [\n"
        + "\n".join(f"  {_lit(n)}," for n in sorted(events))
        + "\n];"
        if events
        else "export const EVENT_NAMES: readonly EventName[] = [];"
    )
    out.append("")

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


# --------------------------------------------------------------------------
# Markdown emitter
# --------------------------------------------------------------------------


def md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def md_rows(schema: dict[str, Any], root: dict[str, Any]) -> list[str]:
    resolved = _resolve(schema, root)
    props = resolved.get("properties")
    if not isinstance(props, dict) or not props:
        extra = resolved.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return [f"| `*` | `{md_escape(ts_type(extra, root))}` | – | free-form object |"]
        return []
    required = set(resolved.get("required") or [])
    rows = []
    for name in sorted(props):
        sub = props[name]
        rendered = ts_type(sub, root)
        rendered = " ".join(rendered.split())
        doc = ""
        if isinstance(sub, dict):
            doc = str(_resolve(sub, root).get("description") or "")
        req = "yes" if name in required else "no"
        rows.append(f"| `{name}` | `{md_escape(rendered)}` | {req} | {md_escape(doc)} |")
    return rows


def md_table(schema: dict[str, Any], empty: str) -> str:
    rows = md_rows(schema, schema if isinstance(schema, dict) else {})
    if not rows:
        return f"{empty}\n"
    head = "| field | type | required | description |\n|---|---|---|---|\n"
    return head + "\n".join(rows) + "\n"


def render_md(schema: dict[str, Any]) -> str:
    methods: dict[str, Any] = schema["methods"]
    events: dict[str, Any] = schema["events"]
    kinds: dict[str, Any] = schema["sessionEventKinds"]
    ws = schema["transport"]["ws"]
    http = schema["transport"]["http"]

    out: list[str] = [GENERATED_BANNER_MD, "# Snowpea protocol", ""]
    out.append(f"- **Protocol version:** `{schema['protocolVersion']}` (semver)")
    out.append(f"- **Source of truth:** `{PROTOCOL_PY}`")
    out.append("- **Generator:** `uv run python scripts/gen_protocol.py`")
    out.append(
        "- **Bindings:** `sdk/src/protocol.ts` (generated alongside this file — never hand-edit)"
    )
    out.append("")

    out.append("## Transport")
    out.append("")
    out.append(
        "The daemon listens on a loopback-only port chosen at start-up and records it in "
        "`$SNOWPEA_HOME/daemon.json` as `{port, pid, token, startedAt, protocolVersion}`."
    )
    out.append("")
    out.append("| surface | endpoint | purpose |")
    out.append("|---|---|---|")
    out.append(f"| WebSocket | `ws://127.0.0.1:<port>{ws}` | JSON-RPC 2.0, bidirectional |")
    for key in sorted(http):
        purpose = {
            "health": "liveness probe",
            "version": "server and protocol versions",
            "schema": "this schema as JSON",
        }.get(key, key)
        out.append(f"| HTTP `GET` | `http://127.0.0.1:<port>{http[key]}` | {purpose} |")
    out.append("")
    out.append(
        "Every state-changing method lives on the WebSocket only; the HTTP endpoints are "
        "read-only operational helpers."
    )
    out.append("")

    out.append("## Handshake")
    out.append("")
    out.append(
        "Immediately after connecting, the client calls `system.hello` with the daemon token, "
        "its own `clientVersion`, and the `protocolVersion` it was built against. Any other "
        "method before a successful `system.hello` fails with `unauthorized`. A mismatched "
        "major version is rejected with `protocol_incompatible`."
    )
    out.append("")
    out.append("```json")
    out.append(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "system.hello",
                "params": {
                    "token": "<contents of $SNOWPEA_HOME/token>",
                    "clientVersion": "0.1.0",
                    "protocolVersion": schema["protocolVersion"],
                },
            },
            indent=2,
        )
    )
    out.append("```")
    out.append("")
    if schema["capabilities"]:
        out.append("Server capabilities advertised in the `system.hello` result:")
        out.append("")
        for cap in schema["capabilities"]:
            out.append(f"- `{cap}`")
        out.append("")

    out.append("## Method index")
    out.append("")
    out.append("| method | direction | summary |")
    out.append("|---|---|---|")
    for name in sorted(methods):
        entry = methods[name]
        direction = "server → client" if entry.get("direction") == "s2c" else "client → server"
        out.append(f"| [`{name}`](#{anchor(name)}) | {direction} | {md_escape(entry.get('summary', ''))} |")
    out.append("")

    out.append("## Methods")
    out.append("")
    for name in sorted(methods):
        entry = methods[name]
        direction = "server → client" if entry.get("direction") == "s2c" else "client → server"
        out.append(f"### `{name}`")
        out.append("")
        out.append(f"*Direction:* {direction}")
        out.append("")
        if entry.get("summary"):
            out.append(md_escape(entry["summary"]))
            out.append("")
        out.append("**Params**")
        out.append("")
        out.append(md_table(entry["params"], "_No params (send `{}`)._"))
        out.append("**Result**")
        out.append("")
        out.append(md_table(entry["result"], "_No result fields._"))

    out.append("## Notifications")
    out.append("")
    if not events:
        out.append("_None._")
        out.append("")
    for name in sorted(events):
        out.append(f"### `{name}`")
        out.append("")
        out.append(md_table(events[name], "_No payload fields._"))

    if kinds:
        out.append("## `session.event` kinds")
        out.append("")
        out.append(
            "Every session event carries a monotonically increasing per-session `seq`. "
            "After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed."
        )
        out.append("")
        for name in sorted(kinds):
            out.append(f"### kind `{name}`")
            out.append("")
            out.append(md_table(kinds[name], "_No payload fields._"))

    out.append("## Error codes")
    out.append("")
    out.append("Returned as the string `error.data.code` of a JSON-RPC error response.")
    out.append("")
    out.append("| code | meaning |")
    out.append("|---|---|")
    meanings = {
        "approval_denied": "The user denied the approval request.",
        "approval_timeout": "No approval arrived before `approvals.timeoutSec` elapsed.",
        "internal": "Unexpected server-side failure.",
        "invalid_params": "Params failed schema validation.",
        "login_unsupported": "The vendor does not support the requested login method.",
        "mode_denied": "The session mode forbids this tool or action.",
        "not_found": "No such session, request, job, or agent.",
        "not_implemented": "Defined in the schema but not implemented in this milestone.",
        "protocol_incompatible": "Client and server protocol majors differ.",
        "tool_inactive": "The tool exists but is disabled for this session.",
        "unauthorized": "Missing or invalid token, or a call before `system.hello`.",
    }
    for code in schema["errorCodes"]:
        out.append(f"| `{code}` | {meanings.get(code, '')} |")
    out.append("")

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


def anchor(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def artifacts(schema: dict[str, Any]) -> dict[Path, str]:
    return {TS_OUT: render_ts(schema), MD_OUT: render_md(schema)}


def cmd_write(schema: dict[str, Any]) -> int:
    for path, text in artifacts(schema).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(text, encoding="utf-8")
        state = "unchanged" if old == text else ("updated" if old is not None else "created")
        print(f"{state}: {path.relative_to(REPO_ROOT)}")
    return 0


def cmd_check(schema: dict[str, Any]) -> int:
    stale = False
    for path, expected in artifacts(schema).items():
        rel = path.relative_to(REPO_ROOT)
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual == expected:
            print(f"ok: {rel}")
            continue
        stale = True
        print(f"STALE: {rel}", file=sys.stderr)
        diff = difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=f"a/{rel} (committed)",
            tofile=f"b/{rel} (generated)",
        )
        sys.stderr.writelines(diff)
        sys.stderr.write("\n")
    if stale:
        print(
            "\nGenerated protocol artifacts are out of date. "
            "Run `uv run python scripts/gen_protocol.py` and commit the result.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_diff(ref_a: str, ref_b: str) -> int:
    paths = [
        str(MD_OUT.relative_to(REPO_ROOT)),
        str(TS_OUT.relative_to(REPO_ROOT)),
    ]
    base = ["git", "-C", str(REPO_ROOT), "diff"]
    stat = subprocess.run(
        [*base, "--stat", ref_a, ref_b, "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if stat.returncode != 0:
        sys.stderr.write(stat.stderr)
        return 2
    if not stat.stdout.strip():
        print(f"protocol artifacts are identical between {ref_a} and {ref_b}")
        return 0
    print(f"protocol artifacts differ between {ref_a} and {ref_b}:\n")
    print(stat.stdout.rstrip())
    print()
    full = subprocess.run(
        [*base, ref_a, ref_b, "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    print(full.stdout.rstrip())
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen_protocol.py",
        description="Generate sdk/src/protocol.ts and docs/protocol.md from server/protocol.py.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and fail with a diff if the committed files are stale",
    )
    group.add_argument(
        "--diff",
        nargs=2,
        metavar=("REF_A", "REF_B"),
        help="show how the committed protocol artifacts changed between two git refs",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="read the schema from a JSON file instead of importing protocol.py",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="dump the normalised schema as JSON and exit",
    )
    args = parser.parse_args(argv)

    if args.diff:
        return cmd_diff(args.diff[0], args.diff[1])

    try:
        schema = load_schema(args.schema)
    except SchemaUnavailable as exc:
        print(f"gen_protocol: {exc}", file=sys.stderr)
        return 2

    if args.print_schema:
        json.dump(schema, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    if args.check:
        return cmd_check(schema)
    return cmd_write(schema)


if __name__ == "__main__":
    raise SystemExit(main())
