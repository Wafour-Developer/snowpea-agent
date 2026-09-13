# Recent commits analysis — `a06a190..v0.1.7` (23 commits) + gaps

Analyst: recent-commits-analyst · Date: 2026-09-13
Repo: `/mnt/data/work/mediagen/snowpea` · Range analysed: `a06a190..22df464` (tag **v0.1.7**)

> **Tree state caveat (read this first).** While this analysis ran, another session
> kept working. At the time of writing, `git HEAD` had already advanced to **`8d3ca31`**
> ("docs: OpenAI device-code login authenticates a ChatGPT/Codex subscription") and the
> working tree was **dirty**: `core/snowpea_core/providers/auth_web.py`,
> `core/snowpea_core/setup/wizard.py`, `docs/design/deviations/CORE-multimodal.md`,
> `docs/manual/{en,ko}/voice.md`, `tests/test_login_web.py`,
> `tests/test_setup_wizard.py`, `tui/dist/*`.
> All verification numbers in §2 were therefore re-run on a **clean detached worktree at
> `22df464` (v0.1.7)** so that every failure is attributable to the 23 commits rather than
> to in-flight WIP. Where a result came from the dirty tree, it says so.

## Sections
1. [Per-commit table](#1-per-commit-table)
2. [Verification](#2-verification)
3. [Contract deltas](#3-contract-deltas)
4. [Cross-surface gap matrix](#4-cross-surface-gap-matrix)
5. [Missing deviations docs](#5-missing-deviations-docs)
6. **[Setup: provider/model configuration and the OAuth choices](#6-setup-providermodel-configuration-and-the-oauth-choices)** — priority deep dive (A)
7. **[Per-agent model assignment](#7-per-agent-model-assignment)** — priority deep dive (B)

---

## 1. Per-commit table

Legend for the manual column: `en/ko` means only those two of the five language trees were
updated. **No commit in this range touched `ja`, `es` or `zh-CN`.** **No commit in this range
edited a `docs/design/m*-contract.md` file.**

| # | sha | What changed (files, behaviour) | Protocol / settings / CLI surface | Tests | Docs | Contract affected |
|---|---|---|---|---|---|---|
| 1 | `00c9d36` | `feat(agents)`: `builtin_agent_definitions()` (`core/snowpea_core/agent/definition.py:289`) globs `prompts/roles/*.md` and synthesises `AgentDefinition`s with `source="builtin"`, `model="inherit"`. `commands/agent_cmd.py:93-105` seeds the merge dict with builtins first so project/global files override by name. `tools/delegate.py:25-31` appends the built-in names to the `delegate_task` description + arg schema. Roles: architect, critic, executor, explorer, test-engineer, verifier. | No new RPC. `agent.list` rows gain `source:"builtin"`. `delegate_task` tool description/schema text changes (LLM-facing). | ✅ `tests/test_generators.py` (description names builtins; project def overrides builtin), `tests/test_subagents.py` (built-in name resolves, applies role prompt, tools unrestricted) | ❌ in-commit; picked up next commit in `en/ko tui.md` | **m6-m7** |
| 2 | `f55d57f` | `fix(tui)`: two fixes. (a) Resume rewrite — `tui/src/app.tsx` `sessionId` prop → `initialSessionId` + local state; `resumeSession(id,"main")` resets refs and dispatches new `session/reset` action (`tui/src/state/store.ts:204`); `/resume <sessionId>` parse added. (b) Unicode Markdown tables — new `tui/src/layout/text-width.ts` (grapheme cell widths via `Intl.Segmenter`, CJK/emoji = 2 cells); `layout/transcript.ts` gains `tableCells()`/`tableAt()` (box-drawing tables, stacked fallback when narrow, code-fence aware) and grapheme-safe `wrapLine()`; `MessageStream.tsx` gutted to delegate to `transcript.ts`. | TUI-only. New slash arg form `/resume <sessionId>`. | ✅ `tui/test/resume.test.tsx` (125 ln), `tui/test/markdown.test.tsx` (77 ln) | ✅ en/ko `tui.md` ("Markdown tables", "Built-in and custom agents") | **m1** (resume) + m6-m7 (doc line) |
| 3 | `28ff1ae` | `fix(update)`: git-install tracking + downgrade guard. `update.py:141-175` `git_install_provenance()` reads `install.json` + PEP 610 `direct_url.json`; `_check_git_branch()` (`update.py:353-413`) requires GitHub `compare` status `== "ahead"` for `available`. `update_handlers.py:47-59` strips internal keys (`installKey`, `trackingSource`, `configured`) from the RPC result; `update_handler` refuses to start when `error` or `not available`. TUI disables input while updating; `restartDaemon()` failure now surfaces as `phase="failed"`. | `system.checkUpdate` result loses internal keys; `system.update` gains refusal semantics. | ✅ `tests/test_update_git.py` (118 ln), `tests/test_update_safety.py` (28 ln), `tui/test/{rpc-client,update-ui,update}.test.*` | ✅ en/ko `tui.md` ("Update notifications at startup" + git-reinstall recovery) | **m8** |
| 4 | `a108071` | `fix(tui)`: help panel. Raw `stdin.on("data")` F1 handler (`tui/src/app.tsx:520-532`) because "Ink 5 removes F1 from `useInput`"; `HelpPanel.tsx` rewritten to a height-bounded, scrollable panel (`width`/`height`/`isActive` props, `offset` state, PgUp/PgDn/arrows, `start+1-end/total` footer, reuses `wrapLine()`). Closes on Esc/Enter/`q`/F1; suppressed during approvals/update-confirm/running. `Chat.tsx` loses `onToggleHelp`. | TUI-only. | ✅ `tui/test/help-ui.test.tsx` (70 ln) | ✅ en/ko `tui.md` (1 line) | none (TUI — see proposed **m12**) |
| 5 | `75893ed` | `feat(config)`: per-agent model profiles. New `config/model_routing.py` (`route_for`, `resolve_reference`, `ModelRoute`). `config/settings.py` adds `ModelProfile{provider,model}`, `ModelsSettings{default,profiles}`, `AgentsSettings.models: dict[str,str]`, and a `model_validator` rejecting unknown profile refs. `session/manager.py:88-98` routes at `create()`. `agent/subagent.py:487-505` + `agent/named.py:299-308` forward `definition_model`. `providers/registry.py` gains `default_profile()`/`agent_profile()`. Large `setup/state.py`+`wizard.py` rework for multi-profile registration and per-agent assignment. | **New settings keys**: `models.default`, `models.profiles.<id>={provider,model}`, `agents.models.<agent>`. `--vendor/--key/--model` flags now create the default profile. No new RPC. | ✅ `tests/test_model_routing.py` (57 ln), `tests/test_setup_models.py` (53 ln) | ✅ en/ko `setup.md` ("Multiple models and agent assignments") | **m3** (+ m1/m6-m7 edges) |
| 6 | `50af009` | `feat(tui)`: new `tui/src/components/SectionRule.tsx`, three full-width `─` rules inserted in the bottom region (before HUD, mode summary, agent panel); `layout.statusRows += 3`. | TUI-only. | ✅ `tui/test/section-rule.test.tsx` | ✅ en/ko `tui.md` (1 sentence) | none (→ **m12**) |
| 7 | `066422e` | `fix(update)`: `update_handlers.py:60` `system.update` now calls `check_update(force=True)` — an explicit request must not reuse a negative 24h cache. TUI `/update` (`app.tsx:1006-1016`) re-checks first, then branches error → `failed`, available → confirm, else toast `"already up to date (<v>)"` instead of a misleading failure. | `system.update` semantics change (implicit forced re-check). | ✅ `tui/test/update-ui.test.tsx` (+1 case) | ✅ en/ko `tui.md` (1 sentence) | **m8** |
| 8 | `cf78386` | `feat(agents)`: project teams + short delegation. New `agent/team_config.py` (`ActiveTeam`, `teams_for`, `active_team`; project wins on name clash). `config/settings.py:262-266` `DEFAULT_AGENT_TEAM` + `AgentsSettings.teams`/`default_team`, with a `Settings.load()` migration writing a `"default"` team. `config/project.py:40-46` adds `teams`/`activeTeam`. `session/session.py:26-27` `Session.team`/`team_agents`. `agent/agent.py:128-133` injects a team-restriction rule into the system prompt. `commands/team_cmd.py:168-223` `/team create\|use\|list\|delete`. `server/agent_handlers.py:83-107` filters `agent.list` to team members and prepends a synthetic `kind:"team"` row. **Breaking**: `subagent.py:397-399` + `_refuse()` (`:430-443`) now REFUSE an unknown agent name (previously it silently ran with the parent's settings). TUI `$agent task` → direct `agent.spawn`. | **New settings**: `agents.teams`, `agents.default_team` (global); `.snowpea/settings.json` `agents.teams`, `agents.activeTeam` (project). `agent.list` result gains `kind:"team"`. New slash subcommands. **Behaviour break** on unknown agent names. | ✅ `tests/test_subagents.py` (`test_an_unknown_agent_name_is_refused` replaces `..._still_runs`), team tests, `tui/test/direct-delegate.test.tsx` | ✅ en/ko `tui.md`/`commands.md` | **m6-m7** (+ m1 for prompt/session fields) |
| 9 | `6a46da9` | `fix(tui)`: `app.tsx:1436-1459` reorders footer JSX so `StatusHud` renders **after** the mode/summary line. Pure visual reorder. | none | ❌ (dist regenerated only) | ❌ | none (→ **m12**) |
| 10 | `a061106` | `fix(tui)`: `app.tsx:791-802,1452-1455` — `activeTeam` no longer defaults to `"main"`; agent-panel current row is hard-coded `"main"` (the session's own agent); `SectionRule` gains a `label` prop rendering `Team: <name>` only when a team is genuinely active (`SectionRule.tsx:5-21`). Fixes the session being mislabelled with the team name. | none | ✅ `tui/test/direct-delegate.test.tsx`, `tui/test/section-rule.test.tsx` | ❌ | none (→ **m12**) |
| 11 | `b80ed64` | `fix(tui)`: `layout/agents.ts:168-180` — collapsed row now reads "N **more** idle agents" (hidden count, not total) and lists the hidden names in `task`. | none | ✅ `tui/test/agents.test.ts` | ❌ | none (→ **m12**) |
| 12 | `569caf9` | `fix(tui)`: resume made persistent. `agent/loop.py:64-73` snapshots `session.history` into `store.replace_messages()` after **every** turn (best-effort). `session/store.py` adds `session()`, `reopen_session()`, `replace_messages()`. `session/manager.py:126-165` new `SessionManager.restore()` rehydrates workdir/mode/provider/model/team/history/seq from the store. `server/session_handlers.py:186-196` `session_resume_handler` calls `restore()` when the session is not live. TUI: typing on a status/agent row returns focus and inserts the char (`append` prop); `/resume` added to `SURFACE_COMMANDS`; `state/history.ts offerSession()` returns local records when no live session. | No wire-schema change; **`session.resume` semantics change** (now succeeds for persisted-but-not-live sessions). | ✅ `tests/test_session_loop.py::test_resume_restores_a_persisted_session_after_daemon_restart`, `tui/test/{history,panel,slash}.test.*` | ❌ | **m1** |
| 13 | `633db7d` | `feat(tui)`: saved-session picker. `server/protocol.py:296-301` new `SessionListParams{includeClosed=False, workdir}` — `session.list` params change from `Empty`. `session_handlers.py:213-236` merges live + stored (`store.list_sessions(include_closed=True)`), dedupes (live wins), `seq` from `max_seq`, optional workdir filter, sorts by `createdAt` **desc**. `sdk/src/protocol.ts:774-778` mirrors. TUI `openResumePicker()`; bare `/resume` now opens a `ConfirmMenu`. | **`session.list` params changed** (additive/optional → backward compatible). | ✅ `tests/test_session_loop.py` (extended), `tui/test/resume.test.tsx` | ❌ (`docs/protocol.md` only regenerated later, by `0f41a4d`) | **m1** |
| 14 | `ce413b1` | `feat(sessions)`: **new RPC `session.deleteSaved`**. `protocol.py` `SessionDeleteParams{sessionId?,workdir?,all=False}` → `SessionDeleteResult{deleted:int}`; registered in `METHODS`/`IMPLEMENTED_METHODS`; mirrored in `sdk/src/protocol.ts`. `session_handlers.py:253-271` never deletes a live session. `session/store.py:146-165` `delete_sessions()` deletes from `messages`, `events`, `sessions` in one transaction. TUI `/session delete <id>`, `/session clear [--all]`. | **New RPC method** + new slash commands. | ✅ `tests/test_session_loop.py::test_delete_saved_sessions_keeps_the_live_session`, `tui/test/slash.test.ts` | ❌ | **m1** (new surface) |
| 15 | `7b87085` | `fix(tui)`: `protocol.py:277` `SessionSummary.lastPrompt: str\|None`; `session_handlers.py:238-249` enriches every row with the latest stored **user** message text (regardless of `includeClosed`). TUI: Enter on the status row now opens a mode `ConfirmMenu` (`accept\|auto\|plan`) instead of toggling the shell list; resume-picker labels show the truncated `lastPrompt`; Enter in the `/` palette completes the command instead of submitting. | **`SessionSummary.lastPrompt` added** (additive). | ✅ `tests/test_session_loop.py`, `tui/test/{panel,resume}.test.tsx` | ❌ | **m1** |
| 16 | `0b3e09c` | `feat(tui)`: editable draft. `Chat.tsx` gains full cursor support (`cursor` state; ←/→; mid-line insert/backspace) and `historyDraft` so ↓ past the newest entry restores the unsent draft. New `/sessions` alias opening the resume picker (blocked with a toast during an active turn). `store.ts` new `errors/clear`; typing clears command errors. | TUI-only; new client-side `/sessions`. | ✅ `tui/test/chat.test.tsx` (new), `tui/test/{resume,slash}.test.*` | ❌ | none (→ **m12**) |
| 17 | `1d14134` | `feat(search)`: new `ExaMcpProvider` (`tools/search_providers/providers.py:308-361`) calling MCP tools `web_search_exa`/`web_fetch_exa` over `streamable_http_client` at `https://mcp.exa.ai/mcp`. `exa_free` re-tagged **no key** and added to `FREE_CHAIN`; paid `exa` keeps the REST client. Side-fix `setup/wizard.py:145-153`: interactive `--search-provider <keyed-id>` without `--search-key` now still prompts. | `provider=exa_free` no longer needs `search.credentials.exa_free.api_key`. No RPC change. | ✅ `tests/test_search_providers.py`, `tests/test_setup_wizard.py` (3 new), `tests/test_tools_contract.py` | ✅ en/ko `setup.md` **+ `docs/design/deviations/CORE-search-fix.md`** (the only deviation doc touched in the whole range) | **m2** |
| 18 | `fe632c2` | `fix(session)`: prompt queueing. `agent/loop.py:23-31` `QueuedTurn`; `start_turn` (`:104-124`) enqueues onto `session.queued_turns` when a turn task is live, else spawns `_drain_turns` (`:127-146`) which pops FIFO and runs serially. `run_turn`/`_drive` gain an `attachments` param so pre-captured attachments survive the queue. `session/session.py:65` `queued_turns: list[Any]` — **in-memory only, not persisted**. | none (internal). | ✅ `tests/test_session_loop.py::test_prompts_submitted_during_a_turn_are_delivered_fifo` (FIFO order, `peak_active == 1`, queue drains) | ❌ | **m1** (→ also proposed **m9**) |
| 19 | `232a383` | `release`: version bump to v0.1.3 (`__init__.py`, `package.json`s, `tui/dist` regenerated). | version only | ❌ | ❌ | m8 (version only) |
| 20 | `55bc6ff` | `fix(update)`: `update.py:373-395` `_check_git_branch` looks up the latest **tag** to display a release version alongside the branch SHA, wrapped in try/except so tag failure can't break the ancestry result. Version → 0.1.4. | `system.checkUpdate` shows e.g. `0.1.4+<sha>`. | ✅ `tests/test_update_git.py::test_git_update_prompt_uses_the_new_release_version` | ❌ | **m8** — note: superseded two commits later by `93be684`'s exact-revision `_git_version_at()`; a tag is not guaranteed to be an ancestor of `latest`. |
| 21 | `0f41a4d` | `fix(schedule)`: reminders to source session. `scheduler/jobs.py:237-241` new `origin_session_id` column with an online `ALTER TABLE` migration. `scheduler/scheduler.py:334-374` `deliver()` calls new `_deliver_to_session()` first, emitting a `message.done` event prefixed `"⏰ Scheduled reminder ({job.id})"`; an explicit `job.channel` is now **additive**, not a replacement (`:339`). `commands/schedule_cmd.py:113` and `scheduler/tools.py:112` stamp `origin_session_id=ctx.session.id`. | **`JobInfo.originSessionId`** (`protocol.py:837-842`). `docs/protocol.md` regenerated here, which is also where `session.deleteSaved` / `session.list` params / `lastPrompt` first got documented. | ✅ `tests/test_scheduler.py` (migration test + delivery assertions) | ✅ `docs/protocol.md`; ❌ all 5 manuals | **m5** |
| 22 | `93be684` | `feat(auth)`: Gemini OAuth + remote tokens. `providers/auth_web.py:152-223` `google_adc_start`/`google_adc_login` shell `gcloud auth application-default login`; only `{"auth_method":"google_adc"}` is persisted, never Google's refresh token. `providers/gemini_native.py` `_oauth_token()` shells `gcloud ... print-access-token`; `_client()` becomes async and sets `Authorization: Bearer`. `providers/presets.py` gemini `auth_methods=("api_key","google_adc","oauth_token")`. `registry.py:157-159,311-329` — `is_configured` recognises `auth_method`/`oauth_token`; OpenAI also accepts `oauth_token` as a bearer. `server/app_server.py:417-449` `provider.configure` allowlist extended. `cli/commands.py:271-310` new `snowpea provider login <vendor> --token [TOKEN]` (getpass prompt; openai\|gemini only). Also replaces `55bc6ff`'s tag lookup with `_git_version_at()` (reads `__init__.py` at the exact commit via raw.githubusercontent.com) and adds `installed_cli_version()` shelling `snowpea --version`. | **`provider.configure` accepts `oauth_token`, `auth_method`.** `ProviderInfo.authMethods` description updated. **New CLI flag** `--token [TOKEN]`. | ✅ `tests/test_login_web.py`, `tests/test_provider_matrix.py` (2 new), `tests/test_update{,_git}.py` | ✅ `README.md`, en/ko `setup.md`; ❌ `docs/protocol.md` not regenerated for the `provider.configure` surface | **m3** |
| 23 | `22df464` | `feat(setup)`: OAuth choices in the wizard. `setup/state.py` `WizardState.oauth_token`/`auth_method`; `select_vendor` seeds `auth_method` from saved config; `has_saved_key` also checks `oauth_token`; `remember_current_provider` (`:149-159`) writes exactly one of `api_key`/`oauth_token`/`auth_method` and clears the others. `setup/wizard.py:251-281` `_ask_for_key` offers **1 = API key / 2 = browser login / 3 = OAuth token** when a vendor has >1 auth method; browser login runs `auth_web.login()` synchronously; the token is read with `ui.ask_text(..., secret=True)`. | Setup-wizard surface only. | ✅ `tests/test_setup_wizard.py` (3 updated/new) | ❌ (covered by `93be684`'s manual edits) | **m3** |

### Surface-change roll-up (what an SDK/IDE consumer must care about)

| Kind | Change | Commit |
|---|---|---|
| New RPC method | `session.deleteSaved` | `ce413b1` |
| Changed params | `session.list`: `Empty` → `SessionListParams{includeClosed, workdir}` | `633db7d` |
| New result field | `SessionSummary.lastPrompt` | `7b87085` |
| New result field | `JobInfo.originSessionId` | `0f41a4d` |
| New result value | `agent.list` row `kind: "team"` | `cf78386` |
| Changed params | `provider.configure` accepts `oauth_token`, `auth_method` | `93be684` |
| Changed result | `system.checkUpdate` no longer returns `installKey`/`trackingSource`/`configured` | `28ff1ae` |
| Changed semantics | `session.resume` restores persisted (non-live) sessions | `569caf9` |
| Changed semantics | `system.update` refuses unless a fresh check says available | `28ff1ae`, `066422e` |
| **Breaking behaviour** | unknown agent name in `delegate_task`/`agent.spawn` is now refused | `cf78386` |
| New settings keys | `models.default`, `models.profiles.<id>`, `agents.models.<agent>` | `75893ed` |
| New settings keys | `agents.teams`, `agents.default_team`; project `agents.teams`, `agents.activeTeam` | `cf78386` |
| New CLI flag | `snowpea provider login <vendor> --token [TOKEN]` | `93be684` |
| New slash commands | `/team create\|use\|list\|delete`, `/session delete`, `/session clear`, `/sessions`, `/resume <id>`, `$agent task` | `cf78386`, `ce413b1`, `0b3e09c`, `f55d57f` |


---

## 2. Verification

### 2.1 Command results

All rows marked **clean v0.1.7** were run in a detached worktree at `22df464`. Rows marked
*dirty tree* were run in the primary checkout at `8d3ca31` + uncommitted WIP; for those, no
source file relevant to the check was among the dirty files (only `tui/dist/*` build
artifacts and the auth/wizard WIP), so the results still hold for v0.1.7.

| Command | Result | Where |
|---|---|---|
| `SNOWPEA_SKIP_BROWSER_TESTS=1 uv run pytest -q` | ❌ **2 failed, 941 passed, 7 skipped** (188.92s) | clean v0.1.7 |
| `uv run ruff check core tests scripts` | ❌ **1 error** (`I001` unsorted imports, `tests/test_setup_models.py:1`) — auto-fixable | clean v0.1.7 |
| `uv run mypy core` | ✅ `Success: no issues found in 164 source files` | dirty tree |
| `uv run python scripts/gen_protocol.py --check` | ✅ `ok: sdk/src/protocol.ts` / `ok: docs/protocol.md` | dirty tree |
| `uv run python scripts/check_docs_cli.py` | ✅ `ok — 565 snowpea invocation(s) and the links in 49 file(s) all check out` | dirty tree |
| `uv run python scripts/verify_vendor_integrity.py` | ✅ `OK vendor-integrity 6 entries` | dirty tree |
| `npm -w sdk test` | ✅ **6 passing** (base + subagent contract AC-15b) | dirty tree |
| `npm -w tui test` | ✅ **37 files, 380 tests passed** (12.93s) | dirty tree |
| `npx tsc -p tui --noEmit` | ✅ exit 0 | dirty tree |

### 2.2 The two pytest failures — both regressions introduced by `93be684`

Both were **passing at `a06a190`** and fail at clean `22df464`. Neither test file was updated
when `93be684` changed the behaviour underneath it.

**F1 — `tests/test_session_loop.py::test_command_and_tool_and_provider_listings`** (`tests/test_session_loop.py:450`)
```
assert web_login == ["openai", "openrouter"]
E  AssertionError: assert ['openai', 'openrouter', 'gemini'] == ['openai', 'openrouter']
E    Left contains one more item: 'gemini'
```
`93be684` added a `gemini` entry to `ENDPOINTS` in `core/snowpea_core/providers/auth_web.py`
(method `google_adc`), so the set of web-login-capable vendors grew from 2 to 3.
`tests/test_provider_matrix.py` and `tests/test_setup_wizard.py` *were* updated for the new
count; this listing assertion was missed. **Fix:** update the expected list to
`["openai", "openrouter", "gemini"]` (or assert membership rather than exact order).

**F2 — `tests/test_update.py::test_update_runs_the_command_and_emits_progress`** (`tests/test_update.py:442`)
```
assert NEWER in done["message"]
E  AssertionError: assert '9.9.9' in 'updated to v0.1.7'
```
`93be684` added `installed_cli_version()`, which **shells out to the real `snowpea --version`**
after an upgrade and uses that string in the completion message. The test's fake `NEWER = 9.9.9`
is therefore overridden by whatever version is actually installed in the environment.
This is both a stale test **and a genuine test-isolation defect**: a unit test now depends on
the host's installed CLI, so it will report a different value on every machine and in CI.
**Fix:** monkeypatch `installed_cli_version` (or the subprocess call) in the test, and consider
whether `update.py` should prefer the *target* version it just installed over re-shelling.

### 2.3 The `ruff` failure

```
I001 [*] Import block is un-sorted or un-formatted
 --> tests/test_setup_models.py:1:1
```
Introduced by `75893ed` (new test file). `from snowpea_core.setup import wizard` sorts before
`from snowpea_core.setup.state import WizardState`. Auto-fixable with `ruff check --fix`.

> A second ruff error (`E501` at `core/snowpea_core/providers/auth_web.py:203`) appears in the
> primary checkout but **not** at clean v0.1.7 — it belongs to the other session's uncommitted WIP.
> Likewise, 4 failures in `tests/test_login_web.py` / `tests/test_provider_matrix.py`
> (`openai: device authorization failed (HTTP 400)`) appear only in the dirty tree: the WIP
> changes the OpenAI device-code endpoint from `https://auth.openai.com/oauth/device/code` to
> `https://auth.openai.com/api/accounts/deviceauth/usercode`, while the test mock still matches
> `request.url.path.endswith("/device/code")`. **The other session must fix that mock before
> committing** — flagged here because it will otherwise land as a red suite.

### 2.4 Correctness risks in the new code

Ordered by severity.

**R1 (High) — `oauth_token` is not in either secret-masking allowlist.**
`93be684`/`22df464` introduced `providers.<vendor>.oauth_token` as a persisted secret, but
neither masking set includes it:
- `core/snowpea_core/config/patch.py:15-17` — `SECRET_KEYS = frozenset({"api_key","apiKey","token","refresh_token","password"})`
- `core/snowpea_core/server/settings_handlers.py:55` — `_SECRET_KEYS = {"api_key","token","refresh_token","password"}`

These gate the masking applied at `settings_handlers.py:58-70` (used at `:103`, `:104`, `:123`,
`:138`). **Any client calling `settings.get` — the TUI settings view, the IDE, a debug dump —
receives the raw OAuth access token in plaintext instead of `"***"`.**
**Fix:** add `"oauth_token"` (and `"oauthToken"` for the camelCase path) to both sets.

> **AMENDED after the §6 deep dive:** R1 and R2 were both true at the commit analysed, and have
> since been **fixed in the other session's uncommitted working tree** — `config/patch.py:15-28`
> now carries one shared `SECRET_KEYS` including `oauth_token`/`oauthToken`/`access_token`/
> `id_token`, `server/settings_handlers.py:54-61` imports it instead of keeping a second list, and
> `config/settings.py:24-32,315-332` adds `FILE_MODE = 0o600` + `_chmod_quietly()`. Verified:
> `stat -c '%a'` on a freshly written `settings.json` returns `600`. **Both fixes are still only in
> a dirty tree — make sure that commit lands.** See §6.3 for the proof, including the raw leak
> reproduced against `HEAD`'s own module.

**R2 (High) — `settings.json` has no file-permission hardening, yet now holds OAuth tokens.**
`Settings.save()` (`core/snowpea_core/config/settings.py:299-303`) does
`tmp.write_text(...); tmp.replace(target)` with **no `os.chmod`**. Contrast with the two places
that do get it right: the daemon auth token (`core/snowpea_core/server/auth.py:16,43,45`,
`TOKEN_MODE = 0o600`) and gateway credentials (`core/snowpea_core/config/credentials.py:28,62,64`,
`FILE_MODE = 0o600`). `settings.json` is created with the process umask — commonly `0644` — and
now contains `api_key` **and** `oauth_token` values. **Fix:** chmod `0o600` on write, matching
`credentials.py`.

**R3 (High) — interrupt does not flush the prompt queue.**
`session_interrupt_handler` (`core/snowpea_core/server/session_handlers.py:344-348`) only calls
`session.interrupt.set()`; it never touches `session.queued_turns`. `_drain_turns`
(`core/snowpea_core/agent/loop.py:132`) then calls `session.interrupt.clear()` after the
interrupted turn and **proceeds to the next queued turn**. A user who queues three follow-ups and
then hits Stop sees the queue drain anyway. There is no RPC or parameter to clear the queue.
**Fix:** clear `queued_turns` in the interrupt handler (or add an `includeQueued` param), and
emit an event so the client can show what was dropped.

**R4 (Medium) — deleting a saved session orphans its attachments.**
`Store.delete_sessions()` (`core/snowpea_core/session/store.py:146-165`) deletes rows from
`messages`, `events` and `sessions`, but nothing removes
`<SNOWPEA_HOME>/attachments/<session_id>/`, where inline attachment bytes are written
(`core/snowpea_core/server/session_handlers.py:311-313`). Files accumulate indefinitely with no
cleanup path. The only test,
`tests/test_session_loop.py::test_delete_saved_sessions_keeps_the_live_session`, asserts only
`store.session(saved_id) is None` and never exercises attachments — so the gap is untested as
well as unfixed. This is also a **privacy** issue: a user who deletes a session reasonably
expects the images they pasted into it to go too.
**Fix:** delete the attachment directory in `session_delete_saved_handler` and add a test.

**R5 (Medium) — queued prompts are invisible on every surface and lost on restart.**
`Session.queued_turns` (`core/snowpea_core/session/session.py:65`) is in-memory only, so a daemon
restart mid-drain silently discards pending prompts. No event is emitted at enqueue time
(`agent/loop.py:105-125` just appends and returns), so neither the TUI, the IDE nor the SDK can
show "1 prompt queued". The user gets no confirmation that their second prompt was accepted.
**Fix:** emit a `turn.queued` / `turn.dequeued` event pair, add it to `EventPayloadMap`, and
render a queue indicator.

**R6 (Medium) — `deleted` count can overcount.**
`session_delete_saved_handler` (`core/snowpea_core/server/session_handlers.py:253-271`) returns
`deleted=len(ids)` — the number of ids it *asked* to delete — not the SQLite rows-affected count
that `Store.delete_sessions` computes internally and discards. In practice ids come from
`list_sessions` so they exist, but the reported number is not a measurement.

**R7 (Medium) — short-SHA prefix matching can skip the ancestry check.**
`_same_revision()` (`core/snowpea_core/update.py:96-101`) does `a.startswith(b) or b.startswith(a)`
and `_SHA_RE` (`:74`) accepts 7–40 hex chars. A 7-char short SHA — GitHub's own default
abbreviation — has a small but real collision chance. A false match short-circuits the
`compare` call entirely (`if not _same_revision(...)` guards it), so a genuinely newer commit
sharing a 7-char prefix would be silently reported as "already on this" rather than compared.
**Fix:** require a full 40-char SHA on both sides before treating them as the same revision, or
compare only when both are the same length.

**R8 (Low) — a positive git update answer is cached for 24h without re-verifying ancestry.**
`_check_git_branch` (`core/snowpea_core/update.py:365-366`) returns a cached positive answer
without re-running `compare`. A force-push that rewrites `main` inside the cache window would
not be re-verified. Mitigated by `066422e`, which makes explicit `/update` pass `force=True`.

**R9 (Low) — `ProviderRegistry.agent_profile()` is dead code.**
Defined at `core/snowpea_core/providers/registry.py:134-137`, mirroring the `route_for`
precedence, but never called anywhere. Either wire it to the display path it was written for or
delete it — as it stands it is a second implementation of the precedence rule that can drift.

**R10 (Low) — two implementations of model-profile precedence.**
`core/snowpea_core/agent/subagent.py:487-505` computes
`has_model_routing = bool(settings.models.default or assigned)` and, when false, resolves
`definition_model` itself via the legacy `_split_model()` instead of deferring to `route_for`.
The legacy path is deliberate (backward compatibility for installs with no multi-model settings)
and is tested, but a reader of `config/model_routing.py` alone would not know it exists.

**R11 (Low) — team membership is not schema-validated, unlike model profiles.**
`Settings._validate_model_profile_refs` rejects an unknown profile id at load time, but a bogus
agent name inside `agents.teams` is validated **only** at `/team create` time
(`core/snowpea_core/commands/team_cmd.py`). A hand-edited `.snowpea/settings.json` loads fine and
fails later at delegation (`subagent.py` `_refuse`). Asymmetric; worth a matching validator.

**R12 (Low) — a typo'd `model:` in a custom agent `.md` degrades silently.**
`resolve_reference()` (`core/snowpea_core/config/model_routing.py:59`) falls back to
`ModelRoute(text, None)` for any string without a `:` — i.e. "treat it as a bare vendor name".
Values persisted in `agents.models` are caught by the settings validator, but a `definition_model`
read from a hand-written agent definition is not validated at all, so a typo silently selects a
different provider with `model=None`.

### 2.5 Answers to the specific questions asked

- **Prompt queue ordering / interrupt / resume / headless `-c`.** Ordering is strict FIFO
  (`agent/loop.py:127-146`); `peak_active == 1` is asserted by the test. Interrupt does **not**
  flush the queue (**R3**). Queued turns are **not** replayed on resume — they are in-memory only
  (**R5**). Headless is unaffected: `run_headless` (`core/snowpea_core/cli/main.py:261-330`)
  creates a fresh session, issues exactly one `session.prompt` and exits on `turn.done`; there is
  **no `--continue`/`-c` flag in the CLI at all**, so no second prompt can arrive mid-turn.
- **Remote tokens / OAuth storage.** Tokens live in `Settings.providers` in `settings.json`.
  Masking gap = **R1**; permission gap = **R2**. No log-line leaks were found in the auth or
  provider paths; the exposure is the `settings.get` RPC and the on-disk mode. On the positive
  side, `google_adc` deliberately persists only `{"auth_method": "google_adc"}` and never Google's
  refresh token — Google's CLI owns that (`providers/auth_web.py`, and asserted by
  `tests/test_login_web.py::test_gemini_google_adc_login_persists_only_auth_method`).
- **Downgrade guard.** For **git installs**: `_check_git_branch` (`update.py:353-413`) requires
  GitHub `compare/{revision}...{latest}` `status == "ahead"`; `"behind"`, `"diverged"` and any
  network/HTTP failure become `available=False` or a hard error, never an install. An install
  pinned to a tag returns `None` from `git_install_provenance()` and is never auto-changed to
  `main`. For **PyPI/tag installs** the guard is only the strict semver `>` in `is_newer()`
  (`update.py:191-196`) — there is no dedicated check. `update_handlers.py:29-38` is a second
  gate that refuses to start when `not available` or `error`. The escape hatch is manual:
  `uv tool install --force --reinstall <req>` (`update.py:551-559`), documented for recovering a
  daemon too broken to run the checker; it bypasses the guard by design. Risks: **R7**, **R8**.
- **Scheduler delivery when the source session is closed.** `_deliver_to_session`
  (`scheduler/scheduler.py:343-374`) tries `core.sessions.get()`, then falls back to
  `await core.sessions.restore(session_id)` (`:349-350`); if restored only for delivery, it closes
  the session again afterwards (`:361-364`). If the session is truly gone (deleted via
  `session.deleteSaved`, or never existed) it logs
  `log.warning("job %s refers to missing session %s")` (`:352-353`) and returns `False`, and
  `deliver()` falls through to `_append_log(...)` (`:339`) writing to `core.paths.jobs_log`.
  So: **not dropped, not queued for replay, not an error** — it degrades to the jobs log.
  Note the interaction with **R4**: deleting a saved session leaves its scheduled jobs pointing at
  a `origin_session_id` that no longer resolves.
- **Session delete — what is removed from disk.** Rows in the `messages`, `events` and `sessions`
  tables of `$SNOWPEA_HOME/state.db`, in one transaction
  (`core/snowpea_core/session/store.py:146-165`). Live sessions are never deleted
  (`session_handlers.py:253-271`). **Attachments are left behind** — see **R4**.
- **Model-profile precedence.** `route_for()` (`core/snowpea_core/config/model_routing.py:18-45`):
  `explicit provider/model args` → `settings.agents.models[agent]` → `definition_model` (the agent
  `.md` `model:` field) → `settings.models.default`. Named-agent sessions
  (`agent/named.py:299-308`) never pass explicit args, so for them it is
  `agents.models[name] > definition.model > models.default`. Ordinary sessions pass neither
  `agent` nor `definition_model`, so only `models.default` applies. Caveats **R9**, **R10**, **R12**.
- **Project-teams config format.** Project: `<workdir>/.snowpea/settings.json`, key `agents`
  (`core/snowpea_core/config/project.py:14-15,40-46` — `ProjectAgentsSettings.teams: dict[str, list[str]]`,
  `activeTeam: str | None`):
  ```json
  { "agents": { "teams": { "delivery": ["architect", "executor", "verifier"] }, "activeTeam": "delivery" } }
  ```
  Global: `$SNOWPEA_HOME/settings.json`, `AgentsSettings.teams` + `default_team`:
  ```json
  { "agents": { "teams": { "default": ["architect","critic","executor","explorer","test-engineer","verifier"] }, "default_team": "default" } }
  ```
  Merge: `teams_for()` (`core/snowpea_core/agent/team_config.py:16-23`) starts from global and
  `.update()`s with project, so **project wins on a name clash**; `active_team()` prefers project
  `activeTeam` over global `default_team`.

---

## 3. Contract deltas

> Drafted in the contracts' own house style (the `m*-contract.md` files are written in Korean
> with normative statements and `AC-` identifiers; the drafts below match that). Every block is
> ready to paste, with an explicit instruction for where it goes. Sub-section (a) covers the six
> existing contracts, (b) gives full drafts for four **new** contract files covering the
> iteration-2 stories that only ever existed as deviations, and (c) lists stale statements to fix.

All facts verified against HEAD. Here is the complete deliverable.

---

### 3a. Deltas to the six existing contracts

## `docs/design/m1-core-contract.md`

### A1 — REPLACE the `session.list` row in §1's method table (currently line 34) and INSERT a new row after it

```markdown
| session.list | includeClosed?: bool=false, workdir?: str | sessions: list[SessionSummary] |
| session.deleteSaved | sessionId?, workdir?, all?: bool=false | deleted: int |
```

### A2 — APPEND to §1, immediately after the method table (before the "서버→클라이언트 요청" line)

```markdown
`PROTOCOL_VERSION`은 현재 `1.3.0`이다 (`server/protocol.py:23`). §1 코드 블록의 `"0.1.0"`은 M1 시점의 값이며, M8의 `1.0.0` 계획은 폐기되었다 — 프로토콜은 추가 변경마다 minor를 올려 왔고(1.1.0 update → 1.2.0 context/models/login → 1.3.0 `config` 권한 태그), v1.0 freeze gate는 v0.2 IDE 이전에 별도로 잡는다.

`SessionSummary`는 계약 이후 세 필드가 추가되었다(모두 가산적): `contextUsed`, `contextWindow` (CORE-context), `lastPrompt: str|None` — 그 세션에 마지막으로 저장된 **user** 메시지의 텍스트 (`server/protocol.py:266-277`). `session.list` 결과는 `createdAt` **내림차순**으로 정렬된다 (`server/session_handlers.py:249`).
```

### A3 — REPLACE §7's permission matrix table (currently lines 131-136)

```markdown
```python
class PermissionPolicy:
    def decide(self, mode: Mode, tag: PermissionTag, tool: Tool, args: dict, session) -> Literal["allow","deny","ask"]
```
| mode \ tag | read | write | exec | network | send | config |
|---|---|---|---|---|---|---|
| plan | allow | deny | deny | allow | deny | deny |
| accept | allow | allow | ask | ask | ask | ask |
| auto | allow | allow | allow | allow | allow | **ask** |

`config`는 CORE-search-fix가 더한 여섯 번째 태그다(`PROTOCOL_VERSION` 1.3.0). auto 모드에서도 `ask`인 것이 의도된 부분이다: 감시자가 없는 에이전트가 `settings.json`을 고쳐 쓰는 것이 고치려던 실패이므로 "아무도 안 본다"는 건너뛸 이유가 아니라 물어볼 이유다. 태그는 호출마다 `Tool.permission_for` → `tools/config_guard.py`가 해석한다(경로가 `$SNOWPEA_HOME` 아래거나 프로젝트의 `.snowpea/settings.json`·`.snowpea/credentials.json`이면 `config`, 아니면 `write`). allowlist(M4)는 `ask`→`allow`로 **승격만** 하되 `UNPROMOTABLE` 태그(=`config`)는 건너뛰며, 승인 캐시도 `cacheable=False`로 비활성이다.
```

### A4 — REPLACE §8's agent loop block (currently lines 145-153)

```markdown
```
turn = prompt → messages(history+system) → provider.stream
  text_delta → session.event message.delta
  tool_call  → policy.decide → deny: event error{mode_denied} + 거부 사유를 실패한 tool.result로 append → 루프 계속
                             → ask: approvals.request → deny: 같은 처리
                             → allow: event tool.call → tool.run → event tool.result (+diff) → 메시지에 tool 결과 추가 → 다시 provider.stream
  done(no tool calls) → message.done → (auto-speak 시 audio.spoken) → context → turn.done{complete}
```
최대 반복 `agent.max_tool_rounds`(기본 50). **거부 한 번이 턴을 끝내지 않는다**: 거부는 tool 결과로 모델에 돌아가 모델이 적응할 수 있고, 한 턴에 `MAX_DENIALS_PER_TURN = 3`(`agent/loop.py:46`) 번째 거부에서야 `turn.done{denied}`로 끝난다(`agent/loop.py:330`). `mode_denied`/`approval_denied` error 이벤트는 그대로이므로 어떤 surface도 새로 배울 것이 없다.

모든 턴 종료는 `agent/loop.py:63-91`의 `finish_turn()` 하나를 거친다. 순서는 **history 영속화 → `context` 이벤트 → `turn.done`** 이며, `Core.stopping` 중에는 앞의 둘을 건너뛴다(CORE-session-race). `turn.done`은 여전히 terminal이다.

`session.interrupt`는 진행 중인 턴만 취소한다 → `turn.done{interrupted}`.
```

### A5 — APPEND as a new section at the end of the file (last existing section is `## 16`)

```markdown
## 17. Post-v0.1.1 amendments (session listing, deletion, restore, prompt queueing, teams)

계약 §1·§4를 HEAD(v0.1.7) 기준으로 맞추는 절. 여기 적힌 것이 구현이다.

1. **`session.list`는 파라미터를 받는다.** `SessionListParams{includeClosed: bool=false, workdir: str|None}` (`server/protocol.py:300-302`). `includeClosed=false`(기본)는 살아 있는 세션만, `true`는 `store.list_sessions(include_closed=True)`의 영속 행을 live 행 뒤에 병합한다(같은 id는 live가 이긴다). `workdir`이 주어지면 그 디렉터리에 뿌리내린 행만 남긴다. `EmptyParams`를 보내던 구버전 클라이언트는 두 필드가 모두 기본값이라 영향이 없다. (`server/session_handlers.py:213-249`)

2. **`SessionSummary.lastPrompt`.** store가 있으면 모든 행에 대해 `store.messages(sessionId)`를 뒤에서부터 훑어 마지막 `role == "user"` 메시지의 텍스트를 채운다(`session_handlers.py:238-248`). store가 없으면 `None`이다. 정렬은 `createdAt` 내림차순(`:249`).

3. **`session.deleteSaved` (신규 RPC).** `SessionDeleteParams{sessionId?, workdir?, all: bool=false}` → `SessionDeleteResult{deleted: int}` (`protocol.py:305-312`, 등록 `protocol.py:1560`·`1818`). 핸들러는 먼저 `core.sessions.list()`의 live id 집합을 빼므로 **살아 있는 세션은 절대 지우지 않는다**(`session_handlers.py:257-269`). 셋 중 아무것도 주지 않으면 아무것도 지우지 않는다. `Store.delete_sessions`는 `messages`·`events`·`sessions` 세 테이블의 행을 한 트랜잭션으로 지운다(`session/store.py:146-165`).

   **알려진 한계.** `<SNOWPEA_HOME>/attachments/<session_id>/` 아래의 첨부 파일은 지워지지 않고 고아로 남는다(`session_handlers.py:318`이 쓰는 경로). 또한 `delete_sessions`는 실제 삭제된 행 수가 아니라 `len(ids)`를 돌려주므로, 그 사이에 사라진 행이 있어도 요청한 id 수가 보고된다(`store.py:165`).

3. **`session.resume`의 의미가 넓어졌다.** 계약 §4는 살아 있는 세션의 이벤트 재전송만 정했다. 이제 데몬을 재시작해 메모리에서 사라진 세션도 `SessionManager.restore()`가 store에서 되살린다(`session/manager.py:126-165`): `sessions` 행에서 workdir·mode·provider·model을 읽고, `store.messages()`로 `History`를 재구성하고, workdir의 활성 팀을 다시 계산하고, `store.reopen_session()`으로 `closed_at`을 지운다. 이것이 가능한 이유는 `finish_turn()`이 매 턴 `store.replace_messages()`로 히스토리를 증분 영속화하기 때문이다(`agent/loop.py:64-73`). 원본 연결 승계(§16-12)는 그대로다.

4. **프롬프트 큐잉.** 턴이 도는 중에 온 `session.prompt`는 거부되지도, 동시에 실행되지도 않는다. `start_turn`은 항상 `QueuedTurn{turn_id, text, unattended, attachments}`를 만들고, `session.turn_task`가 아직 살아 있으면 `Session.queued_turns`(`session/session.py:65`, **메모리 전용**)에 넣고 `turnId`만 돌려준다(`agent/loop.py:103-124`). 하나의 `_drain_turns` 태스크가 FIFO로 소비한다(`agent/loop.py:127-146`). 첨부는 큐에 넣는 시점에 동기적으로 `pending.take()` 되므로 뒤 프롬프트의 이미지가 앞 턴으로 새지 않는다.

   **알려진 한계 — 두 가지, 둘 다 의도적으로 미해결:**
   - `session.interrupt`는 **진행 중인 턴만** 취소한다. `session_interrupt_handler`는 `session.interrupt.set()`만 하고 `queued_turns`를 비우지 않으므로(`server/session_handlers.py:344-349`), 인터럽트 뒤에도 큐는 계속 배수된다. "전부 취소"를 원하는 surface는 현재 방법이 없다.
   - `queued_turns`는 영속화되지 않는다. 데몬이 재시작되면 대기 중이던 프롬프트는 사라지고, 클라이언트는 이미 받은 `turnId`에 대한 `turn.done`을 영영 받지 못한다.

5. **`Session.team` / `Session.team_agents`.** `session.create`와 `SessionManager.restore`가 `agent/team_config.active_team(settings, workdir)`으로 채운다. 둘이 채워져 있고 세션이 subagent가 아니면 시스템 프롬프트에 팀 제한 규칙이 덧붙는다(`agent/agent.py:128-133`):

   > `Active delegation team: <name>. Delegate only to these agents: <a, b, c>. Every delegate_task call must include one of those names in its agent field.`

   규칙은 조언이 아니라 강제다 — 실제 거부는 M6/M7 계약 §3의 갱신본을 볼 것.
```

---

## `docs/design/m2-tools-contract.md`

### B1 — REPLACE §3's paragraph after the code block (currently line 48, the "Registry order …" paragraph)

```markdown
Registry order (also the setup screen order, `tools/search_providers/__init__.py:53-67`): `ddgs`(★ default, no key) → `brave_free`(key) → `exa_free`(no key) → `keenable_free`(key) → `parallel_free`(key) → `tavily`(key) → `searxng`(self-hosted, `SEARXNG_URL`) → `firecrawl_selfhost`(self-hosted) → `exa`(paid) → `keenable`(paid) → `parallel`(paid) → `firecrawl`(key optional) → `xai_grok`(paid).

**Every catalog id MUST have a real client.** The placeholder `ThinProvider` is retired from the catalog (it stays in `tools/search_providers/providers.py` so a plugin can register an honest refusal), and `test_every_catalog_id_has_a_real_client` pins that. The M2 text that allowed "thin HTTP clients with the documented endpoint" for all but four ids is withdrawn: it is what let `exa_free` answer from `ddgs` while claiming to be Exa (CORE-search-fix).

Tags are what the live endpoints actually do, verified keyless:
- `exa_free` is **no key** and is the one `*_free` id that really is keyless — it drives Exa's official anonymous hosted MCP at `https://mcp.exa.ai/mcp` (`tools/search_providers/__init__.py:88-96`), not `api.exa.ai`. Paid `exa` keeps the keyed REST endpoint.
- `keenable_free`, `parallel_free` and `tavily` are free *tiers*, not keyless endpoints; all three answer `401` without a key, so they are tagged **key required** and `available()` is False without one.
- `firecrawl` (cloud) is **key optional**: its `/v1/search` answered keyless queries with real results.

`FREE_CHAIN` is the **fallback** chain, ordered by what a user is likely to have configured (`__init__.py:74-82`): `ddgs` → `exa_free` → `searxng` → `brave_free` → `tavily` → `firecrawl_selfhost` → `firecrawl`. Every entry is still gated by `available()`. A fallback MUST be reported in three places: `ToolResult.meta{provider, fallback_from, reason}`; a `[search via ddgs — fallback from exa_free: …]` prefix on the text handed to the model; and one non-fatal `error{code:"search_provider_unavailable"}` session event **per session** (not per turn). `tool.list` carries `ToolInfo.provider`, written as `exa_free → ddgs` when the configured id cannot run.
```

### B2 — APPEND to §2, after the "Stubs (M5/M7 tools)…" paragraph

```markdown
The `delegate` and `memory` rows are no longer stubs (M5/M7 shipped). Two audio tools joined the catalog (CORE-multimodal, see `docs/design/m10-multimodal-audio-contract.md`):

| category | tools | permission |
|---|---|---|
| audio | transcribe_audio, text_to_speech | declared `read` / `network`, **re-tagged per call** |

`text_to_speech` is no longer studio-only: `tools/audio_tools.py` replaces `media.py`'s version keeping the name and schema, adds an optional `play`, and runs the whole backend chain. `media.py` keeps `generate_speech` in its `FORWARDS` table (that is how the audio code reaches studio) but registers no speech tool. Both audio tools are `inactive` with a reason until a backend exists, mirroring the media tools, and both implement `permission_for`: `network` when the resolved backend is hosted (`openai`, `studio`), `read` when it is local. `text_to_speech` is *declared* `network` — the stricter of its two possibilities — so a failing hook can never widen it.
```

---

## `docs/design/m3-providers-setup-contract.md`

### C1 — REPLACE the last line of §1 (currently line 22)

```markdown
Auth methods per preset (`providers/presets.py`): `openai` = `("api_key","device_code","oauth_token")` (`:120`), `gemini` = `("api_key","google_adc","oauth_token")` (`:140`), `openrouter` = `("api_key","oauth_pkce")`; every other vendor is `("api_key",)`. `provider.list` returns `[{vendor, label, authMethods, configured, defaultModel, models?}]` for all 11.
```

### C2 — REPLACE §3 entirely

```markdown
## 3. Web token login (`providers/auth_web.py`)

Each flow is split into a `*_start` (talk to the vendor / open the callback server, return the code and URL) and a `*_finish` (poll or wait, exchange for a token), joined by a `LoginStart` dataclass whose `finish()` resumes to a `LoginResult` (CORE-login-progress).

- **`openai` device code** — POST the device authorization endpoint, surface `user_code` + `verification_uri`, poll the token endpoint until granted/expired; store under `settings.providers.openai`. Endpoints live in one `ENDPOINTS` dict so they can be corrected without code changes. This authenticates a **ChatGPT/Codex subscription**; direct OpenAI API billing remains a separate API-key path.
- **`openrouter` OAuth PKCE** — verifier/challenge, `https://openrouter.ai/auth?callback_url=http://localhost:<port>/callback&code_challenge=…&code_challenge_method=S256`, local aiohttp callback receives `code`, POST `https://openrouter.ai/api/v1/auth/keys` → `key` saved as `api_key`.
- **`gemini` Google ADC** — `google_adc_start` shells `gcloud auth application-default login` (`providers/auth_web.py:516-583`). It MUST fail with a named prerequisite when `shutil.which("gcloud")` is empty ("gemini OAuth login needs the Google Cloud CLI (`gcloud`)") rather than a generic error. On success it persists `{"auth_method": "google_adc"}` — no token is copied into `settings.json`; `gemini_native` shells `gcloud` per request for a fresh access token and sends it as `Authorization: Bearer …` (`providers/gemini_native.py:66-96`).
- **`gemini` / `openai` remote token** — for a headless or remote machine with no browser and no `gcloud`, an access token obtained elsewhere is stored as `settings.providers.<vendor>.oauth_token` with `auth_method: "oauth_token"`, and sent as a Bearer header (`gemini_native.py:97-98`; `providers/registry.py:157-159`, `:315-329`).
- Any other vendor → `RpcError("login_unsupported", "…use API key: snowpea setup --vendor <v> --key …")`.

RPC `provider.loginWeb(vendor, method)` answers `status: "await_user"` with `userCode` / `verificationUri` / `verificationUriComplete` / `expiresInSec` **as soon as the code or URL is known**, and runs the rest as a connection-tracked background task reporting `provider.loginProgress` (broadcast via `EventHub.notify`, like `system.updateProgress`). A failed background login is reported as `loginProgress{phase:"failed"}` and a log line — the RPC response is already gone, so nothing is attached to it, and nothing is persisted on that path.

`provider.configure` accepts these keys and silently drops the rest (`server/app_server.py:417-438`): `api_key`, `base_url`, `model`, `models`, `variant`, `token`, `oauth_token`, `refresh_token`, `auth_method`. An empty result after filtering is `invalid_params`.

CLI:
- `snowpea setup --login <vendor>` (unchanged).
- `snowpea provider login <vendor>` → `provider.loginWeb`, 900s timeout (`cli/commands.py:298-313`).
- `snowpea provider login <vendor> --token [TOKEN]` stores an OAuth access token directly via `provider.configure`; **`openai` and `gemini` only**, every other vendor exits `EXIT_USAGE` with "does not expose an OAuth access-token login; use its API key". A bare `--token` prompts with `getpass`; an empty answer is a usage error (`cli/commands.py:271-296`).
```

### C3 — REPLACE the `setup/wizard.py` bullet in §5

```markdown
- `setup/wizard.py`: `quick` (vendor screen only + defaults), `full` (all screens in order providers → search → browser → **audio** → tools → gateway → done; the Audio section — speech-to-text and text-to-speech backends, voice, read-aloud — was added by CORE-multimodal and deliberately carries no circled numeral, so the ①–⑥ the other screens print did not have to be renumbered), `blank` (write default settings, nothing asked). `setup/detect.py` finds env keys, existing `~/.hermes` / Claude Code config for import hints, node/uv presence.

  **Authentication prompt (`setup/wizard.py:251-281`).** When the picked vendor's preset lists more than one auth method the wizard asks `authentication [1=API key, 2=browser login, 3=OAuth token (remote/headless)] (Enter=1):`. Option 3 appears only when `"oauth_token"` is in the preset's `auth_methods`. `2` runs `auth_web.login(vendor)` synchronously and, on success, merges the returned credentials into the vendor block while clearing any stale `api_key`/`oauth_token`; a failure is a `state.notes` line, never an exception that ends the wizard. `3` prompts for the token with `secret=True` and sets `auth_method = "oauth_token"`. Anything else falls through to the API-key prompt, so Enter still means "key" as before, and a vendor with one auth method is asked nothing new.

  **Gateway follow-ups (CORE-gateway-autostart).** `setup/screens/gateway.py` stays a pure build/apply pair with no I/O; `wizard._ask_for_gateway` asks for the token and then the approver's account id (`settings.gateway.<platform>.allowed_user_id`) the way `_ask_for_key` follows the providers screen. Blank is allowed and is reported in the summary as `telegram (no approver)` with a note that chat approvals stay blocked — a read-only messenger is a legitimate setup. `--gateway/--token/--user-id` and the non-interactive path are unaffected.
```

### C4 — APPEND a new section after §5

```markdown
## 6. Model profiles and per-agent routing (`config/model_routing.py`)

`settings.json` carries three related keys (`config/settings.py:42-69`):

```jsonc
{
  "models": {
    "default": "sonnet",                                   // profile id used by new sessions
    "profiles": {                                          // ModelProfile{provider, model}
      "sonnet": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
      "cheap":  {"provider": "openai",    "model": "gpt-5-mini"}
    }
  },
  "agents": {
    "models": {"executor": "cheap", "architect": "sonnet"} // agent name -> profile id
  }
}
```

`route_for(settings, *, provider, model, agent, definition_model) -> ModelRoute{provider, model}` (`config/model_routing.py:18-44`) resolves in strict precedence order, first non-empty wins:

1. explicit `provider` / `model` arguments (already-resolved caller intent — `session.create`, `/model`, an RPC arg);
2. `settings.agents.models[agent]`;
3. `definition_model` — the `model:` field of the agent's `.md` definition;
4. `settings.models.default`.

Returning `ModelRoute(None, None)` is meaningful: it means "no multi-model settings apply", and the caller MUST keep the old `ProviderRegistry` default or parent inheritance rather than substituting anything.

A reference is resolved by `resolve_reference` (`:47-61`): a known profile id wins; `"inherit"` and the empty string resolve to nothing; `vendor:model` is accepted as the legacy spelling; a bare unknown word is read as a bare vendor name.

The settings validator MUST reject unknown profile references — both `models.default` and every value in `agents.models` — so a typo is a load-time error rather than a silent fall-through to a different model (`config/settings.py:286-292`).

`/model` lists the configured vendor's models and accepts either a name or a **row number** (`/model 2`); it always persists, and there is no `--save` flag. `provider.models` lists for all eleven vendors; discovery is skipped under `SNOWPEA_PROVIDER_MODE=replay`. The `local` preset's `default_model` is a placeholder: an `openai_compat` adapter built with a placeholder and no `model_resolver` MUST raise `model_not_configured` rather than calling the server, and a listing made only of placeholders counts as no listing.
```

---

## `docs/design/m5-memory-scheduler-gateway-contract.md`

### D1 — REPLACE the `class Job` line in §2's code block

```python
class Job(BaseModel): id, spec: str, kind: Literal["cron","once","interval"], cron: str|None,
    interval_sec: int|None, next_run: datetime|None, task: str, mode: Mode,
    channel: str|None, origin_session_id: str|None, agent: str|None, workdir: str|None,
    enabled: bool, state: Literal["scheduled","running","cancelled"],
    last_run: datetime|None, last_status: Literal["ok","error","denied_by_timeout"]|None, created_at
```

### D2 — REPLACE the "Execution:" bullet of §2, and APPEND the new §2.1

```markdown
- Execution: creates a session (`workdir` = job.workdir or home, `mode` = job.mode, `originSurface="scheduler"`, `unattended=True`) and runs `session.prompt(task)`; `job.event{jobId, kind: started|finished|failed|denied}` notifications are emitted. Delivery of the final assistant text is §2.1.

### 2.1 Reminder delivery (`scheduler/scheduler.py:341-374`)

A scheduled job now reports back **into the session that created it**, not only to an external channel. `Job.origin_session_id` records that session (`scheduler/jobs.py:109`), is projected onto the wire as `JobInfo.originSessionId` (`server/protocol.py:840-842`), and is added to an existing `jobs` table by an online migration at store open — `PRAGMA table_info(jobs)` then `ALTER TABLE jobs ADD COLUMN origin_session_id TEXT` when the column is missing (`scheduler/jobs.py:237-241`). Existing rows are all channel-only jobs, so `NULL` is exactly right and no data moves.

`Scheduler.deliver(job, text)` MUST:

1. Try the originating session first (`_deliver_to_session`, `:353-374`). The reminder is emitted as an ordinary `message.done` **session event**, so it is persisted, replayed on resume and rendered by every surface with no new event kind. Its text is prefixed:

   ```
   ⏰ Scheduled reminder (<job.id>)

   <text>
   ```

2. If the session is not live, restore it through `SessionManager.restore()` (M1 §17-3), deliver, and **close it again** — a reminder must not leave a session open that the user had finished with. If the session cannot be restored at all (deleted, or the store has no row), log a warning naming both ids and fall through.
3. Then, and independently, deliver to `job.channel` through the gateway router when one is configured. **An explicit channel is additive, not a replacement**: a job with both an origin session and `channel: telegram:<id>` reaches both.
4. Fall back to `$SNOWPEA_HOME/logs/jobs.log` when the session delivery failed, or whenever an explicit channel was requested (so the log always records what a channel was asked to carry).

A dead gateway is a warning and a log-file fallback, never a failed job.
```

---

## `docs/design/m6-m7-skills-agents-contract.md`

### E1 — APPEND to §2 (agent definitions)

```markdown
**Built-in roles are resolvable agents.** `builtin_agent_definitions()` (`agent/definition.py:289-305`) synthesises an `AgentDefinition` with `source="builtin"` for every non-underscore file in `core/snowpea_core/prompts/roles/`: **architect, critic, executor, explorer, test-engineer, verifier** (`_preamble.md` is skipped). They make the package roles visible through `/agent list` and `agent.list` without writing files into a user's project, and `delegate_task(agent="architect")` resolves without one.

Precedence is the loader's existing rule, not a new one: `builtin` < `global` (`$SNOWPEA_HOME/agents/*.md`) < `project` (`<workdir>/.snowpea/agents/`, `<workdir>/.claude/agents/`). A project file named `executor.md` therefore **overrides** the built-in role entirely; it does not merge with it.

**`AgentDefinition.prompt` is appended to the composed rules, not substituted for them.** `subagent.py` used to assign it to `child.system_prompt`, which replaced `BASE_PROMPT` outright, so a named agent lost every coding-discipline rule. It is now composed as a persona *after* the role file. `compose.build_tiers(identity=…)` keeps the old replace semantics for a caller that genuinely wants a bare prompt; nothing in core passes it.
```

### E2 — REPLACE §3 (Subagents) — the paragraph gains the refusal contract

```markdown
## 3. Subagents — `agent/subagent.py`, `tools/delegate.py`

`delegate_task(task, agent?: name, tools?: [..], timeout?)` (permission `exec`): spawns a child agent run with its own Session (`parent_session_id`, `originSurface` inherited, mode inherited unless the definition sets one, same backend) under a semaphore `agents.max_concurrent` (global settings < project settings < `session.create(maxConcurrent)`); events on the **parent** session: `subagent.spawn{agentId, name, task}`, `subagent.update{agentId, status: queued|running|done|error, lastText?}`, `subagent.done{agentId, status, summary, usage}`; child events also flow on the child session id. `agent.spawn(name, task)` RPC = same path from a client. `snowpea agents --json` lists running/queued subagents (`agent.list` includes `kind: subagent|named` and `status`). TUI `SubagentTree.tsx` renders them under the parent.

### 3.1 Agent-name resolution — **BREAKING CHANGE**

An unresolvable agent name is now **refused**. Previously a `delegate_task` or `agent.spawn` naming an agent that did not exist ran silently with the parent's own settings and prompt, which made a typo look like a successful delegation. The order of checks in `SubagentManager.run` (`agent/subagent.py:390-399`) is:

1. **Team membership first.** If the parent session has `team_agents` and the requested name is not a member → refuse:
   `agent '<name>' is not in active team '<team>'; choose one of: <members>`
2. **Then existence.** If a name was given and `self.definition(parent, agent)` is `None` → refuse: `unknown agent '<name>'`.

A refusal is not an exception. `_refuse` (`agent/subagent.py:430-443`) sets the record to `status="error"` with the message as `error`, emits the normal `subagent.done` so the parent's tree closes the node, and returns `SubagentResult(ok=False, summary="", status="error", error=…)`. The refusal reaches the model as the tool's failed result, which is exactly the shape §M1-8 uses for a denial — the model can correct the name and retry within the same turn.

A **missing** agent name (none given at all) is not an error: it resolves to the active team's first member when a team is active, and to `"executor"` otherwise.
```

### E3 — APPEND a new §8 (after §7 Tests, renumbering nothing)

```markdown
## 8. Project teams (`agent/team_config.py`, `commands/team_cmd.py`)

A **team** is a named list of agent names that bounds what a session may delegate to. Two settings surfaces define them:

| scope | file | keys |
|---|---|---|
| global | `$SNOWPEA_HOME/settings.json` | `agents.teams: {name: [agentNames]}`, `agents.default_team: str\|null` (`config/settings.py:45-49`) |
| project | `<workdir>/.snowpea/settings.json` | `agents.teams`, `agents.activeTeam: str\|null` (`config/project.py:40-45`) |

Resolution (`agent/team_config.py:16-31`):
- `teams_for()` merges global then project, so **a project team wins on a name clash** — a project may redefine `default` without touching the user's global file.
- `active_team()` reads the project's `activeTeam` first and falls back to `settings.agents.default_team`. A name with no members, or no name at all, resolves to `None` (= no team, unrestricted delegation).
- Member lists are de-duplicated preserving order (`dict.fromkeys`).

`Settings.load()` migrates older installations at first read: a settings document that has an `agents` block but no `teams` gets `teams["default"] = DEFAULT_AGENT_TEAM` and `default_team = "default"` written (`config/settings.py:276-281`). An explicitly empty team set is still representable afterwards as `default_team: null`.

**Effects of an active team**, all three of which MUST hold together:
1. The system prompt carries the restriction rule (M1 §17-6, `agent/agent.py:128-133`).
2. `delegate_task` / `agent.spawn` refuse a non-member (§3.1).
3. `agent.list` filters definitions down to the team's members and **prepends one synthetic row** `AgentInfo(name=<teamName>, description="Active project team", kind="team", source="project")` (`server/agent_handlers.py:83-107`). `kind="team"` is a fourth value alongside `definition|subagent|named`; a client that does not know it MUST render it as an ordinary row rather than dropping it. Named instances and running subagents are appended after the filter and are **not** filtered by team.

`/team` keeps its `N <task>` form (§5) and gains four configuration subcommands (`commands/team_cmd.py:22-23`, `:42`, `:88-140`):

```
/team <N> "<task>"              # unchanged: run N workers on one task
/team create <name> <agent...>  # define/redefine a project team
/team use <name>                # set the project's activeTeam
/team list                      # every team visible here, marking the active one
/team delete <name>             # remove a project team
```

The first word disambiguates: a leading `create|use|list|delete` is configuration, anything else is the worker form. `/team use <unknown>` answers `team: unknown team <name>; use /team list` and changes nothing.

**TUI short delegation.** `$agent-name <task>` in the input line is a surface-local shortcut that calls `agent.spawn` directly rather than going through `session.prompt` (`tui/src/app.tsx:1036-1041`, pattern `/^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/`). The same refusal rules apply — the daemon does not know the input came from a shortcut.
```

---

## `docs/design/m8-packaging-contract.md`

### F1 — REPLACE §4's last sentence

```markdown
On tag `v*`: build TUI bundle → `uv build` → upload wheel/sdist as release assets (+ PyPI publish when `PYPI_TOKEN` secret exists) → `npm publish` `@snowpea/sdk` and `@snowpea/tui` (when `NPM_TOKEN` exists) → attach `installer/install.sh`. ~~Bump `PROTOCOL_VERSION` to `1.0.0` in the v0.1.0 release commit.~~ **Withdrawn.** `PROTOCOL_VERSION` moved with the feature work instead and reads `1.3.0` at v0.1.7 (`server/protocol.py:23`); the v1.0 freeze gate is defined by three consecutive releases with no change to the generated schema, and applies before the v0.2 IDE — not by a release-commit bump.
```

### F2 — APPEND a new §7

```markdown
## 7. Update channels, provenance and the downgrade guard (`update.py`, `server/update_handlers.py`)

### 7.1 Install provenance

An installation records how it was installed in `$SNOWPEA_HOME/install.json` (written by `record_install` in `install.sh` and `Record-Install` in `install.ps1`). For a git install that is cross-checked against **PEP 610** `direct_url.json` in the installed dist-info, and `git_install_provenance(paths) -> GitInstall{branch, installed_revision} | None` (`update.py:141-175`) returns a branch install only when all of the following hold:

- `direct_url.json`'s `url` normalises to this repository's URL and its `vcs_info.vcs == "git"`;
- the resolved branch is in `_TRACKED_BRANCHES` (a bare git install with a commit and no requested revision is read as `main`);
- `vcs_info.commit_id` is a full SHA.

A branch name alone is never enough: without an installed commit there is nothing to compare against, and `_check_git_branch` answers with `error: "cannot determine the installed git revision; reinstall from main"` rather than offering an update (`update.py:353-359`).

### 7.2 The downgrade guard

Two channels, two different guards, and **neither may ever offer a move backwards**:

- **PyPI / tag installs** — strict semver greater-than only. `is_newer(latest, current)` parses both with `_VERSION_RE` and returns `left > right`; an unparseable version on either side is `False`, never `True` (`update.py:191-196`).
- **Git branch installs** (`update.py:360-413`) — the installed commit and the branch head are compared through `GET https://api.github.com/repos/<repo>/compare/<installed>...<head>`, and `available` is set **only** when `status == "ahead"`:

  | compare `status` | outcome |
  |---|---|
  | `identical` (or `_same_revision`) | `available: false`, no error |
  | `ahead` | `available: true` |
  | `behind` | `available: false` — the local build is in front of the branch; replacing it would be a downgrade |
  | anything else (`diverged`, missing) | hard error `"branch history diverged; refusing an automatic replacement"` |
  | compare call non-200 | hard error `"could not verify update ancestry"` |
  | any network failure | `_answer(error=str(exc))` — never an exception out of startup |

  A successful answer is cached under `installKey = f"git:{branch}:{revision}:{__version__}"`, so the cache is invalidated by an upgrade without a new network call. **A failed check is never cached** — a briefly offline machine must not report "no update" for 24 hours.

### 7.3 Displayed version for a branch upgrade

A branch install tracks commits, but the prompt must still name a release. `_git_version_at(client, revision)` (`update.py:322`) reads `core/snowpea_core/__init__.py` at the target commit over `raw.githubusercontent.com` and, when that version `is_newer` than the running one, the offer is labelled with it. Result fields are `latest = f"{displayVersion}+{sha[:8]}"` and `current = f"{__version__}+{installedSha[:8]}"`, so `v0.1.2+oldsha → v0.1.3+newsha` rather than the misleading `v0.1.2+newsha`. **Tag discovery is presentation-only**: any failure is swallowed at `debug` and MUST NOT invalidate the ancestry check. After the upgrade, `installed_cli_version()` (`update.py:658`) shells `snowpea --version` to report what actually landed rather than what was offered.

### 7.4 RPC contract

- `system.checkUpdate{force?}` — 24h cache unless `force`. The handler strips the internal keys `installKey`, `trackingSource` and `configured` from the answer before validating it into `CheckUpdateResult` (`server/update_handlers.py:47-57`); they are provenance bookkeeping, not part of the wire schema.
- `system.update` — **forces a fresh check first** (`check_update(paths, settings, force=True)`, `update_handlers.py:63`). An explicit upgrade must not act on a negative 24-hour cache, because a tracked branch may have advanced since startup checked it. It then refuses when the fresh answer carries an `error` or is not `available`, returning `UpdateResult{started: false, command: "", log: <path>, error: <reason or "No newer update is available; refusing to reinstall or downgrade.">}` (`:64-75`). Refusing is a successful RPC, not an error.
- `system.restart` — shuts the daemon down via `core.request_shutdown("restart")`; the next `snowpea` launch spawns a fresh one through the existing `ensure_daemon`. The TUI half of the handshake is exit code `75` (EX_TEMPFAIL) turned into `os.execv` by `cli/main.launch_tui`.
- `update_command` prefers `uv tool install --force --reinstall` over the recorded install method, falls back to `install.json`, and when neither is recognisable returns `started: false` with the manual command in `command` — it never guesses at `pip` or `npm`. `update.with_images` threads the `[images]` extra through every upgrade path so an upgrade cannot silently drop image downscaling.
- `SNOWPEA_UPDATE_CHECK=0` overrides `updates.check` and disables the daily background check (the test suite sets it globally).
```

---

### 3b. Four new contract files

## `docs/design/m9-prompts-contract.md`

```markdown
# M9 Contract — Prompt Library, Tiers and Turn Queueing (binding; ships CORE-prompts)

Retrofit contract for work that shipped under `docs/design/deviations/CORE-prompts.md` and the
prompt-queueing half of the post-v0.1.1 session work. Builds on M1 §8 (agent loop) and M6/M7 §2
(agent definitions). Source of record: `core/snowpea_core/prompts/`, `core/snowpea_core/agent/`.

Acceptance criteria: **AC-22 … AC-26**.

## 1. Library layout (`core/snowpea_core/prompts/`)

```
prompts/
  base.md                 # the coding-discipline rules every agent gets
  config_rule.md          # never rewrite settings.json with write_file
  search_honesty.md       # never present a fallback provider's answer as the configured one
  modes/{plan,accept,auto}.md
  vendors/{anthropic,openai-family,small-local}.md
  roles/{architect,critic,executor,explorer,test-engineer,verifier}.md + _preamble.md
  fragments/{environment,tools,memory,context-pressure}.md
  workflows/{ralph-prd,ralph-story,ralph-review,ultrawork-split,team-plan,
             team-task,team-conflict,agent-generate,skill-learn,compaction}.md
  compose.py loader.py environment.py tool_descriptions.py
```

Every file above is **prompt text the model reads**, and MUST live here rather than inline in the
module that uses it, so that prompt review sees all of it in one place. In particular tool
descriptions live in `prompts/tool_descriptions.py` and are imported by name from `tools/*.py`;
the JSON schemas stay with the tools.

## 2. Three tiers, and why (`compose.build_tiers`, `compose.build_system_prompt`)

The system prompt is assembled as three concatenated tiers, ordered most-stable first so that a
provider's prefix cache survives as much of it as possible:

| tier | contents | budget (tokens) |
|---|---|---|
| stable | `base.md`, the vendor layer, `config_rule.md`, `search_honesty.md`, the mode file, and for a subagent the `_preamble` + role file | **2200** |
| context | environment block, `AGENTS.md`-style context files, tool listing | 800 |
| volatile | memory recall block, context-pressure note, reply-language override | 600 |

**AC-22.** `tests/test_prompts_compose.py` MUST assert every tier against
`TIER_BUDGET_TOKENS = {"stable": 2200, "context": 800, "volatile": 600}` (the constant and its
rationale live at the top of that file, `:23-29`), for every combination of mode × vendor class ×
role. The measured worst case today is plan + small-local + executor at ~2068 tokens. The budget's
job is not to be tight; it is to make unnoticed growth of the library impossible.

Rules the composition MUST obey:

- **Vendor layers are keyed on the provider preset, not the model id** (`compose.VENDOR_CLASS_BY_PROVIDER`,
  `:33-79`). An unrecognised provider falls to `openai-family`, the safer default for an unknown
  hosted endpoint. `vendors/anthropic.md` is deliberately empty.
- **Only the date goes in the prompt, never the time.** A clock in the stable or context tier
  defeats prefix caching on every provider. The environment block states that the date is fixed and
  names a tool for the exact time.
- **The environment block is cached for 30 seconds per session**, keyed on `(session id, workdir)`,
  and invalidated by `agent.invalidate_environment()`. `compaction.prompt_messages` rebuilds the
  whole prompt purely to measure it, on every turn and every `context` event; probing git and
  reading `AGENTS.md` each time would put a subprocess on an accounting path. **A git failure of any
  kind drops the workspace block entirely** rather than reporting a guess.
- **`agent.replyLanguage` defaults to `"auto"`, which emits no rule at all** (`config/settings.py:90`).
  `base.md` already says to answer in the language the user wrote in. An explicit tag emits a
  directed override naming the language. Prompt files stay in English; translating a system prompt
  degrades instruction following, and the reply language is the part the user cares about.
  `/ralph` and `/team` MUST thread the resolved language into their workers' briefs, because a child
  cannot see the parent's settings.
- `build_system_prompt` and `build_messages` take a keyword-only `core` (default `None`, which
  resolves the language to `"auto"`), so no caller outside `loop._drive` and
  `compaction.prompt_messages` had to change.

## 3. Subagent prompts

**AC-23.** Every child MUST receive the subagent preamble, definition or not. A definition whose
name matches a file in `prompts/roles/` also gets that role; one that does not composes without it.
Before this, `/ralph`, `/ultrawork` and `/team` workers ran the bare base prompt with **no report
contract at all**.

A workflow brief prepends `compose.BRIEF_RULES` — a faithful three-clause digest of `base.md`
(read before you edit; change only what the task needs; run the project's checks; never report a
result you did not produce) — substituted into the brief's `${BASE_RULES}` placeholder, not the
full 717 words, which the child already has in its own system prompt. `base_rules=False` turns it
off per call.

The JSON-emitting workflow prompts — `ralph-prd`, `ultrawork-split`, `team-plan`, `agent-generate`,
`skill-learn`, `compaction` — MUST NOT prepend rules. They are one-shot "answer with a single JSON
object and nothing else" prompts and prose in front of that instruction fights it. The four real
subagent briefs — `ralph-story`, `ralph-review`, `team-task`, `team-conflict` — do prepend.

## 4. Denials adapt; they do not end the turn

**AC-24 (amends M1 §8).** A refused tool call MUST be appended to the conversation as a failed
`tool.result` carrying the refusal text, and the loop MUST continue, so the model can choose a
different approach. `MAX_DENIALS_PER_TURN = 3` (`agent/loop.py:46`) bounds a model that only ever
re-sends the refused call; the third denial ends the turn with `turn.done{reason:"denied"}`
(`agent/loop.py:330`). The `mode_denied` / `approval_denied` error events are unchanged, so no
surface has to learn anything new, and `job.lastStatus == "denied_by_timeout"` is derived from the
approval decision rather than the turn reason and is unaffected.

Nine tests in `test_session_loop.py`, `test_permission_matrix.py` and
`test_approval_multichannel.py` encoded the old "one denial ends the turn" contract and were
updated to the new one (the tool never runs, the error event still fires, the refusal arrives as a
failed `tool.result`, the turn goes on).

## 5. Prompt queueing

**AC-25.** A prompt submitted while a turn is running MUST be queued, never dropped, never run
concurrently against the same history.

```python
# session/session.py:65 — in-memory only
queued_turns: list[QueuedTurn] = field(default_factory=list)
```

`start_turn` (`agent/loop.py:103-124`) always mints a `turnId` and builds a complete
`QueuedTurn{turn_id, text, unattended, attachments}`, taking the pending attachments
**synchronously** so a later prompt's images cannot leak into an earlier turn. If
`session.turn_task` is live the turn is appended and the id returned; otherwise one
`_drain_turns` task is started and drains the FIFO (`agent/loop.py:127-146`), clearing
`session.interrupt` before each turn and `session.current_turn` in a `finally`.

Known limits, both recorded deliberately:

- `session.interrupt` cancels only the in-flight turn. `session_interrupt_handler`
  (`server/session_handlers.py:344-349`) does not clear `queued_turns`, so the queue keeps
  draining afterwards. There is no "cancel everything" call.
- `queued_turns` is not persisted; a daemon restart loses every queued prompt, and the client
  never receives `turn.done` for the `turnId` it was already given.

## 6. Tests

**AC-26.** `tests/test_prompts_compose.py` (tier budgets, vendor keying, role composition,
`BRIEF_RULES` substitution and its absence from the JSON workflows) and
`tests/test_prompts_behaviour.py` (the model adapts after one denial; three denials end the turn)
MUST both pass with the fake provider and no network. Prompt text is not projected into
`protocol.py`, so `scripts/gen_protocol.py --check` MUST report no drift from prompt-library
changes.
```

---

## `docs/design/m10-multimodal-audio-contract.md`

```markdown
# M10 Contract — Attachments, Vision Content Parts and Voice I/O (binding; ships CORE-multimodal)

Retrofit contract for `docs/design/deviations/CORE-multimodal.md`. Builds on M1 §5 (providers),
M1 §6 (tools), M2 §5 (media tools) and M3 §5 (setup wizard). Source of record:
`core/snowpea_core/{attachments,audio}/`, `providers/content.py`, `server/audio_handlers.py`.

Acceptance criteria: **AC-27 … AC-31**.

## 1. Attachments (`attachments/`)

`session.prompt(sessionId, text, attachments?: list[Attachment])`; `Attachment` is
`{kind: "file"|"image"|"text", name?, mimeType?, path?, data?, text?}` with exactly one of
`path`, `data`, `text` carrying content (`server/protocol.py:190-202`).

- **The sniffed MIME type beats the client's, except when the client claims an image.**
  `attachments/model.py` decides from magic bytes, so a binary named `notes.txt` is
  `application/octet-stream`. The one case the client is believed is bytes that sniff to
  octet-stream *and* a declared non-image type; a declared `image/png` whose bytes have no PNG
  header travels as a named file rather than as a broken image block.
- **A file the user points at is referenced, never copied.** `AttachmentStore.save` writes only
  inline (base64) attachments, under `<SNOWPEA_HOME>/attachments/<session>/<sha256>.<ext>`.
  Content addressing means pasting the same screenshot ten times costs one file.
- **Cap: 20MB** in `model.py`; the websocket frame cap is `transport_ws.MAX_MESSAGE_BYTES = 32MB`
  so a 20MB file still fits once base64 has added a third. The thing that refuses an oversized
  file MUST be the check that can name the limit, not a dropped connection.
- **`session.prompt` stashes rather than widening `start_turn`.** `attachments/pending.py` is a
  per-session take-once queue: the handler validates, stores and stashes
  (`server/session_handlers.py:305-340`), and the loop calls `pending.take(session.id)` at the top
  of the turn. A slash command clears it (nothing would consume the bytes) and a second prompt
  before the turn starts **replaces** rather than appends, so an interrupted turn cannot leak its
  images into the next one.
- **`kind: "text"` attachments are inlined into the prompt text**, joined by blank lines and
  labelled `[name]`, and are not stored (`session_handlers.py:322-328`).
- **The prompt text is never rewritten.** The text the user typed reaches the model as typed; the
  `[image: shot.png]` marker lives on the content block, not in the prompt.
- Image downscaling above 1568px on the long edge is **best-effort**: done when Pillow is
  importable, skipped when it is not and when the image is animated. Pillow ships as the
  `[images]` extra and both installers request it; `SNOWPEA_SKIP_IMAGES=1` opts out, and
  `update.with_images` carries the same extra through every upgrade path.

**AC-27.** History MUST store a block list with paths, never base64:

```jsonc
ChatMessage.content = [
  {"type": "text",  "text": "what is in this?"},
  {"type": "image", "path": "/…/<sha256>.png", "sha256": "…", "text": "[image: shot.png]"}
]
```

Three properties fall out and all three are required: the session store stays small; a resumed
session can still send the picture because the bytes are re-read when the request is built; and
the `text` marker on every block means the token estimate, the history renderer and the fake
provider's matcher all see something sensible without decoding anything. **A turn with no
attachments MUST keep a plain string**, so every pre-existing request is byte-identical. An
attachment whose file has gone degrades to `[image: shot.png] (no longer on disk)` rather than
raising — a deleted screenshot must not make a resumed session unanswerable.

**Known gap.** `session.deleteSaved` does not remove `<home>/attachments/<session_id>/`; those
files are orphaned (M1 §17-2).

## 2. Vision content parts (`providers/content.py`)

**AC-28.** `supports_vision(vendor, model)` is an **allowlist**, and an unknown model is assumed
text-only: True for every Anthropic and Gemini model, and for OpenAI-compatible models whose name
matches `VISION_HINTS`. Anything else gets the text fallback — `[image attached: shot.png]` plus a
note telling the model it cannot see it. The alternative default (assume vision, let the vendor
400) turns an unknown model into a failed turn rather than a degraded one.

`to_openai` MUST return a plain string whenever it can, so a text-only turn produces exactly the
request shape the pre-attachment code produced. Vision is decided in `build_openai_request`, which
is the one place that knows both the preset and the resolved model; `messages_to_openai` takes an
explicit `vision` flag defaulting to `False`. Anthropic needs no flag (every model sees); Gemini
takes `inlineData` unconditionally.

## 3. Voice (`audio/`, `server/audio_handlers.py`)

Five RPCs, all in `IMPLEMENTED_METHODS` (`server/audio_handlers.py:47-53`,
`server/protocol.py:1586-1614`):

| method | params | result |
|---|---|---|
| audio.capabilities | – | per-capability availability **plus `reasons`** |
| audio.transcribe | path / recording ref | text |
| audio.speak | text, voice?, play?: bool=false | path, played: bool |
| audio.record.start | sessionId? | ok |
| audio.record.stop | sessionId? | path |

**AC-29.** Both directions are **provider chains, not single integrations, and the local backend
wins.** STT tries `local-whisper` → `openai` → a user `command`: transcription is the one path
where audio of the user's room would otherwise leave the machine, so an installed whisper CLI
outranks the hosted API. TTS tries `studio` (the snowpea-studio MCP forward) → `openai` →
`edge-tts` / `piper` / `say` / `espeak-ng` / `powershell` → a user `command`. The practical
requirement: **speech MUST work on a bare Linux box with `espeak-ng` and nothing configured.**

**AC-30.** Every audio capability reports *why* it is off. `audio.capabilities` returns `reasons`,
a per-capability sentence such as `"install sox (rec), alsa-utils (arecord) or ffmpeg"`. A voice
feature that silently does nothing is indistinguishable from a bug, and the client cannot guess
what the daemon's machine is missing.

Further rules:

- `audio.speak` defaults to synthesise-and-return, because the client is usually where the
  speakers are. `play: true` plays on the daemon's machine; a playback failure is logged and
  reported as `played: false`, never a failed call — the audio exists either way.
- **Speaking can never fail a turn.** `loop.speak_reply` swallows everything: no backend, a
  synthesiser that times out, a player that exits non-zero.
- `audio.spoken` is a **session event kind** (`server/protocol.py:1309`, `:1363`), not a top-level
  notification: it happens between `message.done` and `turn.done`, so it is ordered with the reply
  it speaks and replayed on resume. A failed playback still emits it with `played: false` so a
  client on another machine can play the file itself.
- Recorders are **per session**, and `stop()`/`cancel()` MUST use `communicate()`, not a bare
  `process.wait()` — awaiting `wait()` hangs forever when the capture tool is chatty, because
  asyncio resolves the exit future only after every pipe closes.
  `tests/test_audio.py::test_stop_drains_a_chatty_recorder` is the regression.
- The `audio/` package MUST NOT import `Settings`. `audio.AudioConfig` is a plain frozen dataclass
  mirroring `settings.audio`, built by the server layer, so the whole package unit-tests without a
  settings tree and the dependency points one way. `settings.audio` itself is typed
  (`AudioSettings`/`SttSettings`/`TtsSettings`, `config/settings.py:170-177`), while
  `audio_handlers.audio_config` reads through one tolerant accessor taking either a model or a
  dict, so a hand-written block with missing keys still works.
- OpenAI credentials are **borrowed, not asked for twice**: both OpenAI backends read the key and
  base URL the provider registry already resolved (`api_key_for("openai")`), env fallback included.
- `"off"` is a real answer, distinct from `"auto"`, in both directions.

## 4. Tools

`tools/audio_tools.py` registers two tools and `media.py` registers no speech tool (its `FORWARDS`
table keeps `generate_speech`, because that is how the audio code reaches studio):

| tool | declared tag | resolved per call |
|---|---|---|
| `transcribe_audio` | `read` | `network` for the hosted backend, `read` for a local one |
| `text_to_speech` | `network` | same, but declared at the stricter of its two possibilities |

`transcribe_audio` is tagged `read` because the tag describes the tool's *guaranteed* effect — it
reads a local file — and making every transcription `network` would have made the common
local-whisper case ask for a permission it does not need. `read` rather than `exec` for a local
backend is deliberate: the mode matrix gates egress and changes to your project, and a local
backend does neither; tagging it `exec` would deny "read this out to me" in plan mode. Because
`text_to_speech` is *declared* `network`, a `permission_for` hook that ever fails cannot widen it.
Both tools are `inactive` with a reason until a backend exists, mirroring the media tools, and
`hot_reload.rebind` calls `audio_tools.refresh_state` (guarded, so a reload can never fail on it).

## 5. Setup wizard

See M3 §5. The Audio section sits between Browser and Tools, carries **no circled numeral** (the
other screens print ①–⑥ and renumbering three of them for cosmetics was rejected), and is two
screens plus conditional prompts: the STT list is the screen proper; the TTS list, the voice name,
the "read replies aloud" toggle and the "test the voice now" offer follow it and are asked **only
when the user actually picked a TTS backend**. A non-interactive run writes
`{"stt": {"provider": "auto"}, "tts": {"enabled": true, "provider": "auto", "autoSpeak": false}}`
and asks nothing. Picking Off for speech also clears auto-speak. Rows for backends that are not
installed are shown **inactive, not hidden** — the answer to "why can't I use piper" should be on
the screen rather than missing from it.

## 6. Tests and docs

**AC-31.** `tests/test_multimodal_rpc.py` (validation, storage, sniffing, the text fallback;
it lowers `MAX_BYTES` rather than pushing 20MB through the socket, and says why) and
`tests/test_audio.py` (chain resolution, `reasons`, the recorder drain regression) MUST pass with
no network and no audio hardware. TUI coverage: `tui/test/{attachments,attach-ui,voice,audio-runtime,audio-tools,clipboard}.test.*`.
One manual page covers both halves — `docs/manual/{en,ko}/voice.md` — because attachments and voice
are one story from the user's side; the tool tables in `modes.md` carry the two new tools.
```

---

## `docs/design/m11-context-compaction-contract.md`

```markdown
# M11 Contract — Context-Window Tracking, Compaction and Store Shutdown (binding; ships CORE-context, CORE-memory-race, CORE-session-race)

Retrofit contract for `docs/design/deviations/CORE-context.md`, `CORE-memory-race.md` and
`CORE-session-race.md`. Builds on M1 §4 (sessions/events), M1 §8 (agent loop) and M5 §1 (memory).
Source of record: `core/snowpea_core/session/compaction.py`, `providers/context_windows.py`,
`session/store.py`, `memory/store.py`, `server/app_server.py`.

Acceptance criteria: **AC-32 … AC-36**.

## 1. Protocol surface (all additive, carried by `PROTOCOL_VERSION = 1.2.0`)

| addition | shape |
|---|---|
| `session.event.kind: "context"` | `{used, window, percent, estimated, model, provider}` |
| `session.event.kind: "compaction"` | `{before, after, summaryChars, auto, kept}` |
| method `session.compact` | `{sessionId, instructions?}` → `{before, after, summaryChars}` |
| `SessionSummary.contextUsed`, `.contextWindow` | for a surface that connects mid-session |

```jsonc
{"kind": "context",
 "used": 12345,        // tokens the current prompt occupies
 "window": 200000,     // null when unknown -> render "?"
 "percent": 6.2,       // null when window is null
 "estimated": false,   // true while 'used' is a local chars/4 estimate
 "model": "claude-sonnet-4-5", "provider": "anthropic"}

{"kind": "compaction",
 "before": 48210, "after": 2140, "summaryChars": 1832,
 "auto": true,         // false for /compact and session.compact
 "kept": 4}            // messages kept verbatim after the summary
```

**AC-32.** Exactly one `context` event MUST precede **every** `turn.done`, and one MUST follow
every compaction. Ordering is the whole point and was got wrong once: `turn.done` is what every
surface treats as terminal — the headless CLI stops reading and closes the session on it — so a
`context` emitted afterwards never reached `snowpea -c --json` at all. Every `turn.done` in
`agent/loop.py` therefore goes through one `finish_turn()` helper (`:63-91`) that emits the reading
first, covering the `interrupted`, `denied` and `error` paths that used to emit inline. Consumers
may keep treating `turn.done` as terminal. The emission is skipped entirely while `Core.stopping`
is set.

`--json` gained a `context` field on the final `result` line as well as the streamed event, so a
script that reads only the last line sees the window without buffering. `Renderer.finish` takes the
new argument with a default, so a third-party renderer implementing the old signature still
type-checks. CLI: `snowpea session context [id]` and `snowpea session compact <id> [instructions]`.

## 2. Window resolution (`providers/context_windows.py`)

**Two lookup methods, not one, and the difference is binding:**

```python
ProviderRegistry.context_window(vendor, model) -> int | None      # sync, no I/O
ProviderRegistry.resolve_context_window(vendor, model) -> int|None # async, may ask a local server
```

The HUD path and the auto-compaction check run on every turn and MUST NOT be able to block, so they
use the synchronous one (settings override → discovery cache → static table). Only the
once-per-turn `context` emission awaits the async one.

- **Discovery is attempted for `local` only.** The other ten vendors publish a fixed window the
  static table already carries; a round trip per turn to relearn a constant is pure latency.
- Ollama is asked over `POST /api/show` (not `/v1`, whose `GET /models` carries no window field);
  the base URL's trailing `/v1` is stripped. Any failure — unreachable, 404, no field — is `None`,
  never an exception.
- A single-row `/v1/models` listing is accepted **even when the id does not match**: a one-model
  vLLM or LM Studio server often does not echo back the configured alias, and with exactly one row
  there is no ambiguity.
- The cache is keyed `(vendor, base_url, model)` with `CACHE_TTL_SEC = 600`, matching
  `providers/models.py` so the two expire together without either file knowing about the other.
  `VendorPreset.context_window()` is the **static table only** — the preset is frozen and knows
  nothing about this machine's settings or endpoints.
- **AC-33.** An unrecognised model MUST resolve to `None` and render as `?`, never to a default.
  A wrong window silently truncates or silently never compacts; an honest unknown does neither.
  The table lists `claude-*` and `gpt-5` by prefix.
- The window is discovered at the **top** of a turn, never at the end: the turn-ending path uses
  `emit_context(..., discover=False)` so it is a single await. That path also runs while a turn is
  being cancelled, and a second await there could take the cancellation before `turn.done` is sent
  — `CancelledError` is a `BaseException`, so the `except Exception` guard would not catch it.

## 3. Token estimation

`chars/4` plus four tokens of per-message overhead. `tiktoken` is used instead when it happens to
be importable (cached once) but MUST NOT be added to `pyproject.toml`. The estimate MUST cover
tool-call arguments and tool results, not only `content` strings — a `read_file` result is
frequently the largest thing in the window, and counting only text would under-report exactly the
case the user cares about.

Provider-reported `input_tokens` is **adopted, never averaged** with the estimate — the vendor knows
what it billed. `Session.context_estimated` flips to `False` when a usage event arrives and back to
`True` after a compaction, because the old count no longer describes the new prompt.

## 4. Compaction (`session/compaction.py`)

**AC-34.** Auto-compaction runs at the **top of the agent loop, before the user message is
appended** — the only point that is both "between turns" and inside the code path every surface
shares. Compacting after the append would make the summariser describe a message the model has not
answered yet.

- `should_auto_compact` MUST refuse when the history is no longer than `context.keepLastMessages`
  (default 4). Otherwise a single enormous message compacts on every turn, summarises nothing (the
  head is empty) and grows the history by one system message each time.
- The summary is a **`role: "system"` message inside the history**. The Anthropic and Gemini
  adapters hoist every system message into the request's system field wherever it sits, and the
  OpenAI-compatible adapters pass it through, so one shape works for all eleven vendors with no new
  message role.
- **The verbatim tail is moved forward past orphaned tool results.** Keeping "the last N messages"
  literally can start the tail on a `tool` message whose assistant call has just been summarised
  away, which every vendor rejects. `split_index` walks the boundary forward, so `keepLastMessages`
  is a minimum-ish rather than an exact count and the event reports the `kept` count actually used.
- **A provider failure during compaction MUST NOT lose the conversation.** `summarise` catches
  `ProviderError` and anything else and `fallback_summary` writes a mechanical note (how many
  messages, which tools were used). Leaving an over-full window intact is worse than an imperfect
  summary; discarding the history would be worse still.
- `session.compact` returns `invalid_params` while `session.turn_task` is running. `/compact`
  cannot violate the "never compact mid-tool-loop" rule — it *is* the turn — but the RPC can be
  called at any moment.
- Compaction history is **not** written to the `messages` table by this path; the `compaction`
  event is persisted by `EventHub.emit` like every other event, so a resuming client gets the
  marker it needs for the `— compacted (12.3k → 2.1k tokens) —` divider without a second storage
  path. Rendering the divider from the event is the client's job.

## 5. Store lifetime and the shutdown race

A background task that starts *after* its store closed must not reach a closed
`sqlite3.Connection`. Both stores MUST implement the same three-part discipline:

**AC-35.** `memory/store.py` and `session/store.py` each carry an explicit `_closed` flag checked
under the same lock at the top of every read/write/delete, raising `MemoryClosed` / `StoreClosed`
(both `RuntimeError` subclasses) rather than touching the connection; `close()` is idempotent
(`session/store.py:151-153` shows the guard inside `delete_sessions`). A lock alone is not enough —
it serialises *concurrent* access and does nothing about a call that begins after `close()`.

**AC-36.** `Daemon.stop` MUST drain before it closes, in this order:

1. `core.stopping = True` — the very first thing, before the update watcher, before
   `lifecycle.stop()`. `nudge_after_turn` checks it and returns immediately without touching the
   store at all. `context_for_turn` does not check it: recall runs once at the very start of a
   turn's `_drive`, long before shutdown could plausibly race it, and it already treats every
   failure (including `MemoryClosed`) as `""`.
2. `SessionManager.close_all()` — cancel every live `turn_task` up front, then await them together
   (`asyncio.gather` under one 5s `asyncio.wait_for`, so a task stuck outside a cancellable await
   cannot block shutdown forever), then close each session normally while the session store is
   still open.
3. `await MemoryServices.close()` — now async: it cancels and awaits every task tracked via
   `MemoryServices._track()` **before** `store.close()`. A task cancelled mid-write inside
   `asyncio.to_thread` does not stop the underlying thread, so draining first is what keeps
   shutdown deterministic.
4. Only then: lifecycle, scheduler, gateway, sockets, session store, memory store.

Defense in depth on top of that: `EventHub.emit` and `SessionManager.close`'s `close_session` write
catch `StoreClosed` and drop the write at `debug`; `nudge_after_turn` / `context_for_turn` catch
`MemoryClosed` and `asyncio.CancelledError` explicitly ahead of their broad `except Exception`
(the cancellation belongs to the tracked child task, not to the awaiting coroutine, so swallowing
it suppresses no caller's cancellation). `run_turn` skips the final `turn.done` write on
`CancelledError` when `core.stopping` is set — a turn cancelled by `close_all` has nothing useful
left to tell a subscriber whose socket is being torn down in the same call. A turn cancelled for
any other reason still emits `turn.done{interrupted}`; `core.stopping` is the only thing this
branches on.

**Explicitly out of scope: turn resumption.** A turn cancelled by `close_all` is gone — the
in-flight tool-call/model round is not persisted or replayed on the next daemon start.
`session.list` and the session-store row correctly show the session as closed. "Resume where the
turn left off" would need a separate design.

## 6. Tests

- `tests/test_session_loop.py::test_stop_mid_turn_is_clean` (and `_repeated`, 5× over fresh homes)
  with `tests/fixtures/providers/fake/shutdown_race.json` (`delaySec: 2`): start the prompt, sleep
  until the turn is genuinely inside the provider delay, `daemon.stop()`, assert nothing raises and
  nothing logs at `ERROR`, then reopen `Store` on the same `$SNOWPEA_HOME` and confirm the row
  reads back with `closed_at` set — a fresh `Store.open` succeeding at all is the corruption check.
- `tests/test_memory_recall.py::test_close_during_a_delayed_nudge_is_clean[_repeated]` drives
  `MemoryServices`/`nudge_after_turn` directly rather than through a full daemon turn: an earlier
  version that stopped a real `Daemon` mid-turn reproduced the *session*-store race instead, which
  would have made the regression test misleading for the thing it pins down.
- Two tests MUST pass **unmodified** — `test_subagents.py::test_child_events_flow_on_the_child_session`
  and `test_session_loop.py::test_resume_returns_events_after_seq`. That they do is the check that
  `context` precedes `turn.done` rather than trailing it.
- `FakeProvider` reports the whole prompt as `input_tokens`, not just the last user message. No
  vendor counts only the final message, and the old behaviour made the reported context flat across
  a growing conversation.
```

---

## `docs/design/m12-tui-contract.md`

```markdown
# M12 Contract — Terminal UI Surface (binding)

The TUI is a **client**, not a second copy of the daemon: everything here is surface-local
behaviour on top of the protocol. Where this document and `docs/protocol.md` disagree, the protocol
wins. Builds on M1 §13 (TUI deviations, US-008) and M1 §11 (SDK). Source of record: `tui/src/`.

Acceptance criteria: **AC-37 … AC-42**.

## 1. Layout

Two layouts, one component tree. Inline (default) writes into the terminal's scrollback and keeps
it; `--fullscreen` opts into the alternate-buffer layout (`tui/src/components/FullscreenLayout.tsx`,
`tui/src/app.tsx:13`, `:162-165`). `--no-fullscreen` and `SNOWPEA_TUI_INLINE=1` force inline.
Everything in the fullscreen branch MUST be inert while inline (`app.tsx:689`), so the two layouts
cannot drift into two behaviours.

Regions, top to bottom: transcript → (update banner) → bottom panels → status HUD.

**AC-37.** The persistent bottom regions MUST be separated by full-width rules
(`components/SectionRule.tsx`): one terminal-width `─` run, optionally ending in a label, truncated
to the width so it can never wrap and shift the layout by a row. A bottom region without a rule
above it is a layout bug — the rules are what make the regions readable as regions rather than as
one run-on block.

## 2. Modes and the HUD

`StatusHud.tsx` renders, in order: version, model, **mode**, context, session, daemon, approvals.
Mode is shown **before** status, not after.

Mode changes are local key gestures that call `session.setMode`:
- `⇧Tab` cycles `accept → auto → plan`;
- `Ctrl+P` toggles plan mode;
- `/plan`, `/accept`, `/auto`, `/mode` remain the protocol path and MUST keep working identically.

Context is rendered from the `context` session event (M11 §1): `used/window (percent)`, and `?`
for a null window.

## 3. Panels

| component | opened by | contract |
|---|---|---|
| `HelpPanel` | `F1` | §4 |
| `AgentPanel` / `AgentTranscript` | `Ctrl+A` | subagents under their parent, collapsed when idle and labelled as such; the **session agent** is visually distinct from the **active team** |
| `ApprovalQueue` | always visible when non-empty | advisory; a failure MUST NOT block the session |
| `ApprovalPrompt` | server `approval.request` | `y`/`n` + scope resolves the promise; origin surface only |
| `DiffView` / `ToolCall` / `ToolSummary` | `Ctrl+O` expands the newest | — |
| `UpdateBanner` | `system.checkUpdate` says available, or `U`, or `/update` | y/n confirm |
| `AttachmentChips` | paste / drag / clipboard image | one chip per pending attachment |

## 4. Help panel

**AC-38.** The help panel MUST be dismissible and MUST have a bounded height — it must never
expand into terminal scrollback (`components/HelpPanel.tsx:1`). `Esc`, `F1`, `q` and `Enter` all
close it; `↑↓` / `PgUp` / `PgDn` scroll within the bound.

It lists the workflow commands from one constant
(`WORKFLOW_COMMANDS = ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan,
accept, auto`, `HelpPanel.tsx:8-18`) and the key table, which MUST document at minimum:

```
Esc / F1 / q / Enter close help · ↑↓ / PgUp / PgDn scroll
$agent-name task delegates directly to a member of the active team
Ctrl+C quit · Esc outside help interrupts the current turn
Ctrl+O expand the newest tool call or diff · Ctrl+A open the agent panel
⇧Tab cycles accept -> auto -> plan · Ctrl+P toggles plan mode
```

F1 is read from raw terminal escape sequences, because Ink 5 removes it from `useInput`
(`app.tsx:527`).

## 5. Slash commands

**AC-39.** The command table MUST NOT be hard-coded. `tui/src/slash/registry.ts` takes
`command.list` from the daemon and merges in only the **surface-only** commands — the ones the
daemon cannot implement because they are about this client's own session selection
(`registry.ts:10-26`):

| command | summary |
|---|---|
| `/resume` | Resume the last session in this directory, or `/resume <sessionId>`. |
| `/sessions` | List saved sessions and choose one to resume. |
| `/session` | Delete saved sessions: `/session delete <id>` \| `clear [--all]`. |

A surface command is dropped from the merge if the daemon already advertises that name, so a future
core `command.list` entry silently takes over. `/update` is the one deliberate interception of a
core command (`app.tsx`): it opens the same y/n banner the `U` key opens, rather than forwarding to
`command.run`, because the task requires a confirmation in this one surface. Everything else
starting with `/` goes straight to `command.run`.

## 6. Resume picker and saved sessions

**AC-40.** `/sessions` calls `session.list(includeClosed: true, workdir: <cwd>)` and renders a
picker; choosing a row calls `session.resume(sessionId)`, which restores a persisted session even
after a daemon restart (M1 §17-3). Each row MUST be identifiable without the user remembering an
id — it shows the workdir, the creation time and `lastPrompt`, the session's last user message
(M1 §17-2). Rows arrive already sorted newest-first; the picker MUST NOT re-sort them.

`/resume` with no argument resumes the newest row for the current directory. A failed listing is an
inline error message, never a crash (`app.tsx:1027`).

`/session delete <id>` and `/session clear [--all]` map onto `session.deleteSaved`
(`{sessionId}` / `{workdir: cwd}` / `{all: true}`). The daemon refuses to delete a live session, so
the picker MUST NOT filter live rows out on its own — a row that fails to delete is a row that is
still running, and that is what the user should be told.

## 7. Short delegation

**AC-41.** `$agent-name <task>` in the input line delegates directly, matched by
`/^\$([A-Za-z0-9._-]+)\s+([\s\S]+)$/` and dispatched as `agent.spawn{name, task}`
(`app.tsx:1036-1041`) rather than as a `session.prompt`. The daemon does not know the input came
from a shortcut, so the M6/M7 §3.1 refusal rules apply unchanged: an unknown name, or a non-member
while a team is active, comes back as a refused `subagent.done` and MUST be rendered as an error in
the agent panel, not swallowed.

The agent panel distinguishes the **session's own agent** from the **active team** row that
`agent.list` prepends (`kind: "team"`); a `kind` the client does not recognise MUST be rendered as
an ordinary row rather than dropped.

## 8. Input line, draft and queued prompts

The input line is implemented directly with `useInput` (Ink 5 ships no text-input component and a
bundle dependency was not worth it). It MUST support an **editable draft with a movable cursor** —
left/right, word motion, home/end and mid-string insertion — not append-only typing. `↑/↓` walk
history when the palette is closed and select candidates when it is open; `Tab` completes a
command; `Enter` sends; `Esc` interrupts the current turn; `Ctrl+C` quits.

**AC-42.** Submitting while a turn is running MUST be accepted, not blocked, and MUST show a
**queued-prompt indicator**: the daemon queues it (M9 §5) and answers with a `turnId` whose
`turn.done` arrives later, so the user needs to see that their text was taken and is waiting. Two
consequences the surface MUST render honestly:

- `Esc` cancels only the turn in flight; the queue keeps draining afterwards. The indicator must
  not read as "cancelled" after an interrupt.
- A daemon restart loses the queue. Pending indicators MUST be cleared on `disconnected`, not left
  waiting for a `turn.done` that will never come.

Attachments are captured when the prompt is submitted, not when the turn starts, so the chips
belong to the queued entry rather than to the input line.

## 9. Reconnection and event hygiene

Connection state (connected / reconnecting / closed) is derived from the SDK's lifecycle events
`disconnected{code, reason, willRetry}` and `reconnected{attempt, resumedSessions}`, not from a
protocol notification. The SDK performs the `session.resume(afterSeq)` replay; the TUI only
displays it. `tui/src/rpc/client.ts` additionally tracks the highest `seq` per session and drops
anything at or below it — the SDK already de-duplicates, so this is redundant, and it stays as a
cheap insurance against drawing an event twice.

`tui/src/rpc/sdk.ts` MUST **derive** `SessionEvent`, `ApprovalRequestParams`, `Mode` and
`CommandInfo` from `@snowpea/sdk`'s generated types rather than re-declaring them, so a protocol
regeneration cannot drift silently from the TUI.

## 10. Exit codes

`0` normal, `1` daemon connection or session-create failure, `2` argument error, `75`
(`RESTART_EXIT_CODE`, EX_TEMPFAIL) requests that `cli/main.launch_tui` wait for the old daemon's
pid to disappear and `os.execv` the same argv. `75` is unused by every other snowpea exit path.

## 11. Tests

The suite under `tui/test/` is the contract's executable half and MUST cover at least:
`help-ui`, `resume`, `direct-delegate`, `section-rule`, `mode-cycle`, `palette`, `slash`, `hud`,
`bottom`, `layout`, `frame`, `flicker`, `panel`, `approval`, `attach-ui`, `attachments`,
`update-ui`, `voice`, `markdown` (Unicode table alignment), `history`, `store`, `rpc-client`.
```

---

### 3c. Stale statements to fix

### README.md

| loc | current → proposed |
|---|---|
| `README.md:264` | `- Protocol is \`0.1.0\`; the v1.0 freeze gate applies before the v0.2 IDE.` → `- Protocol is \`1.3.0\`; the v1.0 freeze gate (three consecutive releases with no generated-schema change) applies before the v0.2 IDE.` **Verified:** `core/snowpea_core/server/protocol.py:23` `PROTOCOL_VERSION = "1.3.0"`; `docs/protocol.md:5` already says `1.3.0`. |
| `README.md:258` | `## Known limitations (v0.1)` → `## Known limitations (v0.1.7)` — the list is version-pinned prose and `core/snowpea_core/__init__.py:7` reads `__version__ = "0.1.7"`. |
| `README.md:258-264` (section) | **ADD** two limitations that are real at HEAD and currently undocumented: `- **Interrupt does not clear the prompt queue.** \`session.interrupt\` cancels the turn in flight; prompts queued behind it still run (\`server/session_handlers.py:344-349\`), and a daemon restart loses them entirely (\`session/session.py:65\` — in-memory only).` and `- **Deleting a saved session leaves its attachments.** \`session.deleteSaved\` removes the messages, events and session rows (\`session/store.py:146-165\`) but not \`<SNOWPEA_HOME>/attachments/<sessionId>/\`.` |
| `README.md:243` | `- **v0.1 — this repository.** Core daemon, protocol, TUI, SDK, eleven vendors, tools, memory, scheduler, gateway, plugins, subagents and team mode, installers for three platforms.` → add the shipped-since items: `…subagents, project teams and team mode, attachments and voice I/O, context tracking and compaction, model profiles, in-app updates, installers for three platforms.` |
| `README.md:27` | intro paragraph lists capabilities but omits voice/attachments and context compaction, both of which are user-visible headline features at HEAD (`server/audio_handlers.py:47-53`, `session/compaction.py`). Suggest appending one clause: `…a cron scheduler, voice in and out, and Telegram/Discord/Slack gateways sit behind one command`. |

### docs/design/m1-core-contract.md

| loc | current → proposed |
|---|---|
| `m1:15` | `PROTOCOL_VERSION = "0.1.0"          # semver; M8에서 1.0.0` → `PROTOCOL_VERSION = "1.3.0"          # semver; 추가 변경마다 minor. v1.0 freeze gate는 별도` (**contradicted by** `server/protocol.py:23`). Covered by delta **A2**. |
| `m1:34` | `\| session.list \| – \| sessions: list[SessionSummary] \|` → the two-row replacement in delta **A1**. `session.list` has taken `SessionListParams` since `server/protocol.py:300-302`. |
| `m1:34` (table, missing row) | no `session.deleteSaved` row → add it (delta **A1**); the method is registered at `server/protocol.py:1560` and `:1818`. |
| `m1:53` | the `session.event.kind` enumeration omits `context`, `compaction` and `audio.spoken` → append them; see `server/protocol.py:1309`, `:1363` and CORE-context's event shapes. |
| `m1:131-136` | the five-column permission matrix → the six-column one in delta **A3**. `config` exists (`PROTOCOL_VERSION` 1.3.0, `tools/config_guard.py`) and the table as written says auto-mode `write` is a silent allow with no `config` row at all. |
| `m1:148-149` | `tool_call → policy.decide → deny: event error{mode_denied} + turn.done{denied}` → the continue-and-adapt flow in delta **A4**. **Contradicted by** `agent/loop.py:46` (`MAX_DENIALS_PER_TURN = 3`) and `:330`. |
| `m1:151-152` | `done(no tool calls) → message.done → usage → turn.done{complete}` → `… → message.done → (audio.spoken) → context → turn.done{complete}` — `finish_turn` emits `context` immediately before `turn.done` (`agent/loop.py:63-91`). |
| `m1:88` | `async def create(...)->Session; get(id); list(); async close(id)` → add `async restore(id, *, origin_conn=None) -> Session|None` and `async close_all()` (`session/manager.py:126-165`; `Daemon.stop` calls `close_all`). |
| `m1:86` | `class Session: id, workdir, mode, provider, model, origin_surface, created_at, history, seq` → add `team, team_agents, queued_turns, context_used, context_estimated, context_window, backend, allowed_tools, turn_task, current_turn` (`session/session.py:55-76`). |
| `m1:94` | `이벤트는 store에 append 되어 session.resume(afterSeq)가 재전송한다.` → add: `히스토리는 매 턴 finish_turn에서 store.replace_messages로 증분 저장되므로 데몬 재시작 뒤에도 session.resume이 대화를 복원한다 (agent/loop.py:64-73).` |

### docs/design/m2-tools-contract.md

| loc | current → proposed |
|---|---|
| `m2:48` | `… `exa_free`(no key) → `keenable_free`(no key) → `parallel_free`(no key) → `tavily`(key optional) …` → `keenable_free`(key) → `parallel_free`(key) → `tavily`(key). All three answer `401` keyless; `firecrawl` is `key optional`. Full replacement in delta **B1**. |
| `m2:48` | `Only \`ddgs\`, \`brave_free\`, \`tavily\`, \`searxng\` need real HTTP implementations in M2; the others may be thin HTTP clients…` → **withdraw**: every catalog id now has a real client and `ThinProvider` is retired from the catalog; `test_every_catalog_id_has_a_real_client` pins it. This sentence is the licence that produced the CORE-search-fix bug. |
| `m2:48` | `\`web_search\` uses \`settings.search.provider\` then falls back down the free chain on failure (log which).` → `…falls back down \`FREE_CHAIN\` (\`tools/search_providers/__init__.py:74-82\`) and MUST report the fallback in three places (ToolResult.meta, a \`[search via … — fallback from …]\` text prefix, and one per-session \`error{code:"search_provider_unavailable"}\` event).` "log which" is no longer sufficient. |
| `m2:24-34` (catalog table) | missing the `audio` row and still lists `delegate`/`memory`/`schedule` as stubs → delta **B2**. `text_to_speech` under `media` is stale: `tools/audio_tools.py` owns that name now and `media.py` registers no speech tool. |
| `m2:23` | `| category | tools | permission |` header — `permission` is now potentially per-call (`Tool.permission_for`); add a note that the column is the *declared* tag. |

### docs/design/m3-providers-setup-contract.md

| loc | current → proposed |
|---|---|
| `m3:14` | `auth_methods: tuple[str, ...]        # ("api_key",) | ("api_key","device_code") | ("api_key","oauth_pkce")` → `# ("api_key",) | ("api_key","device_code","oauth_token") | ("api_key","oauth_pkce") | ("api_key","google_adc","oauth_token")` (`providers/presets.py:120`, `:140`). |
| `m3:22` | `Exactly \`openai\` has \`device_code\`, exactly \`openrouter\` has \`oauth_pkce\`.` → the per-preset table in delta **C1**. Stale: `gemini` now has `google_adc`, and both `openai` and `gemini` have `oauth_token`. |
| `m3:30-34` (§3) | lists only two flows and describes `loginWeb` as blocking → full replacement in delta **C2**. Missing: `google_adc` (`providers/auth_web.py:516-583`), the remote `oauth_token` path, the `LoginStart`/`finish()` split and `provider.loginProgress` (CORE-login-progress), and `snowpea provider login` (`cli/commands.py:271-313`). |
| `m3` (nowhere) | `provider.configure`'s accepted key set is undocumented → delta **C2** final paragraph (`server/app_server.py:417-438`). |
| `m3:44` | setup screen list omits the authentication prompt → delta **C3** (`setup/wizard.py:251-281`). |
| `m3:45` | `Output: \`$SNOWPEA_HOME/settings.json\` with \`providers\`, \`search.provider\`, \`browser.provider\`, \`tools.enabled_categories\`, \`gateway\`.` → add `audio`, `models`, `agents.teams`/`agents.default_team` (`config/settings.py:170-177`, `:42-69`). |
| `m3:28` | `\`providers/registry.py.get(vendor, model)\` resolves: SNOWPEA_PROVIDER → session/provider arg → settings.providers.default → first configured vendor.` → insert the model-profile layer: `…session/provider arg → config/model_routing.route_for() (agents.models[agent] → definition \`model:\` → models.default) → settings.providers.default → first configured vendor.` (`config/model_routing.py:18-44`). |

### docs/design/m5-memory-scheduler-gateway-contract.md

| loc | current → proposed |
|---|---|
| `m5:22` | `class Job(BaseModel): id, spec, kind, next_run, task, mode, channel, agent, enabled, last_run, last_status, created_at` → the field list in delta **D1**; `origin_session_id`, `cron`, `interval_sec`, `workdir` and `state` are all real (`scheduler/jobs.py:97-116`). |
| `m5:29` | `…the final assistant text is delivered to \`channel\` via the gateway router (\`telegram:<chat_id>\` \| … \| \`log\`)` → delta **D2**: the originating session is tried **first**, as a `message.done` event prefixed `⏰ Scheduled reminder (<jobId>)`, and an explicit channel is additive rather than a replacement (`scheduler/scheduler.py:341-374`). |
| `m5` (nowhere) | the `origin_session_id` online migration is undocumented → delta **D2** (`scheduler/jobs.py:237-241`). |
| `m5` (nowhere) | `gateway.sync` and settings-driven catch-all bindings are undocumented despite being in §3's subject area → add a `### 3.1 Settings-driven bindings` noting: `gateway_bindings.source` column added by an in-place `ALTER TABLE` defaulting to `'manual'`; the auto binding's `credentials_ref` is the platform name; an enabled platform with no token is a **warning, not an error**; `settings.set` triggers a sync that can never fail the write; `GatewaySyncResult` reports `added`/`removed`/`kept` **by platform**, not by binding id; `snowpea setup` calls `gateway.sync` only when a daemon is already running. |
| `m5:32` | `Lifecycle: \`Lifecycle\` counters \`jobs\` = enabled jobs … \`snowpea daemon status\` prints \`will exit in Ns\`…` → add: `…and one \`messengers\` line built from \`gateway.list\`, naming each platform as \`listening\` or \`stopped\`, wrapped in \`contextlib.suppress(RpcCallError)\` so an older daemon still reports everything else.` |

### docs/design/m6-m7-skills-agents-contract.md

| loc | current → proposed |
|---|---|
| `m6-m7:23` | §3 describes `delegate_task(task, agent?, …)` with **no statement about an unresolvable name**, which is how the old "silently runs with the parent's settings" behaviour was permitted → delta **E2**. This is the breaking change and it MUST be stated: `agent/subagent.py:397-399` + `_refuse` at `:430-443`. |
| `m6-m7:6-9` | search roots list `built-ins: core/snowpea_core/builtin_skills/<name>/SKILL.md` only; it does not mention that **agent definitions** also have a builtin tier → add `core/snowpea_core/prompts/roles/*.md` as `source="builtin"` agent definitions (`agent/definition.py:289-305`), overridable by global then project. Delta **E1**. |
| `m6-m7:20` | `\`agents/<name>.md\` frontmatter: … body = system prompt.` → add that the body is **appended as a persona after the role file**, not substituted for `BASE_PROMPT` (delta **E1**). As written it implies replacement, which is the bug CORE-prompts fixed. |
| `m6-m7:34` | §5 documents `/team N <task>` only → the four configuration subcommands exist (`commands/team_cmd.py:22-23`, `:42`, `:88-140`). Delta **E3**. |
| `m6-m7:23` | `\`agent.list\` includes \`kind: subagent\|named\` and \`status\`` → `…\`kind: definition\|subagent\|named\|team\`; when a team is active the definition rows are filtered to its members and a synthetic \`kind:"team"\` row is prepended (\`server/agent_handlers.py:83-107\`).` |
| `m6-m7:30` | `\`/help\` lists ralph, ralplan, ultrawork, deepinit, deep-research, deep-interview, plan, accept, auto (+ others).` → matches `tui/src/components/HelpPanel.tsx:8-18`; **no change needed**, but cross-reference `docs/design/m12-tui-contract.md` §4 so the two lists stay pinned to one constant. |

### docs/design/m8-packaging-contract.md

| loc | current → proposed |
|---|---|
| `m8:19` | `Bump \`PROTOCOL_VERSION\` to \`1.0.0\` in the v0.1.0 release commit (freeze baseline, plan §4.9).` → withdrawn; see delta **F1**. Directly contradicted by `server/protocol.py:23` (`1.3.0` at v0.1.7). |
| `m8:7` | `\`snowpea --version\` prints \`snowpea 0.1.x\`; version is single-sourced from \`core/snowpea_core/__init__.py\`` → still true (`__init__.py:7` = `0.1.7`); add that `update.installed_cli_version()` shells exactly this command after an upgrade to report what landed (`update.py:658`). |
| `m8` (no §7) | the whole update subsystem — `install.json`, PEP 610 provenance, the downgrade guard, `system.checkUpdate`/`update`/`restart` — is undocumented in any contract → delta **F2**. |
| `m8:10` | `installer/install.sh` bullet does not mention `record_install` or the `[images]` extra → append: `…and records the resolved source in \`$SNOWPEA_HOME/install.json\` (\`record_install\`; \`Record-Install\` in install.ps1), requesting the \`[images]\` extra in whichever of the two spellings the source takes (\`snowpea-agent[images] @ git+https://…\` for a URL, \`/path[images]\` for a path), so \`snowpea update\` repeats it with no second flag.` |

### docs/manual/

| loc | current → proposed |
|---|---|
| `docs/manual/en/protocol.md:44` | `\| session \| \`session.create\`, \`session.list\`, \`session.resume\`, \`session.close\`, \`session.prompt\`, \`session.interrupt\`, \`session.setMode\` \|` → add `\`session.deleteSaved\`` and `\`session.compact\`` (`server/protocol.py:1560`, and `session.compact` registered in `session_handlers.py:548`). Same edit in the `ko`/`zh-CN` copies of that table if they exist. |

### Checked and found **not** stale (recorded so they are not "fixed" by mistake)

- `README.md:111-119` — the vendor table already documents Gemini's `gcloud` ADC and remote access token, and OpenAI's device-code/access-token paths. Correct at HEAD.
- `README.md:259` — the ChatGPT/Codex-subscription note on device-code login matches commit `8d3ca31` and `providers/auth_web.py`'s `ENDPOINTS`. Correct.
- `docs/manual/en/setup.md:139`, `:150`, `:162` — already describe `exa_free` as the keyless hosted MCP at `https://mcp.exa.ai/mcp` and `web_search`'s `exa_free → ddgs` provider string. Correct; only the **m2 contract** lags.
- `docs/protocol.md:5` — `1.3.0`, generated and in sync (`scripts/gen_protocol.py --check` is a CI gate).

---

## 4. Cross-surface gap matrix

Every ✅/❌ below is backed by a grep hit or its documented absence. The private IDE at
`/mnt/data/work/mediagen/snowpea-ide` consumes the same protocol (`docs/design/ide-contract.md`).

Legend: ✅ present · ❌ absent · partial · n/a (not applicable to that surface)

| Capability | TUI | IDE | Headless CLI | SDK types | Manuals (en/ko/ja/es/zh-CN) |
|---|---|---|---|---|---|
| **A** Built-in roles by name, `source="builtin"` | ✅ | ✅ | ✅ | ✅ | ✅/✅/❌/❌/❌ |
| **B** Per-agent model profiles (`models.default`, `models.profiles`, `agents.models`) | ❌ | ❌ | partial | n/a | ✅/✅/❌/❌/❌ |
| **C** Project teams + `/team create\|use\|list\|delete` + `kind:"team"` row | ✅ | ❌ (mis-bucketed → GAP-1) | ❌ | partial | ✅/✅/❌/❌/❌ |
| **D** `$agent-name task` short delegation | ✅ | ❌ | ❌ | n/a | ✅/✅/❌/❌/❌ |
| **E** Unknown agent name REFUSED (breaking) | ✅ (error surfaced) | partial | n/a | n/a | ✅/✅/❌/❌/❌ |
| **F** Resume across daemon restarts | ✅ | partial | ❌ | ✅ | partial/partial/❌/❌/❌ |
| **G** `session.list{includeClosed,workdir}` + `lastPrompt` + picker | ✅ | ❌ | ❌ | ✅ | ❌/❌/❌/❌/❌ |
| **H** `session.deleteSaved` / `/session delete\|clear` | ✅ | ❌ | ❌ | ✅ | ❌/❌/❌/❌/❌ |
| **I** Queued prompts during an active turn | ❌ | ❌ | ❌ | ❌ (no event type) | ❌/❌/❌/❌/❌ |
| **J** Update safety (git tracking, downgrade guard, forced check, "already up to date", branch release version, input disabled) | ✅ | ❌ | ✅ | ✅ | ✅/✅/❌/❌/❌ |
| **K** Help panel dismissible / bounded / scrollable | ✅ | n/a | n/a | n/a | ✅/✅/❌/❌/❌ |
| **L** Full-width section rules + `Team: <name>` label | ✅ | n/a | n/a | n/a | ✅/✅/❌/❌/❌ |
| **M** Hosted Exa MCP, keyless (`exa_free`) | n/a | partial | ✅ | n/a | ✅/✅/✅/✅/✅ |
| **N** Gemini OAuth (`google_adc`) + `oauth_token`/`auth_method` + `provider login --token` | n/a | ❌ | ✅ | n/a (opaque config) | ✅/✅/❌/❌/❌ |
| **O** OAuth choices in the setup provider wizard | n/a | partial (**broken**) | ✅ | n/a | ✅/✅/❌/❌/❌ |
| **P** Scheduler reminders into origin session (`originSessionId`, "⏰ Scheduled reminder") | ✅ (implicit) | partial | partial | ✅ | ❌/❌/❌/❌/❌ |
| **Q** Approval `note` field | ✅ | ❌ | ✅ | ✅ | ❌/❌/❌/❌/❌ |
| **R** Mode picker on Enter at status row; mode-before-status ordering | ✅ | ✅ (ModeBar) | n/a | ✅ | ❌/❌/❌/❌/❌ |
| **S** Editable draft w/ cursor + history-draft preservation; `/sessions` | ✅ | n/a | n/a | n/a | ❌/❌/❌/❌/❌ |

**Evidence anchors (core is the baseline):**
`A` `agent/definition.py:289`; `config/settings.py:28-36` · `B` `config/settings.py:42-49`,
`config/model_routing.py:40-54`, `setup/state.py:272-297` · `C` `agent/team_config.py:18-26`,
`config/project.py:45`, `commands/team_cmd.py:24-25,98-133`, `server/agent_handlers.py:85-87,100-108`
· `E` `agent/subagent.py:399` · `G/H` `server/protocol.py:277,301`,
`server/session_handlers.py:217,248,544` · `I` `agent/loop.py:105-125` — **no event is emitted at
enqueue time**; no `turn.queued`/`prompt.queued` kind exists in `session/events.py` or
`server/protocol.py` · `P` `scheduler/scheduler.py:371`, `server/protocol.py:840` ·
`Q` `server/protocol.py:504` `ApprovalRequest.note`, `permissions/approval_queue.py:181-202`.

### 4.1 IDE: capabilities with no renderer UI at all

Verified by the absence of any grep hit under `/mnt/data/work/mediagen/snowpea-ide/src`:

- **B** model profiles — zero hits for `models.profiles` / `agents.models` / `default_team`.
  `Settings.vue` has five sections and none is models/agents.
- **C** project teams — `renderer/stores/teams.ts` is the **worktree team run**
  (`team.start`/`team.status`), a different concept entirely. No `/team create|use|list|delete`,
  no `agents.teams`, no `activeTeam`.
- **D** `$agent-name` — no `$`-prefix parsing in `renderer/stores/composer.ts`.
- **G** — `sessions.ts:535` and `teams.ts:265` both call `session.list` with `{}`.
- **H** — `session.deleteSaved`: zero hits anywhere in the IDE.
- **I** — nothing (core emits nothing either).
- **J** — `system.checkUpdate` / `system.update`: **zero hits in the entire IDE `src`.**
  The IDE cannot check for or trigger an update at all.
- **N** — `renderer/stores/settings.ts:79` hard-codes `WEB_AUTH_METHODS = ["device_code","oauth_pkce"]`,
  filtering out `google_adc`; `providerConfig` (`settings.ts:88-96`) has no `oauth_token`/`auth_method`.
- **P** — `Schedule.vue`/`jobs.ts` never read `originSessionId`.
- **Q** — `renderer/stores/approvals.ts:37,89` captures `risk` but never `note`;
  `ApprovalModal.vue` renders `risk`/`scopeHint` only, so the "modifies snowpea configuration"
  warning is silently dropped from the approval dialog.

### 4.2 IDE: silently broken by the breaking change (E) and the `session.list` param change

1. `renderer/stores/agents.ts:91` does `const kind = row.kind ?? "definition"`, handles only
   `"subagent"` and `"named"`, and falls through to `definitions.push(...)`. A `kind:"team"` row
   therefore renders as a **spawnable** `AgentCard`; pressing Spawn calls `agent.spawn` with the
   **team** name, which core now refuses (`subagent.py:399`). The user sees an opaque RPC error.
2. With a project team active, core filters `agent.list` down to team members
   (`agent_handlers.py:85-87`). The IDE shows the reduced list with no explanation of why
   definitions vanished.
3. Because the IDE never passes `includeClosed`, its Threads view can never resume a session the
   daemon restarted — it only ever sees live sessions.

### 4.3 Concrete gap items

#### P1 — protocol/behaviour mismatch or data-loss risk

- **GAP-1 [IDE]** `/mnt/data/work/mediagen/snowpea-ide/src/renderer/stores/agents.ts` — in
  `normalizeAgents` (line 91) add an explicit `if (kind === "team")` branch before the definition
  fallthrough. Expose it as a new `activeTeam` field on the store so `Agents.vue` renders it as a
  **non-spawnable** badge; never push it into `definitions`.
- **GAP-2 [IDE]** `.../renderer/stores/agents.ts` — in the `spawn` action (~line 252) catch the
  `INVALID_PARAMS` refusal and surface the daemon's `unknown agent '<name>'` message as a toast.
- **GAP-3 [IDE]** `.../renderer/stores/approvals.ts` — add `note: string | null` to the approval
  record (line 37), populate from `params.note` (line 89), and render it in
  `.../renderer/components/ApprovalModal.vue` (~line 48) next to the `risk` chip, matching
  `tui/src/components/ApprovalPrompt.tsx:81-84` and `core/snowpea_core/cli/main.py:246-247`.
- **GAP-4 [IDE]** `.../renderer/stores/setup.ts` — `webLoginMethods()` (line 352) returns every
  non-`api_key` method and `VendorAuth.vue:57` routes each to `setup.loginWeb(method)`. But
  `oauth_token` is **not** a `provider.loginWeb` flow — it needs a text field written through
  `provider.configure({oauth_token, auth_method})`. Split the list:
  `device_code`/`oauth_pkce`/`google_adc` → `loginWeb`; `oauth_token` → password input feeding the
  existing `provider.configure` call at line 587.
- **GAP-5 [core]** `/mnt/data/work/mediagen/snowpea/core/snowpea_core/agent/loop.py` — at line 121
  (`session.queued_turns.append(queued)`) emit a session event (e.g. `turn.queued` with
  `{turnId, position}`) plus a matching drain event, so any surface can show "1 prompt queued".
  See **R5**.
- **GAP-6 [SDK types]** `/mnt/data/work/mediagen/snowpea/sdk/src/protocol.ts` — add the GAP-5 event
  kind to `EventPayloadMap` (~line 1699) and the event-kind union (~line 1720). Also fix the `kind`
  doc on `AgentListResult.agents[]` (line 110), which reads "definition = …, subagent = …,
  named = …" and omits the `team` value core now emits (`agent_handlers.py:105`) — a typed consumer
  has no way to know `team` exists. *(Note: `protocol.ts` is generated — fix the docstring in
  `core/snowpea_core/server/protocol.py` and re-run `scripts/gen_protocol.py`.)*
- **GAP-6b [core]** add `"oauth_token"` to `config/patch.py:15-17` and
  `server/settings_handlers.py:55`, and chmod `settings.json` to `0o600` in
  `config/settings.py:299-303`. See **R1**/**R2**.
- **GAP-6c [core]** clear `session.queued_turns` in `server/session_handlers.py:344-348`, and delete
  `<home>/attachments/<session_id>/` in `session_delete_saved_handler`. See **R3**/**R4**.

#### P2 — missing feature parity

- **GAP-7 [IDE]** `.../renderer/stores/sessions.ts` — change the `session.list` call at line 535 to
  `{ includeClosed: true, workdir }` and read `row.lastPrompt` into the thread record, mirroring
  `tui/src/app.tsx:1012-1018`. Add a "Saved sessions" picker to `.../renderer/views/Threads.vue`.
- **GAP-8 [IDE]** `.../renderer/stores/sessions.ts` + `.../renderer/components/ThreadMenu.vue` — add
  a `deleteSaved(sessionId?)` action calling `session.deleteSaved` and "Delete saved session" /
  "Clear saved sessions for this project" menu entries.
- **GAP-9 [IDE]** new "Models" section in `.../renderer/views/Settings.vue` reading/writing
  `models.default`, `models.profiles.<id>` and `agents.models.<agent>` through the existing
  `settings.get`/`settings.set` path in `renderer/stores/settings.ts`.
- **GAP-10 [IDE]** new "Teams" section in `.../renderer/views/Agents.vue` listing global
  `agents.teams`/`default_team` and project `agents.teams`/`activeTeam`, with create/use/delete
  wired to `command.run` on `/team`. Must **not** reuse `renderer/stores/teams.ts` (worktree board).
- **GAP-11 [IDE]** `.../renderer/stores/composer.ts` — recognise `^\$([A-Za-z0-9._-]+)\s+(.+)$` and
  route to `agent.spawn({sessionId, name, task})`, mirroring `tui/src/app.tsx:1034-1042`.
- **GAP-12 [IDE]** `.../renderer/stores/settings.ts` — extend `WEB_AUTH_METHODS` (line 79) with
  `google_adc`; add `oauth_token` + `auth_method` to the `providerConfig` draft (lines 88-96).
- **GAP-13 [IDE]** no update surface exists at all. Add a `system.checkUpdate`/`system.update`
  caller to `.../renderer/stores/core.ts` plus a banner/confirm flow honouring
  `CheckUpdateResult.channel`, `latest`, `releaseUrl`, `error` (`server/protocol.py:151-169`).
- **GAP-14 [Headless CLI]** `core/snowpea_core/cli/commands.py` — the `session` subparser
  (line 1156) has only `context` and `compact`. Add `session list [--include-closed] [--workdir DIR]`
  (printing `lastPrompt`) and `session delete <id>` / `session clear [--all]` calling
  `session.deleteSaved`. Saved sessions currently accumulate with no shell-side way to inspect or purge.
- **GAP-15 [Headless CLI]** `core/snowpea_core/cli/main.py` — there is **no `--resume <sessionId>`
  (or `-c`/`--continue`) flag** on the prompt path; a headless run cannot continue a persisted
  session the way the TUI's `/resume` does. Add it alongside `--cwd`/`--provider` (lines 102-106)
  and call `session.resume` before `session.prompt`.
- **GAP-16 [Headless CLI]** `core/snowpea_core/cli/commands.py` — the `team` subparser (line 1344)
  exposes only `status` (the worktree run). Add `team create|use|list|delete` so project teams are
  configurable without a TUI.
- **GAP-17 [Headless CLI]** `core/snowpea_core/cli/commands.py` — nothing reads or writes
  `models.profiles` / `agents.models`; they are reachable only through the interactive wizard
  (`setup/state.py:286-297`). Add `snowpea model profiles [--json]` and
  `snowpea model assign <agent> <profileId>`.
- **GAP-18 [IDE]** `.../renderer/views/Schedule.vue` — surface `JobInfo.originSessionId` as a link
  to the owning thread, so a user can see where a `⏰ Scheduled reminder` will be delivered.

#### P3 — docs / translation

**Structural finding:** `ja/`, `es/` and `zh-CN/` contain only `commands.md`, `install.md`,
`setup.md`, `tui.md`. They are missing `backends.md`, `gateway.md`, `headless.md`, `index.md`,
`modes.md`, `plugins.md`, `protocol.md`, `scheduler.md`, `voice.md`, all of which `en/` and `ko/`
have. Their `tui.md` is frozen at **253 lines** vs **285** for en/ko; `setup.md` at **141** vs
**201/199**; `commands.md` at **174** vs **194**. Everything after line 253 of `tui.md` —
Markdown tables, built-in agents, update notifications, help panel, section rules, teams and short
delegation — is untranslated in all three.

- **GAP-19 [Manuals]** `docs/manual/en/tui.md` — nothing documents the **saved-session picker**,
  `/sessions`, `/session delete <id>`, `/session clear [--all]`, or that `session.list` now returns
  closed sessions with their last prompt (grep for "saved session"/"picker"/"/sessions" returns
  nothing in any language). Add a "Saved sessions" section after `## The launch screen`
  (lines 7-30); mirror into `ko/tui.md`.
- **GAP-20 [Manuals]** `docs/manual/en/tui.md` `## Modes` (lines 75-77) documents only `Shift+Tab`
  and `Ctrl+P`. Add the **mode picker opened with Enter at the status row** and the
  mode-before-status ordering; mirror into `ko/tui.md`.
- **GAP-21 [Manuals]** `docs/manual/en/tui.md` `## Approvals` (lines 79-101) never mentions the
  approval **`note`** warning line that the TUI and CLI render. Add a sentence; mirror into `ko`.
- **GAP-22 [Manuals]** `docs/manual/en/scheduler.md` — no occurrence of "reminder" or
  `originSessionId` in any language. Document that a scheduled reminder is delivered as a
  `message.done` prefixed `⏰ Scheduled reminder (<jobId>)` **into the session that created the job**,
  and that an explicit channel is additive. Mirror into `ko/scheduler.md`; note `ja`/`es`/`zh-CN`
  have no `scheduler.md` at all.
- **GAP-23 [Manuals]** `docs/manual/{ja,es,zh-CN}/tui.md` — append translations of en lines 255-285:
  `## Markdown tables`, `## Built-in and custom agents` (A), `## Update notifications at startup`
  (J, including the downgrade guard and "already up to date"), the help-panel paragraph (K), the
  section-rules paragraph (L), and `## Teams and short delegation` (C + D + E).
- **GAP-24 [Manuals]** `docs/manual/{ja,es,zh-CN}/setup.md` — the vendor table (line 38) lacks the
  `gemini` row with "Google OAuth (ADC via `gcloud`), access token" that en/ko carry at line 61, and
  the `provider login --token` paragraph (en line 79). Also missing the model-profile / multi-model
  wizard text.
- **GAP-25 [Manuals]** `docs/manual/{ja,es,zh-CN}/commands.md` — `/team create|use|list|delete` is in
  `en`/`ko` only; the three stale trees still describe `/team N "<task>"` alone.
- **GAP-26 [Manuals]** the queued-prompt behaviour (I) is undocumented in **every** language. Once
  GAP-5 gives it a visible indicator, document it in `en/tui.md` `## Typing` (line 141) and mirror
  to all five.

---

## 5. Missing deviations docs

**Only one** of the 23 commits touched `docs/design/deviations/` — `1d14134` updated
`CORE-search-fix.md`. The other **22 commits have no deviation record at all**, which breaks the
process set out in `docs/design/deviations/README.md` ("each story records its deviations in its
own file here … create, never edit another story's file").

Existing deviation files: `CORE-context`, `CORE-gateway-autostart`, `CORE-login-progress`,
`CORE-memory-race`, `CORE-models`, `CORE-multimodal`, `CORE-prompts`, `CORE-search-fix`,
`CORE-session-race`, `CORE-settings`, `CORE-settings-reload`, `CORE-update`, `US-009`…`US-023`.

### Proposed new deviation files

Each follows the house format: a title line naming the story and its surface, a short framing
paragraph ending with "Recorded here per `docs/design/deviations/README.md`.", then numbered
**bold-claim + rationale** items explaining where the implementation departed from the task and why.

| Proposed file | Commits | Deviations it must record |
|---|---|---|
| `CORE-builtin-agents.md` | `00c9d36` | (1) Built-ins are **synthesised from `prompts/roles/*.md` at call time** rather than shipped as `.md` agent definitions, so a role prompt and its agent entry cannot drift. (2) Built-ins are seeded **first** into the merge dict so a same-named project/global file silently overrides them — chosen over erroring on a name clash, because a project overriding `executor` is the intended customisation path. (3) `model="inherit"`, `tools=ALL_TOOLS`, `permission="inherit"` — built-ins deliberately add no restriction of their own; a restriction belongs in a project definition. |
| `CORE-session-resume.md` | `f55d57f`, `569caf9`, `633db7d`, `7b87085`, `0b3e09c` | (1) History is snapshotted via `store.replace_messages()` after **every** turn (`agent/loop.py:64-73`) — a whole-history rewrite rather than an append, because provider history is mutated in place by compaction and an append log would need reconciliation. (2) The snapshot is **best-effort** (exceptions swallowed) — a storage failure must never fail a turn the user already paid for. (3) `session.resume` gained restore semantics **without a protocol version bump** because params/result shapes are unchanged. (4) `session.list` gained optional params rather than a new `session.listSaved` method, keeping one listing surface. (5) `lastPrompt` is computed on **every** `session.list` call, not only when `includeClosed` — one extra query per row, accepted for a simpler contract. |
| `CORE-session-delete.md` | `ce413b1` | (1) Named `session.deleteSaved`, not `session.delete`, to make it unmistakable that a **live** session is never affected. (2) Live sessions are filtered out server-side rather than erroring — `/session clear --all` must be usable while a session is open. (3) `deleted` returns `len(ids)`, not SQLite rows-affected (**R6**). (4) **Known gap**: attachments under `<home>/attachments/<session>/` are not removed (**R4**) — must be recorded as a deviation or fixed. |
| `CORE-prompt-queue.md` | `fe632c2` | (1) The queue is a plain in-memory `list` on `Session`, **not persisted** — a restart drops pending prompts (**R5**). (2) Attachments are captured **at enqueue time** and carried on `QueuedTurn`, so a file moved during the wait still resolves. (3) `_drain_turns` clears `session.interrupt` between turns, so **an interrupt does not stop the queue** (**R3**) — the single most important thing to record here, since it is surprising. (4) No event is emitted at enqueue, so the queue is invisible to clients. |
| `CORE-teams.md` | `cf78386` | (1) **Breaking**: an unknown agent name is now refused everywhere, not just inside a team — the old silent fallback to parent settings made typos invisible. (2) A team with no agent named in `delegate_task` defaults to `"executor"` if present, else the first member — deterministic rather than erroring. (3) Team membership is validated only at `/team create`, not by a settings validator, unlike model-profile refs (**R11**). (4) `agent.list` returns a **synthetic** `kind:"team"` row rather than a separate `team.get` method — and this is what breaks the IDE (GAP-1). (5) `Settings.load()` **migrates** existing settings by writing a `"default"` team. |
| `CORE-model-profiles.md` | `75893ed` | (1) Precedence lives in one function, `route_for()` — except that `subagent.py` keeps a legacy bypass for installs with no multi-model settings (**R10**). (2) Unknown profile refs are a **hard load failure** (`_validate_model_profile_refs`), deliberately stricter than the rest of `settings.py`, which validates loosely. (3) `resolve_reference` keeps a `vendor:model` and bare-vendor fallback for backward compatibility, which makes a typo'd `definition_model` degrade silently (**R12**). (4) `ProviderRegistry.agent_profile()` was added but never wired (**R9**). |
| `CORE-update-safety.md` | `28ff1ae`, `066422e`, `55bc6ff` | (1) The git downgrade guard is **ancestry-based** (GitHub `compare` status must be `"ahead"`), not version-based — a branch install has no meaningful version to compare. (2) Any `compare` failure is a **hard error**, never a silent "no update". (3) A tag-pinned install returns `None` from provenance and is never auto-moved to `main`. (4) `system.update` forces a fresh check, so an explicit request can never act on a 24h negative cache. (5) Internal keys are stripped from the RPC result, making them explicitly non-public. (6) `55bc6ff`'s tag-based version display was **superseded two commits later** by `93be684`'s exact-revision lookup — worth recording so the tag approach is not reintroduced. (7) Short-SHA prefix matching (**R7**). |
| `CORE-gemini-oauth.md` | `93be684`, `22df464`, `8d3ca31` | (1) Google ADC is delegated to `gcloud` rather than implementing Google's OAuth flow — Google owns consent, refresh-token storage and refresh; snowpea persists only `{"auth_method":"google_adc"}`. (2) A missing `gcloud` raises `LOGIN_UNSUPPORTED` with an API-key hint rather than falling back. (3) `remember_current_provider` writes exactly one of `api_key`/`oauth_token`/`auth_method` and **clears the others**, so switching auth method cannot leave a stale credential. (4) OpenAI also accepts `oauth_token` as a bearer, widening the field beyond Gemini. (5) **Known gap**: `oauth_token` was not added to either masking allowlist and `settings.json` is not chmod'd (**R1**, **R2**). |
| `CORE-tui-polish.md` | `a108071`, `50af009`, `6a46da9`, `a061106`, `b80ed64` | (1) F1 is handled by a raw `stdin.on("data")` listener because **Ink 5 removed F1 from `useInput`** — the single most important thing to record, since it looks like an accident. (2) `HelpPanel` reuses `wrapLine()` from `transcript.ts` rather than its own wrapper, so help and transcript wrap identically. (3) `layout.statusRows += 3` is hard-coded for the three section rules — a magic number that will drift if a rule is added or removed. (4) The agent panel's current row is hard-coded `"main"`; the team name moved to the rule label. |
| `CORE-markdown-tables.md` | `f55d57f` (table half) | (1) Cell width uses `Intl.Segmenter` graphemes with a CJK/emoji = 2 rule rather than a `wcwidth` dependency. (2) `MessageStream.tsx` was **gutted** so inline and full-screen rendering share one code path — a deliberate deletion of a parallel renderer. (3) A table that cannot fit falls back to stacked `key: value` rather than truncating or scrolling. (4) Tables inside code fences are left literal. |
| `CORE-schedule-origin.md` | `0f41a4d` | (1) `origin_session_id` is added by an **online `ALTER TABLE`** at store init rather than a migration framework. (2) A closed session is **restored purely to deliver**, then closed again — chosen over dropping the reminder. (3) An explicit `job.channel` is **additive**: the job still lands in `jobs_log` as well as the session. (4) A missing session degrades to a `log.warning` + the jobs log, never an error. |

**Also worth writing:** the range contains a release-prep commit (`232a383`) with no accompanying
release note beyond version bumps, and `docs/design/deviations/` has no `US-` file for any of the
TUI stories in this range even though `US-009`…`US-023` exist for earlier ones — the naming scheme
has drifted from `US-0NN` to `CORE-*`, which the README does not yet acknowledge.

---

## 6. Setup: provider/model configuration and the OAuth choices

> Priority deep dive (A). Commits `22df464`, `93be684`, the earlier device-code/PKCE work
> (`docs/design/deviations/CORE-login-progress.md`), and `fe513d7`. Everything below is backed by
> file:line or pasted command output from a scripted run against an **isolated `SNOWPEA_HOME`** in
> the scratchpad — the user's real `~/.snowpea` was never touched.
>
> **Committed vs in-flight.** The untracked `core/snowpea_core/providers/openai_oauth.py` and
> `codex_transport.py` (+ their tests) are **not wired into anything** —
> `grep -rn "openai_oauth\|codex_transport" core/ tests/` hits only those files and their own
> tests. They matter because they are the fix for §6.5's worst defect.

### 6.1 Vendor × auth-method matrix

Sources: `providers/presets.py:101-206` (`auth_methods`), `providers/auth_web.py:114-140`
(`ENDPOINTS`), `setup/wizard.py:255-301` (what the wizard offers), `cli/commands.py:271-314`.

| Vendor | `auth_methods` | Wizard reachable | CLI / RPC reachable |
|---|---|---|---|
| `anthropic` (`presets.py:110`) | `api_key` | no choice prompt | `setup --key`, `provider.configure` |
| `openai` (`:120`) | `api_key`, `device_code`, `oauth_token` | **1 / 2 / 3** | `provider login openai`; `... --token` |
| `openrouter` (`:129`) | `api_key`, `oauth_pkce` | **1 / 2** | `provider login openrouter`; `--token` **refused** (`commands.py:282`) |
| `gemini` (`:140`) | `api_key`, `google_adc`, `oauth_token` | **1 / 2 / 3** | `provider login gemini`; `... --token` |
| `xai` `:151`, `glm` `:159`, `minimax` `:167`, `kimi` `:175`, `deepseek` `:183`, `qwen` `:193` | `api_key` | no choice prompt | `setup --key` |
| `local` (`:200`) | `api_key` (optional) | variant → base URL → optional key (`wizard.py:230-250`) | `setup --vendor local --base-url` |

- `WEB_LOGIN_VENDORS` is **derived** as "more than one auth method" (`presets.py:246-248`) → exactly
  `openai, openrouter, gemini`, matching `ENDPOINTS`. Consistent — and this is the set that grew
  from 2 to 3 and broke the stale test in §2.2/F1.
- `oauth_token` is **not** an `auth_web` flow: `method_for()` (`auth_web.py:209-220`) knows only
  `device_code`/`oauth_pkce`/`google_adc`. It is a paste-a-token path implemented entirely in
  `wizard.py:295-300` and `cli/commands.py:281-298`.
- API-key-only vendors fail cleanly *but with the wrong exit code* — see **A-P3-3**:
  ```
  $ SNOWPEA_HOME=<scratch> uv run snowpea setup --login anthropic
  snowpea: error{code:"login_unsupported"} anthropic has no browser login; run `snowpea setup --vendor anthropic --key <API key>` instead
  $ echo $?   # → 0, not 2
  ```

### 6.2 The interactive flow as the user sees it

Entry `wizard.run()` (`wizard.py:91-219`). Quick = providers → done (`:50-53`); Full = providers →
search → browser → audio → tools → gateway → done (`:40-48`).

**Screen ① "① LLM provider"** (`setup/screens/providers.py:11-32`) — eleven radio rows `(●)/(○)`,
help text `"↑↓ to move, Enter to choose. A key is asked for after the list."`, each row tagged with
its auth methods (`catalog.py:349-352`) and `[active]` when `registry.is_configured()`
(`catalog.py:343`). The last row is `Skip — keep defaults`. Choosing a row calls
`state.select_vendor()` (`providers.py:39-40`); **Skip does not** — which is what causes **A-P2-4**.

**Step A — `_ask_for_key`** (`wizard.py:222-309`), the auth-method choice:

- openrouter → `authentication [1=API key, 2=browser login] (Enter=1): `
- openai / gemini → `authentication [1=API key, 2=browser login, 3=OAuth token (remote/headless)] (Enter=1): `
- **`2`** → `_run_sync(auth_web.login(vendor))`. On success (`:287-293`) credentials merge into
  `provider_configs[vendor]`. On `RpcError` (`:274-283`) it prints `login failed: <message>`, adds a
  403 hint, and **re-prompts** (this is `fe513d7`). Ctrl-C/EOF (`:271-273`) → note
  `"<vendor>: login cancelled — left unconfigured"`.
- **`3`** → `<vendor> OAuth access token: ` read **masked**, sets `auth_method="oauth_token"`.
  **No validation whatsoever.**
- Anything else — including `"9"`, or `"3"` on openrouter — falls through to the API-key prompt
  with **no error message** (**A-P3-1**).

**Masking on screen is correct.** Every secret uses `ui.ask_text(..., secret=True)` → `_read_masked`
(`ui.py:182-228`), echoing one `*` per character with backspace/ctrl-U, falling back to `getpass`
when termios is unavailable. `state.summary()` (`state.py:320-342`) prints **no credential material**
— only vendor/model/profile counts.

**Non-TTY:** `ui.is_interactive()` (`ui.py:28-36`) false → `_ask_for_key`/`_ask_for_model`/
`_configure_models` all early-return, so **no OAuth path is reachable non-interactively**.

### 6.3 What gets persisted, exactly where

Single write point: `WizardState.write()` (`state.py:268-318`) → `ProviderRegistry.configure()`
(`registry.py:163-178`) → `Settings.save()`. File: `$SNOWPEA_HOME/settings.json` (`config/paths.py:60-61`).

| Auth method | Keys written under `providers.<vendor>` | Written by |
|---|---|---|
| `api_key` | `api_key` (+ `model`/`base_url`/`variant`) | `state.py:151-153,159-165` |
| `device_code` (openai) | `token`, `refresh_token`, `expires_in` — **no `auth_method`** | `auth_web.py:350-354` |
| `oauth_pkce` (openrouter) | **nothing** — see **A-P1-1** | `wizard.py:287-291` |
| `google_adc` (gemini) | `auth_method: "google_adc"` only | `auth_web.py:675-680` |
| `oauth_token` | `oauth_token`, `auth_method: "oauth_token"` | `state.py:155-157`, `cli/commands.py:290` |

Proven shape and mode:
```
$ SNOWPEA_HOME=<S>/home1 uv run snowpea setup --vendor openai --key sk-FAKE-KEY-123 --model gpt-4.1
settings written to <S>/home1/settings.json
  provider   openai  model gpt-4.1
  models     1 profile · default openai:gpt-4.1
$ stat -c '%a %n' <S>/home1/settings.json
600 <S>/home1/settings.json          ← 0600 comes from the IN-FLIGHT chmod; HEAD has none
```

**The leak at HEAD, reproduced against HEAD's own module** (both allowlists omitted `oauth_token`):
```
HEAD mask_secrets -> {"providers": {"gemini": {"api_key": "***",
                      "oauth_token": "ya29.REAL-OAUTH",   ← leaked verbatim
                      "auth_method": "oauth_token", "token": "***", "refresh_token": "***"}}}
```
So at the analysed commit, `settings.get` over RPC, the `settings_get` tool, and any IDE settings
view hand the raw OAuth bearer to every authenticated client — and into any transcript recording the
response. **Fixed in the dirty tree** (`config/patch.py:15-28`, `server/settings_handlers.py:54-61`,
`config/settings.py:24-32,315-332`), tagged "CORE-fixes-v017 R1/R2". Confirm it lands.

### 6.4 Model discovery after auth

`_ask_for_model` (`wizard.py:328-384`) → `providers/models.py:81-115`.

- **When:** only from the `providers` screen and `_configure_models`, and only when interactive.
  **A flag-driven run never discovers** — `--vendor` puts `providers` in `answered`
  (`wizard.py:657`) so `_show` is skipped (`wizard.py:184-185`).
- **Which vendors:** all. `openai_compat` → `GET {base_url}/models` (`models.py:154-163`);
  `gemini_native` → `GET {base_url}/models` (`:166-176`); `anthropic_native` → merged with the
  static preset list, **falling back to static on failure** (`:179-195`). Timeout 5 s (`models.py:39`).
- **On failure:** `_ask_for_model` catches everything (`wizard.py:360-365`), prints
  `could not list models (<exc>)` and falls back to a free-text `model id` prompt. Setup never
  aborts on an unreachable vendor — deliberate (`wizard.py:331-337`).
- **Caching:** in-process only — `models._CACHE` keyed `(vendor, base_url)`, TTL 600 s
  (`models.py:42,47-48,61-73`). The wizard passes `refresh=True` (`:358`). **Nothing persists the
  discovered list**: `settings.providers.<vendor>.models` is *read* by `registry.list()`
  (`registry.py:376-379`) but **no code path writes it**.
- **Defect:** discovery reads only `api_key` (`wizard.py:352-355`). After a device-code login the
  credential is under `token`; after `oauth_token` under `oauth_token`; after `google_adc` there is
  none. So the step immediately after a successful OAuth login shows
  `could not list models (… HTTP 401 …)` → **A-P2-1**.

### 6.5 Token expiry and refresh — the most important finding

**There is no refresh logic anywhere in committed code.** `grep -rn "refresh_token\|expires_in\|expires_at"`
over `core/` returns, outside the untracked WIP: `auth_web.py:351-354` (which **writes**
`refresh_token`/`expires_in`), masking strings, and docs. **Nothing reads them.**

| Path | Expiry story |
|---|---|
| `device_code` (openai) | **Broken.** Stores `refresh_token` and never uses it. `expires_in` is a bare **duration with no anchor timestamp** (`auth_web.py:353-354`), so even a future refresher could not tell if it is stale. Consumed as a bearer via `registry.api_key_for()`'s `token` fallback (`registry.py:101-113`) → expiry surfaces as `openai (gpt-4.1): HTTP 401: {…}` (`openai_compat.py:120-125`) with **no "re-run login" hint**. Per `8d3ca31` this is a ChatGPT-subscription token that `api.openai.com` rejects *from the first request*, so this flow is arguably broken today, not only on expiry. |
| `oauth_pkce` (openrouter) | **Correct by construction** — mints a permanent OpenRouter API key (`auth_web.py:554-569`), nothing to expire. And the wizard throws it away (**A-P1-1**). |
| `google_adc` (gemini) | **The only working refresh**, because snowpea delegates entirely. `gemini_native._oauth_token()` (`gemini_native.py:66-85`) shells `gcloud auth application-default print-access-token` on **every** `_client()` call; gcloud refreshes. Good errors (`:82-84`, `:69`). Cost: one subprocess per request, no caching. |
| `oauth_token` (remote) | **Worst case.** No refresh token collected, no expiry recorded, **no validation at entry**. A Google access token lives ~1 h. Proven: `uv run snowpea provider login gemini --token FAKE-OAUTH-TOKEN` → `gemini: OAuth token saved to settings.json`. After expiry: opaque 401. Meanwhile `is_configured()` (`registry.py:157-160`) keeps reporting the vendor configured forever and the screen keeps showing `[active]`. |

### 6.6 Re-running `snowpea setup`

**Remembers:** vendor, model, base_url, variant, `has_saved_key`, per-vendor `provider_configs`,
model profiles, default profile, agent assignments — all via `WizardState.from_settings()`
(`state.py:68-127`).

**Does NOT remember `auth_method` or `oauth_token`** — the `cls(...)` call at `state.py:103-127`
never passes them. Verified: `from_settings: vendor=gemini has_saved_key=True auth_method=None`.
They are reloaded only by `select_vendor()` (`state.py:131-143`), which **Skip never calls**.

**Cross-vendor leakage: the fix holds.** `select_vendor` resets `api_key`/`oauth_token` and reloads
per-vendor state. Verified end to end — configuring `anthropic` after `openai` left both blocks
intact with no leak, and both model profiles survived.

**Switching API key ↔ OAuth does NOT work cleanly** (**A-P1-2**), for two independent reasons:
1. `ProviderRegistry.configure()` merges and **only removes a key when the incoming value is
   `None`** (`registry.py:171-177`). `remember_current_provider`'s `block.pop("api_key")`
   (`state.py:156`) mutates only the in-memory copy; the merge re-supplies the on-disk `api_key`.
2. Both adapters **prefer `api_key` over OAuth**: `gemini_native.py:92-98`, `registry.py:314-316`.

Proven, wizard path (existing API key → choose browser login):
```
settings.providers.gemini = {"api_key": "AIza-OLD-KEY", "model": "gemini-2.5-pro", "auth_method": "google_adc"}
api_key_for(gemini) = AIza-OLD-KEY
adapter prefers api_key? -> True | auth_method: google_adc
```
The user is told the login succeeded, `auth_method` says OAuth, and **every subsequent request still
uses the stale API key**. The reverse direction works but leaves an orphaned `oauth_token` in
`settings.json` forever.

### 6.7 Defects and UX gaps (A)

**A-P1-1 — OpenRouter browser login silently discards the API key it just minted.**
`wizard.py:287-291`:
```python
block.update(result.credentials)   # {"api_key": "sk-or-v1-..."}
block.pop("api_key", None)         # ← deletes the credential it just received
```
The pop is meant to clear a *stale* key when switching to browser auth, but it nukes the fresh one.
`state.write()` then skips the empty block (`state.py:281-283`) while still setting
`providers["default"] = "openrouter"` — the run ends with the default vendor **unconfigured** and the
summary reading `openrouter: API key stored`. Untested: `tests/test_setup_wizard.py:152-172` covers
only the gemini branch. **Fix:** clear stale keys *first*, then `block.update(result.credentials)`;
add an openrouter wizard test asserting the key survives.

**A-P1-2 — a stale `api_key` silently overrides every OAuth login** (both wizard and CLI). See §6.6.
**Fix:** have `LoginResult.credentials` for non-`api_key` methods carry explicit `{"api_key": None, "token": None}`
so `configure`'s `if value is None: merged.pop(key)` branch (`registry.py:173-174`) removes them; and/or
make `auth_method` authoritative in `api_key_for`/`build` instead of letting key presence decide.

**A-P1-3 — `device_code` has a refresh token and never refreshes; expiry is an opaque 401.**
**Fix:** land the in-flight `providers/openai_oauth.py` + `codex_transport.py` — they already
implement absolute `expires_at` (`openai_oauth.py:205-207`), `is_expired` (`:238-246`), refresh
(`:260-298`) and refresh-on-401 (`codex_transport.py:453`) — and route `auth_web.login("openai")` to
them. Minimum interim fix: store absolute `expires_at` at `auth_web.py:353`, and map a 401 from an
OAuth-authenticated vendor to `ProviderError("login_required", "<vendor>: your login expired — run \`snowpea provider login <vendor>\`")`.

**A-P1-4 — *(analysed commit only; fixed in the dirty tree)*** `oauth_token` masked by neither
allowlist + `settings.json` written with no chmod. See §6.3 and the amendment on R1/R2. **Make sure
that commit lands.**

**A-P2-1 — model discovery runs unauthenticated after any OAuth login** (`wizard.py:352-355`).
**Fix:** resolve the credential the way the registry does (reuse `api_key_for()`/`build()` semantics
including the `token` fallback and the gcloud path) instead of hand-reading `api_key`.

**A-P2-2 — `provider login <v> --token` accepts any string without a probe**
(`cli/commands.py:287-298`, `wizard.py:295-300`). **Fix:** after writing, do one cheap authenticated
`list_models` call and *warn* (not fail) on 401.

**A-P2-3 — `device_code` never records `auth_method`** (`auth_web.py:350-354`), so no surface can
distinguish an API-key openai from a device-code openai — exactly what an "your login expired"
message would need. **Fix:** add `credentials["auth_method"] = "device_code"`.

**A-P2-4 — `has_saved_key` disagrees between the two seeding paths**: `state.py:108` (`api_key`
only) vs `state.py:141-143` (`api_key or token or oauth_token`). An OAuth user re-running setup and
pressing Skip is shown `[Enter to use the environment]`, implying nothing is stored.

**A-P2-5 — orphaned secrets at rest** after any auth switch (same root cause as A-P1-2).

**A-P3-1 — invalid input at the auth-method prompt is silently swallowed** (`wizard.py:301`); the
error branches correctly re-prompt, an unrecognised digit should too.
**A-P3-2 — `oauth_token` is advertised as a "web login"** (`catalog.py:331-334`) though no browser
is involved.
**A-P3-3 — unsupported-login paths print the right error but exit 0**; `_fail(..., EXIT_USAGE)`
returns 2 but the code is dropped between `provider_login` and process exit
(`cli/commands.py:283-286`, dispatch `:1432`). Scripts cannot detect the failure.

### 6.8 Comparison against Hermes

A reference clone **is** available at `/tmp/hermes-ref` (`docs/vendoring-map.md` pins
`NousResearch/hermes-agent @ 8d79c2ff`, MIT). **No auth code is vendored** — `core/snowpea_core/vendor/hermes/`
contains only `tools/` — so this compares against the read-only reference clone.
`README.md:208-219`'s table has **no auth or OAuth row**, so nothing below is derived from it.

| Dimension | snowpea | hermes-agent @ 8d79c2f |
|---|---|---|
| LLM-auth code size | `auth_web.py`, 775 lines, 3 flows | `hermes_cli/auth*.py`, **8,633 lines** across 12 modules |
| Refresh on OAuth tokens | none, except delegating gemini to `gcloud` | `auth_codex.py:465-478` refreshes **before every use** when expiring (configurable skew), under a cross-process lock with a re-read inside the lock |
| Expiry representation | `expires_in` duration, **no anchor** (`auth_web.py:353-354`) | absolute `expires_at`/`expires_at_ms`/`last_refresh`, `expires_in` rebased on read (`tools/mcp_oauth.py:318-339`) |
| Refresh-failure classification | n/a | `auth_codex.py:282-320` separates hard relogin-required (`invalid_grant`, `refresh_token_reused`, 401/403) from transient, and can adopt `~/.codex/auth.json` (`:359-392`) |
| Refresh-token rotation | not modelled | explicitly modelled (`auth_oauth_grants.py:19-22,258-285`) |
| Credential file mode | 0600 **only in the uncommitted tree** | `auth_oauth_grants.py:210-211` — `atomic_json_write(..., mode=0o600)` |
| Credential store | one `settings.json` mixing config and secrets | dedicated `~/.hermes/auth.json` with per-provider blocks and labels |
| Remote/headless UX | paste a raw token, no expiry, no validation | `auth_device_flow.py:45-98` detects a remote/SSH session, decides whether a graphical browser can open, prints a loopback SSH-forwarding hint |
| Post-auth model choice | free-text fallback whenever discovery 401s | dedicated `auth_model_picker.py` (291 lines), grouped labels, confirmation guards, persisted choice |

**Honest read:** snowpea's `auth_web.py` is a clean, data-driven, well-factored module and its
`ENDPOINTS`-as-data design is *nicer* than Hermes' per-vendor sprawl. What it lacks is the entire
**lifecycle half**: absolute expiry, refresh-before-use, refresh-on-401, relogin-vs-transient
classification, a locked 0600 credential store, and a remote-session-aware login UX. The untracked
`openai_oauth.py`/`codex_transport.py` are explicitly adapted from `hermes_cli/auth_codex.py`
(docstring at `providers/openai_oauth.py:29-31`) and close most of the gap **for `openai` only** —
leaving `gemini`'s `oauth_token` path with the same expiry cliff.

---

## 7. Per-agent model assignment

> Priority deep dive (B). Commits `75893ed` (model profiles), `00c9d36` (built-in roles),
> `cf78386` (project teams + short delegation). Claims are backed by file:line or by a live probe
> run against an isolated `SNOWPEA_HOME` in the scratchpad.

### 7.1 The real precedence chain

`route_for()` is the whole policy, and it has **exactly one production call site**:
`core/snowpea_core/session/manager.py:88` (verified by exhaustive grep — the only other hits are
`tests/test_model_routing.py` and the definition).

`config/model_routing.py:35-45`:
```python
35  if provider is not None or model is not None:
36      return ModelRoute(provider, model)
38  assignment = None
39  if agent:
40      assignment = settings.agents.models.get(agent)
41  for reference in (assignment, definition_model, settings.models.default):
42      route = resolve_reference(settings, reference)
43      if route.provider is not None or route.model is not None:
44          return route
45  return ModelRoute()
```

**Actual chain — and it applies at session creation only:**
1. explicit `provider`/`model` args to `sessions.create()`
2. `settings.agents.models[<agent name>]` → profile id
3. `definition_model` (the agent `.md` `model:` field) — **only passed by 2 of 5 callers**
4. `settings.models.default` → profile id
5. `ModelRoute(None, None)` → falls through to `ProviderRegistry.default_vendor()`/`model_for()` at
   turn time (`agent/loop.py:252`, `providers/registry.py:301,139-149`), which **re-applies**
   `models.default` at the vendor layer (`registry.py:358-360,142-144`).

**Where reality diverges from the stated mental model — this is the headline of section B:**

| Mental model | Reality |
|---|---|
| "tool call override" | ❌ **Does not exist.** `delegate_task`'s schema (`tools/delegate.py:83-113`) has `task`, `agent`, `tools`, `timeout` — **no `model`**. Same for `agent.spawn` (`server/agent_handlers.py:115-135`). |
| "agent definition/profile" | Split into two rungs with **`agents.models` winning over the `.md`**, and rung 3 silently dropped on 3 of 5 create paths. |
| "session pin" | ❌ **No per-session pin.** There is no `session.setModel` RPC — only `session.setMode` (`server/session_handlers.py:101,581`). `/model <name>` (`commands/model_cmd.py:96-97`) mutates `ctx.session.model` in memory and persists to `settings.providers.<vendor>.model` — **not** to `models.profiles`, and **not** to the session row (`session/store.py` has `update_mode` at :113 but **no `update_model`**), so it is **lost on `session.restore()`** (`session/manager.py:147-149`). |
| "project settings" | ❌ **No project-level model settings exist at all.** `ProjectSettings` (`config/project.py:48-58`) has only `defaultMode`, `allowlist`, `backend`, `agents{max_concurrent, teams, activeTeam}`. Only **teams** are project-scoped. |
| "global default" | ✅ `models.default`, global only. |

**`agents.models` is global-only.** `ProjectAgentsSettings` (`config/project.py:40-45`) deliberately
omits it, and `ProjectSettings` is `extra="allow"` (`:51`) — so a hand-added `"models": {...}` in
`.snowpea/settings.json` is **silently accepted and silently ignored**.

### 7.2 Where profiles live

**`$SNOWPEA_HOME/settings.json` — the only place models live:**
```jsonc
{
  "models": {
    "default": "openai-gpt-4o-mini",                       // a PROFILE ID
    "profiles": { "openai-gpt-4o-mini": { "provider": "openai", "model": "gpt-4o-mini" } }
  },
  "agents": {
    "models":       { "executor": "openai-gpt-4o-mini" },  // agent -> profile id
    "teams":        { "default": ["architect","critic","executor","explorer","test-engineer","verifier"] },
    "default_team": "default"
  }
}
```
`resolve_reference` (`config/model_routing.py:48-61`) accepts three spellings wherever a profile id
is expected: a **profile id**, legacy **`vendor:model`**, or a **bare vendor name**;
`"inherit"`/empty is a no-op. That third spelling is the root of two bugs below.

**Agent `.md` frontmatter — supported fields** are exactly `name`, `description`, `model`, `tools`,
`permission`, `max_turns` (`agent/definition.py:63-96`, parser `:240-270`, round-trip
`render_agent_md` `:215-227`). Real example —
`tests/fixtures/plugins/sample-plugin/agents/fixture-agent.md`:
```markdown
---
name: fixture-agent
description: A fixture agent definition the loader must register.
model: inherit
tools: [read_file, grep]
permission: inherit
max_turns: 4
---
```
Search dirs (`definition.py:45-46,278-286`): `$SNOWPEA_HOME/agents/`, then `<workdir>/.snowpea/agents/`
and `<workdir>/.claude/agents/` (project wins).

**Built-in roles carry no frontmatter at all.** `core/snowpea_core/prompts/roles/*.md` are plain
prose, and `builtin_agent_definitions()` (`definition.py:289-321`) hard-codes `model="inherit"`,
`tools="*"`, `permission="inherit"`, `prompt=""`. ⇒ **a built-in role's model can ONLY come from
`agents.models[<role>]`.**

### 7.3 How each surface can view and change them

| Surface | View | Change | Evidence |
|---|---|---|---|
| TUI HUD | ✅ `Model: provider/model` | ❌ | `tui/src/layout/hud.ts:160-163`. Fed once by `session/ready` (`tui/src/app.tsx:450`, `tui/src/state/store.ts:494`) — **goes stale after `/model`**. |
| `/model` | ✅ lists vendor models | ⚠️ session model only | `commands/model_cmd.py:78-98`; writes `settings.providers.<vendor>.model` (`:45-51`), **never** `models.profiles`/`models.default`/`agents.models`. |
| `/agent` | ✅ `list` shows `model:` | ❌ | `commands/agent_cmd.py:49` `USAGE = '/agent create "<desc>" | /agent list'`; display at `:190`. **No `/agent model`.** |
| `/team` | ✅ | teams only | `commands/team_cmd.py:22,90-134` |
| Headless CLI | ❌ | ❌ | `cli/commands.py:1234` `setup --model` sets the *vendor's* default only. No `snowpea model`, no `snowpea agent model`. |
| Setup wizard | ✅ | ✅ **the only surface that can assign per-agent models** | `setup/wizard.py:387-446` |
| `settings.set` RPC | ✅ (models returned unmasked) | ✅ **can write `models.profiles`, `models.default`, `agents.models`** | `server/settings_handlers.py:97-128` — deep-merged and re-validated through `Settings`, **no key allowlist**. Hot-rebinds via `config/hot_reload.py:63-109`, but **only new sessions** pick it up. |
| IDE Settings | ❌ | ❌ | `snowpea-ide/src/renderer/views/Settings.vue` has three sections — Providers (:138), Project defaults (:205), Allowlist (:271). Zero hits for `models`/`profiles` in it or `stores/settings.ts`. |
| IDE Agents | ⚠️ read-only badge | ❌ | `snowpea-ide/src/renderer/views/Agents.vue:156` renders `definition.model` as a badge; mapped at `stores/agents.ts:37,78,123`. No editor. |

### 7.4 Who actually honours the routing

Five real `sessions.create()` call sites:

| Path | `agent=` | `definition_model=` | Verdict |
|---|---|---|---|
| (a) `server/session_handlers.py:174-183` — top-level interactive (TUI/IDE) | ✅ | ❌ **never passed** | **PARTIAL** — honours `agents.models` + `models.default`; ignores the `.md` `model:` |
| (b) `agent/named.py:299-312` — named agents | ✅ | ✅ | **YES — the only fully correct caller** |
| (c) `agent/subagent.py:480-509` — `delegate_task` | ✅ | ⚠️ conditional | **PARTIAL — real bug below** |
| (d) `scheduler/scheduler.py:284-290` — scheduled jobs | ✅ `job.agent` | ❌ | **PARTIAL** |
| (e) `gateway/router.py:628-634` | ✅ | ❌ | **PARTIAL** |

**(c) The legacy `has_model_routing` bypass is a live bug** — `agent/subagent.py:487-498`. When
`models.default` is unset *and* the agent has no `agents.models` entry, it resolves the `.md`
`model:` through `_split_model()` (`subagent.py:542-548`), which is **not** `resolve_reference` and
**never consults `models.profiles`**. Reproduced live:
```
A) profiles exist, no default, agent .md says `model: fast`
   resolve_reference('fast')        -> ModelRoute(provider='openai', model='gpt-4o-mini')
   route_for(definition_model=fast) -> ModelRoute(provider='openai', model='gpt-4o-mini')
   has_model_routing = False
   -> child session created with provider='fast' model=None      ← WRONG
```
⇒ **a valid profile id in an agent `.md` is mis-read as a vendor name**, and the child dies at its
first turn with `unknown provider vendor: fast`. Second effect of the same branch: when
`has_model_routing` *is* true, `provider, model = (None, None)`, so a `/model`-pinned parent's choice
is **dropped for every child** the moment any `models.default` exists.

**(d′) `/team <N>` worktree workers ignore per-agent profiles entirely.** `agent/team.py:477` calls
`manager.run(anchor, prompt)` with **no `agent=`**, and `_anchor()` (`team.py:623-651`) copies
`id, workdir, mode, provider, model, origin_surface, created_at, max_concurrent, origin_conn` but
**never `team`/`team_agents`**. So `parent.team_agents` is empty, the `"executor"` default at
`subagent.py:373-375` never fires, `record.name == ""`, and `agents.models` is never consulted.

**(e) TUI `$agent task`** ✅ routes identically to `delegate_task` (`tui/src/app.tsx:1008-1017` →
`agent.spawn` → `server/agent_handlers.py:134` → `SubagentManager.run`), and inherits bug (c).

**(f) Scheduled jobs** do create sessions (`scheduler/scheduler.py:282-290`) unless the job names a
persistent agent, in which case they *borrow* that agent's already-routed session (`:280`). Fresh
ones honour `agents.models[job.agent]` + `models.default` but **ignore the definition's `model:`**.

**(g) Auxiliary LLM calls — none get their own profile.** `session/compaction.py:242`,
`agent/loop.py:252`, `agent/team.py:287`, `commands/agent_cmd.py:159`, `commands/skill_cmd.py:149`,
`commands/ultrawork.py:72`, `commands/ralph.py:156` are all `core.providers.get(session.provider,
session.model)` — they inherit the caller's session route. (There is no prompt-enhancer in core.)

### 7.5 Does the setup wizard assign models to roles?

Yes — `setup/wizard.py:387-446`, and it is the **only** surface that can.

- `wizard.py:395-397`: non-interactive runs create exactly one profile, make it the default, and
  assign **nothing** per agent.
- `:399-408` register extra profiles; `:410-422` pick `models.default`.
- `:424-429` the agent roster = `builtin_agent_definitions()` ∪ `discover_definitions(cwd, home)` —
  so **built-in roles AND custom agents**, but **not teams** and not named-agent instances.
- `:432-446` free-text agent name → profile number. **Free text**, so a mistyped agent name is
  silently accepted into `agents.models` (`setup/state.py:182-190` validates the *profile* only).
- **Persistence is 100 % global** (`wizard.py:122`, `state.py:286-297`). No project-scoped write.

### 7.6 Validation and failure modes (tested for real)

**(a) Unknown profile id in `agents.models` → the daemon will not start.**
`Settings._validate_model_profile_refs` raises, and `Settings.load` catches only
`OSError`/`JSONDecodeError`, so the `ValidationError` escapes:
```
$ SNOWPEA_HOME=$SP .venv/bin/snowpea --home $SP tools list
snowpea: the daemon exited immediately with status 1; see .../logs/daemon.out
$ tail logs/daemon.out
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, agents.models references unknown model profile(s): executor=does-not-exist
```
Fail-closed and **unrecoverable without hand-editing JSON** — no daemon means no `settings.set`.
(Through `settings.set` the same patch is correctly rejected as `INVALID_PARAMS`.)

**(b) Typo'd `model:` in a custom agent `.md` → completely unvalidated** (`definition.py:263` just
does `str(meta.get("model") or "inherit")`):
```
resolve_reference('claude-sonnet-4') -> ModelRoute(provider='claude-sonnet-4', model=None)
```
The typo becomes a **vendor name** and blows up only at the child's first turn.

**(c) A profile pointing at an unconfigured provider is caught at no config layer:**
```
Settings validated OK: provider='not-a-vendor' model='x'
default_vendor()       -> not-a-vendor
providers.get() raises -> ProviderError unknown provider vendor: not-a-vendor
```
`ModelProfile` enforces only non-empty strings. Worse, `default_vendor()` (`registry.py:358-360`)
returns the bogus vendor **ahead of** `settings.providers.default` and the first-configured-vendor
fallback — so **one bad default profile bricks every session**, not just routed ones.

### 7.7 Gaps and fixes (B)

**B-P1-1 — `agent/subagent.py:487-498` legacy bypass ignores `models.profiles`.** Delete the bypass
and let `route_for` own it:
```python
provider, model = (None, None)
if not (settings.models.default or assigned or definition_model):
    provider, model = parent.provider, parent.model
```
and drop `_split_model` (`subagent.py:542-548`) — `resolve_reference` already handles `vendor:model`
and bare vendors. This also removes R10 from §2.4.

**B-P1-2 — `_validate_model_profile_refs` bricks the daemon.** In `Settings.load`, catch
`ValidationError`, log the offending keys, drop the bad `agents.models`/`models.default` entries and
boot degraded. Keep the strict validator for the `settings.set` path (`settings_handlers.py:118`)
where `INVALID_PARAMS` is right.

**B-P1-3 — a broken `models.default` hijacks `default_vendor()`** (`registry.py:358-360`). Guard with
`profile[0] in PRESETS` and fall through otherwise; add a `ModelProfile` validator asserting the
provider is a known preset.

**B-P1-4 — IDE has no model-profile UI at all.**
- New `snowpea-ide/src/renderer/views/settings/Models.vue`, registered alongside
  `Appearance.vue`/`Audio.vue` and linked from `views/Settings.vue` after the Providers section (:138).
- Extend `snowpea-ide/src/renderer/stores/settings.ts` with a `profiles` getter over
  `this.global.models.profiles`, plus `setDefaultModel(id)` / `addProfile(id, provider, model)`
  calling the existing `setGlobal(patch)` (`stores/settings.ts:285-303`) with
  `{models:{profiles:{<id>:{provider,model}}}}` / `{models:{default:"<id>"}}`.
- **Deleting a profile needs care:** `_deep_merge` (`server/settings_handlers.py:64-77`) has **no
  delete sentinel**, so a `null` becomes a `null` value and fails `ModelProfile` validation. Either
  add sentinel handling server-side or have the IDE send the full replacement `profiles` object.
- Vendor dropdown source: the already-loaded `provider.list` rows (`stores/settings.ts:39`).

**B-P1-5 — IDE Agents: make the model badge editable.** `views/Agents.vue:156` → a `<select>` of
profile ids + `inherit`. It must write `{agents:{models:{[definition.name]: profileId}}}` via
`setGlobal(...)`, **not** the agent `.md`: the `agent.*` RPCs have no update method
(`server/agent_handlers.py:161-168` exposes only `list`, `create`, `bindChannel`, `delete`, `spawn`),
and built-in roles have no file to edit. `stores/agents.ts:123` should also carry an
`effectiveModel` computed as `settings.global.agents.models[name] ?? definition.model`.

**B-P2-1 — three `sessions.create()` callers silently drop `definition_model`**
(`server/session_handlers.py:174`, `scheduler/scheduler.py:284`, `gateway/router.py:628`). Cleanest
fix: move the definition lookup **into** `SessionManager.create` so `definition_model` becomes
derived rather than a caller obligation.

**B-P2-2 — `/team` workers ignore per-agent profiles.** Copy `team=lead.team,
team_agents=lead.team_agents` onto the anchor in `agent/team.py:623-651` (which also restores the
membership guard at `subagent.py:390-396`), and/or pass `agent="executor"` at `team.py:477`.

**B-P2-3 — `/model` doesn't survive a restart and doesn't update the HUD.** Add
`Store.update_model(session_id, provider, model)` next to `update_mode` (`session/store.py:113`),
call it from `commands/model_cmd.py:96`, and emit a session event the TUI folds into `session/ready`
(`tui/src/state/store.ts:494`, `tui/src/layout/hud.ts:160`).

**B-P2-4 — no project-scoped model overrides.** Add `models` to `ProjectAgentsSettings`
(`config/project.py:40-45`), give `route_for` a `project` argument, and insert project rungs between
rungs 2 and 4 — mirroring what `team_config.teams_for` (`agent/team_config.py:17-21`) already does
for teams. **This is the change that makes the user's stated mental model true.**

**B-P3-1 — teach `/model` about profiles** (`commands/model_cmd.py:78`): `/model` lists profiles
(marking the default) *and* vendor models; `/model <profile-id>` resolves and sets both
`session.provider` and `session.model`; `/model default <id>` writes `settings.models.default`.
**B-P3-2 — add `/agent model <agent> [<profile-id>|inherit]`** (`commands/agent_cmd.py:166-178`
currently branches only on `create`/`list`); validate against `models.profiles`, save, then
`core.adopt_settings(...)` (`server/app_server.py:183-193`) so it lands without a restart. Update
`USAGE` at `agent_cmd.py:49`.
**B-P3-3 — validate the agent *name* in `setup/state.py:182-190`** against
`builtin_agent_definitions() | discover_definitions(...)`.
**B-P3-4 — warn on `.md` `model:` values that resolve to a bare vendor** (`agent/definition.py:263`).
