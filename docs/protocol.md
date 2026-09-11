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
| [`agent.bindChannel`](#agentbindchannel) | client → server |  |
| [`agent.create`](#agentcreate) | client → server |  |
| [`agent.delete`](#agentdelete) | client → server |  |
| [`agent.list`](#agentlist) | client → server |  |
| [`agent.spawn`](#agentspawn) | client → server |  |
| [`approval.list`](#approvallist) | client → server |  |
| [`approval.request`](#approvalrequest) | server → client |  |
| [`approval.respond`](#approvalrespond) | client → server |  |
| [`backend.set`](#backendset) | client → server |  |
| [`command.list`](#commandlist) | client → server |  |
| [`command.run`](#commandrun) | client → server |  |
| [`gateway.bind`](#gatewaybind) | client → server |  |
| [`gateway.list`](#gatewaylist) | client → server |  |
| [`gateway.unbind`](#gatewayunbind) | client → server |  |
| [`job.cancel`](#jobcancel) | client → server |  |
| [`job.list`](#joblist) | client → server |  |
| [`job.runNow`](#jobrunnow) | client → server |  |
| [`job.schedule`](#jobschedule) | client → server |  |
| [`memory.search`](#memorysearch) | client → server |  |
| [`memory.write`](#memorywrite) | client → server |  |
| [`permission.allowlist.add`](#permissionallowlistadd) | client → server |  |
| [`permission.allowlist.list`](#permissionallowlistlist) | client → server |  |
| [`permission.allowlist.remove`](#permissionallowlistremove) | client → server |  |
| [`provider.configure`](#providerconfigure) | client → server |  |
| [`provider.list`](#providerlist) | client → server |  |
| [`provider.loginWeb`](#providerloginweb) | client → server |  |
| [`session.close`](#sessionclose) | client → server |  |
| [`session.create`](#sessioncreate) | client → server |  |
| [`session.interrupt`](#sessioninterrupt) | client → server |  |
| [`session.list`](#sessionlist) | client → server |  |
| [`session.prompt`](#sessionprompt) | client → server |  |
| [`session.resume`](#sessionresume) | client → server |  |
| [`session.setMode`](#sessionsetmode) | client → server |  |
| [`skill.install`](#skillinstall) | client → server |  |
| [`skill.list`](#skilllist) | client → server |  |
| [`skill.reload`](#skillreload) | client → server |  |
| [`skill.search`](#skillsearch) | client → server |  |
| [`system.health`](#systemhealth) | client → server |  |
| [`system.hello`](#systemhello) | client → server |  |
| [`system.info`](#systeminfo) | client → server |  |
| [`system.shutdown`](#systemshutdown) | client → server |  |
| [`team.start`](#teamstart) | client → server |  |
| [`team.status`](#teamstatus) | client → server |  |
| [`tool.list`](#toollist) | client → server |  |

## Methods

### `agent.bindChannel`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string` | yes |  |
| `name` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `agent.create`

*Direction:* client → server

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

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `agent.list`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `agents` | `({ channel?: string \| null; description?: string; name: string; source?: string; })[]` | no |  |

### `agent.spawn`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes |  |
| `sessionId` | `string \| null` | no |  |
| `task` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |

### `approval.list`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ args?: Record<string, unknown>; requestId: string; risk?: string; scopeHint?: "once" \| "session" \| "project" \| "always"; sessionId: string; timeoutSec?: number; tool: string; })[]` | no |  |

### `approval.request`

*Direction:* server → client

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no |  |
| `requestId` | `string` | yes |  |
| `risk` | `string` | no |  |
| `scopeHint` | `"once" \| "session" \| "project" \| "always"` | no |  |
| `sessionId` | `string` | yes |  |
| `timeoutSec` | `number` | no |  |
| `tool` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes |  |
| `scope` | `"once" \| "session" \| "project" \| "always"` | no |  |

### `approval.respond`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `decision` | `"allow" \| "deny"` | yes |  |
| `requestId` | `string` | yes |  |
| `scope` | `"once" \| "session" \| "project" \| "always"` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `backend.set`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | no |  |
| `kind` | `"local" \| "docker" \| "ssh"` | yes |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `command.list`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema?: Record<string, unknown>; name: string; source?: "builtin" \| "skill" \| "plugin"; summary: string; })[]` | no |  |

### `command.run`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `string` | no |  |
| `name` | `string` | yes |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes |  |

### `gateway.bind`

*Direction:* client → server

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

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindings` | `({ bindingId: string; platform: string; state?: "active" \| "inactive"; target: string; })[]` | no |  |

### `gateway.unbind`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `job.cancel`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `job.list`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobs` | `({ channel?: string \| null; jobId: string; mode?: "plan" \| "accept" \| "auto"; nextRunAt?: string \| null; spec: string; state?: "scheduled" \| "running" \| "cancelled"; task: string; })[]` | no |  |

### `job.runNow`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `job.schedule`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string \| null` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | no |  |
| `spec` | `string` | yes |  |
| `task` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |

### `memory.search`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no |  |
| `query` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `hits` | `({ id: string; score?: number; tags?: string[]; text: string; })[]` | no |  |

### `memory.write`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `tags` | `string[]` | no |  |
| `text` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes |  |

### `permission.allowlist.add`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `pattern` | `string` | yes |  |
| `scope` | `"session" \| "project" \| "always"` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes |  |

### `permission.allowlist.list`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"session" \| "project" \| "always" \| null` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patterns` | `({ pattern: string; patternId: string; scope: "session" \| "project" \| "always"; })[]` | no |  |

### `permission.allowlist.remove`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `provider.configure`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | no |  |
| `vendor` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `provider.list`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `providers` | `({ configured?: boolean; default?: boolean; models?: string[]; vendor: string; })[]` | no |  |

### `provider.loginWeb`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `method` | `string` | yes |  |
| `vendor` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `session.close`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `session.create`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no |  |
| `maxConcurrent` | `number \| null` | no |  |
| `mode` | `"plan" \| "accept" \| "auto" \| null` | no |  |
| `model` | `string \| null` | no |  |
| `originSurface` | `string \| null` | no |  |
| `provider` | `string \| null` | no |  |
| `workdir` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

### `session.interrupt`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `session.list`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessions` | `({ createdAt: string; mode: "plan" \| "accept" \| "auto"; model?: string \| null; originSurface?: string \| null; provider?: string \| null; seq?: number; sessionId: string; workdir: string; })[]` | no |  |

### `session.prompt`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ kind?: "file" \| "image" \| "text"; mimeType?: string \| null; path?: string \| null; text?: string \| null; })[] \| null` | no |  |
| `sessionId` | `string` | yes |  |
| `text` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes |  |

### `session.resume`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number \| null` | no |  |
| `sessionId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; sessionId: string; ts: string; })[]` | no |  |
| `sessionId` | `string` | yes |  |

### `session.setMode`

*Direction:* client → server

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

**Params**

| field | type | required | description |
|---|---|---|---|
| `source` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `skill.list`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `({ installed?: boolean; name: string; source?: string; summary?: string; })[]` | no |  |

### `skill.reload`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `skill.search`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `query` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `({ installed?: boolean; name: string; source?: string; summary?: string; })[]` | no |  |

### `system.health`

*Direction:* client → server

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `status` | `"ok"` | no |  |

### `system.hello`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `clientVersion` | `string` | yes |  |
| `protocolVersion` | `string` | yes |  |
| `token` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `capabilities` | `string[]` | no |  |
| `protocolVersion` | `string` | yes |  |
| `serverVersion` | `string` | yes |  |

### `system.info`

*Direction:* client → server

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

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no |  |

### `team.start`

*Direction:* client → server

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

**Params**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | yes |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `state` | `"running" \| "done" \| "failed"` | no |  |
| `tasks` | `({ assignee?: string \| null; status?: "pending" \| "running" \| "done" \| "failed"; taskId: string; title: string; })[]` | no |  |
| `teamId` | `string` | yes |  |

### `tool.list`

*Direction:* client → server

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `tools` | `({ category: string; description?: string; name: string; permissionTag: "read" \| "write" \| "exec" \| "network" \| "send"; source?: string; state?: "active" \| "inactive"; })[]` | no |  |

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
| `payload` | `Record<string, unknown>` | no |  |

### `job.event`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes |  |
| `kind` | `string` | yes |  |
| `payload` | `Record<string, unknown>` | no |  |

### `session.event`

| field | type | required | description |
|---|---|---|---|
| `kind` | `string` | yes |  |
| `payload` | `Record<string, unknown>` | no |  |
| `seq` | `number` | yes |  |
| `sessionId` | `string` | yes |  |
| `ts` | `string` | yes |  |

## `session.event` kinds

Every session event carries a monotonically increasing per-session `seq`. After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed.

### kind `diff`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"diff"` | no |  |
| `patch` | `string` | yes |  |
| `path` | `string` | yes |  |

### kind `error`

| field | type | required | description |
|---|---|---|---|
| `code` | `string` | yes |  |
| `kind` | `"error"` | no |  |
| `message` | `string` | yes |  |

### kind `message.delta`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.delta"` | no |  |
| `text` | `string` | yes |  |

### kind `message.done`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.done"` | no |  |
| `role` | `"assistant" \| "user" \| "system"` | no |  |
| `text` | `string` | yes |  |

### kind `mode.changed`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"mode.changed"` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | yes |  |

### kind `subagent.done`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `kind` | `"subagent.done"` | no |  |
| `ok` | `boolean` | no |  |
| `result` | `string` | no |  |

### kind `subagent.spawn`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `kind` | `"subagent.spawn"` | no |  |
| `name` | `string` | no |  |
| `task` | `string` | no |  |

### kind `subagent.update`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes |  |
| `kind` | `"subagent.update"` | no |  |
| `status` | `string` | no |  |
| `text` | `string` | no |  |

### kind `team.task.update`

| field | type | required | description |
|---|---|---|---|
| `assignee` | `string \| null` | no |  |
| `kind` | `"team.task.update"` | no |  |
| `status` | `"pending" \| "running" \| "done" \| "failed"` | no |  |
| `taskId` | `string` | yes |  |
| `teamId` | `string` | yes |  |

### kind `tool.call`

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no |  |
| `callId` | `string` | yes |  |
| `kind` | `"tool.call"` | no |  |
| `name` | `string` | yes |  |

### kind `tool.result`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes |  |
| `error` | `string \| null` | no |  |
| `kind` | `"tool.result"` | no |  |
| `name` | `string` | yes |  |
| `ok` | `boolean` | yes |  |
| `output` | `string` | no |  |

### kind `turn.done`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.done"` | no |  |
| `reason` | `"complete" \| "interrupted" \| "error" \| "denied" \| "timeout"` | no |  |
| `turnId` | `string` | yes |  |

### kind `usage`

| field | type | required | description |
|---|---|---|---|
| `inputTokens` | `number` | no |  |
| `kind` | `"usage"` | no |  |
| `outputTokens` | `number` | no |  |

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
