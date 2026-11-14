# M18 — snowpea web and the IDE on it (M16 Phases 4–5)

Status: in progress, 2026-11-14. Parent plan: [m16-dsh-adoption.md](m16-dsh-adoption.md).

## One codebase

The IDE's Vue renderer (`snowpea-ide/src/renderer`) already reaches the daemon through one
interface, `SnowpeaBridge` (`src/shared/bridge.d.ts`): `call`/`on`/`onRequest`/`onReconnect`
carry JSON-RPC, and the rest (files, git, terminals, dialogs, attachments) are local functions
the Electron main process provides. So snowpea web is that renderer built for a browser with a
**WebSocket bridge**, and the IDE is the same renderer inside Electron with the IPC bridge. One
set of components, stores, tests and translations; the new layout is built once.

| | IDE (Electron) | snowpea web (browser) |
|---|---|---|
| JSON-RPC | IPC → main → WebSocket | WebSocket from the page (`../snowpea/sdk` client) |
| Files | main process `fs*` | daemon `fs.*` RPC (capability `fs`, 1.40) |
| Git, terminal, folder picker, OS open | main process | hidden when absent (capability check on the bridge) |
| Served by | the app bundle | the daemon: `snowpea web` (token in the URL, like `snowpea remote`) |

## Layout (identical structure to the dsh web client)

Reference: [m16 §5](m16-dsh-adoption.md#5-ui-parity-spec-phases-45), dsh `packages/client/ui-*`,
the 77 text snapshots in dsh `snapshots/web/*/ui.expected.md`, and reference screenshots captured
from a local dsh build (kept outside the repo: they carry DeepSeek branding).

- **Frame** (`AppFrame.vue`): left sidebar 280 px (264–420, rail 56, collapses under 1024 px,
  drawer under 768 px, Mod+B), main, right column (opens at 45 %, keeps the user's width, max
  70 %, shrinks to 300 px to leave 400 px for main, then closes; fullscreen), bottom band,
  overlay.
- **Left sidebar** (`SidebarRoot.vue`): brand row, New Session, panel list (Plugins,
  Automation, More), Workspaces header with search / view options / add folder, workspace
  groups (5 rows, Show more +5, watermark keeps running, unread and open rows visible), pinned
  first, indicators pending > running > unread, row menu (pin, rename, read/unread, fork, copy
  id, reveal, archive), settings at the bottom. Data: `workspace.*`, `session.list`,
  `session.pin`, `session.archive`, `session.fork`.
- **Conversation**: header with lineage breadcrumbs (`session.lineage`) and **Chat /
  Trajectory** tabs; Chat reuses the message stream and tool cards; Trajectory renders
  `session.trajectory` rows with turn and step bands and per-call timing; composer at the
  bottom (mode, model, attachments, plan chip).
- **Right dock** (`dock/`): a split tree of tabbed panes with floating panels, one layout per
  session saved locally; tab types guide (Start: Workspace files, New terminal, More tools),
  document preview (code, markdown, html, image, pdf, sheet, office, notebook — the existing
  previewers), files, plan, changes review, terminal (IDE), subagent chat;
  `openResource(address, {line})` focuses an existing tab for the same address.
- **File references**: markdown links and paths (`./x`, absolute, Windows, `#L24`, `#L24-L30`,
  `:24`, `:24-30`) open in the right dock; inline code only when it names a file the turn
  touched.
- **HTML preview**: static mode (sanitised, CSP, no scripts) and script mode (Blob iframe,
  `sandbox="allow-scripts"` only, relative JS/CSS inlined with caps 4 MiB / 32 MiB / 64 assets).
- **Multi-window** (IDE): a session or a dock tab pops out into its own window; state comes
  from the daemon, so windows agree.

Ported from dsh as code (MIT, noted in `THIRD_PARTY_NOTICES.md`): the dock engine (operations,
planner, geometry, history), the resource-address grammar, tab claim ranking, the file-link
parser, the HTML sandbox recipe, layout numbers.

## Steps

| Step | What |
|---|---|
| 4.1 | Web bridge + web build target (`vite.web.config.ts` → `out/web`) |
| 4.2 | Daemon serves the web build (`snowpea web`, static route behind the token) |
| 4.3 | Frame and left sidebar on the new RPCs |
| 4.4 | Right dock: engine port, tab registry, document preview, files, plan, changes |
| 4.5 | Conversation header, Chat / Trajectory tabs, file references into the dock |
| 4.6 | HTML preview modes |
| 5.1 | IDE switches to the new layout; inspector removed; settings move behind the sidebar |
| 5.2 | Pop-out windows |
| 5.3 | Clients replay turns from `step.done`; then M17 2f drops stored deltas |

Every step: unit tests (vitest), a scripted screenshot scene in Korean and English, compared
side by side with the dsh reference where one exists.
