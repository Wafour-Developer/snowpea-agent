# Deviations — CORE-init (`/init`, a fast sibling to `/deepinit`)

OMC ships `/init` as a full agentic pass over the repository. Snowpea already has that shape —
`/deepinit` fans a subagent out per top-level directory and stitches a hierarchical `AGENTS.md` tree.
What was missing was the *other* `/init`: the rough, single-turn one Claude Code ships, for a repo
where a 60-line file at the root is worth more right now than a documented directory tree. Recorded
here per the deviations-log convention (`docs/design/deviations/README.md`).

## Shape

1. **One main-agent turn, not a subagent.** `cmd_init` (`commands/init_cmd.py`) builds a workflow
   brief and calls `agent_loop.run_turn` directly, exactly the way `/delegate` does — `ctx.handled_turn
   = True`, the turn's own `message.done`/`turn.done` is the answer. No `SubagentManager`, no fan-out,
   no review pass: that is what `/deepinit` and `/ralph` are for. The brief (`prompts/workflows/init.md`)
   caps the exploration at "at most 8 tool calls" and the output at "at most 60 lines", in prose, the
   same way `dir_task` in `deepinit.py` caps its subagent — there is no code-level enforcement of
   either number, because a slash command that clamps its own model's tool budget is more machinery
   than a rough command earns.

2. **Two decisions are made in code, not left to the model.** Everything else about `/init` is the
   model's job (what the project is, how to run it, what merges into the existing file), but two
   things are deterministic enough that hoping the model gets them right every time would be the
   wrong trade:

   - **Whether `AGENTS.md` exists, and its content**, is read by `agents_status()` before the turn
     starts and folded into the brief verbatim (capped at `MAX_EXISTING_CHARS` = 6000 chars). This
     both saves one of the ~8 tool calls and makes the merge-vs-overwrite instruction unambiguous:
     the brief says outright "merge into this" or "rewrite from scratch" rather than "go look and
     decide", which is the difference between a rule and a suggestion.
   - **`.snowpea/settings.json`** is written the way `/mode ... save` writes it — through
     `ProjectSettings`, never through the model's `write_file` tool. `ensure_project_settings()` creates
     it with `defaultMode: "accept"` only when the file is absent, before the turn runs, and reports
     back a `SETTINGS_NOTE` line the brief hands to the model so its own final summary can mention what
     happened. A project's own settings file is not the kind of freeform content a one-shot model
     turn should be trusted to get exactly right; `/mode` already treats it as a typed document, and
     `/init` following that precedent means one code path answers "how does snowpea write project
     settings", not two.

3. **Plan mode falls out of the mode matrix, with one exception.** `write_file` on `AGENTS.md` is
   tagged `write`, which the mode matrix already denies in `plan` — nothing in `cmd_init` special-cases
   it, matching every other command that goes through a normal turn. `.snowpea/settings.json` is the
   exception, precisely because it bypasses the tool registry: `ensure_project_settings(root,
   plan=...)` checks `ctx.session.mode == "plan"` itself and returns a "would create" note instead of
   writing, so the guarantee ("plan mode writes nothing") holds for both files even though only one of
   them goes through the permission policy that would otherwise provide it.

4. **`snowpea init [--force]` is the `-c` path with the prompt pre-filled**, not a second
   implementation. `main()` in `cli/main.py` rewrites `args.prompt` to `"/init"` (or `"/init --force"`)
   and calls the same `run_headless` a bare `-c "…"` uses — same session creation in `--cwd`, same
   renderer, same exit codes — because a subcommand that reimplemented turn plumbing would be a
   second place for the headless contract to drift from `-c`.

## Known limits

- The 8-tool-call and 60-line caps are prose, not enforced. A model that ignores them is not stopped
  mid-turn; this is the same trade `/deepinit`'s per-directory brief already makes.
- `agents_status()` reads `AGENTS.md` synchronously in the command coroutine before the turn starts.
  For a file within the 6000-char cap this is unmeasurable; a much larger existing `AGENTS.md` is
  read in full anyway (only the copy handed to the model is truncated), which is fine for a file that
  is itself supposed to stay under 60-80 lines by convention.
- Tests exercise the plumbing (registration, the brief's content-dependent branches, the code-driven
  `.snowpea/settings.json` write, plan-mode denial) against the deterministic scripted fake provider;
  they do not and cannot assert that a real model actually merges two `AGENTS.md` files well — that
  judgment is exactly what was left to the model rather than to code.
