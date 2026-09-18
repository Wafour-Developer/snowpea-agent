# Deviations — CORE-policies (M15 §A tool discipline, §B5 load timing)

Ported logic and the places where snowpea's implementation departs from
`docs/design/m15-agent-policies.md` or from its upstream source. Recorded per
`docs/design/deviations/README.md`.

## Provenance

Two files are ports of **Hermes Agent** code, MIT licensed. The MIT licence
permits reuse with attribution; neither file was copied verbatim.

| snowpea file | Hermes source | what was taken |
|---|---|---|
| `prompts/fragments/execution.md` | `agent/prompt_builder.py` lines 351-470 — `TASK_COMPLETION_GUIDANCE`, `PARALLEL_TOOL_CALL_GUIDANCE`, `OPENAI_MODEL_EXECUTION_GUIDANCE` (`<tool_persistence>`, `<mandatory_tool_use>`, `<act_dont_ask>`, `<verification>`, `<external_state_verification>`, `<literal_preservation>`) | the rules, rewritten as prose in snowpea's prompt voice |
| `tools/file_state.py` | `tools/file_state.py` (`FileStateRegistry.check_stale`) | the staleness ladder: sibling-wrote-after-your-read > partial read > never read |
| `agent/tool_batch.py` | `agent/tool_dispatch_helpers.py` (`_plan_tool_batch_segments`) | path-scoped parallel/sequential segment planning for one model turn |
| `tools/output_spill.py` | the delegation-report head/tail spill | the shape: keep a head and a tail, write the whole thing to a cache file, leave a pointer |

The exploration-routing rule in `execution.md` comes from **opencode**
(`anthropic.txt:79-86`), also MIT.

## Deviations

1. **The working-discipline rules are prose, not XML tags.** Hermes wraps each
   rule set in `<tool_persistence>`-style pseudo-tags. `base.md` and every other
   snowpea fragment are prose bullets, and mixing the two styles inside one
   stable tier would read as two prompts stapled together. The content is the
   same; the packaging matches the library it joins.

2. **`PARALLEL_TOOL_CALL_GUIDANCE` was not ported.** `base.md` already carries a
   "Batching" section saying exactly this. Porting it again would have spent the
   cached prefix twice for one rule.

3. **The stable-tier budget moves from 2225 to 2900 tokens.** `execution.md` is
   34 lines; the measured worst case (plan + small-local + executor) is 2747
   tokens by the `words × 1.3` approximation the budget is written against. The
   budget's job is to make unnoticed growth impossible, not to be tight, so it
   sits above the intended library with headroom. `docs/design/m9-prompts-contract.md`
   §AC-22 and `tests/test_prompts_compose.py` carry the new number.

4. **`file_state` keys on a content hash and on session ids, not on mtime and
   task ids.** Hermes compares `os.path.getmtime` and works over a process-wide
   task registry. snowpea's file tools go through an `ExecutionBackend` that may
   be a docker or ssh backend with no local mtime to stat, so a read is recorded
   as `sha256` of what the tool actually returned. Records are grouped by
   `Session.parent_session_id or Session.id`, so a fan-out of subagents shares
   one view of the tree and a human's session is a group of one.

5. **A stale write is refused, not warned about.** Hermes' `check_stale` returns
   a model-facing warning string that the caller may ignore. M15 §A3 says
   "refuse", so `edit_file` and `write_file` return `ok=False` with
   `stale_file: <reason>`. `tools.readBeforeWrite: false` turns the guard off
   entirely.

6. **`read_file` gained `offset` and `limit`.** "read only partially" needs a
   partial read to exist. It also makes the §A4 spill pointer a real call rather
   than a shape the model has to improvise: the pointer names the offset and the
   limit that would read the omitted middle. Truncation at `MAX_READ_CHARS`
   counts as partial too.

7. **`spill()` takes an optional `home`.** The signature in the design is
   `spill(text, *, head_lines, tail_lines, kind)`; the helper needs to know
   which `$SNOWPEA_HOME` to write under, and defaults to `resolve_home()` when
   nothing is passed so the documented call still works. The loop passes the
   daemon's own home via `core`.

8. **The spill runs in the agent loop, not inside each tool.** One place applies
   it to `shell`, `grep`, `glob` and `list_dir` (`SPILLED_TOOLS`), so delegation
   (§C5) reuses the same helper instead of each tool growing its own copy.
   Existing per-tool caps (`grep` 2000 matches, `glob` 5000 paths) run first and
   are untouched.

9. **`SkillLoader.workdirs()` reads the store synchronously.** §B5a names
   `core.store.list_sessions(include_closed=True)`, which is a coroutine, but
   `workdirs()` is called from `scan()`, which `wire_core` runs synchronously
   before the event loop owns the daemon. `Store.session_workdirs()` is a new
   synchronous `SELECT workdir, MAX(created_at) … GROUP BY workdir` with the
   same result and no `asyncio.to_thread` hop. Capped at 50, existing
   directories only.

10. **A bare skill installs to `skills/` by relocation, not by a second install
    path.** `marketplace.install` has six resolution routes (local dir, git URL,
    `github:`, registry, marketplace entry, shortcut). Rather than teach each one
    where to land, the routes are wrapped: `_install_bundle` puts the bundle
    under `plugins/` as before, and `_relocate_bare_skill` moves it to
    `$SNOWPEA_HOME/skills/<name>/` when its root is a bare `SKILL.md`.
    `SkillLoader.remove` now looks in both directories.

11. **An existing bare plugin directory is registered where it stands.**
    `_scan_bundle` accepts a root `SKILL.md`, which is what makes
    `~/.snowpea/plugins/flux/SKILL.md` load at all, and logs a one-line hint
    naming `$SNOWPEA_HOME/skills/<name>/` as the right home. Nothing is moved on
    disk behind the user's back.

12. **`session.create` / `session.resume` never fail on a bad skill file.**
    `_load_project_skills` logs and continues: a project whose `SKILL.md` does
    not parse must still get a session.

13. **`tools.maxResultLines` and `tools.readBeforeWrite` are camelCase fields.**
    `ToolsSettings` already mixes `snake_case` and `camelCase` because the M1
    contract does; these two are spelled as M15 spells them.

## Contracts this changed

14. **Four `test_skill_registry.py` install assertions moved.** The fixtures in
    `test_install_registry_spec_extracts_and_strips_wrapper`,
    `test_install_clawhub_spec_downloads_via_registry`,
    `test_install_github_spec_never_asks_the_registry` and
    `test_install_github_spec_with_plugin_installs_only_that_subdir` are all bare
    skills, so they now land in `$SNOWPEA_HOME/skills/<name>/` rather than
    `plugins/<name>/`. That is exactly the case §B5d targets — the last of them is
    `github:anthropics/claude-code@frontend-design`, the bug report the subdir
    resolution was written for. The tests were updated to the new contract rather
    than the behaviour being narrowed to avoid them.

15. **`/init` records the `AGENTS.md` it folds into its brief as a read.**
    `init_cmd` hands the model the current file verbatim *so that* it does not
    spend a tool call reading it, which the guard would otherwise punish. It now
    calls `file_state.note_read` for the file it quoted, and an `AGENTS.md` over
    `MAX_EXISTING_CHARS` is no longer quoted truncated: the brief tells the model
    to `read_file` it itself. `--force` records a complete read, since discarding
    the file is what the flag asks for.

16. **Four fake-provider fixtures now read before they write.** `ralph.json`,
    `e2e.json`, `team.json` and `team_conflict.json` scripted a `write_file` onto
    a file the scripted session had never read, which is precisely what A3
    refuses. A `read_file` for the same path was inserted ahead of each write, so
    the fixtures model a well-behaved agent. `tests/fixtures/providers/fake/session.json`
    and `tests/test_lsp.py::test_ac43_edit_file_appends_a_diagnostics_block` got
    the same one-line fix.
