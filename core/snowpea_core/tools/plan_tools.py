"""``plan_save`` and ``plan_update_step``: the current plan as a tool (CORE-plan-continuity).

``write_todos`` is the list for *this* turn's work and lives on the session;
the plan outlives the session.  ``/ralplan`` and ``/deep-interview`` call
``plan_save`` with their final plan or spec, plan mode calls it when the plan is
done, and whoever then implements a step marks it with ``plan_update_step`` —
the next ``/ralph 실행해줘``, or a prompt after compaction, starts from there.

Both are tagged ``read``: they write nothing but ``.snowpea/plans/current.*``,
which plan mode may write anyway (``permissions/plan_paths``), and a planner
that is refused the one call that keeps its plan is how plans got lost.  That
tag would also let a delegated child rewrite the user's plan with no one
asked, so a child session is refused outright: the plan is the main agent's.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.agent import plan_store
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult


def _render(plan: plan_store.Plan) -> str:
    return f"{plan_store.summary_line(plan)}\n{plan_store.steps_text(plan)}"


#: What a delegated child is told when it reaches for the plan.
CHILD_REFUSAL = (
    "only the main agent may change the user's plan; report what you finished and "
    "let it mark the step"
)


async def plan_save(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Make the given plan the project's current plan, or abandon it (``clear``)."""
    if getattr(ctx.session, "is_subagent", False):
        return ToolResult(ok=False, error=CHILD_REFUSAL)
    if args.get("clear") is True or str(args.get("clear")).lower() == "true":
        current = plan_store.load_current(ctx.session.workdir)
        archived = plan_store.archive_current(ctx.session.workdir)
        if current is None or archived is None:
            return ToolResult(ok=True, output="there was no current plan to clear")
        current.status = "archived"
        ctx.session.plan_saved = True
        await plan_store.publish(ctx.core, ctx.session, current)
        return ToolResult(
            ok=True,
            output=f"cleared the current plan; it was archived as {archived.name}",
        )
    try:
        plan = plan_store.save_plan(
            ctx.session.workdir,
            str(args.get("title") or ""),
            str(args.get("markdown") or ""),
            args.get("steps"),
            str(args.get("source") or "manual"),
        )
    except plan_store.PlanError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        return ToolResult(ok=False, error=f"could not save the plan: {exc}")
    ctx.session.plan_saved = True
    await plan_store.publish(ctx.core, ctx.session, plan)
    return ToolResult(
        ok=True,
        output=f"saved {plan_store.CURRENT_PATH}\n{_render(plan)}",
        meta={"id": plan.id, "steps": [step.to_json() for step in plan.steps]},
    )


async def plan_update_step(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Set one step of the current plan to pending/in_progress/done/blocked."""
    if getattr(ctx.session, "is_subagent", False):
        return ToolResult(ok=False, error=CHILD_REFUSAL)
    step_id = str(args.get("id") or "").strip()
    if not step_id:
        return ToolResult(ok=False, error="id is required, e.g. 'S3'")
    try:
        plan = plan_store.mark_step(
            ctx.session.workdir,
            step_id,
            str(args.get("status") or "").strip(),
            str(args.get("note") or ""),
        )
    except plan_store.PlanError as exc:
        return ToolResult(ok=False, error=str(exc))
    except OSError as exc:
        return ToolResult(ok=False, error=f"could not update the plan: {exc}")
    await plan_store.publish(ctx.core, ctx.session, plan)
    return ToolResult(ok=True, output=_render(plan))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="plan_save",
        category="plan",
        description=descriptions.PLAN_SAVE,
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The plan's one-line title."},
                "markdown": {"type": "string", "description": "The whole plan, as markdown."},
                "steps": {
                    "type": "array",
                    "description": (
                        "The steps in order (ids default to S1, S2, …). Omit to take the "
                        "numbered list under the plan's Steps heading."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
                "source": {
                    "type": "string",
                    "enum": list(plan_store.SOURCES),
                    "description": "What made the plan (default manual).",
                },
                "clear": {
                    "type": "boolean",
                    "description": (
                        "true abandons the current plan (archived; nothing else needed), "
                        "e.g. when the user drops it."
                    ),
                },
            },
        },
        permission="read",
        run=plan_save,
    ),
    Tool(
        name="plan_update_step",
        category="plan",
        description=descriptions.PLAN_UPDATE_STEP,
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Step id, e.g. S3."},
                "status": {"type": "string", "enum": list(plan_store.STEP_STATUSES)},
                "note": {
                    "type": "string",
                    "description": "What verified it, or what blocks it.",
                },
            },
            "required": ["id", "status"],
        },
        permission="read",
        run=plan_update_step,
    ),
)


__all__ = ["TOOLS", "plan_save", "plan_update_step"]
