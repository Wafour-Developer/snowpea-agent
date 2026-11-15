# M19 spike S1: Claude Code

Tested on 2026-11-15 against `claude` **2.1.325 (Claude Code)** on Linux, model `haiku`, in a throwaway git repo. All runs passed `--safe-mode` so the recordings carry no user hooks or plugins; the adapter does not need that flag. Total spend was eight short turns, about $0.03.

Fixtures (scrubbed) are in `tests/fixtures/external_recordings/claude/`. In `bidirectional.jsonl`, `permission_*.jsonl` and `interrupt.jsonl`, each line is `{"dir": "in"|"out", "t": seconds, "frame": {...}}`, where `in` is what the host wrote to stdin. `oneshot.jsonl` and `transcript_sample.jsonl` are raw lines. Placeholders: `<PROJ>` is the project cwd, `<ENCODED_PROJ>` is its encoded form, plus `<HOME>`, `<CLAUDE_TMP>`, `<RUNTIME_DIR>`, `<EMAIL>`, `<SIGNATURE>` and `<ACCOUNTUUID>`/`<ORGANIZATIONUUID>`. The init frame's long lists (tools, skills, commands, plugins) are replaced with `["<trimmed>"]`.

## Verdicts

| # | Capability | Verdict |
|---|---|---|
| 1 | Create a session | **Works as designed.** `--session-id <uuid>` makes the id; `system/init.session_id` echoes it. |
| 2 | Send into an existing session and stream the reply | **Works as designed.** One long-lived process per session: `--resume <id> --input-format stream-json --output-format stream-json`. Many turns run on one process, and each turn ends with a `result` frame. |
| 3 | Permission prompts, answered by the host | **Works, with one extra flag.** `--permission-prompts host` alone **auto-denies** (`system/permission_denied`, no frame for the host). You also need `--permission-prompt-tool stdio`, and then the CLI emits `control_request{subtype:"can_use_tool"}` and waits. The acceptEdits fallback is not needed. |
| 4 | Interrupt a turn | **Works.** Send `control_request{subtype:"interrupt"}`; it is acked with `control_response{response:{still_queued:[]}}` and killed the running background task. The spike did not capture a turn cut off mid-generation (both test turns finished first), so the fake CLI should assume the turn ends with a `result` frame. Closing stdin ends the process cleanly (exit 0). |
| 5 | List sessions and read a transcript | **Works.** Read `~/.claude/projects/<encoded cwd>/<id>.jsonl` directly. `claude agents --json` lists only *live* sessions (keys `cwd,id,kind,name,sessionId,startedAt,state`). The CLI has no command that lists stored sessions. |
| 6 | Is the session open elsewhere? | **Works by inspecting files.** `~/.claude/sessions/<pid>.json` has `sessionId`; the session is open elsewhere when that pid is alive and `procStart` matches. A second `--resume` of a live session is **not refused and not forked**: both processes append to the same file under the same id, and the first process never sees the second one's turn. The adapter must refuse (or use `--fork-session`) when the session is live. |

**Recommended transport:** one `claude -p --input-format stream-json --output-format stream-json` child per open external session, kept alive while it is in use (idle-close after `external.idleSec`). Send an `initialize` control request first, then user frames. Answer `can_use_tool` through `ApprovalQueue`, and interrupt with the control request. Read history from the transcript file, not from the process.

## Command lines

```
# new session (daemon picks the uuid)
claude -p --input-format stream-json --output-format stream-json --verbose \
  --permission-mode default --permission-prompts host --permission-prompt-tool stdio \
  --session-id <uuid> [--model <alias|id>] [--add-dir <dir>...]
# continue an existing one
claude -p ... same flags ... --resume <uuid>
# continue as a copy when it is open elsewhere
claude -p ... --resume <uuid> --fork-session
# one-shot (no permission prompts possible; stdin must be closed or you get a 3 s warning on stderr)
claude -p --output-format stream-json --verbose [--include-partial-messages] --session-id <uuid> "<prompt>" < /dev/null
```

- Run with `cwd` set to the project. The process cwd **is** the session's project; there is no `--cwd` flag. `system/init` reports `cwd` and `original_cwd`.
- `--permission-mode` choices on 2.1.325 are `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`. `init.permissionMode` reports `"default"` when `manual` is passed. Never pass `bypassPermissions` or `--dangerously-skip-permissions` (`proc.py` rejects them).
- `--permission-prompts none` denies everything that would prompt. With `acceptEdits`, file edits in the working dirs run without a prompt, and shell commands that write files still prompt (so they are denied under `none`). Use `--allowedTools` / `--disallowedTools` for per-tool rules.
- `--replay-user-messages` echoes each stdin user frame back as `{"type":"user",...,"uuid","timestamp","isReplay":true}`. That frame is the ack carrying the CLI's uuid for the message. Recommended.
- `--include-partial-messages` adds `stream_event` frames that wrap raw Messages API events. Without it you get whole `assistant` frames per content block.
- `--max-budget-usd`, `--name`, `--effort`, `--fallback-model` exist and fit the `jarvis.budget` setting. `--no-session-persistence` turns off the transcript (do not use it).
- Exit code is 0 on a normal end (stdin closed, or one-shot done). Errors arrive as `result` with `is_error: true` and a `subtype` other than `success` (seen in the frame schema: `subtype`, `is_error`, `api_error_status`, `terminal_reason`). Not tested: logged-out behaviour. The help text says `--bare` uses only `ANTHROPIC_API_KEY`/`apiKeyHelper`, and `init.apiKeySource` reports the source. The adapter should treat a first-turn `result.is_error` with an auth-like `result` text as "not logged in" and point the user to `claude auth`.
- Environment: strip snowpea's daemon token as planned. `CLAUDE_CODE_SIMPLE=1` (= `--bare`) and `CLAUDE_CODE_SAFE_MODE=1` (= `--safe-mode`) change behaviour, so do not leak them from the daemon's environment.

## Frames (stdout → host)

Order of one turn (`oneshot.jsonl`, `bidirectional.jsonl`):

```
system/init        {session_id, cwd, original_cwd, model, permissionMode, tools[], mcp_servers[], claude_code_version, messaging_socket_path, ...}   # once per turn in stream-json input mode
system/status      {status}                                       # one-shot only
user               {isReplay:true, uuid, timestamp, message}       # with --replay-user-messages
stream_event       {event:{type: message_start|content_block_start|content_block_delta{delta:{type:"text_delta",text}}|content_block_stop|message_delta{delta:{stop_reason}}|message_stop}}   # partials only
assistant          {message:{id, model, content:[ONE block: text | thinking{thinking:"",signature} | tool_use{id,name,input}]}, parent_tool_use_id, session_id, uuid}
user               {message:{content:[{type:"tool_result", tool_use_id, content, is_error?}]}, tool_use_result:{...}, tool_result_meta:[{id, permission_decision:{decision, source}}]}
system/*           thinking_tokens, permission_denied{tool_name,tool_use_id,message}, task_started / task_updated / task_notification / background_tasks_changed, ui_invalidate
rate_limit_event   {rate_limit_info:{status, utilization, resetsAt, ...}}
result             {subtype:"success"|error, is_error, result:<final text>, stop_reason, terminal_reason, num_turns, session_id, total_cost_usd, usage, permission_denials:[{tool_name,tool_use_id,tool_input}], duration_ms}
```

Gotchas: each `assistant` frame holds **one** content block, and several frames share one `message.id`, so group by id. The thinking text is empty (only the signature comes through), so show it as a "thinking" marker only. `tool_use_result` on a Write is `{type:"create", filePath, content, structuredPatch:[], originalFile:null}`, and on an Edit it carries `structuredPatch` hunks. That is the diff source.

## Frames (host → stdin)

```
{"type":"control_request","request_id":"init_1","request":{"subtype":"initialize"}}
   -> {"type":"control_response","response":{"subtype":"success","request_id":"init_1","response":{"commands":[...], ...}}}
{"type":"user","message":{"role":"user","content":"<text>"},"parent_tool_use_id":null,"session_id":""}
{"type":"control_request","request_id":"int_1","request":{"subtype":"interrupt"}}
   -> {"type":"control_response","response":{"subtype":"success","request_id":"int_1","response":{"still_queued":[]}}}
```

The CLI accepts a user frame while another turn is running and queues it (`still_queued` lists what was queued when you interrupt).

## Permission prompt (`permission_allow.jsonl`, `permission_deny.jsonl`)

Ask (stdout):

```json
{"type":"control_request","request_id":"<uuid>","request":{"subtype":"can_use_tool","tool_name":"Write","display_name":"Write",
 "input":{"file_path":"<PROJ>/note.txt","content":"hi\n"},"description":"note.txt",
 "permission_suggestions":[{"type":"setMode","mode":"acceptEdits","destination":"session"}],
 "server_policy_ask_only":true,"tool_use_id":"toolu_..."}}
```

Answers (stdin), keyed by the same `request_id`:

```json
{"type":"control_response","response":{"subtype":"success","request_id":"<id>","response":{"behavior":"allow","updatedInput":{...input echoed...}}}}
{"type":"control_response","response":{"subtype":"success","request_id":"<id>","response":{"behavior":"deny","message":"The owner declined this write.","interrupt":false}}}
```

- Allow produces `tool_result_meta.permission_decision {decision:"accept", source:"user_temporary"}`, and the transcript records `grantScope:"once"`.
- Deny makes `message` the tool_result text the model sees (`is_error:true`, `source:"user_reject"`), and the tool is listed in `result.permission_denials`. A deny without a reason should still send a `message` (we used `"Denied"`). Setting `"interrupt": true` also ends the turn (not exercised).
- "Allow always" means adding `"updatedPermissions": <permission_suggestions or your own rules>` to the allow response. For this Write the only suggestion was `setMode acceptEdits` for the session. This was not exercised, so map "always" to it only after the fake-CLI test is written.
- The CLI decides itself what needs asking. Here Bash `sleep` ran without a prompt (`source:"config"`), and some commands are blocked outright by built-in rules (a `tool_use_error` result, no prompt).

## Transcript file and timeline mapping (`transcript_sample.jsonl`)

Path: `~/.claude/projects/<enc>/<sessionId>.jsonl`, where `enc` = absolute cwd with every char outside `[A-Za-z0-9]` replaced by `-` (verified: `/tmp/claude-1000/...-snowpea/c8c4...` → `-tmp-claude-1000--mnt-data-...`). It is append-only and grows on every turn from any process.

Record types seen: `user`, `assistant`, `attachment`, `queue-operation`, `mode`, `last-prompt`, `tools-used`, `cost-state`, `session-owner`, `atis-latch`, `api-request*`. **Render only `user` and `assistant`.** Skip the rest: `attachment` holds the system prompt, environment, **the user's email and org id**, and `session-owner` holds account ids. Never pass those records to Jarvis or clients.

| snowpea timeline item | Transcript source |
|---|---|
| user message | `type:"user"`, `message.content` is a **string** (`promptSource`/`turnOrigin` = `sdk` for our turns), plus `uuid`, `timestamp` |
| assistant message | `type:"assistant"`, block `text`. Group blocks by `message.id` |
| thinking | block `thinking` (text is empty, show a marker only) |
| tool call (args) | block `tool_use {id, name, input}` |
| tool result | `type:"user"` whose content is a list with `tool_result {tool_use_id, content, is_error}`, plus `toolUseResult` (structured), `permissionDecision`, `toolDenialKind` |
| diff (Write/Edit) | `toolUseResult.structuredPatch` (Edit hunks). For Write it is `type:"create"` with `content` and `originalFile:null` |
| title | No title record appeared. Use the `name` from `sessions/<pid>.json` (derived, e.g. `proj-1c`) when live, otherwise `last-prompt.lastPrompt` or the first user text |
| ordering | `parentUuid` chain. `isSidechain:true` marks subagent records (skip or nest) |

## Liveness (`sessions_pid.json`)

`~/.claude/sessions/<pid>.json` keys: `pid, sessionId, cwd, startedAt, procStart, version, peerProtocol, peerFeatures, kind, entrypoint ("sdk-cli" for us, interactive otherwise), pidDomain, tmux, messagingSocketPath, name, nameSource, status ("busy"|"idle"), updatedAt, statusUpdatedAt`. The file exists for print-mode processes too, so snowpea's own children show up and must be told apart by pid. Rule: open elsewhere = some file has `sessionId == id`, the pid is alive, it is not one of our children, and `/proc/<pid>/stat` start time matches `procStart`. `status` also tells whether it is mid-turn.

Delivering a message to a running *interactive* session: the CLI's only public route is for **background** sessions: `claude --bg`, then `claude attach|logs|stop|rm <id>`, listed by `claude agents --json`. `claude --bg --resume <id>` "continues that session in the background under the same ID, or starts a copy and says so when the session is already running". No `send` subcommand exists. `messagingSocketPath` is undocumented and was not probed. Fallback for a live interactive session: view-only, or `--fork-session`.

## Gotchas

1. `--permission-prompts host` without `--permission-prompt-tool stdio` silently auto-denies in print mode.
2. A concurrent `--resume` corrupts the conversation history (two writers, no lock). Check liveness first.
3. In stream-json input mode, `system/init` is re-sent at the start of **every** turn.
4. One-shot mode with an open stdin waits 3 s and warns. Use `< /dev/null`.
5. User settings (hooks, plugins, CLAUDE.md, permission rules) apply unless `--safe-mode`/`--setting-sources` say otherwise. The adapter should leave them on (that is the user's Claude), but the fake-CLI fixtures were recorded without them.
