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
| [`agent.bindChannel`](#agentbindchannel) | client → server | Bind an agent to a messenger channel. (not_implemented at M1) |
| [`agent.create`](#agentcreate) | client → server | Create a persistent agent. (not_implemented at M1) |
| [`agent.delete`](#agentdelete) | client → server | Delete a persistent agent. (not_implemented at M1) |
| [`agent.list`](#agentlist) | client → server | List persistent agents. (not_implemented at M1) |
| [`agent.spawn`](#agentspawn) | client → server | Spawn a one-shot subagent. (not_implemented at M1) |
| [`approval.list`](#approvallist) | client → server | List pending approval requests. |
| [`approval.request`](#approvalrequest) | server → client | Server asks the originating surface to approve a tool call. |
| [`approval.respond`](#approvalrespond) | client → server | Answer a pending approval request. |
| [`backend.set`](#backendset) | client → server | Choose the execution backend for a session. |
| [`command.list`](#commandlist) | client → server | List slash commands. |
| [`command.run`](#commandrun) | client → server | Run a slash command; the only execution path for them. |
| [`gateway.bind`](#gatewaybind) | client → server | Bind a messenger target. (not_implemented at M1) |
| [`gateway.list`](#gatewaylist) | client → server | List gateway bindings. (not_implemented at M1) |
| [`gateway.unbind`](#gatewayunbind) | client → server | Remove a gateway binding. (not_implemented at M1) |
| [`job.cancel`](#jobcancel) | client → server | Cancel a job. (not_implemented at M1) |
| [`job.list`](#joblist) | client → server | List scheduled jobs. (not_implemented at M1) |
| [`job.runNow`](#jobrunnow) | client → server | Run a job immediately. (not_implemented at M1) |
| [`job.schedule`](#jobschedule) | client → server | Schedule a job. (not_implemented at M1) |
| [`memory.search`](#memorysearch) | client → server | Search long-term memory. (not_implemented at M1) |
| [`memory.write`](#memorywrite) | client → server | Write a memory. (not_implemented at M1) |
| [`permission.allowlist.add`](#permissionallowlistadd) | client → server | Add an allowlist pattern. |
| [`permission.allowlist.list`](#permissionallowlistlist) | client → server | List allowlist patterns. |
| [`permission.allowlist.remove`](#permissionallowlistremove) | client → server | Remove an allowlist pattern. |
| [`provider.configure`](#providerconfigure) | client → server | Store provider credentials or settings. |
| [`provider.list`](#providerlist) | client → server | List configured providers. |
| [`provider.loginWeb`](#providerloginweb) | client → server | Start a browser login flow for a vendor. |
| [`session.close`](#sessionclose) | client → server | Close a session. |
| [`session.create`](#sessioncreate) | client → server | Open a session rooted at a working directory. |
| [`session.interrupt`](#sessioninterrupt) | client → server | Interrupt the running turn. |
| [`session.list`](#sessionlist) | client → server | List open sessions. |
| [`session.prompt`](#sessionprompt) | client → server | Start a turn from user text. |
| [`session.resume`](#sessionresume) | client → server | Replay session events after a sequence number. |
| [`session.setMode`](#sessionsetmode) | client → server | Switch the permission mode. |
| [`skill.install`](#skillinstall) | client → server | Install a skill. (not_implemented at M1) |
| [`skill.list`](#skilllist) | client → server | List installed skills. (not_implemented at M1) |
| [`skill.reload`](#skillreload) | client → server | Reload skills from disk. (not_implemented at M1) |
| [`skill.search`](#skillsearch) | client → server | Search the skill registry. (not_implemented at M1) |
| [`system.health`](#systemhealth) | client → server | Liveness probe. |
| [`system.hello`](#systemhello) | client → server | Authenticate and negotiate the protocol version. |
| [`system.info`](#systeminfo) | client → server | Daemon identity and runtime facts. |
| [`system.shutdown`](#systemshutdown) | client → server | Stop the daemon. |
| [`team.start`](#teamstart) | client → server | Start a team run. (not_implemented at M1) |
| [`team.status`](#teamstatus) | client → server | Team run status. (not_implemented at M1) |
| [`tool.list`](#toollist) | client → server | List tools visible to a session. |

## Methods

### `agent.bindChannel`

*Direction:* client → server

Bind an agent to a messenger channel. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string` | yes |  |
| `name` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `agent.create`

*Direction:* client → server

Create a persistent agent. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `description` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes |  |

### `agent.delete`

*Direction:* client → server

Delete a persistent agent. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `agent.list`

*Direction:* client → server

List persistent agents. (not_implemented at M1)

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `agents` | `(Record<string, unknown>)[]` | yes |  |

### `agent.spawn`

*Direction:* client → server

Spawn a one-shot subagent. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes |  |
| `task` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |

### `approval.list`

*Direction:* client → server

List pending approval requests.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ args: Record<string, unknown>; requestId: string; risk: string; scopeHint?: string; sessionId: string; timeoutSec: number; tool: string; })[]` | yes |  |

### `approval.request`

*Direction:* server → client

Server asks the originating surface to approve a tool call.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | yes |  |
| `requestId` | `string` | yes |  |
| `risk` | `string` | yes |  |
| `scopeHint` | `string` | no |  |
| `sessionId` | `string` | yes |  |
| `timeoutSec` | `number` | yes |  |
| `tool` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes |  |
| `scope` | `"once" \| "session" \| "project" \| "always"` | yes |  |

### `approval.respond`

*Direction:* client → server

Answer a pending approval request.

**Params**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes |  |
| `requestId` | `string` | yes |  |
| `scope` | `"once" \| "session" \| "project" \| "always"` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `backend.set`

*Direction:* client → server

Choose the execution backend for a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | yes |  |
| `kind` | `"local" \| "docker" \| "ssh"` | yes |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `command.list`

*Direction:* client → server

List slash commands.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema: Record<string, unknown>; name: string; source: "builtin" \| "skill" \| "plugin"; summary: string; })[]` | yes |  |

### `command.run`

*Direction:* client → server

Run a slash command; the only execution path for them.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `string` | yes |  |
| `name` | `string` | yes |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes |  |

### `gateway.bind`

*Direction:* client → server

Bind a messenger target. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `credentialsRef` | `string` | yes |  |
| `platform` | `string` | yes |  |
| `target` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes |  |

### `gateway.list`

*Direction:* client → server

List gateway bindings. (not_implemented at M1)

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindings` | `(Record<string, unknown>)[]` | yes |  |

### `gateway.unbind`

*Direction:* client → server

Remove a gateway binding. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `job.cancel`

*Direction:* client → server

Cancel a job. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `job.list`

*Direction:* client → server

List scheduled jobs. (not_implemented at M1)

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobs` | `(Record<string, unknown>)[]` | yes |  |

### `job.runNow`

*Direction:* client → server

Run a job immediately. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `job.schedule`

*Direction:* client → server

Schedule a job. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | no |  |
| `spec` | `string` | yes |  |
| `task` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

### `memory.search`

*Direction:* client → server

Search long-term memory. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no |  |
| `query` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `results` | `(Record<string, unknown>)[]` | yes |  |

### `memory.write`

*Direction:* client → server

Write a memory. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `tags` | `string[]` | no |  |
| `text` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `permission.allowlist.add`

*Direction:* client → server

Add an allowlist pattern.

**Params**

| field | type | required | description |
|---|---|---|---|
| `pattern` | `string` | yes |  |
| `scope` | `"once" \| "session" \| "project" \| "always"` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes |  |

### `permission.allowlist.list`

*Direction:* client → server

List allowlist patterns.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"once" \| "session" \| "project" \| "always"` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patterns` | `({ pattern: string; patternId: string; scope: "once" \| "session" \| "project" \| "always"; })[]` | yes |  |

### `permission.allowlist.remove`

*Direction:* client → server

Remove an allowlist pattern.

**Params**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `provider.configure`

*Direction:* client → server

Store provider credentials or settings.

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | yes |  |
| `vendor` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `provider.list`

*Direction:* client → server

List configured providers.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `providers` | `({ configured: boolean; models: string[]; vendor: string; })[]` | yes |  |

### `provider.loginWeb`

*Direction:* client → server

Start a browser login flow for a vendor.

**Params**

| field | type | required | description |
|---|---|---|---|
| `method` | `string` | yes |  |
| `vendor` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `session.close`

*Direction:* client → server

Close a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `session.create`

*Direction:* client → server

Open a session rooted at a working directory.

**Params**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string` | no |  |
| `maxConcurrent` | `number` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | no |  |
| `model` | `string` | no |  |
| `originSurface` | `string` | no |  |
| `provider` | `string` | no |  |
| `workdir` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

### `session.interrupt`

*Direction:* client → server

Interrupt the running turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `session.list`

*Direction:* client → server

List open sessions.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessions` | `({ createdAt: string; mode: "plan" \| "accept" \| "auto"; model?: string; originSurface?: string; provider?: string; seq: number; sessionId: string; workdir: string; })[]` | yes |  |

### `session.prompt`

*Direction:* client → server

Start a turn from user text.

**Params**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ kind: string; mimeType?: string; path?: string; text?: string; })[]` | no |  |
| `sessionId` | `string` | yes |  |
| `text` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes |  |

### `session.resume`

*Direction:* client → server

Replay session events after a sequence number.

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number` | no |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload: Record<string, unknown>; seq: number; sessionId: string; ts: string; })[]` | yes |  |
| `sessionId` | `string` | yes |  |

### `session.setMode`

*Direction:* client → server

Switch the permission mode.

**Params**

| field | type | required | description |
|---|---|---|---|
| `mode` | `"plan" \| "accept" \| "auto"` | yes |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mode` | `"plan" \| "accept" \| "auto"` | yes |  |

### `skill.install`

*Direction:* client → server

Install a skill. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `source` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `skill.list`

*Direction:* client → server

List installed skills. (not_implemented at M1)

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `(Record<string, unknown>)[]` | yes |  |

### `skill.reload`

*Direction:* client → server

Reload skills from disk. (not_implemented at M1)

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `skill.search`

*Direction:* client → server

Search the skill registry. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `query` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `(Record<string, unknown>)[]` | yes |  |

### `system.health`

*Direction:* client → server

Liveness probe.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `status` | `"ok"` | yes |  |

### `system.hello`

*Direction:* client → server

Authenticate and negotiate the protocol version.

**Params**

| field | type | required | description |
|---|---|---|---|
| `clientVersion` | `string` | yes |  |
| `protocolVersion` | `string` | yes |  |
| `token` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `capabilities` | `string[]` | yes |  |
| `protocolVersion` | `string` | yes |  |
| `serverVersion` | `string` | yes |  |

### `system.info`

*Direction:* client → server

Daemon identity and runtime facts.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `home` | `string` | yes |  |
| `pid` | `number` | yes |  |
| `port` | `number` | yes |  |
| `protocolVersion` | `string` | yes |  |
| `startedAt` | `string` | yes |  |
| `version` | `string` | yes |  |

### `system.shutdown`

*Direction:* client → server

Stop the daemon.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | yes |  |

### `team.start`

*Direction:* client → server

Start a team run. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `n` | `number` | yes |  |
| `sessionId` | `string` | yes |  |
| `task` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | yes |  |

### `team.status`

*Direction:* client → server

Team run status. (not_implemented at M1)

**Params**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | yes |  |

**Result**

_No result fields._

### `tool.list`

*Direction:* client → server

List tools visible to a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `tools` | `({ category: string; description: string; name: string; permissionTag: "read" \| "write" \| "exec" \| "network" \| "send"; source: string; state: "active" \| "inactive"; })[]` | yes |  |

## Notifications

### `approval.resolved`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes |  |
| `decision` | `"allow" \| "deny"` | yes |  |
| `requestId` | `string` | yes |  |

### `gateway.event`

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes |  |
| `kind` | `string` | yes |  |
| `payload` | `Record<string, unknown>` | yes |  |

### `job.event`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |
| `kind` | `string` | yes |  |
| `payload` | `Record<string, unknown>` | yes |  |

### `session.event`

| field | type | required | description |
|---|---|---|---|
| `kind` | `string` | yes |  |
| `payload` | `Record<string, unknown>` | yes |  |
| `seq` | `number` | yes |  |
| `sessionId` | `string` | yes |  |
| `ts` | `string` | yes |  |

## `session.event` kinds

Every session event carries a monotonically increasing per-session `seq`. After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed.

### kind `diff`

| field | type | required | description |
|---|---|---|---|
| `patch` | `string` | yes |  |
| `path` | `string` | yes |  |

### kind `error`

| field | type | required | description |
|---|---|---|---|
| `code` | `string` | yes |  |
| `message` | `string` | yes |  |

### kind `message.delta`

| field | type | required | description |
|---|---|---|---|
| `text` | `string` | yes |  |

### kind `message.done`

| field | type | required | description |
|---|---|---|---|
| `role` | `string` | yes |  |
| `text` | `string` | yes |  |

### kind `mode.changed`

| field | type | required | description |
|---|---|---|---|
| `mode` | `"plan" \| "accept" \| "auto"` | yes |  |

### kind `subagent.done`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `ok` | `boolean` | no |  |
| `summary` | `string` | no |  |

### kind `subagent.spawn`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `task` | `string` | no |  |

### kind `subagent.update`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `status` | `string` | no |  |

### kind `team.task.update`

| field | type | required | description |
|---|---|---|---|
| `status` | `string` | yes |  |
| `taskId` | `string` | yes |  |
| `teamId` | `string` | yes |  |

### kind `tool.call`

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | yes |  |
| `callId` | `string` | yes |  |
| `name` | `string` | yes |  |

### kind `tool.result`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes |  |
| `error` | `string` | no |  |
| `name` | `string` | yes |  |
| `ok` | `boolean` | yes |  |
| `output` | `string` | yes |  |

### kind `turn.done`

| field | type | required | description |
|---|---|---|---|
| `reason` | `"complete" \| "interrupted" \| "error" \| "denied" \| "timeout"` | yes |  |
| `turnId` | `string` | yes |  |

### kind `usage`

| field | type | required | description |
|---|---|---|---|
| `inputTokens` | `number` | yes |  |
| `outputTokens` | `number` | yes |  |

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
