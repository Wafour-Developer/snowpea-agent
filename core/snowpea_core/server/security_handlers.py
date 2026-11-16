"""``security.presets.list|get|check`` (1.54.0, capability ``securityLists``).

The lists themselves are written with ``settings.set {scope: "global", patch:
{security: …}}`` (a local person only, ``settings_gate.PERSON_LOCAL_ONLY``);
these methods read the catalog and the lists and judge a command or a path
without running anything.  The logic is :mod:`snowpea_core.security`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.security import catalog, enforce
from snowpea_core.security.check import check_command, check_path, path_entries
from snowpea_core.security.lists import (
    Lists,
    from_raw,
    global_lists,
    problems_in,
    project_additions,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    SecurityCheckParams,
    SecurityCheckResult,
    SecurityGetParams,
    SecurityGetResult,
    SecurityPresetsListParams,
    SecurityPresetsListResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = ("security.presets.list", "security.get", "security.check")


def _wire(lists: Lists) -> dict[str, Any]:
    return lists.to_wire()


async def presets_list_handler(
    _conn: RpcConnection | None, params: SecurityPresetsListParams, core: Core
) -> SecurityPresetsListResult:
    listed = catalog.catalog(params.locale)
    await enforce.refresh_projects(core)
    ctx = enforce.context(home=getattr(core.paths, "home", None),
                          projects=enforce.live_projects(core))
    for entry in listed["paths"]:
        shown: list[str] = []
        try:
            resolved = catalog.path_entries(entry["id"], user_home=ctx.user_home, home=ctx.home,
                                            workdir=None, projects=ctx.projects)
        except Exception:  # noqa: BLE001 - a preset that cannot resolve lists nothing
            resolved = []
        for item in resolved:
            text = item.shown or str(item.path)
            if text not in shown:
                shown.append(text)
        entry["paths"] = shown
    listed["locale"] = catalog.pick({"en": "en", "ko": "ko"}, params.locale)
    return SecurityPresetsListResult.model_validate(listed)


def effective_commands(lists: Lists) -> list[dict[str, str]]:
    """Every command pattern in force, a predicate-only rule shown by its examples."""
    out: list[dict[str, str]] = []
    for preset_id in lists.command_presets:
        preset = catalog.command_preset(preset_id)
        if preset is None:
            continue
        by = f"commands:{preset_id}"
        out.extend({"pattern": p, "by": by} for p in preset.patterns
                   if not p.startswith("re:"))
        if preset.predicates or preset.line_predicates:
            shown = {p["pattern"] for p in out}
            out.extend({"pattern": e, "by": by} for e in preset.examples if e not in shown)
    out.extend({"pattern": p, "by": "custom"} for p in lists.patterns)
    return out


async def get_handler(
    conn: RpcConnection | None, params: SecurityGetParams, core: Core
) -> SecurityGetResult:
    from snowpea_core.server.person_gate import connection_kind

    await enforce.refresh_projects(core)
    stored = global_lists(core.settings)
    project = project_additions(params.workdir) if params.workdir else None
    merged = stored.merged(project) if project is not None else stored
    ctx = enforce.context(settings=core.settings, home=getattr(core.paths, "home", None),
                          workdir=params.workdir,
                          projects=enforce.live_projects(core), locale=params.locale)
    resolved = [
        {"path": str(e.path), "level": e.level, "scope": e.scope, "by": e.by,
         "exempt": [str(x) for x in e.exempt]}
        for e in path_entries(merged, ctx)
    ]
    effective = {**_wire(merged), "commands": effective_commands(merged),
                 "paths": list({r["path"] + r["by"]: r for r in resolved}.values())}
    from snowpea_core.config.settings import SECURITY_PRESET_PROBLEMS

    problems = (list(SECURITY_PRESET_PROBLEMS.values()) + list(stored.problems)
                + (list(project.problems) if project else []))
    # Security review 6, M-4: a lost trusted-projects record is said here too.
    from snowpea_core.server.trust_handlers import store_problem

    lost = store_problem(core)
    if lost:
        problems.append(lost)
    items = list(stored.problem_items) + (list(project.problem_items) if project else [])
    return SecurityGetResult.model_validate({
        **_wire(stored),
        "unknownCode": enforce.unknown_code(core.settings),
        "project": _wire(project) if project is not None and not project.empty else None,
        "effective": effective,
        "writable": connection_kind(conn) == "person",
        "problems": problems,
        "problemItems": items,
    })


def _problem_details(problems: list[str]) -> dict[str, Any]:
    """``invalid_params`` details: the text, and its structured twin (1.55.0)."""
    return {"problems": list(problems),
            "problemItems": list(getattr(problems, "structured", []) or [])}


async def check_handler(
    _conn: RpcConnection | None, params: SecurityCheckParams, core: Core
) -> SecurityCheckResult:
    if not (params.command or "").strip() and not (params.path or "").strip():
        raise RpcError(errors.INVALID_PARAMS, "give a command or a path to check")
    if params.draft is not None:
        raw = params.draft.model_dump(mode="json")
        problems = problems_in(raw)
        if problems:
            raise RpcError(errors.INVALID_PARAMS, "; ".join(problems[:5]),
                           _problem_details(problems))
        lists = from_raw(raw, where="draft", defaults=False)
    else:
        lists = global_lists(core.settings)
    if params.workdir:
        lists = lists.merged(project_additions(params.workdir))
    await enforce.refresh_projects(core)
    ctx = enforce.context(settings=core.settings, home=getattr(core.paths, "home", None),
                          workdir=params.workdir,
                          projects=enforce.live_projects(core), locale=params.locale)
    cwd = ctx.workdir
    if params.command and params.command.strip():
        verdict = check_command(params.command, lists, ctx, cwd=cwd)
        if verdict.blocked or not (params.path or "").strip():
            return SecurityCheckResult.model_validate(verdict.to_wire())
    verdict = check_path(str(params.path), lists, ctx, write=params.access == "write")
    return SecurityCheckResult.model_validate(verdict.to_wire())


def settings_problems(patch: dict[str, Any], *, project_current: Any = None,
                      project: bool = False) -> None:
    """``settings.set``'s check of a ``security`` patch: ``invalid_params`` with every reason.

    ``project``: the project scope may only add (``project_current`` is its
    ``security`` before the patch).
    """
    from snowpea_core.config.patch import deep_merge
    from snowpea_core.security.lists import removed_by

    if "security" not in patch:
        return
    value = patch["security"]
    if value is None and not project:
        return  # back to the defaults, a local person's choice
    if project and isinstance(value, dict) and "unknownCode" in value:
        # Review 6, M-1: it takes a question away, so it is never a project's.
        raise RpcError(
            errors.INVALID_PARAMS,
            "security.unknownCode is a global setting; a project cannot change it",
            {"reason": "global_only", "key": "security.unknownCode"},
        )
    problems = problems_in(value) if value is not None else []
    if problems:
        raise RpcError(errors.INVALID_PARAMS, "; ".join(problems[:5]), _problem_details(problems))
    if project:
        before = project_current if isinstance(project_current, dict) else {}
        after = deep_merge(dict(before), value) if isinstance(value, dict) else {}
        gone = removed_by(before, after)
        if gone:
            raise RpcError(
                errors.INVALID_PARAMS,
                "a project can only add to the security lists; it would remove "
                + ", ".join(gone[:5]) + " (change the global lists instead)",
                {"reason": "project_add_only", "removed": gone},
            )


def register_security_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("security.presets.list", presets_list_handler)
    dispatcher.register("security.get", get_handler)
    dispatcher.register("security.check", check_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_security_handlers", "settings_problems"]
