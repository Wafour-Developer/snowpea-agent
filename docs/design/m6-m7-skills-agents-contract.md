# M6/M7 Contract — Plugins, Skills, Generators, Subagents, Team, Named Agents (binding for US-017…US-021)

Plan: §2.7, §3.1 skills/commands/agent, §4 M6/M7, AC-03/04/10/11/14/15b/16/17, docs/omc-porting-map.md.

## 1. Plugin format (Claude Code compatible) — `skills/loader.py`
Search roots (in order, later wins on name clash and the loader records `source` = builtin|global|project|plugin:<name>):
1. built-ins: `core/snowpea_core/builtin_skills/<name>/SKILL.md`, and (v0.1.x) `core/snowpea_core/prompts/roles/*.md` as `source="builtin"` **agent definitions** — see §2.
2. global: `$SNOWPEA_HOME/skills/*/SKILL.md`, `$SNOWPEA_HOME/agents/*.md`, `$SNOWPEA_HOME/commands/*.md`, `$SNOWPEA_HOME/plugins/<plugin>/…`
3. project: `<workdir>/.snowpea/{skills,agents,commands}/…` and `<workdir>/.claude/{skills,agents,commands}/…` (read-only compatibility)

**Load timing (M15 §B5, v0.1.8+).** The roots above are scanned at three moments, and each one broadcasts `commands.changed`:
- `Daemon.start` → `skills.reload()`: built-ins, `$SNOWPEA_HOME`, every plugin, and every workdir `SkillLoader.workdirs()` returns — the live sessions plus `Store.session_workdirs()`, the distinct workdirs of stored sessions (closed included, newest first, capped at `MAX_SCANNED_WORKDIRS = 50`, existing directories only). `Daemon.start` also calls `mcp_client.sync_tools(core, None)` so `$SNOWPEA_HOME/.mcp.json` servers are running before any session exists.
- `session.create` / `session.resume` → `skills.reload_workdir(workdir)`: an **incremental** scan of `<workdir>/.claude` and `<workdir>/.snowpea` that registers that project's skills, commands and agents **before the first turn**, then `mcp_client.sync_tools(core, workdir)` for the project's own servers. A project the daemon has never opened therefore has its `/commands` on the first prompt. A `SKILL.md` that does not parse is logged, never fatal to `session.create`.
- `skill.install` / `skill.remove` / `/skill create` → a full `reload()`.

A bundle whose root **is** a `SKILL.md` (no `skills/` directory around it) is registered as that one skill — this is what makes `$SNOWPEA_HOME/plugins/flux/SKILL.md` load. `marketplace.install` of such a source lands in `$SNOWPEA_HOME/skills/<name>/` instead of `plugins/`; an existing bare plugin directory is left where it is and logs a one-line hint. `SkillLoader.remove` looks in both directories.

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

**위임 결과는 비어 있지 않다(CORE-subagent-budget).** `SubagentResult`는 `reason`(`complete|budget|error|timeout|interrupted|denied`, 자식의 `turn.done`에서 그대로 읽는다), `rounds_used`(자식 턴이 쓴 도구 라운드 수, `session.rounds_used`), `last_calls`(마지막 3개의 도구 호출)를 함께 싣는다. `delegate_task`의 tool 결과는 `render_report()`가 만든 텍스트다 — `status:` / `reason:` / `roundsUsed:` 세 줄, 빈 줄, 자식의 보고문, 그리고 부분 종료(`budget|timeout|error|interrupted|denied`)일 때는 마지막 호출 목록과 "같은 작업을 그대로 다시 위임하지 말라"는 한 줄. **절대 빈 문자열을 돌려주지 않는다**: 보고문이 없으면 `NO_REPORT`가 대신 들어간다. `reason="budget"`은 `ok=True`로 돌아간다 — 쓸 만한 보고가 있는데 실패한 호출로 돌려주면 부모가 같은 일을 다시 위임한다. 자식의 브리프에는 `BUDGET_LINE`("You have N tool rounds …")이 붙어, 자식이 자기 예산을 알고 쓴다. 예산 자체는 정의의 `tool_rounds:` > `agents.toolRounds[이름]` > `agents.toolRounds` > `agent.max_tool_rounds`(자식은 80이 하한) 순으로 정해진다(M1 §8).

A refusal is not an exception. `_refuse` sets the record to `status="error"` with the message as `error`, emits the normal `subagent.done` so the parent's tree closes the node, and returns `SubagentResult(ok=False, summary="", status="error", error=…)`. The refusal reaches the model as the tool's failed result, which is exactly the shape M1 §8 uses for a denial — the model can correct the name and retry within the same turn.

A **missing** agent name (none given at all) is not an error: when a team is active it resolves to `"executor"` if that is a member and otherwise to the team's first member, before the membership check runs; with no team it falls through to the loader's own default. An empty `task` is refused separately with `delegate_task needs a non-empty task`.

### 3.2 Single owner per task (M15 §C) — v0.1.x

Delegation policy is one sentence with two guards behind it: **one task, one agent**. The prompt half lives in `prompts/tool_descriptions.DELEGATE_TASK`, which the parent reads in the tool fragment of every turn: after delegating, never do the work in the main context as well and never hand the same task to a second agent while the first runs; split by non-overlapping files or areas; sequence dependent steps (implement → review → fix) instead of sending identical briefs in parallel; USE FOR reasoning-heavy, context-flooding or genuinely independent work, NOT FOR a single tool call, mechanical steps, or anything needing the user's answer; the child's reply is a self-report, so external effects are verified through a handle; relay in your own words; say whether the child writes code or only researches, and how to verify; never poll for a child.

**Dedupe (`agent/subagent.py`).** Every record carries a fingerprint: the agent name plus the task text with whitespace collapsed, lowercased, first `FINGERPRINT_CHARS` (400) characters. While a child with that fingerprint is `queued` or `running` under the same parent, a second `delegate_task` is refused through the normal `_refuse` path with `duplicate_task: <agentId> is already working on this; wait for its report or change the task`. `delegate_task(force: true)` (and `SubagentManager.run(force=True)`) skips the check — documented as deliberate best-of-N, off by default. Fingerprints are per parent session, so two humans delegating the same brief never collide. No new event: the refusal is a tool result.

**Sibling file ownership (`tools/file_state.py`).** The registry already records the last writer per path, grouped by parent (M15 §A3). `check_stale` now asks whether that writer is a child of the same parent that is *still running*; when it is, the refusal becomes `file_owned_by_sibling: <path> was changed by <child title>; report instead of editing` and `edit_file`/`write_file` fail with it. Once the sibling reports, the ordinary `stale_file` message applies again — re-read and carry on. `tools.readBeforeWrite: false` turns both halves off. Parallel non-team delegation is therefore limited to disjoint files; `/team` gives each worker a worktree and is not.

**Report folding (`tools/delegate.py`).** `render_report` keeps its three-line header, then trims a report longer than `REPORT_HEAD_LINES` + `REPORT_TAIL_LINES` (120 + 40) through `tools/output_spill.spill()`, which leaves a `read_file(...)` pointer to the full text under `$SNOWPEA_HOME/cache/tool-output/`. One "next step" line closes the report, keyed on `reason` (`complete` / `budget` / `timeout` / `error` / `interrupted` / `denied`) — the parent reads the reason before the report, so what to do about it belongs next to it.

### 3.3 Built-in agent definitions — `agent/definitions/*.md`

Built-in roles (`prompts/roles/*.md`) stay the base catalogue, and `agent/definitions/*.md` — real agent markdown with frontmatter — overrides them by name, so a built-in can carry a **tool allowlist**. Two ship:

| name | tools | contract |
|---|---|---|
| `explore` | `read_file`, `list_dir`, `glob`, `grep`, `lsp_*`, `git_status`/`git_diff`/`git_log`, `web_search`, `web_extract` | thoroughness levels (quick / medium / very thorough), OMC context-budget rules (symbols before reading a file over ~500 lines, ≤ 5 parallel reads, stop after 2 diminishing rounds), absolute paths, findings as text with `path:line` |
| `reviewer` | the same minus the web tools | must open what it judges; `VERDICT: APPROVE` / `REQUEST_CHANGES` / `NEEDS_MORE_EVIDENCE` first, then findings by severity, each with evidence; never approves what it did not read; the final message is the deliverable |

Both resolve through `delegate_task(agent="explore"|"reviewer")`, `/delegate` and `$explore` / `$reviewer`, and a project (or global) definition of the same name overrides them completely.

**Reviews only on request** (decided with the user, 2026-09-14). Nothing schedules a reviewer pass. `/review [what]` (`commands/review_cmd.py`) delegates one over `git diff HEAD` plus the changed-file list and relays the verdict. `/ralph` still prefers a project `architect` and falls back to the built-in `reviewer` when the only `architect` in scope is the built-in role (`ralph.reviewer_agent`).

## 4. Built-in commands (Python workflows) — `commands/{ralph,ultrawork,deepinit,team_cmd}.py`
- `/ralph <task>`: PRD loop — ask the model for 3–8 user stories with acceptance criteria (JSON) → iterate: pick first failing story → run implementation via subagents (≥2 concurrent when stories are independent) → run verification commands the story names (tests/build) via shell → mark passes → when all pass, run a reviewer subagent ("architect") that must answer APPROVE; otherwise loop (max `ralph.max_iterations`=10). State in `<workdir>/.snowpea/ralph/{prd.json,progress.md}`. Ends with `turn.done{reason:"complete"}` only when approved.
- `/ultrawork <task>`: split into independent subtasks (JSON) → fan out subagents in parallel → merge summaries. The splitter prompt (`prompts/workflows/ultrawork-split.md`) requires each subtask to own a **disjoint** set of files and to list them in `files`; `merge_overlapping` then validates the split and folds subtasks that claim the same file into one brief (which says so) before anything runs, logging each merge. (M15 §C4)
- `/review [what]`: one `reviewer` pass over the uncommitted diff, relayed as a verdict (§3.3).
- `/deepinit`: walk the repo, write hierarchical `AGENTS.md` (root + per top-level dir) via subagents.
- `/workers <N> "<task>"`: see §5. `/team "<task>"`: see §9.
- Bundled markdown skills: `builtin_skills/{deep-interview,deep-research,ralplan}/SKILL.md` — original texts written for snowpea (OMC has no `deep-research`; compose from its external-context/autoresearch ideas), loaded by the same loader (source=builtin). `/help` lists ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan, accept, auto (+ others) — the rendered list lives in `tui/src/components/HelpPanel.tsx`; keep it and `docs/design/m12-tui-contract.md` §4 pinned to the same constant.
- Replacement map for OMC concepts per `docs/omc-porting-map.md`.

## 5. Worker mode — `agent/team.py`, `/workers <N> "<task>"`
`team.start(sessionId, n, task) -> teamId`: lead (the session) splits `task` into tasks (JSON list with ids/deps) → shared task list persisted in state.db (`team_tasks`: id, team_id, title, status queued|claimed|done|conflict|merged|failed, agent_n, retries, conflict_hunks) → N worker subagents each get a git worktree `<workdir>/.snowpea/worktrees/<teamId>-<n>` on branch `snowpea/team-<teamId>-<n>` (created with `git worktree add -b …`) and loop: claim next queued task (atomic UPDATE), work, commit on their branch, mark done, post a message to the team board (`team_messages`) → lead merges done tasks in completion order with `git merge --no-ff <branch>` in the main worktree; on conflict `git merge --abort`, task → `conflict` (retries+1, hunks captured from `git diff --diff-filter=U` or the merge output) and re-queued to the same agent with the hunks in its prompt; after `team.max_conflict_retries` (default 2) → `failed` (hunks kept) and the lead continues. End: remove worktrees (`git worktree remove --force`) and branches; `team.status(teamId)` returns tasks with states; events `team.task.update{teamId, taskId, status, agentN, retries}`; CLI `snowpea workers status [runId]` (and `snowpea team status`, its older spelling); `/workers <N> "<task>"` command. Test uses the fake provider with a script that makes one worker always write conflicting content to the same file.

**Optional review stage (`team.review`, default `false`, M15 §C5).** When it is on, a task that merges cleanly is first read by a `reviewer` child over `git diff HEAD~1 HEAD` from a stand-in parent rooted at the repository (`_review_anchor`, outside the team roster so the §3.1 membership guard does not refuse it). A `REQUEST_CHANGES` verdict re-queues the task to the **same** worker once — status back to `queued` with `agent_n` kept, the findings handed to it through `workflows/team-review-fix.md` on its next brief — and every other verdict lets the merge stand. One review per task, however often it is re-queued; the merge is not reverted, because the rest of the board may already build on it.

A worker's stand-in parent (`TeamManager._anchor`) MUST carry the lead's `agent`, `team` and `team_agents` alongside its workdir, mode and route, and `_work` MUST name the worker (`"executor"` when there is no roster to pick from). Without a name `record.name` is empty, `agents.models` is never consulted, and every worktree worker silently ignores per-agent model profiles — and the §3.1 membership guard never fires either. (CORE-model-assignment)

## 6. Named persistent agents — `agent/named.py`
`agent.create(description)` (from §2) may also register a **named instance**: `named_agents` table (name, definition_path, session_id, memory_namespace `agent:<name>`, channel_bindings JSON, schedule_ids JSON, created_at). `agent.bindChannel(name, channel)` → gateway binding whose target is this agent (its persistent session, recreated on daemon start from the table); jobs may reference `agent` so they run in the agent's session/namespace. Survives restart: on daemon start `NamedAgentRegistry.restore()` recreates sessions, gateway bindings and re-attaches jobs. Memory isolation = namespace. Lifecycle counter `named_agents`. `agent.list` shows `kind:"named"` entries with bindings; `agent.delete(name)` unbinds and removes.

## 7. Tests
- `tests/test_plugin_load.py` (fixture plugin `tests/fixtures/plugins/sample-plugin/` pinned: plugin.json, skills/hello/SKILL.md, agents/fixture-agent.md, commands/fixture-cmd.md, hooks/hooks.json PreToolUse→writes `$SNOWPEA_HOME/fixture-hook.marker` containing the tool name, .mcp.json → `tests/fixtures/mcp/echo_server.py`): 5 kinds loaded; `/fixture-cmd` runs; hook marker; `mcp__fixture-echo__echo` round trip; `skill.search("pdf")` returns 3 sources from offline fixtures.
- `tests/test_delegation_policy.py` (M15 §C: duplicate refused and `force` allowed, sibling ownership refusal and its `tools.readBeforeWrite` switch, ultrawork overlap merge, `explore`/`reviewer` load with enforced allowlists and are overridable by a project file, `/review` delegates the diff and relays a verdict, report spill pointer and per-reason next step, the `team.review` re-queue loop).
- `tests/test_generators.py`, `tests/test_ralph_e2e.py` (≥2 concurrent running subagents observed via `agent.list`, non-empty git diff, turn.done complete), `tests/test_team_worktree.py` (3 merged; injected conflict → retries 2 → failed; worktrees cleaned), `tests/test_named_agent_persistence.py` (2 agents, restart, bindings kept, memory isolated), SDK `--grep subagent` contract test (AC-15b).

## 8. Project teams (`agent/team_config.py`, `commands/team_cmd.py`)

Added in v0.1.x. A **team** is a named list of agent names that bounds what a session may delegate to. Two settings surfaces define them:

| scope | file | keys |
|---|---|---|
| global | `$SNOWPEA_HOME/settings.json` | `agents.teams: {name: [agentNames]}`, `agents.default_team: str\|null` (`config/settings.py`, `AgentsSettings`) |
| project | `<workdir>/.snowpea/settings.json` | `agents.teams`, `agents.activeTeam: str\|null` (`config/project.py`, `ProjectAgentsSettings`) |

Resolution (`agent/team_config.py`):
- `teams_for()` merges global then project, so **a project team wins on a name clash** — a project may redefine `default` without touching the user's global file.
- `active_team()` reads only the project's `activeTeam` (written by `/team create` / `/team use`). The global `settings.agents.default_team` is a *roster* — what `/team` starts from when no team is named (`default_roster()`) — never a whitelist: it used to activate a team in every session and hid project, named and built-in agents from delegation (fixed 2026-09-15, M15 C). Inside an explicit team, a persistent named agent stays delegatable. No active team = unrestricted delegation.
- Member lists are de-duplicated preserving order (`dict.fromkeys`).

`Settings.load()` migrates older installations at first read: a settings document that has an `agents` block but no `teams` gets `teams["default"] = DEFAULT_AGENT_TEAM` (architect, critic, executor, explorer, test-engineer, verifier) and `default_team = "default"`. An explicitly empty team set is still representable afterwards as `default_team: null`.

**Effects of an active team**, all three of which MUST hold together:
1. The system prompt carries the restriction rule (M1 §17-7, `agent/agent.py`).
2. `delegate_task` / `agent.spawn` refuse a non-member (§3.1).
3. `agent.list` filters definitions down to the team's members and **prepends one `kind:"team"` row per team the user could pick here** (`server/agent_handlers.py`, `_team_rows`) — every global and project team from `teams_with_source()`, the active one first, then by name. `kind="team"` is a fourth value alongside `definition|subagent|named`; a client that does not know it MUST render it as an ordinary row rather than dropping it. Named instances and running subagents are appended after the filter and are **not** filtered by team.

Each team row carries (all additive to `AgentInfo`, v0.1.x):

| field | meaning |
|---|---|
| `active` | `true` for the project's `activeTeam`; at most one row, none when the project has chosen no team |
| `source` | `"project"` or `"global"` — where the winning definition came from (a project team wins a name clash) |
| `agents` | its member names, in roster order |
| `stages` | `{explore?, plan?, implement, test?, review?} -> member`, the §9 mapping; **empty** when the team has no implementer and therefore cannot run `/team "<task>"` at all |

A team that cannot run the pipeline is listed with empty `stages` rather than hidden: a picker still has to show it, and say why it is not offered.

`/team` has four configuration subcommands (`commands/team_cmd.py`); the `N <task>` form it used to carry is now `/workers` (§9):

```text
/workers <N> "<task>"           # §5: run N identical workers on one task
/team create <name> <agent...>  # define/redefine a project team
/team use <name>                # set the project's activeTeam
/team use none                  # clear it (also --none, -): no team, unrestricted delegation
/team list                      # every team visible here, marking the active one
/team delete <name>             # remove a project team
```

The first word disambiguates; the full grammar is in §9. `/team use <unknown>` answers `team: unknown team <name>; use /team list` and changes nothing. `/team use none` clears `activeTeam`, so the session goes back to unrestricted delegation and `/team "<task>"` falls back to the global default roster. `/team list` prints each team with its source and the stage every member fills.

**TUI short delegation.** `$agent-name <task>` in the input line is a surface-local shortcut that calls `agent.spawn` directly rather than going through `session.prompt` (`tui/src/app.tsx`, pattern `/^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/`). The same refusal rules apply — the daemon does not know the input came from a shortcut.

## 9. Team pipeline mode (`agent/team_pipeline.py`) — `/team "<task>"`

Added in v0.1.x. `/team` now wraps **two** modes, and the first word decides which:

```text
/team "<task>"                  # the active team's members, by role
/team run "<task>"              # the same, spelled explicitly
/team <name> "<task>"           # the same on a named team, for this run only
/workers <N> "<task>"           # §5: N identical workers, one git worktree each
```

**Worker mode moved off `/team` (v0.1.x).** N identical workers are `/workers <N> "<task>"` (alias `/worker`, `commands/workers_cmd.py`); `/team` is the roster, by role, and nothing else. The two were never variations of one idea — a team is the people you assembled each doing the job their role implies, workers are N copies of one anonymous agent racing through a task list — and telling them apart by whether the first word happened to be a number was a puzzle rather than a grammar. `/team <N> …` now answers `worker mode is /workers <N> "<task>"` and runs nothing: a silent fallback would leave someone who typed the old spelling with no idea the grammar had changed. The machinery is untouched; only the spelling moved, and `TeamManager` still owns the board, the worktrees and the merges.

The first word disambiguates, in this order: `create|use|list|delete` is configuration; a leading integer is the moved-mode hint; `run` is the explicit pipeline spelling; a bare word followed by a **quoted** task is a team name; anything else is the task itself. The quotes are what make a leading word a team name — `/team add docstrings to the parser` is a task, not an unknown team called `add`. An unknown team name answers with the teams that do exist and runs nothing.

**`/team <name> "<task>"` never writes `activeTeam`.** The run delegates through a stand-in parent session (`TeamPipeline._anchor`) that carries the lead's id, workdir, mode, route and backend but the *named* team in `team`/`team_agents`, so the §3.1 membership guard admits that roster for the run and nothing outside it. Sharing the lead's id keeps the `subagent.*` events, the concurrency semaphore and the sibling-file registry where they were. The project's own settings are untouched, which is the whole point: a global ("external") team is as runnable as an adopted one.

`/team "<task>"` with no active team falls back to the global `agents.default_team` roster; with no roster at all it answers `no active team — /team use <name>, or /team <name> "<task>" to run one just this once` and runs nothing.

Pipeline mode runs the roster **as ordinary subagents in the session's own checkout** — no worktrees, no board, no merge. Every stage is one `SubagentManager.run` on the lead's session, so the `subagent.spawn|update|done` events a TUI or IDE already renders show the pipeline as a tree under the lead. No new RPC method and no new event: the stage is carried in the delegation's `title` (`plan`, `implement: <task title>`, `test`, `review`, `review (2)`, `fix`), not in a new `SubagentSpawn` field.

**Roster → stages.** The roster is the active project team (`agents.activeTeam`) or, when the session has none, the global `agents.default_team` roster (`default_roster()`, §8). Each stage takes the first roster member that resolves to a definition here, best candidate first, and no agent owns two stages:

| stage | candidates | when absent |
|---|---|---|
| `explore` | `explore`, `explorer` | skipped |
| `plan` | `architect`, `planner` | the lead plans for itself, one plain provider call |
| `implement` | `executor` | **error** — `/team <N>` is the mode for a roster with no implementer |
| `test` | `test-engineer` | skipped |
| `review` | `reviewer`, `critic`, `verifier` | skipped |

Roster members matching no stage are reported (`not used by any stage: …`) rather than dropped silently, and `/team list` prints the stage each member of the active roster fills. The default roster (architect, critic, executor, explorer, test-engineer, verifier) therefore fills every stage, with `verifier` unused.

**Stages.**
1. **explore** (optional) — a read-only survey of the project, ≤ 20 lines of findings, handed to `plan`.
2. **plan** — `workflows/team-pipeline-plan.md`: one JSON object `{"tasks": [{id, title, brief, files[], dependsOn[]}]}`, at most `team.pipeline.maxTasks` (8, hard ceiling `MAX_TASKS`). Validated in code: a `dependsOn` naming a task not listed *before* this one is dropped (the list is ordered, so a forward or circular reference would leave nothing ready), and tasks claiming the same file are folded into one brief by the same `merge_overlapping` rule `/ultrawork` uses (M15 §C4) — there are no worktrees, so two agents on one file is how a run loses half a change.
3. **implement** — the tasks run in dependency order, **one owner per task**. Tasks whose file sets are disjoint run in the same wave, up to the resolved `agents.max_concurrent`; a task that claims no file runs alone, because nothing can be proved disjoint from it. `file_state`'s sibling-ownership guard (§3.2) is the safety net under the file scoping. Each brief carries `${BASE_RULES}`, the task's file list, and the previous stage's handoff.
4. **test** (optional) — runs the project's checks over the changed files and answers `TESTS: PASS` / `TESTS: FAIL` with the commands it ran.
5. **review** (optional) — the reviewer gets `git diff` of the changed files and answers `VERDICT: APPROVE` / `REQUEST_CHANGES` / `NEEDS_MORE_EVIDENCE`. `REQUEST_CHANGES` buys exactly one **fix** pass by the implementer of the task whose files the findings name, then one more review, told it is a second look. Still `REQUEST_CHANGES` after that (`MAX_REVIEW_ROUNDS = 2`) ends the run and the report says so; there is no third round.
6. **report** — composed by the lead **in code**, in `render_report` shape: a header (`stages:` / `tasks: n/m finished` / `tests:` / `review:`), what each task changed (its own first report line, trimmed), the files the plan claimed, and what was left unfinished. A child's raw output is never relayed whole.

**Handoffs** are the previous stage's report trimmed to `HANDOFF_LINES` (20) and prefixed `What <stage> handed over:`. Implementers and fixers are asked to answer in `Decided:` / `Files:` / `Remaining:` lines so the handoff is already the right shape. Nothing is written to disk and a pipeline run is not resumable.

**Stage reporting.** `stages_map(core, workdir, roster)` is the non-raising counterpart of `stage_assignments` — it answers `{}` for a roster with no implementer instead of raising — and is what `agent.list` (§8) and `/team list` describe teams with. `stage_line` renders it as one line.

**Settings** (`config/settings.py`, `TeamPipelineSettings`):

| key | default | meaning |
|---|---|---|
| `team.pipeline.maxTasks` | `8` | ceiling on the plan stage's task list |
| `team.pipeline.review` | unset | unset = on when the roster has a reviewer; `false` turns it off anyway |
| `team.pipeline.test` | unset | unset = on when the roster has a test agent; `false` turns it off anyway |

`team.review` and `team.max_conflict_retries` still belong to worker mode (§5) and are unaffected.

The staged shape is ported from oh-my-claudecode's `team` skill; see `docs/design/deviations/CORE-team-pipeline.md`. Tests: `tests/test_team_pipeline.py`.
