# OMC porting map — Claude Code concepts → snowpea

Source of the originals: `/home/whitevil/.claude/plugins/cache/omc/oh-my-claudecode/4.15.1/skills/`
(oh-my-claudecode 4.15.1, MIT). That tree is **read-only reference material**. Prompt
text we keep verbatim is copied under `core/snowpea_core/builtin_skills/`; everything
else is re-implemented against snowpea primitives.

This document exists to satisfy risk 5 of the consensus plan: a ported command must
not silently change behaviour because a Claude Code-only primitive was dropped. Every
ported command additionally carries a "Differences from the OMC original" section at
the bottom of its `SKILL.md` or module docstring.

## 1. Concept replacements

| Claude Code / OMC concept | snowpea replacement | Notes |
|---|---|---|
| Named sub-agents (`executor`, `architect`, `planner`, `critic`, `verifier`, `explore`, `designer`, `writer`, …) | `agent.spawn(role="executor", model=..., prompt=...)` | Role is a first-class argument, not an agent file. The role catalogue lives in `core/snowpea_core/agent/roles.py`; model routing (`haiku`/`sonnet`/`opus` in OMC) becomes a provider-neutral tier that `providers/presets.py` maps onto the configured vendor. |
| `Task` tool (spawning one sub-agent and awaiting its report) | `delegate_task` tool | Same request/report shape. Parallel fan-out is `delegate_task` with a batch argument rather than several tool calls in one assistant turn. |
| `state_write` / `state_read` MCP tools | `memory.write(...)` / `memory.search(...)` | Backed by the SQLite session store rather than an MCP server. Keys become namespaced memory records so they are searchable, not just addressable. |
| `notepad_read` / `notepad_write_working` / `notepad_write_priority` / `notepad_prune` MCP tools | `<project>/.snowpea/notepad.md` plus `memory.write(scope="notepad")` | The notepad is a plain file in the project so a human can read and edit it. Priority vs. working entries become sections in that file; pruning is a core maintenance routine, not a tool call. |
| `AskUserQuestion` tool | `clarify` tool (interactive mode) / approval channel (unattended mode) | Interactive turns surface the question on the originating surface (TUI or IDE). Unattended and scheduled runs route it to the bound messenger channel as a one-shot `requestId` approval request with `approvals.timeoutSec` (default 300); expiry is a rejection. |
| `Skill(name)` invocation / `/skill-name` slash commands | `command.run(name, args)` | One loader reads bundled skills and user-installed skills from the same format, so the bundled three exercise the user-skill path from day one. |
| Claude Code hooks (`SessionStart`, `PreToolUse`, `PostToolUse`, `SubagentStart`, `Stop`, …) | `core/snowpea_core/skills/hooks.py` | Same event names and the same "hook output is fed back as context" semantics. Hooks are Python callables registered by a skill or plugin instead of shell commands in `settings.json`. |
| `<system-reminder>` injection by hooks | Hook return values appended to the turn as system context | Identical effect; the wrapper tag is applied by `skills/hooks.py`. |
| `.omc/state/`, `.omc/notepad.md`, `.omc/plans/`, `.omc/logs/` | `<project>/.snowpea/` with the same subdirectories | `config/paths.py` resolves this and honours `SNOWPEA_HOME`; no global path is hardcoded. |
| `run_in_background` on Bash | `terminal(background=true)` + process registry | Background processes are tracked so `status`/`kill` work across turns. |
| OMC model routing keywords (`model=opus`) | Capability tier (`tier="deep"` / `"standard"` / `"fast"`) | Vendor-neutral, because snowpea targets 11 providers. `providers/presets.py` holds the per-vendor mapping. |
| `DISABLE_OMC`, `OMC_SKIP_HOOKS` kill switches | `SNOWPEA_DISABLE_SKILLS`, `SNOWPEA_SKIP_HOOKS` | Same comma-separated semantics for the skip list. |

## 2. Built-in command port strategy

Boundary from §2.7 of the consensus plan: commands whose essence is **control flow**
become Python; commands whose essence is a **long prompt** stay as markdown so the
original wording survives.

| Command | Port strategy | Destination | Why |
|---|---|---|---|
| `ralph` | Python workflow | `core/snowpea_core/commands/ralph.py` | A loop with a completion predicate and a verification reviewer. Non-deterministic if left to markdown; needs a behaviour test (`tests/test_ralph_e2e.py`). |
| `ultrawork` | Python workflow | `core/snowpea_core/commands/ultrawork.py` | Parallel fan-out over `delegate_task` batches with result joining. Concurrency belongs in code. |
| `team` | Python workflow | `core/snowpea_core/commands/team_cmd.py` | Shared task list with atomic claim semantics across several agents. Requires real locking, not prompt discipline. |
| `deepinit` | Python workflow | `core/snowpea_core/commands/deepinit.py` | Walks the tree and writes hierarchical `AGENTS.md` files. File production and idempotency are code concerns. |
| `deep-interview` | Bundled `SKILL.md` | `core/snowpea_core/builtin_skills/deep-interview/SKILL.md` | The Socratic questioning ladder and ambiguity gate are the prompt. Rewriting it in Python would lose fidelity. |
| `deep-research` | Bundled `SKILL.md` | `core/snowpea_core/builtin_skills/deep-research/SKILL.md` | Research method expressed as instructions; the tools it calls (`web_search`, `web_extract`, `delegate_task`) are already native. |
| `ralplan` | Bundled `SKILL.md` | `core/snowpea_core/builtin_skills/ralplan/SKILL.md` | Consensus-planning gate that reads as a prompt; its only control flow is "ask before executing", which the `clarify` tool already provides. |

Note on `deep-research`: oh-my-claudecode 4.15.1 ships the research capability as
`external-context` and `autoresearch` rather than under the name `deep-research`. The
bundled skill takes its text from those two and is named `deep-research` in snowpea.
Record the exact source skill directory in the skill's front matter when porting.

## 3. Porting checklist

For each ported command:

1. Every Claude Code primitive it references has a row in §1, or the port stops until
   one is added.
2. The `SKILL.md` (or module docstring) ends with a "Differences from the OMC original"
   section listing behavioural deltas.
3. At least one behaviour test exists (`tests/test_<command>_e2e.py`), runnable against
   the scripted fake provider so CI needs no API key.
4. Attribution: bundled markdown taken from oh-my-claudecode keeps its MIT notice and is
   listed in `NOTICE`.
