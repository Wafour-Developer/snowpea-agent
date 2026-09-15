"""Chat-level slash commands: sessions, projects, status and stop.

These are not registry commands.  ``/mode``, ``/model`` and the rest live in
``snowpea_core.commands`` and run *inside* a session, identically on every
surface.  The seven here exist only in a messenger, because they answer
questions a terminal answers with its own window: which conversation am I in,
what else is open, put me in another one, stop what you are doing.

:class:`GatewayRouter` calls :meth:`ChatCommands.handle` before it consults the
registry, so ``/help`` and ``/new`` mean these rather than a session command of
the same name, and everything else falls through untouched.

Which session a chat is in survives a daemon restart.  The choice is a single
line in ``$SNOWPEA_HOME/gateway-chats.json``::

    {"gw-4f21a0c91b33|123456": "s-7c68c2"}

— the key is ``"<binding_id>|<channel_id>"``, the value a session id.  It is
written on every switch and read back in ``GatewayRouter._session_for``; a
session that has since been deleted is ignored, and the chat falls back to the
binding's own target exactly as before.  Deliberately not ``state.db``: losing
this file costs a person one ``/resume``, and a chat that cannot find its
session must still work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.gateway.base import (
    Button,
    InboundMessage,
    parse_project_callback,
    parse_session_callback,
    project_callback,
    session_callback,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.gateway.router import Binding, GatewayRouter

log = logging.getLogger("snowpea.gateway.chat")

#: The chat commands, in the order ``/help`` and Telegram's menu list them.
CHAT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("sessions", "List the open conversations and switch to one"),
    ("resume", "Switch this chat to a session: /resume 2, /resume s-7c68c2"),
    ("new", "Start a session: /new, /new ~/src/api, /new 3"),
    ("projects", "List known projects and start a session in one"),
    ("status", "What this chat is attached to, and what it is doing"),
    ("stop", "Interrupt the running turn"),
    ("help", "Show these commands"),
)
#: Registry commands worth offering beside them on a phone, with the wording
#: Telegram's menu needs (the registry's own summaries are used in ``/help``).
REGISTRY_MENU: tuple[tuple[str, str], ...] = (
    ("mode", "Show or change the permission mode"),
    ("model", "Show or change the model"),
    ("effort", "Show or change the reasoning effort"),
)
#: What an adapter registers as its command menu.  Telegram caps a description
#: at 256 characters; every one here is far inside that.
MENU_COMMANDS: tuple[tuple[str, str], ...] = CHAT_COMMANDS + REGISTRY_MENU

#: Registry commands ``/help`` lists, in this order.  The rest either need a
#: terminal (``/setup``, ``/login``, ``/update``) or are long-form work nobody
#: starts from a phone; they all still *run* from chat, they are only not
#: advertised here.
HELP_REGISTRY: tuple[str, ...] = (
    "mode",
    "model",
    "effort",
    "compact",
    "memory",
    "agent",
    "delegate",
    "schedule",
    "approvals",
    "ralph",
    "review",
    "tools",
)

#: Session kinds a chat may attach itself to.  A subagent's session belongs to
#: its parent turn and a scheduled run belongs to its job; neither is a
#: conversation anyone can take over.
CHAT_KINDS: frozenset[str] = frozenset({"chat", "agent"})

#: Rows in a ``/sessions`` answer, and in a ``/projects`` answer.
SESSION_ROWS = 10
PROJECT_ROWS = 15
#: Longest slice of a remembered prompt shown in a row.
PROMPT_CHARS = 40
#: Telegram refuses a message over 4096 characters; stay well inside it.
MESSAGE_CHARS = 3500

#: Same wording as the approval path: short, and it names the reason.
NOT_YOUR_CHAT = "not your session — only the bound user can change it"

#: Name of the file remembering which session each chat is in.
CHATS_FILE = "gateway-chats.json"


class ChatSessionMemory:
    """``{"<binding>|<channel>": "<session>"}`` on disk, best effort.

    Every method swallows its I/O errors: a read-only home must cost a person
    the memory of which session they were in, not the ability to chat.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}

    def _write(self, data: dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:  # pragma: no cover - unwritable home
            log.warning("could not remember the chat session in %s: %s", self.path, exc)

    @staticmethod
    def key(binding_id: str, channel_id: str) -> str:
        return f"{binding_id}|{channel_id}"

    def get(self, binding_id: str, channel_id: str) -> str | None:
        return self._read().get(self.key(binding_id, channel_id))

    def remember(self, binding_id: str, channel_id: str, session_id: str) -> None:
        data = self._read()
        data[self.key(binding_id, channel_id)] = session_id
        self._write(data)

    def forget(self, binding_id: str, channel_id: str) -> None:
        data = self._read()
        if data.pop(self.key(binding_id, channel_id), None) is not None:
            self._write(data)


class Project:
    """One entry of the ``/projects`` list."""

    __slots__ = ("path", "pinned", "opened")

    def __init__(self, path: str, pinned: bool = False, opened: str = "") -> None:
        self.path = path
        self.pinned = pinned
        self.opened = opened

    @property
    def name(self) -> str:
        return Path(self.path).name or self.path


def read_projects(settings: Any, workdirs: list[str]) -> list[Project]:
    """``ide.projects`` merged with the store's recent workdirs, pinned first.

    ``ide`` is the IDE's own block of the settings document and the core never
    writes it, so every field is read permissively: an entry may be a bare path
    string or an object, and anything unreadable is skipped rather than
    failing the command.
    """
    block = getattr(settings, "ide", None)
    entries = (block or {}).get("projects") if isinstance(block, dict) else None
    seen: set[str] = set()

    def clean(path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return ""
        resolved = str(Path(text).expanduser())
        if resolved in seen:
            return ""
        seen.add(resolved)
        return resolved

    listed: list[Project] = []
    for entry in entries or []:
        if isinstance(entry, str):
            path, pinned, opened = clean(entry), False, ""
        elif isinstance(entry, dict):
            path = clean(str(entry.get("workdir") or entry.get("path") or ""))
            pinned = bool(entry.get("pinned"))
            opened = str(entry.get("lastOpenedAt") or "")
        else:
            continue
        if path:
            listed.append(Project(path, pinned, opened))
    # Two stable passes rather than one composite key: most recently opened
    # first (an entry with no timestamp keeps its place at the back), then
    # pinned to the top.
    listed.sort(key=lambda project: project.opened, reverse=True)
    listed.sort(key=lambda project: not project.pinned)
    for workdir in workdirs:
        path = clean(workdir)
        if path:
            listed.append(Project(path))
    return listed[:PROJECT_ROWS]


def clip(text: str, limit: int) -> str:
    """One line of ``text``, at most ``limit`` characters."""
    single = " ".join(str(text or "").split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


class ChatCommands:
    """The seven chat commands, plus the buttons and bare numbers they leave."""

    def __init__(self, router: GatewayRouter) -> None:
        self.router = router
        #: ``(binding, channel) -> "sessions" | "projects"``: which list the
        #: chat is looking at, so a bare ``2`` after it means that row.  Mirrors
        #: how an open ``ask_user`` question makes ``2`` an answer.
        self._pending: dict[tuple[str, str], str] = {}

    # -- entry points ---------------------------------------------------
    async def handle(self, binding: Binding, message: InboundMessage) -> bool:
        """Run a chat command; True when this message was one."""
        text = message.text.strip()
        if not text.startswith("/"):
            return False
        name, _, args = text[1:].partition(" ")
        runner = getattr(self, f"_cmd_{name.lower()}", None)
        if runner is None:
            return False
        await runner(binding, message, args.strip())
        return True

    async def handle_callback(self, binding: Binding, message: InboundMessage) -> bool:
        """Answer a ``/sessions`` or ``/projects`` button; True when it was one."""
        session_id = parse_session_callback(message.callback_data)
        if session_id is not None:
            await self._acknowledge(binding, message, "resume")
            if await self._refuse(binding, message):
                return True
            await self._resume_id(binding, message, session_id)
            return True
        index = parse_project_callback(message.callback_data)
        if index is not None:
            await self._acknowledge(binding, message, "new")
            if await self._refuse(binding, message):
                return True
            await self._new_from_project(binding, message, index)
            return True
        return False

    async def pick(self, binding: Binding, message: InboundMessage) -> bool:
        """Read a bare number as the row of the list this chat last saw."""
        text = message.text.strip()
        key = (binding.id, message.channel_id)
        kind = self._pending.get(key)
        if kind is None or not text.isdigit():
            return False
        del self._pending[key]
        if await self._refuse(binding, message):
            return True
        if kind == "sessions":
            await self._cmd_resume(binding, message, text)
        else:
            await self._new_from_project(binding, message, int(text))
        return True

    def clear_pick(self, binding: Binding, channel_id: str) -> None:
        """Forget the pending list; anything that is not a row ends it."""
        self._pending.pop((binding.id, channel_id), None)

    # -- commands -------------------------------------------------------
    async def _cmd_sessions(self, binding: Binding, message: InboundMessage, _args: str) -> None:
        rows = await self._sessions(binding, message.channel_id)
        if not rows:
            await self._say(binding, message, "no sessions yet — send a message, or /new")
            return
        current = self._current_session_id(binding, message.channel_id)
        lines = [
            f"{'★ ' if row.sessionId == current else ''}{index}. {self._session_row(row)}"
            for index, row in enumerate(rows, start=1)
        ]
        buttons = [
            Button(
                text=f"{index}. {row.sessionId} {Path(row.workdir).name}",
                data=session_callback(row.sessionId),
            )
            for index, row in enumerate(rows, start=1)
        ]
        self._pending[(binding.id, message.channel_id)] = "sessions"
        await self._say(binding, message, "\n".join(lines), buttons=buttons)

    async def _cmd_resume(self, binding: Binding, message: InboundMessage, args: str) -> None:
        if await self._refuse(binding, message):
            return
        wanted = args.strip()
        if not wanted:
            await self._say(binding, message, "usage: /resume <number | session id>")
            return
        rows = await self._sessions(binding, message.channel_id)
        if wanted.isdigit() and 1 <= int(wanted) <= len(rows):
            await self._resume_id(binding, message, rows[int(wanted) - 1].sessionId)
            return
        matches = [row for row in rows if row.sessionId.startswith(wanted)]
        if len(matches) == 1:
            await self._resume_id(binding, message, matches[0].sessionId)
            return
        if len(matches) > 1:
            count = len(matches)
            await self._say(binding, message, f"{wanted} matches {count} sessions — be exact")
            return
        await self._resume_id(binding, message, wanted)

    async def _cmd_new(self, binding: Binding, message: InboundMessage, args: str) -> None:
        if await self._refuse(binding, message):
            return
        wanted = args.strip()
        if wanted.isdigit():
            await self._new_from_project(binding, message, int(wanted))
            return
        if wanted:
            workdir = self._workdir_for(wanted)
            if workdir is None:
                await self._say(binding, message, f"no such path or project: {wanted}")
                return
        else:
            workdir = self._default_workdir(binding)
        await self._open(binding, message, workdir)

    async def _cmd_projects(self, binding: Binding, message: InboundMessage, _args: str) -> None:
        projects = self._projects()
        if not projects:
            await self._say(binding, message, "no projects yet — /new <path> starts one")
            return
        lines = [
            f"{index}. {project.name} · {project.path}"
            for index, project in enumerate(projects, start=1)
        ]
        buttons = [
            Button(text=f"{index}. {project.name}", data=project_callback(index))
            for index, project in enumerate(projects, start=1)
        ]
        self._pending[(binding.id, message.channel_id)] = "projects"
        await self._say(binding, message, "\n".join(lines), buttons=buttons)

    async def _cmd_status(self, binding: Binding, message: InboundMessage, _args: str) -> None:
        core = self.router.core
        session_id = self._current_session_id(binding, message.channel_id)
        session = core.sessions.get(session_id) if core is not None and session_id else None
        if session is None:
            await self._say(binding, message, "no session yet — send a message, or /new")
            return
        provider = session.provider or ""
        model = session.model or ""
        if not provider or not model:
            default = core.providers.default_profile() if core is not None else None
            if default is not None:
                provider, model = provider or default[0], model or default[1]
        route = "/".join(part for part in (provider, model) if part) or "unset"
        lines = [
            f"{session.id} · {session.workdir}",
            f"mode {session.mode} · {route} · effort {session.effort or 'default'}",
            self._running_line(binding, message.channel_id, session),
        ]
        if session.queued_turns:
            lines.append(f"queued {len(session.queued_turns)}")
        if session.context_used:
            window = f" / {session.context_window}" if session.context_window else ""
            lines.append(f"context {session.context_used}{window} tokens")
        await self._say(binding, message, "\n".join(lines))

    async def _cmd_stop(self, binding: Binding, message: InboundMessage, _args: str) -> None:
        if await self._refuse(binding, message):
            return
        from snowpea_core.server.session_handlers import interrupt_session

        core = self.router.core
        session_id = self._current_session_id(binding, message.channel_id)
        session = core.sessions.get(session_id) if core is not None and session_id else None
        if core is None or session is None:
            await self._say(binding, message, "nothing running")
            return
        stopped = await interrupt_session(core, session)
        await self._say(binding, message, "stopped" if stopped else "nothing running")

    async def _cmd_help(self, binding: Binding, message: InboundMessage, _args: str) -> None:
        lines = [f"/{name} — {summary}" for name, summary in CHAT_COMMANDS]
        core = self.router.core
        extras: list[str] = []
        for name in HELP_REGISTRY:
            command = core.commands.get(name) if core is not None else None
            if command is not None:
                extras.append(f"/{name} — {clip(command.summary, 60)}")
        if extras:
            lines.append("")
            lines.append("Also, in the session itself:")
            lines.extend(extras)
        await self._say(binding, message, "\n".join(lines))

    # -- doing the work -------------------------------------------------
    async def _resume_id(self, binding: Binding, message: InboundMessage, session_id: str) -> None:
        core = self.router.core
        if core is None:
            return
        session = core.sessions.get(session_id)
        if session is None:
            # A closed session is reopened, which is what ``session.resume``
            # does for a TUI reattaching to yesterday's thread.
            session = await core.sessions.restore(session_id)
        if session is None:
            await self._say(binding, message, f"no such session: {session_id}")
            return
        if session.kind not in CHAT_KINDS:
            await self._say(binding, message, f"{session.id} is a {session.kind} session")
            return
        await self.router.switch_session(binding, message.channel_id, session)
        await self._say(
            binding, message, f"Now in {session.id} · {session.workdir} · {session.mode}"
        )

    async def _new_from_project(
        self, binding: Binding, message: InboundMessage, index: int
    ) -> None:
        projects = self._projects()
        if not 1 <= index <= len(projects):
            await self._say(binding, message, f"no project {index} — /projects lists them")
            return
        await self._open(binding, message, Path(projects[index - 1].path))

    async def _open(self, binding: Binding, message: InboundMessage, workdir: Path) -> None:
        core = self.router.core
        if core is None:
            return
        session = await core.sessions.create(
            workdir=str(workdir),
            mode=(binding.target.get("new_session") or {}).get("mode"),
            origin_surface=f"gateway:{binding.platform}:{message.channel_id}",
            origin_conn=None,
        )
        await self.router.switch_session(binding, message.channel_id, session)
        await self._say(binding, message, f"New {session.id} · {session.workdir} · {session.mode}")

    # -- lookups --------------------------------------------------------
    async def _sessions(self, binding: Binding, channel_id: str) -> list[Any]:
        """The chat-usable sessions of this daemon, newest first."""
        core = self.router.core
        if core is None:
            return []
        from snowpea_core.server.protocol import SessionListParams
        from snowpea_core.server.session_handlers import collect_sessions

        rows = await collect_sessions(core, SessionListParams(includeClosed=True))
        usable = [row for row in rows if (row.kind or "chat") in CHAT_KINDS]
        current = self._current_session_id(binding, channel_id)
        if current and not any(row.sessionId == current for row in usable):
            usable = [row for row in rows if row.sessionId == current] + usable
        return usable[:SESSION_ROWS]

    def _session_row(self, row: Any) -> str:
        prompt = clip(row.lastPrompt or "", PROMPT_CHARS)
        name = Path(row.workdir).name or row.workdir
        line = f"{row.sessionId} · {name} · {row.mode}"
        return f'{line} · "{prompt}"' if prompt else line

    def _projects(self) -> list[Project]:
        core = self.router.core
        if core is None:
            return []
        workdirs = core.store.session_workdirs() if core.store is not None else []
        return read_projects(core.settings, list(workdirs))

    def _workdir_for(self, wanted: str) -> Path | None:
        """A path the user typed, or the name of a project they know."""
        if wanted.startswith(("/", "~")):
            path = Path(wanted).expanduser()
            return path if path.is_dir() else None
        lowered = wanted.lower()
        for project in self._projects():
            if project.name.lower() == lowered:
                path = Path(project.path)
                return path if path.is_dir() else None
        return None

    def _default_workdir(self, binding: Binding) -> Path:
        """Where a plain ``/new`` starts: the binding's own workdir."""
        core = self.router.core
        spec = binding.target.get("new_session") or {}
        workdir = spec.get("workdir") if isinstance(spec, dict) else None
        if workdir:
            return Path(str(workdir)).expanduser()
        return Path(core.paths.home) if core is not None else Path.home()

    def _current_session_id(self, binding: Binding, channel_id: str) -> str | None:
        conn = self.router.connection(binding.id, channel_id)
        return conn.session_id if conn is not None else None

    def _running_line(self, binding: Binding, channel_id: str, session: Any) -> str:
        if session.current_turn is None:
            return "idle"
        conn = self.router.connection(binding.id, channel_id)
        running_for = conn.activity.running_for() if conn is not None else None
        if running_for is None:
            return "running"
        from snowpea_core.gateway.activity import elapsed_text

        return f"running for {elapsed_text(running_for)}"

    # -- plumbing -------------------------------------------------------
    async def _refuse(self, binding: Binding, message: InboundMessage) -> bool:
        """True (and says so) when this user may not change the chat's session."""
        if not binding.user_id or message.user_id == binding.user_id:
            return False
        log.info(
            "ignoring a chat command from %s:%s on binding %s — not the bound user",
            binding.platform,
            message.user_id,
            binding.id,
        )
        await self.router.send(binding, message.channel_id, NOT_YOUR_CHAT)
        return True

    async def _acknowledge(self, binding: Binding, message: InboundMessage, text: str) -> None:
        """Clear the spinner on a pressed row, where the platform has one."""
        adapter = self.router.adapter(binding.id)
        acknowledge = getattr(adapter, "acknowledge", None)
        if acknowledge is None or not message.callback_id:
            return
        try:
            await acknowledge(message.callback_id, text)
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not acknowledge a chat button: %s", exc)

    async def _say(
        self,
        binding: Binding,
        message: InboundMessage,
        text: str,
        *,
        buttons: list[Button] | None = None,
    ) -> None:
        await self.router.send(binding, message.channel_id, text[:MESSAGE_CHARS], buttons=buttons)


__all__ = [
    "CHATS_FILE",
    "CHAT_COMMANDS",
    "CHAT_KINDS",
    "HELP_REGISTRY",
    "MENU_COMMANDS",
    "MESSAGE_CHARS",
    "NOT_YOUR_CHAT",
    "PROJECT_ROWS",
    "PROMPT_CHARS",
    "REGISTRY_MENU",
    "SESSION_ROWS",
    "ChatCommands",
    "ChatSessionMemory",
    "Project",
    "clip",
    "read_projects",
]
