# CORE-team-pipeline — `/team "<task>"` runs the roster by role

**Ported from:** oh-my-claudecode (MIT), `skills/team/SKILL.md` (v4.15.1) — the
"Staged Pipeline (Canonical Team Runtime)" section, its stage-agent routing
table, the stage handoff convention and the bounded fix loop.

**What was ported:** the *shape*, not the text. No wording, prompt or table
from the source file is copied into snowpea. What was taken:

1. A staged pipeline instead of a fan-out — OMC's `team-plan → team-prd →
   team-exec → team-verify → team-fix`. snowpea runs
   `explore? → plan → implement → test? → review? → fix? → review?`.
2. **Pre-assigned owners.** OMC's lead assigns each task an owner up front
   because its task list has no atomic claiming. snowpea's `/team N` board does
   have atomic claiming (`agent/team.py`), but the pipeline has no board at
   all: every stage and every task is given its owner by the lead before it
   runs, from the roster.
3. **File-scoped subtasks.** OMC: "each subtask should be file-scoped or
   module-scoped to avoid conflicts". snowpea already had the stronger version
   of this in `/ultrawork` (M15 §C4) and reuses it: the plan must list the
   files each task owns, and tasks claiming the same file are merged into one
   brief before anything runs.
4. **Handoff documents, 10–20 lines, decisions not specifications.** snowpea
   keeps the size limit and the Decided/Files/Remaining fields, but does not
   write them to disk: a handoff is the previous stage's own report, trimmed
   and passed into the next brief. There is no `.omc/handoffs/` equivalent
   because snowpea has no resume for a pipeline run.
5. **A bounded fix loop.** OMC bounds `team-fix` by a max-attempts counter and
   goes to a terminal `failed` rather than looping. snowpea allows exactly one
   review, one fix and one re-review (`MAX_REVIEW_ROUNDS = 2`) and reports the
   run as unfinished afterwards.

**What was deliberately not ported:**

- OMC's `team-prd` stage. snowpea has `/ralph` for PRD-shaped work, and a
  second planning stage on every `/team` run is a model turn the user did not
  ask for.
- The stage-agent *routing table*. OMC's lead picks agents per stage by task
  characteristics and cost mode. snowpea takes the owners from the project's
  own roster (`agents.activeTeam`, else `agents.default_team`), because the
  roster is the thing the user configured and M15 §C makes it the boundary of
  what a session may delegate to. Fallback chains per stage replace the table.
- Persisted pipeline state, resume, the watchdog, and worker reassignment.
  A stage here is one `SubagentManager.run` the lead awaits; there is nothing
  to poll and nothing to restart.
- tmux/CLI workers, `state_write`, `TodoWrite`.

**Interpretation recorded for review.** The brief mapped `explore = explore |
explorer` among the stages but listed no explore stage in the pipeline. It is
implemented as an optional stage that runs before `plan` when (and only when)
the roster has an explore agent, and its findings become the plan stage's
handoff. A mapped stage that could never run would be dead configuration.

**Beyond the OMC shape (snowpea's own).** Two things have no counterpart in the
source skill, because they answer a need snowpea's surfaces have and a CLI
skill does not:

- **Named one-off runs.** `/team <name> "<task>"` runs any project or global
  team for one invocation through a stand-in parent session carrying that
  roster, leaving `activeTeam` alone. OMC's lead picks agents per stage itself
  and has no roster to borrow.
- **Team rows in `agent.list`.** Every team the user could pick, with its
  members, source and stage mapping, so a desktop or IDE surface can offer
  "assign a team to this project" without reimplementing the mapping.

**Where it lives:** `core/snowpea_core/agent/team_pipeline.py`,
`core/snowpea_core/prompts/workflows/team-pipeline-*.md`,
`core/snowpea_core/commands/team_cmd.py`. Contract:
`docs/design/m6-m7-skills-agents-contract.md` §9. Tests:
`tests/test_team_pipeline.py`.
