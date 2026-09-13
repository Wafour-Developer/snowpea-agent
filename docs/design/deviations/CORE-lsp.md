# Deviations — CORE-lsp (LSP integration, M13)

Implements `docs/design/m13-lsp-contract.md`. The layer is a port of opencode's
`packages/opencode/src/lsp/{server,client,lsp,diagnostic,language,launch}.ts` at commit
`95daf90` (reference clone `/tmp/opencode-ref`), plus the diagnostics hook in its
`tool/edit.ts:190-205` and `tool/write.ts:70-100`.

## The port, and what it kept

opencode is MIT. Every ported file — `core/snowpea_core/lsp/{servers,client,manager,diagnostic,language}.py`
— carries the full MIT notice from `/tmp/opencode-ref/LICENSE` as a header comment, names the
upstream file it came from, and says where it diverges. `core/snowpea_core/lsp/tools.py` and
`core/snowpea_core/server/lsp_handlers.py` are ours and carry no notice: the tool shapes and the
RPC are snowpea's.

| snowpea | upstream | relationship |
|---|---|---|
| `lsp/language.py` | `lsp/language.ts` | the extension table verbatim; `language_id()` / `extension_of()` added |
| `lsp/diagnostic.py` | `lsp/diagnostic.ts` | same renderer, two changes (items 3, 4) |
| `lsp/servers.py` | `lsp/server.ts` + `lsp/launch.ts` | rewritten as data (item 1) |
| `lsp/client.py` | `lsp/client.ts` | same protocol handling, own framing (item 2) |
| `lsp/manager.py` | `lsp/lsp.ts` | same lazy-per-root structure, Effect removed (item 5) |

## Deviations

1. **A server is data, not a `spawn()` closure.** Upstream writes one async `spawn()` per server,
   each with bespoke download logic — fetching a zip from GitHub, extracting it, chmod-ing a
   binary. Here a `ServerInfo` declares `candidates` (binary + argv), `root_markers`,
   `exclude_markers`, `strict_root`, an optional `install` recipe and an optional `initialization`
   hook, and a single `servers.spawn()` drives every one of them. Three reasons: `lsp.servers` in
   settings.json can then define a server with no code, which upstream also supports but through a
   second, parallel code path; a 2000-line file of near-identical closures is a maintenance tax
   nobody here will pay; and AC-46 ("no shell strings — every spawn is argv") becomes checkable,
   because there is exactly one call to `create_subprocess_exec` in the whole layer and one test
   asserts on it.

2. **The JSON-RPC framing is written out rather than taken from a dependency.** Upstream uses
   `vscode-jsonrpc`. `pygls`/`lsprotocol` is the Python equivalent, but it brings a full server
   framework and a generated model of the entire LSP surface for what is, on the client side,
   ~80 lines: read `Content-Length`, read that many bytes, match `id` to a future. The wire types
   stay plain dicts for the same reason — the tools reach into four or five fields of them and
   typing the other four hundred buys nothing.

3. **The `Diagnostics` block reports warnings, not only errors.** Upstream's `report()` filters to
   `severity === 1` and returns `""` otherwise. The M13 contract §3 says "errors and warnings", and
   it is right: "unused import" is exactly the kind of thing the agent should clear up in the edit
   it is already making. Errors sort before warnings so a truncated block never hides an error
   behind twenty warnings.

4. **The rendered line carries the diagnostic `code`.** `ERROR [12:5] message (code)` per the
   contract, against upstream's `ERROR [12:5] message`. The code is what a reader greps for.

5. **`LspManager` is a plain object on `Core`, not an Effect service.** Nothing else in this tree
   is written in that style. The substance is upstream's: match servers by extension, resolve a
   root per server, one client per `(server, root)`, an in-flight map so two concurrent edits do
   not start two servers, fan a request out over every matching client.

6. **Crash handling is restart-once-then-`broken`, which upstream does not do.** Upstream adds a
   key to a `broken` set on the first failure and never tries again. AC-45 asks for one restart
   first, so `Entry.crashes` counts and the second crash is terminal. A `broken` server is reported
   as such by `lsp.status` and never fails an edit.

7. **Idle servers are shut down; upstream keeps them for the process lifetime.** `lsp.idleTimeoutSec`
   (default 600) with a 30-second sweep task that stops itself when no server is left. opencode's
   process is a session; snowpea's daemon is not, and a long-lived daemon that has visited six
   projects would otherwise be holding six gopls and three rust-analyzers.

8. **Auto-install is npm, pip and go only.** The contract says "installs it the way opencode does
   (npm/pip/go install into `<SNOWPEA_HOME>/lsp/bin`)". Upstream *also* downloads and unzips GitHub
   releases for eslint, csharp (Roslyn), zls, elixir-ls, terraform-ls, kotlin, jdtls and tinymist.
   Those are PATH-only here. Writing an archive fetcher per server is a large, fragile surface
   whose failure mode is a half-extracted binary, and the user who wants one of those servers
   already has a package manager that installs it properly. `pip` installs into a venv at
   `$SNOWPEA_HOME/lsp/py` rather than `--target`, because `--target` does not reliably produce
   console scripts.

9. **`ServerInfo.precondition` was added, and TypeScript needs it.** Upstream's `spawn()` can simply
   `return` when the project is unsuitable — the TypeScript server resolves the workspace's own
   `typescript/lib/tsserver.js` and gives up without it. In the data-driven design that check has
   nowhere to live, so `precondition(root) -> bool` is it. This is not theoretical: verified against
   `typescript-language-server` 5.x, which exits *during `initialize`* with "Could not find a valid
   TypeScript installation" when the workspace has no TypeScript, which read from the outside as a
   server that came up and then died. `typescript` and `astro` declare it; `tsserver_options()` /
   `tsdk_options()` supply the path the servers need in `initializationOptions`.

10. **Pull diagnostics are driven by *dynamic* registration, which is the whole reason pyright
    works.** Upstream tracks `client/registerCapability` for `textDocument/diagnostic` and so does
    `client.py` — worth calling out because omitting it is silent and total: pyright advertises no
    static `diagnosticProvider`, registers the method after the first `didOpen`, and then never
    sends `publishDiagnostics` at all. A client that only reads the initialize response sees zero
    diagnostics from pyright, forever, with no error anywhere. `_pull_registered` is sticky
    (pyright registers and unregisters repeatedly while it settles) and only decides whether a pull
    is *worth attempting*; a pull that fails is `False`, not an exception.

11. **`wait_for_diagnostics` takes a `since` sequence number.** Upstream compares a document
    `version` and a wall-clock `after`. `publish_seq[path]`, read before the edit and waited past,
    is the same guarantee with less to go wrong: a second edit to the same file is never answered
    out of the first edit's publish. The 150 ms debounce after a publish is upstream's.

12. **`ruff` and `ty` are registered but off by default.** Upstream gates `ty` behind an
    experimental runtime flag and deletes `pyright` when it is set. Here `DISABLED_BY_DEFAULT`
    holds both, and naming one in `lsp.servers` turns it on. Two Python servers over every edit
    doubles the latency for one set of answers, and picking the winner by flag is less honest than
    letting the user say which they run.

13. **`lsp_rename` applies the `WorkspaceEdit` rather than only returning it.** The contract says
    applying it "is a `write` action subject to the permission mode", which the `write` permission
    tag on the tool already delivers — plan mode refuses it, accept and auto ask. A tool that
    returned a `WorkspaceEdit` as JSON for the model to hand-apply with `edit_file` would throw
    away the whole point. Edits are applied end-first per file, as the LSP spec requires.

14. **Seven tools, and `lsp_symbols` covers both symbol shapes.** Servers answer
    `textDocument/documentSymbol` with either a `DocumentSymbol` tree or a flat
    `SymbolInformation` list; `_flatten_symbols` renders both, indented by depth.

15. **`ToolInfo.reason` was added to the protocol.** Contract §4 asks for "`state: inactive` +
    reason" and there was no field for it. It is additive, defaults to `""`, and `Tool.reason`
    carries it. Nothing but the `lsp_*` tools sets it today; `web_search` and the media tools could
    later, instead of encoding the reason into `provider`.

16. **Everything in `lsp/` is registered through `register_builtin_tools`, not a separate
    `wire_*`.** Memory and scheduler tools get their own registration hook because they need a
    service built first. The LSP tools do not: `manager_for(core)` creates the manager on first use,
    so `wire_lsp(core)` only exists to set the initial tool state.

## Verified against real servers

The suite needs none, but both were installed and run during development:

* **pyright 1.1.414** (`pyright-langserver --stdio`): `test_ac43_real_pyright_reports_a_type_error`
  and `test_ac44_real_pyright_finds_a_definition` pass. The first of these is why item 10 exists.
* **typescript-language-server 5.x with TypeScript 5.x**:
  `test_ac47_real_typescript_reports_a_type_error` and `test_ac47_real_typescript_lists_references`
  pass. These are why item 9 exists. Note that TypeScript **7.x** ships no `tsserver.js` at all, so
  a workspace on TS 7 gets no TypeScript language server — correctly, and silently.

All four skip cleanly when the binary is not on `PATH`.

## A cold server misses the first edit, by design

AC-43 asks for a `Diagnostics` block "within 5 s". Contract §3 bounds the *server start* at 3 s,
and pyright's cold start is longer than that: the first `edit_file` in a fresh workdir legitimately
returns with no block, and every edit after it has one. The real-pyright test warms the server the
way a real session does — by having touched a file already — and then asserts the block arrives in
under five seconds. The alternative, blocking the first edit for as long as rust-analyzer wants to
index, is worse.

## What is not here

Per contract §7: code actions, formatting, semantic tokens, multi-root workspaces beyond one root
per server, and remote (ssh/docker) backends — `LspManager` runs on the daemon host, so a session
on a docker or ssh backend gets no diagnostics. Call hierarchy (`incomingCalls`/`outgoingCalls`) and
`implementation` exist upstream and are not ported; they are not in the contract's seven tools.

The TUI's `lsp` HUD segment and `/lsp`, and the IDE's diff-review count and Settings → Tools card
(contract §5), are the surfaces' work. The core side they read — `lsp.status` and the
`lsp.diagnostics` session event — is here.
