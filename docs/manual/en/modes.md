# Modes, permissions and the allowlist

Every tool carries one permission tag. The mode decides what happens to each tag.

| tag | tools |
|---|---|
| `read` | `read_file`, `list_dir`, `glob`, `grep`, `git_status`, `git_diff`, `git_log`, `process_list`, `memory_search`, `transcribe_audio`, `skill_search`, `skill_list`, `ask_user`, `set_mode` |
| `write` | `write_file`, `edit_file`, `git_commit`, `memory_write` |
| `exec` | `shell`, `execute_code`, `process_kill`, `skill_install`, `skill_remove` |
| `delegate` | `delegate_task` — the child inherits the session's mode, so delegating can never do more than the parent may; the child's own calls are what get asked |
| `network` | `web_search`, `web_extract`, `browser_*`, media tools, `text_to_speech`, MCP servers by default |
| `send` | `schedule_create`, `schedule_list`, `schedule_cancel` |

## The matrix

| mode | read | write | exec | network | send | delegate |
|---|---|---|---|---|---|---|
| **plan** | allow | deny † | ask ‡ | allow | deny | allow |
| **accept** (default) | allow | allow | ask | ask | ask | allow |
| **auto** | allow | allow | allow | allow | allow | allow |

† plan mode writes markdown and plan files only. ‡ plan mode runs read-only commands without asking; everything else still asks.

**plan** is for thinking. The agent reads your repository and searches the web, and cannot change your code. Two things it can do, because otherwise it cannot do its job at all:

- **Write the plan.** A `.md`, `.markdown` or `.txt` file, anything under `docs/` or `.snowpea/plans/`, and `$SNOWPEA_HOME/plans/`. Every other path is refused, and the refusal says `plan mode: only markdown/plan files may be written` so the agent writes the plan somewhere else instead of retrying. Settings files are never writable in plan mode, whatever they are called.
- **Run read-only commands.** `ls`, `cat`, `grep`, `find`, `git status`/`diff`/`log`/`show`/`blame`, `npm test`, `pytest` and the like go through without a prompt. Anything that could change something — `rm`, `mv`, `git commit`, `python -c`, a redirection, a command substitution, a chain with one unsafe link — still asks, and an unrecognised program always asks.

A denied call produces an `error` event with code `mode_denied` and ends the turn; headless runs exit `4`.

Change what counts as writable with `modes.plan.writableGlobs`, a list of globs relative to your working directory:

```json
{ "modes": { "plan": { "writableGlobs": ["**/*.md", ".snowpea/plans/**", "docs/**"] } } }
```

**accept** is the working default, and matches Claude Code's acceptEdits: file reads and edits happen without a prompt, while shell commands, network calls and anything that sends a message ask first.

**auto** asks nothing. Use it when you are watching, in a throwaway container, or for a scheduled job whose blast radius you have thought about.

## Leaving plan mode

A finished plan does not ask you to change modes yourself. The agent calls `set_mode("accept")`, which puts a picker in front of you: switch to accept and start implementing, switch to auto and start implementing, or stay in plan mode and keep the plan as the whole answer. The recommended row comes first and is marked `(recommended)`.

Whatever you pick takes effect immediately, in the turn that asked. Choose accept and the agent keeps going and starts editing files right there; no second prompt, no repeated plan. Stay in plan mode, press Esc, or let the question time out and nothing changes.

Headless runs (`snowpea -c`) have nobody to ask, so the question declines itself and the mode stays as it was — plan mode is still a read-only gate in CI. On a bound chat the question arrives as buttons; see [gateway](gateway.md).

`/plan`, `/accept`, `/auto` and `/mode` still work, and are what you use to switch modes on your own initiative.

## Switching

```
/plan
/accept
/auto
/mode
/mode show
/mode save
```

```bash
snowpea --mode plan
snowpea -c "draft a migration plan" --mode plan
```

`/mode save` writes `"defaultMode"` into `<project>/.snowpea/settings.json`, so the next session in this repository starts there. The status line shows the current mode at all times.

## Approvals

When the policy says *ask*, the core creates an approval request and blocks that tool call. Interactive requests go only to the surface the turn came from — the TUI window you typed in, or the terminal running `snowpea -c`. They never appear in anyone else's queue.

An answer carries a scope:

| scope | meaning |
|---|---|
| `once` | this call only |
| `session` | anything matching, for the rest of this session |
| `project` | stored in `<project>/.snowpea/settings.json` |
| `always` | stored in `$SNOWPEA_HOME/settings.json` |

Nobody answering is an answer. After `approvals.timeoutSec` (300 seconds by default) the request is denied and the turn ends. Every decision, including timeouts, is appended to `$SNOWPEA_HOME/logs/approvals.jsonl`.

Requests raised by a scheduled job or an incoming chat message are *unattended*, and behave differently — see [Gateway](gateway.md).

## The allowlist

An allowlist entry promotes *ask* to *allow* for a matching command. It can never promote *deny*, so nothing you add here weakens plan mode.

```
/allow ^ls( .*)?$
/allow ^git (status|diff|log)\b
/allow ^npm (run )?test$ --global
/allow tool:web_search
/allowlist
/allowlist remove 3
```

The pattern is a regular expression matched against the shell command. The form `tool:<name>` allowlists an entire tool. Without `--global` the entry lands in the project settings; with it, in `$SNOWPEA_HOME/settings.json`.

Write patterns that are anchored and narrow. `^git ` allows `git push --force` too. `^git (status|diff|log)\b` does not.

## Headless and unattended

`snowpea -c` prompts on stdin when it has a TTY. Without one — a CI job, a pipe — there is nobody to ask, so an approval request is an immediate denial and the process exits `4`. Make that explicit when you mean it:

```bash
snowpea -c "run the linter" --approve-none
```

For CI, the honest combinations are plan mode for anything that only reads, or auto mode inside a container you are willing to lose. Do not reach for auto on a developer machine to silence a prompt; that is what the allowlist is for.

## Next

[Commands](commands.md) — the full command surface.
