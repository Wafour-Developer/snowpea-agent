# M18 — snowpea web and the IDE on it (M16 Phases 4–5)

Status: Phases 4–5 done 2026-11-15 except M17 2f (delta pruning). Parent plan: [m16-dsh-adoption.md](m16-dsh-adoption.md).

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

| Step | What | Status |
|---|---|---|
| 4.1 | Web bridge + web build target (`vite.web.config.ts` → `out/web`) | done (IDE b51e8ca) |
| 4.2 | Daemon serves the web build (`snowpea web`, static route behind the token) | done (core 45c3eef, wheel c776123) |
| 4.3 | Frame and left sidebar on the new RPCs | done |
| 4.4 | Right dock: engine port, tab registry, document preview, files, plan, changes | done; file reads in the browser go through `fs.*` (`src/web/webFs.ts`) |
| 4.5 | Conversation header, Chat / Trajectory tabs, file references into the dock | done (IDE e486d91) |
| 4.6 | HTML preview modes | done (static by default, "run scripts" toggle) |
| 5.1 | IDE switches to the new layout; inspector removed; settings move behind the sidebar | done; terminal and changes are dock tabs, Ctrl/⌘ J toggles the dock, e2e suite on the new layout |
| 5.2 | Pop-out windows | done (IDE: sidebar "Open in new window", ⧉ on dock tabs; approvals stay in the main window) |
| 5.3 | Clients replay turns from `step.done`; then M17 2f drops stored deltas | clients done (TUI, IDE, browser); 2f pruning not yet enabled |

### Shipping the web build

The web build lives in snowpea-ide. A core release copies it into the package:

```bash
(cd ../snowpea-ide && npm run build:web)
rm -rf core/snowpea_core/web_dist && cp -r ../snowpea-ide/out/web core/snowpea_core/web_dist
uv build    # web_dist is git-ignored and listed under the wheel's artifacts
```

The IDE pin of the protocol (`src/shared/protocolVersion.ts`) must match the core being released,
the same rule as for the desktop bundle.

Every step: unit tests (vitest), a scripted screenshot scene in Korean and English, compared
side by side with the dsh reference where one exists.

## Remote access

Opt-in second listener for using snowpea web from another machine (`web.remote.*` in
`settings.json`, user guide in `docs/manual/en/web.md`). The loopback listener is unchanged: the
page still gets `?token=` there, and `/web/session`, `/web/login`, `/web/logout` answer `404` on
it so the token never leaks through a new path. No JSON-RPC change (protocol stays 1.50.0).

The remote listener (`web.remote.host:port`, default `0.0.0.0:8787`) serves only:

| Request | Answer |
|---|---|
| `GET /web/…` | The static build (same files, SPA fallback to `index.html`). |
| `POST /web/login` `{"username", "password"}` (`Content-Type: application/json`, body ≤ 4 KiB) | `200 {"ok": true}` + `Set-Cookie: snowpea_web=<32-byte random>; HttpOnly; SameSite=Strict; Path=/; Max-Age=<sessionHours>` (`Secure` when the request is HTTPS, or `X-Forwarded-Proto: https` with `trustProxy`). Wrong username or password: `401 {"ok": false, "error": "invalid_credentials"}` after ~300 ms. Locked out (5 failures per IP or 20 in total within 5 min; every attempt that does not sign in counts, malformed ones too): `429 {"ok": false, "error": "rate_limited"}` with `Retry-After: <s>`. Malformed body: `400 {"error": "bad_request"}`; too large: `413`; a cross-origin `Origin`: `403 {"error": "bad_origin"}`. |
| `GET /web/session` | Valid cookie: `200 {"ok": true, "token": "<daemon token>", "username": "…"}`; otherwise `401 {"ok": false, "error": "unauthorized"}`. Always `Cache-Control: no-store`. |
| `POST /web/logout` | `200 {"ok": true}`, clears the cookie, drops the session and closes its sockets (code 4401). |
| `GET /ws` | Upgrade only with a valid cookie (`401` otherwise) and an `Origin` equal to the request's own `scheme://host` or listed in `web.remote.allowedOrigins` (`403` otherwise, also when `Origin` is missing). Then `system.hello` with the token from `/web/session`, as on loopback. |

Anything else is `404` (no `/health`, `/version`, `/protocol.json`). Every response carries
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and a
CSP equal to the page's meta CSP with `connect-src 'self'` and `frame-ancestors 'none'`.

Client flow: on load, `GET /web/session`; `200` → hello with `token`; `401` → show a sign-in form,
`POST /web/login`, then `/web/session` again; on `429` show the `Retry-After` wait; a socket closed
with 4401, or a later `401`, goes back to the form. A `?token=` in the address still wins
(loopback).

Sessions live in daemon memory (lost on restart) and expire after `sessionHours`; settings apply
on daemon restart. `snowpea web status` reads the daemon's view from the loopback-only
`GET /web/remote/status` (`Authorization: Bearer <daemon token>`).
