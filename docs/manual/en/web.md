# snowpea web

`snowpea web` opens the agent in your browser. It is the same screen as the snowpea IDE — the same sidebar, conversation, trajectory and dock — served by your local daemon, so there is nothing else to install and nothing leaves your machine.

```bash
snowpea web            # start the daemon if needed, print the address, open it
snowpea web --no-open  # print the address only
```

The address looks like `http://127.0.0.1:<port>/web/?token=…`. The token is the daemon's own (the one in `$SNOWPEA_HOME/daemon.json`); the page keeps it for that browser tab and takes it out of the address bar. Anyone who has the token can drive the agent, so do not paste the address into chats or tickets. The daemon listens on the loopback address only; to use snowpea web from another machine, tunnel the port over ssh (`ssh -L <port>:127.0.0.1:<port> host`).

## The screen

The layout follows the DeepSeek Harness web client: three columns, with the conversation in the middle.

| Column | What is in it |
|---|---|
| Left — sidebar | **New Session**, Plugins, Automation tasks, More (the other screens: skills, schedule, settings…), then **Workspaces**: one group per project folder with its conversations, pinned ones first. A dot on a row says a turn is running, an approval is waiting, or there is something unread. The row menu pins, renames, marks read, forks, copies the id or archives. |
| Middle — conversation | A header with the conversation's title, the parents it was delegated or forked from, and two tabs: **Chat** (the messages, tool cards and composer) and **Trajectory** (see below). |
| Right — dock | Tabs that open next to the conversation: files, the workspace file tree, the plan, delegated work, changes and, in the IDE, terminals. Panes split and tabs move between them; the layout is kept per conversation in this browser. |

The sidebar is 280 px wide (drag its edge between 264 and 420), folds to a rail under 1024 px and becomes a drawer under 768 px; `Ctrl/⌘ B` toggles it. The dock opens at 45 % of the window, keeps the width you give it, never takes more than 70 %, and closes rather than leave the conversation less than 400 px; `Ctrl/⌘ J` toggles it.

## Files open in the dock

A file the conversation mentions opens as a tab in the dock, not in a new window:

- a markdown link to a file (`[math.ts]` followed by `(math.ts)`), a relative or absolute path, a Windows path;
- a line or a range: `#L24`, `#L24-L30`, `:24`, `:24-30`;
- inline code such as `` `src/app.ts` ``, but only when it names a file the turn read or wrote;
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
- the native folder picker — add a workspace by typing its path;
- speaking replies aloud.

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
