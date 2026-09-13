# Deviations — CORE-teams (project teams, `$agent task`, and the refusal of unknown agent names)

`cf78386` adds reusable agent teams — a named set of agents a session may delegate to — plus the
`$agent task` short form, and closes the hole that made a typo in an agent name look like success.
Recorded here per `docs/design/deviations/README.md`.

1. **Breaking: an unknown agent name is now refused everywhere, not only inside a team.**
   `subagent.py:392-399` routes an unresolvable name through `_refuse()` instead of silently running
   the child with the parent's own settings. The old fallback meant `delegate_task(agent="exectuor")`
   produced a plausible answer from a generic agent and nobody ever learned the name was wrong. The
   breakage is deliberate and is the reason the commit exists; `tests/test_subagents.py` replaced
   `..._still_runs` with `test_an_unknown_agent_name_is_refused` to pin it.

2. **A delegation with no agent named falls back to `"executor"`, or the first team member.**
   `subagent.py:374`: `agent = "executor" if "executor" in parent.team_agents else parent.team_agents[0]`.
   Erroring on an omitted agent was rejected — the model omitting the field is a routine
   underspecification, not a typo — and picking deterministically beats picking at random or asking.

3. **Team membership is validated only at `/team create`, not by a settings validator.** Unlike
   model profiles, which `Settings._validate_model_profile_refs` rejects at load time, a bogus agent
   name hand-edited into `agents.teams` in `settings.json` loads cleanly and only fails later at
   delegation, through rule 1's refusal. This asymmetry is a recorded known gap (R11): the failure is
   now at least loud, but it arrives at delegation time rather than at load time.

4. **`agent.list` reports the active team as a synthetic `kind:"team"` row rather than gaining a
   `team.get` method.** `server/agent_handlers.py:98-107` prepends one `AgentInfo` whose `kind` is
   `"team"` when the session has a team, and filters the definition rows to the team's members. One
   method keeps every surface's refresh path unchanged. The cost is that a client which buckets rows
   by assuming `kind` is always a spawnable definition will list the team as an agent and offer to
   spawn it — which is exactly what the IDE does today (GAP-1). A client must branch on
   `kind === "team"` before the definition fallthrough and render it as a non-spawnable badge.

5. **`Settings.load()` migrates existing installs by writing a `"default"` team.**
   `config/settings.py:292-297`: when a settings file has an `agents` block but no teams, the loader
   seeds `teams["default"] = DEFAULT_AGENT_TEAM` and `default_team = "default"`. Migrating on load
   rather than shipping a class-level default means an explicit empty team can still be expressed —
   `default_team: null` after a team exists — instead of being indistinguishable from "never
   configured".

6. **Project teams win over global teams on a name clash** (`agent/team_config.py`), matching how
   every other `.snowpea/` setting overrides its global counterpart, and the team restriction is
   injected into the system prompt (`agent/agent.py:128-133`) rather than only enforced at the tool
   boundary — the model should know which agents exist before it picks one, not be corrected after.
