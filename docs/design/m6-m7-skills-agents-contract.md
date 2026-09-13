# M6/M7 Contract — Plugins, Skills, Generators, Subagents, Team, Named Agents (binding for US-017…US-021)

Plan: §2.7, §3.1 skills/commands/agent, §4 M6/M7, AC-03/04/10/11/14/15b/16/17, docs/omc-porting-map.md.

## 1. Plugin format (Claude Code compatible) — `skills/loader.py`
Search roots (in order, later wins on name clash and the loader records `source` = builtin|global|project|plugin:<name>):
1. built-ins: `core/snowpea_core/builtin_skills/<name>/SKILL.md`, and (v0.1.x) `core/snowpea_core/prompts/roles/*.md` as `source="builtin"` **agent definitions** — see §2.
2. global: `$SNOWPEA_HOME/skills/*/SKILL.md`, `$SNOWPEA_HOME/agents/*.md`, `$SNOWPEA_HOME/commands/*.md`, `$SNOWPEA_HOME/plugins/<plugin>/…`
3. project: `<workdir>/.snowpea/{skills,agents,commands}/…` and `<workdir>/.claude/{skills,agents,commands}/…` (read-only compatibility)

Plugin directory (Claude Code layout): `plugin.json` `{name, version, description}` (or `.claude-plugin/plugin.json`), `skills/<name>/SKILL.md`, `agents/*.md`, `commands/*.md`, `hooks/hooks.json`, `.mcp.json`, optional `marketplace.json` at a marketplace repo root listing `plugins: [{name, source: <subdir|git url>}]`.

SKILL.md (agentskills.io): YAML frontmatter `name`, `description`, optional `argument-hint`, `user-invocable` (default true), `allowed-tools`; body = instructions. A skill becomes a **command** `/<name>` whose `run` injects the body (with `$ARGUMENTS` substituted) as a system+user instruction into the session and starts a turn. `skills/skill_md.py` parses; `skills/hooks.py` runs hooks; `skills/marketplace.py` aggregates two sources: the local `claude-marketplace` scan (`marketplace.json` of each registered repo) and `skills/registry_client.py`, the hosted registry client (v0.3, live): `HttpRegistryClient` talks to `https://registry.snowpea.ai/v1` (overridable via `--registry`/`SNOWPEA_REGISTRY_URL`/`settings.skills.registry.url`), itself a *federation* of other skill hubs — `GET /v1/skills?sources=all&live=1` returns the registry's own published skills plus mirrored/live hits from other hubs, each item carrying its own `source`/`sourceLabel`, which the core uses directly instead of a fixed per-adapter label. A hub or the whole registry being unreachable answers with nothing (or a per-hub entry in `unavailable`) rather than failing the aggregate.

Sources the registry federates today (`GET /v1/sources` lists health): the local registry, ClawHub (`clawhub`, live), Claude Code marketplaces (`claude-marketplaces`, live). agentskills.io and hermes-hub adapters were removed from the core (and ship disabled on the registry side): agentskills.io is the Agent Skills *specification* site with no skill-listing API, and hermes-hub.ai does not resolve (NXDOMAIN) — both verified 2026-09-13.

`skill.install` resolves any registry-issued install spec — `registry:<id>`, `clawhub:<id>`, `github:<owner>/<repo>[@plugin]` — through the registry's generic download proxy (the whole spec percent-encoded as the path id for anything but `registry:`), falling back to `git clone` for a `github:` spec the registry answers 501 (not fetchable) for. `snowpea skill publish <dir>` (validates `SKILL.md` frontmatter, zips, `POST`s with a bearer publisher token — `--token`/`SNOWPEA_REGISTRY_TOKEN`/`settings.skills.registry.token`), `snowpea skill rate <id> <stars>` and `snowpea skill sources` are CLI-only, direct to the registry, no RPC method added. See `docs/design/deviations/CORE-registry-client.md` and the registry's own `registry-contract.md` for the full wire contract.

Hooks (`hooks/hooks.json`, Claude Code shape): `{"hooks": {"PreToolUse": [{"matcher": "shell|write_file|…", "hooks": [{"type": "command", "command": "…"}]}], "PostToolUse": […], "Stop": […]}}`. The command receives JSON on stdin `{event, tool_name, tool_input, session_id, cwd}` and env `SNOWPEA_HOME`, `SNOWPEA_TOOL_NAME`; exit 2 blocks the tool (PreToolUse) with stderr as the error. Hooks run in `agent/loop.py` around tool execution.

RPC: `skill.list() -> [{name, kind: skill|agent|command|plugin, source, description}]`, `skill.install(source)` (local path | git URL | marketplace `<marketplace>/<plugin>` | `oh-my-claudecode` shortcut = the OMC marketplace repo) copies into `$SNOWPEA_HOME/plugins/<name>` and reloads, `skill.search(query) -> [{id, name, description, source: claude-marketplace|agentskills.io|hermes-hub, installSpec}]`, `skill.reload()` re-scans and emits notification `commands.changed` (add to protocol additively + regenerate). CLI `snowpea skill search|install|list|remove`. Offline test fixtures for the three search sources under `tests/fixtures/marketplace/`.

## 2. Agent definitions & generators — `agent/definition.py`, `commands/agent_cmd.py`, `commands/skill_cmd.py`
`agents/<name>.md` frontmatter: `name`, `description`, `model` (vendor[:model] or "inherit"), `tools` (list or "*"), `permission` (plan|accept|auto|inherit), `max_turns`; body = the agent's **persona**, appended after the composed rules rather than replacing them (see the CORE-prompts note at the end of this section). `/agent create "<desc>"` asks the provider (structured prompt) for name/description/tools/prompt, writes `<workdir>/.snowpea/agents/<name>.md`, reloads, and answers with the path; `/agent list`; `agent.create/list` RPC. `/skill learn [name]` summarizes the current session history into a SKILL.md (frontmatter + steps) at `<workdir>/.snowpea/skills/<name>/SKILL.md` and reloads. Both work with the fake provider in tests (script returns a JSON block).

**Built-in roles are resolvable agents (v0.1.x).** `builtin_agent_definitions()` (`agent/definition.py`) synthesises an `AgentDefinition` with `source="builtin"`, `model="inherit"` and all tools for every non-underscore file in `core/snowpea_core/prompts/roles/`: **architect, critic, executor, explorer, test-engineer, verifier** (`_preamble.md` is skipped). They make the package roles visible through `/agent list` and `agent.list` without writing files into a user's project, and `delegate_task(agent="architect")` resolves without one.

Precedence is the loader's existing rule (§1), not a new one: `builtin` < `global` (`$SNOWPEA_HOME/agents/*.md`) < `project` (`<workdir>/.snowpea/agents/`, `<workdir>/.claude/agents/`). A project file named `executor.md` therefore **overrides** the built-in role entirely; it does not merge with it.

**`AgentDefinition.prompt` is appended to the composed rules, not substituted for them (v0.1.x, CORE-prompts).** `subagent.py` used to assign it in a way that replaced `BASE_PROMPT` outright, so a named agent lost every coding-discipline rule. A child is now always marked `is_subagent` (so it always gets the subagent report contract), its `prompt_role` is the matching role file, and the definition's body is composed as a persona *after* that role. `compose.build_tiers(identity=…)` keeps the old replace semantics for a caller that genuinely wants a bare prompt; nothing in core passes it.

## 3. Subagents — `agent/subagent.py`, `tools/delegate.py`
`delegate_task(task, agent?: name, tools?: [..], timeout?, model?)` (permission exec): spawns a child agent run with its own Session (`parent_session_id`, `originSurface` inherited, mode inherited unless the definition sets one, same backend) under a semaphore `agents.max_concurrent` (global settings < project settings < `session.create(maxConcurrent)`); events on the **parent** session: `subagent.spawn{agentId, name, task}`, `subagent.update{agentId, status: queued|running|done|error, lastText?}`, `subagent.done{agentId, status, summary, usage}`; child events also flow on the child session id. `agent.spawn(name, task, model?)` RPC = same path from a client. `model` is a one-shot routing override — a profile id, `vendor:model`, or a bare vendor — and is the **highest** rung of the model precedence chain (M3 §6); an unresolvable reference **fails the delegation** with `unknown model …` rather than falling back, because the caller named a specific model. Without it the child routes through the chain, inheriting the parent's pinned route when nothing else applies. (CORE-model-assignment) `snowpea agents --json` lists running/queued subagents. `agent.list` rows carry `kind: definition|subagent|named|team` and `status`; when a team is active the definition rows are filtered to its members and a synthetic `kind:"team"` row is prepended (§8, `server/agent_handlers.py`). TUI `SubagentTree.tsx` renders them under the parent.

### 3.1 Agent-name resolution — **BREAKING CHANGE (v0.1.x)**

An unresolvable agent name is now **refused**. Previously a `delegate_task` or `agent.spawn` naming an agent that did not exist ran silently with the parent's own settings and prompt, which made a typo look like a successful delegation. The order of checks in `SubagentManager.run` (`agent/subagent.py`) is:

1. **Team membership first.** If the parent session has `team_agents` and the requested name is not a member → refuse:
   `agent '<name>' is not in active team '<team>'; choose one of: <members>`
2. **Then existence.** If a name was given and `self.definition(parent, agent)` is `None` → refuse: `unknown agent '<name>'`.

A refusal is not an exception. `_refuse` sets the record to `status="error"` with the message as `error`, emits the normal `subagent.done` so the parent's tree closes the node, and returns `SubagentResult(ok=False, summary="", status="error", error=…)`. The refusal reaches the model as the tool's failed result, which is exactly the shape M1 §8 uses for a denial — the model can correct the name and retry within the same turn.

A **missing** agent name (none given at all) is not an error: when a team is active it resolves to `"executor"` if that is a member and otherwise to the team's first member, before the membership check runs; with no team it falls through to the loader's own default. An empty `task` is refused separately with `delegate_task needs a non-empty task`.

## 4. Built-in commands (Python workflows) — `commands/{ralph,ultrawork,deepinit,team_cmd}.py`
- `/ralph <task>`: PRD loop — ask the model for 3–8 user stories with acceptance criteria (JSON) → iterate: pick first failing story → run implementation via subagents (≥2 concurrent when stories are independent) → run verification commands the story names (tests/build) via shell → mark passes → when all pass, run a reviewer subagent ("architect") that must answer APPROVE; otherwise loop (max `ralph.max_iterations`=10). State in `<workdir>/.snowpea/ralph/{prd.json,progress.md}`. Ends with `turn.done{reason:"complete"}` only when approved.
- `/ultrawork <task>`: split into independent subtasks (JSON) → fan out subagents in parallel → merge summaries.
- `/deepinit`: walk the repo, write hierarchical `AGENTS.md` (root + per top-level dir) via subagents.
- `/team N <task>`: see §5.
- Bundled markdown skills: `builtin_skills/{deep-interview,deep-research,ralplan}/SKILL.md` — original texts written for snowpea (OMC has no `deep-research`; compose from its external-context/autoresearch ideas), loaded by the same loader (source=builtin). `/help` lists ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan, accept, auto (+ others) — the rendered list lives in `tui/src/components/HelpPanel.tsx`; keep it and `docs/design/m12-tui-contract.md` §4 pinned to the same constant.
- Replacement map for OMC concepts per `docs/omc-porting-map.md`.

## 5. Team mode — `agent/team.py`
`team.start(sessionId, n, task) -> teamId`: lead (the session) splits `task` into tasks (JSON list with ids/deps) → shared task list persisted in state.db (`team_tasks`: id, team_id, title, status queued|claimed|done|conflict|merged|failed, agent_n, retries, conflict_hunks) → N worker subagents each get a git worktree `<workdir>/.snowpea/worktrees/<teamId>-<n>` on branch `snowpea/team-<teamId>-<n>` (created with `git worktree add -b …`) and loop: claim next queued task (atomic UPDATE), work, commit on their branch, mark done, post a message to the team board (`team_messages`) → lead merges done tasks in completion order with `git merge --no-ff <branch>` in the main worktree; on conflict `git merge --abort`, task → `conflict` (retries+1, hunks captured from `git diff --diff-filter=U` or the merge output) and re-queued to the same agent with the hunks in its prompt; after `team.max_conflict_retries` (default 2) → `failed` (hunks kept) and the lead continues. End: remove worktrees (`git worktree remove --force`) and branches; `team.status(teamId)` returns tasks with states; events `team.task.update{teamId, taskId, status, agentN, retries}`; CLI `snowpea team status [teamId]`; `/team N <task>` command. Test uses the fake provider with a script that makes one worker always write conflicting content to the same file.

A worker's stand-in parent (`TeamManager._anchor`) MUST carry the lead's `agent`, `team` and `team_agents` alongside its workdir, mode and route, and `_work` MUST name the worker (`"executor"` when there is no roster to pick from). Without a name `record.name` is empty, `agents.models` is never consulted, and every worktree worker silently ignores per-agent model profiles — and the §3.1 membership guard never fires either. (CORE-model-assignment)

## 6. Named persistent agents — `agent/named.py`
`agent.create(description)` (from §2) may also register a **named instance**: `named_agents` table (name, definition_path, session_id, memory_namespace `agent:<name>`, channel_bindings JSON, schedule_ids JSON, created_at). `agent.bindChannel(name, channel)` → gateway binding whose target is this agent (its persistent session, recreated on daemon start from the table); jobs may reference `agent` so they run in the agent's session/namespace. Survives restart: on daemon start `NamedAgentRegistry.restore()` recreates sessions, gateway bindings and re-attaches jobs. Memory isolation = namespace. Lifecycle counter `named_agents`. `agent.list` shows `kind:"named"` entries with bindings; `agent.delete(name)` unbinds and removes.

## 7. Tests
- `tests/test_plugin_load.py` (fixture plugin `tests/fixtures/plugins/sample-plugin/` pinned: plugin.json, skills/hello/SKILL.md, agents/fixture-agent.md, commands/fixture-cmd.md, hooks/hooks.json PreToolUse→writes `$SNOWPEA_HOME/fixture-hook.marker` containing the tool name, .mcp.json → `tests/fixtures/mcp/echo_server.py`): 5 kinds loaded; `/fixture-cmd` runs; hook marker; `mcp__fixture-echo__echo` round trip; `skill.search("pdf")` returns 3 sources from offline fixtures.
- `tests/test_generators.py`, `tests/test_ralph_e2e.py` (≥2 concurrent running subagents observed via `agent.list`, non-empty git diff, turn.done complete), `tests/test_team_worktree.py` (3 merged; injected conflict → retries 2 → failed; worktrees cleaned), `tests/test_named_agent_persistence.py` (2 agents, restart, bindings kept, memory isolated), SDK `--grep subagent` contract test (AC-15b).

## 8. Project teams (`agent/team_config.py`, `commands/team_cmd.py`)

Added in v0.1.x. A **team** is a named list of agent names that bounds what a session may delegate to. Two settings surfaces define them:

| scope | file | keys |
|---|---|---|
| global | `$SNOWPEA_HOME/settings.json` | `agents.teams: {name: [agentNames]}`, `agents.default_team: str\|null` (`config/settings.py`, `AgentsSettings`) |
| project | `<workdir>/.snowpea/settings.json` | `agents.teams`, `agents.activeTeam: str\|null` (`config/project.py`, `ProjectAgentsSettings`) |

Resolution (`agent/team_config.py`):
- `teams_for()` merges global then project, so **a project team wins on a name clash** — a project may redefine `default` without touching the user's global file.
- `active_team()` reads the project's `activeTeam` first and falls back to `settings.agents.default_team`. A name with no members, or no name at all, resolves to `None` (= no team, unrestricted delegation).
- Member lists are de-duplicated preserving order (`dict.fromkeys`).

`Settings.load()` migrates older installations at first read: a settings document that has an `agents` block but no `teams` gets `teams["default"] = DEFAULT_AGENT_TEAM` (architect, critic, executor, explorer, test-engineer, verifier) and `default_team = "default"`. An explicitly empty team set is still representable afterwards as `default_team: null`.

**Effects of an active team**, all three of which MUST hold together:
1. The system prompt carries the restriction rule (M1 §17-7, `agent/agent.py`).
2. `delegate_task` / `agent.spawn` refuse a non-member (§3.1).
3. `agent.list` filters definitions down to the team's members and **prepends one synthetic row** `AgentInfo(name=<teamName>, description="Active project team", kind="team", source="project")` (`server/agent_handlers.py`). `kind="team"` is a fourth value alongside `definition|subagent|named`; a client that does not know it MUST render it as an ordinary row rather than dropping it. Named instances and running subagents are appended after the filter and are **not** filtered by team.

`/team` keeps its `N <task>` form (§5) and gains four configuration subcommands (`commands/team_cmd.py`):

```text
/team <N> "<task>"              # unchanged: run N workers on one task
/team create <name> <agent...>  # define/redefine a project team
/team use <name>                # set the project's activeTeam
/team list                      # every team visible here, marking the active one
/team delete <name>             # remove a project team
```

The first word disambiguates: a leading `create|use|list|delete` is configuration, anything else is the worker form. `/team use <unknown>` answers `team: unknown team <name>; use /team list` and changes nothing.

**TUI short delegation.** `$agent-name <task>` in the input line is a surface-local shortcut that calls `agent.spawn` directly rather than going through `session.prompt` (`tui/src/app.tsx`, pattern `/^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/`). The same refusal rules apply — the daemon does not know the input came from a shortcut.
