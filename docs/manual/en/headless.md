# Headless runs

`snowpea -c` runs one turn without a UI and exits with a code you can branch on. It is the way to put snowpea in a script, a git hook, or a CI job.

```bash
snowpea -c "what does this repository do?"
snowpea -c "add a regression test for the parser" --mode auto
snowpea -c "summarize today's diff" --json --cwd ~/src/api --timeout 300
```

## Options

| Option | Meaning |
|---|---|
| `-c`, `--prompt TEXT` | the prompt; its presence is what makes the run headless |
| `--mode plan\|accept\|auto` | permission mode, defaults to the project default |
| `--json` | emit JSON Lines instead of prose |
| `--cwd DIR` | working directory of the session |
| `--timeout SEC` | interrupt and close the session after SEC seconds |
| `--provider VENDOR` | vendor for this run |
| `--resume SESSION_ID` | continue a saved session instead of opening a new one |
| `--approve-none` | deny every approval instead of prompting |
| `--home DIR` | override `SNOWPEA_HOME` |

## What it does

It ensures a daemon is running, creates a session in `--cwd`, sends the prompt, renders `session.event` notifications as they arrive, and closes the session when the turn ends. No TUI is spawned, so Node is not needed.

A slash command is a valid prompt, because the command registry belongs to the core rather than to the UI:

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit"
```

`snowpea init [--force]` is a shortcut for exactly that: it starts a session in `--cwd` (or the current directory) and runs `/init` as one headless turn, so `snowpea init` and `snowpea -c "/init"` do the same thing.

```bash
snowpea init
snowpea init --force
```

## Continuing a saved session

`-c` normally opens a fresh session. `--resume` continues one you already have, which is the headless half of the TUI's `/resume`: the saved history is reloaded and the prompt is appended to it.

```bash
snowpea session list --include-closed
snowpea -c "and now write the tests" --resume s-4f2c9a1b7e30
```

`--mode` and `--provider` are ignored with `--resume` — the saved session keeps its own. `--resume` on its own does nothing: inside the TUI, use `/resume` to pick a session interactively.

## Saved sessions

Sessions persist in `$SNOWPEA_HOME/state.db` after they close, and their attachments and speech under `$SNOWPEA_HOME/attachments/<id>/` and `$SNOWPEA_HOME/audio/<id>/`. Deleting a saved session removes all of it.

```bash
snowpea session list                                  # live sessions
snowpea session list --include-closed --json          # plus the saved ones
snowpea session list --include-closed --workdir ~/src/api
snowpea session delete s-4f2c9a1b7e30                 # one saved session
snowpea session clear --workdir ~/src/api             # every saved session of one project
snowpea session clear --all                           # every saved session, everywhere
```

A live session is never deleted; close it first. Each row prints the session id, mode, creation time, working directory and the last prompt it saw.

## Teams and model profiles

Project teams and per-agent model routing are settings, so they can be configured without a UI.

```bash
snowpea team list                                     # global + project teams, * marks the active one
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery

snowpea model profiles --json                         # profiles, the default, per-agent assignments
snowpea model default fast                            # models.default
snowpea model assign executor deep                    # agents.models.executor
```

`snowpea team create` writes `<workdir>/.snowpea/settings.json` — the same file `/team create` writes — and refuses an agent name it cannot find. `snowpea team delete` removes project teams only; a global team is edited in `$SNOWPEA_HOME/settings.json`. `snowpea model assign` is rejected by the daemon if the profile id does not exist.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | the turn completed |
| `1` | the agent ended in failure |
| `2` | usage or configuration error |
| `3` | could not connect to the daemon |
| `4` | an approval was denied, or the mode blocked the action |
| `5` | `--timeout` elapsed; the session was interrupted and closed |

```bash
if snowpea -c "does this repo have a failing test?" --mode plan; then
  echo "clean"
else
  echo "exit $?"
fi
```

Code `4` is the one to think about. In plan mode a write attempt produces a `mode_denied` error and exit `4`, which makes plan mode a usable read-only gate in CI.

## JSON output

With `--json`, each received `session.event` is written as one JSON object per line, and the last line is a result record:

```json
{"kind":"message.delta","sessionId":"…","seq":12,"payload":{"text":"Looking at "}}
{"kind":"tool.call","sessionId":"…","seq":13,"payload":{"callId":"c1","name":"grep","args":{"pattern":"def main"}}}
{"kind":"tool.result","sessionId":"…","seq":14,"payload":{"callId":"c1","name":"grep","ok":true,"output":"…"}}
{"kind":"turn.done","sessionId":"…","seq":20,"payload":{"turnId":"t1","reason":"complete"}}
{"kind":"result","exitCode":0,"sessionId":"…","usage":{"inputTokens":4120,"outputTokens":380}}
```

Event kinds are `message.delta`, `message.done`, `tool.call`, `tool.result`, `diff`, `subagent.spawn`, `subagent.update`, `subagent.done`, `team.task.update`, `mode.changed`, `usage`, `error` and `turn.done`. The `turn.done` reason is one of `complete`, `interrupted`, `error`, `denied`, `timeout` or `budget` (the turn used its whole tool-round budget, reported what it had done, and stopped; it exits 1, like `error`). The full payload schema is in [docs/protocol.md](../../protocol.md).

```bash
snowpea -c "list the modules" --json | jq -r 'select(.kind=="message.delta") | .payload.text' | tr -d '\n'
```

## Approvals without a person

With a TTY, an approval request becomes a y/n prompt on stdin. Without one — a pipe, a CI runner — there is nobody to ask, so the request is denied immediately and the process exits `4`. Say so explicitly when that is what you want:

```bash
snowpea -c "run the linter" --approve-none
```

The two honest configurations for automation are plan mode for anything that only needs to read, and auto mode inside a container you are willing to discard. See [Backends](backends.md) for the container part.

## Session-free subcommands

Some subcommands call a single RPC method and exit without creating a session or contacting a model. They are fast, deterministic and free, which makes them the right post-install smoke test:

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea daemon status --json
```

## In CI

```yaml
- run: curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
- run: snowpea --version
- run: snowpea tools list --json
- run: snowpea -c "/deep-research whether this dependency has a known CVE" --mode plan --timeout 600 --json
```

Set `SNOWPEA_HOME` to a job-local directory so runs do not share state, and remember that the daemon keeps running after your step ends — `snowpea daemon stop` at the end of the job if the runner is long-lived.

## Next

[Protocol](protocol.md) — what the CLI is actually speaking.
