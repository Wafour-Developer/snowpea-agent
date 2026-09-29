"""``write_todos``: the agent's visible task list for a session (protocol 1.7.0).

The list lives on the session and every change is published as a
``todos.updated`` session event, so a surface (the browser's side panel, the
desktop app) can show what the agent plans to do and how far it got.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

STATUSES = ("pending", "in_progress", "completed")

#: Longest list the model may keep; a plan longer than this is not a plan.
MAX_TODOS = 50


def _clean(raw: Any) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(raw, list):
        return [], "todos must be a list"
    out: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"todos[{index}] must be an object"
        todo_id = str(item.get("id") or "").strip() or str(index + 1)
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending").strip()
        if status not in STATUSES:
            return [], f"todos[{index}].status must be one of {', '.join(STATUSES)}"
        if not content and not item.get("status"):
            return [], f"todos[{index}] needs content"
        out.append({"id": todo_id, "content": content, "status": status})
    return out, None


def merge_todos(
    current: list[dict[str, str]], changes: list[dict[str, str]]
) -> list[dict[str, str]]:
    """``changes`` applied onto ``current`` by id; new ids are appended in order."""
    by_id = {todo["id"]: dict(todo) for todo in current}
    order = [todo["id"] for todo in current]
    for change in changes:
        existing = by_id.get(change["id"])
        if existing is None:
            by_id[change["id"]] = dict(change)
            order.append(change["id"])
            continue
        if change["content"]:
            existing["content"] = change["content"]
        existing["status"] = change["status"]
    return [by_id[todo_id] for todo_id in order]


def render(todos: list[dict[str, str]]) -> str:
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = [f"{marks[t['status']]} {t['id']}. {t['content']}" for t in todos]
    done = sum(1 for t in todos if t["status"] == "completed")
    return "\n".join([f"{done}/{len(todos)} done", *lines]) if todos else "the list is empty"


async def write_todos(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Replace the session's task list, or merge changes into it by id."""
    changes, error = _clean(args.get("todos"))
    if error:
        return ToolResult(ok=False, error=error)
    session = ctx.session
    current = list(getattr(session, "todos", []) or [])
    todos = merge_todos(current, changes) if args.get("merge") else changes
    if len(todos) > MAX_TODOS:
        return ToolResult(ok=False, error=f"keep the list under {MAX_TODOS} items")
    if sum(1 for t in todos if t["status"] == "in_progress") > 1:
        return ToolResult(ok=False, error="only one item may be in_progress at a time")
    session.todos = todos
    from snowpea_core.session import events

    hub = getattr(ctx.core, "hub", None)
    if hub is not None:
        await hub.emit_event(session.id, events.todos_updated(todos))
    return ToolResult(ok=True, output=render(todos))


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="write_todos",
        category="interaction",
        description=(
            "Keep a short task list for multi-step work, visible to the user as you "
            "go. Write it once you know the steps; mark one item in_progress while "
            "you work on it and completed as soon as it is done. merge=true updates "
            "items by id (send only the changed ones); merge=false replaces the list. "
            "Skip it for a one- or two-step task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["id", "status"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "Update items by id instead of replacing the list.",
                },
            },
            "required": ["todos"],
        },
        permission="read",
        run=write_todos,
    ),
)

__all__ = ["MAX_TODOS", "STATUSES", "TOOLS", "merge_todos", "render", "write_todos"]
