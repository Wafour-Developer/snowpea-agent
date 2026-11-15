# S4 — Cursor agent CLI (`cursor-agent`) spike

Tested 2026-11-15 on Linux. `cursor-agent --version` reports 2026.10.22-27dcf5f, but the launcher ran `versions/2026.11.13-eacade1/index.js` for interactive use. Pin by reading the version from `system/init` instead of trusting `--version`. The spike used six short turns plus `create-chat` and `--list-models`. Fixtures are in `tests/fixtures/external_recordings/cursor/` (raw stdout NDJSON; paths scrubbed to `/tmp/spike-cursor/proj`).

**Recommended transport:** spawn one process per message: `cursor-agent -p --output-format stream-json --stream-partial-output --trust --sandbox enabled --workspace <root> --resume <chatId>` with the prompt as the last argv element. There is no long-lived bidirectional channel and no permission protocol. Read listings and transcripts from disk.

## Verdicts

| # | Capability | Verdict |
|---|---|---|
| 1 | Create a session | Works. `cursor-agent create-chat` prints just the chat id (a UUID) on stdout and spends **no** tokens. Or omit `--resume`; `system/init.session_id` is the new id. |
| 2 | Send and stream | Works as designed: `--resume <chatId>` continues the chat (`resume.jsonl`). |
| 3 | Permissions | **Not possible** as prompts. Print mode stores the chat with `isRunEverything: true`. Edits **and** shell ran without `-f`, and no permission or interaction event is emitted. The degraded mode is described below. |
| 4 | Interrupt | Works differently: there is no in-band cancel. Kill the process group (SIGTERM, then SIGKILL). The transcript keeps whatever was written so far. |
| 5 | List and transcript | Works as designed: `~/.cursor/chats/<md5(workspace)>/<chatId>/meta.json` plus `~/.cursor/projects/<encoded>/agent-transcripts/<chatId>/<chatId>.jsonl`. The transcript is lossy (no tool results), and the title lives in `store.db`. |
| 6 | Open in another process | Works differently. Use a process scan of `/proc/*/fd` for `chats/<hash>/<chatId>/store.db`. A second `-p --resume` while the TUI has the chat open **runs anyway**: there is no lock or refusal. |

## Stream-json events (print mode)

The events below are in order (`oneshot.jsonl`, `write_without_force.jsonl`, `shell_sandbox_without_force.jsonl`):

```
{"type":"system","subtype":"init","apiKeySource":"login","cwd":…,"session_id":<chatId>,"model":"Composer 2.5","permissionMode":"default"}
{"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]},"session_id":…}
{"type":"thinking","subtype":"delta","text":…,"session_id":…,"timestamp_ms":…}   # emitted even without --stream-partial-output
{"type":"thinking","subtype":"completed",…}
{"type":"tool_call","subtype":"started","call_id":…,"tool_call":{"editToolCall"|"shellToolCall"|…:{"args":{…}},"toolCallId":…,"startedAtMs":…},"model_call_id":…,"session_id":…,"timestamp_ms":…}
{"type":"tool_call","subtype":"completed","call_id":…,"tool_call":{"<kind>ToolCall":{"args":{…},"result":{"success":{…}}|{"permissionDenied":{command,error,isReadonly}}}},…}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":…}]},"session_id":…,"timestamp_ms":…}   # deltas when --stream-partial-output
{"type":"assistant","message":{…full text…},"session_id":…}        # final full message, NO timestamp_ms: do not append it twice
{"type":"result","subtype":"success","duration_ms":…,"is_error":false,"result":<full text>,"session_id":…,"request_id":…,"usage":{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}}
```

- **Edit result:** `editToolCall.result.success` has `{path, linesAdded, linesRemoved, diffString (unified), afterFullFileContent}`. Convert it to a diff item.
- **Shell call:** `shellToolCall.args` has `{command, workingDirectory, timeout, requestedSandboxPolicy{type, networkAccess, additionalReadwritePaths}, skipApproval, …}`, and `result.success` has `{exitCode, stdout, stderr, interleavedOutput, executionTime}`.
- **Tool start/end pairing:** `call_id` contains a newline. Pair started and completed events by it, and never put it in a path or regex unescaped.
- **Partial output:** with `--stream-partial-output`, each delta is an `assistant` event with `timestamp_ms`. The closing duplicate has no `timestamp_ms`. Without the flag, only the full message arrives.
- **Models:** `--model <id>`. `--list-models` (no tokens) prints a text table (`auto - Auto (default)`, `gpt-5.3-codex …`). Parse lines of the form `<id> - <name>`.

## Permissions in print mode without `-f`

| Setup | File write in workspace | Shell `echo hi > f` | Network (`curl example.com`) |
|---|---|---|---|
| `--trust --sandbox enabled` | ran, no event | ran inside the sandbox (`TYPE_WORKSPACE_READWRITE`, `networkAccess:false`) | **ran**: the model asked for `networkAccess:true` and got it, with no prompt |
| plus project `.cursor/cli.json` `{"permissions":{"deny":["Shell(*)"]}}` | ran | **blocked**: `result.permissionDenied {"error":"Command blocked by permissions configuration"}`, and the turn continued | blocked (it is a shell call) |

- No `interaction_query` or permission event is emitted, and stdin is not read (the spike ran with `</dev/null` and nothing hung). The chat's `store.db` meta records `"isRunEverything": true` for print-mode chats.
- `--mode plan|ask` is documented as read-only (help: "no edits", "read-only"). It was not exercised here, to stay within the turn budget.
- `--auto-review` ("server classifier auto-runs safe tool calls and prompts for the rest") exists. Print mode has no way to answer its prompts.
- **Safest mode where edits work and shell does not:** `--sandbox enabled --trust` plus a `deny: ["Shell(*)"]` rule. The only place that rule was shown to work is the **project** `.cursor/cli.json`, which means writing into the user's repo. The other place is the owner's global `~/.cursor/cli-config.json` `permissions`, which today allows only `Shell(ls)`. Neither can be set per invocation.
- **Adapter rule:** the default is `--mode ask` (read-only, no edits). When the person enables "edits" for a cursor session, require the project `.cursor/cli.json` shell deny. Offer to write it, ask before writing, and warn that it also affects their own interactive cursor use. Without that rule, a cursor session is "runs everything, sandboxed but network allowed", and the window header must say so. Snowpea approvals cannot gate individual tool calls.

## Listing and transcript

- **Chat dir:** `~/.cursor/chats/<hash>/<chatId>/`, where `hash = md5(absolute workspace path)` as hex (verified). Files: `meta.json`, `store.db` (sqlite; `-wal`/`-shm` while open).
- **`meta.json`:** `{"schemaVersion":1,"createdAtMs","hasConversation","updatedAtMs","cwd"}`. `hasConversation` is presumably false for an empty `create-chat` chat (not checked). Use `updatedAtMs` for sorting and `cwd` for the workspace (`meta.json` fixture).
- **Title:** `store.db` table `meta(key,value)`; key `"0"` holds **hex-encoded JSON** `{agentId, latestRootBlobId, name, createdAt, mode, isRunEverything, blobEncryptionKey}`. `name` is the title ("New Agent" for print-mode chats; the TUI renames). Open it with `?mode=ro`, read `name`/`mode` only, and never log `blobEncryptionKey`. If the title is "New Agent", fall back to the first user query.
- **Transcript:** `~/.cursor/projects/<encoded>/agent-transcripts/<chatId>/<chatId>.jsonl`. `<encoded>` is the absolute path with the leading `/` dropped and every `/` turned into `-`, so `/tmp/a/b` becomes `tmp-a-b`. The encoding is lossy; map back through `meta.json.cwd`. Records (`transcript_sample.jsonl`):

| Record | Snowpea timeline item |
|---|---|
| `{"role":"user","message":{"content":[{"type":"text","text":"<timestamp>…</timestamp>\n<user_query>\n…\n</user_query>"}]}}` | user message (strip the wrappers; the timestamp is local text, not epoch) |
| `{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Write"/"Shell"/…,"input":{…}}]}}` | tool call (no id, **no result record**: show it as completed with no output) |
| `{"role":"assistant","message":{"content":[{"type":"text","text":…}]}}` | assistant text |
| `{"type":"turn_ended","status":"success"}` | turn end |

  There are no thinking records and no per-record timestamps. For live sessions that snowpea drives, render from stream-json and use the transcript only for chats it did not drive.
- **Live tail:** yes. During a turn the file grew in steps (547 → 796 → 1059 → 1133 → 1174 bytes over about 5 s), appended record by record.

## Liveness

- An interactive TUI started with `--resume <chatId>` shows the id in its argv (`node …/index.js --workspace <w> --resume <id>`), but a chat started fresh in the TUI does not. The reliable signal is any cursor-agent `node` process whose `/proc/<pid>/fd` links to `…/chats/<hash>/<chatId>/store.db`. Seeing `store.db-wal` is only a hint; `/proc/locks` shows sqlite locks that any reader takes.
- The first print-mode run spawned a detached `node …/index.js worker-server`. Its cwd was the spike workspace, it was reparented away from the `-p` process, and it outlived the run, so killing the child's process group does not catch it. `proc.py` must find and kill it by cwd or track it, and must not count it as liveness.
- `-p --resume <chatId>` while the TUI had that chat open **succeeded** and appended to the same transcript (`network_sandbox_concurrent_resume.jsonl`). There is no fencing, so the adapter must refuse `send` and offer view-only when another process holds the chat's `store.db`.

## Gotchas

- `--workspace` decides the chat hash. Every run here used `--trust`, so print mode without it in an untrusted workspace was not tested. Running with `--trust` writes `~/.cursor/projects/<encoded>/.workspace-trusted`.
- The model answered in the owner's language (Korean), so do not match on the reply text.
- `cursor-agent persist` (sessions that survive disconnect) and `worker` exist but were not needed.
