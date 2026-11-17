# External sessions (beta)

External sessions are sessions of other agent CLIs that run on your computer: Claude Code, Codex, Antigravity (agy) and Cursor agent. snowpea lists them by the folder they work in. You can open one in snowpea's chat view, in the desktop app, the web app or the terminal UI, and continue it there. Jarvis can read them and, under the rules in [Jarvis mode](jarvis.md), instruct them.

This feature is in beta. The CLIs' session records are not a published format, so a new CLI version can change them.

Other languages: [한국어](../ko/external-sessions.md) · [all pages](../README.md)

Related: [Jarvis mode (beta)](jarvis.md) · [Security lists](security.md) · [Agents and delegation](agents.md) · [Terminal UI](tui.md)

## What it is

- snowpea finds the four CLIs on the machine that runs the daemon. A CLI that is not installed shows as "not installed". You can turn each CLI off in settings.
- Sessions are listed with their project, beside snowpea's own sessions. Use **Load external session…** in the project's **+** menu to see sessions from another folder or from every project.
- A session opens in snowpea's chat view. It looks like a snowpea session: messages, tool calls and approvals appear in one stream.

## The three kinds of session

Each external session has a **host**. The host says where its process runs, and that decides what you can do.

| Host | What it means | What you can do |
|---|---|---|
| **Run by snowpea** | snowpea runs the session's process and handles its approvals | Send, interrupt, change mode, stop. Approvals appear in snowpea. |
| **In another terminal** | You run the session in a plain terminal. snowpea follows it live. | View only. snowpea cannot type into that terminal. When the terminal closes, the session becomes available: continue it here. |
| **Shared terminal (tmux)** | The session runs in a tmux pane: one snowpea started, or your own pane with any of the four CLIs. | Send, stop and switch modes in the same pane. Prompts snowpea recognises come to snowpea; you answer the rest in the terminal. See [Sessions in your tmux pane](#sessions-in-your-tmux-pane). |
| **Not running** | No process is running. snowpea shows the history. | Read the history. Send a message or press **Continue here** to start it under snowpea. |

Shared terminal (tmux) works on Linux and macOS only. It is not available on Windows.

## Create, load, send, stop

In the desktop app and snowpea web, a project in the sidebar shows one list of its sessions: snowpea's own and the external ones together, each row with the icon of the agent that runs it. The project's **+** menu has **New Session**, **New external session…** and **Load external session…**.

| Task | In the app | In the terminal UI |
|---|---|---|
| Start a new session | Project **+** → **New external session…**: pick the CLI, an optional model (the default if empty), and the folder | `/external new` |
| Open a session from this folder | Pick its row in the project's session list (the agent icon shows which CLI) | `/external` (the picker) |
| Open a session by its handle | Project **+** → **Load external session…**, then search or filter by CLI and project | `/external open <cli>:<id>`, where `<cli>` is `claude`, `codex`, `agy` or `cursor` |
| Send a message | Type in the composer and send | Type in the prompt |
| Interrupt the running turn | **Stop** | **Esc** |
| Leave the screen and keep it running | Close the tab or panel | `/external close` |
| Stop the process and keep the history | **Stop session** | `/external stop` |
| Load older messages | **Load earlier messages** | `/external more` |

Notes on these actions:

- **Reading a session starts nothing.** Opening a session that is not running only reads it. Your first message continues it, in the CLI's default mode unless you pick a mode first.
- **Leaving and stopping differ.** Leaving the screen keeps the process running. Stopping ends the process. The history stays in both cases.
- **Esc in the terminal UI.** In an open external session, Esc stops the running turn. When no turn is running, Esc leaves the session (it keeps running). For a session in another terminal, snowpea cannot interrupt it: Esc shows a message, and pressing Esc again right away leaves the screen.
- **A busy session takes no new message.** While a turn runs, the composer says the CLI is still working. Wait for the turn to end, or press **Stop**.
- **Steering a running turn (Codex only).** **Add to turn** sends your text into the turn that is running now. Only Codex can take text into a running turn. For Claude Code, agy and Cursor agent, wait for the turn to end or stop it.
- **A session in another terminal** takes no input from snowpea. Stop it in that terminal.

## Approvals

When the CLI asks for permission, the request appears in snowpea as an ordinary approval. Approve it, deny it, or choose **Allow for this session**.

- **Allow for this session** remembers that one exact call: the same tool with the same whole command line, or the same arguments. It never applies to a request that Jarvis's guarded rules or the never-yours rules cover. Those always come to you.
- **Blocked commands.** A command on your [security list](security.md) is refused before anything is asked. This holds for the approvals snowpea handles. It does not reach inside an unrestricted session or a shared terminal (tmux) session.
- **Approvals in the terminal.** A shared terminal (tmux) session answers its prompts in the terminal. In your own tmux pane, only prompts snowpea recognises come here as approvals, and the answer key is sent only while the same prompt is on screen ([Sessions in your tmux pane](#sessions-in-your-tmux-pane)).
- **Limited approvals.** Some modes have no per-action prompt. Edits are allowed automatically and everything else is blocked. The composer labels such a session.
- **agy** follows the approval setting in agy's own configuration. If agy is set to run without asking, it may never ask you. snowpea cannot change that for one session.

## Modes

Each CLI has its own modes, and snowpea offers them under the CLI's own names. Pick one when you start a session, or change it with the mode switch in the app or `/external mode <mode>` in the terminal UI. snowpea asks the installed CLI which modes it has, so a mode your version lacks is not offered.

Each mode is described by two things: whether the CLI stops to ask you, and what it may write.

| CLI | Mode | Asks you | Writes | Who may choose it |
|---|---|---|---|---|
| Claude Code | **Manual** (`default`; called Default before 2.1.326) | Whenever a permission is needed | The project, after you allow it | Anyone, Jarvis included |
| Claude Code | **Plan** (`plan`) | Whenever a permission is needed, and before it starts the work | Only its plan file | Anyone |
| Claude Code | **Accept edits** (`acceptEdits`) | Before commands; edits go through | The project | Only you |
| Claude Code | **Auto** (`auto`) | Sometimes: it proceeds on its own and asks when unsure | The project | Only you |
| Claude Code | **Don't ask** (`dontAsk`) | Never: what your own Claude Code rules do not allow is refused | What your rules allow | Only you |
| Claude Code | **Bypass permissions** (`bypassPermissions`) | Never | Anywhere | Only you |
| Codex | **Untrusted** (`untrusted`) | Before anything not known to be safe | The project (sandbox) | Anyone. snowpea's default for Codex |
| Codex | **Read Only** (`read-only`) | Never | Nothing (read-only sandbox) | Anyone |
| Codex | **Default** (`default`) | Sometimes: Codex decides when to ask | The project (sandbox) | Only you |
| Codex | **Full Access** (`full-access`) | Never | Anywhere, with network | Only you |
| agy | **Default** (`default`) | Per agy's own approval setting | The project | Anyone |
| agy | **Plan** (`plan`) | Per agy's setting; asks in the chat before it starts the work | Only its plan | Anyone |
| agy | **Accept edits** (`accept-edits`) | Before commands; edits go through | The project | Only you |
| agy | **Auto** (`auto`) | Sometimes: edits and safe commands go through, irreversible actions ask | The project | Only you |
| agy | **Skip permissions** (`dangerously-skip-permissions`) | Never | Anywhere | Only you |
| Cursor agent | **Ask** (`ask`) | Never | Nothing. **Cursor's starting mode.** | Anyone |
| Cursor agent | **Plan** (`plan`) | Never | Only its plan | Anyone |
| Cursor agent | **Agent** (`agent`) | Never: Cursor has no prompts to bring here | The project (sandbox) | Anyone |
| Cursor agent | **Run Everything** (`yolo`) | Never | Anywhere | Only you |

Things to know:

- **Plan mode is not read-only.** It writes its plan. When Claude Code finishes a plan, it asks to start the work. That question comes to you as an approval, and only you can answer it: Jarvis and automatic rules cannot. If you allow it, the session continues in the default mode, which asks before each change. If you deny it, Claude Code keeps planning. agy asks the same question in the chat instead.
- **Auto still asks sometimes.** In Claude Code's and agy's auto mode the CLI decides most actions alone. Whatever it still asks comes to snowpea as an ordinary approval, as in the default mode.
- **The words "read-only"** are used only for a mode that writes nothing: Codex Read Only and Cursor Ask.
- **A mode marked "Only you"** goes through the same check as unrestricted mode below: only a person at the app, the web app or the terminal UI can choose it, not a remote web sign-in, and each choice is written to Jarvis's ledger. Jarvis cannot choose it and does not send work into a session that runs in it. A session you continue later comes back in the CLI's default mode, not in that mode.
- **Older clients** still see three modes: default, read-only and unrestricted. Each CLI mode maps to one of them. For example Claude Code's Plan shows as read-only and Auto shows as default.

What **unrestricted** means (Bypass permissions, Full Access, Skip permissions, Run Everything):

- The CLI runs without asking. snowpea does not control what it does, so Jarvis cannot decide its approvals and its guarded-kind rules and the security lists do not apply inside it.
- **Only you can choose it, and only for one session.** The choice is never remembered. It is not a default, and a session you continue does not come back in unrestricted mode. snowpea shows a confirmation that names the CLI, what the mode allows, and the folder, and it writes each switch to Jarvis's ledger.
- A remote web sign-in cannot choose it either. See [Using Jarvis from another computer](jarvis.md#using-jarvis-from-another-computer).
- Jarvis cannot start an unrestricted session. From a messenger, you can allow one with a single press in the chat, in a turn that you started yourself. See [Jarvis mode](jarvis.md#messengers).

Raising a session's mode is always yours. Jarvis can start a session in a mode that anyone may choose (for example Claude Code's Plan) and cannot change a session's mode afterwards.

## Sessions in your tmux pane

A session that is open in another terminal is view only, because a second process on the same session would split its history. There is one exception: when that terminal is a **tmux pane**, snowpea can drive the same process through tmux. Nothing new is started.

- **How snowpea finds the pane.** It starts from the process that holds the session, takes that process's terminal, and looks for the tmux pane with that terminal. It never uses text on the screen or in the transcript to pick a pane. A terminal that is not tmux stays view only until you close it, as before.
- **What you can do.** Send messages, stop a running turn with the CLI's own key (Escape for Claude Code, Codex and agy; Cursor agent has none here), and switch modes. Reading works as before, from the CLI's own history files.
- **The guard.** Before every key it sends, snowpea checks that the pane still exists, that its terminal is still the CLI's terminal, that the CLI is still running, and that the CLI, not a shell, a pager or an editor, is in the foreground. If any check fails, nothing is sent and the session goes back to view only. Text that reached a shell prompt would run as a command, so there is no "send anyway".
- **How text goes in.** As a bracketed paste, so a line break in your message cannot send it early, followed by Enter. Control characters such as Escape are refused, and so is a message that starts with `/` or `!` (the CLIs' command and shell prefixes).
- **Busy CLI.** While the CLI is working, or while a prompt waits in the terminal, snowpea does not send. Wait, or stop the turn.
- **Who may type.** You, at the app, the web app or the terminal UI. Jarvis or a remote web sign-in may send only when the pane shows a mode snowpea recognises, that anyone may choose and that asks before it acts. Mode switches are always yours. Every key snowpea sends is logged with the pane, never with your text.
- **Modes.** Claude Code: plan with its `/plan` command; Manual, Accept edits and Auto with Shift+Tab, pressed until the status line shows the mode. agy and Cursor agent: Shift+Tab the same way. Codex has no mode switch here, because its `/permissions` opens a list. After a switch snowpea checks the screen; if the screen does not show the new mode, the session says **unconfirmed** instead of claiming it. When the screen shows no mode snowpea knows, the session says **mode not shown**.
- **Permission prompts (best effort).** A prompt in a terminal is not sent to snowpea the way a prompt from a session snowpea runs is. snowpea reads the screen. It recognises Claude Code's prompt ("Do you want to …? 1. Yes … Esc to cancel") and Codex's ("Would you like to run the following command? … (y) … (esc)") and shows them as approvals. Your answer goes to the terminal as the CLI's key (Claude Code `1` or Escape, Codex `y` or Escape), and only while the same prompt is still on screen. Only you can answer Codex's prompts, because part of their text is written by the model. Jarvis may answer a recognised Claude Code prompt under the usual delegation rules. A prompt snowpea does not recognise, including every agy and Cursor agent prompt, is never answered for you: the session says a prompt waits in the terminal, and **/external snapshot** (or the snapshot in the app) shows the pane's text.

## Trust a project for external CLIs

The first time you open an external session in a project, snowpea asks you to trust the project.

Why: a CLI loads the project's own settings and hooks. A cloned repository could ship a hook that runs code on your machine. Trusting the project tells snowpea that you accept what that project's settings do.

- Only you can confirm trust. Jarvis, a browser session and other programs cannot.
- snowpea keeps the trust record in its own home folder, in `jarvis/external-trust.json`. It does not store trust in the project's repository.
- Until you trust a project, snowpea does not start an external CLI there. If Jarvis tries, it is told that a person must open an external session in that project first.

## A custom CLI path

If you set a path for a CLI in **Settings → External agent CLIs**, snowpea does not run that program until you confirm it. This applies even to a version check. Until you confirm, the CLI shows the path as pending and is not installed.

To confirm, press the confirmation in settings, or type `/external confirm-path <cli>` in the terminal UI. snowpea shows the program's resolved real path before you confirm. Only you can confirm a path, and not from a remote web sign-in.

## Environment variables for a CLI

A CLI gets a short list of safe variables: the search path, your home folder, locale, terminal, temp folder, certificates and proxy settings, and the variables that point the CLI at its own config folder. Anything else is removed, including snowpea's daemon token.

If a CLI needs a provider key, such as an API key variable, name that variable in `jarvis.external.clis.<id>.passEnv`:

```json
{
  "jarvis": {
    "external": {
      "clis": {
        "claude": { "passEnv": ["ANTHROPIC_API_KEY"] }
      }
    }
  }
}
```

Rules for `passEnv`:

- Only you can set it, from snowpea on the machine that runs the daemon. Jarvis cannot set it.
- snowpea rejects names that start with `SNOWPEA_`. snowpea's own variables are never passed to a CLI.
- Each name must be a valid variable name.

## What Jarvis can do with external sessions

Jarvis is limited to the same rules as above. In short:

| Jarvis can | Jarvis cannot |
|---|---|
| List external sessions and read them (up to 200 entries, in summaries) | Read an external session in a project you hid from it |
| Send an instruction to a session snowpea controls and that is not paused | Send into a paused session, or into a session in unrestricted mode or another mode only you may choose |
| Interrupt a running turn | Start a session in unrestricted mode or another mode only you may choose (it can ask you for one) |
| Start a session in a mode anyone may choose, such as Plan | Change a session's mode |
| Decide the approvals routed to it, under the delegation rules | Decide approvals in a shared terminal (tmux) session |

When you type into a session that Jarvis manages, Jarvis pauses there. See [Stepping in](jarvis.md#stepping-in).

## Notes for each CLI

- **Claude Code.** Its history comes from its own project records. Its approvals stream into snowpea. A shared terminal (tmux) is available on Linux and macOS.
- **Codex.** All Codex sessions share one `codex app-server` process that snowpea runs. After its terminal closes, Codex takes about a minute to release the session (measured at 57 seconds), so **Continue here** may wait for a short time. Codex is the only CLI that takes text into a running turn (**Add to turn**).
- **agy.** Its approvals follow agy's own setting. If that setting is to run without asking, agy may never ask, and snowpea cannot change it for one session; its modes then all show as never asking. agy has no read-only mode: its plan mode writes a plan.
- **Cursor agent.** It does not send approval requests to snowpea. It starts in Ask mode, which is read-only. Agent mode lets it edit and run sandboxed shell commands without a prompt. Run Everything runs it with `--yolo`. Cursor agent can leave helper processes running. snowpea cleans them up when it stops.
- **Windows.** Shared terminal (tmux) is not available on Windows. snowpea starts external CLI processes without a console window and ends a CLI's whole process tree together when it stops one. Cleaning up leftover CLI processes after an unclean daemon exit needs the process start time, which Windows does not give, so such leftovers are not cleaned up automatically there.

## Terminal UI commands

| Command | What it does |
|---|---|
| `/external` | Opens the picker of this folder's sessions |
| `/external new` | Starts a new session in the current folder |
| `/external open <cli>:<id>` | Opens a session by its handle |
| `/external close` | Leaves the open session. It keeps running. |
| `/external stop` | Stops the open session's process. The history stays. |
| `/external adopt` | Lets snowpea type into the tmux pane the open Claude Code session runs in. You confirm first. |
| `/external mode <mode>` | Switches the open session to one of its CLI's modes, by the id in the table above (for example `plan`, `auto`, `read-only`). `default`, `readonly` and `unrestricted` still work. A mode only you may choose asks you to type its name to confirm. |
| `/external snapshot` | Shows the text of the tmux pane the open session runs in |
| `/external more` | Loads earlier messages of the open session |
| `/external confirm-path <cli>` | Confirms the path you configured for a CLI |

Type `/external` followed by a space and the TUI lists these subcommands.

## Settings reference

All settings are under `jarvis.external` in `settings.json`.

| Setting | Default | What it does |
|---|---|---|
| `jarvis.external.enabled` | `true` | Allows external sessions. |
| `jarvis.external.maxProcesses` | `4` | Most CLI processes snowpea runs at once (1 to 32). |
| `jarvis.external.idleSec` | `900` | Seconds an idle CLI process is kept before it stops (at least 30). |
| `jarvis.external.clis.<id>.enabled` | `true` | Uses this CLI. `<id>` is `claude`, `codex`, `agy` or `cursor`. |
| `jarvis.external.clis.<id>.path` | `null` | Executable path. `null` searches `PATH`. A path runs only after you confirm it. |
| `jarvis.external.clis.<id>.defaultMode` | `null` | Mode new or continued sessions start in. `null` uses the CLI's own default (read-only for Cursor agent). Unrestricted is never accepted here. |
| `jarvis.external.clis.<id>.passEnv` | `[]` | Environment variable names to pass to the CLI. Only you can change it. |
