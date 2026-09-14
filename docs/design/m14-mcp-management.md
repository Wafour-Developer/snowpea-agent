# M14 — MCP server management (core RPC + CLI + TUI + desktop)

Status: **binding for story CORE-mcp-manage**. Added 2026-09-14 at the user's
request ("mcp 서버 연동 지원 — TUI에서도 추가 가능해야 하고 Desktop에서도").

## 1. What exists

`tools/mcp_client.py` already discovers `.mcp.json` (`$SNOWPEA_HOME`, the
project workdir, each installed plugin) plus `settings.mcp.servers`, starts
servers lazily (stdio or SSE/streamable HTTP by `url`) and registers their
tools as `mcp__<server>__<tool>` with permission tag `mcp.permissions[server]`
(default `network`). There is **no way to add, remove, inspect or test a server
from a client** — users edit JSON by hand. This milestone adds that surface.

## 1b. Conventions adopted (surveyed 2026-09-14)

The user asked for the most general shape, so the contract is the intersection
of Claude Code (`claude mcp add … -- cmd args`, `.mcp.json`, scopes, `/mcp`),
Codex (`codex mcp add|list|get|remove|login`, `enabled`, `startup_timeout_sec`,
`tool_timeout_sec`, `enabled_tools`/`disabled_tools`), Hermes
(`hermes mcp add --url|--command --args … --env`, `test`, `configure` tool
picker, discovery-first install "Connected! Found N tools — enable all /
select", curated catalog + presets, `mcp_security` shape checks, `login`) and
opencode (`mcp.<name>.{type,command,url,environment,headers,enabled,timeout}`):

| concern | adopted form | source |
|---|---|---|
| config file | Claude Code `.mcp.json` `mcpServers` (project + global) | Claude Code, most widely shared |
| entry keys | `type: stdio\|http\|sse` (optional, inferred), `command`, `args`, `env`, `url`, `headers`, `cwd`, `disabled`, `timeoutSec` (startup + tools/list), `toolTimeoutSec`, `tools: {include?: [], exclude?: []}` | Claude Code (`type`), Codex (timeouts, enabled), Hermes (tools include/exclude) |
| CLI verbs | `snowpea mcp list \| get <name> \| add \| add-json <name> '<json>' \| remove <name> \| test <name> \| configure <name> \| reload [name]` | Claude Code + Codex + Hermes union |
| `add` syntax | `snowpea mcp add <name> [--scope project\|global] [--env K=V…] [--header K=V…] [--timeout S] (--url <u> \| -- <cmd> [args…])`; also accepts Hermes-style `--command <cmd> --args …` | Claude Code/Codex (`--` passthrough), Hermes (`--command/--args`) |
| discovery-first UX | `add` and the desktop/TUI forms always `test` first: "Connected — N tools" then *Enable all / Select tools / Cancel*; nothing is saved if the probe fails unless `--no-test` | Hermes |
| status in session | `/mcp` lists servers with state + tool counts; `/mcp add` opens the form; `/mcp test <name>` | Claude Code `/mcp`, Hermes |
| safety | port Hermes `mcp_security.validate_mcp_server_entry` shapes (IOC substrings, shell interpreter + egress, persistence writes) as warnings that `add` refuses without `--force`; argv only, never a shell string; secrets never echoed | Hermes (MIT, noted in deviations) |
| catalog | `mcp.catalog {}` → curated list (id, label, description, transport, install entry, needs: [env keys]); `snowpea mcp add <name> --preset <id>`; desktop sheet shows the catalog as a starting point | Hermes catalog/presets |
| remote auth | reserved: `auth: header\|oauth`; `mcp.login` + OAuth flow are **M14b**, not in this story | Claude Code/Codex/Hermes `login` |

## 2. Scope of a server

| scope | file | who wins |
|---|---|---|
| `project` (default when a session/workdir is given) | `<workdir>/.mcp.json` | project entry overrides a global one of the same name |
| `global` | `$SNOWPEA_HOME/.mcp.json` | |
| `plugin` | plugin's own `.mcp.json` | read-only from the management API (remove the plugin instead) |
| `settings` | `settings.json` `mcp.servers` | read-only here (legacy inline form); listed with `scope: settings` |

The file format stays the Claude Code one so a project's `.mcp.json` remains
shareable:

```json
{"mcpServers": {"name": {"command": "npx", "args": ["-y", "@x/server"], "env": {"KEY": "…"}},
                "remote": {"url": "https://host/mcp", "headers": {"Authorization": "Bearer …"}}}}
```

`headers` is new (optional; used for `url` servers). Secrets stay in the file
the user chose; the API never echoes `env`/`headers` values — it returns
`envKeys: [..]`, `headerKeys: [..]` and the client shows `KEY=•••`.

## 3. Protocol (additive, no version bump — protocol stays 1.5.0)

All methods live in `server/mcp_handlers.py`, registered like `lsp_handlers`.

* `mcp.list {sessionId?, workdir?}` → `{servers: [McpServerInfo]}`
  `McpServerInfo = {name, scope: project|global|plugin|settings, transport: stdio|http,
  command?, args: [], url?, envKeys: [], headerKeys: [], cwd?, permission: str,
  disabled: bool, state: stopped|starting|ready|error, error?: str,
  toolCount: int, tools: [{name, description}] (only when ready), plugin?: str}`
* `mcp.add {name, scope: project|global, workdir?, command?, args?, env?, url?, headers?, cwd?, permission?, force?}` →
  `{ok, path}`. Validates: name `^[a-zA-Z0-9_-]{1,64}$`, exactly one of command/url,
  no shell strings (command + args list only). Existing name in that scope →
  `mcp_exists` error unless `force`. After writing, calls `mcp.reload` semantics
  for that server and returns.
* `mcp.remove {name, scope, workdir?}` → `{ok}`; stops the server if running,
  drops its tools, `not_found` when absent; `read_only` for plugin/settings scope.
* `mcp.update {name, scope, workdir?, patch: {…same keys as add…, disabled?}}` →
  `{ok}`; `disabled: true` keeps the entry but never starts it (stored as
  `"disabled": true` in the entry; `mcp_client.parse` must tolerate the key).
* `mcp.test {name?, scope?, workdir?, command?, args?, env?, url?, headers?}` →
  `{ok, state, tools: [{name, description}], error?, elapsedMs}`. Either an
  existing server (by name) or an unsaved draft (inline fields): starts it with
  an 20 s cap, runs `tools/list`, shuts it down if it was a draft. Never throws
  for a broken server — `ok: false, error`.
* `mcp.reload {name?}` → `{ok, servers: [name]}`; restarts one or all.
* `mcp.catalog {}` → `{entries: [{id, label, description, transport, entry: {command|url,…}, needs: [envKey], homepage}]}`
  — curated presets shipped in `tools/mcp_catalog.py` (github, filesystem, fetch,
  playwright, context7, exa, memory, sequential-thinking, snowpea-studio, …).
* `mcp.add`/`mcp.update` accept the full entry keys of §1b (`type`, `timeoutSec`,
  `toolTimeoutSec`, `tools.include/exclude`, `disabled`) and `preset: <id>` as a
  shorthand that copies the catalog entry before applying explicit fields.
  `mcp.add` runs the security check; findings come back as `mcp_unsafe` unless
  `force`. Default `test: true` probes first and returns `tools` so a client can
  offer "select tools" before the entry is saved with `tools.include`.
* event `mcp.changed {name, scope, state, toolCount, error?}` on every state
  transition and after add/remove/update, so the TUI HUD and the desktop screen
  update without polling. Sent to every connection (like `settings.changed`).
* `tool.list` already carries category `mcp`; add `server: <name>` to those
  entries so clients can group them.

Errors: `mcp_exists`, `mcp_not_found`, `mcp_read_only`, `mcp_invalid` (message
says which field), `mcp_start_failed`.

## 4. Command and CLI

* `/mcp` (core command, `commands/mcp_cmd.py`, like `/skill`):
  `/mcp` or `/mcp list` — table name · scope · transport · state · tools;
  `/mcp get <name>`; `/mcp add <name> [--global] [--env K=V…] [--header K=V…] [--timeout S] [--preset id] (--url <https://…> | -- <command> [args…] | --command <cmd> --args …)`;
  `/mcp add-json <name> '<json>'`; `/mcp remove <name> [--global]`; `/mcp test <name>`;
  `/mcp configure <name>` (tool include list; TUI shows a checklist);
  `/mcp enable|disable <name>`; `/mcp reload [name]`; `/mcp catalog`. Output is plain text through `ctx.say`. The TUI adds
  an interactive form when `/mcp add` is typed with no args (name → transport →
  command/url → env → scope), the same way `/skill create` does.
* CLI: `snowpea mcp list|get|add|add-json|remove|test|configure|reload|catalog` mirroring the command (JSON
  with `--json`); documented in `docs/cli.md` and `docs/manual/{en,ko}/plugins.md`
  (rename that page's MCP section to "MCP servers" and link from setup).

## 5. Surfaces

* TUI: `/mcp` completion (sub-actions + server names), HUD segment `mcp 2/3`
  (ready/total) hidden when zero, `mcp.changed` toasts on error only.
* Desktop: **Settings → MCP servers** screen (`views/settings/McpServers.vue`):
  table (name, scope chip, transport, state dot, tools count, permission),
  "Add server" sheet (Tabs: Command / URL; fields per §3; "Test" button runs
  `mcp.test` on the draft and lists the tools before saving; scope picker
  project/global; Advanced: env, headers, cwd, permission); row actions Edit /
  Test / Disable / Remove (plugin/settings rows: View only + "from plugin X").
  A `mcp.changed` listener updates state dots live. Link from Tools settings.

## 6. Acceptance (AC-48…52)

* AC-48: `mcp.add` with a stdio server writes `<workdir>/.mcp.json`, the server
  starts, its tools show in `tool.list` with `server` set, `mcp.changed` fires.
* AC-49: `mcp.test` on a bad command returns `ok:false` with the spawn error in
  under 20 s and leaves nothing running.
* AC-50: `mcp.remove` stops the server and its tools vanish from `tool.list`
  in the same session; a plugin server returns `mcp_read_only`.
* AC-51: `mcp.list` never returns env/header values — only key names.
* AC-52: `/mcp add` in the TUI with no args walks the form and ends with the
  server listed; the desktop sheet's Test shows the tool list before Save.

## 7. Out of scope

OAuth for remote MCP servers (`mcp.login`, M14b — the `auth` key is reserved),
MCP resources/prompts (tools only, as today), running snowpea itself as an MCP
server (Hermes `mcp serve`, later).
