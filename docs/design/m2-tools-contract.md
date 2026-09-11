# M2 Tools & Backends Contract (binding for US-009, US-010)

Builds on `docs/design/m1-core-contract.md` §6 (`Tool`, `ToolContext`, `ToolResult`, `ToolRegistry`) and the M1 implementation in `core/snowpea_core/tools/registry.py`, `exec/backend.py`, `exec/local.py`. Plan: §3.1 tools/exec, §4 M2, AC-06, AC-18, §2.4 vendoring rules.

## 1. Execution backends (`exec/`)
```python
class ExecutionBackend(Protocol):
    kind: Literal["local","docker","ssh"]
    async def run(self, command: str, *, cwd: str|None, timeout: float, env: dict[str,str]|None=None) -> RunResult   # RunResult(exit_code, stdout, stderr, timed_out)
    async def read_file(self, path: str) -> bytes
    async def write_file(self, path: str, data: bytes) -> None
    async def exists(self, path: str) -> bool
    async def list_dir(self, path: str) -> list[str]
    def cwd(self) -> str
    async def close(self) -> None
```
- `exec/local.py` — asyncio subprocess (already M1).
- `exec/docker.py` — `docker exec` into a container created from `config.image` (default `python:3.11-slim`) with `workdir` bind-mounted at the same absolute path; container name `snowpea-<sessionId[:8]>`; created lazily on first call, removed on `close()`. File ops via `docker exec` + `cat`/`sh -c 'cat > path'`.
- `exec/ssh.py` — `asyncssh` (add dependency) with `host, port, user, key` (or `password`); file ops via SFTP; cwd = remote `config.cwd` or `~`.
- `backend.set(sessionId, kind, config)` RPC swaps `session.backend`; `ToolContext.backend` is that object. `/backend <kind> [json]` command (commands/backend_cmd.py). Every tool in `tools/fs.py`, `tools/shell.py`, `tools/git.py`, `tools/grep.py`, `tools/glob.py` MUST go through `ctx.backend` (never `open()`/`subprocess` directly) so AC-18 holds.

## 2. Tool catalog (names are the API; categories drive setup toggles)
| category | tools | permission |
|---|---|---|
| file | read_file, write_file, edit_file, list_dir, glob, grep | read / write |
| terminal | shell, process_list, process_kill | exec |
| git | git_status, git_diff, git_commit, git_log (thin wrappers → shell on backend) | read / write |
| web | web_search, web_extract | network |
| browser | browser_navigate, browser_click, browser_type, browser_scroll, browser_snapshot | network |
| delegate | delegate_task (M1 stub → M7) | exec |
| schedule | schedule_create/list/cancel (stub → M5) | send |
| memory | memory_write, memory_search (stub → M5) | read/write |
| media | image_generate, video_generate, music_generate, text_to_speech | network |
| mcp | `mcp__<server>__<tool>` (dynamic) | per server config, default network |

Stubs (M5/M7 tools) are registered with `state="inactive"` and return `error{code:"not_implemented"}`; media tools are `inactive` until credentials exist and then flip to `active` (no restart) — `ToolRegistry.set_state(name, state)` + a `tool.state.changed` session event is NOT required; `tool.list` reflects current state.

## 3. Web search providers (`tools/search_providers/`)
```python
@dataclass
class SearchProviderMeta: id: str; label: str; tier: Literal["free","paid","subscription"]; key: Literal["no key","key optional","key required","self-hosted"]; default: bool=False; env: list[str]=[]
class SearchProvider(Protocol):
    meta: SearchProviderMeta
    def available(self, settings) -> bool            # keyless providers → always True
    async def search(self, query: str, *, limit: int) -> list[SearchHit]   # SearchHit(title, url, snippet)
    async def extract(self, url: str) -> str | None    # optional; None → fallback to httpx+readability-lite
```
Registry order (also the setup screen order): `ddgs`(★ default, no key) → `brave_free`(key) → `exa_free`(no key) → `keenable_free`(no key) → `parallel_free`(no key) → `tavily`(key optional) → `searxng`(self-hosted, `SEARXNG_URL`) → `firecrawl_selfhost`(self-hosted) → `exa`(paid) → `keenable`(paid) → `parallel`(paid) → `firecrawl`(paid) → `xai_grok`(paid). Only `ddgs`, `brave_free`, `tavily`, `searxng` need real HTTP implementations in M2; the others may be thin HTTP clients with the documented endpoint and must degrade with a clear `error{code:"search_provider_unavailable"}` — but every id MUST exist in the registry with correct tags. `web_search` uses `settings.search.provider` then falls back down the free chain on failure (log which). `web_extract` fetches with SSRF guard (block private/link-local; vendored `tools/url_safety.py` from Hermes) and truncates to `settings.tools.max_output_chars` (default 20k).

## 4. Browser providers (`tools/browser_providers/`)
Ids: `local_chromium`(★ default, free, playwright headless chromium — add `playwright` dependency; install-on-first-use message if browsers missing), `camoufox`(free local), `browser_use_local`(free local), `browserbase`(paid), `firecrawl_cloud`(paid). Same meta dataclass. Only `local_chromium` needs a working implementation in M2 (navigate/click/type/scroll/snapshot via Playwright, one browser per session, closed on session close); others register with tags and return `error{code:"browser_provider_unavailable"}`.

## 5. Media tools (`tools/media.py`)
Backed by the snowpea-studio MCP server (`settings.media.mcp` = command/url + api key). Always registered; `state="inactive"` when no credentials; `provider.configure("media", {...})` or settings change → `active` via `ToolRegistry.set_state`. Calls forward to `tools/mcp_client.py`.

## 6. MCP client (`tools/mcp_client.py`)
Loads `.mcp.json` from `$SNOWPEA_HOME/.mcp.json` and `<workdir>/.mcp.json` (Claude Code format: `{"mcpServers": {"name": {"command","args","env"} | {"url"}}}`); stdio and SSE transports (implement stdio with the `mcp` python package if available, else a minimal JSON-RPC-over-stdio client); `tools/list` → register `mcp__<server>__<tool>` (permission from `settings.mcp.permissions[server]` default `network`); `tools/call` forwarded. Servers start lazily and are cached per daemon. Test fixture: `tests/fixtures/mcp/echo_server.py` (stdio, one tool `echo`).

## 7. Vendoring for M2
Use `uv run python scripts/verify_vendor_integrity.py --add <upstream> <dest> --reason "..."` for each file taken from Hermes (candidates in `docs/vendoring-map.md` M2 section). Minimum expected: `tools/ansi_strip.py`, `tools/url_safety.py`, `tools/fuzzy_match.py`, `tools/binary_extensions.py`, `tools/tool_output_limits.py`, `tools/path_security.py`. Vendored code lives under `core/snowpea_core/vendor/hermes/tools/` and is imported by our adapters only. If you modify a vendored file, run `--update-patch <dest>`. The integrity script must pass at the end (`HERMES_REF=/tmp/hermes-ref`).

## 8. Tests
- `tests/test_tools_contract.py`: every tool name in §2 present in `tool.list`; categories/permission tags correct; media inactive → configure → active without restart; `web_search` with `ddgs` mocked (monkeypatch HTTP) returns hits; SSRF guard rejects `http://127.0.0.1/`; MCP echo fixture appears as `mcp__fixture-echo__echo` and round-trips; search provider registry order and tags.
- `tests/test_backends.py`: local/docker/ssh hostname differ; ssh write not visible locally; docker & ssh tests `pytest.skip` when `docker` CLI unavailable or the compose fixture cannot start (never fail CI for missing infra).
