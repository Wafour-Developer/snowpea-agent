# snowpea web

`snowpea web` opens the agent in your browser. It is the same screen as the snowpea IDE — the same sidebar, conversation, trajectory and dock — served by your local daemon, so there is nothing else to install and nothing leaves your machine.

```bash
snowpea web            # start the daemon if needed, print the address, open it
snowpea web --no-open  # print the address only
```

The address looks like `http://127.0.0.1:<port>/web/?token=…`. The token is the daemon's own (the one in `$SNOWPEA_HOME/daemon.json`); the page keeps it for that browser tab and takes it out of the address bar. Anyone who has the token can drive the agent, so do not paste the address into chats or tickets. This address only works on the machine the daemon runs on.

## From another machine

The simplest way is still an ssh tunnel: nothing else listens, and the connection is encrypted.

```bash
ssh -L <port>:127.0.0.1:<port> host   # then open the printed address locally
```

To open snowpea web from other machines directly — a laptop on the same network, a phone — give `snowpea web` an address and a port. The daemon opens a second listener at once, without a restart; the loopback one keeps working as before.

```bash
snowpea web --host 0.0.0.0 --port 8787           # an access link, no password
snowpea web --host 0.0.0.0 --port 8787 --save    # and again at every daemon start
```

Without a password, other machines open the **access link** it prints, `http://<this machine's LAN address>:8787/web/?t=<token>`. The token is a random 256-bit key of its own — never the daemon's token — kept in `credentials.json` (mode 0600). The first visit trades it for a sign-in cookie and moves the page to the same address without the token, so it does not stay in the browser's history. Anyone with the link can drive this machine: treat it like a password.

```bash
snowpea web token              # print the link again
snowpea web token --rotate     # a new token: the old link stops working, everyone is signed out
```

For a sign-in page instead, set a username and a password. Then the link alone is not enough: the page asks for both.

```bash
snowpea web --host 0.0.0.0 --port 8787 --user me --password-stdin   # one line on stdin
SNOWPEA_WEB_PASSWORD=… snowpea web --host 0.0.0.0 --port 8787 --user me
snowpea web --host 0.0.0.0 --port 8787 --user me --password '…'      # works, but see below
snowpea web password             # set or change it later (asked twice)
snowpea web password --clear     # back to the access link
```

`--password` on the command line works, with a warning: it is visible in your shell history and the process list. Prefer `--password-stdin` or `SNOWPEA_WEB_PASSWORD`. The password is stored only as a salted scrypt hash in `credentials.json`, never in `settings.json`.

`snowpea web status` shows whether remote access is on, the mode (access link or password), the address and the link, the username, whether a password is set, how many people are signed in, and whether `settings.json` says otherwise than what runs (the next daemon start would differ; `--save` keeps what runs). A client with a Remote access settings screen shows the same (read-only on a page that is itself a remote sign-in).

You can still set it in `settings.json` and restart the daemon:

| Setting (`web.remote.…`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Start the second listener. |
| `host` | `0.0.0.0` | Address to listen on (`0.0.0.0` is every interface). |
| `port` | `8787` | Port to listen on. |
| `username` | `""` | The one account that may sign in. |
| `sessionHours` | `12` | How long a sign-in lasts. |
| `allowedOrigins` | `[]` | Extra page origins the WebSocket accepts, e.g. `https://snowpea.example.com` behind a proxy. |
| `trustProxy` | `false` | Believe `X-Forwarded-Proto` / `X-Forwarded-For` from a reverse proxy you run. |

If a password is set without a username, the daemon starts on loopback only, writes why to its log, and `snowpea web` says so.

Security notes:

- **It is plain HTTP.** The access link, the password and everything you do travel unencrypted. On anything beyond a network you trust, put it behind TLS (a reverse proxy such as Caddy or nginx, with `trustProxy: true`) or reach it over a VPN or the ssh tunnel above.
- Whoever signs in can drive the agent, and with it this machine's shell and files. Pick a long password, and keep the link private.
- Every connection through the second listener is a **remote sign-in**, with the link or with a password alike. It is you for running work — chats, approvals, MCP servers — but not for security settings: it cannot change Jarvis, the security lists, remote access itself, the daemon or allowlist rules, open an external session unrestricted, or confirm a CLI path. Do those on the machine itself.
- Five failed sign-ins (wrong passwords and wrong links count together) from one address in five minutes, or twenty from anywhere, lock sign-in for the rest of those five minutes. Sign-ins and failures are logged with the client address, never the password or the token.
- The sign-in is a cookie (`HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS). Sessions live in the daemon's memory: restarting the daemon signs everyone out. Changing or clearing the password, or rotating the token, signs everyone out at once; other sessions last until they expire or you sign out.
- A page whose session has ended (it expired, the password changed, or it was signed out elsewhere) goes back to the sign-in screen.
- The remote listener serves only the page, sign-in and the WebSocket — no `/health`, `/version` or schema — and the WebSocket refuses a page from another origin.

## The screen

The layout follows the DeepSeek Harness web client: three columns, with the conversation in the middle.

| Column | What is in it |
|---|---|
| Left — sidebar | **New Session**, Plugins, Automation tasks, More (the other screens: skills, schedule, settings…), then **Workspaces**: one group per project folder with its conversations, pinned ones first. A dot on a row says a turn is running, an approval is waiting, or there is something unread. The row menu pins, renames, forks, copies the id or archives. |
| Middle — conversation | A header with the conversation's title, the parents it was delegated or forked from, and two tabs: **Chat** (the messages, tool cards and composer) and **Trajectory** (see below). |
| Right — dock | Tabs that open next to the conversation: files, the workspace file tree, the plan, delegated work, changes and, in the IDE, terminals. Panes split and tabs move between them; the layout is kept per conversation in this browser. |

The sidebar is 280 px wide (drag its edge between 264 and 420), folds to a rail under 1024 px and becomes a drawer under 768 px; `Ctrl/⌘ B` toggles it. The dock opens at 45 % of the window, keeps the width you give it, never takes more than 70 %, and closes rather than leave the conversation less than 400 px; `Ctrl/⌘ J` toggles it.

An empty pane in the dock shows a **Start** tab with cards for workspace files, changes and the plan (the terminal card is IDE only). The status bar shows whether the page is connected, and an open tab keeps the daemon from shutting down for being idle.

## Files open in the dock

A file the conversation mentions opens as a tab in the dock, not in a new window:

- a markdown link to a file (`[math.ts]` followed by `(math.ts)`), a relative or absolute path, a Windows path;
- a line or a range: `#L24`, `#L24-L30`, `:24`, `:24-30`;
- inline code such as `` `src/app.ts` ``, but only when it names a file the turn read or wrote;
- a bare file name in the reply, such as `notes.md`, when the turn read or wrote that file. Other bare names stay plain text, and plain text is linked only when it is an explicit http(s), mailto: or www. address.
- the file chips and artifact cards under a reply.

Opening the same file again focuses its tab. The preview is the IDE's: code with highlighting, markdown, images, PDF, spreadsheets, office documents and notebooks. An HTML file is shown **static** by default (sanitised, no scripts, a strict content-security policy); switch it to **run scripts** to load it in an isolated frame that may run its own scripts but cannot reach the page, your cookies or the daemon.

In the browser, files are read through the daemon (`fs.*`), and only inside the conversation's folders: its working directory, its workspace and its attachments. A path outside them is refused, just as the agent's own tools would refuse it.

## Trajectory

The **Trajectory** tab shows what the agent did, step by step, instead of as a chat: each turn is a band, each model call inside it a step with its text, reasoning and tool calls, and each tool call shows its arguments, its result and how long it took. Delegated work appears where it was started; clicking it opens the child conversation in the dock, and a file in a tool call opens in the dock as above. It is built from the session's event log (`session.trajectory`), so it is the same for a running turn, a finished one, or a conversation you resume tomorrow.

## Fork, pin and archive

From a finished turn you can **fork** the conversation: a new conversation starts with the history up to that turn, and the original is left as it is. Its header shows where it came from. Restoring files to before a turn (checkpoints) still works as before; fork is for trying another direction without losing this one.

**Pin** keeps a conversation at the top of its workspace; **archive** hides it from the sidebar without deleting it (the sidebar's view options show archived ones again).

## What the browser cannot do

The page has no access to your operating system, so these stay in the IDE and the terminal UI:

- terminals (Open in terminal, New terminal);
- git actions that run locally, and Reveal in folder / Open with the system app;
- the Terminal, Files and Worktrees screens, the connection manager and app self-update (their addresses return to the conversation);
- the native folder picker — add a workspace by typing its path;

Buttons for them are hidden in the browser rather than failing.

## Where the page comes from

The web build ships inside the snowpea package and the daemon serves it under `/web/`. `index.html` is never cached and every other file has a content hash in its name, so a reload after an upgrade always gets the new version. For development, point the daemon at another build with `SNOWPEA_WEB_DIST`:

```bash
# in snowpea-ide
npm run build:web
# then, with a separate home so your own daemon is not touched
SNOWPEA_WEB_DIST=$PWD/out/web snowpea --home /tmp/snowpea-dev web
```

If the build is missing, `snowpea web` says so and the address answers `404`.

## Next

- [Terminal UI](tui.md) — the same agent in the terminal
- [Protocol](protocol.md) — the RPCs the page uses (`workspace.*`, `session.trajectory`, `session.fork`, `session.lineage`, `fs.*`)
- [Remote sessions](remote.md) — running the daemon on another machine
