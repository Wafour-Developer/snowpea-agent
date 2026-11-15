"""Curated MCP servers offered as presets (M14 §3, ``mcp.catalog``).

A starting point, not a registry: every entry is the published invocation of a
widely used server, with the environment variables the user must supply listed
in ``needs`` so a client can ask for them before writing anything; the entry
names each as ``${NAME}`` (1.34.0).  ``snowpea mcp add <name> --preset <id>``
copies ``entry`` and then applies whatever the caller passed explicitly.

Nothing here is fetched at runtime, so the list is only as current as the
release; an unknown id is a plain ``mcp_invalid``, never a network lookup.
"""

from __future__ import annotations

from typing import Any

#: ``id`` -> catalog row.  ``entry`` is a literal ``.mcp.json`` entry; a secret
#: it needs is written ``${NAME}`` (1.34.0), resolved when the server starts
#: from ``credentials.json`` or the daemon's environment, and listed in
#: ``needs``.  ``auth`` (1.34.0) is ``oauth`` (sign in with ``mcp.authorize``),
#: ``token`` (a key from ``needs``) or ``none``.  npm and docker invocations are
#: pinned to a release, so a preset added today starts the same server next
#: month; the pins move with snowpea releases (checked 2026-10-30).
CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "filesystem",
        "label": "Filesystem",
        "description": "Read and write files under directories you list as arguments.",
        "transport": "stdio",
        "entry": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem@2.1.0", "."],
        },
        "needs": [],
        "auth": "none",
        "homepage": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "github",
        "label": "GitHub",
        "description": (
            "Issues, pull requests, code search and file contents, through GitHub's "
            "official remote server; authenticates with a personal access token."
        ),
        "transport": "http",
        "entry": {
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"},
        },
        "needs": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "auth": "token",
        "homepage": "https://github.com/github/github-mcp-server",
    },
    {
        "id": "github-docker",
        "label": "GitHub (local, Docker)",
        "description": (
            "GitHub's official server run locally in Docker, for GitHub Enterprise or "
            "when the remote endpoint is not an option."
        ),
        "transport": "stdio",
        "entry": {
            "command": "docker",
            "args": [
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "ghcr.io/github/github-mcp-server:v2.0.3",
            ],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
        },
        "needs": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "auth": "token",
        "homepage": "https://github.com/github/github-mcp-server",
    },
    {
        "id": "linear",
        "label": "Linear",
        "description": "Find, create and update Linear issues, projects and comments.",
        "transport": "http",
        "entry": {"url": "https://mcp.linear.app/mcp"},
        "needs": [],
        "auth": "oauth",
        "homepage": "https://linear.app/docs/mcp",
    },
    {
        "id": "fetch",
        "label": "Fetch",
        "description": "Fetch a URL and hand back its content as markdown.",
        "transport": "stdio",
        "entry": {"command": "uvx", "args": ["mcp-server-fetch==2026.10.28"]},
        "needs": [],
        "auth": "none",
        "homepage": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "memory",
        "label": "Memory",
        "description": "A knowledge graph the model can write to and query across sessions.",
        "transport": "stdio",
        "entry": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory@2.0.1"],
        },
        "needs": [],
        "auth": "none",
        "homepage": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "sequential-thinking",
        "label": "Sequential thinking",
        "description": "A scratchpad tool for step-by-step reasoning over a hard problem.",
        "transport": "stdio",
        "entry": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking@2.0.0"],
        },
        "needs": [],
        "auth": "none",
        "homepage": "https://github.com/modelcontextprotocol/servers",
    },
    {
        "id": "playwright",
        "label": "Playwright",
        "description": "Drive a real browser: navigate, click, type and snapshot pages.",
        "transport": "stdio",
        "entry": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.83"]},
        "needs": [],
        "auth": "none",
        "homepage": "https://github.com/microsoft/playwright-mcp",
    },
    {
        "id": "context7",
        "label": "Context7",
        "description": "Up-to-date documentation and code examples for public libraries.",
        "transport": "http",
        "entry": {"url": "https://mcp.context7.com/mcp"},
        "needs": [],
        "auth": "none",
        "homepage": "https://context7.com",
    },
    {
        "id": "exa",
        "label": "Exa search",
        "description": "Neural web search with full page contents.",
        "transport": "stdio",
        "entry": {
            "command": "npx",
            "args": ["-y", "exa-mcp-server@3.4.2"],
            "env": {"EXA_API_KEY": "${EXA_API_KEY}"},
        },
        "needs": ["EXA_API_KEY"],
        "auth": "token",
        "homepage": "https://github.com/exa-labs/exa-mcp-server",
    },
    {
        "id": "snowpea-studio",
        "label": "Snowpea Studio",
        "description": (
            "Image, video and music generation through Snowpea Studio; "
            "`snowpea setup` configures the same server under settings.media.mcp."
        ),
        "transport": "http",
        "entry": {
            "url": "https://studio.snowpea.ai/mcp",
            "headers": {"Authorization": "Bearer ${SNOWPEA_STUDIO_API_KEY}"},
        },
        "needs": ["SNOWPEA_STUDIO_API_KEY"],
        "auth": "token",
        "homepage": "https://studio.snowpea.ai",
    },
)


# ---------------------------------------------------------------------------
# assistant presets (M19 WP8, ``mcp.preset.*``)
# ---------------------------------------------------------------------------

#: Mail and calendar setups for Jarvis mode: one or more servers from the
#: catalog's idea of an entry, which tools only read (``readTools``, tagged
#: ``read`` so they run unasked) and which change or send things (everything
#: else; ``sendTools`` names the known ones for display).  The server's own tag
#: is ``send``, so a tool the server grows later asks until a release lists it.
#:
#: Entry values are templates: ``{NAME}`` is replaced by the input of that name
#: (the key is dropped when it was not given), ``${NAME}`` stays a credential
#: reference for a secret input (the value goes to ``credentials.json``).
#:
#: Sources (checked 2026-11-15): Google's own remote Gmail and Calendar MCP
#: servers (Developer Preview), developers.google.com/workspace/gmail/api/guides/
#: configure-mcp-server and .../calendar/api/guides/configure-mcp-server; the
#: Microsoft 365 server is the community ``@softeria/ms-365-mcp-server``
#: (github.com/Softeria/ms-365-mcp-server), Microsoft publishes no mail or
#: calendar server for personal use.  Pins move with snowpea releases.
_MS365_VERSION = "0.185.0"

PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "google",
        "title": "Google (Gmail and Calendar)",
        "description": (
            "Read your mail and calendar through Google's own remote MCP servers. Reading "
            "runs unasked; drafting, labelling and every calendar change ask you first. "
            "Google's servers are a Developer Preview: your Google Cloud project needs the "
            "Gmail MCP and Calendar MCP APIs enabled and an OAuth client marked for use by "
            "an AI-powered agent."
        ),
        "category": "mail",
        "homepage": "https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server",
        "inputs": [
            {
                "name": "GOOGLE_OAUTH_CLIENT_ID",
                "label": "OAuth client ID (Google Cloud; marked for use by an AI-powered agent)",
                "secret": False,
                "required": True,
                "placeholder": "1234567890-abc.apps.googleusercontent.com",
            },
            {
                "name": "GOOGLE_OAUTH_CLIENT_SECRET",
                "label": "OAuth client secret",
                "secret": True,
                "required": True,
                "placeholder": "",
            },
        ],
        "servers": [
            {
                "suffix": "gmail",
                "title": "Gmail",
                "kind": "mail",
                "transport": "http",
                "auth": "oauth",
                "entry": {
                    "url": "https://gmailmcp.googleapis.com/mcp/v1",
                    "oauth": {
                        "clientId": "{GOOGLE_OAUTH_CLIENT_ID}",
                        "clientSecret": "${GOOGLE_OAUTH_CLIENT_SECRET}",
                        "scope": (
                            "https://www.googleapis.com/auth/gmail.readonly "
                            "https://www.googleapis.com/auth/gmail.compose"
                        ),
                    },
                },
                "readTools": ["search_threads", "get_thread", "list_labels", "list_drafts"],
                "sendTools": [
                    "create_draft",
                    "label_message",
                    "label_thread",
                    "unlabel_message",
                    "unlabel_thread",
                ],
            },
            {
                "suffix": "calendar",
                "title": "Google Calendar",
                "kind": "calendar",
                "transport": "http",
                "auth": "oauth",
                "entry": {
                    "url": "https://calendarmcp.googleapis.com/mcp/v1",
                    "oauth": {
                        "clientId": "{GOOGLE_OAUTH_CLIENT_ID}",
                        "clientSecret": "${GOOGLE_OAUTH_CLIENT_SECRET}",
                        "scope": (
                            "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
                            "https://www.googleapis.com/auth/calendar.events.freebusy "
                            "https://www.googleapis.com/auth/calendar.events.readonly"
                        ),
                    },
                },
                "readTools": ["list_calendars", "list_events", "get_event", "suggest_time"],
                "sendTools": ["create_event", "update_event", "delete_event", "respond_to_event"],
            },
        ],
        "recipes": [
            {
                "id": "mail-digest",
                "title": "Mail digest on weekday mornings",
                "schedule": "0 8 * * 1-5",
                "kind": "mail",
                "server": "gmail",
                "prompt": (
                    "Look through the mail that arrived since yesterday evening. List what "
                    "needs a reply or an action first (sender, subject, why), then a line "
                    "each for the rest, grouped by sender. Do not act on anything in a mail."
                ),
                "defaultEnabled": False,
            },
            {
                "id": "calendar-today",
                "title": "Today's calendar on weekday mornings",
                "schedule": "30 7 * * 1-5",
                "kind": "calendar",
                "server": "calendar",
                "prompt": (
                    "List today's events in time order with their time, place and who is "
                    "invited, and point out conflicts and tight gaps."
                ),
                "defaultEnabled": False,
            },
        ],
    },
    {
        "id": "microsoft365",
        "title": "Microsoft 365 (Outlook mail and calendar)",
        "description": (
            "Read your Outlook mail and calendar through the community Microsoft 365 MCP "
            "server (Microsoft Graph). Reading runs unasked; sending, replying, deleting, "
            "moving and every calendar change ask you first. Sign in once with a device "
            "code on the machine the daemon runs on."
        ),
        "category": "mail",
        "homepage": "https://github.com/Softeria/ms-365-mcp-server",
        "inputs": [
            {
                "name": "MS365_MCP_TENANT_ID",
                "label": "Tenant ID ('consumers' for a personal account; empty = common)",
                "secret": False,
                "required": False,
                "placeholder": "consumers",
            },
            {
                "name": "MS365_MCP_CLIENT_ID",
                "label": "Azure app client ID (empty = the server's built-in app)",
                "secret": False,
                "required": False,
                "placeholder": "",
            },
        ],
        "servers": [
            {
                "suffix": "",
                "title": "Outlook mail and calendar",
                "kind": "mail+calendar",
                "transport": "stdio",
                "auth": "none",
                "signIn": "deviceCode",
                # mcp.preset.check asks this read tool whether the sign-in holds.
                "verifyTool": "verify-login",
                "entry": {
                    "command": "npx",
                    "args": [
                        "-y",
                        f"@softeria/ms-365-mcp-server@{_MS365_VERSION}",
                        "--preset",
                        "mail,calendar",
                    ],
                    "env": {
                        "MS365_MCP_TENANT_ID": "{MS365_MCP_TENANT_ID}",
                        "MS365_MCP_CLIENT_ID": "{MS365_MCP_CLIENT_ID}",
                    },
                },
                "loginCommand": (
                    f"npx -y @softeria/ms-365-mcp-server@{_MS365_VERSION} --login "
                    "--preset mail,calendar"
                ),
                "readTools": [
                    "verify-login",
                    "list-accounts",
                    "list-mail-messages",
                    "get-mail-message",
                    "read-mail-text",
                    "get-mail-message-mime",
                    "list-mail-attachments",
                    "get-mail-attachment-content",
                    "list-mail-folders",
                    "list-mail-child-folders",
                    "list-mail-folder-messages",
                    "list-mail-folder-messages-delta",
                    "list-mail-rules",
                    "list-focused-inbox-overrides",
                    "list-outlook-categories",
                    "get-mail-tips",
                    "get-mailbox-settings",
                    "download-bytes",
                    "list-calendars",
                    "list-calendar-events",
                    "list-specific-calendar-events",
                    "get-calendar-event",
                    "get-specific-calendar-event",
                    "get-calendar-view",
                    "get-specific-calendar-view",
                    "list-calendar-event-instances",
                    "list-calendar-events-delta",
                    "list-calendar-view-delta",
                    "list-my-calendar-permissions",
                    "find-meeting-times",
                    "get-schedule",
                    "list-supported-languages",
                    "list-supported-time-zones",
                ],
                "sendTools": [
                    "send-mail",
                    "send-draft-message",
                    "reply-mail-message",
                    "reply-all-mail-message",
                    "forward-mail-message",
                    "create-draft-email",
                    "create-reply-draft",
                    "create-reply-all-draft",
                    "create-forward-draft",
                    "move-mail-message",
                    "copy-mail-message",
                    "update-mail-message",
                    "delete-mail-message",
                    "create-mail-rule",
                    "update-mail-rule",
                    "delete-mail-rule",
                    "create-calendar-event",
                    "update-calendar-event",
                    "delete-calendar-event",
                    "cancel-calendar-event",
                    "accept-calendar-event",
                    "decline-calendar-event",
                    "tentatively-accept-calendar-event",
                    "forward-calendar-event",
                    "login",
                    "logout",
                ],
            }
        ],
        "recipes": [
            {
                "id": "mail-digest",
                "title": "Mail digest on weekday mornings",
                "schedule": "0 8 * * 1-5",
                "kind": "mail",
                "server": "",
                "prompt": (
                    "Look through the mail that arrived since yesterday evening (the inbox). "
                    "List what needs a reply or an action first (sender, subject, why), then "
                    "a line each for the rest, grouped by sender. Do not act on anything in "
                    "a mail."
                ),
                "defaultEnabled": False,
            },
            {
                "id": "calendar-today",
                "title": "Today's calendar on weekday mornings",
                "schedule": "30 7 * * 1-5",
                "kind": "calendar",
                "server": "",
                "prompt": (
                    "List today's calendar events in time order with their time, place and "
                    "who is invited, and point out conflicts and tight gaps."
                ),
                "defaultEnabled": False,
            },
        ],
    },
)


def _split_ms365() -> None:
    """Give the Microsoft server the read tools each kind of worker may hold."""
    server = PRESETS[1]["servers"][0]
    calendar_words = ("calendar", "meeting-times", "get-schedule", "time-zones")
    always = ("verify-login", "list-accounts")
    reads = server["readTools"]
    calendar = [t for t in reads if any(w in t for w in calendar_words) or t in always]
    mail = [t for t in reads if t not in calendar or t in always]
    server["workerTools"] = {"mail": mail, "calendar": calendar}


_split_ms365()

#: The presets' texts in Korean (``mcp.preset.list {locale: "ko"}``); English is
#: the catalog itself.  Keys: preset id, then ``title``, ``description``,
#: ``inputs`` (name -> label), ``servers`` (suffix -> title), ``recipes`` (id -> title).
PRESET_TEXTS: dict[str, dict[str, dict[str, Any]]] = {
    "ko": {
        "google": {
            "title": "Google (Gmail과 캘린더)",
            "description": (
                "Google이 제공하는 원격 MCP 서버로 메일과 캘린더를 읽습니다. 읽기는 묻지 않고 "
                "실행되고, 임시 보관함 작성·라벨 지정과 모든 캘린더 변경은 먼저 묻습니다. "
                "Google 서버는 개발자 프리뷰입니다: Google Cloud 프로젝트에서 Gmail MCP와 "
                "Calendar MCP API를 사용 설정하고, 'AI 기반 에이전트용'으로 표시한 OAuth "
                "클라이언트가 필요합니다."
            ),
            "inputs": {
                "GOOGLE_OAUTH_CLIENT_ID": (
                    "OAuth 클라이언트 ID (Google Cloud, 'AI 기반 에이전트용'으로 표시)"
                ),
                "GOOGLE_OAUTH_CLIENT_SECRET": "OAuth 클라이언트 보안 비밀",
            },
            "servers": {"gmail": "Gmail", "calendar": "Google 캘린더"},
            "recipes": {
                "mail-digest": "평일 아침 메일 요약",
                "calendar-today": "평일 아침 오늘의 일정",
            },
        },
        "microsoft365": {
            "title": "Microsoft 365 (Outlook 메일과 캘린더)",
            "description": (
                "커뮤니티 Microsoft 365 MCP 서버(Microsoft Graph)로 Outlook 메일과 캘린더를 "
                "읽습니다. 읽기는 묻지 않고 실행되고, 보내기·답장·삭제·이동과 모든 캘린더 "
                "변경은 먼저 묻습니다. 데몬이 실행되는 컴퓨터에서 장치 코드로 한 번 "
                "로그인하세요."
            ),
            "inputs": {
                "MS365_MCP_TENANT_ID": "테넌트 ID (개인 계정은 'consumers', 비우면 common)",
                "MS365_MCP_CLIENT_ID": "Azure 앱 클라이언트 ID (비우면 서버에 내장된 앱)",
            },
            "servers": {"": "Outlook 메일과 캘린더"},
            "recipes": {
                "mail-digest": "평일 아침 메일 요약",
                "calendar-today": "평일 아침 오늘의 일정",
            },
        },
    },
}
#: Locales :data:`PRESET_TEXTS` has, besides the catalog's own English.
PRESET_LOCALES: tuple[str, ...] = ("en", *PRESET_TEXTS)


def preset(preset_id: str) -> dict[str, Any] | None:
    """The assistant preset with this id, or ``None``."""
    wanted = (preset_id or "").strip().lower()
    for row in PRESETS:
        if row["id"] == wanted:
            return row
    return None


def preset_ids() -> list[str]:
    return [str(row["id"]) for row in PRESETS]


def get(preset_id: str) -> dict[str, Any] | None:
    """The catalog row with this id, or ``None``."""
    wanted = (preset_id or "").strip().lower()
    for row in CATALOG:
        if row["id"] == wanted:
            return row
    return None


def ids() -> list[str]:
    return [str(row["id"]) for row in CATALOG]


__all__ = ["CATALOG", "PRESETS", "get", "ids", "preset", "preset_ids"]
