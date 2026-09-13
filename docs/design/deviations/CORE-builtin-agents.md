# Deviations — CORE-builtin-agents (built-in roles as resolvable agents)

`delegate_task` already knew how to compose `prompts/roles/<name>.md` for an agent that had a
definition file, but the six package roles themselves were invisible: `/agent list` and
`agent.list` showed only what a user had written, and the model had no way to learn that
`executor` or `verifier` existed. `00c9d36` makes the package roles first-class agents. Recorded
here per `docs/design/deviations/README.md`.

1. **The built-ins are synthesised from `prompts/roles/*.md` at call time, not shipped as `.md`
   agent definitions.** `builtin_agent_definitions()` (`core/snowpea_core/agent/definition.py:289`)
   globs the packaged roles directory on every call and builds `AgentDefinition`s with
   `source="builtin"`. The alternative — writing six definition files into a user's project or
   home on first run — would have created two copies of every role prompt that could drift apart,
   and would have put files a user never asked for into their tree. The synthetic definitions
   carry `prompt=""` and a `path` pointing at the role file; the prompt text is composed by the
   existing role-file path, so there is exactly one source of truth per role.

2. **A same-named project or global definition silently overrides a built-in; a name clash is not
   an error.** `_all_definitions` (`core/snowpea_core/commands/agent_cmd.py:97-104`) seeds the
   merge dict with the built-ins **first**, then the loader's definitions, then the on-disk
   global/project ones, so the last writer wins by name. Erroring on a clash was rejected because
   a project shipping its own `executor.md` is the intended customisation path, not a mistake —
   the built-in is the default, and a project overriding the default is the feature.

3. **Built-ins add no restriction of their own: `model="inherit"`, `tools=ALL_TOOLS`,
   `permission="inherit"`.** A built-in role describes *how* to work, not *what* it is allowed to
   touch. Baking a narrower tool set or a fixed model into the package would have been a policy
   decision made for every install with no way to widen it back; a restriction belongs in a
   project definition, which overrides by rule 2 anyway.

4. **A role file that will not parse is skipped, not reported.** The loop swallows
   `DefinitionError` and `OSError` per file, and files whose name starts with `_` are skipped so
   shared fragments can live in the same directory. A malformed packaged role must not be able to
   break `/agent list` for every other role.

5. **The built-in names are baked into the `delegate_task` description and argument schema**
   (`core/snowpea_core/tools/delegate.py:25-31`), which makes them LLM-facing text rather than
   just an RPC listing. That is the point: a name the model cannot see is a name it will not use.
