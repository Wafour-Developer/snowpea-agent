# CLI hook integrations

When Claude Code, Codex, Cursor agent or Antigravity (agy) runs inside a snowpea terminal, snowpea needs to know what it is doing: which session it is, whether it is working, whether it is done, and whether it is waiting for an answer. Each of these CLIs can call a hook when that happens. The integration registers one small snowpea hook script in each CLI's own hook config, so the CLI tells snowpea.

Outside a snowpea terminal the hook does nothing. Installing the integration does not change how the CLI behaves when you run it yourself.

This feature is in beta, as are [terminal sessions](terminals.md).

Other languages: [한국어](../ko/integrations.md) · [all pages](../README.md)

Related: [Terminal sessions (beta)](terminals.md) · [External sessions (beta)](external-sessions.md)

## Commands

```bash
snowpea integration status                  # all four CLIs
snowpea integration install claude          # one CLI
snowpea integration install all             # every CLI whose config folder exists
snowpea integration uninstall codex
snowpea integration status agy --json
```

`install`, `uninstall` and `status` take one of `claude`, `codex`, `cursor`, `agy` or `all` (the default). They work on the files directly, so the daemon does not need to be running.

## Which files each CLI touches

| CLI | Config folder (env override, default) | Files | Events and what snowpea learns |
|---|---|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR`, or `~/.claude` | `settings.json` | `SessionStart` (session id, on startup, resume, clear, compact or fork) · `UserPromptSubmit` → working · `Stop` → done · `Notification` for `permission_prompt` or `elicitation_dialog` → blocked |
| Codex | `$CODEX_HOME`, or `~/.codex` | `hooks.json`, and `config.toml` | `SessionStart` (session id) · `UserPromptSubmit` → working · `Stop` → done. Turns on the `[features]` `hooks` flag in `config.toml` if it is not already on |
| Cursor agent | `$CURSOR_CONFIG_DIR`, or `~/.cursor` | `hooks.json` | `sessionStart` (session id) · `stop` → done |
| Antigravity (agy) | `$ANTIGRAVITY_CLI_CONFIG_DIR`, or `~/.gemini/config` | `hooks.json`, in a `snowpea` block | `PreInvocation` (session id) |

Every install also touches two snowpea files:

- `$SNOWPEA_HOME/hooks/snowpea-agent-state.sh` (`.ps1` on Windows): the hook script, shared by all four CLIs. It does nothing unless `SNOWPEA_TERMINAL_ID` is set, and then it passes the CLI's event to the snowpea core.
- `$SNOWPEA_HOME/integrations/state.json`: a record of exactly what snowpea added to each CLI, so uninstall can undo it.

The hook script is replaced when you update snowpea and reinstall. Put your own hooks in another file, not in this one.

Only Claude Code reports a blocked state through its hooks. For Codex, Cursor agent and agy, snowpea reads a permission prompt or question from the screen, using the screen rules for that CLI.

snowpea looks at the hooks only by the exact command it wrote. Your other hooks stay as they are.

## What install does

- Adds snowpea's entries to the files above and changes nothing else. Formatting, comments and the order of your own entries are kept.
- Writes each file atomically (a temporary file, then a rename), keeps its permissions, and re-reads it to check the result.
- Refuses a file that is a hard link, because a write could change a second file you did not expect.
- Changes no bytes when the integration is already current, so running install twice is safe.
- Keeps one backup before the first change to an existing file: `<file>.snowpea-backup-<date>`, in the same folder. Uninstall does not delete backups.
- Requires the CLI's config folder to exist. If it does not, `install <cli>` says to run the CLI once first. `install all` skips a CLI whose folder is missing and says so.

## What uninstall does

Uninstall removes exactly what install added, and nothing else:

- Only snowpea's entries are removed, and any group that only held them. Your own hooks stay.
- A file that install created is deleted when nothing else is left in it. An empty file that existed before is put back as it was.
- For Codex, the `[features]` `hooks` flag is turned off again only if install turned it on. If you had it on already, it stays on.
- The hook script is deleted when no CLI is left with an integration.

## Status values

`status` prints one line per CLI: the status, the config folder and a reason when there is one. A `[CLI not found on PATH]` note means the CLI itself is not installed, so the integration does nothing yet.

| Status | Meaning | What to do |
|---|---|---|
| `not-installed` | snowpea's entries are not in the CLI's config | `snowpea integration install <cli>` |
| `current` | everything is in place and up to date | nothing |
| `outdated` | installed by an older snowpea, or the registered events differ from this version's | `snowpea integration install <cli>` |
| `modified` | one of snowpea's entries was edited or removed by hand, or the Codex flag was turned off | uninstall and install again, or edit the file back |

## Desktop app and web

The Integrations page in the settings of the desktop app and of snowpea web shows these same rows, with the files each CLI touches, and runs the same install and uninstall actions. The page and its actions are for the person on the local machine only: a remote sign-in cannot see or change them.

## Environment variables

| Variable | Used for |
|---|---|
| `SNOWPEA_HOME` | where the hook script and `integrations/state.json` live (default `~/.snowpea`) |
| `CLAUDE_CONFIG_DIR` | Claude Code's config folder |
| `CODEX_HOME` | Codex's config folder |
| `CURSOR_CONFIG_DIR` | Cursor agent's config folder |
| `ANTIGRAVITY_CLI_CONFIG_DIR` | agy's config folder |

If you start a CLI with one of these variables set, install the integration with the same variable set, so it writes to the folder the CLI reads.
