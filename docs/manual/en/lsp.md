# Language servers

snowpea talks to the same language servers your editor does. When you save a file, the agent sees the type errors; when it is about to rename something, it can ask the compiler who uses it instead of guessing from a `grep`.

Other languages: [한국어](../ko/lsp.md) · [all pages](../README.md)

## What it changes

Two things, and you get both without configuring anything.

**Every `write_file` and `edit_file` result carries a `Diagnostics` block** when a server is running for that file:

```text
replaced 1 occurrence(s) in src/app.py

<diagnostics file="src/app.py">
ERROR [42:5] "userId" is not defined (reportUndefinedVariable)
WARN [3:1] Import "os" is not accessed (reportUnusedImport)
</diagnostics>
```

The agent reads that before it moves on, so a typo is caught in the same turn it was introduced rather than three tool calls later when the tests run.

**Seven tools** are available to the agent:

| Tool | What it answers |
|---|---|
| `lsp_diagnostics` | what is wrong with a file, or with everything seen so far |
| `lsp_definition` | where the symbol at this position is defined |
| `lsp_references` | every use of the symbol at this position, project-wide |
| `lsp_symbols` | an outline of one file |
| `lsp_workspace_symbols` | find a class or function by name, anywhere |
| `lsp_hover` | the type and docs for the symbol at this position |
| `lsp_rename` | rename the symbol everywhere and write the files |

All of them are `read` except `lsp_rename`, which is a `write` and is refused in plan mode like any other write.

```bash
snowpea tools list --json
```

## Which servers

snowpea knows about the servers below. It starts one the first time a file it serves is touched, in that file's project root, and shuts it down again after ten idle minutes.

typescript, deno, vue, eslint, biome, oxlint, gopls, ruby-lsp, pyright, ruff, ty, elixir-ls, zls, csharp, fsharp, sourcekit-lsp, rust, clangd, svelte, astro, yaml-ls, lua-ls, bash, dockerfile, terraform, dart, ocaml-lsp, gleam, clojure-lsp, nixd, prisma, haskell-language-server, julials.

A server is used only if its binary is on your `PATH` or under `$SNOWPEA_HOME/lsp`. Nothing is downloaded unless you ask for it, and a missing server is never an error — you just get no diagnostics for that language. A missing server is looked for again every five minutes, and at once after an install, a restart or a change to `lsp` settings.

These servers can be installed for you, with `lsp.install` (a settings screen's Install button) or `autoInstall`:

| Kind | Servers |
|---|---|
| npm | typescript (with `typescript` itself, so a project without its own TypeScript still gets diagnostics), vue, eslint (`vscode-langservers-extracted`), biome, oxlint (`oxlint --lsp`), pyright, svelte, astro, yaml-ls, bash, dockerfile, prisma (`@prisma/language-server`) |
| pip | ruff, ty (into a venv at `$SNOWPEA_HOME/lsp/py`) |
| go | gopls |
| dotnet | csharp (`csharp-ls`; 0.16.0 on a .NET 8 SDK), fsharp (`fsautocomplete`) |
| rustup | rust (`rustup component add rust-analyzer`) |

eslint, biome and oxlint are installed automatically only in a project that has their config file, and only by name with `lsp.install`. The rest are PATH-only; `lsp.catalog` says what each one `needs`.

`ruff` and `ty` are off by default because `pyright` already claims `.py`, and running two Python servers over every edit doubles the wait for one set of answers. Turn one on with `"enable": ["ruff"]` (or an `lsp.servers` entry with no `command`), which keeps its install.

## Seeing what is running

The daemon answers `lsp.status` with one row per server — its id, its project root, its state (`starting`, `ready`, `broken` or `stopped`), its pid while it is up, and a `reason` when it is not ready (`not installed`, `not for this project`, `exited: code N`, `initialize failed: …`, or `idle` for one the idle sweep stopped, which the next edit restarts). Each change is also broadcast as `lsp.changed`. Any client on the [protocol](protocol.md) can ask; the terminal UI's `/lsp` view and the IDE's LSP card both read it.

A server that failed twice is reported `broken` and is not started again until you fix the install and ask for a retry: `lsp.restart {id?}`, an `lsp.install`, or a settings change.

`lsp.status` only knows about servers that a root has actually spawned. To see every server snowpea *could* run — without touching a file first — ask `lsp.catalog`: one row per registered id with its `languageIds`, `extensions`, whether it is `installable` (with npm/pip/go/dotnet/rustup) with an `installHint` command for doing that by hand, whether it is currently `disabled` (off by default, named in `lsp.disabled`, or disabled through an `lsp.servers` override), and whether it is `available` now, at which `path`, whether that is `managed` (under `$SNOWPEA_HOME/lsp`), and otherwise what it `needs` first (`npm`, `go`, `gem install ruby-lsp` …). This is what a settings screen's LSP card lists before anything has started.

```bash
snowpea daemon stop
snowpea daemon start
```

## Settings

Everything lives under `lsp` in `$SNOWPEA_HOME/settings.json` and is picked up without a restart.

```json
{
  "lsp": {
    "enabled": true,
    "autoInstall": false,
    "disabled": ["eslint"],
    "idleTimeoutSec": 600,
    "servers": {
      "clangd": { "command": ["clangd", "--header-insertion=never"] },
      "zig": { "command": ["zls"], "extensions": [".zig"] }
    }
  }
}
```

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | `false` removes the seven tools from the agent's list and stops the `Diagnostics` block |
| `autoInstall` | `false` | `true` lets snowpea install a missing server by itself into `$SNOWPEA_HOME/lsp`; `lsp.install` works either way |
| `disabled` | `[]` | server ids to leave alone |
| `enable` | `[]` | default-off servers to turn on as they are (`ruff`, `ty`) |
| `servers` | `{}` | define a new server, or replace a builtin's command |
| `idleTimeoutSec` | `600` | seconds a server may sit unused before it is stopped; `0` keeps them all alive |

A `servers` entry takes `command` (required, an argv list), `extensions`, `rootMarkers`, `env` and `initialization`. Naming an id that already exists replaces that server's command and leaves the rest of its definition alone, which is how you point `pyright` at a wrapper script. An entry for a builtin id without `command` keeps the builtin's command and install and only changes what it sets.

Set `"enabled": false` in that file to turn the whole thing off.

## Why a language is quiet

In order, the things to check:

1. **The binary is not on `PATH`.** `which pyright-langserver`, `which gopls`. snowpea does not search anywhere else unless `autoInstall` is on.
2. **The project root has no marker.** Each server looks upwards from the edited file for its own markers — `go.mod` for gopls, `Cargo.toml` for rust-analyzer, `pyproject.toml` for pyright, a lockfile for the JavaScript servers. Without one, strict servers (rust, deno, biome, oxlint) decline the file entirely.
3. **TypeScript needs the workspace's own TypeScript.** `typescript-language-server` and `astro-ls` exit during startup unless `node_modules/typescript/lib/tsserver.js` exists, so snowpea does not start them until it does. Run your install first.
4. **The server is still starting.** An edit waits at most three seconds for a server to come up. A cold pyright or rust-analyzer takes longer than that, so the first edit in a fresh session can come back without a block and the next one will have it.
5. **It crashed.** `lsp.status` says `broken`.

## Security

Every server is started with an argument list, never a shell command line, so nothing in a filename or a setting can be interpreted as a command. With `autoInstall` left at its default `false`, the LSP layer makes no network request of any kind.
