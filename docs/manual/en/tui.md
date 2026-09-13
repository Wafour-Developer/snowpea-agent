# The terminal UI

`snowpea` with no arguments opens the terminal UI. It is a thin client: every decision — which tool may run, what a command means, when to compact — belongs to the daemon, and this page is about the surface those decisions come out of.

Other languages: [한국어](../ko/tui.md) · [日本語](../ja/tui.md) · [简体中文](../zh-CN/tui.md) · [Español](../es/tui.md) · [all pages](../README.md)

## The launch screen

The first thing a session prints is the wordmark, sized to your terminal, and then three lines that say where you are:

```text
██████████  ██      ██  ██████████  ██      ██  ██████████  ██████████  ██████████
██          ████    ██  ██      ██  ██      ██  ██      ██  ██          ██      ██
██████████  ██  ██  ██  ██      ██  ██      ██  ██████████  ██████████  ██████████
        ██  ██    ████  ██      ██  ██  ██  ██  ██          ██          ██      ██
        ██  ██      ██  ██      ██  ████  ████  ██          ██          ██      ██
██████████  ██      ██  ██████████  ██      ██  ██          ██████████  ██      ██
🌱 snowpea v0.1.2

          Open-source multi-vendor coding agent and personal AI assistant
        v0.1.2 · anthropic/claude-sonnet-4-5 · /home/you/project · ACCEPT

Last session: 26m ago · "add the worktree parallel session story to the IDE plan"
Press R or type /resume to continue it
```

The last-session block appears when the daemon still has a session open for this directory — a second terminal, a headless run, a session a crash left behind. `R` on an empty input replays it into this window; `/resume` opens the picker over every saved session instead. When the daemon has no such session, the block is not shown, because an offer that cannot be taken is worse than no offer.

The banner is printed once. It scrolls away like any other output and never comes back.

## Saved sessions

The offer on the launch screen is only the newest one. Everything this machine has ever run is still on disk, and `/sessions` opens the picker over it:

```text
╭──────────────────────────────────────────────────────────────────────╮
│ ❯  01J9F2… · 2026-03-14 09:41 · fix the flaky worktree test          │
│    01J9DR… · 2026-03-13 18:02 · add the scheduler reminder story     │
│    01J9C7… · 2026-03-13 11:26 · (no prompt)                          │
│    Cancel                                                            │
│ ↑↓ move · Enter resume · Esc cancel                                  │
╰──────────────────────────────────────────────────────────────────────╯
```

The listing includes closed sessions, not just the ones the daemon still holds open, and each row carries the last prompt that session saw, so a row is identifiable without remembering an id. Rows are newest first, scoped to this directory, and the session you are already in is not offered to you.

| What you type | What it does |
|---|---|
| `/sessions` | open the picker |
| `/resume` | the same picker |
| `/resume <sessionId>` | reopen that session directly, no picker |
| `R` on an empty input | skip the picker and take the newest session in this directory |
| `/session delete <id>` | delete one saved session |
| `/session clear` | delete the saved sessions for this directory |
| `/session clear --all` | delete every saved session on this machine |

Resuming replays the session's history into this window and continues it — the working directory, mode, provider, model and team come back with it, whether or not the daemon still had it open.

Deleting takes the messages, the events and the session row together, and with them the attachments and speech files that session owned, because a thread deleted for what was pasted into it should not leave the paste behind. A live session is never deleted: close it first, and until then `clear` steps over it. Resuming, on the other hand, needs an idle turn — `/resume` and `/sessions` refuse while something is running, because swapping the transcript under a live turn would tear it in half.

## The layout

The UI draws inline, the way `git log` does, not as a full-screen application. Finished output is handed to the terminal, which means your own scrollback, your own mouse, your own `Ctrl+Shift+F`. Only the bottom of the screen is live.

```text
 › explain the retry logic                 ← scrollback: yours, never redrawn
 ⏺ Read 3 files (128 lines)
 ◆ The retry lives in `client.ts`…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← the live region starts here
 > the next thing you type
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

Top to bottom, the bottom panel is: the mode summary row, the status line, the context warning when there is one, then the agent rows. The mode comes first on purpose — it is the line that decides what the next turn is allowed to do, so it sits closest to what you are typing. The input sits above them, with the working indicator above that. Nothing below the input is redrawn unless it changed.

## While it is working

A turn shows one line above the input, and that line says what is actually happening:

| Line | Means |
|---|---|
| `✢ Pondering… (12s · ↓ 3.7k tokens)` | the model is thinking; the verb changes every few seconds |
| `✳ Running shell: npm test… (4s · …)` | a tool is in flight, named and with its argument |
| `✶ 3 agents working… (1m 2s · …)` | the turn has delegated |
| `✳ /ralph… (3m 10s · …)` | a command workflow owns the turn |
| `⏸ Waiting for approval` | it is blocked on you |

The clock counts this turn and the token counts are this turn's, not the session's — the session totals are in the status line. `Esc` interrupts.

When a run of tool calls finishes, it collapses into one line in the scrollback rather than a card each:

```text
⏺ Ran 2 shell commands (34 lines)
⏺ Read 3 files (128 lines)
```

A call that failed keeps its own card, with its output, because that is the one you need to read. `Ctrl+O` expands the newest tool call or diff while it is still live.

## Modes

`Shift+Tab` cycles accept → auto → plan → accept. The mode is in the status line and in the summary row, and [Modes](modes.md) explains what each one permits. `Ctrl+P` toggles plan mode on and off without cycling.

There is also a picker, for when you want a mode rather than the next one. `↓` past the newest history entry moves onto the summary row; `Enter` there opens it:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
❯  accept mode
   auto mode
   plan mode
```

It starts on the mode you are in, `Enter` takes the highlighted one and `Esc` leaves the mode alone. Either way the mode summary row is drawn above the status line, not below it.

## Choosing a model

`/model <ref>` switches the session to a model id or a named profile. `/model` on its own opens a picker rather than printing a list you would have to type back:

```text
╭──────────────────────────────────────────────────────────────╮
│ Model                                                        │
│ ❯ fast    anthropic/claude-haiku-4-5 · used by reviewer       │
│   deep    anthropic/claude-sonnet-4-5 · default               │
│   claude-opus-4-1    anthropic                                │
│ ↑↓ move · Enter pick · Esc cancel                             │
╰──────────────────────────────────────────────────────────────╯
```

The rows are your model profiles first — `models.profiles`, with `models.default` and `agents.models` saying which is the default and which agent uses which — then whatever the vendor's endpoint reports, then the model in use if nothing else named it. `Enter` runs `/model <ref>` for the row you are on. The status line's `Model:` segment names the model in use, and adds where it came from when the daemon says.

## Approvals

When the daemon asks, it asks with a menu. `↑`/`↓` move, `Enter` takes the highlighted row, `Esc` refuses:

```text
╭──────────────────────────────────────────────────────╮
│ Approval required                                    │
│ shell risk=high timeout=300s                         │
│   command: rm -rf build                              │
│                                                      │
│ ❯  Yes   (y)                                         │
│    Yes, and don't ask again this session   (a)       │
│    Yes for this project   (p)                        │
│      adds an allowlist rule the daemon keeps         │
│    No   (n)                                          │
│ ↑↓ move · Enter confirm · Esc cancel                 │
╰──────────────────────────────────────────────────────╯
```

When the daemon has something to warn you about — a command that reaches outside the working directory, a risk the arguments alone do not show — the request carries a `note`, and it is rendered in red above the arguments in both the TUI and the headless CLI:

```text
shell risk=high timeout=300s
  ⚠ this deletes a directory outside the working tree
  command: rm -rf ../build
```

The cursor starts on `Yes`, so `Enter` means yes. `y`, `a`, `p` and `n` still work directly. While the prompt is up it owns the keyboard: nothing you type reaches the draft behind it, and `Shift+Tab` does not change mode.

Approvals raised by a turn with nobody watching — a scheduled job, a Telegram message — queue instead. `Ctrl+R` hands the keyboard to that queue; `/approvals` lists it.

## Questions

An approval is the daemon asking whether it may do something. A **question** is the agent asking *you* something, through the `ask_user` tool, and it is drawn as a picker rather than as "(a) … (b) … (c) …" in the transcript:

```text
╭──────────────────────────────────────────────────────╮
│ Renderer                                             │
│ What should the renderer be? This decides how much   │
│ engine control you keep.                             │
│                                                      │
│ ❯  1. three.js (recommended)                         │
│       fast start, little control over the engine     │
│    2. Raw WebGL2                                     │
│       more work, complete control                    │
│    기타 / Other…                                      │
│ ↑↓ move · Enter choose · 1-9 pick · Esc cancel       │
╰──────────────────────────────────────────────────────╯
```

`↑`/`↓` (or `j`/`k`) move, `1`–`9` jump straight to a row and answer, `Enter` takes the highlighted one. When the question allows several answers, `Space` ticks a row and `Enter` sends the set. The last row, `기타 / Other…`, opens a one-line text input for an answer nobody offered. `Esc` answers "I am not choosing", and the agent is told the question was declined rather than left to read silence as agreement.

A question is drawn ahead of a pending approval and owns the keyboard while it is up. It gives up after ten minutes by default — `questions.timeoutSec` in `settings.json`.

Two lines of it, from a skill:

```text
ask_user(question="Where should the cache live? An in-process cache is simpler; Redis survives a restart.",
         options=[{"label": "In-process (recommended)", "description": "no new service"}, {"label": "Redis", "description": "survives a restart, one more thing to run"}])
```

The argument schema is a superset of Claude Code's `AskUserQuestion` — `questions[]` with `header`, `question`, `options[]` of `{label, description}`, `multiSelect` and an optional `preview` — and of Hermes' `clarify`, whose `choices[]` of plain strings and `multi_select` are accepted too, so a skill ported from either works unchanged. `allow_other` defaults to true and the "Other" row is added for you.

On a messenger binding the same question arrives as numbered text with one button per option plus an "Other" button, and a typed reply of `2` (or `1,3` for a multi-select) answers it. Headless `snowpea -c` has no picker to draw, so it declines the question at once and says so on stderr instead of waiting out the timeout.

## Diffs

A file change appears where it happened, in the conversation:

```text
✎ Edited README.md  (+4 −2)
--- a/README.md
+++ b/README.md
@@ -1,5 +1,7 @@
 # snowpea
-an agent
+an open-source multi-vendor coding agent
… 3 more lines (Ctrl+O)
```

A new file reads `✚ Created notes.md (7 lines)`. Long patches are cut to twelve lines; `Ctrl+O` opens the newest one while it is still in the live region.

## Context

The status line carries how full the model's context window is:

```text
ctx 34% (68k/200k)
```

It is dim until 70%, amber from there, red from 85%, and past 80% a row appears above the summary saying what to do about it:

```text
[!!] context 85% — /compact to free space
```

`/compact [instructions]` summarises the conversation so far and continues from the summary; the optional instructions say what to keep. The daemon also compacts on its own before a turn would pass `context.autoCompactPercent`. Either way the transcript shows where it happened:

```text
───────────── compacted (68.0k → 12.1k tokens) ─────────────
```

`ctx 12.3k used` with no percentage means the daemon does not know this model's window. See [Commands](commands.md) for `snowpea session context` and the `providers.<vendor>.context_window` override.

## Typing

`↑` walks back through your earlier prompts and `↓` walks forward to what you were writing. The history is per machine, not per session: it lives in `$SNOWPEA_HOME/tui-history.jsonl`, keeps the last 500 entries, and does not record the same prompt twice in a row.

`↓` past the newest entry does something else: it moves the cursor out of the input and into the rows below it. First the summary row, where `Enter` opens the mode picker:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to choose mode
```

Then the agent rows, one at a time. `Esc` or `↑` walks back up to the input.

### Sending while it is working

You do not have to wait for a turn to finish. A prompt sent while one is running is accepted and queued rather than refused, and the queue drains first in, first out — one turn at a time against one history, so two provider loops never run over the same conversation. Attachments are captured when you press `Enter`, so a chip queued now is still the file you meant by the time its turn starts. The queue is in memory only; it does not survive a daemon restart.

`Esc` drops the queue along with the running turn. Interrupting means stop what I asked for, and that has to include the follow-ups still waiting, or Stop would be followed by the queue running anyway. Every dropped prompt is reported to clients as `turn.dequeued` with reason `dropped`, followed by its own `turn.done`, so nothing waiting on that turn id is left hanging.

While the queue drains the UI shows it: the working line gains a `⏳ N queued` count, and the prompts themselves are listed under the input, dimmed and numbered in the order they will run.

```text
✽ Noodling… (4s · ↓ 0 tokens) · ⏳ 2 queued
   1. run the tests after this
   2. then read the diff
 > ask anything, or /command
```

A prompt leaves the list the moment its turn starts. `Esc` clears the whole queue and says so once — `2 queued prompts dropped` — rather than one notice per prompt.

### Handing one prompt to an agent

A draft that starts with `$name ` goes to that agent rather than to the main session, and the input says so before you send it:

```text
[delegate to executor]
> $executor review the queue work
```

The names come from the daemon's agent list. A name nothing answers to is still shown, marked `(no such agent)`, because that is worth seeing before `Enter` rather than after.

### Looking inside an agent

`Enter` on an agent row replaces the transcript with that agent's own conversation — the brief it was given, its tool calls, its answer:

```text
╭────────────────────────────────────────────────────────────────────╮
│ ◯ executor · Implement story IDE-004      running · 28s · ↓ 159.1k │
│ › Implement story IDE-004 (Worktree parallel sessions)             │
│ ✓ read_file path=docs/stories/IDE-004.md (1 lines)                 │
│ ◆ Added the worktree manager and its tests; 3 files changed.       │
│ ↑↓ PgUp/PgDn scroll · Esc back to the main transcript              │
╰────────────────────────────────────────────────────────────────────╯
```

Each delegate runs in a session of its own, and this view is that session, replayed and then followed live. `Esc`, or `Enter` on `● main`, comes back. `Ctrl+A` opens the panel out past its collapsing rules, so idle agents and anything hidden behind `↓ N more` are listed.

## Updates

When a newer release exists the status line says so, and `U` on an empty input — or `/update` — opens the confirmation. The upgrade runs, the daemon restarts, and the UI comes back on the new version.

```text
snowpea v0.1.2 → v0.1.3 (U to update)
```

## Attachments

Paste or drop a file path into the input and it becomes a chip instead of text:

```text
[📎 screenshot.png 1.2MB] (backspace removes the last · Ctrl+X clears)
> what is wrong with this layout?
```

It understands what a terminal actually delivers: one path, several at once, a path whose spaces are escaped, a path whose spaces are not, a `file://` URL, quotes a file manager added. `Ctrl+V` takes an image straight off the system clipboard — `wl-paste`, `xclip`, AppleScript or PowerShell, whichever this machine has — and saves it under `$SNOWPEA_HOME/tmp/`. `/attach <path>` does it by hand.

`Backspace` on an empty input removes the newest chip and `Ctrl+X` removes them all. Sending the prompt sends the files with it, and the transcript entry says which:

```text
› what is wrong with this layout?
  📎 screenshot.png
```

Files are sent as paths, so nothing is copied. What the model then does with them — and the 20MB limit, the downscaling, what happens on a model that cannot see — is in [Attachments and voice](voice.md).

## Voice

Voice needs a backend, and the daemon is the one that has them. `snowpea setup audio` configures them; [Attachments and voice](voice.md) lists what each one needs.

| Key or command | What it does |
|---|---|
| `/voice` | arm voice input |
| `Ctrl+Space`, or `/rec` | start recording; again to stop |
| `/tts on`, `/tts off` | speak each reply as it finishes |
| `Esc` | stop a reply that is being read out |

While recording, the indicator slot counts up:

```text
● REC 00:07
```

Stopping transcribes and puts the text in the draft rather than sending it, because speech recognition is wrong often enough to need reading first. Recording happens on the daemon when the daemon has a microphone, and on this machine when it does not.

`🔊` in the status line means replies are being spoken. When a capability is missing the command says so in the daemon's own words rather than doing nothing:

```text
voice input needs speech-to-text: no transcription backend: install the whisper CLI, set an OpenAI API key, or …
```

## Full screen

`--fullscreen` switches to an alternate-buffer layout: the transcript is a window the UI scrolls itself with `PgUp`/`PgDn` and `Ctrl+U`/`Ctrl+D`, and nothing is left in your scrollback when you quit.

```bash
snowpea --fullscreen
```

It costs less bandwidth over a slow link, because only the rows that changed are redrawn. It also takes your terminal's scrollback away for the session, which is why it is not the default.

## Keys

| Key | What it does |
|---|---|
| `Enter` | send, or confirm the highlighted option |
| `Shift+Tab` | cycle mode |
| `Ctrl+P` | toggle plan mode |
| `↑` / `↓` | earlier prompts; `↓` past the newest moves into the panel |
| `Esc` | interrupt the turn, stop speech, or leave an agent view |
| `Ctrl+O` | expand the newest tool call or diff |
| `Ctrl+A` | open the agent panel out |
| `Ctrl+R` | focus the unattended approval queue |
| `Ctrl+V` | attach an image from the clipboard |
| `Ctrl+X` | clear the attachments |
| `Ctrl+Space` | start or stop recording |
| `U` | take the offered update |
| `R` | resume the session the launch screen offered |
| `F1` | help |
| `Ctrl+C` | quit |

`/help` lists every command the daemon has, including the ones your plugins added, and repeats this table.

## Markdown tables

Markdown tables in responses are rendered with aligned borders. Column sizing accounts for Korean, CJK and emoji terminal widths, and long paths or sentences wrap inside their cells. When there are too many columns to fit, values are stacked under their column labels instead. Inline and full-screen views use the same renderer; table source inside code fences stays literal.

## Built-in and custom agents

`/agent list` includes the packaged roles (`architect`, `critic`, `executor`, `explorer`, `test-engineer`, `verifier`) alongside custom definitions. Built-in roles need no user-created files and have source `builtin`. A custom definition with the same name overrides the built-in; project definitions take precedence over global definitions. The same names are available through the `agent` argument of `delegate_task`.

## Update notifications at startup

Each launch checks for updates in the background. `/update` always bypasses an older negative cache and checks again; when nothing newer exists it reports that the build is already up to date rather than showing an installation failure. When a banner appears, press `U` on an empty input or type `/update` to open confirmation. Choose `y` to install and restart, or `n`/Esc to defer. Lowercase `u` in ordinary typing is not an update shortcut.

Git `main`/`master` installs compare the installed commit, so a version bump is not required to detect new commits. Failed checks, unchanged builds and downgrades do not trigger installation. PyPI/release installs retain version-based checks.

If an older automatic update leaves startup failing with `Cannot read properties of undefined (reading 'rawCall')`, reinstall current `main`:

```sh
uv tool install --force --reinstall 'snowpea-agent[images] @ git+https://github.com/Wafour-Developer/snowpea-agent@main'
```

After reinstalling, once no work is running, use `snowpea daemon stop` and then launch `snowpea` to load the new daemon code.

Help stays within the terminal height. Scroll with `↑`/`↓` or `PgUp`/`PgDn`; close with **Esc, F1, q, or Enter**. Esc inside help does not interrupt the running turn.

The input, connection/model status, mode summary, and agent list at the bottom are separated by rules spanning the full terminal content width, making the active region easy to distinguish.

## Teams and short delegation

The first `snowpea setup` creates a `default` team from the built-in roles. `/team create delivery architect executor verifier` creates a project team from existing agents, activates it immediately, and rejects unknown names. Manage it with `/team list`, `/team use <name>`, and `/team delete <name>`. With a team active, the footer shows its name and members only, and automatic delegation is confined to that roster.

Type `$executor fix the tests` (or `/delegate executor fix the tests`) to delegate directly without a long command. Both run an ordinary turn: the daemon parses the prefix itself, so this behaves the same from any client, and the turn calls `delegate_task`, waits for the child, and answers with what it reported — not silence once the child finishes. Unknown or out-of-team names fail instead of silently becoming a generic agent. Internal delegation that omits a name deterministically uses `executor` when present, otherwise the first team member.
