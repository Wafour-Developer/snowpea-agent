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
| session | `session.create`, `session.list`, `session.resume`, `session.close`, `session.prompt`, `session.interrupt`, `session.setMode` |
| commands and tools | `command.list`, `command.run`, `tool.list` |
| approvals | `approval.list`, `approval.respond`, and `permission.allowlist.add` / `list` / `remove` |
| providers | `provider.list`, `provider.configure`, `provider.loginWeb` |
| agents | `agent.list`, `agent.create`, `agent.spawn`, `agent.bindChannel`, `agent.delete` |
| team | `team.start`, `team.status` |
| jobs | `job.schedule`, `job.list`, `job.cancel`, `job.runNow` |
| gateway | `gateway.bind`, `gateway.list`, `gateway.unbind` |
| memory | `memory.search`, `memory.write` |
| skills | `skill.search`, `skill.install`, `skill.list`, `skill.remove`, `skill.reload` |
| backend | `backend.set` |

One method goes the other way. `approval.request` is a server-to-client *request*, not a notification: the daemon asks the client for a decision and waits for the reply. That bidirectionality is the reason the protocol is WebSocket JSON-RPC rather than HTTP plus a stream.

## Events

Notifications flow from the daemon to subscribed clients.

- `session.event(sessionId, seq, kind, payload, ts)` carries everything that happens in a turn. Kinds: `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `backend.changed`, `usage`, `error`, `turn.done`.
- `approval.pending` and `approval.resolved` for the unattended approval queue.
- `job.event` and `gateway.event`.
- `commands.changed` after a skill or plugin reload.

Every `session.event` carries a `seq` that increases monotonically within its session. A client that drops its connection reconnects and calls `session.resume(sessionId, afterSeq)` to be re-sent exactly what it missed. That is why events are persisted rather than merely broadcast.

## Errors

Errors are JSON-RPC errors with a string code in `error.data.code`: `unauthorized`, `protocol_incompatible`, `not_found`, `invalid_params`, `mode_denied`, `approval_denied`, `approval_timeout`, `tool_inactive`, `not_implemented`, `login_unsupported`, `search_provider_unavailable`, `browser_provider_unavailable`, `internal`.

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
