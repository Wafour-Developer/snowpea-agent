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


def get(preset_id: str) -> dict[str, Any] | None:
    """The catalog row with this id, or ``None``."""
    wanted = (preset_id or "").strip().lower()
    for row in CATALOG:
        if row["id"] == wanted:
            return row
    return None


def ids() -> list[str]:
    return [str(row["id"]) for row in CATALOG]


__all__ = ["CATALOG", "get", "ids"]
