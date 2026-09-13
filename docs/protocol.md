<!-- GENERATED — do not edit. Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py. -->

# Snowpea protocol

- **Protocol version:** `1.5.0` (semver)
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
    "protocolVersion": "1.5.0"
  }
}
```

Server capabilities advertised in the `system.hello` result:

- `approvals`
- `audio`
- `commands`
- `lsp`
- `sessions`
- `settings`
- `setup`
- `tools`
- `update`

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
| [`audio.capabilities`](#audiocapabilities) | client → server | Report what voice input and output can do on this machine. |
| [`audio.record.start`](#audiorecordstart) | client → server | Start recording the microphone. |
| [`audio.record.stop`](#audiorecordstop) | client → server | Stop the recording and return the wav it wrote. |
| [`audio.speak`](#audiospeak) | client → server | Synthesise speech, optionally playing it on the daemon's machine. |
| [`audio.transcribe`](#audiotranscribe) | client → server | Transcribe recorded audio to text. |
| [`backend.set`](#backendset) | client → server | Choose where a session's tools execute: local, docker or ssh. |
| [`command.list`](#commandlist) | client → server | List the slash commands available to a session. |
| [`command.run`](#commandrun) | client → server | Run a slash command; the only execution path for them. |
| [`gateway.bind`](#gatewaybind) | client → server | Attach the daemon to a chat platform channel. |
| [`gateway.list`](#gatewaylist) | client → server | List live gateway bindings. |
| [`gateway.sync`](#gatewaysync) | client → server | Reconcile the messenger bindings with settings.gateway. |
| [`gateway.unbind`](#gatewayunbind) | client → server | Detach a gateway binding. |
| [`job.cancel`](#jobcancel) | client → server | Cancel a scheduled job. |
| [`job.list`](#joblist) | client → server | List scheduled jobs and their next run times. |
| [`job.runNow`](#jobrunnow) | client → server | Fire a scheduled job immediately. |
| [`job.schedule`](#jobschedule) | client → server | Schedule a prompt to run unattended. |
| [`lsp.catalog`](#lspcatalog) | client → server | List every registered language server, regardless of whether it has started. |
| [`lsp.status`](#lspstatus) | client → server | Report every language server the daemon has started and its state. |
| [`memory.search`](#memorysearch) | client → server | Recall stored memories matching a query. |
| [`memory.write`](#memorywrite) | client → server | Store a memory with tags. |
| [`permission.allowlist.add`](#permissionallowlistadd) | client → server | Promote a pattern from ask to allow. |
| [`permission.allowlist.list`](#permissionallowlistlist) | client → server | List stored allowlist patterns. |
| [`permission.allowlist.remove`](#permissionallowlistremove) | client → server | Delete an allowlist pattern by id. |
| [`provider.configure`](#providerconfigure) | client → server | Store settings and credentials for a provider. |
| [`provider.list`](#providerlist) | client → server | List chat providers and whether they are configured. |
| [`provider.loginWeb`](#providerloginweb) | client → server | Start a browser-based login flow for a provider. |
| [`provider.models`](#providermodels) | client → server | Ask a vendor's endpoint which models it serves. |
| [`question.list`](#questionlist) | client → server | List questions the agent is still waiting on. |
| [`question.request`](#questionrequest) | server → client | Ask the client to put a question to the human. |
| [`question.respond`](#questionrespond) | client → server | Answer a pending question and unblock the turn. |
| [`session.close`](#sessionclose) | client → server | Close a session and release its resources. |
| [`session.compact`](#sessioncompact) | client → server | Summarise the conversation so far and replace the history with it. |
| [`session.create`](#sessioncreate) | client → server | Open a session rooted at a working directory. |
| [`session.deleteSaved`](#sessiondeletesaved) | client → server | Delete saved sessions. |
| [`session.interrupt`](#sessioninterrupt) | client → server | Stop the running turn as soon as possible. |
| [`session.list`](#sessionlist) | client → server | List live or saved sessions. |
| [`session.prompt`](#sessionprompt) | client → server | Send user text to a session and start a turn. |
| [`session.resume`](#sessionresume) | client → server | Replay the events a disconnected client missed. |
| [`session.setMode`](#sessionsetmode) | client → server | Switch a session between plan, accept and auto. |
| [`session.setModel`](#sessionsetmodel) | client → server | Pin a session to a model profile, or clear the pin. |
| [`settings.get`](#settingsget) | client → server | Read global or project settings, with secrets masked. |
| [`settings.set`](#settingsset) | client → server | Deep-merge a patch into global or project settings and persist it. |
| [`setup.catalog`](#setupcatalog) | client → server | The setup wizard's vendor, search, browser, tools and gateway catalogs. |
| [`skill.install`](#skillinstall) | client → server | Install a skill from a path, URL or registry. |
| [`skill.list`](#skilllist) | client → server | List installed skills. |
| [`skill.reload`](#skillreload) | client → server | Reload skills from disk without restarting. |
| [`skill.remove`](#skillremove) | client → server | Delete an installed skill or plugin. |
| [`skill.search`](#skillsearch) | client → server | Search available skills. |
| [`system.checkUpdate`](#systemcheckupdate) | client → server | Report whether a newer snowpea release exists; cached for 24h. |
| [`system.health`](#systemhealth) | client → server | Liveness probe; answers as long as the daemon serves requests. |
| [`system.hello`](#systemhello) | client → server | Authenticate a connection and agree on the protocol version. |
| [`system.info`](#systeminfo) | client → server | Report the daemon's version, pid, port, start time and home. |
| [`system.reloadSettings`](#systemreloadsettings) | client → server | Re-read settings.json and rebind the daemon's in-memory state. |
| [`system.restart`](#systemrestart) | client → server | Shut the daemon down so the next launch runs the newly installed version. |
| [`system.shutdown`](#systemshutdown) | client → server | Ask the daemon to shut down gracefully. |
| [`system.update`](#systemupdate) | client → server | Upgrade snowpea in a detached subprocess and report progress. |
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
| `model` | `string \| null` | no | Run this one spawn on a specific model: a models.profiles id, a 'vendor:model' pair, or a bare vendor. Outranks the agent's own assignment; null uses the configured routing. |
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
| `requests` | `({ args?: Record<string, unknown>; note?: string; requestId: string; risk?: string; scopeHint?: "once" \| "session" \| "project" \| "always"; sessionId: string; timeoutSec?: number; tool: string; })[]` | no | Approvals still pending. |

### `approval.request`

*Direction:* server → client

Ask the client to approve a tool call.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments it wants to use. |
| `note` | `string` | no | Extra warning shown with the prompt, e.g. "modifies snowpea configuration". |
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

### `audio.capabilities`

*Direction:* client → server

Report what voice input and output can do on this machine.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `autoSpeak` | `boolean` | no | True when replies are spoken without being asked. |
| `play` | `boolean` | no | True when the daemon can play audio itself. |
| `players` | `string[]` | no | Audio players found on PATH. |
| `reasons` | `Record<string, string>` | no | Per-capability explanation of why it is off. |
| `record` | `boolean` | no | True when the microphone can be recorded. |
| `recorders` | `string[]` | no | Recorders found on PATH. |
| `stt` | `string \| null` | no | Transcription backend in use, or null when there is none. |
| `sttProviders` | `string[]` | no | Every usable transcription backend, preferred first. |
| `tts` | `boolean` | no | True when speech synthesis is available. |
| `ttsProvider` | `string \| null` | no | Speech backend in use. |
| `ttsProviders` | `string[]` | no | Every usable speech backend, preferred first. |
| `voice` | `string \| null` | no | Configured voice, when one is set. |

### `audio.record.start`

*Direction:* client → server

Start recording the microphone.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session the recording belongs to. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | no | Media type of the recording. |
| `path` | `string` | yes | Wav file being written, or the finished recording. |
| `provider` | `string \| null` | no | Backend that transcribed it. |
| `recording` | `boolean` | yes | True while capture is still running. |
| `text` | `string \| null` | no | Transcript, when 'stop' was asked to transcribe. |

### `audio.record.stop`

*Direction:* client → server

Stop the recording and return the wav it wrote.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session that is recording. |
| `transcribe` | `boolean` | no | Also transcribe the recording and return its text. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | no | Media type of the recording. |
| `path` | `string` | yes | Wav file being written, or the finished recording. |
| `provider` | `string \| null` | no | Backend that transcribed it. |
| `recording` | `boolean` | yes | True while capture is still running. |
| `text` | `string \| null` | no | Transcript, when 'stop' was asked to transcribe. |

### `audio.speak`

*Direction:* client → server

Synthesise speech, optionally playing it on the daemon's machine.

**Params**

| field | type | required | description |
|---|---|---|---|
| `play` | `boolean` | no | Play on the daemon's machine instead of returning only a path. |
| `sessionId` | `string \| null` | no | Session the audio belongs to. |
| `text` | `string` | yes | What to say. |
| `voice` | `string \| null` | no | Voice id; defaults to the setting. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | yes | Media type of that file. |
| `path` | `string` | yes | Audio file the speech was written to. |
| `played` | `boolean` | no | True when the daemon played it. |
| `provider` | `string` | yes | Backend that synthesised it. |
| `voice` | `string \| null` | no | Voice that was used. |

### `audio.transcribe`

*Direction:* client → server

Transcribe recorded audio to text.

**Params**

| field | type | required | description |
|---|---|---|---|
| `data` | `string \| null` | no | Base64 (or data-URI) audio. |
| `language` | `string \| null` | no | BCP-47 hint for the backend. |
| `mime` | `string \| null` | no | Media type of the audio, e.g. audio/wav. |
| `path` | `string \| null` | no | Audio file on the daemon's machine. |
| `sessionId` | `string \| null` | no | Session the audio belongs to. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `provider` | `string` | yes | Backend that produced the transcript. |
| `text` | `string` | yes | What the backend heard. |

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
| `bindings` | `({ bindingId: string; channelId?: string \| null; credentialsRef?: string; platform: string; source?: string; state?: "active" \| "inactive"; target: string; userId?: string \| null; })[]` | no | Live gateway bindings. |

### `gateway.sync`

*Direction:* client → server

Reconcile the messenger bindings with settings.gateway.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `added` | `string[]` | no | Platforms that started listening. |
| `kept` | `string[]` | no | Platforms that were already listening. |
| `removed` | `string[]` | no | Platforms whose auto binding was dropped. |

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
| `jobs` | `({ channel?: string \| null; enabled?: boolean; jobId: string; kind?: "cron" \| "once" \| "interval"; lastRunAt?: string \| null; lastStatus?: "ok" \| "error" \| "denied_by_timeout" \| null; mode?: "plan" \| "accept" \| "auto"; nextRunAt?: string \| null; originSessionId?: string \| null; spec: string; state?: "scheduled" \| "running" \| "cancelled"; task: string; })[]` | no | Known jobs. |

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

### `lsp.catalog`

*Direction:* client → server

List every registered language server, regardless of whether it has started.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `servers` | `({ disabled: boolean; extensions?: string[]; id: string; installHint?: string \| null; installable: boolean; languageIds?: string[]; })[]` | no | One row per registered server id. |

### `lsp.status`

*Direction:* client → server

Report every language server the daemon has started and its state.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `servers` | `({ id: string; languageId?: string; pid?: number \| null; root: string; state: "starting" \| "ready" \| "broken" \| "stopped"; })[]` | no | One row per (server, root) pair. |

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
| `providers` | `({ authMethods?: string[]; authStatus?: "unconfigured" \| "active" \| "expired"; configured?: boolean; default?: boolean; defaultModel?: string; label?: string; models?: string[]; vendor: string; })[]` | no | Known chat providers. |

### `provider.loginWeb`

*Direction:* client → server

Start a browser-based login flow for a provider.

**Params**

| field | type | required | description |
|---|---|---|---|
| `method` | `string` | yes | Login flow to start: 'browser_pkce' or 'google_oauth' (browser), 'device_code' or 'google_adc' (headless), 'oauth_pkce' (OpenRouter). 'web' or an empty value picks the best flow this machine can complete. |
| `vendor` | `string` | yes | Vendor to log into. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `expiresInSec` | `number \| null` | no | Seconds until the code or session expires, when known. |
| `ok` | `boolean` | no | True when the call succeeded. |
| `status` | `"await_user" \| "done" \| "failed"` | no | 'await_user' once userCode/verificationUri are ready and polling has started in the background; 'done' or 'failed' if the flow finished synchronously before this response was sent. |
| `userCode` | `string \| null` | no | Short code the user types in, for device-code flows. |
| `verificationUri` | `string \| null` | no | URL to open to approve the login. |
| `verificationUriComplete` | `string \| null` | no | verificationUri with the code already embedded, when known. |

### `provider.models`

*Direction:* client → server

Ask a vendor's endpoint which models it serves.

**Params**

| field | type | required | description |
|---|---|---|---|
| `vendor` | `string \| null` | no | Vendor to query; defaults to the configured one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `current` | `string` | no | Model this vendor uses today. |
| `detail` | `string` | no | One line naming the source, for a picker to show. |
| `models` | `string[]` | no | Model ids the vendor's endpoint reports. |
| `source` | `string` | no | Which rung answered: live (the vendor's endpoint), settings (providers.<vendor>.models), cache (the last good listing) or curated (this build's list, merged with models.dev). |
| `vendor` | `string` | yes | Vendor the listing came from. |

### `question.list`

*Direction:* client → server

List questions the agent is still waiting on.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ allowOther?: boolean; header?: string; index?: number; multi?: boolean; options?: ({ description?: string; label: string; preview?: string; })[]; question: string; requestId: string; sessionId: string; timeoutSec?: number; total?: number; })[]` | no | Questions still waiting for an answer. |

### `question.request`

*Direction:* server → client

Ask the client to put a question to the human.

**Params**

| field | type | required | description |
|---|---|---|---|
| `allowOther` | `boolean` | no | Offer a free-text '기타 / Other' row alongside the options. |
| `header` | `string` | no | Short chip above the question, e.g. "Auth method" (<=12 chars). |
| `index` | `number` | no | Position of this question in the batch, from 1. |
| `multi` | `boolean` | no | More than one option may be picked. |
| `options` | `({ description?: string; label: string; preview?: string; })[]` | no | Closed set of answers; empty means free text. |
| `question` | `string` | yes | The question, including why the answer matters. |
| `requestId` | `string` | yes | Id to answer with question.respond. |
| `sessionId` | `string` | yes | Session whose turn is blocked. |
| `timeoutSec` | `number` | no | Seconds before the question gives up. |
| `total` | `number` | no | How many questions the tool call asks in all. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `selected` | `string[]` | no | Labels the human picked. |
| `text` | `string \| null` | no | Free text, when there was any. |

### `question.respond`

*Direction:* client → server

Answer a pending question and unblock the turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `requestId` | `string` | yes | Question being answered. |
| `selected` | `string[]` | no | Labels the human picked, in the order offered. |
| `text` | `string \| null` | no | Free text, for 'Other' or no options. |

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

### `session.compact`

*Direction:* client → server

Summarise the conversation so far and replace the history with it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `instructions` | `string \| null` | no | Extra guidance for the summary, e.g. 'keep the API design decisions'. |
| `sessionId` | `string` | yes | Session whose history to compact. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `after` | `number` | no | Estimated tokens the history holds now. |
| `before` | `number` | no | Estimated tokens the history held before. |
| `summaryChars` | `number` | no | Length of the summary in characters. |

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

### `session.deleteSaved`

*Direction:* client → server

Delete saved sessions.

**Params**

| field | type | required | description |
|---|---|---|---|
| `all` | `boolean` | no | Delete saved sessions from every directory. |
| `sessionId` | `string \| null` | no | Delete one saved session. |
| `workdir` | `string \| null` | no | Delete saved sessions rooted here. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `deleted` | `number` | no |  |

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

List live or saved sessions.

**Params**

| field | type | required | description |
|---|---|---|---|
| `includeClosed` | `boolean` | no | Include persisted closed sessions. |
| `workdir` | `string \| null` | no | Only sessions rooted here. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `sessions` | `({ contextUsed?: number; contextWindow?: number \| null; createdAt: string; lastPrompt?: string \| null; mode: "plan" \| "accept" \| "auto"; model?: string \| null; originSurface?: string \| null; provider?: string \| null; seq?: number; sessionId: string; workdir: string; })[]` | no | Every live session. |

### `session.prompt`

*Direction:* client → server

Send user text to a session and start a turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ data?: string \| null; kind?: "file" \| "image" \| "text"; mimeType?: string \| null; name?: string \| null; path?: string \| null; size?: number \| null; text?: string \| null; })[] \| null` | no | Files or images to include. |
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

### `session.setModel`

*Direction:* client → server

Pin a session to a model profile, or clear the pin.

**Params**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | A models.profiles id, a 'vendor:model' pair, or a bare vendor name. Null or 'inherit' clears the pin and lets the configured routing decide. |
| `sessionId` | `string` | yes | Session to pin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | Model id now in effect. |
| `pinned` | `boolean` | no | False when the pin was cleared. |
| `provider` | `string \| null` | no | Vendor now in effect. |

### `settings.get`

*Direction:* client → server

Read global or project settings, with secrets masked.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"global" \| "project"` | no | "global" reads $SNOWPEA_HOME/settings.json; "project" reads <workdir>/.snowpea/settings.json. |
| `workdir` | `string \| null` | no | Project root; required when scope is "project". |

**Result**

| field | type | required | description |
|---|---|---|---|
| `settings` | `Record<string, unknown>` | yes | The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. |

### `settings.set`

*Direction:* client → server

Deep-merge a patch into global or project settings and persist it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `patch` | `Record<string, unknown>` | yes | Fields to deep-merge into the existing settings. |
| `scope` | `"global" \| "project"` | no | "global" writes $SNOWPEA_HOME/settings.json; "project" writes <workdir>/.snowpea/settings.json. |
| `workdir` | `string \| null` | no | Project root; required when scope is "project". |

**Result**

| field | type | required | description |
|---|---|---|---|
| `settings` | `Record<string, unknown>` | yes | The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. |

### `setup.catalog`

*Direction:* client → server

The setup wizard's vendor, search, browser, tools and gateway catalogs.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `browser` | `({ active?: boolean; default?: boolean; description?: string; id: string; key: string; label: string; tags?: string[]; tier: string; })[]` | no | Browser-control providers. |
| `gateway` | `({ active?: boolean; default?: boolean; description?: string; id: string; key: string; label: string; tags?: string[]; tier: string; })[]` | no | Chat gateways (telegram, discord, slack), all off. |
| `search` | `({ active?: boolean; default?: boolean; description?: string; id: string; key: string; label: string; tags?: string[]; tier: string; })[]` | no | Web-search providers, ddgs first. |
| `tools` | `({ active?: boolean; default?: boolean; description?: string; id: string; key: string; label: string; tags?: string[]; tier: string; })[]` | no | Tool categories and their default on/off state. |
| `vendors` | `({ active?: boolean; default?: boolean; description?: string; id: string; key: string; label: string; tags?: string[]; tier: string; })[]` | no | LLM vendors. |

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
| `skills` | `({ downloads?: number; id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; rating?: number; source?: string; summary?: string; })[]` | no | Installed skills. |

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
| `skills` | `({ downloads?: number; id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; rating?: number; source?: string; summary?: string; })[]` | no | Matching skills. |
| `unavailable` | `string[]` | no | Sources that could not be reached, as '<source>: <reason>'. Empty skills with a non-empty list means offline, not no match. |

### `system.checkUpdate`

*Direction:* client → server

Report whether a newer snowpea release exists; cached for 24h.

**Params**

| field | type | required | description |
|---|---|---|---|
| `force` | `boolean` | no | Ignore the 24h cache and ask the network right now. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `available` | `boolean` | yes | True when latest is strictly newer than current. |
| `cached` | `boolean` | no | True when this came from $SNOWPEA_HOME/update-check.json. |
| `channel` | `"git" \| "pypi"` | yes | Where the answer came from: 'pypi' or 'git'. |
| `checkedAt` | `string` | yes | UTC ISO-8601 timestamp of the answer. |
| `current` | `string` | yes | Version of the running daemon (snowpea_core.__version__). |
| `error` | `string \| null` | no | Why the check could not complete; available is false whenever it is set. |
| `latest` | `string` | yes | Newest version found; equal to current when nothing is known. |
| `releaseUrl` | `string \| null` | no | Human page for the release, when one exists. |
| `source` | `string` | yes | What an installer would be handed to get 'latest'. |

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
| `restartRequired` | `boolean` | no | True once system.update finished; the daemon runs the old code until restarted. |
| `startedAt` | `string` | yes | UTC ISO-8601 timestamp of daemon start. |
| `version` | `string` | yes | Daemon version. |

### `system.reloadSettings`

*Direction:* client → server

Re-read settings.json and rebind the daemon's in-memory state.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `changedKeys` | `string[]` | no | Top-level settings sections that changed, e.g. ['providers']. |
| `reloaded` | `boolean` | yes | True when settings.json differed from what the daemon held and the in-memory state was rebound; false when it was already current. |

### `system.restart`

*Direction:* client → server

Shut the daemon down so the next launch runs the newly installed version.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `system.shutdown`

*Direction:* client → server

Ask the daemon to shut down gracefully.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `system.update`

*Direction:* client → server

Upgrade snowpea in a detached subprocess and report progress.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `command` | `string` | yes | The command line that runs, or that has to be run by hand. |
| `error` | `string \| null` | no | Why nothing was started; null on the happy path. |
| `log` | `string` | yes | Absolute path of the file the upgrade writes its output to. |
| `started` | `boolean` | yes | True when the upgrade subprocess was spawned. |

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
| `tools` | `({ category: string; description?: string; name: string; permissionTag: "read" \| "write" \| "exec" \| "network" \| "send" \| "config"; provider?: string; reason?: string; source?: string; state?: "active" \| "inactive"; })[]` | no | Registered tools. |

## Notifications

### `approval.pending`

| field | type | required | description |
|---|---|---|---|
| `request` | `{ args?: Record<string, unknown>; note?: string; requestId: string; risk?: string; scopeHint?: "once" \| "session" \| "project" \| "always"; sessionId: string; timeoutSec?: number; tool: string; }` | yes | The request now in the shared queue. |

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

### `provider.loginProgress`

| field | type | required | description |
|---|---|---|---|
| `expiresInSec` | `number \| null` | no | Seconds until the code or session expires, when known. |
| `message` | `string \| null` | no | One line for humans. |
| `method` | `string` | yes | Login flow in progress, e.g. 'device_code' or 'oauth_pkce'. |
| `phase` | `"started" \| "await_user" \| "polling" \| "done" \| "failed"` | yes | Where the login got to. |
| `userCode` | `string \| null` | no | Short code the user types in, for device-code flows. |
| `vendor` | `string` | yes | Vendor being logged into. |
| `verificationUri` | `string \| null` | no | URL to open to approve the login. |
| `verificationUriComplete` | `string \| null` | no | verificationUri with the code already embedded, when known. |

### `question.pending`

| field | type | required | description |
|---|---|---|---|
| `request` | `{ allowOther?: boolean; header?: string; index?: number; multi?: boolean; options?: ({ description?: string; label: string; preview?: string; })[]; question: string; requestId: string; sessionId: string; timeoutSec?: number; total?: number; }` | yes | The question now in the shared queue. |

### `question.resolved`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes | Surface or user that answered. |
| `requestId` | `string` | yes | Question that was resolved. |

### `session.event`

| field | type | required | description |
|---|---|---|---|
| `kind` | `string` | yes | Event kind; see sessionEventKinds for the payload schema. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |
| `seq` | `number` | yes | Monotonic per-session sequence number. |
| `sessionId` | `string` | yes | Session the event belongs to. |
| `ts` | `string` | yes | UTC ISO-8601 timestamp. |

### `settings.changed`

| field | type | required | description |
|---|---|---|---|
| `keys` | `string[]` | no | Top-level settings sections that changed, e.g. ['providers']. |
| `scope` | `"global" \| "project"` | no | Which document was reloaded; "global" for $SNOWPEA_HOME/settings.json. |

### `system.updateProgress`

| field | type | required | description |
|---|---|---|---|
| `message` | `string` | no | One line for humans. |
| `phase` | `"started" \| "done" \| "failed"` | yes | Where the upgrade got to. |

## `session.event` kinds

Every session event carries a monotonically increasing per-session `seq`. After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed.

### kind `audio.spoken`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"audio.spoken"` | no |  |
| `mime` | `string` | no | Media type of that file. |
| `path` | `string` | yes | Audio file the speech was written to. |
| `played` | `boolean` | no | True when the daemon played it. |
| `provider` | `string` | no | Backend that synthesised it. |
| `voice` | `string \| null` | no | Voice that was used. |

### kind `backend.changed`

| field | type | required | description |
|---|---|---|---|
| `backend` | `"local" \| "docker" \| "ssh"` | yes | Where tools now execute. |
| `kind` | `"backend.changed"` | no |  |

### kind `compaction`

| field | type | required | description |
|---|---|---|---|
| `after` | `number` | no | Estimated tokens the history holds now. |
| `auto` | `boolean` | no | True when the auto-compaction threshold triggered it. |
| `before` | `number` | no | Estimated tokens the history held before. |
| `kept` | `number` | no | Messages kept verbatim after the summary. |
| `kind` | `"compaction"` | no |  |
| `summaryChars` | `number` | no | Length of the summary in characters. |

### kind `context`

| field | type | required | description |
|---|---|---|---|
| `estimated` | `boolean` | no | True while 'used' is a local estimate; false once the provider reported it. |
| `kind` | `"context"` | no |  |
| `model` | `string \| null` | no | Model the window belongs to. |
| `percent` | `number \| null` | no | used/window as a percentage, null when the window is unknown. |
| `provider` | `string \| null` | no | Vendor serving that model. |
| `used` | `number` | no | Tokens the current prompt occupies. |
| `window` | `number \| null` | no | Context window of the model in tokens; null when unknown. |

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

### kind `lsp.diagnostics`

| field | type | required | description |
|---|---|---|---|
| `count` | `number` | no | Diagnostics of every severity. |
| `errors` | `number` | no | How many of them are errors. |
| `kind` | `"lsp.diagnostics"` | no |  |
| `path` | `string` | yes | File the diagnostics are about. |
| `warnings` | `number` | no | How many of them are warnings. |

### kind `message.delta`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.delta"` | no |  |
| `text` | `string` | yes | Text fragment to append to the current message. |

### kind `message.done`

| field | type | required | description |
|---|---|---|---|
| `continuations` | `number` | no | How many times the turn was resumed after hitting the output limit. |
| `kind` | `"message.done"` | no |  |
| `role` | `"assistant" \| "user" \| "system"` | no | Who produced the message. |
| `text` | `string` | yes | Full message text. |
| `truncated` | `boolean` | no | The answer still hit the output limit and is incomplete. |

### kind `message.reasoning`

| field | type | required | description |
|---|---|---|---|
| `chars` | `number` | no | Characters of reasoning so far in this turn. |
| `kind` | `"message.reasoning"` | no |  |
| `text` | `string` | no | Reasoning fragment; not part of the answer. |

### kind `mode.changed`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"mode.changed"` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | yes | Mode now in effect. |

### kind `model.changed`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"model.changed"` | no |  |
| `model` | `string \| null` | no | Model id now in effect. |
| `provider` | `string \| null` | no | Vendor now in effect. |

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
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |
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
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |

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
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |

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

### kind `turn.dequeued`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.dequeued"` | no |  |
| `queued` | `number` | no | Prompts still waiting after this one left. |
| `reason` | `"started" \| "dropped"` | no | started = it is now running, dropped = it was discarded. |
| `turnId` | `string` | yes | Turn id that left the queue. |

### kind `turn.done`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.done"` | no |  |
| `reason` | `"complete" \| "interrupted" \| "error" \| "denied" \| "timeout"` | no | Why the turn ended. |
| `turnId` | `string` | yes | Turn that ended. |

### kind `turn.queued`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.queued"` | no |  |
| `position` | `number` | yes | 1-based place in the queue behind the running turn. |
| `queued` | `number` | yes | Prompts waiting in the queue after this one was added. |
| `turnId` | `string` | yes | Turn id assigned to the queued prompt. |

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
| `auth_expired` |  |
| `internal` | Unexpected server-side failure. |
| `invalid_params` | Params failed schema validation. |
| `login_unsupported` | The vendor does not support the requested login method. |
| `mode_denied` | The session mode forbids this tool or action. |
| `not_found` | No such session, request, job, or agent. |
| `not_implemented` | Defined in the schema but not implemented in this milestone. |
| `protocol_incompatible` | Client and server protocol majors differ. |
| `tool_inactive` | The tool exists but is disabled for this session. |
| `unauthorized` | Missing or invalid token, or a call before `system.hello`. |
