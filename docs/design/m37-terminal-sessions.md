# M37: terminal sessions, CLI hook integrations, web tunnel

Status: approved by the owner 2026-11-24 ("계획 나오면 바로 진행해").
Protocol: **1.73.0**, additive only (1.71/1.72 are m36). Every new method sits behind a capability, and older clients keep working.
Reference: herdr (Apache-2.0), reviewed at source level. See `/mnt/data/work/mediagen/branch_job/ref-herdr/notes/herdr-review.md` (with part-integrations.md and part-app.md). Where code or data (hook scripts, screen rules) is ported, keep the Apache-2.0 attribution in `THIRD_PARTY_NOTICES.md` ("Portions derived from herdr, Apache-2.0") and in a header comment of each derived file.

## 1. Goal (owner's words, condensed)

- External sessions (Claude Code, Codex, agy, Cursor agent) run as **real terminal sessions**, like herdr. When an external session is opened in IDE or web, a terminal pane takes the place of the chat view, and the CLI runs in it.
- A snowpea session can also run as the **snowpea CLI in a terminal**. A setting makes terminal the default view.
- The launch dialog has checkboxes for each CLI's yolo / dangerous permission flags.
- **Jarvis manages these terminal sessions.**
- Hook integrations get **install / uninstall** for each CLI, the same way herdr does it.
- **Web tunnel**: when web access from outside is on, an ngrok or Cloudflare tunnel can be chosen, and the public URL is made automatically.

## 2. Architecture

```
xterm.js (IDE / web / pop-out)  ──ws JSON-RPC──  core daemon: TerminalManager ── unix socket / named pipe ── holder process ── PTY ── CLI
                                                   │  pyte screen + scrollback, OSC 7501 parser, status machine,
                                                   │  input gate, screen rules, hook reports
Jarvis tools / intake  ─────────────── in-process ─┘
```

### 2.1 Holder (`core/snowpea_core/terminal/holder.py`)

There is one holder process per terminal. It is started detached: `start_new_session` on Unix, `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows. Because of this, terminals survive a daemon restart or upgrade.

- **Holder job:**
  - Owns the PTY and is the CLI's parent.
  - Always drains output and keeps the last 1 MiB.
  - Sends the exit code only after the last output.
- **PTY:**
  - Unix: `os.openpty` + `setsid` + `TIOCSCTTY` (`pty_unix.py`).
  - Windows: ConPTY via `pywinpty` (`pty_win.py`). Add the optional dependency `pywinpty>=2; sys_platform == "win32"`.
  - The holder runs inside a job object that kills the process when the holder dies.
- **Link to the core:**
  - Unix: socket `$SNOWPEA_HOME/terminals/<id>.sock`. The directory is mode 0700 and the socket 0600.
  - Windows (as built, T1): a loopback TCP port, `127.0.0.1` only, ephemeral, kept in the 0600 meta file as `tcp:127.0.0.1:<port>`. The 32-byte token is compared with `hmac.compare_digest` and the connection is closed on the first bad frame. This replaces the planned named pipe with a current-user ACL: the Python standard library has no ACL'd pipe server without pywin32.
  - In both cases the core sends a 32-byte token from the meta file in a HELLO frame first.
  - Frames are `[kind u8][len u32][payload]`. Kinds:

    | Kind | Meaning |
    |---|---|
    | HELLO | first frame, carries the token |
    | DATA (out) | PTY output |
    | INPUT | input to the PTY |
    | RESIZE | new size |
    | SIGNAL | hup / term / kill / int |
    | EXIT | exit code |
    | TAIL | the request on a reattach |
- **Meta file:** `$SNOWPEA_HOME/terminals/<id>.json` holds the spec, pid, socket, token, created time, labels and parent.
- **When the daemon starts:** it reconnects to every live holder and rebuilds the screen from TAIL. Meta files of dead holders become "lost" entries that can be resumed (§2.8).
- **Kill sequence:** HUP, then TERM after 500 ms, then KILL after another 500 ms, then a 2 s final wait. Signals go to the process group.

### 2.2 Screen (`terminal/screen.py`)

- Uses `pyte` (pure Python; add the dependency). It keeps a HistoryScreen of 10 000 lines and is fed every byte.
- It tracks the modes viewers need:
  - 2004 bracketed paste
  - 1049/47 alternate screen
  - ?1 application cursor
  - ?25 cursor visible
  - mouse modes
  - the title (OSC 0/2)
- `repaint() -> bytes` produces a full ANSI repaint for a new viewer:
  - RIS;
  - the mode set;
  - the scrollback lines as SGR text;
  - the screen;
  - the cursor position;
  - the title.
- `text(lines=N) -> str` returns plain text with no ANSI: the screen plus N history lines. It is used by Jarvis and by the screen rules.
- The core answers terminal queries itself only when no human viewer is attached; otherwise the viewer's xterm answers them:
  - DA1/DA2
  - DSR 6n cursor position
  - OSC 10/11 colour queries
  - kitty keyboard query `CSI ? u`

  The headless size is 160x48. While no viewer is attached, without these answers many TUIs hang waiting for a reply.

### 2.3 Status (`terminal/status.py`, `terminal/osc.py`, `terminal/rules/*.toml`)

- **States:** `idle | working | done | blocked | error`, plus `kind` (`permission | question | auth`). The extra fields are `turn` (incremented each time the root record leaves working), `stuck` (set on entering blocked or error), `seen` (whether a person has looked since the last done) and `source`.
- **Sources, highest first:**
  1. OSC 7501 from the program.
  2. A blocker visible on screen (from the rules).
  3. A hook report (`terminal.report`).
  4. The screen rules.
- **OSC 7501 grammar:** `ESC ] 7501 ; key=val(:key=val)* (ST|BEL)`.
  - Keys: `state`, `kind`, `progress`, `id`, `app`, `title`, `msg`. `title` and `msg` are base64.
  - The parser must handle a sequence split across chunks. Unknown states are dropped. A `state=clear` record clears the program's status.
- **Screen rules:** one TOML per CLI: `claude.toml`, `codex.toml`, `cursor.toml`, `agy.toml`.
  - Match types: contains, regex and line regex.
  - Regions: bottom N lines, the whole screen, or the title. The highest priority match wins.
  - Port them from herdr's manifests, with attribution.
  - Timing:
    - re-check every 400 ms while output flows;
    - working → idle needs the idle rule to match on 3 checks 100 ms apart, or 700 ms of quiet;
    - nothing is published for 3 s after launch;
    - a visible blocker is republished every 800 ms.
- **The snowpea TUI emits OSC 7501 itself** (§2.9).
- **Output:** status changes go out as the event `terminal.status`.

### 2.4 Input gate (`terminal/input.py`)

There is one input queue per terminal.

- **Human input:** bytes from a viewer's `terminal.write` (`human: true`) pass straight through. They also update `lastHumanKeyAt`, and the gate tracks whether the person has an unfinished draft (printable keys since the last Enter or Ctrl-C).
- **Agent input:** text from Jarvis, from `terminal.send`, or a first prompt goes through these steps:
  1. **Startup wait.** Within 10 s of launch, wait up to 5 s for 800 ms of quiet output.
  2. **Human-quiet wait.** Wait until the person has been quiet for 3 s, or up to 60 s while their draft is unfinished. While the agent's input is going in, the person's keystrokes are held and flushed afterwards.
  3. **Paste.** Paste the text, bracketed only when mode 2004 is on. Strip any embedded `ESC[201~`.
  4. **Gap.** Wait 200 ms.
  5. **Submit.** Press the profile's submit key, encoded for the current keyboard mode. The default is `\r`.
  6. **Watch.** For 5 s, watch for `working`. Report `accepted: true|false`.
- **Blocked terminals:** sending to a terminal in `blocked` is refused unless `force: true`, and Jarvis never sets force.
- **Keys and interrupt:** `keys` sends named keys 30 ms apart. `interrupt` sends Esc to a program that reports status, otherwise Ctrl-C.

### 2.5 Viewers, size and flow control

- **Attach:** `terminal.attach {terminalId, mode: "interactive"|"observe"}` returns `{repaint (base64), seq, cols, rows, status, title, info}`.
- **Data events:** after attaching, the connection alone receives `terminal.data {terminalId, seq, data (base64)}`. These events are never broadcast.
- **Credit:** each viewer has 256 KiB of credit, replenished by `terminal.ack {terminalId, bytes}`. A viewer more than 4 MiB behind gets a `terminal.data {repaint: true}` instead of the backlog.
- **Size:** the most recently active interactive viewer sets the size. Observers never resize. With no viewer attached, the headless size applies.
- **Detach:** a connection that closes is detached automatically. Closing the viewer never kills the terminal; only `terminal.close` does.
- **Observe is not a boundary** (security review L7): an observer's own write and resize get `unavailable` with reason `observe_only`, but the same person may attach again interactively, and a connection that never attached may still write. Who may type into a terminal is decided by the access rules (people only, the tunnel unlock), never by the attach mode.

### 2.6 Profiles and launch (`terminal/profiles.py`)

Profiles: `shell`, `snowpea`, `claude`, `codex`, `agy`, `cursor`. Each profile defines:

- the argv for a new session and for resuming a session id;
- the submit, interrupt and stop keys;
- its rule file;
- its integration id;
- its **permission options**.

**Permission options**

- The source of truth is `external/modes.py`, the NativeMode table with its `launch={"bypass": …}`.
- Each option is `{id, label, args, dangerous: bool}`. Examples:
  - claude: `acceptEdits`, `plan`, and `bypass` = `--dangerously-skip-permissions` (dangerous).
  - codex: `--full-auto` (not dangerous), and `yolo` = `--dangerously-bypass-approvals-and-sandbox` (dangerous).
  - agy: its bypass flag.
  - cursor: `--yolo` / `--force` (dangerous).
- Verify every flag against the installed CLI's `--help` before shipping. Drop any flag a CLI doesn't have.
- `terminal.profiles` returns the profiles, their options and whether each CLI is installed.

**Dangerous options**

- They reuse the existing grant path: `confirm_person` (a human connection only) → `grant_unrestricted` (with a ledger entry) → `bypass_args`. The request must carry `confirmDangerous: true`, which the UI sets only after a confirm dialog.
- They are refused:
  - from Jarvis or any tool;
  - when `web.remote.maxMode` is below `auto`;
  - for a connection over the tunnel without a terminal unlock (§2.10).
- `assert_safe_args` still guards every other path. Only the grant object lets those flags through.
- A terminal with a dangerous option shows `dangerous: true` everywhere: a red badge in the UI, and Jarvis never answers prompts in it.

**How the CLI is started**

- argv: `[$SHELL, "-ic", '"$0" "$@"; exec "$SHELL" -i', <absolute CLI path>, args…]`. The CLI runs inside the person's interactive shell, so PATH, nvm and similar setups work, and the pane drops back to a shell when the CLI exits (setting `terminals.keepShell`, default true).
- The CLI path is the absolute path the person already confirmed (`confirmedPath`).
- On Windows: `[cli path, args…]` directly; if `keepShell` is on, the CLI runs under `cmd /k` or `pwsh -NoExit`.

**Environment**

- The person's environment, minus daemon tokens and `SNOWPEA_*` secrets.
- Added variables:
  - `TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=snowpea`
  - `SNOWPEA_TERMINAL_ID`
  - `SNOWPEA_TERMINAL_REPORT` (the path of the report socket)
  - `SNOWPEA_TERMINAL_TOKEN` (a per-terminal token that may only call `terminal.report` for its own terminal)

**Working folder and limits**

- The working folder must be a registered project, or the session workspace for `noProject`.
- At most `terminals.max` terminals (default 16).

### 2.7 Hook integrations: install / uninstall (`terminal/integrations/`)

This follows herdr's integration code (H/integration/*, see the notes).

**Commands**

- CLI: `snowpea integration install|uninstall|status [claude|codex|cursor|agy|all]`.
- RPCs: `integration.list` and `integration.install|uninstall {cli}`. The last two accept a human connection only and are listed in `settings_gate`.

**Hook script**

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh`, plus `.ps1` for Windows.
- It carries `SNOWPEA_INTEGRATION_ID` and `SNOWPEA_INTEGRATION_VERSION` lines.
- It exits 0 immediately when `SNOWPEA_TERMINAL_ID` is unset, so it is harmless outside snowpea terminals.
- It calls `snowpea-core hook report …` (a fast path, stdlib only). That command sends `{terminalId, token, event, state, sessionId, transcriptPath}` to the report socket.

**Targets**

Take the exact files and events from herdr's targets.rs.

| CLI | Config | Events |
|---|---|---|
| Claude | `settings.json` (`$CLAUDE_CONFIG_DIR` or `~/.claude`) | SessionStart (session id and transcript path), UserPromptSubmit → working, Stop → done, Notification → blocked |
| Codex | `hooks.json` and `[features] hooks = true` in `config.toml` (`$CODEX_HOME` or `~/.codex`) | as supported by Codex |
| Cursor | `~/.cursor/hooks.json` | `sessionStart` (and whatever else Cursor supports) |
| agy | `~/.gemini/config/hooks.json` | `PreInvocation` (and others) |

**Edit rules**

- Our entries are found by exact match on the command string; no marker key is written into the person's config. Only those entries, and any groups they leave empty, are removed.
- Writes keep the file's formatting: minimal edits, then a re-parse check.
- Each write is atomic (temp file then rename), keeps the file mode, and refuses hard-linked files.
- A repeat install changes no bytes.
- Before the first change to a file, save a backup once: `<file>.snowpea-backup-<date>`.
- Record what we added in `$SNOWPEA_HOME/integrations/state.json`. This includes, for example, whether we turned on Codex's `features.hooks` flag. Uninstall undoes exactly what we added, unlike herdr, which leaves that flag on.

**Status values**

`not-installed`, `current`, `outdated` and `modified` (our entry is there but was edited).

**What the hooks give us**

- The CLI's session id, so a terminal can be resumed and the transcript reader can feed Jarvis.
- State, which ranks below a blocker on screen (§2.3).

### 2.8 External sessions as terminals

- **Setting:** add `jarvis.external.view: "terminal" | "chat"`, default `"terminal"`.
- **Opening:** `external.open` / `external.create` gain `transport: "pty"`, `options: [ids]` and `confirmDangerous`. New clients pass `pty` when the view setting is terminal. Old clients don't pass it and keep today's structured transport, so nothing changes for them.
- **Session rows:** `ExternalSessionRow.terminal.kind` gains `"pty"` along with `terminalId` and `status`.
- **Resume:** opening an existing CLI session as a terminal starts the profile's resume argv: `claude --resume <id>`, `codex resume <id>`, the agy and cursor equivalents (verify each against `--help`).
  - A session that is live in another terminal: view-only, as today.
  - A holder lost after a reboot: the row offers "resume" using the session id from the hooks.
- **Transcript readers** stay as they are: Jarvis's `external_read` keeps working, now with the hook-provided session id.

### 2.9 snowpea sessions in a terminal

- **TUI attach:** a new TUI flag `--session <id>` attaches to an existing daemon session with `session.resume` instead of creating one. `launch_tui` and `tui/src/index.tsx` pass it through.
- **Status:** the TUI emits OSC 7501:
  - working while a turn runs;
  - blocked with kind=permission or question while an approval or question is open;
  - done when a turn ends;
  - idle when no turn is running.
- **Profile `snowpea`:** `snowpea --session <id> --cwd <workdir>` against the same daemon (port and token from the daemon's discovery file, never from the argv shown in `ps`; use env or the discovery file).
- **Default view:** setting `ui.sessionView: "chat" | "terminal"`, default `"chat"` and person-local. Each session also has a toggle. Both views can be open at the same time, because both are clients of the same session.
- **Raw attach command:** `snowpea term ls | attach <id> | read <id> | send <id> <text>`. `attach` puts the TTY in raw mode and detaches on Ctrl+\.

### 2.10 Web tunnel (`server/tunnel.py`)

**Settings and credentials**

- Setting: `web.remote.tunnel = {provider: "off" | "cloudflare-quick" | "cloudflare" | "ngrok", autoStart: true, hostname?: str, binary?: str}`.
- Tokens live in `credentials.json`:
  - ngrok authtoken;
  - Cloudflare tunnel token.
  
  Set them with `snowpea web tunnel token --provider …` (stdin) or the RPC `web.tunnel.setToken`, which accepts a human local connection only.

**Binary**

- Look in `binary` first, then PATH, then `$SNOWPEA_HOME/bin`.
- `web.tunnel.install` downloads a **pinned version with a pinned sha256** for each platform into `$SNOWPEA_HOME/bin`. If no checksum is pinned for the platform, it refuses and shows the install command instead.

**Run**

| Provider | Command | URL |
|---|---|---|
| cloudflare-quick | `cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<port>` | `https://*.trycloudflare.com` parsed from its log |
| cloudflare | `cloudflared tunnel --no-autoupdate run` (token in env `TUNNEL_TOKEN`, never on the command line, so it is not visible in `ps`) | `https://<hostname>` |
| ngrok | `ngrok http 127.0.0.1:<port> --log stdout --log-format json` (authtoken via env `NGROK_AUTHTOKEN`) | `url` parsed from the JSON log, falling back to `127.0.0.1:4040/api/tunnels` |

- If the tunnel drops, it is restarted with backoff.
- The URL goes out in the event `web.tunnel.changed {state, provider, url, error}` and is returned by `web.tunnel.status`.

**Preconditions**

`web.remote.enabled` must be on and a web password must be set. Otherwise the tunnel refuses with a clear message.

**Origins and proxy headers**

- The tunnel's origin is added to the allowed origins at runtime.
- Forwarded headers (`CF-Connecting-IP`, `X-Forwarded-For`) are trusted only from a peer on 127.0.0.1 while the tunnel runs.

**Connections over the tunnel**

- A connection whose Host is the tunnel host is marked `viaTunnel`.
- On such a connection, every terminal call (create, attach in interactive mode, write) needs a **terminal unlock**: `web.tunnel.unlock {password}` re-enters the web password, and the unlock lasts for that sign-in session.
- Dangerous options are refused unless the connection is unlocked and `maxMode` is `auto`.
- Observe-attach is allowed after sign-in.

**IDE settings**

Provider picker, token field, start/stop, the URL with a copy button and a QR code, and a status line.

### 2.11 Jarvis

**Tools** (Jarvis session only)

| Tool | What it does |
|---|---|
| `terminal_list` | lists terminals |
| `terminal_read` | `{id, lines≤400}` → screen text, status, title |
| `terminal_send` | `{id, text}` through the input gate, never force |
| `terminal_keys` | named keys |
| `terminal_interrupt` | interrupts the terminal |
| `terminal_wait` | `{id, timeout≤180, output?}`; ends on leaving working, on exit, or on an output match after the call |
| `terminal_create` | `{profile, cwd, prompt?}`; never dangerous options; the terminal is labelled `parent=jarvis` |

**Intake**

Intake subscribes to `terminal.status` and `terminal.exit`.

- **What wakes Jarvis:** a delegated terminal that enters blocked or error, or reaches done, wakes Jarvis through the existing batching and budget. Other terminals go into the 30 s digest.
- **Overview rows:** each terminal appears with its status, title, a seen flag and its last lines.
- **Child notes:** when a child terminal of Jarvis is blocked, errors or exits, Jarvis gets an event note `<terminal from="t3 codex">…</terminal>` with its last 20 lines. Notes are events, never typed into a PTY.

**Prompts inside a terminal** (a blocked state with kind=permission)

- Jarvis may answer only when all of these hold:
  - the terminal is under full delegation;
  - it is not dangerous;
  - the screen rule extracted the command text;
  - the delegation engine classifies it safe or risky, not an exception.
- Everything else goes to the person.
- Answers go through `terminal_keys`, and every decision is written to the ledger.

### 2.12 IDE / web

**Terminal store**

`stores/terminal.ts` gets a core backend (terminal.* through the SDK) next to the existing node-pty backend.

- `TerminalPane.vue` works with either backend.
- The web build uses the core backend.
- `terminal` is removed from `WEB_HIDDEN_ROUTES` for core-backed terminals. The local shell stays Electron-only.

**External sessions**

- `ExternalTab.vue`, `/external/:cli/:id` and `PopOut.vue` render `TerminalPane` when `row.terminal.kind == "pty"`, and `ExternalChat` otherwise.
- `NewExternalDialog`:
  - CLI, project and view (terminal/chat);
  - permission option checkboxes. Dangerous ones are red and need a confirm dialog that names the risk;
  - disabled with a reason when over the tunnel without an unlock, or when maxMode is below auto.
- `LoadExternalDialog` resumes a session in a terminal.

**snowpea sessions**

- A chat/terminal toggle in the session header.
- The default comes from `ui.sessionView`.
- The terminal view runs the `snowpea` profile attached to that session.

**Sidebar and alerts**

- Each terminal or external session gets a status dot: needs you (blocked/error), done (not yet seen), working, idle.
- The project row rolls these up in the order blocked > done > working > idle.
- Entering blocked or error plays a sound and shows a toast. Done does the same only when the person isn't looking. Both are switchable in settings.
- alt+↑/↓ jumps to the next terminal that needs you.

**Settings pages**

- Terminal: default views, keepShell, sounds.
- Integrations: install / uninstall / status for each CLI, with the files each one touches.
- Tunnel.

**Jarvis view**

Lists terminals with their status, and a click opens the pane.

### 2.13 Protocol 1.73.0 (additive)

**Methods**

- `terminal.profiles`, `terminal.list`, `terminal.create`, `terminal.attach`, `terminal.detach`, `terminal.write`, `terminal.resize`, `terminal.ack`, `terminal.read`, `terminal.send`, `terminal.keys`, `terminal.interrupt`, `terminal.wait`, `terminal.close`, `terminal.seen`, `terminal.unlock`
- `terminal.report` (hook; token auth; also on the report socket)
- `integration.list`, `integration.install`, `integration.uninstall`
- `web.tunnel.status`, `web.tunnel.start`, `web.tunnel.stop`, `web.tunnel.setToken`, `web.tunnel.install`, `web.tunnel.unlock`

**Events**

- `terminal.data` (to attached viewers only)
- `terminal.status`, `terminal.created`, `terminal.exit`, `terminal.title`, `terminal.closed`
- `integration.changed`
- `web.tunnel.changed`

**Types**

`TerminalInfo`:

```
{terminalId, profile, title, cwd, cols, rows,
 status {state, kind?, turn, stuck, seen, source},
 alive, pid?, exitCode?, parent?, labels,
 external? {cli, sessionId?}, snowpeaSessionId?,
 options [ids], dangerous, createdAt}
```

Also `TerminalProfile`, `TerminalOption`, `IntegrationRow`, `TunnelStatus`.

**Fields on existing types**

- `ExternalOpenParams` / `ExternalCreateParams`: `transport` gains `"pty"`; new `options` and `confirmDangerous`.
- `ExternalTerminal.kind` gains `"pty"`; new `terminalId` and `status`.
- Settings keys: `terminals.*`, `ui.sessionView`, `jarvis.external.view`, `web.remote.tunnel`.

**Capabilities**

`terminalSessions`, `terminalStatus`, `cliIntegrations`, `externalPty`, `tuiSessionAttach`, `webTunnel`. Each stays in `PENDING_CAPABILITIES` until its implementation lands.

## 3. Work packages

All branches start from `m37-terminal` (the integration branch, worktree `branch_job/snowpea-m37-terminal`). The lead merges each one into `m37-terminal`, then `m37-terminal` into `main` after m36.

| WP | Scope | Model | Depends on |
|---|---|---|---|
| T0 | Protocol 1.73 surface: types, methods, events and capabilities as pending; SDK and docs generation; settings models | sonnet | — |
| T1 | Terminal runtime: holder, PTY Unix and Windows, screen, OSC 7501, status machine, input gate, viewers and flow control, manager, RPC handlers, restore on daemon start | opus | T0 |
| T2 | Hook integrations install/uninstall/status, the hook script, `hook report` fast path and report socket, screen rules TOML + engine | opus | T0 (engine interface only) |
| T3 | Profiles and permission options (grant path), external `pty` transport and resume, the snowpea profile, Jarvis tools and intake | opus | T1 |
| T4 | Web tunnel | sonnet | T0 |
| T5 | TUI `--session` attach, OSC 7501 emit, `snowpea term` subcommands | sonnet | T0 (term subcommands need T1) |
| T6 | IDE / web: core terminal backend, pane switch, dialogs, sidebar status, alerts, settings pages, Jarvis view | opus | T0 SDK types; real wiring after T1/T3 |
| T7 | Docs (en/ko/ja/zh/es manual, protocol, beta labels), e2e (Playwright with a fake CLI that emits 7501 + real `bash`), security review (read-only) | haiku / opus | all |

## 4. Tests that must exist

- **Holder:**
  - survives a daemon restart (a test daemon with a throwaway home);
  - exit code arrives after the last output;
  - the kill sequence;
  - the token is refused when wrong.
- **Screen:** repaint round-trip (feed bytes → repaint → a fresh pyte gives the same screen); alt-screen; a 7501 sequence split across chunks.
- **Status:**
  - the source precedence;
  - the debounce timings, with a fake clock;
  - the turn counter, stuck and seen flags.
- **Input gate:**
  - human quiet and draft hold;
  - bracketed paste only when 2004 is on;
  - ESC[201~ stripped;
  - blocked is refused;
  - the submit key.
- **Flow control:** credit, and a repaint when a viewer falls behind.
- **Integrations, for each CLI with fixture configs:**
  - install is idempotent: identical bytes on a repeat install;
  - uninstall restores the original bytes, including Codex's flag;
  - formatting is preserved;
  - hard links are refused;
  - CLAUDE_CONFIG_DIR and CODEX_HOME are honoured;
  - a status of modified is detected.
- **Profiles:**
  - a dangerous option is refused without the grant, from Jarvis, over the tunnel without an unlock, and with maxMode below auto;
  - argv is built as a list;
  - the environment has no daemon token.
- **Tunnel:** URL parsing from fixture logs of each provider; the restart backoff; refusal without a password; the unlock gate.
- **Compatibility:** an old client (1.70) still gets structured external sessions; `gen_protocol --check`; the capability list.
- **e2e:**
  - IDE: open an external fake CLI as a terminal, type into it, see the status dot change;
  - toggle a snowpea session to terminal;
  - web build: the terminal pane works.
