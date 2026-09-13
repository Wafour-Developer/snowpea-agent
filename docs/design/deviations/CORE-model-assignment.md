# Deviations — CORE-model-assignment (per-agent model routing, end to end)

Section 7 of the v0.1.7 commit review asked a simple question — "which model does this agent
talk to?" — and found that the answer users expected did not exist. Three of the five rungs
people assumed were there were missing entirely, and the one rung that *was* there was resolved
by a private helper that never looked at `models.profiles`. This file records what was built and
the decisions taken along the way. Recorded per `docs/design/deviations/README.md`.

## What the chain is now

`route_for()` (`config/model_routing.py`) is still the only policy, and still has exactly one
production call site — `SessionManager.create` — which is the point: one place decides.

| # | Rung | Where it comes from |
|---|---|---|
| 1 | explicit override | `delegate_task(model=…)`, `agent.spawn(model=…)`, `--provider` |
| 2 | the agent's assignment | project `models.agents[<agent>]` over global `agents.models[<agent>]`, then the definition's `.md` `model:` |
| 3 | the session pin | `/model <ref>`, `session.setModel` |
| 4 | the project default | `<workdir>/.snowpea/settings.json` → `models.default` |
| 5 | the global default | `$SNOWPEA_HOME/settings.json` → `models.default` |

Unresolved is `ModelRoute(None, None)` — "no opinion" — and the provider registry's defaults
apply, as before.

**The one place the lead's ordering needed interpretation.** The brief listed the project `models`
block (default *and* per-agent) below the session pin, while also saying it merges over global.
Those two pull apart for the per-agent half: an assignment is a statement about *an agent*, and a
pin is a statement about *this conversation*, so a project's per-agent assignment belongs beside
the global one it overrides, not below the pin. The chain above therefore merges the per-agent
maps at rung 2 and keeps only the project *default* at rung 4. Every other relative order is the
brief's.

## The bugs

**B-P1-1 — a profile id in an agent `.md` was read as a vendor name.** `agent/subagent.py` had a
second copy of the precedence rule. When `models.default` was unset *and* the agent had no
`agents.models` entry, it resolved the definition's `model:` through `_split_model()`, which is not
`resolve_reference` and never consulted `models.profiles`. So `model: fast` — a perfectly valid
profile id — became `provider="fast", model=None`, and the child died at its first turn with
`unknown provider vendor: fast`. The bypass and `_split_model` are both deleted; `route_for` owns
it. The parent's own route is now passed down as the *session pin*, which is what makes the
inheritance the bypass was protecting work through the normal chain instead of around it. This also
retires R10 from the earlier pass.

**B-P1-2 — an unknown profile reference bricked the daemon.** `Settings.load` caught only
`OSError`/`JSONDecodeError`, so `_validate_model_profile_refs` raising escaped and the daemon exited
on startup — which left no `settings.set` to repair it with. Fail-closed and unrecoverable without
hand-editing JSON.

`Settings.load` now catches `ValidationError`, and **only** for this failure: `_drop_unresolvable_model_refs`
removes the `models.default` / `agents.models` entries that name no profile, logs exactly what went
at ERROR, and re-validates. If nothing was droppable the original error is re-raised — a genuinely
broken settings file is still a hard error, which `test_a_genuinely_broken_settings_file_still_raises`
pins. The strict validator is untouched on the `settings.set` path, where `invalid_params` is the
right answer.

**B-P1-3 — a broken `models.default` hijacked `default_vendor()`.** `ModelProfile` only checks that
the strings are non-empty, and `default_vendor()` returned the profile's vendor *ahead of*
`settings.providers.default` — so one bad default profile broke **every** session, not just routed
ones. It is now guarded with `profile[0] in PRESETS` and logs a warning when it falls through.

A `ModelProfile` validator rejecting unknown providers was considered and **not** added: a local
vLLM or a plugin-registered vendor is a legitimate provider that is not in `PRESETS`, and rejecting
it at load would trade this bug for a worse one. The guard is at the point of use instead.

**B-P2-2 — `/team` workers ignored per-agent profiles.** `_anchor()` copied the lead's workdir,
mode, route and backend but not its `team`/`team_agents`, so `SubagentManager.run` never filled in
the `"executor"` default, `record.name` stayed empty and `agents.models` was never consulted for a
single worktree worker — and the membership guard never fired either. The anchor now carries
`agent`, `team` and `team_agents`, and `_work` names the worker `"executor"` when there is no roster
to pick from.

## The three missing rungs

**The tool-call override (rung 1).** `delegate_task` gained a `model` property and `agent.spawn` a
`model` param; both resolve through the project-merged profile table and land on the child session.
An unresolvable reference **fails the delegation** rather than falling back: the caller named a
specific model, and quietly substituting another is the kind of thing that wastes an afternoon.

**The session pin (rung 3).** `session.setModel {sessionId, model}` is new and additive, and
`/model <ref>` goes through the same `SessionManager.set_model`. The pin is written to the
**session row** (`Store.update_model`, next to `update_mode`), so it survives a restart and a
`session.resume` — it used to live in memory plus `settings.providers.<vendor>.model` and was lost
on restore. `null`/`inherit` clears it, and clearing re-runs the configured routing rather than
leaving the session with nothing. A new `model.changed` session event carries the new route, so a
HUD fed once by `session/ready` stops going stale.

**Project model settings (rung 4).** `ProjectSettings` gained a real `models` block —
`default`, `profiles`, `agents`. It was `extra="allow"`, so a hand-added `models` key was already
being silently accepted and silently ignored; now it is read. `ModelProfile` moved to
`config/project.py` (re-exported from `config/settings.py`, so the old import path still works)
because the project document needed it and `settings.py` already imports from `project.py`.

**B-P2-1 — `definition_model` is derived, not a caller obligation.** Three of the five
`sessions.create()` callers silently dropped it. Rather than fix three call sites and wait for the
fourth to be written, `SessionManager.create` now looks the definition up itself through a
`definition_model_for` hook injected by `wire_core` (the lookup needs the plugin loader, which
lives on `Core`).

## `null` deletes — the settings patch sentinel

A deep merge cannot express a removal, so there was no way to delete a model profile, an agent
assignment or a team through `settings.set` **at all**: the key survived every patch. `deep_merge`
now treats `null` as "delete this key", in `config/patch.py`, with `server/settings_handlers.py`
importing it rather than keeping its own copy.

This is safe rather than clever: every optional field in `Settings` and `ProjectSettings` defaults
to `None`, so for a scalar, deleting the key and setting it to `null` validate to exactly the same
document — checked programmatically, not assumed. The validator still runs afterwards, so deleting
a profile something still references is refused with `invalid_params`; move `models.default` off it
first. That refusal is pinned by a test, because it is the behaviour a client will meet.

## Surfaces

`/model` now lists the configured profiles (project merged over global, default marked) above the
vendor's models, takes a profile id / `vendor:model` / bare vendor / row number / `inherit`, and
`/model default <id>` writes `models.default`. A plain model id of the current vendor still works
and still persists to `settings.providers.<vendor>.model`, so the `local` preset escape hatch that
`/model` exists for is unchanged.

Headless: `snowpea model profiles` prints the merged view with each row tagged `[global]` or
`[project]`; `model assign <agent> [<profileId>]` and `model default [<profileId>]` both take
`--project`, and both clear the setting when the id is omitted — which is what the `null` sentinel
bought.

The TUI side (model picker, HUD) landed separately in `77e8b4c`.

## Known limitations

- **Auxiliary LLM calls have no profile of their own.** Compaction, the title/summary calls,
  `/ralph`, `/ultrawork` and the team planner all use `core.providers.get(session.provider,
  session.model)` — they inherit the caller's session route. Giving them their own rung is a
  separate change.
- **`agents.models` is validated against profiles, but the agent *name* is not.** The setup
  wizard still takes free text, so a mistyped agent name is accepted into `agents.models` and simply
  never matches. (B-P3-3 in the report; not in this story's scope.)
- **A `.md` `model:` is still unvalidated at parse time.** It is validated at *resolve* time — an
  unknown reference logs a warning and falls through to the next rung (R12 in the earlier pass) —
  but nothing rejects the file when it is written.
- **No `/agent model <agent> <profile>` slash command** (B-P3-2). `snowpea model assign` and
  `settings.set` both do it; the slash command was not added.

## Tests

| Test | Pins |
| --- | --- |
| `test_model_assignment.py::test_every_rung_of_the_precedence_chain` | the whole chain, rung by rung |
| `…::test_project_models_merge_over_global`, `…::test_a_project_models_block_round_trips` | rung 2/4 merge |
| `…::test_a_definition_model_resolves_through_profiles_without_a_default` | B-P1-1 (unit) |
| `…::test_an_unknown_profile_reference_degrades_instead_of_bricking` | B-P1-2 |
| `…::test_settings_set_still_rejects_an_unknown_profile`, `…::test_a_genuinely_broken_settings_file_still_raises` | B-P1-2 limits |
| `…::test_a_default_profile_naming_an_unknown_vendor_does_not_brick_every_session` | B-P1-3 |
| `…::test_the_session_pin_survives_a_restore`, `…::test_clearing_the_pin_goes_back_to_the_configured_routing`, `…::test_pinning_an_unknown_reference_is_refused` | rung 3 |
| `…::test_create_derives_the_definition_model` | B-P2-1 |
| `…::test_null_deletes_a_key_in_a_settings_patch` | the sentinel (unit) |
| `test_subagents.py::test_a_profile_id_in_an_agent_definition_is_not_read_as_a_vendor` | B-P1-1 (live repro) |
| `test_subagents.py::test_a_delegation_model_override_outranks_the_assignment`, `…::test_an_unresolvable_delegation_model_is_refused` | rung 1 |
| `test_subagents.py::test_a_child_inherits_the_parents_pin_when_nothing_else_applies` | rung 3 inheritance |
| `test_team_worktree.py::test_a_worker_anchor_keeps_the_team_and_is_named` | B-P2-2 |
| `test_settings_rpc.py::test_null_in_a_patch_deletes_the_key`, `…::test_deleting_a_profile_that_is_still_referenced_is_refused` | the sentinel over RPC |
| `test_cli_sessions_teams_models.py::test_session_set_model_pins_and_clears` | `session.setModel` |
| `…::test_model_profiles_shows_the_project_merged_view`, `…::test_model_assign_can_clear_a_global_assignment` | the CLI surface |
| `test_models.py::test_model_listing_shows_the_configured_profiles` and three more | `/model` |
