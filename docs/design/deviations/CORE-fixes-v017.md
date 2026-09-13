# Deviations — CORE-fixes-v017 (the regressions and risks the v0.1.7 commit review found)

The 23 commits between `a06a190` and `22df464` landed web login, model profiles, project teams,
prompt queueing and the update channels, and the review of them
(`.omc/research/recent-commits-analysis.md`) came back with two red tests, one lint error and
twelve correctness risks. This is what was done about each, in the order the report lists them.
Recorded per the deviations-log convention (`docs/design/deviations/README.md`).

Nothing here changes the setup wizard, the login flows or any provider client — that work was
landing in a different session at the same time. The single exception is a five-line deletion in
`providers/registry.py` (R9, dead code). The one pre-existing failure this branch does **not** own is
`tests/test_provider_matrix.py::test_device_code_login_polls_until_granted`, which the report
already flagged: the OpenAI device-code endpoint moved and the test's mock still matches the old
path.

## The two failing tests, and the lint error

**`test_command_and_tool_and_provider_listings`** asserted the web-login-capable vendor list was
`["openai", "openrouter"]`. `93be684` added Gemini's `google_adc`, so it is three. The assertion
was updated rather than loosened: the list is short, ordered and worth pinning.

**`test_update_runs_the_command_and_emits_progress`** was not just stale, it was
environment-dependent. `watch_update` calls `installed_cli_version()`, which shells out to whatever
`snowpea` is on the host's PATH, so the test's fake `9.9.9` was overwritten by the real installed
version — a different value on every machine. `watch_update` gained a `version_reader` parameter,
resolved at call time so monkeypatching the module attribute works, and the test now says what the
freshly installed executable reports instead of asking the host.

`ruff check --fix` sorted the import block in `tests/test_setup_models.py` (I001).

## R1 — `oauth_token` was masked by neither allowlist

`providers.<vendor>.oauth_token` became a persisted secret without being added to either masking
set, so **every `settings.get` caller — the TUI settings view, the IDE, a debug dump — received a
live OAuth access token in plaintext.**

The fix is not just the missing key. `server/settings_handlers.py` had its own private copy of the
set and of `mask_secrets`; that duplication is what let the two drift in the first place. It now
imports both from `config/patch.py`, which is the single definition, and that definition was
widened to every token-shaped name in both spellings: `token`, `refresh_token`/`refreshToken`,
`access_token`/`accessToken`, `id_token`/`idToken`, `oauth_token`/`oauthToken`, plus the existing
`api_key`/`apiKey` and `password`.

`test_settings_get_never_returns_a_token_in_clear` writes all six and asserts `settings.set`'s echo
and `settings.get` both mask them — and that the file on disk still holds the real values, because
masking is a wire concern, not a storage one.

## R2 — `settings.json` was written with the process umask

It holds `api_key` and now `oauth_token`, and it was created `0644` on a normal umask while the
daemon auth token (`server/auth.py`) and gateway credentials (`config/credentials.py`) both use
`0600`. `Settings.save()` now matches them: `chmod` on the temp file *before* the rename, so the
secret is never briefly visible at the final path, and again afterwards so an existing
world-readable file from an older install is tightened on the next write. A filesystem without
modes logs at debug rather than failing the save.

Project settings (`<workdir>/.snowpea/settings.json`) were deliberately left alone: they carry
teams, modes and allowlists, never credentials.

## R3 — interrupt did not flush the prompt queue

`session.interrupt` only set the event. `_drain_turns` cleared it after the interrupted turn and
started the next queued prompt, so a user who queued three follow-ups and pressed Stop watched all
three run anyway, with no RPC to stop them.

`agent/loop.py` gained `flush_queued_turns()`, called from `session_interrupt_handler`. Every
dropped prompt emits `turn.dequeued{reason:"dropped"}` **and** `turn.done{interrupted}`: without the
second one, a client awaiting that turn id (the SDK's `prompt()` promise, the headless consumer)
would hang forever on a turn that will never run.

**Where the flush lives is a design call, and it differs from the M1 contract's wording.** The
contract §6 describes `_drain_turns` flushing once the interrupted turn ends. Flushing in the
handler instead is both tighter and kinder: the copy-and-clear happens synchronously, before the
first `await`, so it drops exactly the prompts that were waiting at the instant Stop was pressed and
closes the window in which the drain loop could pop the next one before noticing the flag. Flushing
*after* the turn would additionally discard a prompt the user typed in the second after pressing
Stop — new intent, not part of what they cancelled. `_drain_turns` therefore does not re-flush, and
carries a comment saying why.

## R4 — deleting a saved session orphaned its files

`Store.delete_sessions` removed rows from `messages`, `events` and `sessions` and nothing else, so
`<home>/attachments/<id>/` — where pasted image bytes are written — and `<home>/audio/<id>/`
accumulated with no cleanup path at all. That is a disk leak and a privacy surprise: deleting a
thread because of what you pasted into it should take the paste with it.

`session_delete_saved_handler` now purges both directories for each id it deleted. Best effort: the
rows are already gone, so a file that will not delete is logged, not raised.

## R5 / GAP-5 / GAP-6 — queued prompts were invisible on every surface

Nothing was emitted when a prompt was queued, so no surface could show "1 prompt queued" and the
user got no confirmation their second prompt had been accepted rather than swallowed.

Two additive session events, carried by **`PROTOCOL_VERSION` 1.4.0** (the 23 commits had already
changed `protocol.py` without a bump):

| kind | payload |
| --- | --- |
| `turn.queued` | `{turnId, position, queued}` — `position` is 1-based behind the running turn |
| `turn.dequeued` | `{turnId, reason: "started" \| "dropped", queued}` |

`sdk/src/protocol.ts` and `docs/protocol.md` are regenerated from it. The same pass fixed the
`AgentInfo.kind` docstring, which described `definition`/`subagent`/`named` and omitted the `team`
value core has been emitting since `agent_handlers.py` started prepending the active project team —
a typed consumer had no way to know it existed.

`tui/src/state/store.ts` needed no change: its `applySessionEvent` switch already falls through to
`default` for unknown kinds, and the TUI keeps no local prompt queue that could go stale. A visible
queue indicator was left out — it needs new store state plus layout work in files another lane is
editing, and the report scopes it as a TUI item, not a core one.

**Still open, deliberately:** `Session.queued_turns` remains in-memory. A daemon restart mid-drain
still discards pending prompts without a `turn.done`. Persisting the queue is a larger change than
this pass, and is recorded as a known limitation in the M1 contract §5.

## R6 — `deleted` was the request size, not a measurement

`Store.delete_sessions` computed a row count and then returned `len(ids)`. It now returns the
`sessions` `DELETE` cursor's `rowcount`, so asking to delete an id that is not stored reports `0`.
(The old count also summed three tables' `total_changes`, which would have been wrong in a
different way.)

## R7 — a 7-character SHA prefix could skip the ancestry check

`_same_revision` matched by prefix, and `_SHA_RE` accepts 7 hex chars — GitHub's own default
abbreviation. A false match short-circuits the `compare` call entirely, so a genuinely newer commit
sharing a prefix would be reported as "already on this" and never compared.

Only a full 40-character match now counts. Being unsure is cheap: the caller just runs the
comparison, which answers `identical` for a revision that really is the same.

## R8 — a positive update answer was cached for 24h without re-verifying ancestry

**This is the design call in this pass, and it went the safer way.** A *negative* answer is safe to
cache — nothing is installed from "no update". A positive one is an install offer, and a force-push
inside the cache window would keep it offering a commit whose ancestry was never re-checked. So
`_check_git_branch` now serves only negative answers from the cache and re-verifies positive ones.

The cost is one extra GitHub request per check while an update is pending; the daily background
check is the only unforced caller, and explicit `/update` already passed `force=True`.
`test_git_install_detects_new_commit_without_a_version_bump` asserted the old caching and was
updated to assert the new semantics; `test_a_negative_git_answer_is_cached` pins the half that still
caches.

## R9 — `ProviderRegistry.agent_profile()` was dead code

Never called, and a second implementation of the precedence rule (one that ignored
`definition_model`, so it would have disagreed with `route_for` the moment anyone wired it up).
Deleted.

## R10 — two implementations of model-profile precedence

Kept, and documented. The legacy branch in `agent/subagent.py` is load-bearing for installs with no
multi-model settings at all: there a definition's bare `model:` has to be split against the
*parent's* provider so a child inherits the parent vendor, which `route_for` cannot do because it
never sees the parent. When any routing settings exist the branch is skipped and `route_for` decides.
A comment at the branch now says so, since a reader of `config/model_routing.py` alone could not
have known.

## R11 — team membership was not schema-validated

`models.default` and `agents.models` are rejected at load time when they name something unknown;
`agents.teams` was validated only at `/team create`, so a hand-edited settings file loaded fine and
failed much later at delegation.

`normalise_teams` (in `config/project.py`, shared by `AgentsSettings` and `ProjectAgentsSettings`)
now validates the *shape* at load: a non-empty team name, a list, well-formed non-empty agent names,
de-duplicated and stripped.

It deliberately does **not** check that an agent exists. Definitions are discovered per workdir at
runtime and include plugin-provided ones, so existence is not knowable at settings-load time; an
unknown but well-formed name stays a delegation-time refusal. The asymmetry with model profiles is
real and intentional.

One behavioural consequence: `ProjectSettings.load` now catches `ValidationError` and returns
defaults with a warning. It is read on every `session.create`, and a hand-edited project file must
degrade to "inherit everything", not break the session. Global `Settings.load` still raises, which
is what `settings.set` turns into `invalid_params`.

## R12 — a typo'd `model:` silently picked a phantom provider

`resolve_reference` treated any string without a `:` as a bare vendor name, so an agent definition
saying `model: anthropc` routed to a provider that does not exist with `model=None`. A bare name is
now accepted only when it is a real vendor in `providers/presets.PRESETS`; anything else logs a
warning and falls through to the next candidate — the configured default, or no route at all.

## GAP-14..17 — the headless CLI could not reach four things the TUI could

All four are parity gaps, not new features: the state already existed and only the interactive
surfaces could touch it.

**`snowpea session list|delete|clear` (GAP-14).** Saved sessions accumulated in `state.db` with no
shell-side way to inspect or purge them. `list [--include-closed] [--workdir DIR]` prints id, mode,
creation time, workdir and last prompt; `delete <id>` and `clear [--all] [--workdir DIR]` call
`session.deleteSaved`, which (see R4) now takes the files with it.

**`snowpea -c "…" --resume <sessionId>` (GAP-15).** The headless path always created a fresh
session, so there was no equivalent of the TUI's `/resume`. It now calls `session.resume` and
prompts into that session, discarding the replayed events — a headless run renders *this* turn, not
the history it continues. `--mode`/`--provider` are refused politely (warned, ignored) because the
saved session keeps its own, and `--resume` without `-c` is a usage error rather than a silently
dropped flag on the TUI path.

**`snowpea team create|use|list|delete` (GAP-16).** Scoped to **project** teams, which is what the
gap asked for: they write `<workdir>/.snowpea/settings.json` directly — the same file `/team create`
writes and the daemon merges over global teams — so no daemon round trip is needed to change them.
`team list` still reads global teams over RPC and shows the merged view with the active team marked,
matching `team_config.teams_for`.

Two consequences worth stating. Agent names are validated against built-in plus on-disk definitions
loaded locally, so a **plugin-provided** agent is not visible to `snowpea team create` and is
refused with that caveat in the message; `/team create` inside a session still sees it. And `team
delete` is project-only: a global team cannot be removed through `settings.set`, because the patch
is a deep merge and a merge cannot express a deletion. Editing `$SNOWPEA_HOME/settings.json` is the
documented route.

**`snowpea model profiles|default|assign` (GAP-17).** `models.profiles`, `models.default` and
`agents.models` were reachable only through the interactive wizard. `assign` goes through
`settings.set`, so the existing settings validator is the gate on an unknown profile id — the CLI
just reports its refusal. For the same merge-cannot-delete reason there is no `assign --clear`;
clearing an assignment means editing the file.

Docs: `docs/manual/{en,ko}/headless.md` gained "Continuing a saved session", "Saved sessions" and
"Teams and model profiles" sections plus the `--resume` row; `docs/manual/{en,ko}/commands.md`
gained "Sessions" and "Model profiles" subsections and the team roster commands.
`scripts/check_docs_cli.py` passes, which means every one of those invocations was parsed against
the CLI's own `--help`.

## Tests

| Test | Pins |
| --- | --- |
| `test_settings_rpc.py::test_settings_get_never_returns_a_token_in_clear` | R1 |
| `test_settings_rpc.py::test_settings_json_is_written_owner_only` | R2 |
| `test_settings_rpc.py::test_saving_tightens_an_already_world_readable_settings_file` | R2, older installs |
| `test_session_loop.py::test_interrupt_flushes_the_prompt_queue` | R3, R5 |
| `test_session_loop.py::test_prompts_submitted_during_a_turn_are_delivered_fifo` | R5 event pair |
| `test_session_loop.py::test_deleting_a_saved_session_removes_its_files` | R4 |
| `test_session_loop.py::test_deleting_an_unknown_session_reports_zero` | R6 |
| `test_update_git.py::test_a_short_sha_prefix_is_not_treated_as_the_same_revision` | R7 |
| `test_update_git.py::test_a_negative_git_answer_is_cached` | R8 |
| `test_team_config.py::test_malformed_teams_are_rejected_at_load` | R11 |
| `test_team_config.py::test_an_invalid_project_settings_file_degrades_to_defaults` | R11 |
| `test_model_routing.py::test_a_bare_vendor_name_must_be_a_real_vendor` | R12 |
| `test_cli_sessions_teams_models.py` (11 cases) | GAP-14..17 |

The two POSIX-mode tests skip on Windows.
