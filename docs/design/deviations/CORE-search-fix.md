# Deviations — CORE-search-fix (honest search providers, and a guard on config writes)

Two user-facing failures, one root cause each. A user set `search.provider: exa_free`; `web_search`
"worked" and never mentioned that it had answered from ddgs, because `exa_free` was a catalog row
with an endpoint string and no client, tagged `no key` so `available()` said yes. Separately, an
agent asked to *show* `settings.json` rewrote it, because `write_file` carries the `write` tag and
`write` is a silent `allow` in accept and auto mode. Recorded here per the deviations-log convention
(`docs/design/deviations/README.md`).

## Search providers

1. **Every catalog id now has a real client; `ThinProvider` is retired from the catalog.** The class
   stays in `tools/search_providers/providers.py` so a plugin can still register a placeholder and
   get an honest refusal, but none of the thirteen ids use it. `test_every_catalog_id_has_a_real_client`
   pins that.

2. **Most `*_free` ids are free *tiers*, not keyless endpoints.** This was
   checked against the live APIs rather than inferred from the docs pages:

   | endpoint | keyless response |
   | --- | --- |
   | `POST https://api.exa.ai/search` | `402 Payment required to access this resource` |
   | `POST https://api.parallel.ai/v1beta/search` | `401 No API key provided (C.0)` |
   | `POST https://api.keenable.ai/v1/search` | `401 Missing authentication` |
   | `POST https://api.tavily.com/search` | `401 Unauthorized: missing or invalid API key` |
   | `POST https://api.x.ai/v1/chat/completions` | `401 unauthenticated:no-credentials` |
   | `POST https://api.firecrawl.dev/v1/search` | **`200` with real results** |

   `keenable_free`, `parallel_free` and `tavily` therefore remain `key required` and unavailable
   without a key. `exa_free` now intentionally differs: it uses Exa's official anonymous hosted
   MCP at `https://mcp.exa.ai/mcp`, while paid `exa` keeps the keyed REST endpoint.

3. **Firecrawl Cloud is tagged `key optional`, not `no key`.** Its cloud `/v1/search` answered two
   distinct keyless queries with real results, so calling it "key required" would be as dishonest as
   the bug being fixed; calling it `no key` would promise a rate-limited third-party endpoint as a
   default. It stays in the fallback chain, last, and `key optional` is the tag that matches what was
   observed. If that endpoint starts refusing keyless calls, the fallback reports the HTTP status
   like any other failure — nothing silently degrades.

4. **`FREE_CHAIN` is now the *fallback* chain, ordered by what a user is likely to have configured**
   (`ddgs`, `exa_free`, `searxng`, `brave_free`, `tavily`, `firecrawl_selfhost`, `firecrawl`). Every entry is
   still gated by `available()`, so the keyed ones are only reached once they have a key. The old
   chain listed unwired `*_free` entries ahead of real clients, which is precisely
   how the fall-through went unnoticed.

5. **The fallback is reported in three places, not one.** `ToolResult` gained a `meta` dict
   (`provider`, `fallback_from`, `reason`); the text handed to the model starts with
   `[search via ddgs — fallback from exa_free: anonymous MCP rate limit]`; and the
   session gets one non-fatal `error{code:"search_provider_unavailable"}` event. The event is emitted
   **once per session**, tracked by an attribute on the `Session` object rather than a new field, to
   avoid colliding with concurrent work in `session/`. A per-turn event would have turned a warning
   into noise on a research-heavy turn.

6. **`tool.list` carries `ToolInfo.provider`.** The string is the configured id, or
   `exa_free → ddgs` when the configured id cannot run. It is filled in `tool_list_handler` rather
   than in `Tool.info()`, because the registry has no settings to read.

7. **`snowpea search test` runs in-process, with no daemon.** It reads
   `$SNOWPEA_HOME/settings.json` directly and walks the same chain `web_search` does. Going through
   the daemon would have meant a new RPC method for a diagnostic whose whole point is to work when
   the user is unsure what is configured. Exit code 2 when nothing answered.

## The `config` permission tag

8. **A sixth permission tag, not a special case inside `write_file`.** `PermissionTag` gained
   `config`, the mode matrix gained a row (plan `deny`, accept `ask`, **auto `ask`**), and
   `PROTOCOL_VERSION` went to `1.3.0`. Auto mode asking is the deliberate part: an unattended agent
   rewriting `settings.json` is the failure being fixed, so "nobody is watching" is a reason to ask,
   not a reason to skip.

9. **The tag is resolved per call, through `Tool.permission_for`.** `effective_permission(tool, args,
   session, core)` in `tools/registry.py` is what the agent loop judges; `tools/config_guard.py`
   returns `config` when the resolved path is under `$SNOWPEA_HOME`, or is a project's
   `.snowpea/settings.json` or `.snowpea/credentials.json`, and `write` otherwise. Resolving the path
   means `../../.snowpea/settings.json` and symlinks into the home directory are caught. A hook that
   raises falls back to the tool's static tag, so a broken hook can never *widen* permission.

   The project rule is those two filenames, **not** the whole `.snowpea/` directory. A first cut
   matched any `.snowpea` path segment and broke `/team`: its worktrees live at
   `.snowpea/worktrees/<team>-<n>/`, so every worker's source edit became a config approval and the
   four `tests/test_team_worktree.py` cases failed. `.snowpea/` holds working state (worktrees,
   skills, agents) as well as configuration, and only the configuration part is `config`.

10. **`config` is unpromotable and uncacheable.** `PermissionPolicy.decide` skips the allowlist for
    tags in `UNPROMOTABLE`, and the approval queue is called with `cacheable=False`, so answering
    "always" on one settings write does not silence the next one. Without that, a single wide answer
    would have re-opened the hole through the `(session, tool, "")` cache key.

11. **`ApprovalRequest.note`** carries "modifies snowpea configuration" to the surface, rather than
    the surface inferring it from the tag. The headless CLI prompt prints it above the question; a
    client that ignores the field is unaffected.

12. **`settings_get` / `settings_set` exist so the model has an honest path.** `settings_get` is
    `read` and masks secrets; `settings_set` is `config` and validates the whole document through the
    pydantic model before persisting, then calls `Core.adopt_settings` so a running daemon sees the
    change. The system prompt (`agent/agent.py::CONFIG_RULE`) states the rule explicitly rather than
    relying on the tool descriptions.

## Known limits

- The guard resolves `$SNOWPEA_HOME` lazily through `config.paths.resolve_home`, which reads the
  environment. `python -m snowpea_core --home DIR` therefore exports `SNOWPEA_HOME` before starting
  the daemon, so a non-default home is still recognised. A `Core` built in-process with a `home=`
  argument and no environment variable is covered only by the `.snowpea` segment rule.
- `config_guard` resolves paths against the **local** filesystem. Under a docker or ssh backend a
  remote `$SNOWPEA_HOME` is not recognised as config. Within the local case the guard errs toward
  asking, which is the safe direction.
- A project file named `settings.json` or `credentials.json` that happens to sit in a directory
  called `.snowpea` is treated as configuration even if the project means something else by it.
- The Keenable and Parallel response shapes are parsed generically (`results` / `data` / `hits`,
  `excerpts` joined into the snippet). Neither could be exercised against a live key here; the
  request shape is confirmed by the `401` each returns, the response parsing is not.
- Firecrawl Cloud's keyless search is an observation, not a documented guarantee.
