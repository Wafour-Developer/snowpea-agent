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

The last-session block appears when the daemon still has a session open for this directory — a second terminal, a headless run, a session a crash left behind. `R` on an empty input, or `/resume`, replays it into this window. When the daemon has no such session, the block is not shown, because an offer that cannot be taken is worse than no offer.

The banner is printed once. It scrolls away like any other output and never comes back.

## The layout

The UI draws inline, the way `git log` does, not as a full-screen application. Finished output is handed to the terminal, which means your own scrollback, your own mouse, your own `Ctrl+Shift+F`. Only the bottom of the screen is live.

```text
 › explain the retry logic                 ← scrollback: yours, never redrawn
 ⏺ Read 3 files (128 lines)
 ◆ The retry lives in `client.ts`…

 ✢ Pondering… (12s · ↓ 3.7k tokens)       ← the live region starts here
 > the next thing you type
 snowpea v0.1.2 | ~/project | Model: anthropic/claude-sonnet-4-5 | Mode: ACCEPT | ctx 34% (68k/200k)
 [!!] context 85% — /compact to free space
 ⏵⏵ auto mode on · 1 shell · ← 2 agents
 ● main
 ✳ executor         Implement story IDE-004               running · 27s · ↓ 159.1k tokens
 ◯ 4 idle agents
```

Top to bottom, the bottom panel is: the status line, the context warning when there is one, the summary row, then the agent rows. The input sits above them, with the working indicator above that. Nothing below the input is redrawn unless it changed.

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

The cursor starts on `Yes`, so `Enter` means yes. `y`, `a`, `p` and `n` still work directly. While the prompt is up it owns the keyboard: nothing you type reaches the draft behind it, and `Shift+Tab` does not change mode.

Approvals raised by a turn with nobody watching — a scheduled job, a Telegram message — queue instead. `Ctrl+R` hands the keyboard to that queue; `/approvals` lists it.

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

`↓` past the newest entry does something else: it moves the cursor out of the input and into the rows below it. First the summary row, where `Enter` lists what is running:

```text
⏵⏵ auto mode on · 1 shell · ← 2 agents · Enter to list them
    ◦ Ran shell: npm -w tui test · 12s
```

Then the agent rows, one at a time. `Esc` or `↑` walks back up to the input.

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

Each launch checks for updates in the background. When a banner appears, press `U` on an empty input or type `/update` to open confirmation. Choose `y` to install and restart, or `n`/Esc to defer. Lowercase `u` in ordinary typing is not an update shortcut.

Git `main`/`master` installs compare the installed commit, so a version bump is not required to detect new commits. Failed checks, unchanged builds and downgrades do not trigger installation. PyPI/release installs retain version-based checks.

If an older automatic update leaves startup failing with `Cannot read properties of undefined (reading 'rawCall')`, reinstall current `main`:

```sh
uv tool install --force --reinstall 'snowpea-agent[images] @ git+https://github.com/Wafour-Developer/snowpea-agent@main'
```

After reinstalling, once no work is running, use `snowpea daemon stop` and then launch `snowpea` to load the new daemon code.
