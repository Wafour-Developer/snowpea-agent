<!-- GENERATED — do not edit. Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py. -->

# Snowpea protocol

- **Protocol version:** `0.1.0` (semver)
- **Source of truth:** `core/snowpea_core/server/protocol.py`
- **Generator:** `uv run python scripts/gen_protocol.py`
- **Bindings:** `sdk/src/protocol.ts` (generated alongside this file — never hand-edit)

## Transport

The daemon listens on a loopback-only port chosen at start-up and records it in `$SNOWPEA_HOME/daemon.json` as `{port, pid, token, startedAt, protocolVersion}`.

| surface | endpoint | purpose |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0, bidirectional |
| HTTP `GET` | `http://127.0.0.1:<port>/health` | liveness probe |
| HTTP `GET` | `http://127.0.0.1:<port>/protocol.json` | this schema as JSON |
| HTTP `GET` | `http://127.0.0.1:<port>/version` | server and protocol versions |

Every state-changing method lives on the WebSocket only; the HTTP endpoints are read-only operational helpers.

## Handshake

Immediately after connecting, the client calls `system.hello` with the daemon token, its own `clientVersion`, and the `protocolVersion` it was built against. Any other method before a successful `system.hello` fails with `unauthorized`. A mismatched major version is rejected with `protocol_incompatible`.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "system.hello",
  "params": {
    "token": "<contents of $SNOWPEA_HOME/token>",
    "clientVersion": "0.1.0",
    "protocolVersion": "0.1.0"
  }
}
```

Server capabilities advertised in the `system.hello` result:

- `approvals`
- `commands`
- `sessions`
- `tools`

## Method index

| method | direction | summary |
|---|---|---|
| [`agent.bindChannel`](#agentbindchannel) | client → server | Route a gateway channel to a named agent. |
| [`agent.create`](#agentcreate) | client → server | Define a named agent from a description. |
| [`agent.delete`](#agentdelete) | client → server | Delete a named agent. |
| [`agent.list`](#agentlist) | client → server | List the named agents that are defined. |
| [`agent.spawn`](#agentspawn) | client → server | Run a named agent on a task. |
| [`approval.list`](#approvallist) | client → server | List tool calls still waiting for a decision. |
| [`approval.request`](#approvalrequest) | server → client | Ask the client to approve a tool call. |
| [`approval.respond`](#approvalrespond) | client → server | Answer a pending approval and unblock the turn. |
| [`backend.set`](#backendset) | client → server | Choose where a session's tools execute: local, docker or ssh. |
| [`command.list`](#commandlist) | client → server | List the slash commands available to a session. |
| [`command.run`](#commandrun) | client → server | Run a slash command; the only execution path for them. |
| [`gateway.bind`](#gatewaybind) | client → server | Attach the daemon to a chat platform channel. |
| [`gateway.list`](#gatewaylist) | client → server | List live gateway bindings. |
| [`gateway.unbind`](#gatewayunbind) | client → server | Detach a gateway binding. |
| [`job.cancel`](#jobcancel) | client → server | Cancel a scheduled job. |
| [`job.list`](#joblist) | client → server | List scheduled jobs and their next run times. |
| [`job.runNow`](#jobrunnow) | client → server | Fire a scheduled job immediately. |
| [`job.schedule`](#jobschedule) | client → server | Schedule a prompt to run unattended. |
| [`memory.search`](#memorysearch) | client → server | Recall stored memories matching a query. |
| [`memory.write`](#memorywrite) | client → server | Store a memory with tags. |
| [`permission.allowlist.add`](#permissionallowlistadd) | client → server | Promote a pattern from ask to allow. |
| [`permission.allowlist.list`](#permissionallowlistlist) | client → server | List stored allowlist patterns. |
| [`permission.allowlist.remove`](#permissionallowlistremove) | client → server | Delete an allowlist pattern by id. |
| [`provider.configure`](#providerconfigure) | client → server | Store settings and credentials for a provider. |
| [`provider.list`](#providerlist) | client → server | List chat providers and whether they are configured. |
| [`provider.loginWeb`](#providerloginweb) | client → server | Start a browser-based login flow for a provider. |
| [`session.close`](#sessionclose) | client → server | Close a session and release its resources. |
| [`session.create`](#sessioncreate) | client → server | Open a session rooted at a working directory. |
| [`session.interrupt`](#sessioninterrupt) | client → server | Stop the running turn as soon as possible. |
| [`session.list`](#sessionlist) | client → server | List every live session. |
| [`session.prompt`](#sessionprompt) | client → server | Send user text to a session and start a turn. |
| [`session.resume`](#sessionresume) | client → server | Replay the events a disconnected client missed. |
| [`session.setMode`](#sessionsetmode) | client → server | Switch a session between plan, accept and auto. |
| [`skill.install`](#skillinstall) | client → server | Install a skill from a path, URL or registry. |
| [`skill.list`](#skilllist) | client → server | List installed skills. |
| [`skill.reload`](#skillreload) | client → server | Reload skills from disk without restarting. |
| [`skill.remove`](#skillremove) | client → server | Delete an installed skill or plugin. |
| [`skill.search`](#skillsearch) | client → server | Search available skills. |
| [`system.health`](#systemhealth) | client → server | Liveness probe; answers as long as the daemon serves requests. |
| [`system.hello`](#systemhello) | client → server | Authenticate a connection and agree on the protocol version. |
| [`system.info`](#systeminfo) | client → server | Report the daemon's version, pid, port, start time and home. |
| [`system.shutdown`](#systemshutdown) | client → server | Ask the daemon to shut down gracefully. |
| [`team.start`](#teamstart) | client → server | Split a task across parallel workers. |
| [`team.status`](#teamstatus) | client → server | Inspect a team's task board. |
| [`tool.list`](#toollist) | client → server | List the tools registered for a session. |

## Methods

### `agent.bindChannel`

*Direction:* client → server

Route a gateway channel to a named agent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string` | yes | Gateway channel that will reach the agent. |
| `credentialsRef` | `string \| null` | no | Credential entry the platform account uses; defaults to the platform name, e.g. 'telegram' for channel 'telegram:123'. |
| `name` | `string` | yes | Agent to bind. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `agent.create`

*Direction:* client → server

Define a named agent from a description.

**Params**

| field | type | required | description |
|---|---|---|---|
| `description` | `string` | yes | Natural-language brief the daemon turns into an agent. |
| `name` | `string \| null` | no | Name for the agent; when omitted the daemon takes the generated one. |
| `named` | `boolean` | no | Also register a persistent named instance: its own session, the memory namespace agent:<name>, and rows that survive a daemon restart. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Name assigned to the new agent. |
| `path` | `string \| null` | no | Where the definition was written. |

### `agent.delete`

*Direction:* client → server

Delete a named agent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Agent to delete. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `agent.list`

*Direction:* client → server

List the named agents that are defined.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `agents` | `({ agentId?: string \| null; bindings?: string[]; channel?: string \| null; channels?: string[]; description?: string; jobs?: string[]; kind?: string; name: string; namespace?: string \| null; parentSessionId?: string \| null; path?: string \| null; sessionId?: string \| null; source?: string; status?: string \| null; task?: string \| null; })[]` | no | Defined named agents. |

### `agent.spawn`

*Direction:* client → server

Run a named agent on a task.

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Agent to run. |
| `sessionId` | `string \| null` | no | Parent session, when spawned from one. |
| `task` | `string` | yes | Task handed to the agent. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Id correlating the subagent.* events. |

### `approval.list`

*Direction:* client → server

List tool calls still waiting for a decision.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ args?: Record<string, unknown>; requestId: string; risk?: string; scopeHint?: "once" \| "session" \| "project" \| "always"; sessionId: string; timeoutSec?: number; tool: string; })[]` | no | Approvals still pending. |

### `approval.request`

*Direction:* server → client

Ask the client to approve a tool call.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments it wants to use. |
| `requestId` | `string` | yes | Id to answer with approval.respond. |
| `risk` | `string` | no | Risk hint for the UI. |
| `scopeHint` | `"once" \| "session" \| "project" \| "always"` | no | Scope the UI should preselect. |
| `sessionId` | `string` | yes | Session whose turn is blocked. |
| `timeoutSec` | `number` | no | Seconds before the request auto-denies. |
| `tool` | `string` | yes | Tool the model wants to run. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes | The human's decision. |
| `scope` | `"once" \| "session" \| "project" \| "always"` | no | How long the decision applies. |

### `approval.respond`

*Direction:* client → server

Answer a pending approval and unblock the turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes | allow runs the tool, deny ends the turn. |
| `requestId` | `string` | yes | Request being answered. |
| `scope` | `"once" \| "session" \| "project" \| "always"` | no | How long the decision applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `backend.set`

*Direction:* client → server

Choose where a session's tools execute: local, docker or ssh.

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | no | Backend settings, e.g. container or SSH target. |
| `kind` | `"local" \| "docker" \| "ssh"` | yes | Where tools execute. |
| `sessionId` | `string` | yes | Session whose execution backend changes. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `command.list`

*Direction:* client → server

List the slash commands available to a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema?: Record<string, unknown>; name: string; source?: string; summary: string; })[]` | no | Available slash commands. |

### `command.run`

*Direction:* client → server

Run a slash command; the only execution path for them.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `string` | no | Raw argument string, as typed after the name. |
| `name` | `string` | yes | Command name without the leading slash. |
| `sessionId` | `string` | yes | Session to run the command in. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes | Id of the started turn; turn.done carries it back. |

### `gateway.bind`

*Direction:* client → server

Attach the daemon to a chat platform channel.

**Params**

| field | type | required | description |
|---|---|---|---|
| `channelId` | `string \| null` | no | Restrict the binding to one chat, channel or room. |
| `credentialsRef` | `string` | yes | Name of the credential to use: a key in credentials.json or an env var. |
| `platform` | `string` | yes | Chat platform key: telegram, discord or slack. |
| `target` | `Record<string, unknown>` | yes | What the conversation talks to: {'agent': name}, {'session': id} or {'new_session': {'workdir': path, 'mode': mode}}. |
| `userId` | `string \| null` | no | Platform user allowed to answer approvals from chat. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Id used to unbind later. |

### `gateway.list`

*Direction:* client → server

List live gateway bindings.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindings` | `({ bindingId: string; channelId?: string \| null; credentialsRef?: string; platform: string; state?: "active" \| "inactive"; target: string; userId?: string \| null; })[]` | no | Live gateway bindings. |

### `gateway.unbind`

*Direction:* client → server

Detach a gateway binding.

**Params**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Binding to remove. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `job.cancel`

*Direction:* client → server

Cancel a scheduled job.

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Target job. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `job.list`

*Direction:* client → server

List scheduled jobs and their next run times.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobs` | `({ channel?: string \| null; enabled?: boolean; jobId: string; kind?: "cron" \| "once" \| "interval"; lastRunAt?: string \| null; lastStatus?: "ok" \| "error" \| "denied_by_timeout" \| null; mode?: "plan" \| "accept" \| "auto"; nextRunAt?: string \| null; spec: string; state?: "scheduled" \| "running" \| "cancelled"; task: string; })[]` | no | Known jobs. |

### `job.runNow`

*Direction:* client → server

Fire a scheduled job immediately.

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Target job. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `job.schedule`

*Direction:* client → server

Schedule a prompt to run unattended.

**Params**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no | Named agent that runs the task. |
| `channel` | `string \| null` | no | Gateway channel that receives the output. |
| `mode` | `"plan" \| "accept" \| "auto"` | no | Permission mode for the unattended run. |
| `spec` | `string` | yes | Cron expression or natural-language schedule. |
| `task` | `string` | yes | Prompt run on each firing. |
| `workdir` | `string \| null` | no | Working directory for the run; defaults to the daemon's home. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Id of the scheduled job. |
| `nextRunAt` | `string \| null` | no | UTC ISO-8601 time of the first firing. |

### `memory.search`

*Direction:* client → server

Recall stored memories matching a query.

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no | Maximum number of hits. |
| `namespace` | `string \| null` | no | Memory namespace to search; defaults to "default". Never crosses namespaces. |
| `query` | `string` | yes | Free-text query. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `hits` | `({ id: string; score?: number; tags?: string[]; text: string; })[]` | no | Matches, best first. |

### `memory.write`

*Direction:* client → server

Store a memory with tags.

**Params**

| field | type | required | description |
|---|---|---|---|
| `namespace` | `string \| null` | no | Memory namespace to write into; defaults to "default". |
| `tags` | `string[]` | no | Tags for later filtering. |
| `text` | `string` | yes | Text to remember. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Id of the stored memory. |

### `permission.allowlist.add`

*Direction:* client → server

Promote a pattern from ask to allow.

**Params**

| field | type | required | description |
|---|---|---|---|
| `pattern` | `string` | yes | Glob or command prefix promoted from ask to allow. |
| `scope` | `"session" \| "project" \| "always"` | no | Where the pattern is stored. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes | Id used to remove the pattern later. |

### `permission.allowlist.list`

*Direction:* client → server

List stored allowlist patterns.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"session" \| "project" \| "always" \| null` | no | Filter by scope; omit for all. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patterns` | `({ pattern: string; patternId: string; scope: "session" \| "project" \| "always"; })[]` | no | Stored allowlist entries. |

### `permission.allowlist.remove`

*Direction:* client → server

Delete an allowlist pattern by id.

**Params**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes | Pattern to delete. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `provider.configure`

*Direction:* client → server

Store settings and credentials for a provider.

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | no | Vendor-specific settings, including credentials. |
| `vendor` | `string` | yes | Vendor to configure. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `provider.list`

*Direction:* client → server

List chat providers and whether they are configured.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `providers` | `({ authMethods?: string[]; configured?: boolean; default?: boolean; defaultModel?: string; label?: string; models?: string[]; vendor: string; })[]` | no | Known chat providers. |

### `provider.loginWeb`

*Direction:* client → server

Start a browser-based login flow for a provider.

**Params**

| field | type | required | description |
|---|---|---|---|
| `method` | `string` | yes | Login flow to start, e.g. 'oauth'. |
| `vendor` | `string` | yes | Vendor to log into. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.close`

*Direction:* client → server

Close a session and release its resources.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.create`

*Direction:* client → server

Open a session rooted at a working directory.

**Params**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no | Named agent whose persona to load. |
| `maxConcurrent` | `number \| null` | no | Override for concurrent subagents. |
| `mode` | `"plan" \| "accept" \| "auto" \| null` | no | Starting mode; defaults to the project setting. |
| `model` | `string \| null` | no | Model id; defaults to the provider's default. |
| `originSurface` | `string \| null` | no | Surface that owns approvals for this session (TUI, gateway, ...). |
| `provider` | `string \| null` | no | Chat provider vendor; defaults to configured. |
| `workdir` | `string` | yes | Absolute path the session operates in. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Id of the new session. |

### `session.interrupt`

*Direction:* client → server

Stop the running turn as soon as possible.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.list`

*Direction:* client → server

List every live session.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessions` | `({ createdAt: string; mode: "plan" \| "accept" \| "auto"; model?: string \| null; originSurface?: string \| null; provider?: string \| null; seq?: number; sessionId: string; workdir: string; })[]` | no | Every live session. |

### `session.prompt`

*Direction:* client → server

Send user text to a session and start a turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ kind?: "file" \| "image" \| "text"; mimeType?: string \| null; path?: string \| null; text?: string \| null; })[] \| null` | no | Files or images to include. |
| `sessionId` | `string` | yes | Session to prompt. |
| `text` | `string` | yes | User text; a leading '/' is parsed as a slash command. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes | Id of the started turn; turn.done carries it back. |

### `session.resume`

*Direction:* client → server

Replay the events a disconnected client missed.

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number \| null` | no | Replay only events with a greater seq. |
| `sessionId` | `string` | yes | Session to resume. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; sessionId: string; ts: string; })[]` | no | Missed events in seq order. |
| `sessionId` | `string` | yes | Session that was resumed. |

### `session.setMode`

*Direction:* client → server

Switch a session between plan, accept and auto.

**Params**

| field | type | required | description |
|---|---|---|---|
| `mode` | `"plan" \| "accept" \| "auto"` | yes | New permission mode. |
| `sessionId` | `string` | yes | Session to change. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mode` | `"plan" \| "accept" \| "auto"` | yes | Mode now in effect. |

### `skill.install`

*Direction:* client → server

Install a skill from a path, URL or registry.

**Params**

| field | type | required | description |
|---|---|---|---|
| `source` | `string` | yes | Path, URL or registry name to install from. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.list`

*Direction:* client → server

List installed skills.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `({ id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; source?: string; summary?: string; })[]` | no | Installed skills. |

### `skill.reload`

*Direction:* client → server

Reload skills from disk without restarting.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.remove`

*Direction:* client → server

Delete an installed skill or plugin.

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Installed plugin or skill to delete. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.search`

*Direction:* client → server

Search available skills.

**Params**

| field | type | required | description |
|---|---|---|---|
| `query` | `string` | yes | Free-text query over skill names and summaries. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `({ id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; source?: string; summary?: string; })[]` | no | Matching skills. |
| `unavailable` | `string[]` | no | Sources that could not be reached, as '<source>: <reason>'. Empty skills with a non-empty list means offline, not no match. |

### `system.health`

*Direction:* client → server

Liveness probe; answers as long as the daemon serves requests.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `status` | `"ok"` | no | Always 'ok' when the daemon answers. |

### `system.hello`

*Direction:* client → server

Authenticate a connection and agree on the protocol version.

**Params**

| field | type | required | description |
|---|---|---|---|
| `clientVersion` | `string` | yes | Version string of the connecting client. |
| `protocolVersion` | `string` | yes | Protocol semver the client speaks; major must match. |
| `token` | `string` | yes | Shared secret read from $SNOWPEA_HOME/token or daemon.json. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `capabilities` | `string[]` | no | Feature flags this daemon supports. |
| `protocolVersion` | `string` | yes | Protocol semver the daemon speaks. |
| `serverVersion` | `string` | yes | Version of the running daemon. |

### `system.info`

*Direction:* client → server

Report the daemon's version, pid, port, start time and home.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `counters` | `Record<string, number>` | no | Lifecycle counters: sessions, jobs, gateway_bindings, named_agents. |
| `home` | `string` | yes | Resolved SNOWPEA_HOME directory. |
| `lifecycle` | `{ reason?: string; reasons?: string[]; secondsUntilExit?: number \| null; summary?: string \| null; willExit?: boolean; } \| null` | no | Idle-shutdown status, omitted by older daemons. |
| `pid` | `number` | yes | Process id of the daemon. |
| `port` | `number` | yes | TCP port the daemon is listening on (127.0.0.1 only). |
| `protocolVersion` | `string` | yes | Protocol semver the daemon speaks. |
| `startedAt` | `string` | yes | UTC ISO-8601 timestamp of daemon start. |
| `version` | `string` | yes | Daemon version. |

### `system.shutdown`

*Direction:* client → server

Ask the daemon to shut down gracefully.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `team.start`

*Direction:* client → server

Split a task across parallel workers.

**Params**

| field | type | required | description |
|---|---|---|---|
| `n` | `number` | yes | Number of workers to run in parallel. |
| `sessionId` | `string` | yes | Session the team works under. |
| `task` | `string` | yes | Task the team splits between workers. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | yes | Id to poll with team.status. |

### `team.status`

*Direction:* client → server

Inspect a team's task board.

**Params**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | no | Team to inspect; empty means the most recent one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `state` | `"running" \| "done" \| "failed"` | no | Overall state. |
| `task` | `string` | no | The task the team was given. |
| `tasks` | `({ agentN?: number \| null; assignee?: string \| null; branch?: string; conflictHunks?: string; conflictSummary?: string; dependsOn?: string[]; note?: string; retries?: number; status?: "pending" \| "queued" \| "claimed" \| "running" \| "done" \| "conflict" \| "merged" \| "failed"; taskId: string; title: string; })[]` | no | Task board contents. |
| `teamId` | `string` | yes | Team that was inspected. |
| `workers` | `number` | no | How many workers the run was started with. |
| `worktrees` | `string[]` | no | Worker worktrees that exist right now. |

### `tool.list`

*Direction:* client → server

List the tools registered for a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `tools` | `({ category: string; description?: string; name: string; permissionTag: "read" \| "write" \| "exec" \| "network" \| "send"; source?: string; state?: "active" \| "inactive"; })[]` | no | Registered tools. |

## Notifications

### `approval.pending`

| field | type | required | description |
|---|---|---|---|
| `request` | `{ args?: Record<string, unknown>; requestId: string; risk?: string; scopeHint?: "once" \| "session" \| "project" \| "always"; sessionId: string; timeoutSec?: number; tool: string; }` | yes | The request now in the shared queue. |

### `approval.resolved`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes | Surface or user that answered. |
| `decision` | `"allow" \| "deny"` | yes | The decision that was recorded. |
| `requestId` | `string` | yes | Request that was resolved. |

### `commands.changed`

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema?: Record<string, unknown>; name: string; source?: string; summary: string; })[]` | no | The command table as it stands now. |
| `reason` | `string` | no | Why the table changed. |

### `gateway.event`

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Binding the event belongs to. |
| `kind` | `string` | yes | Event kind, e.g. 'message' or 'error'. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |

### `job.event`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Job the event belongs to. |
| `kind` | `"started" \| "finished" \| "failed" \| "denied"` | yes | Where the run got to. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |

### `session.event`

| field | type | required | description |
|---|---|---|---|
| `kind` | `string` | yes | Event kind; see sessionEventKinds for the payload schema. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |
| `seq` | `number` | yes | Monotonic per-session sequence number. |
| `sessionId` | `string` | yes | Session the event belongs to. |
| `ts` | `string` | yes | UTC ISO-8601 timestamp. |

## `session.event` kinds

Every session event carries a monotonically increasing per-session `seq`. After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed.

### kind `backend.changed`

| field | type | required | description |
|---|---|---|---|
| `backend` | `"local" \| "docker" \| "ssh"` | yes | Where tools now execute. |
| `kind` | `"backend.changed"` | no |  |

### kind `diff`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"diff"` | no |  |
| `patch` | `string` | yes | Unified diff of the change. |
| `path` | `string` | yes | File that changed. |

### kind `error`

| field | type | required | description |
|---|---|---|---|
| `code` | `string` | yes | One of the protocol error codes. |
| `kind` | `"error"` | no |  |
| `message` | `string` | yes | Human-readable detail. |

### kind `message.delta`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.delta"` | no |  |
| `text` | `string` | yes | Text fragment to append to the current message. |

### kind `message.done`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.done"` | no |  |
| `role` | `"assistant" \| "user" \| "system"` | no | Who produced the message. |
| `text` | `string` | yes | Full message text. |

### kind `mode.changed`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"mode.changed"` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | yes | Mode now in effect. |

### kind `subagent.done`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Subagent that finished. |
| `kind` | `"subagent.done"` | no |  |
| `name` | `string` | no | Named agent that ran, when there was one. |
| `ok` | `boolean` | no | False when it failed. |
| `result` | `string` | no | Final report. |
| `sessionId` | `string \| null` | no | The subagent's own session. |
| `status` | `"queued" \| "running" \| "done" \| "error"` | no | Terminal state: done or error. |
| `summary` | `string` | no | The subagent's final answer. |
| `usage` | `{ inputTokens?: number; outputTokens?: number; }` | no | Tokens the subagent consumed. |

### kind `subagent.spawn`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Id correlating this subagent's events. |
| `kind` | `"subagent.spawn"` | no |  |
| `name` | `string` | no | Named agent that was spawned. |
| `sessionId` | `string \| null` | no | The subagent's own session, once it has one. |
| `status` | `"queued" \| "running" \| "done" \| "error"` | no | State at spawn: queued until a concurrency slot frees up. |
| `task` | `string` | no | Task it was given. |

### kind `subagent.update`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Subagent reporting progress. |
| `kind` | `"subagent.update"` | no |  |
| `lastText` | `string` | no | Most recent text the subagent produced. |
| `name` | `string` | no | Named agent that is running, when there is one. |
| `sessionId` | `string \| null` | no | The subagent's own session. |
| `status` | `"queued" \| "running" \| "done" \| "error"` | no | Lifecycle state. |
| `text` | `string` | no | Human-readable progress text. |

### kind `team.task.update`

| field | type | required | description |
|---|---|---|---|
| `agentN` | `number \| null` | no | 1-based worker index that owns the task. |
| `assignee` | `string \| null` | no | Worker that owns the task. |
| `kind` | `"team.task.update"` | no |  |
| `retries` | `number` | no | How many times a merge conflict re-queued it. |
| `status` | `"pending" \| "queued" \| "claimed" \| "running" \| "done" \| "conflict" \| "merged" \| "failed"` | no | New state. |
| `taskId` | `string` | yes | Task that changed. |
| `teamId` | `string` | yes | Team the task belongs to. |

### kind `tool.call`

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments supplied. |
| `callId` | `string` | yes | Id pairing this call with its tool.result. |
| `kind` | `"tool.call"` | no |  |
| `name` | `string` | yes | Tool being called. |

### kind `tool.result`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | Id of the matching tool.call. |
| `error` | `string \| null` | no | Failure detail when ok is false. |
| `kind` | `"tool.result"` | no |  |
| `name` | `string` | yes | Tool that ran. |
| `ok` | `boolean` | yes | False when the tool failed. |
| `output` | `string` | no | Output handed back to the model. |

### kind `turn.done`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.done"` | no |  |
| `reason` | `"complete" \| "interrupted" \| "error" \| "denied" \| "timeout"` | no | Why the turn ended. |
| `turnId` | `string` | yes | Turn that ended. |

### kind `usage`

| field | type | required | description |
|---|---|---|---|
| `inputTokens` | `number` | no | Prompt tokens consumed. |
| `kind` | `"usage"` | no |  |
| `outputTokens` | `number` | no | Completion tokens produced. |

## Error codes

Returned as the string `error.data.code` of a JSON-RPC error response.

| code | meaning |
|---|---|
| `approval_denied` | The user denied the approval request. |
| `approval_timeout` | No approval arrived before `approvals.timeoutSec` elapsed. |
| `internal` | Unexpected server-side failure. |
| `invalid_params` | Params failed schema validation. |
| `login_unsupported` | The vendor does not support the requested login method. |
| `mode_denied` | The session mode forbids this tool or action. |
| `not_found` | No such session, request, job, or agent. |
| `not_implemented` | Defined in the schema but not implemented in this milestone. |
| `protocol_incompatible` | Client and server protocol majors differ. |
| `tool_inactive` | The tool exists but is disabled for this session. |
| `unauthorized` | Missing or invalid token, or a call before `system.hello`. |
