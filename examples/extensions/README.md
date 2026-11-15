# Example extensions

Small, complete snowpea extensions, one per capability and one that combines several. The developer guide, [docs/manual/en/extensions.md](../../docs/manual/en/extensions.md), walks through them; `tests/test_extension_examples.py` loads every one into a private daemon, exercises what it registered and checks that unloading it leaves nothing behind.

| Folder | Shows |
|---|---|
| [hello](hello/plugin.py) | the starter that `snowpea extension new` writes: one tool, plus [its test](hello/tests/test_hello.py) |
| [file-stats](file-stats/plugin.py) | a tool in depth: schema, permission tag, errors, the session's backend, progress, `asyncio.to_thread`, timeout, `meta` |
| [house-style](house-style/plugin.py) | a system-prompt section in the stable band |
| [git-branch](git-branch/plugin.py) | a runtime-context provider |
| [redact-secrets](redact-secrets/plugin.py) | a `post_execute` hook, with a `configSchema` in [plugin.json](redact-secrets/plugin.json) |
| [finish-the-job](finish-the-job/plugin.py) | a `turn_stopping` listener with a per-turn cap |
| [scratchpad](scratchpad/plugin.py) | a slash command keeping data in `ctx.data_dir` |
| [glossary-mcp](glossary-mcp/plugin.py) | an MCP server ([server.py](glossary-mcp/server.py)) that runs while the extension is loaded |
| [ticket-workflow](ticket-workflow/plugin.py) | several together: validated config, async `apply`, an HTTP client closed by an async `on_dispose`, a network tool, a section, a provider, a command, and a module of its own ([tracker.py](ticket-workflow/tracker.py)) |

To try one, copy its folder into `~/.snowpea/extensions/` and run `snowpea extension reload <name>`. The extensions that need configuration (`ticket-workflow`) say what in their `plugin.json`.
