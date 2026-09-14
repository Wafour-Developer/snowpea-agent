# M9 Contract — Prompt Library, Tiers and Turn Queueing (binding; ships CORE-prompts)

Retrofit contract for work that shipped under `docs/design/deviations/CORE-prompts.md` and the
prompt-queueing half of the post-v0.1.1 session work. Builds on
[`m1-core-contract.md`](m1-core-contract.md) §8 (agent loop) and
[`m6-m7-skills-agents-contract.md`](m6-m7-skills-agents-contract.md) §2 (agent definitions).
Source of record: `core/snowpea_core/prompts/`, `core/snowpea_core/agent/`.

Acceptance criteria: **AC-22 … AC-26**.

## 1. Library layout (`core/snowpea_core/prompts/`)

```
prompts/
  __init__.py             # re-exports load / render / workflow_brief
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

Every `.md` above is **prompt text the model reads**, and MUST live here rather than inline in the
module that uses it, so that prompt review sees all of it in one place. In particular tool
descriptions live in `prompts/tool_descriptions.py` and are imported by name from `tools/*.py`
(`tools/fs.py`, `tools/shell.py`, `tools/glob.py`, `tools/grep.py`, `tools/delegate.py`); the JSON
schemas stay with the tools.

`test_prompts_compose.py::test_every_workflow_prompt_renders_with_no_placeholders_left` asserts the
workflow set is exactly 10 files, so adding one without registering it fails the suite.

## 2. Three tiers, and why (`compose.build_tiers`, `compose.PromptTiers`)

The system prompt is assembled as three concatenated tiers, ordered most-stable first so that a
provider's prefix cache survives as much of it as possible. `PromptTiers` is a frozen dataclass with
fields `stable`, `context`, `volatile`; `PromptTiers.text()` joins them in that order, and
`compose.build_system_prompt(**kwargs)` is a thin wrapper around `build_tiers(...).text()`.

| tier | contents | budget (tokens) |
|---|---|---|
| stable | `base.md` (or an explicit `identity`), `fragments/execution.md`, the vendor layer, `config_rule.md`, `search_honesty.md`, the mode file, for a subagent `roles/_preamble.md` + the role file, the persona, and the reply-language rule | **2900** |
| context | environment block, `AGENTS.md`-style context files, tool listing (only when tools are present) | 800 |
| volatile | memory recall block, context-pressure note (above `CONTEXT_PRESSURE_THRESHOLD = 0.75`) | 600 |

The reply-language rule sits in **stable**, not volatile: the module docstring defines stable as
"changes only when the mode, model, role or reply language changes", and a language override is
exactly that kind of per-session constant.

**AC-22.** The tier budgets MUST be enforced by test. `TIER_BUDGET_TOKENS = {"stable": 2900,
"context": 800, "volatile": 600}` is a **test-file constant** (`tests/test_prompts_compose.py:29`),
not production code. `test_stable_tier_stays_under_budget` is parametrized over the three modes ×
the three vendor classes × `ROLES = (None, "executor")`; the context and volatile budgets are
single unparametrized cases. The budget's job is not to be tight; it is to make unnoticed growth of
the library impossible. (The "measured worst case ≈2747 tokens" figure, after the M15 working-discipline fragment, is recorded in
`deviations/CORE-prompts.md` as prose — no constant or assertion carries it.)

Rules the composition MUST obey:

- **Vendor layers are keyed on the provider preset, not the model id** (`compose.VENDOR_CLASS_BY_PROVIDER`,
  eleven presets). `compose.vendor_class_for` lowercases, splits on `:`, and falls back to
  `openai-family` — the safer default for an unknown hosted endpoint. A falsy/absent provider
  resolves to `anthropic`. `vendors/anthropic.md` is deliberately empty, pinned by
  `test_anthropic_vendor_layer_adds_nothing`.
- **Only the date goes in the prompt, never the time.** `environment._date_line` renders
  `- Today: <weekday, day month year>` and no clock; `fragments/environment.md` states "The date
  above is fixed when the turn starts; ask a tool for the exact time if you need it." A clock in the
  stable or context tier defeats prefix caching on every provider.
- **The environment block is cached per session**, `agent.ENVIRONMENT_TTL_SEC = 30.0`, keyed
  `f"{session.id}:{session.workdir}"` in `agent._ENV_CACHE`, and invalidated by
  `agent.invalidate_environment(session=None)` (a `None` session clears the whole cache).
  `compaction.prompt_messages` rebuilds the whole prompt purely to measure it, on every turn and
  every `context` event; probing git and reading `AGENTS.md` each time would put a subprocess on an
  accounting path.
- **A git failure of any kind drops the workspace block entirely** rather than reporting a guess:
  `environment.git_snapshot` returns `None` when git is missing, raises `OSError`/`SubprocessError`
  or exits non-zero, and `environment.workspace_block(None)` returns `""`.
- **`agent.replyLanguage` defaults to `"auto"`, which emits no rule at all**
  (`config/settings.py:106`, `AgentSettings.replyLanguage`). `compose.reply_language_rule` returns
  `""` for an empty value or `"auto"`; `base.md` already says to answer in the language the user
  wrote in. An explicit tag emits a directed override naming the language. Prompt files stay in
  English; translating a system prompt degrades instruction following, and the reply language is the
  part the user cares about. `/ralph` and `/team` MUST thread the resolved language into their
  workers' briefs, because a child cannot see the parent's settings —
  `commands/ralph.py::reply_language_for` and `commands/team.py` pass `reply_language=` into
  `workflow_brief` for `ralph-story`, `ralph-review` and `team-task`.
- The keyword-only `core` argument lives on **`agent.build_system_prompt`** and
  **`agent.build_messages`** (`core: Core | None = None`), not on `compose.build_system_prompt`.
  It is consumed only by `agent.reply_language(core)`. The sole external callers passing it are
  `agent/loop.py::_drive` and `session/compaction.py::prompt_messages`, so no other caller had to
  change.

## 3. Subagent prompts

**AC-23.** Every child MUST receive the subagent preamble, definition or not.
`subagent._apply_definition` sets `child.is_subagent = True` unconditionally, before it inspects the
definition; `build_tiers` loads `roles/_preamble` whenever `subagent or role` is set. A definition
whose name matches a file in `prompts/roles/` also gets that role (`subagent.role_file` returns the
name only when `load("roles/<name>")` resolves); one that does not composes without it. Before this,
`/ralph`, `/ultrawork` and `/team` workers ran the bare base prompt with **no report contract at
all**.

A workflow brief prepends `compose.BRIEF_RULES` — a faithful digest of `base.md` (read before you
edit; change only what the task needs; run the project's checks; never report a result you did not
produce) — substituted into the brief's `${BASE_RULES}` placeholder rather than the full text, which
the child already has in its own system prompt. `compose.workflow_brief(..., base_rules: bool = True)`
turns it off per call; it also always substitutes `${LANGUAGE_RULE}`.

The JSON-emitting workflow prompts — `ralph-prd`, `ultrawork-split`, `team-plan`, `agent-generate`,
`skill-learn`, `compaction` — MUST NOT carry `${BASE_RULES}`. They are one-shot "answer with a
single JSON object and nothing else" prompts and prose in front of that instruction fights it.
Exactly three workflow files carry the placeholder: `ralph-story.md`, `ralph-review.md` and
`team-task.md`. **`team-conflict.md` deliberately carries neither placeholder** — it is appended as a
second part after the `team-task` brief that already supplied the rules, so re-stating them would
duplicate them in one message.

## 4. Denials adapt; they do not end the turn

**AC-24 (amends M1 §8).** A refused tool call MUST be appended to the conversation as a failed
`tool.result` carrying the refusal text, and the loop MUST continue, so the model can choose a
different approach. `agent/loop.py::_deny_call` → `_fail_call` emits
`events.tool_result(callId, name, ok=False, ...)` and appends a `role="tool"` history message reading
`Denied: {reason}. Choose a different action; do not retry the same call.`, with
` In plan mode, finish the plan and call set_mode("accept") so the user can choose to start
implementing; never ask them in prose to switch modes.` appended in plan mode, so a model that
tried to edit is routed into the mode picker rather than into a request the user has to act on.

`MAX_DENIALS_PER_TURN = 3` (`agent/loop.py:46`) bounds a model that only ever re-sends the refused
call; the round loop counts denials and the third ends the turn via
`finish_turn(core, session, turn_id, "denied")` → `turn.done{reason:"denied"}`. The `mode_denied` /
`approval_denied` error events are unchanged, so no surface has to learn anything new.
`job.lastStatus == "denied_by_timeout"` is derived in `scheduler/scheduler.py` from the collected
**error codes** (`errors.APPROVAL_TIMEOUT`), never from the turn reason, and is unaffected.

Tests in `tests/test_session_loop.py`
(`test_accept_mode_shell_denied_is_fed_back_to_the_model`, `test_plan_mode_denies_writes`),
`tests/test_permission_matrix.py` and
`tests/test_approval_multichannel.py::test_an_unanswered_unattended_approval_times_out_as_a_denial`
encode the new contract: the tool never runs, the error event still fires, the refusal arrives as a
failed `tool.result`, and the turn reaches `complete`.

## 5. Prompt queueing

**AC-25.** A prompt submitted while a turn is running MUST be queued, never dropped, never run
concurrently against the same history.

```python
# session/session.py:65 — in-memory only; typed list[Any] to avoid a session -> loop import cycle
queued_turns: list[Any] = field(default_factory=list)
```

`QueuedTurn` itself is a frozen dataclass in `agent/loop.py` with fields `turn_id`, `text`,
`unattended`, `attachments`. `agent/loop.py::start_turn` always mints a `turnId` and builds a
complete `QueuedTurn`, taking the pending attachments **synchronously** (`pending.take(session.id)`)
so a later prompt's images cannot leak into an earlier turn. If `session.turn_task` is live the turn
is appended, a `turn.queued{turnId, position, queued}` event is emitted and the id returned;
otherwise one `_drain_turns` task is started and drains the FIFO, clearing `session.interrupt` and
setting `session.current_turn` before each turn, emitting `turn.dequeued{turnId, reason:"started",
queued}`, and clearing `session.current_turn` in a `finally`.

The queue is observable on the wire: `protocol.TurnQueued` (`kind: "turn.queued"`) and
`protocol.TurnDequeued` (`kind: "turn.dequeued"`, `reason` ∈ `started` | `dropped`) are registered
session event kinds with constructors in `session/events.py`.
`tests/test_session_loop.py` asserts the exact sequence for two stacked prompts.

**An interrupt drops the queue.** `server/session_handlers.py::session_interrupt_handler` sets
`session.interrupt` **and** calls `agent_loop.flush_queued_turns`, which clears `queued_turns` and
emits `turn.dequeued{reason:"dropped"}` + `turn.done{reason:"interrupted"}` for each dropped prompt,
so no client is left waiting on a `turnId` that will never complete. `_drain_turns` performs the
same flush after a turn that ended interrupted.

Known limit, recorded deliberately: `queued_turns` is **not persisted** — it appears nowhere in
`session/store.py` and is not part of `Session.summary()`. A daemon restart loses every queued
prompt, and the client never receives `turn.done` for the `turnId` it was already given.

## 6. Tests

**AC-26.** `tests/test_prompts_compose.py` (loader caching and traversal, tier ordering and
contents, the empty anthropic layer, `vendor_class_for`, environment/workspace/context-file
rendering, `BRIEF_RULES` substitution and its absence from the JSON workflows, the three budget
tests, and golden snapshots under `tests/golden/prompts` with a `test_no_stale_snapshots` guard) and
`tests/test_prompts_behaviour.py` (plan mode makes no write calls; the model adapts after one
denial; three denials end the turn; the prompt actually contains the reply-language rule, the
context-pressure line, the tool list, the environment block and project context files) MUST both
pass with the fake provider and no network.

Prompt text is not projected into `server/protocol.py`, so `scripts/gen_protocol.py --check` MUST
report no drift from prompt-library changes.
