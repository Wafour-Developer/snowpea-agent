# M19 spike S2 — Codex app-server

Tested 2026-11-15 with `codex-cli 0.167.1` on Linux (model reported by the server: `gpt-6.1-terra`, effort `low`). The owner's
background daemon (`~/.codex/app-server-control/`, a separate 0.169.0 package) was not touched; every probe used a private
`codex app-server` over stdio. Seven short turns in total. Recordings: `tests/fixtures/external_recordings/codex/`.

## Verdict

| Capability | Verdict |
|---|---|
| Create a thread | Works as designed: `thread/start {cwd, approvalPolicy, sandbox}` |
| Send into an existing thread and stream | Works as designed: `thread/resume {threadId}` then `turn/start`; stream `item/*`, end on `turn/completed` |
| Approvals | Works as designed. Server→client JSON-RPC request `item/commandExecution/requestApproval` (or `item/fileChange/requestApproval`); reply `{decision}` |
| Interrupt / steer | Works as designed: `turn/interrupt {threadId, turnId}`, `turn/steer {threadId, expectedTurnId, input}` |
| List threads, read transcript | Works, with two gotchas: the default source filter and the source our threads get (see Gotchas) |
| Open in another process | Works differently: `thread/list` in another process says `notLoaded`. Use the writer lock (`flock` on `~/.codex/thread-writer-locks/<id>.lock`), and treat the `already has an active writer` error from `thread/resume` as authoritative |
| One shared app-server, many threads | Works: two turns on two threads ran concurrently in one process; the process exits cleanly in 30 ms on stdin EOF |
| Fallback `codex exec --json` | Works for send and resume. It cannot answer approvals: exec has no approval channel |

**Recommended transport:** one shared `codex app-server` child per daemon (stdio, newline-delimited JSON-RPC 2.0 without
the `"jsonrpc"` field), spawned lazily, killed after `idleSec` with no loaded threads. Use `codex exec --json` only when
the app-server fails to start or `initialize` fails.

## Wire format

Each line is one JSON object. Requests are `{"id", "method", "params"}`, responses `{"id", "result"}` or
`{"id", "error": {"code", "message"}}`, notifications `{"method", "params"}`. The server sends its own requests (approvals)
with its own id space starting at `0`, which can collide with client ids. Route a frame that has both `method` and `id` as a
server request.

Schemas: `codex app-server generate-json-schema --out DIR` and `codex app-server generate-ts --out DIR` both exist on
0.167.1 (106 client methods, 10 server requests, 86 notifications). Generate them in CI or at adapter build time and pin
the version. The adapter should not depend on fields not listed below.

## Sequence the adapter should use

```text
spawn   codex app-server            (argv only, cwd = a registered root, env scrubbed)
→ {"id":1,"method":"initialize","params":{"clientInfo":{"name":"snowpea","title":"snowpea","version":"<v>"},"capabilities":null}}
← {"id":1,"result":{"userAgent":"snowpea/0.167.1 (...)","codexHome":"...","platformFamily":"unix","platformOs":"linux"}}
→ {"method":"initialized"}

create  → thread/start {"cwd":ROOT,"approvalPolicy":"untrusted"|"on-request","sandbox":"read-only"|"workspace-write","model"?}
        ← result {thread:{id,...}, model, approvalPolicy, sandbox:{type:"readOnly",...}, cwd, reasoningEffort, ...}
        ← thread/started {thread}
open    → thread/resume {"threadId":ID}          (error -32600 "thread <id> already has an active writer" = open elsewhere)
        ← result {thread:{..., turns:[...]}, model, approvalPolicy, sandbox, ...}
send    → turn/start {"threadId":ID,"input":[{"type":"text","text":PROMPT,"text_elements":[]}]}
        ← result {turn:{id, status:"inProgress", ...}}
        ← thread/status/changed {status:{type:"active",activeFlags:[]}} , turn/started {turn}
        ← item/started {item} … item/agentMessage/delta {itemId, delta} … item/completed {item}
        ← thread/tokenUsage/updated, account/rateLimits/updated
        ← thread/status/changed {status:{type:"idle"}} , turn/completed {turn:{id,status,items,error,durationMs}}
steer   → turn/steer {"threadId":ID,"expectedTurnId":TURN,"input":[...]}   ← result {turnId}
        wrong id: error -32600 "expected active turn id `X` but found `Y`"
stop    → turn/interrupt {"threadId":ID,"turnId":TURN}   ← result {} ; later turn/completed status "interrupted"
        no turn running: error -32600 "no active turn to interrupt"
close   → thread/unsubscribe {"threadId":ID}  ← result {status:"unsubscribed"}   (releases the writer lock)
list    → thread/list {"limit":N,"cursor"?,"cwd"?:PATH|[PATH],"sourceKinds"?:[...],"sortKey"?:"updated_at","searchTerm"?}
        ← result {data:[Thread], nextCursor, backwardsCursor}
read    → thread/read {"threadId":ID}  (metadata) then thread/turns/list {threadId,limit,cursor,itemsView:"summary"|"full"}
          and thread/items/list {threadId,turnId?,limit,cursor} ; each returns {data, nextCursor, backwardsCursor}
fork    → thread/fork {"threadId":ID,"lastTurnId"?} ← result {thread:{id, forkedFromId}}
```

`thread/read {includeTurns:true}` works but sends `deprecationNotice` ("Full-history hydration is deprecated for paginated
threads"). Page with `thread/turns/list` and `thread/items/list` instead.

`Thread` fields the adapter needs: `id`, `name` (null until set), `preview` (first user message), `cwd`, `createdAt`,
`updatedAt` (unix seconds), `status` (`notLoaded | idle | systemError | active{activeFlags: waitingOnApproval |
waitingOnUserInput}`), `source`, `path` (the rollout file), `forkedFromId`, `parentThreadId` (sub-agents), `model`,
`cliVersion`.

## Approvals

With `approvalPolicy: "untrusted"` and `sandbox: "read-only"`, "Create a file named note.txt containing the word hi"
produced a shell command, not a patch (the model wrote it with `printf`):

```json
{"method":"item/commandExecution/requestApproval","id":0,"params":{"kind":"command","threadId":"…","turnId":"…",
 "itemId":"exec-…","startedAtMs":1794744499878,"environmentId":"local",
 "command":"/bin/bash -lc 'printf hi > note.txt && cat note.txt'","cwd":"/work/proj",
 "commandActions":[{"type":"unknown","command":"printf hi > note.txt && cat note.txt"}],
 "proposedExecpolicyAmendment":["/bin/bash","-lc","printf hi > note.txt && cat note.txt"],
 "availableDecisions":["accept",{"acceptWithExecpolicyAmendment":{"execpolicy_amendment":["/bin/bash","-lc","…"]}},"cancel"]}}
```

- Allow: `{"id":0,"result":{"decision":"accept"}}` → `serverRequest/resolved {requestId:0}`, the command item completes, the file exists.
- Deny: `{"id":0,"result":{"decision":"decline"}}` → accepted although it is not in `availableDecisions`. The command item
  completes with `status: "declined"`, the model says it could not create the file, nothing is written, and the turn
  completes normally. `"cancel"` (per the schema, it also aborts the turn) was not exercised.
- Session-wide approval: `"acceptForSession"` exists in the schema for commands and file changes but was not offered for
  this command; `acceptWithExecpolicyAmendment` (persist a rule) was. Map snowpea's "always" to `acceptForSession` only when
  it appears in `availableDecisions`, never to the amendment (it writes the owner's exec policy).
- While waiting, `thread/status/changed` reports `activeFlags:["waitingOnApproval"]`. Use it for the "needs you" badge.
- A patch approval is `item/fileChange/requestApproval {threadId, turnId, itemId, reason?, grantRoot?}` with the diff in the
  matching `fileChange` item (`changes:[{path, kind, diff}]`); decisions `accept | acceptForSession | decline | cancel`. Not
  triggered in this spike. Other server requests to refuse or route: `item/permissions/requestApproval`,
  `item/tool/requestUserInput`, `mcpServer/elicitation/request`, and the legacy `execCommandApproval` /
  `applyPatchApproval`. Reply with an error to any request the adapter does not handle; an unanswered request blocks
  the turn indefinitely (an early driver bug left one unanswered and the tool call sat for 177 s until interrupted).

## Item → timeline mapping

| Codex `ThreadItem.type` | snowpea timeline |
|---|---|
| `userMessage {content:[UserInput]}` | user message |
| `agentMessage {text, phase: commentary\|final_answer}` + `item/agentMessage/delta {delta}` | assistant message (commentary may render as interim text) |
| `reasoning {summary[], content[]}` + `item/reasoning/*Delta` | reasoning block |
| `commandExecution {command, cwd, status, aggregatedOutput, exitCode, durationMs}` + `item/commandExecution/outputDelta` | tool call `shell`: args = command, cwd; result = output, exit code |
| `fileChange {changes:[{path, kind, diff}], status}` + `turn/diff/updated` | diff item |
| `mcpToolCall {server, tool, arguments, result, error, durationMs}` | tool call `mcp:<server>.<tool>` |
| `dynamicToolCall`, `functionCallOutput` | tool call (generic) |
| `plan` + `turn/plan/updated` | plan/todo item |
| `error` notification `{error:{message, codexErrorInfo, additionalDetails}, willRetry}` | error notice (transient when `willRetry`) |

## Storage, visibility and liveness

- New threads are written as rollout JSONL `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl` (`Thread.path`), with
  `historyMode: "paginated"`. The 1 GB `thread_history_1.sqlite` and `state_5.sqlite` also exist. Do not read them; use
  `thread/list` and `thread/read`, which return the same data. `session_index.jsonl` did not list the spike thread.
- `thread/list` with no `sourceKinds` returns only interactive sources (`cli`, `vscode`). A fresh app-server sees the
  owner's TUI threads (`source: "cli"`). `codex exec` threads appear only with `sourceKinds:["exec"]`.
- Threads created through a plain stdio app-server are recorded with `source: "vscode"`, not `appServer`, so they show up in
  the TUI's resume picker like an IDE thread. `sourceKinds:["appServer"]` returned nothing. `ThreadStartParams.threadSource`
  / `sessionStartSource` exist if we want to tag ours.
- Writer lock: loading a thread (start, resume or fork) creates an empty
  `~/.codex/thread-writer-locks/<id>.lock` held with `flock` by that app-server (`flock -n` fails, `fuser` shows the
  server PID). It is released on `thread/unsubscribe` or process exit, and the file is removed on clean exit. After
  `kill -9` the file stays but the lock is free, and a new process resumes normally. So the stale file alone means nothing.
- A second process calling `thread/resume` on a thread loaded elsewhere gets
  `{"code":-32600,"message":"thread <id> already has an active writer"}`. Its `thread/list` and `thread/read` still say
  `status: notLoaded`. Status is per process, so it is useless for liveness.
- **Open elsewhere** = the lock is held by a process that is not our app-server. Check without taking the lock: read
  `/proc/locks` and match the lock file's inode (Linux). On macOS, use `lsof <lockfile>`. Do not probe with `flock -n`,
  because it could make a concurrent codex writer fail. Confirm at open time with the `active writer` error, and then
  show the view-only banner. Not verified: whether the interactive TUI (0.167 standalone or via the 0.169 daemon) takes
  the same lock. It is the same code path, so it should, but WP 6b must check this once with a live TUI.

## Shared server, idle, shutdown, auth

- Concurrency: a steered and interrupted turn on thread A and an approval-denied turn on fork B ran at the same time in
  one process. Notifications carry `threadId` (and `turnId`). Demultiplex on those fields.
- `thread/loaded/list` returns the threads this process holds. Unsubscribe each thread when its view closes, so the
  owner's TUI can take it. Then exit after `idleSec` with nothing loaded.
- Shutdown: closing stdin makes the server exit 0 in about 30 ms and release all locks.
- Startup noise: `configWarning`, `warning` (config and hooks parse problems from the owner's config),
  `remoteControl/status/changed`, `mcpServer/startupStatus/updated` for each configured MCP server. Every thread loads the
  owner's MCP servers, `AGENTS.md` and hooks.
- Auth: with an empty `CODEX_HOME`, `account/read` returns `{account:null, requiresOpenaiAuth:true}` and `thread/start`
  succeeds. `turn/start` is accepted, then
  `error {error:{message:"Reconnecting... 2/5", codexErrorInfo:{responseStreamDisconnected:{httpStatusCode:401}},
  additionalDetails:"unexpected status 401 Unauthorized: …"}, willRetry:true}`. Call `account/read` after `initialize`
  and show "codex: not logged in" when `account` is null, instead of waiting for retries.
  `CodexErrorInfo` also has `unauthorized`, `usageLimitExceeded` and `rateLimitExceeded`.

## Fallback: `codex exec --json`

```text
echo "Reply with exactly: OK" | codex exec --json -C ROOT -s workspace-write -
{"type":"thread.started","thread_id":"01a1df1d-…"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"<config warning>"}}   (non-fatal, ignore)
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"OK"}}
{"type":"turn.completed","usage":{"input_tokens":25877,"cached_input_tokens":7168,"output_tokens":5,…}}
echo "…" | codex exec resume --json <thread_id> -        (same shape, same thread_id)
```

- Prompt on stdin via `-`. Item types are snake_case (`agent_message` and `error` were observed). `command_execution`,
  `file_change`, `turn.failed` and the top-level `error` come from upstream docs and were not triggered in this spike.
- exec has no `-a`/approval flag and no channel to answer approvals. Anything outside the sandbox fails, and the model
  is told. `--approve-for-me` exists; do not use it. In fallback, `workspace-write` is the most we allow, and approvals
  show as "not available in degraded mode".
- `codex exec resume --help` lists no `-C`/`-s`. Run it with the process cwd set to the thread's root.

## Gotchas

1. The server's request ids start at 0 and are independent of ours. Never match responses by id alone.
2. A Python reader that combines `select()` with a buffered `readline()` misses frames. Read raw bytes and split lines.
3. The flags `--dangerously-bypass-approvals-and-sandbox` and `--dangerously-bypass-hook-trust` exist on exec, and
   `danger-full-access` is a valid `sandbox`. `proc.py` must refuse them, along with `approvalPolicy: "never"` combined with
   `workspace-write` unless the person granted it.
4. The owner's `AGENTS.md`, MCP servers and hooks apply to every thread we start, and `features.code_mode` wraps shell
   calls in a JS `exec` tool. The approval still arrives as a normal `commandExecution` request.
5. The `--listen unix://` daemon (`codex app-server proxy`) could share the owner's server. We do not use it: it is
   0.169 vs CLI 0.167 here, and a crash or restart on our side would affect the owner's TUI.
6. Do not call `acceptWithExecpolicyAmendment`; it writes rules into the owner's codex config.

## Fixtures

`tests/fixtures/external_recordings/codex/`. Each `.jsonl` starts with a `_comment` line. Frames are
`{"dir":"client->server"|"server->client","t":<unix>,"msg":{…}}`. Paths, host name, installation id, plan, MCP server
names and config warnings are scrubbed.

| File | Content |
|---|---|
| `appserver_turn.jsonl` | initialize, thread/start, one text turn |
| `appserver_approval_allow.jsonl` | resume, command approval, accept |
| `appserver_approval_deny.jsonl` | fork thread, command approval, decline |
| `appserver_steer_interrupt.jsonl` | steer ok, steer wrong id, interrupt → `interrupted` |
| `appserver_second_writer.jsonl` | second process: `already has an active writer` |
| `appserver_noauth_error.jsonl` | no login: 401 error notification |
| `thread_list.json`, `thread_read.json`, `thread_turns_list.json`, `thread_items_list.json` | list/read responses |
| `exec_oneshot.jsonl`, `exec_resume.jsonl` | `codex exec --json` stdout |
