# M19 spike S5: shared terminal mode through tmux

Tested on 2026-11-15 on Linux with **tmux 3.2a** against `claude` 2.1.325, `codex` 0.167.1, `cursor-agent` 2026.11.13-eacade1 and `agy` 1.5.9. Every CLI ran its normal interactive UI in a pane on a dedicated server (`tmux -L snowpea-spike`), in a throwaway git repo, with at most three short turns per CLI. No bypass flags were used. The server was killed at the end.

The idea: run the CLI's own UI in tmux. Then there is one process. The person can `tmux attach` from any terminal, and the daemon types into the same process with `send-keys` while the chat view tails the CLI's transcript file. That would lift the "a session open in a terminal is view-only" limit (Part 2 §A.4, S1 §Liveness) for sessions that live in tmux.

Fixtures (scrubbed) are in `tests/fixtures/external_recordings/tmux/`: pane captures named `<cli>_NN_<moment>.txt`, `sharing_attach.txt` (second client test), `claude_sessions_pid_waiting.json` (Claude's status file during a permission dialog) and `commands.sh` (the exact command sequences). Placeholders: `<PROJ>`, `<HOME>`, `<EMAIL>`, `<SOCKET>`, `<PROCSTART>`, `<PIDDOMAIN>`.

## Verdicts

Legend: **OK** works reliably · **caveat** works with the caveats listed · **no** not reliable · **untested**.

| Capability | Claude Code | Codex | Cursor (`cursor-agent`) | Antigravity (`agy`) |
|---|---|---|---|---|
| Start with a known session id | **OK** `--session-id <uuid>` | **caveat** no flag; the id (uuidv7) is only known once the rollout file appears at the first turn | **OK** `cursor-agent create-chat` prints an id (no model call), then `--resume <id>` | **caveat** no flag; the id appears at the first turn (`brain/<id>/`, `presence/<id>.lock`) |
| First start in a new folder | **caveat** trust dialog, default is "No, exit" | **caveat** trust dialog, default "Trust" | **caveat** trust dialog, key `a` | **caveat** trust dialog, default "Yes" |
| Ready test without screen text | **OK** `~/.claude/sessions/<pid>.json` `status: idle` (file appears ~0.8 s after trust) | **no** screen only (`›` input line, no `esc to interrupt`) | **no** screen only (`→ Add a follow-up` / `→ Plan, search, build anything`) | **no** screen only (`? for shortcuts` vs `esc to cancel`) |
| Send a prompt | **caveat** see Input | **caveat** see Input | **caveat** (only single-line and multi-line paste tested) | **caveat** (only single-line and multi-line paste tested) |
| Tail the transcript | **OK** jsonl appended per message | **OK** rollout jsonl appended per item | **caveat** SQLite `store.db`, mixed JSON and protobuf blobs | **OK** `transcript.jsonl` appended per step |
| Turn end | **OK** `status: idle` and transcript `system/turn_duration` | **OK** rollout `event_msg/task_complete` | **no** no end record found; screen spinner only | **caveat** no explicit end record; last `PLANNER_RESPONSE` without tool calls, or screen |
| Knows a permission is pending, and what | **OK** `status: waiting` + the last `tool_use` in the transcript without a `tool_result` (name + full input) | **no** nothing in the rollout; the ask is screen-only, and the `custom_tool_call` holds a JS `exec` script, not the command | **untested** (owner config `approvalMode: unrestricted`, nothing prompted) | **untested** (Auto/accept-edits approved the write and `touch`) |
| Answer a permission with keys | **caveat** `1` = Yes once, `Esc` = cancel; other numbers vary by tool | **caveat** `y` = once, `esc` = no (screen-only) | **untested** | **untested** |
| Interrupt a turn | **untested** (Esc per the UI; `C-c` twice quits) | **untested** (`esc to interrupt` shown while working) | **untested** | **untested** (`esc to cancel` shown) |
| Text sent while a turn runs | **OK** queued, injected at the next tool boundary of the same turn | **OK** Enter steers into the running turn; Tab queues | **untested** | **untested** |
| Adopt an already running pane | **OK** status file has `tmux: "<session>:@<win>.%<pane>"`; socket from `/proc/<pid>/environ` `TMUX` | **caveat** tty/env match only | **caveat** tty/env match only | **caveat** tty/env match only |
| Second client attached | **caveat** see Sharing (same for all, tested with Claude) | same | same | same |

## Recommendation

**Ship it as an additional "shared terminal" transport for Claude Code only, beta, Linux and macOS.** Keep the structured transport (S1) as the default, and offer "open in shared terminal" as a choice per session. Restrictions:

1. **Approvals are answered by the person by default**, in the terminal or with a "send key" control in snowpea that shows the pending tool call from the transcript. A one-press **Allow once / Deny** bridge is safe enough for Claude only, guarded as in the sequence below. Jarvis never auto-decides in tmux mode, and grants ("always") are never sent as keys.
2. **Refuse prompts whose first non-space character is `/` or `!`** (both CLIs switch mode on them, also inside a bracketed paste). Use the structured transport for slash commands.
3. **The daemon types only when the input box is empty and no client is attached, or when the person pressed Send in snowpea.** Typing from both sides goes into the same input line.
4. **No Windows** (tmux needs WSL, where the CLIs, their logins and their transcripts are a separate install).

**Codex: not yet.** Sending, tailing and turn end work, but readiness and the permission ask are screen-only, the ask's "Reason:" line is written by the model (it came out in Korean), and the id is only known after the first turn. Revisit when Codex writes approval requests to the rollout or a hook can report them. `codex queue --thread <id> --message <text>` exists and may be a cleaner way to deliver a message to a running session; it was not tested. **Cursor and agy: do not ship.** Their permission prompts could not be reached with the owner's settings, there is no status file, and Cursor's transcript is a binary store.

## Adapter sequence (Claude)

```
SOCK=snowpea                                   # dedicated server: tmux -L snowpea
# start
tmux -L $SOCK new-session -d -s sp-<short id> -x 200 -y 50 -c <proj> 'claude --session-id <uuid> [--model ...]'
#   pane_pid == claude pid (tmux runs a single command without a shell in between)
#   wait for ~/.claude/sessions/<pane_pid>.json with sessionId == <uuid>, status == "idle"
#   if it does not appear within ~3 s, capture-pane: a trust dialog ("Quick safety check") is the
#   usual cause; show it to the person (default answer is "No, exit"), never answer it blind
# send (single line)
tmux -L $SOCK send-keys -t <s> -l '<text>' ; tmux -L $SOCK send-keys -t <s> Enter
# send (multi-line, Korean, any length): bracketed paste
printf '%s' "<text>" | tmux -L $SOCK load-buffer -b sp-<id> - ; tmux -L $SOCK paste-buffer -p -d -b sp-<id> -t <s> ; tmux -L $SOCK send-keys -t <s> Enter
# watch: status busy within 0.5 s of Enter; idle at the end; waiting = permission dialog
# tail:  ~/.claude/projects/<enc>/<uuid>.jsonl (created at the first prompt, not at start)
# approve once:  only if status == waiting AND the newest assistant tool_use has no tool_result AND
#                capture-pane shows "Do you want" and "1. Yes" → send-keys 1
# deny:          same guard → send-keys Escape   (the model then sees a rejected tool call)
# interrupt:     send-keys Escape while status == busy (not exercised); never C-c
# stop:          tmux -L $SOCK kill-session -t <s>
```

## Findings

### 1. Start

- Every CLI shows a **trust dialog** on first use in a folder. Claude's default choice is "No, exit", so an Enter quits it; Codex and agy default to trust; Cursor wants `a`. The daemon cannot treat a new folder as ready. It must show the dialog to the person or start only in folders the CLI already trusts.
- Claude: the status file appeared 0.8 s after the trust answer (`status: idle`, `kind: interactive`, `entrypoint: cli`, `tmux: "cl:@0.%0"`). The transcript file is created at the **first prompt**, not at start. The UI was ready at the same moment as the file.
- Codex: shows an "Update available" box first; no session file until the first turn. The rollout name is `rollout-<start time>-<uuidv7>.jsonl`. To find it, the adapter must watch for a new rollout whose `session_meta` `cwd` is the project, which races with other Codex sessions in the same folder. The process does not keep the rollout file open, so `/proc/<pid>/fd` does not help.
- Cursor: `create-chat` gives the id up front. Then `~/.cursor/chats/<hash>/<id>/{store.db,meta.json}` exist.
- agy: shows the **account email** in its banner (scrub captures). It says "Sign-in update needed" on this machine and still answered. Files appear at the first turn: `brain/<id>/.system_generated/logs/transcript.jsonl`, `conversations/<id>.db`, `presence/<id>.lock` (empty file).

### 2. Input

- `send-keys -l` then `Enter` works for one line on all four. A newline sent with `send-keys` would submit early, so multi-line text must go through **`load-buffer` + `paste-buffer -p`** (bracketed paste). That kept a two-line prompt with Korean intact on all four (transcripts show `\n` and the Korean exactly).
- Prefixes (`claude_04_prefixes.txt`, `codex_08_prefixes.txt`): `/` opens the command menu (both). `!` switches to shell mode (both), **also when pasted** (Codex shows `! ls` after a bracketed paste of `!ls`). `$` opens Codex's skill picker. `#` is plain text in both now. A leading space displays as literal text, but whether it is then submitted as a prompt was not tested. Refusing these prompts is the safe rule.
- Long prompts: not tested. Claude is known to collapse large pastes into a "[Pasted text]" chip, which still submits the full text.
- While a turn runs: Claude **queues** the text (`queue-operation enqueue`, then `remove` when it is injected at the next tool boundary). The model answered the queued text inside the same turn, and its summary of the file write never came. Codex **steers** on Enter (a `UserMessage` lands in the running turn, one `task_complete`), and offers Tab to queue instead. Either way nothing was lost.
- **Never type while a dialog is open.** Keys go to the dialog: a digit or `y` would answer it. Check `status != waiting` (Claude) before sending text.

### 3. Output

- Claude: one transcript line per message while the UI runs. User records carry `promptSource: "typed"`, `turnOrigin: "human"` (the same values whether the daemon or the person typed, so they cannot tell the author apart). The turn ends with `system/stop_hook_summary` and `system/turn_duration`. Render rules from S1 apply (skip `attachment`, `session-owner`, `api-request*`, which hold the system prompt and account data).
- Codex: rollout items are appended as they complete: `task_started`, `response_item/message` (user, assistant), `custom_tool_call` (the code-mode `exec` script) and its output, `item_completed`, `task_complete {last_agent_message}`. The first turn also writes the whole system prompt and AGENTS.md as `response_item/message` records, so skip developer-role records.
- Cursor: `store.db` (tables `blobs`, `meta`) gains rows during the turn. The user and assistant messages are JSON blobs (`{"role","content"}`, the user text wrapped in `<user_query>`), the rest are protobuf (tool calls, assistant text, the tree). `meta` key `0` is hex-encoded JSON with `latestRootBlobId` and the chat `name`. Tailing means polling a read-only SQLite connection and decoding protobuf. That is too brittle to ship without a schema.
- agy: `transcript.jsonl` lines `{step_index, source: USER_EXPLICIT|MODEL, type: USER_INPUT|PLANNER_RESPONSE|GENERIC, status, content, tool_calls}`. The user text is wrapped in `<USER_REQUEST>` plus metadata.

### 4. Permission prompts

- **Claude** (`claude_06_permission_dialog.txt`): after switching to manual mode with Shift+Tab (the owner's default is **auto**, where nothing prompts), the Write dialog shows "Do you want to create note.txt? 1. Yes / 2. Yes, and switch to accept edits … / 3. No". While it is open, the status file says `status: "waiting"` (`claude_sessions_pid_waiting.json`), and the transcript already holds the assistant `tool_use {name: "Write", input: {file_path, content}}`. So the daemon knows **that** it waits and **what** is asked without reading the screen. The keys come from the screen: option 2 differs by tool (Bash offers "don't ask again for …"), so only `1` (Yes once) and `Esc` are safe to bridge. `1` produced `permissionDecision {decision: accept, source: user_temporary, grantScope: once}`. The permission mode lives only on screen and in transcript `permission-mode` records, so the adapter cannot assume it.
- **Codex** (`codex_05_permission_dialog.txt`, `-s read-only -a on-request`): "Would you like to run the following command? / Environment / Reason: <model text> / $ printf 'hi' > note.txt / 1. Yes, proceed (y) / 2. Yes, and don't ask again … (p) / 3. No … (esc)". Nothing about the request or the answer reaches the rollout. Screen parsing works but rests on English UI strings plus a model-written line, so treat it as person-only.
- **Cursor**: the owner's `~/.cursor/cli-config.json` has `approvalMode: "unrestricted"` and sandbox disabled, so the file write and `touch` ran with no prompt (`cursor_05`, `cursor_06`). **agy**: Auto mode approves edits and "safe" commands; `touch` also ran unprompted in the unlabelled mode after Plan. Neither prompt was reached, so both are untested.
- **Verdict:** approvals can be bridged for **Claude only**, as Allow once / Deny, behind the guard in the adapter sequence. For every other CLI, the person answers in the terminal or through a "send key" control.

### 5. Adoption of an existing pane

- When the command is passed to `new-session`, `pane_pid` is the CLI pid. When a person runs the CLI from a shell in a pane, `pane_pid` is the shell, so match on **tty** (`ps -o tty= -p <cli pid>` against `#{pane_tty}`) or on the process ancestry.
- The CLI's environment names the server: `TMUX=/tmp/tmux-1000/snowpea-spike,<server pid>,<session index>` and `TMUX_PANE=%0`. Read it from `/proc/<pid>/environ` on Linux. On macOS, reading another process's environment is restricted, so try the default socket (`$TMUX_TMPDIR` or `/tmp/tmux-$UID/default`) and match ttys. Servers started with other `-L`/`-S` sockets are only found through the environment.
- Claude helps more: its status file stores `tmux: "<session>:@<window id>.%<pane id>"`. With the socket from `TMUX`, that is a direct address for `send-keys -t %<pane id>`.
- Tested on the spike server only. The default server was not touched. `list-panes -a -F '#{pane_pid} #{pane_tty} #{session_name} #{window_index}.#{pane_index} #{pane_current_command} #{pane_id}'` returned `<pid> /dev/pts/28 cl 0.0 claude %0`.
- Adopting someone's pane means typing into their terminal. Ask the person once per pane, and keep restriction 3.

### 6. Sharing

- Attaching a second client from a 100×30 pty (`sharing_attach.txt`) shrank the window to 100×29 (`window-size latest`; one row for the status line), and it **stayed 100×29 after detach**. The daemon should `resize-window -x 200 -y 50` after the last client leaves (it switches that window to `manual` sizing) or set `window-size` explicitly on its own server. Claude redrew correctly at both sizes.
- Daemon text appeared in the person's terminal (verified on the pane; the raw pty stream is redraw escapes and was not matched literally).
- Both typing at once: the person's keys and the daemon's keys landed in one input line, `❯ typed by daemon and person`. There is no lock in tmux. `list-clients` tells the daemon someone is attached, and `#{client_activity}` tells when they last typed. Use those for the Part 1 hand-back rule (pause on person input, resume after `jarvis.quietMinutes`).

### 7. Lifecycle

- The tmux server is its own daemon (its parent here is the user's systemd, not the starter), so the session survives the snowpea daemon. **Untested risk:** if the daemon runs as a systemd service and starts the tmux server inside its own cgroup, stopping the service kills the server too. Start it with `systemd-run --user --scope tmux -L snowpea …` or equivalent, and check on macOS launchd.
- `kill-server` (or `kill-session`) ended all four CLIs. Claude's own session-end hooks still ran after the hang-up, so user hooks see a normal end.
- Interrupt was not exercised, to stay within three turns. The UIs advertise Esc (Codex "esc to interrupt", agy "esc to cancel"). Claude quits on a double `C-c`, so the adapter must never send `C-c`.

### 8. Platform

- Linux: tmux in every distribution. macOS: Homebrew or MacPorts (not preinstalled). Windows: no native tmux; WSL only, and a CLI installed in WSL is a separate install with its own login and transcript folder.
- Features used: `new-session -x/-y -c`, `send-keys -l`, `load-buffer -`, `paste-buffer -p -d`, `capture-pane -p`, `list-panes -a -F`, `resize-window` and `window-size`. `resize-window` and `window-size` need **tmux ≥ 2.9**. Require 2.9 or later and check `tmux -V` at enable time. Tested on 3.2a.

## Failure modes

1. Trust dialog on a new folder blocks start; Claude's default answer quits.
2. Owner defaults hide prompts (Claude auto mode, Cursor unrestricted, agy Auto). Tmux mode inherits whatever mode the person last chose, and Shift+Tab state is visible only on screen.
3. Text typed while a dialog is open answers the dialog.
4. `/`, `!` (and `$` in Codex) switch mode even inside a bracketed paste.
5. Person and daemon typing interleave in one input line.
6. Window size stays at the last client's size after detach.
7. Codex session id unknown until the first turn; the rollout search can pick another session in the same folder.
8. Screen markers (`Do you want`, `Would you like to run`, `esc to interrupt`, `Add a follow-up`) are UI strings that change between versions and with locale. Use them only as a second check, never as the only signal.
9. The daemon's service manager can kill a tmux server started in its cgroup.
10. Transcript records cannot tell a daemon-typed prompt from a person-typed one.

## Untested

Interrupt on all four; long prompts; Claude leading-space neutralisation; Cursor and agy permission dialogs, mid-turn input and prefixes; adoption on the default server and on macOS; Codex `queue` subcommand and Codex hooks as an approval signal.
