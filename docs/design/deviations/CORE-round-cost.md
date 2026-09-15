# Deviations — CORE-round-cost (reducing cost and tokens per model round)

Validation report: on a two-file project, explorer turns consumed 177K input tokens and critic turns consumed 532K input tokens because each model round re-sent the full tool schema catalogue, the parent's memory recall block, the full skills index, and unbounded nested instruction files. Recorded here per `docs/design/deviations/README.md`.

## Provenance

Two upstream harnesses, ported in shape, none vendored:

- **Deferred tools, `ToolSearch`, and instruction-file caps** — Claude Code (claw-code), Anthropic:
  - Only core tools always loaded; the rest loaded on demand via tool search.
  - Instruction-file caps (4K per file, 12K total block cap).
  - Subagents start with an empty history and type-specific tool allowlists.
- **Child context diet and byte-stable prefix caching** — Hermes, Nous Research:
  - Children built with `skip_context_files` / `skip_memory`.
  - Cached skills index.
  - Byte-stable prompt prefix preserving vendor KV caches across rounds.

No upstream code was copied. All implementations adapt snowpea's own `Session`, `ToolRegistry`, `ToolSpec`, `ToolInfo`, and `PromptTiers` models.

## What was wrong

1. **Full tool schema re-sending**: Every round sent full JSON schemas for every registered tool (built-ins and MCP servers), paying for dozens of schemas on every provider call even when none was needed.
2. **Subagent context bloating**: Delegated child sessions (`is_subagent=True`) start with an empty history and an explicit brief, but were handed the parent's memory recall block, full skills catalog, and all nested `AGENTS.md` files up front.
3. **Unbounded merged instruction blocks**: A monorepo with multiple nested `AGENTS.md` files multiplied the per-file character budget by directory depth, and small-context models (<= 32k tokens) were overwhelmed by the 20,000-character per-file floor.
4. **Prefix cache invalidation**: The tools prompt fragment was re-rendered every round without session-level caching keyed on stable tool sets, breaking prompt prefix caching across rounds.

## What it does now

### 1. Deferred tools and `tool_search` (`tools/deferred.py`, `tools/tool_search.py`, `tools/registry.py`)

- `ToolSpec` and protocol `ToolInfo` gain an additive `deferred: bool` (default `False`).
- **Eager set**: `read_file`, `write_file`, `edit_file`, `shell`, `grep`, `glob`, `ask_user`, `delegate_task`, `skill_view`, `set_mode`, plus `tool_search`, and any tools required by session mode/role (`allowed_tools`).
- **Deferred by default**: Browser tools, web search/extract, audio/media tools, git helpers, settings tools, skill management, and all `mcp__*` tools.
- `tools.deferred` (bool, default `true`): Setting `false` restores the legacy behavior of sending all schemas every round.
- `tools.eager` (list of strings, default `[]`): Forces specific tool names to be sent eagerly.
- Built-in tool `tool_search`:
  - `select:name,name` selects exact tool schemas.
  - Keywords rank name and descriptions (`+term` denotes required terms), returning at most 5 tools.
  - Matched tools join `session.loaded_tools` and are included in all subsequent rounds of that session.
- Grouped prompt line in `prompts/fragments/tools.md`:
  `Deferred (load with tool_search): browser (4), git (4), media (5), mcp:<server> (n) …`
  Deferred tools are named by group count only, with no schemas or descriptions in the prompt.
- **Unprompted deferred calls**: An unloaded deferred tool invoked by the model still runs; the registry automatically loads it into `session.loaded_tools` and appends `loaded <tool> for this session` to the result.
- `tool.list` RPC keeps returning every registered tool with the additive `deferred` boolean.

### 2. Child context diet (`agent/agent.py`, `agent/subagent.py`, `config/settings.py`)

- Setting `agents.childContext` (string, default `"lean"` | `"full"`).
- In `"lean"` mode for `session.is_subagent=True`:
  - No memory guidance or memory recall block (memory tools remain available if permitted).
  - No skills index in prompt (tools still can call `skill_view` by name).
  - Up-front project context limited to the root `AGENTS.md` / `CLAUDE.md`. Nested files attach on-demand when a tool touches that directory.
  - Read-only child sessions (`explore`, `reviewer`) use a narrowed eager set: `read_file`, `grep`, `glob`, `shell` (if allowed), and `tool_search`.
- Preserves the child's persona/definition prompt, role, and the tool round `BUDGET_LINE`.
- `"full"` restores the parent's full prompt context.

### 3. Context file caps (`prompts/environment.py`, `config/settings.py`)

- `context_file_max_chars`: Per-file character cap scales with context window, clamped to `[20 000, 500 000]`. For context windows <= 32k, the floor drops from 20,000 to 8,000. Override via `agent.contextFileMaxChars`.
- `context_files_max_chars`: Total cap for the `# Project Context` block scales with context window, clamped to `[12 000, 120 000]`. Override via `agent.contextFilesMaxChars`.
- When total budget is exceeded, deeper files are truncated first with `…[truncated: N more chars; read <path> for the rest]`, keeping the root instruction file whole.

### 4. Byte-stable tool list and prompt caching (`agent/agent.py`)

- `tools_fragment()` is cached per session keyed by `(sorted eager+loaded names, mode, role)`.
- Cache invalidation occurs on:
  - `commands.changed` / skill reload
  - MCP client sync / tools reload
  - `tool_search` loading new tools
- Consecutive rounds with no changes reuse identical rendered tool strings, allowing model provider prefix caching to hit.

## Deviations from the upstreams

1. **Self-loading deferred tools**: Claw-code returns an unknown-tool error if a deferred tool is called directly. Snowpea executes the tool anyway, loads it for the session, and informs the model with a one-line note.
2. **Hierarchical total budget truncation**: Claw-code applies flat character limits; snowpea allocates budget root-first so root repository guidelines remain intact while package-level details are trimmed first.
