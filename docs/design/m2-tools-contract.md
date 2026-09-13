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

The `permission` column is the **declared** tag — what `tool.list` reports and what a hook or a surface may rely on. A tool may additionally implement `Tool.permission_for(args, session, core)` to re-tag an individual call (added in v0.1.x); a per-call tag may only be *at least as strict* as the declared one for the call it is given, so a failing `permission_for` can never widen a tool. `write_file`/`edit_file` re-tag to `config` via `tools/config_guard.py` (M1 §7) and the audio tools resolve `read` vs `network` from the configured backend.

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

The `delegate`, `schedule` and `memory` rows are no longer stubs (M5/M7 shipped). Two audio tools joined the catalog (CORE-multimodal, see `docs/design/m10-multimodal-audio-contract.md`):

| category | tools | permission |
|---|---|---|
| audio | transcribe_audio, text_to_speech | declared `read` / `network`, **re-tagged per call** |

`text_to_speech` is no longer studio-only: `tools/audio_tools.py` owns that name with the same schema, adds an optional `play`, and runs the whole backend chain. `tools/media.py` keeps the mapping `text_to_speech → generate_speech` in its `FORWARDS` table (that is how the audio code reaches studio) but registers no speech tool of its own. Both audio tools are `inactive` with a reason until a backend exists, mirroring the media tools, and both implement `permission_for`: `network` when the resolved backend is hosted (`openai`, `studio`), `read` when it is local. `text_to_speech` is *declared* `network` — the stricter of its two possibilities — so a failing hook can never widen it.

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
Registry order (also the setup screen order, `PROVIDER_ORDER` in `tools/search_providers/__init__.py`): `ddgs`(★ default, no key) → `brave_free`(key) → `exa_free`(no key) → `keenable_free`(key) → `parallel_free`(key) → `tavily`(key) → `searxng`(self-hosted, `SEARXNG_URL`) → `firecrawl_selfhost`(self-hosted) → `exa`(paid) → `keenable`(paid) → `parallel`(paid) → `firecrawl`(paid, key optional) → `xai_grok`(paid).

**Every catalog id MUST have a real client** (changed in v0.1.x, CORE-search-fix). The placeholder `ThinProvider` is retired from the catalog — it stays in `tools/search_providers/providers.py` so a plugin can register an honest refusal — and `tests/test_search_providers.py::test_every_catalog_id_has_a_real_client` pins that. The M2 text that allowed "thin HTTP clients with the documented endpoint" for all but four ids is **withdrawn**: it is what let `exa_free` answer from `ddgs` while claiming to be Exa.

Tags are what the live endpoints actually do, verified keyless:
- `exa_free` is **no key** and is the one `*_free` id that really is keyless — it drives Exa's official anonymous hosted MCP at `https://mcp.exa.ai/mcp` (`ExaMcpProvider`), not `api.exa.ai`. Paid `exa` keeps the keyed REST endpoint.
- `keenable_free`, `parallel_free` and `tavily` are free *tiers*, not keyless endpoints; all three answer `401` without a key, so they are tagged **key required** and `available()` is False without one.
- `firecrawl` (cloud) is **key optional**: its `/v1/search` answers keyless queries with real results.

`FREE_CHAIN` is the **fallback** chain, ordered by what a user is likely to have configured (`tools/search_providers/__init__.py`): `ddgs` → `exa_free` → `searxng` → `brave_free` → `tavily` → `firecrawl_selfhost` → `firecrawl`. Every entry is still gated by `available()`. `web_search` uses `settings.search.provider` and then walks that chain. A fallback MUST be reported in three places (`tools/web.py`): `ToolResult.meta{provider, fallback_from, reason}`; a `[search via ddgs — fallback from exa_free: …]` prefix on the text handed to the model; and one non-fatal `error{code:"search_provider_unavailable"}` session event **per session** (not per turn). `tool.list` carries `ToolInfo.provider`, written as `exa_free → ddgs` when the configured id cannot run. The M2 wording "log which" is no longer sufficient.

`web_extract` fetches with SSRF guard (block private/link-local; vendored `tools/url_safety.py` from Hermes) and truncates to `settings.tools.max_output_chars` (default 20k).

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
