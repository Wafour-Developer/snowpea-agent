# Deviations — CORE-skills-index (skills index, `skill_view`, compaction markers, MCP grouping, memory guidance)

M15 §B1-B4, §D and §E. Four things are ported from **hermes-agent** (MIT; see `NOTICE` and
`docs/vendoring-map.md`) and one from its MCP schema layer. Nothing is vendored byte-for-byte:
every port is a rewrite against snowpea's own objects, so the differences are recorded here.

## Ported, and what changed

1. **The skills-index preamble** (`prompts/fragments/skills.md`) is hermes
   `agent/prompt_builder.py::_render_skills_index` (~1288) reworded: scan before replying, load a
   matching *or partially relevant* skill with `skill_view(name)` and follow it, skills encode how
   it is done here, only proceed without one if none is relevant, reload a `[SKILL_PRUNED]` body
   before acting on it. Dropped: the `[names only]` category demotion, the `web_search or terminal`
   substitution, the org-sharing labels and the name-collision warnings. snowpea has no org sharing
   and no per-category compaction, and a source clash is already resolved by root order in
   `SkillLoader.scan` (later root wins), so there is no ambiguous name for the index to flag.

2. **Grouping is by origin, not by category.** Hermes groups skills under author-declared
   categories; snowpea groups them under `[project]` / `[global]` / `[plugin:<name>]` / `[builtin]`
   — the four search roots the loader already has. A category field would have to be invented in
   `SKILL.md` front matter and would be empty for every skill that exists today, while the root a
   skill came from is both known and the thing a reader actually wants (is this mine, or the
   package's?).

3. **`skill_view`'s repeat-view stub** (`tools/skills_tools.py`) is hermes
   `tools/skills_tool_dedup.py::_check_skill_view_dedup`, keyed differently. Hermes keys on
   `(name, file_path)` and compares `(st_mtime_ns, st_size)`; snowpea keys on the skill name and
   compares a SHA-256 of the body it actually served. The hash is what the rule is about — "the
   body you already have is still current" — and it is correct for a skill whose body is held in
   memory by the loader rather than re-read from disk on each view. The tracking dict lives on the
   `Session` (`skill_views`), not in a module-level table keyed by task id: snowpea already has a
   per-session object and a process-global cache would leak between concurrent sessions.

4. **`[SKILL_PRUNED: …]`** (`session/compaction.py`) keeps hermes' one-canonical-marker discipline
   (`context_compressor.py` ~688, ~2749, ~3318, ~3458): one builder, one presence check, the
   summariser told to copy markers verbatim, and re-injection of any it paraphrased away. Three
   differences. (a) The wording is "content lost in compaction" and the reload hint uses double
   quotes — `skill_view(name="x")` — matching the rest of snowpea's tool-call prose. (b) Protection
   is by **turn**, not by token budget or tail count: `skills.protectRecentViews` (default 2)
   counts user messages back from the end, because snowpea's history has no turn ids and a turn
   boundary is the honest unit for "loaded recently". (c) Re-injected markers land under a plain
   `Skills pruned from this context:` line appended to the summary, rather than hermes'
   `## Pruned Skills` section with a 20-marker cap; snowpea's summary prompt already names the
   markers as a heading of its own, and the cap protected a budget snowpea does not model here.

5. **Capability-gated MCP helper tools** (`tools/mcp_client.py`) port
   `tools/mcp_tool_schema.py:153-218` plus `_UTILITY_CAPABILITY_ATTRS`: `list_resources`,
   `read_resource`, `list_prompts` and `get_prompt` are registered only when the server's
   `initialize` result advertises `resources` / `prompts`. Same reason as hermes' (a tools-only
   server got all four and every call returned JSON-RPC `-32601`, which made the model conclude the
   server was broken). The schemas are rewritten rather than copied — hermes freezes its wire bytes
   for cache stability, which snowpea does not need — and the four helpers go through the server's
   include/exclude lists like any other tool, which hermes does not do.

6. **The memory guidance** (`prompts/fragments/memory-guidance.md`) is hermes
   `build_memory_guidance` (~162) condensed to twelve lines, and the **"lessons not logs"** rules in
   `prompts/workflows/skill-learn.md` are its `_LESSON_LAYER_BLOCK` and `_DO_NOT_CAPTURE_BLOCK`
   (`agent/background_review.py:313,343`) cut to the rules that survive without hermes'
   `references/` directory convention and its `skill_manage(action='patch')` tool.

## Decisions taken here, not in the design

7. **The index lists skills only, never `commands/*.md`.** `SkillLoader.skills` holds both, but a
   command file is something the *user* runs with `/name`; telling the model to `skill_view` one
   would point it at a body it cannot act on out of context. The fragment still names `/name` in one
   sentence so the slash route stays visible.

8. **`ToolSpec` gained `source` and `permission`** (`providers/base.py`) so the composer can group
   the tool list by MCP server. The alternative — re-deriving the server from the `mcp__<server>__`
   name prefix — works but makes the prompt layer parse a naming convention, and the permission tag
   in the heading has no name to parse out of at all. No provider sends either field, and neither is
   protocol: `ToolSpec` is the provider-facing dataclass, not a wire type, so `gen_protocol.py` is
   untouched.

9. **The injection scan warns on the tool description only, at registration time** — not on tool
   *results*, and never blocking (`tools/mcp_security.py::description_findings`). A description is
   prompt text the model reads every turn, which is what makes it worth a log line; a result is data
   the model reads once, and scanning it would put a heuristic on the hot path of every MCP call.
   Blocking was rejected outright by the design: the marker phrases are ordinary English, and
   dropping a tool would present as a broken server rather than as a warning.

10. **The MCP grouping lives in `compose.tool_lines`, not in `fragments/tools.md`.** The template is
    still `Available tools:` + `${TOOL_LINES}`. Headings, the permission tag and the one-line note
    that explains them (`compose.MCP_GROUP_NOTE`) are emitted by the function, because they exist
    only when MCP servers are configured — putting the explanation in the template would spend a
    sentence on every prompt of every install that has none.

11. **Two new MCP fixture servers, rather than a change to `echo_server.py`.** The shared echo server
    is asserted on by the tools-contract and policy tests, and giving it capabilities would have added
    four tools to every listing they check. `tests/fixtures/mcp/capable_server.py` covers the positive
    case. The negative case needed a hand-rolled JSON-RPC server
    (`tests/fixtures/mcp/tools_only_server.py`): the `mcp` package's `MCPServer` advertises
    `resources` and `prompts` in its `initialize` result whether or not any are registered, so it
    cannot express a genuinely tools-only server — which is precisely the shape the gate exists for.
    Note the consequence: against a server built on that package the gate is a no-op, and the four
    helpers are registered because the server said it has them.
