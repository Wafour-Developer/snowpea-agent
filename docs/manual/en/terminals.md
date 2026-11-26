# Terminal sessions (beta)

A terminal session is a real program running in a terminal inside snowpea: a shell, the snowpea CLI, or one of the external CLIs (Claude Code, Codex, agy, Cursor agent). You see its screen, type into it, and snowpea keeps track of whether it is working, waiting for you, or done.

This feature is in beta. The screen-reading rules for each CLI follow what the CLI prints, so a new CLI version can change what snowpea sees. If a status looks wrong, the terminal itself is always the truth.

Other languages: [한국어](../ko/terminals.md) · [all pages](../README.md)

Related: [External sessions (beta)](external-sessions.md) · [Jarvis mode (beta)](jarvis.md) · [CLI hook integrations](integrations.md) · [snowpea web](web.md) · [Terminal UI](tui.md)

## What a terminal session is

- The daemon starts one small holder process per terminal. The holder owns the program's pseudo-terminal, keeps the last 1 MiB of output, and records the exit code.
- Terminals are opened from the desktop app, from snowpea web, or by Jarvis. The `snowpea term` command lists, reads, types into and attaches to terminals that already exist.
- At most `terminals.max` terminals run at once (default 16, allowed 1 to 64).
- Profiles: `shell`, `snowpea`, `claude`, `codex`, `agy`, `cursor`.

## External CLI sessions as terminals

An external session always opens as a terminal, on this computer and on a [remote machine](machines.md): the CLI runs in a pane. There is no chat view for external sessions. This holds for every CLI and every operating system.

- A new session starts the CLI in the project folder you chose.
- Opening an existing session resumes it in a snowpea terminal, with the CLI's own resume command (`claude --resume <id>`, `codex resume <id>`, and the matching agy and Cursor commands). The session id comes from the hook integration, so run [`snowpea integration install`](integrations.md) first if you want resume to work.
- A session that is live somewhere else is resumed anyway, in its own pane; the CLI handles the rest.

In the app, the project's **+** menu → **New external session…** opens a dialog with **Start new session** first, then the project's sessions, newest first. Choosing a session resumes it in a terminal. Pick the permission option before you start ([below](#permission-options)): a dangerous one shows an inline warning, and there is no separate confirm dialog.

The CLI runs inside your own interactive shell, so your PATH and tools such as nvm still apply. When the CLI exits, the pane ends and the IDE returns to its empty state (exit code 0 is normal; anything else is an error).

## A snowpea session in a terminal

Every snowpea session can be shown two ways: as the chat, or as the snowpea CLI running in a terminal attached to the same session. Both views are clients of the same session, so you can have both open at once. In the app, the conversation header has a **Terminal** tab next to **Chat** and **Trajectory** (only with a daemon that supports it). The tab reuses the session's running terminal, or starts one.

- On this computer the default view is `ui.sessionView`: `chat` (the default) or `terminal`. It is kept in this daemon's settings, so every app window and web page connected to it opens sessions the same way. Change it in **Settings → Terminal**.
- On a remote machine the default is `ui.remoteSessionView` (also `chat` by default), and a machine can override it. The first time you open a snowpea session on a machine with no setting, snowpea asks chat or terminal and remembers the answer. See [Remote machines](machines.md#how-a-session-opens).
- To attach from a shell to a session that already exists in the local daemon, run `snowpea --session <id>`. The TUI resumes that session instead of creating a new one.

## Settings

| Key | Default | What it does |
|---|---|---|
| `ui.sessionView` | `chat` | the default view of a snowpea session on this computer |
| `ui.remoteSessionView` | `chat` | the default view of a snowpea session on a remote machine |
| `terminals.max` | `16` | most terminals at once (1 to 64) |
| `terminals.keepShell` | `true` | after the snowpea TUI exits, drop back to the shell instead of closing the pane (shell and snowpea profiles only) |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | the size a terminal uses while no viewer is attached |
| `terminals.sounds.blocked` | `true` | sound and toast when a terminal needs you |
| `terminals.sounds.done` | `true` | sound and toast when a terminal finishes and you are not looking |

The `terminals.*` and `ui.*` settings belong to the person on the local machine. A remote or tunnel connection cannot change them.

In the app, **Settings → Terminal** sets the two default views, `terminals.keepShell` and the two alerts. Each alert switch turns its sound and its notice on or off together; **Play the sound** lets you hear it. The sounds are two short tones the app makes itself.

## Status: what the dots mean

Each terminal has one of five states, and the app shows them as a dot:

| Dot | State | Meaning |
|---|---|---|
| needs you | `blocked` or `error` | the program waits for an answer (a permission prompt or a question) or failed |
| done | `done` | a turn finished and nobody has looked at the terminal since (`seen` is false) |
| working | `working` | a turn is running |
| idle | `idle` | nothing is running |

A blocked state also has a kind: `permission`, `question` or `auth`. The sidebar rolls a project's dots up in this order: needs you, done, working, idle.

In the app the dots are red with a ring (needs you), green (done), the accent colour and pulsing (working; steady when your system asks for reduced motion) and grey (idle). They appear on the terminal tabs, on an external session's row in the sidebar, on a conversation's row while its terminal is open, on the project's row (the rollup) and in the Jarvis view's **Terminals** tab.

- Clicking into a terminal marks it seen, so a done dot turns grey.
- When a terminal starts needing you, the app plays a sound and shows a notice. When one finishes, it does the same only if that terminal's pane does not have the keyboard or the window is in the background. Alerts come from the main window only.
- **Alt+↑ / Alt+↓** jumps to the previous or next terminal that needs you, or that finished and nobody has looked at yet. An external session opens in its own view; any other terminal opens on the Terminal screen.

snowpea takes the state from the first source that has an opinion, highest first:

1. the program itself (the snowpea TUI reports its own status);
2. a permission or question visible on the screen, read by the screen rules for that CLI;
3. the CLI's hook reports ([integrations](integrations.md));
4. the screen rules alone.

## Permission options

When you start a CLI in a terminal you can pick its permission mode. The options come from the CLI's own modes:

| CLI | Options offered | Dangerous (red, confirm first) |
|---|---|---|
| Claude Code | Accept edits, Plan, Auto, Don't ask | Bypass permissions (`--dangerously-skip-permissions`) |
| Codex | Read only, Default | Full access (`--dangerously-bypass-approvals-and-sandbox`) |
| agy | Accept edits, Auto, Plan | Skip permissions (`--dangerously-skip-permissions`) |
| Cursor agent | Ask, Plan | Run everything (`--yolo`) |

In the app you pick at most one option: the CLI takes one permission mode. An option the daemon refuses on this connection is shown disabled, with the reason, and over the tunnel with **Unlock…**.

A dangerous option:

- shows a red badge everywhere it appears and an inline warning in the start dialog. There is no separate confirm dialog, so read the warning before you pick it;
- is refused when Jarvis or any tool asks for it, whatever the settings;
- is refused while `web.remote.maxMode` is below `auto`;
- is refused over the web tunnel unless the terminal is unlocked ([snowpea web](web.md#a-public-address-with-a-tunnel)).

Jarvis never answers a permission prompt in a terminal that was started with a dangerous option. Non-dangerous modes such as Accept edits or Don't ask are also granted only to the person at the local app, not to Jarvis. Jarvis starts CLI terminals only with the CLI's default permissions.

## In the app

- **The Terminal screen and the dock's terminal tab.** In snowpea web they show the daemon's terminals, with **New shell** for a project folder. The desktop app also keeps its own shells: a switch picks **This computer** or **snowpea terminals**.
- **The strip above a terminal** shows its dot and title, a red **Permissions skipped** badge for a terminal started with a dangerous option, **Exited (code)** once it ended, and **Close terminal**, which asks first because it ends the program. Closing a pane or a window does not end the terminal.
- **Pop-out** (desktop app): the button on the strip opens just that terminal in a window of its own. Every window shows the same terminal, and closing one leaves the others running.
- **Watch-only.** When the daemon lets a page only watch a terminal, the pane stops sending keys, shows a **Watching** badge and says it is watch-only.
- **Over the web tunnel**, a pane that is not unlocked shows **Unlock…**, which asks for the web password once for this sign-in ([snowpea web](web.md#a-public-address-with-a-tunnel)).

## Terminals survive a daemon restart

Because the holder runs on its own, a terminal keeps running when the daemon restarts or upgrades. When the daemon starts again, it reconnects to every live holder and redraws each screen from the holder's recent output. A terminal whose holder has died (for example after a reboot) is listed as lost, and you can resume its CLI session.

```bash
snowpea daemon stop                    # the daemon stops; terminals keep running
snowpea daemon stop --and-terminals    # the daemon and every terminal session end
```

## The `snowpea term` command

```bash
snowpea term ls                        # live terminals
snowpea term ls --all                  # include the ended ones
snowpea term read <id> --lines 100     # the screen as plain text, with 100 history lines
snowpea term send <id> "run the tests" # type the text and press Enter
snowpea term send <id> --no-submit "draft"   # type without pressing Enter
snowpea term attach <id>               # take over the terminal in this window
snowpea term attach <id> --observe     # watch only: never types, never resizes
snowpea term ls --machine <machine-id> # the terminals of a remote machine
snowpea term read <machine-id>:<id>    # the same, with MACHINE:ID instead of --machine
```

- `attach` puts your terminal in raw mode and shows the session. **Ctrl+\\** detaches and leaves the session running. `attach` needs an interactive terminal and is not supported on Windows yet; on Windows use `read` and `send`, or open the terminal in the app.
- `send` waits until you have stopped typing in that terminal, then types the text. It refuses a terminal that is blocked on a prompt, unless you add `--force`. Use `--force` only when you have read the screen and you know what the prompt is asking.
- `read` prints the screen only. It never changes anything.

## Jarvis and terminals (beta)

When Jarvis mode is on, Jarvis sees a terminal only when that terminal's own Jarvis switch is on. A terminal with its switch off is invisible to Jarvis. The same rule covers terminals on [remote machines](jarvis.md#remote-machines). Jarvis has these tools:

| Tool | What it does |
|---|---|
| `terminal_list` | lists terminals, with status and who started them |
| `terminal_read` | the screen text, status and title of one terminal |
| `terminal_send` | types a message into a CLI terminal and presses Enter |
| `terminal_keys` | presses named keys (enter, escape, up, y, 1 …) |
| `terminal_interrupt` | interrupts what the CLI is doing |
| `terminal_wait` | waits until the terminal stops working, blocks, exits, or prints matching text |
| `terminal_create` | starts Claude Code, Codex, agy or Cursor in a project, with its default permissions and an optional first prompt |

The rules:

- Jarvis types only into CLI terminals. It never types into a plain shell, never uses `force`, and never types into a terminal started with a dangerous option.
- Jarvis types only for your request or for work you delegated to it. Every read and every instruction is logged in the access log and the decision ledger.
- `terminal_create` never starts a dangerous option, and labels the terminal as started by Jarvis.
- Jarvis cannot switch a terminal's Jarvis switch on. A terminal that Jarvis creates starts with its switch on; every other terminal starts with it off, until you turn it on.
- When you type into a terminal that Jarvis manages, Jarvis steps back for `jarvis.quietMinutes` (default 10), or until you hand it back. Your keystrokes count; keys Jarvis sends do not.

What wakes Jarvis:

- a delegated terminal that becomes `blocked` or `error`, or reaches `done`, wakes Jarvis at once (within its usual batching and budget);
- every other terminal goes into the 30-second digest;
- when a terminal that Jarvis started becomes blocked, errors or exits, Jarvis gets a note with that terminal's last 20 lines. The note is an event for Jarvis; it is never typed into the terminal.

Answering a prompt in a terminal: Jarvis answers a permission prompt only when all of these hold: the terminal is on this computer, not on a remote machine; it is fully delegated to Jarvis and not held by you; it is not a dangerous terminal; the screen rule could read the command the prompt asks about; and the delegation engine classifies that command as `safe` or `risky`. A permission prompt on a remote machine always goes to you. For `guarded` or `never` commands, and for anything else, the prompt is yours. Every answer, and every refusal, is written to the decision ledger. Delegation levels are described in [Jarvis mode](jarvis.md#delegation).
