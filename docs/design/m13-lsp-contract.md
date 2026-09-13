# M13 — LSP integration (v0.2 core feature)

Status: **binding for story CORE-lsp**. Added 2026-09-13 at the user's request
("v0.2 코어 기능으로 넣어주고 opencode 소스를 거의 채용"). The design ports the
opencode LSP layer (MIT, `packages/opencode/src/lsp/*` at 95daf90, reference
clone `/tmp/opencode-ref`) to the Python core. Ported code keeps the MIT notice
and is recorded in `docs/design/deviations/CORE-lsp.md`.

## 1. Purpose

Give the agent what an editor has: diagnostics right after an edit, precise
definition/reference lookup, symbols and hover, and safe rename — for every
language that has a language server. It replaces "grep and hope" with "the
compiler is watching".

## 2. Layout (core/snowpea_core/lsp/)

| file | port of | role |
|---|---|---|
| `servers.py` | `lsp/server.ts` | registry of `ServerInfo(id, extensions, root_markers, spawn(), initialization)` for: typescript, deno, vue, eslint, biome, oxlint, gopls, ruby-lsp, pyright, ty, elixir-ls, zls, csharp, fsharp, sourcekit, rust-analyzer, clangd, svelte, astro (+ any from opencode not listed). `spawn()` finds the binary on PATH, else (opt-in, `lsp.autoInstall`) installs it the way opencode does (npm/pip/go install into `<SNOWPEA_HOME>/lsp/bin`). Missing server ⇒ `None`, never an error. |
| `client.py` | `lsp/client.ts` | one JSON-RPC 2.0 client over stdio per (server, root): `initialize`/`initialized`, `textDocument/didOpen|didChange|didClose`, `publishDiagnostics` subscription, request helpers with timeouts (default 8 s) and server-crash recovery (restart once, then mark `broken`). |
| `manager.py` | `lsp/lsp.ts` | `LspManager` owned by `Core`: lazy start per root (root = nearest `root_markers` dir of the touched file, else workdir), `touch_file(path, wait_for_diagnostics: bool)`, `diagnostics(path?)`, `definition`, `references`, `document_symbols`, `workspace_symbols`, `hover`, `prepare_rename`/`rename` (returns a WorkspaceEdit; applying it is a `write` action subject to the permission mode), `status()`; idle servers shut down after `lsp.idleTimeoutSec` (default 600). All subprocesses are `create_subprocess_exec` with argv lists (no shell). |
| `diagnostic.py` | `lsp/diagnostic.ts` | pretty-printer for tool output: `ERROR [12:5] message (code)`, capped at 20 per file with "… N more". |
| `language.py` | `lsp/language.ts` | extension → language id map. |

## 3. Behaviour in the agent loop

* After every successful `write_file` / `edit_file` the tool result appends a
  `Diagnostics` block for that file (errors and warnings; max 20 lines) when a
  server is running or can start within 3 s; otherwise nothing — the edit never
  fails because a server is missing. Mirrors opencode `tool/edit.ts:197-200`.
* `lsp.enabled` (default true), `lsp.autoInstall` (default false), `lsp.disabled: [ids]`,
  `lsp.servers: {id: {command: [...], extensions: [...]}}` for user-defined
  servers — all in settings.json, hot-reloaded.
* New tools (permission tag `read`, category `lsp`): `lsp_diagnostics {path?}`,
  `lsp_definition {path,line,character}`, `lsp_references {path,line,character}`,
  `lsp_symbols {path}` / `lsp_workspace_symbols {query}`, `lsp_hover {path,line,character}`;
  `lsp_rename {path,line,character,newName}` (tag `write`; plan mode blocks).
  Descriptions follow the prompt library's usage-rule style (`prompts/tool_descriptions.py`).
* System prompt (base.md "Working in the codebase"): one sentence — "after an
  edit, read the Diagnostics block before moving on; use lsp_references before
  renaming or changing a signature".

## 4. Protocol (additive, 1.4.0 → 1.5.0)

* `lsp.status {}` → `{servers: [{id, root, state: starting|ready|broken|stopped, languageId, pid?}]}`
* `lsp.catalog {}` → `{servers: [{id, languageIds, extensions, installable, installHint?, disabled}]}` —
  every server registered in `lsp/servers.py`, whether or not it has ever started (unlike
  `lsp.status`, which only knows about servers a root has actually spawned). `installable`
  is true when `lsp.autoInstall` could obtain it (npm/pip/go); `installHint` is a manual
  install command for that case (e.g. `"pip install ty"`), `null` for PATH-only servers.
  `disabled` reflects what `lsp.servers.catalog()` would actually start: a default-off id
  (`ty`, `ruff`), one named in `lsp.disabled`, or one an `lsp.servers` override explicitly
  disables. Additive to 1.5.0 — no protocol version bump. Backs a settings UI's LSP card
  (§5) so it can list every server and its status without needing one already running.
* session event `lsp.diagnostics {path, count, errors, warnings}` after each publish so the TUI/IDE can show a badge.
* `tool.list` reports the lsp tools with `state: inactive` + reason when `lsp.enabled=false`.

## 5. Surfaces

* TUI: HUD segment `lsp 2` (ready server count) hidden when none; inline
  Diagnostics rendered like tool output; `/lsp` shows status.
* IDE: Diff review shows the per-file diagnostics count; Settings → Tools has an
  LSP card (enabled, auto-install, per-server disable).

## 6. Acceptance (AC-43…47)

* AC-43: editing a `.py` file with a deliberate type error in a workdir where
  `pyright` is on PATH yields a `Diagnostics` block in the `edit_file` result
  within 5 s; with `lsp.enabled=false` the result is unchanged.
* AC-44: `lsp_references` on a symbol in the fixture project lists every use
  across files; `lsp_definition` jumps to the right file:line.
* AC-45: a server that crashes is restarted once and, on a second crash,
  reported `broken` in `lsp.status` without breaking edits.
* AC-46: no shell strings — every spawn is argv; `autoInstall=false` never
  downloads anything.
* AC-47: TypeScript fixture (typescript-language-server) passes the same checks
  as AC-43/44 when the server is installed; tests skip cleanly otherwise.

## 7. Out of scope (v0.2)

Code actions/formatting, semantic tokens, multi-root workspaces beyond one root
per server, remote (ssh/docker) backends (the manager runs on the daemon host only).
