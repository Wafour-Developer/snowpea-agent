# CORE-team-guide — team persona and routing

Product intent: let a project attach a **persona** and **routing rules** to a team (or to `default` when no team is active), and inject that guidance into the lead session, pipeline stages, and delegated children.

## What landed

- Markdown guides at `<workdir>/.snowpea/teams/<team>.md` (project) and `~/.snowpea/teams/<team>.md` (global). Project wins on a name clash.
- `default` applies only when `agents.activeTeam` is unset. An active team without its own guide does **not** inherit `default`.
- `core/snowpea_core/agent/team_guide.py` loads, saves, renders (`lead` vs `worker`), and caches by `(path, mtime)`.
- Injection: `build_system_prompt`, `/team` pipeline and worktree workflow briefs (`${TEAM_GUIDE}`), and subagent briefs.
- `/team guide …`, `snowpea team guide …`, and `team.guide.*` RPC with `teams.changed` notifications.
- `agent.list` team rows expose `hasGuide`.

## Tests

- `tests/test_team_guide.py` — parse/precedence/safety, rendering, injection surfaces, commands, RPC, protocol registration.
