# Jarvis mode (beta)

Jarvis mode is a supervising conversation. One Jarvis session watches every session in your projects, reports what they are doing, decides the things you delegated to it, and brings you the rest. Use it when you run several sessions at once, or when you want work to keep moving while you are away from the screen.

This feature is in beta. It may misjudge a request, and some kinds of session cannot be decided for yet. Read [the security notes and known limits](#security-notes-and-known-limits) before you delegate anything.

Other languages: [한국어](../ko/jarvis.md) · [all pages](../README.md)

Related: [External sessions (beta)](external-sessions.md) · [Security lists](security.md) · [Agents and delegation](agents.md) · [Gateway](gateway.md) · [snowpea web](web.md) · [Voice](voice.md)

## What Jarvis mode is

Without Jarvis, every session waits for you. You approve each command, answer each question, and type "continue" when a session stops early.

With Jarvis mode on:

- Jarvis reads the state of every exposed session and keeps a running picture of it.
- Jarvis answers the approvals and questions you allowed it to handle, and logs each decision.
- Jarvis keeps a session going when you handed that session over completely.
- Jarvis tells you, in one list, what finished, what failed, and what needs you.
- You can talk to Jarvis about any session, in any screen, and ask it for a report.

Jarvis mode is **off by default**. Nothing changes until you turn it on.

### Turn it on

| Where | How |
|---|---|
| Desktop app or snowpea web | Open **Jarvis** in the sidebar and press **Enable Jarvis**. You can also open **Settings → Jarvis & agents** and turn on **Turn Jarvis on**. |
| Terminal UI | Type `/jarvis on`. Type `/jarvis` alone to open the Jarvis screen. |
| Terminal, outside the UI | Run `snowpea jarvis`. This opens the terminal UI on the Jarvis screen. |
| Messenger | Send `/jarvis on` from the chat that is bound to you. Only the person bound to that chat can do this. See [Messengers](#messengers). |

Turn it off with **Turn off** in the Jarvis screen, `/jarvis off`, or `/jarvis off` from the bound messenger chat. When Jarvis mode is off, every approval and question goes to you again.

Jarvis runs as one session per daemon. It uses the model you choose in `jarvis.model`. If you leave that empty, it uses your default model.

The working folder is `~/Snowpea/Jarvis` by default (`jarvis.folder`). Jarvis writes its reports and to-do list there. The folder must not be inside snowpea's own home folder or a credentials folder. Settings refuses such a path, and Jarvis does not start on one.

## What Jarvis can and cannot do

| Jarvis can | Jarvis cannot |
|---|---|
| Read the state, summary and recent history of every session in an exposed project | Read a project you hid from it |
| Read the global memory and the memory of exposed projects | Write to any memory except its own |
| Write files, but only in its own working folder | Write files in your projects. Changes go through a worker session. |
| Send instructions to sessions snowpea runs, and to external sessions snowpea controls | Instruct a session snowpea does not control, such as an unrestricted external session |
| Answer approvals and questions that were routed to it | Answer its own approvals, or change its own rights |
| Lower a session's permission mode | Raise a session's permission mode. Only you can. |
| Search the web with a short query (up to 200 characters) | Fetch a URL. It has no URL-fetch tool. |
| Read a file in your project, or in its own folder | Read anything under snowpea's home folder: tokens, settings, credentials, records |
| Propose a delegation for you to confirm | Create or widen a delegation by itself. Only you can confirm one. |

Three rules matter most:

- **Jarvis changes your projects only by instructing a worker session.** It does not edit a project file itself.
- **Nothing Jarvis reads can give it more authority.** A web page, a mail, a log or an external session's output cannot change a delegation. A question that Jarvis answers reaches the worker labelled as answered by Jarvis, not by you.
- **Only a person raises a permission mode.** A session's mode (for example from "plan" to "accept", or on to "auto") can be lowered by Jarvis or by an agent, but raising it needs your own answer. The question or command is refused if anything else tries to answer it, and no delegation changes that. See [Modes](modes.md).

## Delegation

Delegation decides what Jarvis may decide without asking you. Jarvis never acts beyond the delegation you gave.

### Levels

| Level | What Jarvis decides |
|---|---|
| **Staged** (default) | Safe and read-like approvals, and how-to questions. Everything riskier comes to you. |
| **Full** | Everything in staged, plus risky approvals, stops, questions and unfinished endings. Jarvis keeps the session moving. |

Staged is the level with no grant. Full is never the default. You give Full for a scope, as a grant.

### Scope

A grant applies to one of these scopes:

- **Session**: one session. It ends when the session ends, or when you revoke it.
- **Project**: every session in one project. The default expiry is 24 hours (`jarvis.delegation.projectTtlHours`).
- **Everywhere**: a global grant. It applies to every exposed project until you revoke it.

### Hand a session or a project over

1. Tell Jarvis in conversation what you want. For example: "Take over this session, staged." Or: "Take over the whole project, full."
2. Jarvis shows a confirmation card. The card names the scope, the level, the guarded kinds you chose to hand over (see below), the expiry, and Jarvis's reason.
3. Press **Confirm** once. Only you can press it. Nothing Jarvis reads can confirm a grant. In a messenger, the confirmation is a one-press reply from your bound chat.

A remote web sign-in cannot hand over a guarded kind in a grant (see [Using Jarvis from another computer](#using-jarvis-from-another-computer)). Do that on the machine itself.

Press **Decline** to change nothing. Jarvis keeps the authority it already has.

Jarvis can propose a grant. It cannot create one. A grant that Jarvis proposes shows **proposed by Jarvis** in the list, and it takes effect only after you confirm it.

### "Delegate everything"

A global Full grant is "delegate everything". You give it the same way as any grant, with one confirmation. The default setting cannot be Full (`jarvis.delegation.default` accepts only `staged`). A Full value saved by an earlier beta loads as Staged, and snowpea raises one notice that explains how to grant it again properly.

### Guarded kinds

Three kinds of action are guarded. Jarvis asks you about them even under Full, unless you hand that kind over separately in the grant:

| Kind | Covers |
|---|---|
| Deploy, release, push | Deploys, releases, pushes (and publishing or merging of the same kind) |
| Outside the project, deletes | Work outside the project folder, deletes, hard resets |
| Secrets and sending | Tokens, passwords and other secrets, and sending data out (uploads, mail, sharing) |

The grant card lists the three kinds with **Also decide** checkboxes. All three are off by default.

### Always yours

These requests always go to you. No grant changes that, and Jarvis cannot decide them even when you allow everything else:

- Anything in snowpea's own home folder: its token, settings, credentials, session and memory records, and the database files.
- The permission files of an agent CLI, such as its settings under `.claude/` or `.codex/`.
- Changes to snowpea's own configuration.
- Raising a session's permission mode.
- Questions that mention a guarded action, or that ask for a secret.
- A line snowpea cannot check against your blocked-command list. See [Security lists](#security-lists).
- Approvals of a browser session. Their approvals stay with the browser connection in this release.

### See and revoke delegations

Open **Settings → Delegations**. The list shows each grant: its scope, level, guarded kinds handed over, who granted it, and its expiry. Expired grants are hidden unless you choose **Show expired**.

To end a grant, press **Revoke** and confirm. Jarvis goes back to the default level for that scope. Decisions it already made stay as they were.

Every grant and every change to one is written to Jarvis's ledger.

## What gets auto-allowed, what Jarvis decides, what comes to you

| Request | Staged | Full |
|---|---|---|
| Safe approval (routine, low-risk actions the permission rules class as safe) | Allowed automatically. Logged. No model call. | Same |
| Read-like approval (a command that only reads, but is not on the safe list; the card is marked "read-like") | Jarvis decides: it allows it once, or leaves it to you | Jarvis decides |
| Other risky approval | Comes to you | Jarvis decides |
| Question with a how-to answer | Jarvis answers. The worker is told Jarvis answered. | Jarvis answers |
| Other question | Comes to you | Jarvis decides, unless it touches a guarded kind or asks for a secret |
| Stopped or unfinished session | Comes to you | Jarvis keeps it moving |
| Guarded kind (deploy, outside or destructive, secrets and sending) | Comes to you | Comes to you, unless you handed that kind over |
| Always yours (snowpea's records and settings, the CLI's own permission files, raising a mode) | Comes to you | Comes to you |
| A compound command snowpea cannot read | Treated as guarded | Treated as guarded |
| A blocked command | Never put to anyone. It does not run. | Same |

**Safe versus read-like.** Safe is a short, fixed list of commands and options whose every operand is a plain path inside the project, such as `ls`, `cat`, `head`, `wc` or a plain `grep`. Read-like is a command that reads but could reach further or run something: read-only `git` commands (`git status`, `git diff`, `git log` and similar) at the project's top level, `rg`, `jq`, a recursive option such as `grep -r`, `ls -R` or `find -L`, a wildcard, or a shell command that an external CLI sent. Jarvis allows a read-like request once only when the command is plainly read-only and every path stays inside the project. Otherwise it leaves it to you.

If a request does not fit a row, Jarvis treats it as the more careful choice and sends it to you.

## Stepping in

You can work in a session that Jarvis manages.

- **Typing pauses Jarvis there.** When you type into a session that Jarvis manages, Jarvis stops deciding in that session. The session banner shows **Paused while you type**.
- **It returns on its own.** Jarvis returns after `jarvis.quietMinutes` (default 10) with no input from you. The banner counts down.
- **Hand back now.** Press **Hand back now** in the session banner, or type `/jarvis handback` (in the terminal UI) or `/jarvis handback <name>` (in a messenger). Jarvis returns at once.
- **Take over.** Press **Take over** in the banner, or type `/jarvis takeover`. You keep the session until you hand it back. Jarvis does not return by itself. In the terminal UI, `/jarvis takeover <minutes>` gives you the session for that many minutes, and `/jarvis takeover <n> <minutes>` picks a session by its row number.

Typing in a session counts as your input for this rule. Typing in another session does not pause Jarvis here.

## Notices

Jarvis does not report every event. It tells you about the things that matter. You can switch each kind off:

| Notice | Setting | Default |
|---|---|---|
| A session finished | `jarvis.proactive.finished` | on |
| A session failed or stopped with an error | `jarvis.proactive.error` | on |
| Something needs you | `jarvis.proactive.needsYou` | on |
| Jarvis decided something for you | `jarvis.proactive.decided` | on |

Where a notice appears:

| Place | What reaches it | Setting |
|---|---|---|
| The app and the terminal UI (badge and list) | Every notice | `jarvis.notify.inApp` (on) |
| The operating system (desktop notification) | Notices of normal importance and up | `jarvis.notify.os` (on) |
| Your messenger, in your private chat | High and urgent notices only | `jarvis.notify.messenger` (on) |

Notices stay in the list until you dismiss them.

- In the app, press **Dismiss**, or **Dismiss all** in the Waiting for you panel.
- In the terminal UI, type `/jarvis waiting` to see the list, then `/jarvis dismiss <n>` or `/jarvis dismiss all`.
- In a messenger, notices arrive only in the private chat of the bound person.

## Security lists

Blocked commands and protected folders apply to Jarvis exactly as to you: they are checked before anything Jarvis, its workers or a delegation could decide. A blocked command is never put to Jarvis, never runs, and cannot be granted; Jarvis's ledger records the block. A line snowpea cannot check against the lists (a program in a variable, `eval`, a script piped into a shell) is always routed to you, never to Jarvis — the card says "cannot be checked against your block list". External sessions that run unrestricted, or that answer their prompts in their own terminal, are marked in the overview: the lists do not reach inside them. See [Security lists](security.md) for the presets, the pattern syntax and the limits.

## Projects

Jarvis supervises the projects that you expose to it.

- **Exposure is per project.** Every project is exposed by default. Turn off **Expose to Jarvis** in the project's settings to hide it completely. Jarvis then cannot see its sessions, its transcripts or its memory. Hidden projects are listed in Settings as a count, for example "1 project(s) are hidden from Jarvis." The switch is stored as `jarvis.expose` in the project's own `.snowpea/settings.json`. If that file cannot be read, snowpea treats the project as hidden.
- **A project under a hidden project is hidden too.** If a folder above a registered project is hidden, so is the project.
- **Browser sessions** (sessions that the snowpea browser runs) follow `jarvis.exposeBrowser`, which is on by default. When it is off, Jarvis sees only the status of those sessions. It cannot read their transcripts or instruct them.
- **Names.** Jarvis refers to projects by the names snowpea shows for them, and by any alias you gave a project. Use the same name in conversation. For example, "the website project" works when that is the project's name or alias.
- **Renaming and aliases.** A project's display name and aliases are kept in snowpea's home folder, outside every project, so a session working in a project cannot rename it. They are changed by a client through the `jarvis.project.rename` and `jarvis.project.alias` calls (see the [protocol reference](protocol.md)). Jarvis has no tool to set them, and you cannot set them by asking Jarvis.

## Messengers

When Jarvis mode is on, a bound messenger chat talks to Jarvis, not to an ordinary session.

- **Only the bound person.** Jarvis talks only to the person bound to the chat. Other members of a group chat get a short reply that Jarvis talks only to its owner there, at most once every 10 minutes. Nothing they write reaches Jarvis.
- **Private chat only for anything Jarvis sends on its own.** Notices, briefings, delegation confirmations and forwarded requests go to the bound person's private chat, never to a group. If snowpea does not know a private chat for that person, nothing is sent, and the app shows an escalation: "messenger: no private chat." Write to the bot once in a direct message so snowpea learns it. The escalation repeats at most once an hour.
- **Hidden projects stay hidden.** A request from a project you hid from Jarvis is not forwarded to your messenger either.
- **Forwarded messages.** A message that someone forwarded reaches Jarvis as quoted, untrusted text. It cannot confirm a grant.
- **Routing.** `jarvis.messenger.route` (on) sends the chat to Jarvis while Jarvis mode is on. When you turn Jarvis mode off, the chat goes back to the session it was bound to.
- **Other slash commands are locked.** While Jarvis mode routes a chat, the other `/` commands of snowpea do not run from it, because they would act on Jarvis's own session. Use the `/jarvis` commands below.

Commands in a messenger chat:

| Message | What it does | Who |
|---|---|---|
| `/jarvis` or `/jarvis status` | Shows the sessions, how many are running, and how many wait for you | Bound person |
| `/jarvis on` or `/jarvis off` | Turns Jarvis mode on or off | Bound person |
| `/jarvis takeover <name>` | Gives you the named session. Jarvis decides nothing there until you hand it back. | Bound person |
| `/jarvis handback <name>` | Gives the named session back to Jarvis | Bound person |
| `/jarvis help` | Lists the commands | Anyone |

The short forms also work: `enable`/`disable`, `take`, `back` or `return`. The Korean forms 켜기, 끄기, 인수, 반환 and 상태 also work. If a name matches more than one session, snowpea lists them and asks you to be exact.

Granting from a messenger is allowed by default (`jarvis.messenger.allowGrants`, on). The grant still needs one press from you, in the same chat. The confirmation shows the whole first message. If that message is longer than 3,000 characters, snowpea refuses the grant rather than show you part of it.

To stop grants from a messenger, set `jarvis.messenger.allowGrants` to `false`. Grants then come only from the app or the terminal UI.

See [Gateway](gateway.md) for binding a chat to snowpea in the first place.

## Using Jarvis from another computer

You can open snowpea web from another machine (see [snowpea web](web.md#from-another-machine)) and talk to Jarvis there. The short version:

- `snowpea web --host <address> --port <n>` opens the remote listener now, without restarting the daemon. Add `--save` to keep it at every start.
- Without a password it prints an **access link**, `http://<address>:<n>/web/?t=<token>`. The token in the link is a separate web access token, not the daemon's. The first visit exchanges it for a sign-in cookie and removes it from the address.
- For a sign-in page instead, set a username and a password: `--user <name>` with `--password-stdin`, the `SNOWPEA_WEB_PASSWORD` variable, or `--password` (visible in your shell history). `snowpea web password` sets or changes it later.

Every connection that comes in through the remote listener is a **remote sign-in**, whether it used the link or the password. A remote sign-in is you: anyone who has the link or the password can use snowpea as you, including running commands on this computer. It is not a limited account.

A few things are kept on this computer all the same, so that a mistake or a borrowed session cannot change them by a click. Through snowpea's own screens a remote sign-in cannot:

- turn Jarvis mode on, or change Jarvis settings;
- hand a guarded kind over in a delegation grant;
- change the security lists or the remote-access settings themselves;
- open an external session unrestricted, or confirm the path of an external CLI.

Do those on the machine itself. This list is not a security boundary against someone who holds the link; [snowpea web](web.md#what-stays-on-this-computer) has the whole list and says what it is for. The remote page is also plain HTTP; read the warnings there before you open it on a network you do not trust.

## Assistant features

Jarvis can also keep your schedule, mail and to-dos in view. These features do not run unless you set them up.

### Briefings

A briefing is a short summary from Jarvis on a schedule. It is **off until you turn it on**.

- Turn it on in the Jarvis screen under **Briefing**, or ask Jarvis for one in conversation.
- Choose the days and times, the time zone, and what the briefing covers: sessions, what waits for you, to-dos, mail, and the calendar.
- Each briefing is saved as a report in the Jarvis folder, under `reports/`.
- Mail and calendar sections need a connected mail or calendar preset (see below).

The schedule is a cron expression in `jarvis.briefing.schedule`. The default is `0 9 * * 1-5` (09:00 on weekdays). The zone is `jarvis.briefing.timezone`. Leave it empty to use the daemon's zone.

### Scheduled tasks and to-dos

- Jarvis can list, create, change, remove and run scheduled tasks. A scheduled task runs in its own worker session, and the result comes back to Jarvis.
- Jarvis does not create or change a scheduled task because of something a session said. It refuses when the turn was started by a wake-up message rather than by you. Ask in your own turn.
- To-dos are kept in the Jarvis folder. Jarvis can add, list and complete them. A to-do can repeat daily, weekly or monthly. A repeating to-do needs a due date.

### Mail and calendar presets

A preset connects mail and calendar to Jarvis through an MCP server, with the steps already set. Open **Settings → Jarvis & agents → Assistant presets**, pick a preset, and follow the steps. Each preset lists its read tools and the tools that change or send things.

Read tools run without asking. Tools that send, reply, delete, move or change anything always ask you, and Jarvis cannot decide them.

| Preset | What it covers | What you need |
|---|---|---|
| **Google (Gmail and Calendar)** | Gmail and Google Calendar, through Google's own remote MCP servers | An OAuth client ID and client secret from your own Google Cloud project. The Gmail MCP API and the Calendar MCP API must be enabled in that project. The OAuth client must be marked for use by an AI-powered agent. Google's servers are a Developer Preview: join the preview with the account you will sign in with. |
| **Microsoft 365 (Outlook mail and calendar)** | Outlook mail and calendar | Sign in once with a device code. For a personal account, leave the tenant ID empty or enter `consumers`. A work account may need admin approval. The first start downloads the server with `npx`, so Node.js must be on the daemon's machine. |

Google read tools: search and read threads, list labels and drafts, list calendars and events, get an event, and suggest a time. Drafting, labelling and every calendar change ask you first. Gmail has no send tool, so snowpea cannot send mail through it.

Microsoft 365 read tools: list and read mail, folders and rules; list calendars and events; find meeting times; and check the sign-in. Sending, replying, forwarding, moving, deleting, rule changes, calendar changes, and fetching raw message files or attachments always ask you.

**Know the risk of the Microsoft 365 preset.** It uses a community server, `@softeria/ms-365-mcp-server`, not a server from Microsoft. It runs with your account's access. snowpea pins the server's version, but not the versions of its dependencies. npm install scripts are turned off when the server is downloaded.

Sign-in with the Microsoft 365 preset is a command you run once on the daemon's machine:

```text
npx -y --ignore-scripts @softeria/ms-365-mcp-server@<pinned version> --login --preset mail,calendar
```

The setup screen shows the exact command with the pinned version. Copy that command. It prints a code and a Microsoft address. Open the address in a browser, enter the code, and sign in. Then press **Check sign-in**.

Removing a preset removes its servers, its permission tags, its stored client secret and its scheduled routines. It does not sign you out of the Microsoft account. The server keeps its own sign-in, and snowpea says so when you remove the preset.

### Mail triage runs in an isolated worker

When a mail task runs, a worker session reads the mail. The worker gets only that preset's read tools, an empty scratch folder, and no delegation or memory. Jarvis receives only the worker's summary. The worker's transcript is deleted after it runs. Mail content therefore does not mix into Jarvis's decisions.

## Voice

You can speak to Jarvis with push-to-talk.

1. Hold the shortcut, **Ctrl+Shift+Space** by default, or hold the microphone button in the composer.
2. Speak while you hold the key or the button.
3. Release the key or button. The text is sent when you release (**send on release**).

Change the shortcut and the send-on-release behaviour in **Settings → Audio → Push-to-talk**. The same screen has a switch that reads Jarvis's answers aloud. See [Voice](voice.md) for the speech settings.

## External agent CLIs under Jarvis

Jarvis can read and, under the delegation rules, instruct sessions of Claude Code, Codex, agy and Cursor agent. Four things in this area are yours to confirm, and Jarvis can do none of them:

- **Trust a project.** A person must trust a project before snowpea starts an external CLI there. Jarvis cannot give that trust.
- **Confirm a CLI path.** A path you set for a CLI runs only after you confirm it.
- **Choose unrestricted.** Only you can choose it, for one session at a time.
- **Name the variables a CLI may receive.** `passEnv` is yours to set.

The steps are on the [External sessions](external-sessions.md) page.

## Settings reference

All Jarvis settings are in `settings.json` under `jarvis`. Per-project exposure is set per project. Settings values are the defaults.

| Setting | Default | What it does |
|---|---|---|
| `jarvis.enabled` | `false` | Turns Jarvis mode on. |
| `jarvis.model` | `null` | Model profile for Jarvis. `null` follows `models.default`. |
| `jarvis.folder` | `~/Snowpea/Jarvis` | Jarvis's working folder. The only folder it writes to. |
| `jarvis.delegation.default` | `staged` | The level with no grant. Only `staged` is accepted. |
| `jarvis.delegation.projectTtlHours` | `24` | Lifetime, in hours, of a project grant given without an expiry. |
| `jarvis.quietMinutes` | `10` | Minutes without your input after which Jarvis returns to a session you typed in. |
| `jarvis.stallMinutes` | `15` | Minutes a fully delegated session may sit still before Jarvis looks at it. |
| `jarvis.exposeBrowser` | `true` | Whether Jarvis reads and instructs browser sessions. When off, it sees only their status. |
| `jarvis.proactive.finished` | `true` | Notice when a session finishes. |
| `jarvis.proactive.error` | `true` | Notice when a session fails or stops with an error. |
| `jarvis.proactive.needsYou` | `true` | Notice when something needs you. |
| `jarvis.proactive.decided` | `true` | Notice when Jarvis decided something for you. |
| `jarvis.briefing.enabled` | `false` | Turns the scheduled briefing on. |
| `jarvis.briefing.schedule` | `0 9 * * 1-5` | Cron expression for the briefing, in the briefing's time zone. |
| `jarvis.briefing.timezone` | `null` | Time zone for the briefing. `null` uses the daemon's zone. |
| `jarvis.briefing.sections` | `sessions`, `waiting`, `todos`, `mail`, `calendar` | What the briefing covers. |
| `jarvis.briefing.instructions` | empty | Extra instructions for the briefing. |
| `jarvis.budget.wakesPerHour` | `30` | How many times Jarvis may be woken in an hour. |
| `jarvis.budget.dailyTokens` | `2000000` | Tokens Jarvis may use per local day. `0` means no cap. |
| `jarvis.messenger.route` | `true` | Sends a bound messenger chat to Jarvis while Jarvis mode is on. |
| `jarvis.messenger.allowGrants` | `true` | Allows delegation grants to be confirmed from a messenger. |
| `jarvis.notify.inApp` | `true` | Shows notices in the app and the terminal UI. |
| `jarvis.notify.os` | `true` | Sends operating-system notifications for notices of normal importance and up. |
| `jarvis.notify.messenger` | `true` | Forwards high and urgent notices to your private messenger chat. |
| `jarvis.external.enabled` | `true` | Allows external agent sessions under Jarvis. |
| `jarvis.external.maxProcesses` | `4` | Most external CLI processes snowpea runs at once (1 to 32). |
| `jarvis.external.idleSec` | `900` | Seconds an idle external CLI process is kept before it stops. |
| `jarvis.external.clis.<id>.enabled` | `true` | Uses this CLI (`claude`, `codex`, `agy` or `cursor`). |
| `jarvis.external.clis.<id>.path` | `null` | Executable path. `null` searches `PATH`. A path you set runs only after you confirm it. |
| `jarvis.external.clis.<id>.defaultMode` | `null` | Mode new or continued sessions start in. `null` uses the CLI's own default. Unrestricted is never a default. |
| `jarvis.external.clis.<id>.passEnv` | `[]` | Environment variable names passed to this CLI, such as an API key variable. |

Two settings are decided by you, not by Jarvis: `jarvis.external.clis.<id>.defaultMode` and `passEnv` can be changed only by you, and snowpea records each change. The blocked-command and protected-folder lists are separate settings under `security`; see [Security lists](security.md).

## Security notes and known limits

Jarvis mode is a beta. These are the limits you should know before you rely on it.

- **One daemon token.** Every snowpea client on the machine (the desktop app, the IDE, the terminal UI, browser hosts and the remote connector) uses the same daemon token file in snowpea's home folder. snowpea cannot yet tell two local clients apart, so any local program that can read that file counts as you. Separate credentials for each client are future work. A sign-in through the remote web listener is different: every such connection is marked as a remote sign-in and is refused the Jarvis, security and remote-access settings and the handing over of guarded kinds (see [Using Jarvis from another computer](#using-jarvis-from-another-computer)).
- **The classifier reads the command line, not what runs inside it.** Jarvis judges an action from the command, its arguments and the file paths it names. It cannot see what a script or a program does once it runs. A script that deletes files can run as a safe command if its command line looks harmless. The security lists check the same command line, so they have the same limit; a line they cannot check is routed to you.
- **External CLIs in unrestricted mode.** snowpea does not control what an unrestricted CLI does. Jarvis can read and summarise such a session, but it cannot instruct it or start one, and its guarded-kind rules and the security lists do not apply inside it. Only you can choose unrestricted, and only for one session at a time.
- **Other tools' record formats can change.** Claude Code, Codex, agy and Cursor agent do not publish stable formats for their session records. A new CLI version can change them. snowpea checks the CLI version and shows an error when it cannot read a session.
- **Browser sessions.** Jarvis can read and instruct browser sessions, but it does not decide their approvals in this release.
- **Windows.** The tmux shared-terminal option is not available on Windows. Processes of external CLIs have no console window of their own, and snowpea ends a CLI's whole process tree together when it stops one. snowpea cleans up leftover external CLI processes after an unclean daemon exit only when it can read the process's start time, which it cannot on Windows, so such leftovers stay until you end them yourself.
- **Jarvis cannot fetch a URL.** It searches the web with short queries only. It cannot open a link that you paste.
- **Jarvis cannot read snowpea's own files.** Its reads exclude snowpea's home folder. Searches leave out lines from secret files.
- **A guarded action can still go wrong.** A grant you confirm is applied exactly as shown. Read the scope and the guarded kinds before you press Confirm.
