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

By default (`jarvis.external.view` set to `terminal`), an external session opens as a terminal: the CLI runs in a pane, in place of the chat view. Set it to `chat` to keep the structured chat view that snowpea used before. Clients that do not ask for a view keep the chat view.

- A new session starts the CLI in the project folder you chose.
- An existing session resumes with the CLI's own resume command (`claude --resume <id>`, `codex resume <id>`, and the matching agy and Cursor commands). The session id comes from the hook integration, so run [`snowpea integration install`](integrations.md) first if you want resume to work from a terminal.
- A session that is already live in another terminal is only viewable, as before.

The CLI runs inside your own interactive shell, so your PATH and tools such as nvm still apply. When the CLI exits, the pane drops back to the shell, unless `terminals.keepShell` is off.

## A snowpea session in a terminal

Every snowpea session can be shown two ways: as the chat, or as the snowpea CLI running in a terminal attached to the same session. Both views are clients of the same session, so you can have both open at once. The toggle is in the session header.

- The default view is `ui.sessionView`: `chat` (the default) or `terminal`. It is a setting of your own surface and does not change other people's views.
- To attach from a shell to a session that already exists in the local daemon, run `snowpea --session <id>`. The TUI resumes that session instead of creating a new one.

## Settings

| Key | Default | What it does |
|---|---|---|
| `ui.sessionView` | `chat` | the default view of a snowpea session |
| `jarvis.external.view` | `terminal` | how an external CLI session opens: `terminal` or `chat` |
| `terminals.max` | `16` | most terminals at once (1 to 64) |
| `terminals.keepShell` | `true` | after the CLI exits, drop back to the shell instead of closing the pane |
| `terminals.headlessCols` / `terminals.headlessRows` | `160` / `48` | the size a terminal uses while no viewer is attached |
| `terminals.sounds.blocked` | `true` | sound and toast when a terminal needs you |
| `terminals.sounds.done` | `true` | sound and toast when a terminal finishes and you are not looking |

The `terminals.*` and `ui.*` settings belong to the person on the local machine. A remote or tunnel connection cannot change them.

## Status: what the dots mean

Each terminal has one of five states, and the app shows them as a dot:

| Dot | State | Meaning |
|---|---|---|
| needs you | `blocked` or `error` | the program waits for an answer (a permission prompt or a question) or failed |
| done | `done` | a turn finished and nobody has looked at the terminal since (`seen` is false) |
| working | `working` | a turn is running |
| idle | `idle` | nothing is running |

A blocked state also has a kind: `permission`, `question` or `auth`. The sidebar rolls a project's dots up in this order: needs you, done, working, idle.

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

A dangerous option:

- shows a red badge everywhere it appears, and the app asks you to confirm the risk before it starts;
- is refused when Jarvis or any tool asks for it, whatever the settings;
- is refused while `web.remote.maxMode` is below `auto`;
- is refused over the web tunnel unless the terminal is unlocked ([snowpea web](web.md#a-public-address-with-a-tunnel)).

Jarvis never answers a permission prompt in a terminal that was started with a dangerous option. Non-dangerous modes such as Accept edits or Don't ask are also granted only to the person at the local app, not to Jarvis. Jarvis starts CLI terminals only with the CLI's default permissions.

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
```

- `attach` puts your terminal in raw mode and shows the session. **Ctrl+\\** detaches and leaves the session running. `attach` needs an interactive terminal and is not supported on Windows yet; on Windows use `read` and `send`, or open the terminal in the app.
- `send` waits until you have stopped typing in that terminal, then types the text. It refuses a terminal that is blocked on a prompt, unless you add `--force`. Use `--force` only when you have read the screen and you know what the prompt is asking.
- `read` prints the screen only. It never changes anything.

## Jarvis and terminals (beta)

When Jarvis mode is on, Jarvis sees the terminals in the projects it can see, and has these tools:

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

What wakes Jarvis:

- a delegated terminal that becomes `blocked` or `error`, or reaches `done`, wakes Jarvis at once (within its usual batching and budget);
- every other terminal goes into the 30-second digest;
- when a terminal that Jarvis started becomes blocked, errors or exits, Jarvis gets a note with that terminal's last 20 lines. The note is an event for Jarvis; it is never typed into the terminal.

Answering a prompt in a terminal: Jarvis answers a permission prompt only when all of these hold: the terminal is fully delegated to Jarvis and not held by you, it is not a dangerous terminal, the screen rule could read the command the prompt asks about, and the delegation engine classifies that command as `safe` or `risky`. For `guarded` or `never` commands, and for anything else, the prompt is yours. Every answer, and every refusal, is written to the decision ledger. Delegation levels are described in [Jarvis mode](jarvis.md#delegation).
