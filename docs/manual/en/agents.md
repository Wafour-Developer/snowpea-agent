# Agents and delegation

Other languages: [한국어](../ko/agents.md)

A **subagent** is one delegated task running in its own session. It inherits your working directory, your mode, your provider and your backend, and nothing else: it cannot see this conversation, it cannot ask you a question, and only its final message comes back. That isolation is the point — a search that would flood your context with a hundred file excerpts costs you one paragraph instead.

The agent delegates with the `delegate_task` tool. You can do it yourself with `/delegate <agent> <task>`, or with the `$<agent> <task>` shorthand at the start of a prompt.

```text
$explore where does the daemon decide the reply language?
/delegate reviewer look at the change in tools/fs.py
```

## One task, one agent

Delegation goes wrong in one particular way: the same work gets two owners. Two children editing one file produce half of each change, and a parent that delegates a task and then does it anyway pays for it twice. So the rule the agent is held to is one task, one agent, and two guards make it true rather than merely stated.

**A duplicate is refused.** While a child is working, a second `delegate_task` with the same agent and the same brief comes back as an error naming the child that is already on it. Whitespace and capitalisation do not make a brief different. If you genuinely want two attempts at one task — best-of-N — the tool takes `force: true`.

**A running sibling owns the files it wrote.** When two children work in parallel and one writes a file the other then tries to edit, the second is refused with `file_owned_by_sibling` and told to report the collision instead. The parent then serialises the work. The guard follows the `tools.readBeforeWrite` setting, and it only applies while the sibling is still running: once it has reported, an ordinary re-read is all that is needed.

Parallel delegation without worktrees is therefore only safe across disjoint files. `/team` gives every worker its own git worktree and does not have this limit.

## The built-in agents

| Name | What it is for |
|---|---|
| `explore` | read-only search: finds where something lives and reports it as `path:line` evidence |
| `reviewer` | read-only review: a verdict plus findings, each with the evidence behind it |
| `executor`, `architect`, `critic`, `verifier`, `explorer`, `test-engineer` | role prompts, with every tool available |

`explore` and `reviewer` carry a tool allowlist, so they are read-only in fact and not only by instruction: no `write_file`, no `edit_file`, no `shell`. A definition of the same name in `<project>/.snowpea/agents/` overrides the built-in one completely.

### `explore` vs `explorer`, `reviewer` vs `critic`

Two pairs read like duplicates and are not. `explore` and `reviewer` are the **built-in agents you delegate to**: read-only, tool-allowlisted, meant for one question or one diff. `explorer` and `critic` are **team roles**, filled by the pipeline's explore and review stages, with every tool available. Pick the built-in when you are asking for something now; the role name is what a roster is written in.

| Delegate to | Team role | Difference |
|---|---|---|
| `explore` | `explorer` | `explore` is the built-in read-only search agent; `explorer` owns the pipeline's explore stage |
| `reviewer` | `critic` | `reviewer` is the built-in on-request review agent; `critic` owns the pipeline's review stage |

`snowpea agents` prints the same sentence in each description, so the list itself says which is which.

### Where a definition came from

Each row carries the root it was read from, not just the word `project`:

| `source` | Read from |
|---|---|
| `builtin` | shipped with snowpea |
| `global` | `~/.snowpea/agents/` |
| `project` | `<project>/.snowpea/agents/` |
| `claude-global` | `~/.claude/agents/` |
| `claude-project` | `<project>/.claude/agents/` |

Claude Code's own agent directories are read as they are, and labelled as theirs, so an agent you did not write for snowpea is recognisable at a glance.

`explore` takes a thoroughness level in the brief — quick, medium, or very thorough — and reports findings as text with absolute paths, never as file dumps. `reviewer` opens the files it judges and answers with one of three verdicts:

```text
VERDICT: APPROVE
VERDICT: REQUEST_CHANGES
VERDICT: NEEDS_MORE_EVIDENCE
```

Findings without evidence are opinions, so each finding names a location, what goes wrong, and the condition that triggers it.

## Reviews happen when you ask for them

Nothing starts a review on its own. `/review` runs one over whatever is uncommitted in your working tree, and relays the verdict:

```text
/review
/review the error handling in the new parser
```

`/ralph` also ends its loop with a review. It uses your own `architect` definition when the project has one, and the built-in `reviewer` otherwise.

Team mode can review each task before it is accepted, which is off by default:

```json
{ "team": { "review": true } }
```

With it on, a `reviewer` child reads the merge of each finished task. A `REQUEST_CHANGES` verdict sends that task back to the same worker once, with the findings attached; anything else lets the merge stand.

## Teams and workers are two commands

```text
/workers 3 "add docstrings to the three parser modules"   # three identical workers
/team "add docstrings to the three parser modules"        # your team, by role
```

They used to be one command told apart by whether the first word was a number, which was a puzzle rather than a grammar. A **team** is the people you assembled, each doing the job their role implies. **Workers** are N copies of one anonymous agent racing through a task list. `/team 3 "…"` still runs worker mode, and says once that `/workers N` is the current spelling.

**`/workers <N> "<task>"`** (alias `/worker`) is the mode that moved: N identical workers, one git worktree each, branches merged by the lead as tasks finish.

**`/team "<task>"`** runs the members of your active project team, each in the role its name implies, one stage after another in your own checkout:

```text
explore? -> plan -> implement -> test? -> verify? -> review? -> fix? -> review?
```

Every stage is an ordinary subagent, so you see the whole pipeline in the agent tree. Who fills which stage comes from the roster, never from the model:

| Stage | Taken by | If nobody fits |
|---|---|---|
| explore | `explore`, then `explorer` | skipped |
| plan | `architect`, then `planner` | the lead plans for itself |
| implement | `executor` | the command stops and tells you |
| test | `test-engineer` | skipped |
| verify | `verifier` | skipped, with a note in the report |
| review | `critic`, then `reviewer` | skipped |

### Picking which team runs it

`/team "<task>"` uses the project's active team. To run a different one just this once, name it:

```text
/team external "add docstrings to the three parser modules"
```

The named team may be one of the project's own or a global one from your `settings.json`, and running it changes nothing: the project's active team is exactly what it was afterwards. An unknown name is answered with the list of teams you do have.

`/team list` shows every team, where it came from, and the stage each member fills, so a roster is never just a list of names. A member matching no stage is named too, rather than quietly ignored. `/team use <name>` switches the active team and `/team use none` clears it, which puts delegation back to unrestricted.

The same list reaches a client through `agent.list`: one row per team with `kind: "team"`, carrying `active`, `source` (`global` or `project`), `agents` and `stages`. A team with no implementer is listed with an empty `stages`, so a picker can show it and say why it cannot run.

There are no worktrees here, so the plan has to keep the work apart by hand: the plan stage names the files each task owns, tasks claiming the same file are merged into one before anything runs, and tasks with disjoint files run together up to `agents.max_concurrent`. A task that names no file runs on its own.

The test, verify and review stages each answer with one explicit line — `TESTS: PASS` or `TESTS: FAIL`, `VERIFY: PASS` or `VERIFY: FAIL`, `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`. A report with no such line, an empty report, a denied approval, or an approval with no tool call behind it counts as `NEEDS_MORE_EVIDENCE`, which is not a pass. The final report says `Nothing was left unfinished.` only when the tests really passed and the review really approved; otherwise it lists what did not, and a headless run fails.

A `REQUEST_CHANGES` verdict buys one fix pass by whoever wrote the code the findings point at, and one more review. If the reviewer still wants changes after that, the run ends and the report says what is unfinished — there is no third round.

Three settings shape it:

```json
{ "team": { "pipeline": { "maxTasks": 8, "review": true, "test": false } } }
```

`maxTasks` caps the plan. `review` and `test` are on whenever your roster has somebody for that stage; set either to `false` to turn the stage off anyway.

### Stage hand-offs and audit trail

Between pipeline stages, agents pass a concise 10–20 line structured hand-off block (````handoff ... ````) containing:
- `Decided`: key decisions made during the stage
- `Files touched`: files edited or checked
- `Findings`: concrete findings with `path:line` references
- `Remaining`: unfinished tasks or "nothing"
- `Risks`: potential risks or caveats

If no fence is present, the first 20 lines are kept. Hand-offs are stored on disk under:

```text
<workdir>/.snowpea/handoffs/<team-run-id>/<stage>.md
```

Each subsequent stage receives all prior hand-offs verbatim, plus `git diff --stat` and `git diff` capped at 20,000 characters (spilled to a cache file with a reader pointer if larger). At the end of the run, the summary lists all hand-off file paths and prints a telemetry table with per-stage rounds, budgets, and token usage.

## Tool round budgets and grace call

To prevent runaway token spend, subagents and pipeline stages operate with role-specific tool round budgets.

| Role / Agent | Default tool rounds |
|---|---|
| `explore`, `explorer` | 8 |
| `reviewer`, `critic` | 12 |
| `test-engineer` | 15 |
| `verifier` | 10 |
| `architect` | 10 |
| `executor` & other subagents | 32 |

### Configuring round budgets

Budgets are resolved in precedence order:
1. `agents.maxToolRoundsBy.<role>` (e.g. `{ "agents": { "maxToolRoundsBy": { "explore": 10 } } }`)
2. `agents.maxToolRounds` global scalar in `settings.json`
3. `max_tool_rounds` in the agent definition frontmatter
4. Role defaults shown above
5. Fallback of 32 rounds

### Toolless grace call on budget exhaustion

Modeled on Hermes Agent, when a subagent exhausts its tool rounds (`rounds_left <= 0`), the run is not aborted abruptly. The loop invokes one final toolless "grace" call (`_budget_report`), giving the model a final turn with no tools to summarize what was done, what was learned, and what remains unfinished. The result is returned with reason `budget` and status `done`.

## What a report is and is not

A child's report is a self-report. It says what the child believes it did, which is not the same as what happened. For anything with an effect outside the session — a file written, something uploaded, a service called — ask for a handle in the brief (a path, a URL, an id) and check it yourself before telling anyone it worked.

The result the agent reads starts with three lines:

```text
status: done
reason: budget
roundsUsed: 40
```

`reason` says why the child stopped. `complete` means it finished; `budget` and `timeout` mean it ran out of rounds or time and the report is partial; `error`, `interrupted` and `denied` speak for themselves. A partial report is still useful — the right move is to take what is done and delegate only what is left, never to re-send the same task. A long report is trimmed to its head and tail with a pointer to the full text on disk, which the agent can read back with `read_file`.

## Concurrency

`agents.max_concurrent` (3 by default) bounds how many children one parent runs at once; a project's `.snowpea/settings.json` can lower or raise it, and `session.create(maxConcurrent)` overrides both for one session. Children beyond the limit queue rather than fail.

`/ultrawork <task>` splits a task into parallel subtasks. The splitter is asked to give each subtask a disjoint set of files, and the split is then checked: subtasks that claim the same file are merged into one brief before anything runs, and the merge is reported in the output.

```bash
snowpea agents --json
```

lists the children that are queued or running right now.

## Child context diet

Delegated children (`delegate_task`) start with an empty history and a brief explaining the task. Handing a child the parent's full prompt — the memory recall block, the full skills index, and every nested `AGENTS.md` — wastes hundreds of thousands of input tokens across its rounds.

Setting `agents.childContext` controls the context diet:

| setting | default | values | what it does |
|---|---|---|---|
| `agents.childContext` | `"lean"` | `"lean"`, `"full"` | `"lean"` strips the memory recall block, the skills index, and nested instruction files from delegated children, and narrows read-only child tools. `"full"` restores the parent's prompt for children. |

In `"lean"` mode:
- **No memory block or guidance**: the child has no memory recall block or guidance line (though memory tools still work if permitted).
- **No skills index**: installed skills are not enumerated in the prompt, but the child can still view any skill by name with `skill_view`.
- **Root instructions only**: only the root `AGENTS.md` or `CLAUDE.md` is loaded up front; nested instruction files attach on demand when a tool touches that directory.
- **Smaller eager tool set**: read-only children (`explore`, `reviewer`) start with `read_file`, `grep`, `glob`, `shell` (if allowed), and `tool_search`. Other tools are deferred and loaded on demand.
- **Preserved**: the child's definition prompt, role, and the tool round budget line (`BUDGET_LINE`) are always preserved.

