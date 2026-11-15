"""Assistant presets: mail and calendar in one step (M19 WP8, ``mcp.preset.*``).

A preset (:data:`snowpea_core.tools.mcp_catalog.PRESETS`) is one or more MCP
servers plus the person's sign-in, which of their tools only read, and the
schedule templates (recipes) worth turning on.  :func:`apply` does all of it as
one transaction:

* the servers go into the global (or project) ``.mcp.json`` through the same
  code ``mcp.add`` uses (probe first, nothing written when the server does not
  start);
* ``mcp.permissions`` gets the server's own tag ``send`` and, per read tool, a
  ``"<server>/<tool>"`` tag ``read`` - so reading runs unasked while sending,
  drafting and every calendar change ask the person, and Jarvis classifies those
  as ``secret_send`` (guarded);
* a secret input (a client secret) goes to ``credentials.json`` and the entry
  refers to it as ``${NAME}``;
* each chosen recipe becomes a ``jarvis-task`` scheduler job.

Anything that fails undoes everything done so far.  What was there before is
kept in ``$SNOWPEA_HOME/jarvis/presets.json`` (mode 0600), so :func:`remove`
puts the previous state back: servers, permission tags, credentials and recipe
jobs.  Applying again with the same inputs changes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, mcp_handlers
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    McpAddParams,
    McpAddResult,
    McpPresetApplyParams,
    McpPresetApplyResult,
    McpPresetCheckResult,
    McpPresetInfo,
    McpPresetInput,
    McpPresetListResult,
    McpPresetRecipe,
    McpPresetRemoveResult,
    McpPresetServer,
    McpPresetServerCheck,
    McpPresetSignIn,
)
from snowpea_core.tools import mcp_catalog, mcp_config, mcp_oauth
from snowpea_core.tools.mcp_client import tool_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.jarvis.presets")

STATE_FILE = "presets.json"
VALUE_MAX = 2000

#: One apply or remove at a time per daemon.
_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# what was applied
# ---------------------------------------------------------------------------


def _book_path(core: Core) -> Path:
    return Path(core.paths.home) / "jarvis" / STATE_FILE


def load_book(core: Core) -> dict[str, dict[str, Any]]:
    path = _book_path(core)
    if path.exists():
        # Owner-only on every open, not only when written (review L3).
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    presets = raw.get("presets") if isinstance(raw, dict) else None
    return {k: v for k, v in presets.items() if isinstance(v, dict)} if presets else {}


def save_book(core: Core, book: dict[str, dict[str, Any]]) -> None:
    path = _book_path(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with contextlib.suppress(OSError, AttributeError):
            os.fchmod(fd, 0o600)  # a leftover .tmp keeps its old mode otherwise
        os.write(fd, json.dumps({"presets": book}, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def server_name(prefix: str, server: dict[str, Any]) -> str:
    suffix = str(server.get("suffix") or "")
    return f"{prefix}-{suffix}" if suffix else prefix


def _secret_names(preset: dict[str, Any]) -> set[str]:
    return {str(i["name"]) for i in preset["inputs"] if i.get("secret")}


def render_entry(
    template: dict[str, Any], values: dict[str, str], secrets: set[str]
) -> dict[str, Any]:
    """The ``.mcp.json`` entry for ``template``: ``{NAME}`` filled, unset keys dropped."""

    def fill(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                rendered = fill(value)
                if rendered is None or (isinstance(rendered, dict) and not rendered and value):
                    continue
                out[key] = rendered
            return out
        if isinstance(node, str):
            if node.startswith("{") and node.endswith("}") and node[1:-1].isidentifier():
                return values.get(node[1:-1]) or None
            if node.startswith("${") and node.endswith("}"):
                return node if node[2:-1] in values and node[2:-1] in secrets else None
            return node
        return deepcopy(node)

    return fill(template)  # type: ignore[no-any-return]


def desired_permissions(names: list[tuple[str, dict[str, Any]]]) -> dict[str, str]:
    table: dict[str, str] = {}
    for name, server in names:
        table[name] = "send"
        for tool in server["readTools"]:
            table[f"{name}/{tool}"] = "read"
    return table


def read_tool_names(name: str, server: dict[str, Any], kind: str | None = None) -> list[str]:
    """Registered-style names of the tools a read-only worker of ``kind`` may hold."""
    by_kind = server.get("workerTools") or {}
    tools: list[str] = (
        by_kind[kind] if kind and kind in by_kind else server["readTools"]
    )
    return [tool_name(name, str(t)) for t in tools]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _info(
    preset: dict[str, Any], record: dict[str, Any] | None, locale: str = "en"
) -> McpPresetInfo:
    servers = preset["servers"]
    first = servers[0]
    texts = mcp_catalog.PRESET_TEXTS.get(locale, {}).get(preset["id"], {})
    labels = texts.get("inputs", {})
    recipe_titles = texts.get("recipes", {})
    server_titles = texts.get("servers", {})
    return McpPresetInfo(
        id=preset["id"],
        title=texts.get("title") or preset["title"],
        description=texts.get("description") or preset["description"],
        category=preset["category"],
        auth=first["auth"],
        transport=first["transport"],
        entry=deepcopy(first["entry"]),
        inputs=[
            McpPresetInput(**{**i, "label": labels.get(i["name"]) or i["label"]})
            for i in preset["inputs"]
        ],
        readTools=[t for s in servers for t in s["readTools"]],
        sendTools=[t for s in servers for t in s["sendTools"]],
        recipes=[
            McpPresetRecipe(
                id=r["id"],
                title=recipe_titles.get(r["id"]) or r["title"],
                schedule=r["schedule"],
                prompt=r["prompt"],
                defaultEnabled=bool(r.get("defaultEnabled")),
            )
            for r in preset["recipes"]
        ],
        homepage=preset.get("homepage", ""),
        applied=record is not None,
        appliedServers=[s["name"] for s in (record or {}).get("servers", [])],
        servers=[
            McpPresetServer(
                suffix=str(s.get("suffix") or ""),
                title=server_titles.get(str(s.get("suffix") or "")) or s["title"],
                kind=s["kind"],
                transport=s["transport"],
                auth=s["auth"],
                signIn=s.get("signIn") or ("oauth" if s["auth"] == "oauth" else "none"),
                loginCommand=s.get("loginCommand"),
                entry=deepcopy(s["entry"]),
                readTools=list(s["readTools"]),
                sendTools=list(s["sendTools"]),
            )
            for s in servers
        ],
    )


def resolve_locale(locale: str | None) -> str:
    """``en`` or a locale :data:`mcp_catalog.PRESET_TEXTS` has (``ko``, ``ko-KR`` -> ``ko``)."""
    wanted = str(locale or "").strip().lower().replace("_", "-").split("-")[0]
    return wanted if wanted in mcp_catalog.PRESET_TEXTS else "en"


async def list_presets(core: Core, locale: str | None = None) -> McpPresetListResult:
    book = load_book(core)
    chosen = resolve_locale(locale)
    return McpPresetListResult(
        presets=[_info(p, book.get(p["id"]), chosen) for p in mcp_catalog.PRESETS],
        locale=chosen,
    )


# ---------------------------------------------------------------------------
# check (is the sign-in still good?)
# ---------------------------------------------------------------------------

#: How long a verify tool may take before the check says "unknown".
VERIFY_TIMEOUT_SEC = 20.0


def mask_account(value: str) -> str:
    """``j***@example.com`` / ``j***``: enough to recognise, never the whole id."""
    text = str(value or "").strip()
    if not text:
        return ""
    local, at, domain = text.partition("@")
    return f"{local[:1]}***@{domain}" if at else f"{text[:1]}***"


def _verdict(text: str) -> tuple[bool | None, str | None, str]:
    """``(authenticated, account, detail)`` read from a verify tool's answer."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        user = data.get("userData") if isinstance(data.get("userData"), dict) else data
        account = next(
            (str(user[k]) for k in ("userPrincipalName", "mail", "email", "username")
             if isinstance(user.get(k), str) and user.get(k)),
            None,
        )
        ok = data.get("success", data.get("authenticated", data.get("loggedIn")))
        if isinstance(ok, bool):
            return ok, account, "signed in" if ok else "not signed in"
    lowered = str(text or "").lower()
    if any(w in lowered for w in ("not logged in", "not authenticated", "login required",
                                  "not signed in")):
        return False, None, "not signed in"
    return None, None, "the server's answer did not say"


async def check(core: Core, preset_id: str) -> McpPresetCheckResult:
    """``mcp.preset.check``: per server, connected and still signed in (no secrets)."""
    from snowpea_core.tools.mcp_client import MANAGER

    preset = mcp_catalog.preset(preset_id)
    if preset is None:
        raise RpcError(errors.INVALID_PARAMS, f"unknown preset {preset_id!r}")
    record = load_book(core).get(preset["id"])
    if record is None:
        return McpPresetCheckResult(id=preset["id"], applied=False)
    by_name = {server_name(_prefix_of(record, preset), s): s for s in preset["servers"]}
    rows: list[McpPresetServerCheck] = []
    for entry in record["servers"]:
        name = entry["name"]
        spec = by_name.get(name) or {}
        server = MANAGER.get(name)
        connected = server is not None and server.running
        method = spec.get("signIn") or ("oauth" if spec.get("auth") == "oauth" else "none")
        authenticated: bool | None = None
        account: str | None = None
        detail = "connected" if connected else "not connected"
        if method == "oauth":
            row = mcp_handlers.declared(core, None).get(name)
            url = row.config.url if row is not None else None
            stored = bool(url) and mcp_oauth.TokenStore(core.paths.home).get(name, url) is not None
            refused = bool(getattr(server, "needs_auth", False))
            authenticated = stored and not refused
            detail = "signed in" if authenticated else "sign in with mcp.authorize"
        elif spec.get("verifyTool"):
            if not connected:
                detail = "not connected, so the sign-in cannot be checked"
            else:
                try:
                    answer = await asyncio.wait_for(
                        server.call(str(spec["verifyTool"]), {}),  # type: ignore[union-attr]
                        VERIFY_TIMEOUT_SEC,
                    )
                    authenticated, found, detail = _verdict(getattr(answer, "text", str(answer)))
                    account = mask_account(found) if found else None
                except Exception as exc:  # noqa: BLE001 - any failure is "cannot tell"
                    detail = f"could not check: {type(exc).__name__}"
            if authenticated is False and spec.get("loginCommand"):
                detail += f"; run once on the daemon's machine: {spec['loginCommand']}"
        rows.append(McpPresetServerCheck(server=name, connected=connected,
                                         authenticated=authenticated, account=account,
                                         detail=detail))
    return McpPresetCheckResult(id=preset["id"], applied=True, servers=rows)


# ---------------------------------------------------------------------------
# settings and credentials
# ---------------------------------------------------------------------------


async def _write_permissions(core: Core, changes: dict[str, str | None]) -> None:
    """Set (or, for ``None``, drop) keys of ``mcp.permissions`` in one settings write."""
    if not changes:
        return
    merged = core.settings.model_dump(mode="json")
    mcp = dict(merged.get("mcp") or {})
    table = dict(mcp.get("permissions") or {})
    for key, value in changes.items():
        if value is None:
            table.pop(key, None)
        else:
            table[key] = value
    mcp["permissions"] = table
    merged["mcp"] = mcp
    try:
        updated = Settings.model_validate(merged)
    except ValidationError as exc:  # pragma: no cover - tags are plain strings
        raise RpcError(errors.MCP_INVALID, f"permissions: {exc}") from exc
    updated.save(core.paths)
    await core.adopt_settings(updated, ["mcp"])


def _permission_table(core: Core) -> dict[str, str]:
    table = getattr(getattr(core.settings, "mcp", None), "permissions", None) or {}
    return dict(table)


def _credentials(core: Core) -> CredentialStore:
    return CredentialStore(core.paths)


def _restore_credentials(core: Core, previous: dict[str, Any]) -> None:
    store = _credentials(core)
    data = store.load()
    for name, value in previous.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    store.save(data)


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------


def _config_file(core: Core, scope: str, workdir: str | None) -> Path:
    try:
        return mcp_config.config_path(scope, core.paths.home, workdir)
    except mcp_config.McpConfigError as exc:
        raise RpcError(
            errors.MCP_INVALID, f"{exc.field}: {exc.message}", {"field": exc.field}
        ) from exc


async def _put_entry(
    core: Core, name: str, scope: str, workdir: str | None, entry: dict[str, Any] | None
) -> None:
    """Write ``entry`` as ``name`` (or remove ``name``) and make the runtime follow."""
    path = _config_file(core, scope, workdir)
    wd = Path(workdir) if workdir else None
    before = mcp_handlers.declared(core, wd).get(name)
    if entry is None:
        mcp_config.delete_entry(path, name)
    else:
        mcp_config.save_entry(path, name, entry)
    await mcp_handlers.reconcile(core, name, wd or Path.cwd(), before)


async def _restart(core: Core, name: str, workdir: str | None) -> None:
    """Start ``name`` afresh so its tools carry the tags now in settings."""
    row = mcp_handlers.declared(core, Path(workdir) if workdir else None).get(name)
    if row is not None:
        await mcp_handlers.apply(core, row)


def _sign_in(core: Core, name: str, server: dict[str, Any], workdir: str | None) -> McpPresetSignIn:
    method = server.get("signIn") or ("oauth" if server["auth"] == "oauth" else "none")
    if method == "oauth":
        row = mcp_handlers.declared(core, Path(workdir) if workdir else None).get(name)
        signed = (
            row is not None
            and bool(row.config.url)
            and mcp_oauth.TokenStore(core.paths.home).get(name, row.config.url) is not None
        )
        if signed:
            return McpPresetSignIn(server=name, method="none", note="signed in")
        return McpPresetSignIn(
            server=name,
            method="oauth",
            note=(
                f"Sign in with mcp.authorize {{name: {name!r}}}; a remote web host passes "
                "redirectUri and finishes with mcp.authorize.complete."
            ),
        )
    if method == "deviceCode":
        return McpPresetSignIn(
            server=name,
            method="deviceCode",
            command=server.get("loginCommand"),
            note=(
                "Run the command once on the machine the daemon runs on and enter the code it "
                "prints at microsoft.com/devicelogin; skip it when already signed in."
            ),
        )
    return McpPresetSignIn(server=name, method="none", note="")


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _clean_inputs(preset: dict[str, Any], given: dict[str, str]) -> dict[str, str]:
    known = {str(i["name"]): i for i in preset["inputs"]}
    values: dict[str, str] = {}
    for key, raw in (given or {}).items():
        if key not in known:
            raise RpcError(
                errors.INVALID_PARAMS,
                f"inputs.{key}: not an input of {preset['id']}",
                {"field": f"inputs.{key}"},
            )
        text = str(raw or "").strip()
        if len(text) > VALUE_MAX or any(c in text for c in "\r\n\x00"):
            raise RpcError(
                errors.INVALID_PARAMS,
                f"inputs.{key}: not a valid value",
                {"field": f"inputs.{key}"},
            )
        if text:
            values[key] = text
    missing = [n for n, i in known.items() if i.get("required", True) and n not in values]
    if missing:
        raise RpcError(
            errors.INVALID_PARAMS,
            f"inputs: {', '.join(missing)} required",
            {"field": "inputs", "missing": missing},
        )
    return values


def _recipe_job(
    core: Core,
    preset: dict[str, Any],
    recipe: dict[str, Any],
    names: dict[str, tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """The ``jarvis-task`` description of ``recipe``: which worker, with which tools."""
    suffix = recipe.get("server") or ""
    name, server = names[suffix]
    return {
        "kind": recipe["kind"],
        "preset": preset["id"],
        "recipe": recipe["id"],
        "servers": [name],
        "tools": read_tool_names(name, server, recipe["kind"]),
        "title": recipe["title"],
    }


async def apply(core: Core, params: McpPresetApplyParams) -> McpPresetApplyResult:
    preset = mcp_catalog.preset(params.id)
    if preset is None:
        raise RpcError(
            errors.MCP_INVALID,
            f"id: '{params.id}' is not a preset ({', '.join(mcp_catalog.preset_ids())})",
            {"field": "id"},
        )
    values = _clean_inputs(preset, params.inputs)
    recipes = {r["id"]: r for r in preset["recipes"]}
    unknown = [r for r in params.recipes if r not in recipes]
    if unknown:
        raise RpcError(
            errors.INVALID_PARAMS, f"recipes: unknown {', '.join(unknown)}", {"field": "recipes"}
        )
    if params.scope == "project" and not params.workdir:
        raise RpcError(
            errors.INVALID_PARAMS, "workdir: needed for scope 'project'", {"field": "workdir"}
        )
    prefix = params.name or preset["id"]
    try:
        mcp_config.validate_name(prefix)
    except mcp_config.McpConfigError as exc:
        raise RpcError(errors.MCP_INVALID, f"name: {exc.message}", {"field": "name"}) from exc
    secrets = _secret_names(preset)
    named = [(server_name(prefix, s), s) for s in preset["servers"]]
    by_suffix = {str(s.get("suffix") or ""): (n, s) for n, s in named}
    entries = {n: render_entry(s["entry"], values, secrets) for n, s in named}
    for entry in entries.values():
        try:
            mcp_config.validate_entry(entry)
        except mcp_config.McpConfigError as exc:
            raise RpcError(
                errors.MCP_INVALID, f"{exc.field}: {exc.message}", {"field": exc.field}
            ) from exc
    permissions = desired_permissions(named)
    secret_values = {n: v for n, v in values.items() if n in secrets}
    wanted_jobs = {
        rid: _recipe_job(core, preset, recipes[rid], by_suffix)
        for rid in dict.fromkeys(params.recipes)
    }

    async with _LOCK:
        return await _apply_locked(
            core, params, preset, named, entries, permissions, secret_values, wanted_jobs, by_suffix
        )


async def _apply_locked(
    core: Core,
    params: McpPresetApplyParams,
    preset: dict[str, Any],
    named: list[tuple[str, dict[str, Any]]],
    entries: dict[str, dict[str, Any]],
    permissions: dict[str, str],
    secret_values: dict[str, str],
    wanted_jobs: dict[str, dict[str, Any]],
    by_suffix: dict[str, tuple[str, dict[str, Any]]],
) -> McpPresetApplyResult:
    from snowpea_core.scheduler import services

    book = load_book(core)
    old = book.get(preset["id"])
    workdir = params.workdir if params.scope == "project" else None
    scope = params.scope
    if old is not None and (
        old.get("scope") != scope
        or old.get("workdir") != workdir
        or [s["name"] for s in old["servers"]] != [n for n, _ in named]
    ):
        raise RpcError(
            errors.MCP_EXISTS,
            f"{preset['id']} is already applied as "
            f"{', '.join(s['name'] for s in old['servers'])} ({old.get('scope')}); remove it first",
        )
    scheduler = services(core)
    stored_creds = _credentials(core).load()

    # Idempotence: the same thing again changes nothing.
    if old is not None:
        same_entries = all(s["entry"] == entries[s["name"]] for s in old["servers"])
        same_perms = all(_permission_table(core).get(k) == v for k, v in permissions.items())
        same_creds = all(stored_creds.get(k) == v for k, v in secret_values.items())
        same_jobs = set(old.get("jobs", {})) == set(wanted_jobs)
        for job_id in old.get("jobs", {}).values():
            same_jobs = same_jobs and await scheduler.get(job_id) is not None
        declared = mcp_handlers.declared(core, Path(workdir) if workdir else None)
        present = all(n in declared for n, _ in named)
        if same_entries and same_perms and same_creds and same_jobs and present:
            return _result(core, named, by_suffix, workdir, [], already=True)

    path = _config_file(core, scope, workdir)
    existing = mcp_config.read_servers(path)
    ours = {s["name"] for s in old["servers"]} if old else set()
    for name, _server in named:
        if name in existing and name not in ours and existing[name] != entries[name]:
            raise RpcError(
                errors.MCP_EXISTS,
                f"'{name}' is already declared in {path} and is not from this preset; "
                "pick another name (the name parameter) or remove it first",
            )

    # What to put back on remove: the very first state, kept across re-applies.
    snapshot: dict[str, Any] = (
        old["snapshot"]
        if old
        else {
            "entries": {n: existing.get(n) for n, _ in named},
            "permissions": {k: _permission_table(core).get(k) for k in permissions},
            "credentials": {k: stored_creds.get(k) for k in secret_values},
        }
    )
    for key in permissions:
        snapshot["permissions"].setdefault(key, _permission_table(core).get(key))
    for key in secret_values:
        snapshot["credentials"].setdefault(key, stored_creds.get(key))

    undo: list[Any] = []
    adds: list[McpAddResult] = []
    created_jobs: list[str] = []
    prior_entries = {n: existing.get(n) for n, _ in named}
    prior_perms = {k: _permission_table(core).get(k) for k in permissions}
    prior_creds = {k: stored_creds.get(k) for k in secret_values}
    try:
        if secret_values:
            store = _credentials(core)
            store.save({**store.load(), **secret_values})
            undo.append(lambda: _async_none(_restore_credentials, core, prior_creds))
        await _write_permissions(core, dict(permissions))
        undo.append(lambda: _write_permissions(core, dict(prior_perms)))
        for name, _server in named:
            entry = entries[name]
            fields = McpAddParams(
                name=name,
                scope=scope,
                workdir=workdir,
                replace=True,
                test=True,
                **{k: v for k, v in entry.items() if k in McpAddParams.model_fields},
            )
            adds.append(await mcp_handlers.mcp_add_handler(None, fields, core))
            undo.append(lambda n=name: _put_entry(core, n, scope, workdir, prior_entries[n]))
        for name, _server in named:
            await _restart(core, name, workdir)
        jobs = dict(old.get("jobs", {})) if old else {}
        for rid in list(jobs):
            if rid not in wanted_jobs:
                await scheduler.delete(jobs.pop(rid))
        for rid, data in wanted_jobs.items():
            if rid in jobs and await scheduler.get(jobs[rid]) is not None:
                await scheduler.update(jobs[rid], target_data=data)
                continue
            recipe = next(r for r in preset["recipes"] if r["id"] == rid)
            job = await scheduler.schedule(
                recipe["schedule"],
                recipe["prompt"],
                mode="accept",
                workdir=str(_folder(core)),
                title=recipe["title"],
                created_via="rpc",
                target="jarvis-task",
                target_data=data,
            )
            created_jobs.append(job.id)
            jobs[rid] = job.id
        book[preset["id"]] = {
            "scope": scope,
            "workdir": workdir,
            "servers": [{"name": n, "entry": entries[n]} for n, _ in named],
            "jobs": jobs,
            "snapshot": snapshot,
        }
        save_book(core, book)
    except BaseException:
        for job_id in created_jobs:
            with contextlib.suppress(Exception):
                await scheduler.delete(job_id)
        for action in reversed(undo):
            try:
                outcome = action()
                if asyncio.iscoroutine(outcome):
                    await outcome
            except Exception:  # noqa: BLE001 - keep undoing the rest
                log.warning("could not undo a step of applying %s", preset["id"], exc_info=True)
        raise
    return _result(core, named, by_suffix, workdir, adds, jobs=list(jobs.values()))


async def _async_none(fn: Any, *args: Any) -> None:
    fn(*args)


def _folder(core: Core) -> Path:
    from snowpea_core.jarvis.service import service_of

    folder = service_of(core).folder()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _result(
    core: Core,
    named: list[tuple[str, dict[str, Any]]],
    by_suffix: dict[str, tuple[str, dict[str, Any]]],
    workdir: str | None,
    adds: list[McpAddResult],
    *,
    jobs: list[str] | None = None,
    already: bool = False,
) -> McpPresetApplyResult:
    sign_in = [_sign_in(core, n, s, workdir) for n, s in named]
    if not adds:
        adds = [McpAddResult(ok=True, path="", state="stopped") for _ in named] if already else adds
    book_jobs = jobs
    if book_jobs is None:
        book_jobs = []
    return McpPresetApplyResult(
        server=named[0][0],
        add=adds[0],
        servers=[n for n, _ in named],
        adds=adds,
        jobs=book_jobs,
        needsAuth=any(s.method == "oauth" for s in sign_in),
        signIn=sign_in,
        already=already,
    )


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def remove(core: Core, preset_id: str) -> McpPresetRemoveResult:
    """Put back what applying ``preset_id`` replaced; ``removed`` False when it was not applied."""
    from snowpea_core.scheduler import services

    async with _LOCK:
        book = load_book(core)
        record = book.get(preset_id)
        if record is None:
            return McpPresetRemoveResult(removed=False)
        scope, workdir = record["scope"], record.get("workdir")
        snapshot = record["snapshot"]
        scheduler = services(core)
        jobs: list[str] = []
        for job_id in record.get("jobs", {}).values():
            if await scheduler.delete(job_id) is not None:
                jobs.append(job_id)
        servers: list[str] = []
        for server in record["servers"]:
            name = server["name"]
            url = (server["entry"] or {}).get("url")
            if url:
                mcp_oauth.TokenStore(core.paths.home).drop(name, url)
            await _put_entry(core, name, scope, workdir, snapshot["entries"].get(name))
            servers.append(name)
        await _write_permissions(core, dict(snapshot["permissions"]))
        _restore_credentials(core, snapshot["credentials"])
        for name in servers:
            if snapshot["entries"].get(name) is not None:
                await _restart(core, name, workdir)
        book.pop(preset_id, None)
        save_book(core, book)
        return McpPresetRemoveResult(removed=True, servers=servers, jobs=jobs)


def applied_servers(core: Core) -> dict[str, dict[str, Any]]:
    """``{server name: {"preset", "spec"}}`` for the connected presets (briefings read it)."""
    found: dict[str, dict[str, Any]] = {}
    for preset_id, record in load_book(core).items():
        preset = mcp_catalog.preset(preset_id)
        if preset is None:
            continue
        by_name = {server_name(_prefix_of(record, preset), s): s for s in preset["servers"]}
        for entry in record["servers"]:
            spec = by_name.get(entry["name"])
            if spec is not None:
                found[entry["name"]] = {"preset": preset_id, "spec": spec}
    return found


def _prefix_of(record: dict[str, Any], preset: dict[str, Any]) -> str:
    first = record["servers"][0]["name"]
    suffix = str(preset["servers"][0].get("suffix") or "")
    return first[: -(len(suffix) + 1)] if suffix else first


__all__ = [
    "applied_servers",
    "apply",
    "check",
    "mask_account",
    "desired_permissions",
    "list_presets",
    "load_book",
    "read_tool_names",
    "remove",
    "render_entry",
    "server_name",
]
