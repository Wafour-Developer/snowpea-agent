# S3 — Antigravity CLI (`agy`) spike

Tested 2026-11-15 on Linux with `agy` 1.5.7 (`agentInfo.version` 1.5.7, ACP `protocolVersion` 1). Nine short turns (plus token-free `session/load` and `session/list` calls) in a throwaway git repo. Fixtures are in `tests/fixtures/external_recordings/agy/`; each ACP line is `{"dir": "c2a"|"a2c", "t": seconds, "msg": <JSON-RPC frame>}` (`c2a` = client to agent). Paths are scrubbed to `/tmp/spike-agy/proj`.

**Recommended transport:** one `agy acp` child per open session (stdio JSON-RPC, NDJSON). It covers create, send, stream, permissions, cancel, list and transcript replay. `agy -p --output-format stream-json` is only a fallback, because it cannot answer permission prompts.

## Verdicts

| # | Capability | Verdict |
|---|---|---|
| 1 | Create a session | Works as designed: `session/new`. |
| 2 | Send into an existing session and stream | Works as designed: `session/load`, then `session/prompt`. `session/resume` is also advertised but untested. |
| 3 | Permission prompts | Works, with two differences. Only `allow_once`/`reject_once` are offered (no "always"). The prompts follow the **owner's global `toolPermission`**, and on this host that setting is `always-proceed`, so no prompt ever arrives. See [Permissions](#permissions). |
| 4 | Interrupt | Works as designed: the `session/cancel` notification, then `session/prompt` resolves `{"stopReason":"cancelled"}` within 3 ms. |
| 5 | List and transcript | Works as designed. ACP `session/list` returns `{sessionId,cwd,title,updatedAt}`. The summaries sqlite has richer rows. `session/load` replays the history as `session/update` notifications. |
| 6 | Open in another process | Works differently. The `status` column is not a liveness signal. An interactive `agy` holds an **flock** on `~/.gemini/antigravity-cli/presence/<conversationId>.lock`. ACP processes do **not** take that lock, and a second `session/load` of a session already loaded elsewhere succeeds without complaint. |

## ACP sequence the adapter should use

Spawn `agy acp` with cwd set to the project root. Frames are newline-delimited JSON-RPC 2.0 on stdin and stdout. stderr is a log.

```
c2a {"id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false},"terminal":false}}}
a2c result: protocolVersion 1, agentCapabilities{loadSession, promptCapabilities{image,embeddedContext}, sessionCapabilities{resume,close,load,list,additionalDirectories}, auth{logout}}, agentInfo, authMethods[oauth-personal|oauth-business|gemini-api-key|agent-platform]
c2a {"id":2,"method":"session/new","params":{"cwd":"<root>","mcpServers":[]}}
a2c result: {sessionId, models{availableModels,currentModelId}, modes{currentModeId:"default", availableModes:[default,accept-edits,auto,plan]}, configOptions[model,thought_level,mode], _meta.antigravity.tier}
c2a {"id":3,"method":"session/prompt","params":{"sessionId":S,"prompt":[{"type":"text","text":"..."}]}}
a2c session/update notifications ... then result {"stopReason":"end_turn","usage":{inputTokens,outputTokens,cachedReadTokens,cachedWriteTokens,thoughtTokens,totalTokens}}
```

- **The client implements no fs or terminal methods.** With `fs`/`terminal` set to false, agy runs its tools itself and never calls the client. The adapter must answer any other agent-to-client request with `-32601` (none was seen).
- **Existing conversation:** `session/load {sessionId,cwd,mcpServers:[]}`. The history arrives as `session/update` frames **before** the load result. The order is `user_message_chunk`, `agent_thought_chunk`, `tool_call` (final status, args, diff content), and one coalesced `agent_message_chunk` per response. A trailing `session_info_update {title}` comes after the result. Render these as the transcript (`acp_load.jsonl`). Loading spends no tokens.
- **Cancel:** send the notification `{"method":"session/cancel","params":{"sessionId":S}}` (no id). The pending `session/prompt` returns `stopReason:"cancelled"` (`acp_cancel.jsonl`).
- **Modes:** `modes.availableModes` lists `default` ("Ask before edits and commands, per your permission settings"), `accept-edits`, `auto` and `plan`. It is set through `session/set_mode {sessionId,modeId}` or `session/set_config_option` (id `mode`). Both handlers exist in the binary; neither was exercised.
- **Close:** `session/close` is advertised. Otherwise close stdin and the process exits within 1 s.

### `session/update` variants seen

| `sessionUpdate` | Shape | Timeline item |
|---|---|---|
| `available_commands_update` | `availableCommands[{name,description}]` (slash commands, skills) | ignore |
| `usage_update` | `{used,size}` context tokens (appears only with a fresh `--gemini-dir`) | context meter |
| `user_message_chunk` | `content{type:text,text}` (echo of the prompt) | user message |
| `session_info_update` | `{title}` (auto title about 2 s into the first turn) | title |
| `agent_thought_chunk` | `content{type:text,text}` | thinking |
| `agent_message_chunk` | `content{type:text,text}`, streamed in deltas of about 50 chars | assistant text |
| `tool_call` | `{toolCallId,name,args,status:"pending"/"in_progress",kind:"edit"/"execute",title,locations[{path}],rawInput}` | tool start |
| `tool_call_update` | the same fields plus `status:"in_progress"/"completed"/"failed"`, `rawOutput`, `content[{type:"diff",path,newText}]` or `[{type:"content",content{type:text,text}}]` | tool result |

No `plan` update was observed. `name` and `args` are agy extras beyond standard ACP.

### Permissions

The agent sends a request with a **string** id:

```
a2c {"id":"1","method":"session/request_permission","params":{"sessionId":S,"toolCall":{"sessionUpdate":"tool_call","toolCallId":..,"name":"run_command","args":{"CommandLine":"echo hi > shell_allow.txt",...},"status":"pending","kind":"execute","title":"echo hi > shell_allow.txt","rawInput":{...}},"options":[{"optionId":"allow","name":"Allow","kind":"allow_once"},{"optionId":"deny","name":"Deny","kind":"reject_once"}]}}
c2a {"id":"1","result":{"outcome":{"outcome":"selected","optionId":"allow"}}}     # or "deny"
```

- **Allow:** `tool_call_update` goes to `in_progress`, then `completed` with `rawOutput.output`, and the turn continues (`acp_permission_allow.jsonl`).
- **Deny:** `tool_call_update` goes to `failed` with empty output, and the turn ends at once with `end_turn` and **no assistant text** (`acp_permission_deny.jsonl`). The adapter should add a "denied" note itself.
- **Missing option:** there is no `allow_always` option. Remembering a choice has to happen on snowpea's side through grants. Echo the request `id` back verbatim, since it is a string.
- **Global policy decides whether a prompt fires.** `~/.gemini/antigravity-cli/settings.json` holds `toolPermission`, with the values `always-proceed`, `request-review` (the default), `strict` and `proceed-in-sandbox`, plus `allowNonWorkspaceAccess`. On the owner's settings (`always-proceed`), in-workspace writes, outside-workspace writes and commands all ran with **no** request. Passing `_meta.toolPermission` (top level or under `antigravity`) in `session/new` is ignored. With a fresh settings file (`--gemini-dir <dir>`, default `request-review`), commands (`kind:"execute"`) prompt, but in-workspace file writes still do not.
- **Adapter rule:** surface ACP permission requests into the ApprovalQueue when they come. Show the effective `toolPermission` in the window header. When it is `always-proceed`, mark the session "runs without asking (owner setting)" instead of promising approvals. Do not use an isolated `--gemini-dir`: conversations created that way go to a separate store and are missing from the owner's list. `--gemini-dir` must be absolute, and `--app-data-dir` must be relative or the language server fails.
- **Authentication:** `initialize` advertises `authMethods` and `auth.logout`. A fresh `--gemini-dir` still authenticated (the credential is not under the gemini dir), so a real unauthenticated state was not observed. Logging out was not attempted, per the spike rules. Expect `session/new` to fail with a JSON-RPC error. Map that error, and any `-32603` "Failed to start Antigravity: …", to `external.state {status:"needs-login"}` with the hint `agy acp --login`.

## Listing

- **Primary:** ACP `session/list {}` returns `{sessions:[{sessionId,cwd,title,updatedAt}]}`, newest first. It was **capped at 500** rows with no cursor, and the `cwd` param did not filter (500 rows across 11 cwds both ways). Filter on the client (`acp_list.jsonl`).
- **Richer and offline:** open `~/.gemini/antigravity-cli/conversation_summaries.db` with `file:…?mode=ro`. Table `conversation_summaries` (542 rows here, so the ACP list truncates):

| Column | Use |
|---|---|
| `conversation_id` (PK) | id |
| `title` | title (`""` until the first title update) |
| `preview` | first prompt; overwritten with the title after a load |
| `last_modified_time` | sort key (text datetime with zone) |
| `workspace_uris` | JSON array of `file://` URIs: the workspace |
| `step_count`, `last_user_input_time` | activity |
| `status` | `""` or `CASCADE_RUN_STATUS_IDLE`; **not** liveness |
| `not_fully_idle`, `killed` | always 0 here |
| `parent_conversation_id`, `nesting_depth`, `agent_name` | subagent rows: hide when `nesting_depth > 0` |
| `project_id`, `app_data_dir`, `group_id`, `raw_summary` (protobuf) | ignore |

Also present: the FTS5 table `conversations_fts` (title, workspace, user_body, agent_body), which a search can use. A sample row is in `summaries_row.json`.
- ACP-created conversations appear in the DB **immediately** (the row was written by the end of the first turn). They do **not** appear in `history.jsonl`. That file is the interactive prompt history with keys `{display, timestamp(ms), workspace, conversationId?, type?}` (`type: "slash_command"`), and it is useless for listing ACP sessions.
- Transcript bodies live in `~/.gemini/antigravity-cli/conversations/<id>.db`. Do not parse them; use `session/load`.

## Liveness

- `presence/<conversationId>.lock` (an empty file) is flock-held by the interactive `agy` that has the conversation open. To check, look for the file's inode in `/proc/locks`; do not take the lock. Lock files persist after exit, so their existence alone means nothing.
- `agy acp` sessions take no presence lock. A process scan for `agy` without `acp` and an `/proc/locks` inode match give "open in the agy TUI". Snowpea's own ACP children are known by pid.
- `session/load` of a session that another `agy acp` process has loaded **succeeds**, and nothing fences the two writers. This was tested with two ACP processes; loading against a TUI that holds the presence lock was not tested, because the only held lock belongs to the owner's conversation. The adapter must refuse `send` when the presence lock is held, offer view-only, and say the session is open in the agy TUI.

## Fallback: print mode

`agy -p "<prompt>" --output-format stream-json [--conversation <id>] [--mode accept-edits|auto|plan] [--settings '<json>']` (`print_stream_json.jsonl`):

- `{"event":"init","conversation_id":…,"init":{cwd,tools[…]}}` carries the new conversation id.
- `{"event":"step_update","step_update":{conversation_id,step_index,state:"ACTIVE"|"DONE",step_type:"user_input"|"agent_response"|"tool",tool_name?,tool_info{name,parameters}?,text_delta?,usage?,duration_seconds?}}`
- `{"event":"result","result":{conversation_id,status:"SUCCESS",response,duration_seconds,num_turns,usage}}`
- Even with `--settings '{"toolPermission":"request-review"}'`, the in-workspace write ran without asking. Print mode cannot answer prompts, so only use it with `--mode plan`, or with `accept-edits` under a policy that blocks commands.

## Gotchas

- `name`/`args` duplicate `rawInput` in every `tool_call` frame. Diff content has `newText` only (no `oldText` for new files).
- Assistant text deltas split paths and links mid-token. Concatenate them before rendering links.
- The first `session/new` takes about 2 s (language-server start). Keep the child alive per open session and close it on idle.
- `agy acp --process-per-session` exists (an isolated worker per session) but was not needed.

## Re-check on 1.5.9 (WP6c adapter, 2026-11-15)

Two real turns in empty scratch folders, plus token-free calls:

- `agy --version` prints `1.5.9`; `initialize` still reports ACP `protocolVersion` 1.
- `session/set_model {sessionId, modelId}` works (answers `{}` and sends a `config_option_update`); `agy models` lists the same ids as `availableModels`, one per line after a `Fetching available models...` line (tab-separated id and name). `agy acp` has no `--mode` or `--model` flag.
- **`plan` mode is not read-only.** Asked to create a file in `plan` mode, agy wrote an implementation plan into its own `brain/<id>/` folder (outside the workspace, without asking under `always-proceed`) and asked in chat whether to proceed; it executes once the chat says so. With in-workspace edits never prompting either, nothing keeps a session read-only, so the adapter offers only `default` and `unrestricted`.
- `user_message_chunk` echoes can carry a leading space (`" Create a file…"`); the adapter strips user text.
- A conversation created by `session/new` with no turn yet is missing from ACP `session/list`; it is in the summaries database after its first turn, as before.
- `session/load` did not change `last_modified_time`, `title` or `preview` of a conversation whose title was already set.
- An `antigravity-oauth-token` file in `~/.gemini/antigravity-cli/` was present early in the re-check and gone during the smoke run while agy stayed signed in, so a sign-in cannot be detected from files (existence is only a positive hint); "not signed in" is only known when `session/new`/`session/load` fails.
