# Deviations — CORE-skill-create (`/skill create`, `skill.create`/`read`/`write`)

Snowpea already had two skill-authoring paths — `/agent create` (a JSON payload from one
non-tool provider completion, rendered into `agents/<name>.md`) and `/skill learn` (the same
shape, summarising a session into a `SKILL.md`). What was missing was a *from-a-brief*
generator for skills themselves: `/agent create`'s counterpart, but for the `SKILL.md`
format instead of the agent-definition frontmatter. Recorded here per the deviations-log
convention (`docs/design/deviations/README.md`).

## Shape

1. **One non-tool provider completion, not an agentic turn with `write_file`.** `create_skill`
   (`commands/skill_cmd.py`) mirrors `learn_skill` and `create_definition`, not `/init`'s
   `agent_loop.run_turn`: `complete_text` (from `agent/definition.py`) runs the workflow brief
   (`prompts/workflows/skill-generate.md`) as a plain system+user exchange and the reply *is*
   the `SKILL.md` text, not JSON — a JSON envelope around a multi-section markdown body would
   only add a round of escaping to get wrong, which is why this generator departs from
   `/agent create`'s and `/skill learn`'s JSON-payload shape while keeping their non-tool,
   single-completion mechanics. The command still writes the file itself
   (`skills/generate.py`'s `write_skill_document`), reuses `skills/publish.py`'s
   `validate_frontmatter` — the exact check `/skill publish` runs — and reloads the skill
   loader before answering, all in code, none of it trusted to the model.

2. **The requested name always wins, even over the model's own choice.** `parse_skill_md`
   (`skills/skill_md.py`) resolves a skill's name from its frontmatter `name:` field first,
   falling back to the directory only when that field is absent — so a generated document
   naming itself something else would make `/<name>` resolve to the wrong command, or to
   nothing at all, without a second check. `force_frontmatter_name` (`skills/generate.py`)
   rewrites (or inserts) the frontmatter's `name:` line to the slug the user asked for before
   `validate_frontmatter` ever sees the text, so the promised "Run it with /<name>." is never
   a lie a stray model choice could make true only sometimes.

3. **Plan mode and the overwrite check are both resolved before the provider is ever asked.**
   `create_skill` checks `ctx.session.mode == "plan"` and `path.is_file()` up front — a plan-mode
   request never spends a completion it cannot act on, and an existing `SKILL.md` without
   `--force` is refused with a one-line message, matching `/agent create`/`/skill learn`'s
   error-reporting shape rather than the mode matrix's `write`-tag denial `/init` relies on
   (there is no tool call here for the matrix to deny). Both checks are pure filesystem/session
   state, the same trade `/init`'s `ensure_project_settings` and `agents_status` already make
   for deterministic-enough decisions.

4. **`skill.create`'s two RPC paths are one function, split on `content`.** `content` given
   means the caller already has the document (a hand-written skill, or an editor's save-as):
   `skill_create_handler` validates and writes it directly, synchronously, no session or turn
   needed. `description` given means generation is wanted, and that takes a completion's worth
   of time — so rather than reimplementing `create_skill`'s logic over RPC, the handler builds
   the identical `/skill create <name> "<description>" [--global] [--force]` text and calls
   `core.commands.start(core, session, "skill", ..., conn)`, the same scheduling
   `session.prompt` uses for every slash command (`commands/registry.py`): a turn id comes back
   immediately, the command runs in the background, and `message.done`/`turn.done` on the
   session answer it. This is the one RPC path that needs an open session (picked by `workdir`
   first, then the connection's own, then the sole open one — `agent.create`'s
   `_session_for` idiom, extended with the `workdir` hint since `skill.create` is given one and
   `agent.create` never was); the `content` path needs none.

5. **`skill.read`/`skill.write` are new, small, and deliberately not `skill.get`/`skill.set`.**
   They exist for the desktop editor's form, resolving a name to whichever `SKILL.md` the
   loader would actually pick (`.snowpea` before `.claude`, project before global — the same
   order `skills/loader.py`'s `PROJECT_DIRS` scans, just narrowed to "first match" instead of
   "every match"), and saving text back verbatim through the same
   `write_skill_document`/`validate_frontmatter` path `skill.create`'s content branch uses, with
   `force=True` (a save from an editor is inherently "yes, overwrite this one").

## Known limits

- The generator's brief caps nothing about length or tool-call count the way `/init`'s does —
  there are no tool calls to cap, since this is a single completion, but a verbose reply is not
  truncated before being validated; an overlong `description` (over `DESCRIPTION_MAX` = 500
  chars in `skills/publish.py`) is simply rejected rather than trimmed.
- `force_frontmatter_name`'s regex expects the frontmatter to open the document (after only
  a BOM/blank lines) with a well-formed `---`/`---` fence; a reply where the model's fence is
  itself malformed is caught by `validate_frontmatter` immediately afterward and reported as
  "could not create the skill", not silently patched.
- Tests (`tests/test_skill_create.py`) exercise the plumbing — the write, the overwrite
  refusal, `--global`, plan mode, bad generated frontmatter, both `skill.create` RPC branches,
  and `skill.read`/`skill.write` — against the deterministic scripted fake provider; they do
  not and cannot assert that a real model writes a *good* procedure, which is exactly the
  judgment left to the model rather than to code.
