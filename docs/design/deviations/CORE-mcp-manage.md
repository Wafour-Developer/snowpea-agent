# Deviations — CORE-mcp-manage (`mcp.*`, `/mcp`, `snowpea mcp`)

Against `docs/design/m14-mcp-management.md` §3 and §4 (core side; the TUI form
and the desktop screen are other stories).  Everything the contract names is
implemented; the notes below are where the implementation had to choose.

1. **No `docs/cli.md`.** §4 says the CLI is "documented in `docs/cli.md` and
   `docs/manual/{en,ko}/plugins.md`".  This tree has no `docs/cli.md` — the CLI
   reference is `docs/manual/<lang>/commands.md`, which is also what
   `scripts/check_docs_cli.py` validates.  `snowpea mcp …` is documented there
   (en and ko) and in `plugins.md`'s "MCP servers" section, and `/mcp` is added
   to the slash-command tables of all five locales.  Creating a second CLI page
   nobody links to would have been worse than putting it where the rest is.

2. **`mcp.get` is not an RPC.** §4 lists `/mcp get <name>` and `snowpea mcp
   get`, but §3's method list has no `mcp.get`.  Both are served by filtering
   `mcp.list`, which already carries every field a detail view needs, rather
   than adding a method the protocol section does not declare.

3. **`mcp_unsafe` is a new error code.** §3's prose introduces the security
   findings and says they come back "as `mcp_unsafe` unless `force`", but the
   Errors line lists only the other five.  `mcp_unsafe` is registered in
   `server/errors.py` alongside them.

4. **`permission` lives in settings, not in `.mcp.json`.** `mcp.add`/`mcp.update`
   accept a `permission` field, and it is persisted to
   `settings.mcp.permissions[<name>]` — the table `mcp_client.permission_for`
   already reads (M2 §6).  Writing it into the shared `.mcp.json` would put a
   snowpea-only key into a file the contract wants to stay shareable.

5. **`ToolInfo.server` is filled in `Tool.info()`**, not in `tool_list_handler`.
   The source of an MCP tool is already `mcp:<name>`, so deriving the field once
   in the registry gives every surface that builds a `ToolInfo` the same answer,
   including `tool.list`.

6. **HTTP and SSE transports now actually run.** §1 describes the existing
   client as starting "stdio or SSE/streamable HTTP by `url`", but
   `McpServer.start` raised `the SSE transport is not implemented yet`.  Since
   §3 requires adding and testing `url` servers, `McpServer` now opens a
   streamable-HTTP session (`type: "sse"` selects the SSE client instead), with
   `headers` applied through the mcp SDK's own httpx client factory.
   `McpServerConfig.transport` therefore answers `stdio|http|sse`, where it used
   to answer `stdio|sse`.

7. **Plugin attribution.** `McpServerInfo.plugin` needs to know which plugin
   brought a server; the loader merged every plugin's `.mcp.json` into one flat
   table.  `SkillLoader.mcp_server_plugins` records the plugin name alongside,
   which is additive and read by nothing else.

8. **The Hermes security check is vendored, not re-written.**
   `hermes_cli/mcp_security.py` is vendored byte-for-byte (plus the standard
   provenance header) at `core/snowpea_core/vendor/hermes/mcp/mcp_security.py`,
   recorded in `docs/vendoring-map.json` and verified by
   `scripts/verify_vendor_integrity.py` like every other vendored file, so the
   MIT attribution the `NOTICE` promises is machine-checked rather than a
   comment.  `tools/mcp_security.py` is a thin wrapper that calls
   `validate_mcp_server_entry` and adds the two things hermes cannot check
   because it has no remote-server concept: a plain-http endpoint, and a shell
   interpreter used as the server command at all (hermes only objects once that
   shell also reaches the network or writes a persistence surface).  Findings
   come back as `mcp_unsafe`, which `force` overrides; no finding text carries
   an `env` or `header` value.

9. **`mcp.test` on a saved server leaves nothing running either.** The contract
   only requires cleaning up a draft.  The probe always runs on a throwaway
   `McpServer` (with `announce=False`, so a draft never publishes `mcp.changed`
   under a name nobody configured) and always closes it in a `finally`, so a
   named test does not disturb the server the session is using.

10. **`mcp.changed` carries `removed`.** §3's payload is
    `{name, scope, state, toolCount, error?}`; a removal would be
    indistinguishable from a stop.  The added `removed: false` default is
    additive and ignorable.

11. **The CLI's `--` passthrough is split before argparse.** argparse cannot
    parse `snowpea mcp add notes --global -- python -m server`: a trailing
    `nargs="*"` positional stops collecting once an optional flag has been seen,
    and the `--` then reads as an unrecognised argument. `cli/main.py` gains
    `split_passthrough`/`parse_argv`, which take everything after the first bare
    `--` out of an `mcp` command line and put it back on the namespace as
    `rest`. The tail is never parsed, which is the point: it is the child's
    argv verbatim, flags and all. `snowpea mcp test -- <cmd> [args…]` probes a
    draft the same way. `/mcp test <name>` stays name-only.

12. **`stdio_client(errlog=…)` is now bound per call.** The mcp SDK's
    `stdio_client` defaults `errlog` to whatever `sys.stderr` was at *import*
    time, which under pytest is one test's capture object — a later spawn then
    fails with "I/O operation on closed file". Passing the live `sys.stderr`
    at call time is behaviour-neutral in production and makes a server's stderr
    go wherever the daemon's does now.


13. **The `snowpea-studio` catalog preset points at the hosted endpoint.** The
    lead asked for the studio server's command "if you can find it in this
    repo"; there is none. `settings.media.mcp` (`MediaMcpSettings`) is entirely
    user-supplied — `snowpea setup` and `provider.configure("media", …)` write
    the command or url — so the preset carries the hosted
    `http://studio.snowpea.ai/mcp` endpoint plus an `Authorization` header, and
    its description points at `snowpea setup` for the same server.

14. **`configure` takes both spellings.** §4 spells it `--tools a,b`; the first
    implementation took bare tool names. Both work: `/mcp configure notes
    --tools a,b`, `/mcp configure notes a b`, and the same two on the CLI.
