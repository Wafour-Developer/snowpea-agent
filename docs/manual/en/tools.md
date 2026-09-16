# Tools and context budget

Every model round re-sends the session's prompt: the system instructions, the tool definitions, the project context files, and the conversation history. In a long turn or a multi-agent workflow, re-sending dozens of unused tool schemas, re-reading the same files, or carrying hundreds of lines of old tool output burns hundreds of thousands of input tokens on answers the model already has.

snowpea addresses this with five coordinated mechanisms: a **repeat guard** that stops loops and redundant reads, **output pruning** that trims old tool results before sending requests, **deferred tools** that load schemas on demand via `tool_search`, **lean child context** for delegated subagents, and **hierarchical context file caps**.

## Inspecting tools

```bash
snowpea tools list
snowpea tools list --json
```

`snowpea tools list` prints every registered tool, its permission tag, and a summary. In JSON output (`--json`), each entry includes `"deferred": true|false`, indicating whether its full schema is sent on every round or loaded on demand.

## The repeat guard

A model that loses track of a file it read several rounds ago will often re-read it, or re-run the same search command when stuck. The repeat guard (`tools/repeat_guard.py`) intercepts redundant calls before they run, returns compact stubs, and blocks persistent loops.

```
unchanged since your earlier read_file of core/app.py (412 lines, sha256 9f2c1ab4); the content in that result is still current
```

The guard operates on three levels:

1. **Re-read stubs and blocks (`read_file`).** Keyed on `(resolved path, offset, limit)` per session. When a read targets a file whose content hash on disk has not changed since the previous read, snowpea returns a one-line stub confirming the earlier content is still current. After two such stubs, a third repeat is refused with `ok=False` and error code `repeat_blocked`. Any write or edit to the path (or any modification on disk) clears the recorded hash so subsequent reads proceed normally.
2. **Consecutive and identical-result repeats.** If the exact same call (any tool, identical arguments) is made three times in a row, a warning line is appended to the result. The fourth consecutive call is refused with `repeat_blocked`. Any intervening call resets the streak. For scanning tools (`shell`, `grep`, `glob`, `list_dir`, and MCP tools), the guard also compares output text: if a call produces the exact same text as the previous call, the second call returns a stub (`same result as your earlier grep call (N lines)`), and the fourth is refused.
3. **Loop suspicion and `loop.suspected`.** snowpea tracks a rolling window of the last 20 tool calls `(name, arguments)`. If the same call appears 5 times in that window (consecutive or not), a warning is appended to the result:
   ```
   loop suspected: shell with the same arguments has run 5 times this turn; change approach or finish with what you have
   ```
   At the same time, the session emits a `loop.suspected` event carrying `{tool, count}`. This event fires at most once per turn so the model is not overwhelmed with notifications.

The repeat guard is enabled by default. To turn it off entirely:

```json
{
  "tools": {
    "repeatGuard": false
  }
}
```

## Pruning old tool outputs

During a long session, large command outputs (such as test runs, build logs, or wide `grep` searches) accumulate in the conversation history. Sending 5,000 characters of compiler output from round 2 into round 40 costs tokens on every single turn.

`session/compaction.py:prune_old_tool_outputs` runs in `agent/agent.py:build_messages` right before requests go to the provider:

- **Older than `agent.keepToolRounds` (default `6`):** Results from tool rounds older than the cutoff are replaced in the request by a single line:
  ```
  [earlier shell output pruned — 8123 chars; re-run the tool if you need it again]
  ```
- **Recent rounds (within the last 6 rounds):** sent verbatim. The model is still working from them, and a result cut short reads as a truncated file.
- **`skill_view` bodies are exempt:** Skills carry their own compaction lifecycle and reload pointers (`[SKILL_PRUNED: …]`), governed by `skills.protectRecentViews`.

> [!IMPORTANT]
> **The stored transcript is never changed.** Pruning happens solely on the snapshot prepared for the model provider. Your SQLite database (`state.db`), session resumption, exports, and transcript logs preserve every byte the tools produced.

Settings for output pruning:

| Setting | Default | Description |
|---|---|---|
| `agent.pruneToolOutputs` | `true` | Replace old tool outputs with stubs in outgoing requests. Set to `false` to send all results verbatim. |
| `agent.keepToolRounds` | `6` | Number of recent tool rounds kept intact before pruning kicks in. |

## Deferred tools and `tool_search`

When multiple MCP servers, browser tools, media engines, and system utilities are active, their JSON parameter schemas can total thousands of tokens. Sending all of them every round forces the provider to re-parse dozens of schemas that the current turn may never need.

snowpea divides tools into an **eager** set (sent in full every round) and a **deferred** set (schemas loaded on demand):

### The eager set

The eager set contains tools fundamental to core agent workflows:

- Core tools: `read_file`, `write_file`, `edit_file`, `shell`, `grep`, `glob`, `ask_user`, `delegate_task`, `skill_view`, `set_mode`, plus `tool_search` itself.
- For read-only child sessions (such as `explore` or `reviewer` definitions without write tools), the eager set is narrowed to: `read_file`, `grep`, `glob`, `shell`, and `tool_search`.
- Any tools explicitly required by session mode or role (`allowed_tools`), or added to `tools.eager`.

### What the model sees

All remaining tools (browser actions, media generation, git operations, and all `mcp__*` tools) are deferred. Instead of full schemas, the system prompt presents them on a single grouped line:

```
Deferred (load with tool_search): browser (4), git (4), media (5), mcp:github (12)
```

No parameter schemas or tool descriptions are sent until requested.

### Loading tools with `tool_search`

When the agent requires a deferred tool, it invokes `tool_search`:

- **Exact selection:** `tool_search(query="select:git_diff,git_commit")`
- **Keyword search:** `tool_search(query="+browser click")` (a leading `+` denotes required terms; returns up to 5 matching tools)

Matched tools are added to `session.loaded_tools` and their full schemas are included in all subsequent rounds for that session.

**Self-loading fallback:** If the model attempts to invoke a deferred tool directly by name without calling `tool_search` first, snowpea executes the tool normally, registers it in `session.loaded_tools`, and appends a note: `loaded <tool> for this session`.

### Deferred settings

| Setting | Default | Description |
|---|---|---|
| `tools.deferred` | `true` | Send only core tools eagerly and defer the rest. Set to `false` to send all schemas every round. |
| `tools.eager` | `[]` | List of tool names to always send eagerly, such as a frequently used MCP tool. |

In addition, rendered tool fragments are cached per session keyed by the active tool set, mode, and role. Unchanged rounds produce byte-identical tool blocks, preserving provider prefix caching (KV caching).

## Child context diet

Delegated subagents (`session.is_subagent=True`) start with an explicit brief and an empty conversation history. Handing a child agent the parent's entire memory digest, complete skill catalog, and deeply nested instruction files wastes tokens on context irrelevant to the delegated task.

The `agents.childContext` setting controls this behavior:

- **`"lean"` (default):**
  1. **No memory block:** Skips the parent's memory recall digest and memory instructions (memory tools remain available if permitted).
  2. **No skills index:** Skips the full skills catalog index from the prompt. The child can still inspect and execute any skill by name using `skill_view`.
  3. **Root instructions only:** Up-front project instructions are restricted to the root `AGENTS.md` or `CLAUDE.md`. Nested package-level instruction files are omitted up front and attached dynamically only when a tool touches that directory.
  4. **Narrowed eager tools:** Read-only subagents start with a minimized tool set (`read_file`, `grep`, `glob`, `shell`, `tool_search`).
  5. **Preserved:** The child's persona/definition prompt, role instructions, and the `BUDGET_LINE` ("You have N tool rounds for this task...") are preserved.
- **`"full"`:** Restores the parent's full prompt context (memory digest, complete skills index, and all nested context files).

```json
{
  "agents": {
    "childContext": "lean"
  }
}
```

## Context files and caps

Project instructions (`AGENTS.md`, `CLAUDE.md`, `.snowpea/instructions.md`, `.cursorrules`) guide the agent on repository conventions. In large monorepos with nested instruction files in multiple subdirectories, loading every file can consume a large fraction of the model's context window.

snowpea applies caps to individual files and the aggregated block:

- **Per-file cap (`agent.contextFileMaxChars`):** Defaults to `None`, which derives the cap dynamically from the model's context window (clamped between 20,000 and 500,000 characters). For small context windows (&le; 32k tokens), the floor drops from 20,000 to 8,000 characters.
- **Total block cap (`agent.contextFilesMaxChars`):** Defaults to `None`, which derives a total limit for the `# Project Context` block based on the context window (clamped between 12,000 and 120,000 characters).
- **Hierarchical truncation:** When the combined context files exceed the total budget, snowpea truncates deeper nested files first, keeping the root repository instructions whole. Clipped files preserve their head (70%) and tail (20%), inserting the truncation marker in the middle:
  ```
  …[truncated: 4120 more chars; read src/client/AGENTS.md for the rest]
  ```
- **Skipping context files:** Set `agent.ignoreContextFiles: true` to bypass project instruction files entirely, useful when reproducing issues with vanilla agent defaults.

```json
{
  "agent": {
    "contextFileMaxChars": 20000,
    "contextFilesMaxChars": 60000,
    "ignoreContextFiles": false
  }
}
```

## When to turn something off

Each of these optimizations is enabled by default to save tokens and avoid loops, but specific workflows may warrant adjusting or disabling them:

- **Turn off `tools.repeatGuard` (`tools.repeatGuard: false`):** When you intentionally run polling scripts in `shell`, write loops waiting for background processes to change status, or test repeated invocations where output or file modification times do not change.
- **Turn off `tools.deferred` (`tools.deferred: false`) or add to `tools.eager`:** When using smaller models that lack the reasoning ability to invoke `tool_search` effectively, or when a specific MCP server's tools are called in nearly every round of your workflow.
- **Turn off `agent.pruneToolOutputs` (`agent.pruneToolOutputs: false`) or raise `agent.keepToolRounds`:** When performing complex, multi-round refactoring or debugging where the agent must repeatedly inspect exact compiler logs or test traces produced early in the conversation without re-running the command.
- **Set `agents.childContext: "full"`:** When subagents perform autonomous architecture or research tasks that require global long-term memories or immediate visibility into all installed skills.
- **Adjust `agent.contextFilesMaxChars`:** In monorepos where subpackage instructions contain essential build or lint rules that must never be clipped.
