# Protocol

The full, generated reference is [docs/protocol.md](../../protocol.md). This page is the orientation you want before reading it.

Everything the core can do is a JSON-RPC method before it is a UI affordance. There is no private channel between the daemon and the terminal UI — the TUI is an ordinary client, and anything it can do, your client can do too.

## Source of truth

`core/snowpea_core/server/protocol.py` defines `PROTOCOL_VERSION`, every method's parameter and result schema, and every event payload. `scripts/gen_protocol.py` generates `docs/protocol.md` and `sdk/src/protocol.ts` from it. Neither generated file is ever edited by hand, and CI fails the build if they drift:

```bash
uv run python scripts/gen_protocol.py --check
```

## Transport

The daemon binds a loopback port chosen at start-up and records it, with an auth token, in `$SNOWPEA_HOME/daemon.json`.

| Surface | Endpoint | Purpose |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0, bidirectional |
| HTTP GET | `/health` | liveness |
| HTTP GET | `/version` | server and protocol versions |
| HTTP GET | `/protocol.json` | the whole schema as JSON |

The HTTP endpoints are read-only conveniences for probes, installers and debugging. Every state-changing call exists on the WebSocket only.

## Handshake

Immediately after connecting, a client calls `system.hello` with the token from `$SNOWPEA_HOME/token`, its own version, and the protocol version it was built against. Any other method before that fails with `unauthorized`. A major-version mismatch is rejected with `protocol_incompatible`.

```json
{"jsonrpc":"2.0","id":1,"method":"system.hello",
 "params":{"token":"…","clientVersion":"0.1.0","protocolVersion":"0.1.0"}}
```

The result carries `protocolVersion`, `serverVersion` and a `capabilities` list, which is how features are negotiated once the version is frozen.

## Methods, by area

| Area | Methods |
|---|---|
| system | `system.hello`, `system.info`, `system.health`, `system.shutdown` |
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.compact`, `session.deleteSaved`, `session.setMode`, `session.setModel` |
| commands and tools | `command.list`, `command.run`, `tool.list` |
| approvals | `approval.list`, `approval.respond`, and `permission.allowlist.add` / `list` / `remove` |
| providers | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agents | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow`, `job.update`, `job.runs` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memory | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload`, `skill.create`, `skill.read`, `skill.write` |

One method goes the other way. `approval.request` is a server-to-client *request*, not a notification: the daemon asks the client for a decision and waits for the reply. That bidirectionality is the reason the protocol is WebSocket JSON-RPC rather than HTTP plus a stream.

## Events

Notifications flow from the daemon to subscribed clients.

- `session.event(sessionId, seq, kind, payload, ts)` carries everything that happens in a turn. Kinds: `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `backend.changed`, `model.changed`, `usage`, `context`, `compaction`, `audio.spoken`, `error`, `turn.queued`, `turn.dequeued`, `turn.done`.
- `approval.pending` and `approval.resolved` for the unattended approval queue.
- `job.event` and `gateway.event`.
- `commands.changed` after a skill or plugin reload.

Every `session.event` carries a `seq` that increases monotonically within its session. A client that drops its connection reconnects and calls `session.resume(sessionId, afterSeq)` to be re-sent exactly what it missed. That is why events are persisted rather than merely broadcast.

Since 1.51.0 the events a client used to read English text out of carry codes instead, so it can write its own line in its own language: `provider.retry` has `reason` (`unreachable`, `silent`, `transient`) and `waitLeftSec`, with `max` null when the limit is time; `hook.continue` has `kind` (who kept the turn going), `count` and `limit`; `step.done` has `errorCode`; a goal's `blockedReason.code` is one of `round-limit`, `needs-input`, `no-credentials`, `no-access`, `external`, `other`; and `session.trajectory` rows of the context, system and compacted kinds carry `data.kind`. The English text stays alongside for older clients; the daemon does not translate it.

Since 1.53.0 every `message.user` event carries `origin` — `{kind, surface}`, with `kind` one of `human`, `jarvis`, `schedule`, `gateway`, `subagent`, `system` (capability `messageOrigin`); `session.trajectory` user rows repeat it as `data.origin`. A session's `lastPrompt` and `lastUserAt` follow only the person's own prompts (origin `human`), so goal rounds, hand-backs, scheduled and delegation prompts no longer replace them, and `session.fork` keeps the source's title unchanged. 1.53.0 also declares the contract of Jarvis mode (beta): `jarvis.*`, `external.*` and `mcp.preset.*` methods, the `jarvis.*` and `external.*` events, the `role` and `jarvis` session fields and the approval `external` and `route` fields. Those methods answer `not_implemented`, and their capabilities (`jarvis`, `jarvisDelegation`, `externalSessions`, `mcpPresets`, `jarvisBriefing`) stay out of `system.hello`, until the feature ships.

## Errors

Errors are JSON-RPC errors with a string code in `error.data.code`: `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `skill_exists`, `mcp_auth_failed`, `search_provider_unavailable`, `browser_provider_unavailable`, `fork_after_compaction`, `fork_turn_not_found`, `fork_no_finished_turn`, `fork_child_session`, `fork_running` (`session.fork`, 1.50.0), `steer_slash_command` (`session.steer`, 1.50.0), `goal_not_allowed` (`goal.set`/`goal.resume`, 1.50.0), `jarvis_disabled`, `jarvis_person_only`, `external_view_only`, `external_unavailable` (Jarvis mode, 1.53.0), `security_blocked` (1.54.0), `internal`.

Some codes arrive only as the `code` of an `error` session event, inside a turn. Since 1.55.0 that event may carry `details` with the facts for the code, so a client can write its own message:

| Code | When | `details` |
| --- | --- | --- |
| `hook_failed` | A plugin hook could not start, crashed or timed out (shown once per session per hook; the call itself goes on unless the hook is `failClosed`). | `{plugin, hook, reason, exitCode}`: `hook` is `PreToolUse`, `PostToolUse` or `Stop`; `exitCode` is `-1` when the hook could not start or timed out. |
| `context_window_too_small` | The model's context window cannot hold snowpea's system prompt and tool definitions, so no request can fit; the turn ends with reason `error`. | `{windowTokens, neededTokens}` |

A started tool call always ends with a `tool.result`: when the turn is cancelled under it (daemon shutdown, an interrupt that cancels the task) the result has `ok: false` and `error: "cancelled"`.

A `personOnly` question or approval answered by the asking connection without a person's marker is not dropped: that connection receives `question.pending` / `approval.pending` again with the same request and a `refused` object (`{code: "jarvis_person_only", reason, message}`), and the request stays open for a person to answer.

## Using the SDK

```ts
import { connect } from "@snowpea/sdk";

const client = await connect({ port, token, clientVersion: "1.0.0" });
const { sessionId } = await client.call("session.create", { workdir: process.cwd(), mode: "accept" });

client.on("session.event", (e) => {
  if (e.kind === "message.delta") process.stdout.write(e.payload.text);
});

client.onRequest("approval.request", async (req) => ({ decision: "deny", scope: "once" }));

await client.call("session.prompt", { sessionId, text: "what does this repo do?" });
```

The client reconnects on its own and resumes from the last `seq` it saw. `sdk/src/protocol.ts` gives you the types for every method and event; it is generated, so it cannot describe a method the daemon does not have.

## Versioning and the freeze gate

`PROTOCOL_VERSION` is semver. Before the v0.2 desktop IDE starts, the protocol must pass a freeze gate: version `1.0.0`, and no change to `docs/protocol.md` or the generated schema across three consecutive release tags, with the SDK contract tests passing on all three.

```bash
uv run python scripts/gen_protocol.py --check
git log --oneline -- docs/protocol.md sdk/src/protocol.ts
```

After the freeze, additive changes need a minor bump and go through `capabilities` negotiation in `system.hello`. A major change means releasing every client at the same time, which is exactly the cost the gate exists to make visible.
