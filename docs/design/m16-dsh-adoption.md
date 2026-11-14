# M16 — Adopting the DeepSeek Harness design

Status: approved by the owner 2026-11-14; Phase 1 done except the user-facing goal command and its manual pages.

Source studied: `deepseek-ai/deepseek-harness` (dsh), commit `39163dd` (2026-11-13), MIT.
Whatever code is ported keeps its MIT notice in `THIRD_PARTY_NOTICES.md`. Brand rules
(`BRAND_GUIDELINES.md` there) forbid DeepSeek logos and brand assets and the name
"DeepSeek Harness" for our product; "built on ideas from DSH" is fine.

## 1. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Core | Port the design into the Python daemon. dsh is not forked; TUI, browser host, gateway and the JSON-RPC protocol stay. |
| D2 | Web UI | A new `snowpea web` (Vue 3) whose **structure is identical** to the dsh web client: regions, seats, sizes, breakpoints, list rules, right-dock behaviour, file-link rules, HTML-preview modes. React code is not copied; framework-free logic is ported as code. Colours and icons are snowpea's. |
| D3 | IDE | The IDE becomes an Electron shell around `snowpea web`, as dsh desktop wraps its web app. The inspector is removed. A conversation has top tabs (Chat / Trajectory), as in dsh. |
| D4 | Rewind | Keep checkpoint restore; add dsh-style fork ("branch from here" into a new session). |
| D5 | Order | Context engine first (task completion), measured with the agent benchmark before and after. |
| D6 | Docs | Every phase ends with the manual (en, ko, es, ja, zh-CN), `docs/protocol.md`, `ARCHITECTURE.md`, IDE screenshots, agent.snowpea.ai (landing, docs, screenshots) and the qai deployment updated. Screenshots are scripted, never hand-made. |

## 2. What dsh does that we adopt

Paths are in the dsh repo.

**Context.** The system prompt is a list of named sections ordered by one central table
(`packages/core/system-prompt/src/index.ts`, `SECTION_ORDERS`); a section that renders empty
is dropped, so tool guidance disappears with its tool. Facts that change (working directory,
approval policy, plan, time) are *contexts*, a second ordered list, rendered into one
user-role `<system-reminder>` snapshot that is appended **only when the ordered
`{name, text}` list changed** and says it replaces earlier versions
(`packages/core/agent-loop/src/runtime-context.ts`). Every injected message carries its
producer and a form: `instructions`, `catalog`, `snapshot`, `notice`, `relay`, `recall`
(`packages/llm/llm/src/message.ts`).

**Tool results.** `{isError, value, content, meta, additionalContexts?, concludesTurn?}`:
`content` is what the model reads, `meta` is for the UI only, `additionalContexts` rides
reminders on the result, `concludesTurn` ends the loop. A post-execute hook may accept,
replace or block with corrective feedback (`packages/core/tools/src/contracts.ts`). Large
results spill to a file with a head/tail preview and an exact read/grep instruction
(`packages/spill/spill-policy`).

**Finishing tasks.** `agent/turn-stopping` lets a listener steer one more step (verification
gates are built on it). A goal driver keeps an active goal going in rounds, demands evidence
before `complete`, allows `blocked` only after three rounds of the same condition, and caps
rounds (`packages/goal/*`). Repeated identical tool calls get reminders at 3/5/8.

**Compaction.** Prune tool results first (no model call), summarise only if that was not
enough, with a fixed 8-section template, keeping a priced recent tail; one maximal
reduction and retry on overflow (`packages/compaction/compaction-basic`).

**Plugins.** Every registration returns a disposer tracked per plugin, so unloading is
undoing; named interception points with a fixed dispatch mode (`tools/pre-execute`,
`tools/post-execute`, `agent/turn-stopping`, `approval/request`, `fs/*-intent`); registries
for tools, sections, contexts, commands, skills and MCP layered by scope; a plugin's config
schema becomes its settings form; a market is one static `index.json`. dsh has no plugin
sandbox; untrusted code needs process isolation, which we defer.

**Sessions and trajectory.** "Trajectory" is a view, not a data model: a turn-aware table
of the same event log the chat reads, with timing per model call
(`packages/client/ui-trajectory`). The model is an append-only log with contiguous `seq`,
explicit `turn/start|end` and `step/start|end`, the settled assistant stream instead of
deltas, failed attempts kept as `assistant/attempt`, compaction as an event that *replaces*
a range (`surfaceOp`) so model history is a fold, projections with versions and caches, a
subagent as its own session with a parent link, fork into a new session at a turn boundary,
workspaces keyed by canonical path (`docs/subsystems/session*.md`).

**UI.** See §5.

## 3. Phases

Each phase is releasable on its own and is done only when its tests pass, the benchmark (where
named) does not regress, and §6 is done for it.

### Phase 1 — Context engine (core)

| Item | What | Replaces / reuses |
|---|---|---|
| P1.1 | **Section registry**: `PromptSection(name, order, render(ctx) -> str)` registered with a disposer; order from one table `SECTION_ORDERS`; empty renders drop out; the system prompt is assembled from it. Byte-identical output to today for the default set is the first acceptance test. | `prompts/compose.build_tiers`, `agent.build_system_prompt` |
| P1.2 | **Runtime context providers**: `ContextProvider(name, order, form, required, render(session) -> str)`; one `<system-reminder>` snapshot appended as a user message only when the ordered `(name, text)` list changed, after compaction, or on first turn; text says it replaces earlier versions. Per-turn recall stays a `recall`-form note on the turn's own message. | `agent.turn_context` (plan line, plan-auto note, context pressure, host note), `pending_notices` |
| P1.3 | **Message provenance**: `ChatMessage.source = {producer, form}` stored and sent through the protocol so surfaces can render and compaction can drop superseded snapshots. | ad-hoc prefixes (`_SYNTHETIC_PREFIXES`, `_STEER_PREFIX`) |
| P1.4 | **Tool result contract**: `ToolResult` gains `contexts: list[ContextNote]`, `concludes_turn: bool`, and a `post_execute` hook that can accept, replace or block with feedback; `meta` stays UI-only. Spill is the single path for oversized output (head/tail + "read with offset/limit or grep <path>"). | `ToolResult`, `tools/output_spill.py`, `repeat_guard` notes |
| P1.5 | **`turn.stopping` hook**: one ordered listener chain that may steer another step with a reason; verify-on-stop, open-todo nudge, plugin Stop hooks and the budget probe become listeners. | `verify_gate.nudge`, `skills/hooks.stop`, loop's inline checks |
| P1.6 | **Goal driver**: `goal.set/update/clear` RPC and tools; `GoalSnapshot{id, revision, objective, phase, blocked_reason, max_rounds}`; rounds while active; evidence before complete; blocked after 3 identical rounds; no re-arm after resume. | `/ralph` keeps working; goal is the generic form |
| P1.7 | **Two-stage compaction** — already covered, no change. snowpea prunes old tool output in every outgoing request once the window is known (`build_messages` → `prune_old_tool_outputs`), and the auto-compaction check measures that pruned request, so pruning is always tried before a summary; a stored-history prune stage was built, found redundant, and reverted. The summary template (`workflows/compaction`) is richer than dsh's 8 sections and already merges a prior summary. The P1.2 snapshot is re-sent after every compaction. | `agent.build_messages`, `compaction.compact_session` |
| P1.8 | **Benchmark** — done 2026-11-14: the five harness missions, v0.2.86 vs Phase 1 HEAD, 2 runs each, interleaved one at a time on the same server (`local` → glm-5.3-flash), each version in its own throwaway home. Both passed 10/10. Totals per version (sum of per-mission means): rounds 33.5 → 33.0, tool calls 16 → 15, input tokens 333 043 → 328 401 (−1.4 %), output tokens 4 591 → 5 043, wall time 161.6 s → 153.5 s. No regression; the differences are within run-to-run noise for missions this short. The Phase 1 effects (snapshot only on change, goals, stop chain) show on long tasks; a long-task mission set is to be added before Phase 2 is measured. | `scripts/bench-snowpea-hermes` (local, not committed) |

Protocol: additive 1.43.x (`message.user` gains `source`; `goal.*` RPC and `goal.changed`
event; `tool.result` gains `concludesTurn`). No client breaks.

### Phase 2 — Session log v2 and Trajectory (core + protocol 2.0)

- Event log with contiguous `seq`, `turn.started/turn.done` plus `step.started/step.done`,
  `turn` and `step` on every event; the settled assistant message and `assistant.attempt`
  stored, deltas streamed but not stored; `request.header` when tools/config change.
- Compaction as a replacing event (`surface_op`), model history = fold; the transcript
  still shows replaced text.
- Projections `init/apply` with version and high-water mark, cached (title, todos, goal,
  plan, subagent catalog, usage, changed files).
- Subagent = its own session row with `parent_session_id`, `origin`, `depth`; parent gets a
  catalog event.
- Fork at a turn boundary into a new session (`session.fork`); checkpoint restore kept.
- Workspaces: registry keyed by canonical path; original `cwd` immutable, current working
  directory as an event.
- Crash recovery appends closers; force-write before model calls and before side effects.
- Protocol 2.0 with a 1.x compatibility layer for the TUI and the browser until they move.

### Phase 3 — Plugin API (core)

- `PluginContext` handed to a Python plugin: `tools.register`, `prompt.section`,
  `prompt.context`, `commands.register`, `mcp.register`, `hooks.on(<point>, fn)`,
  `settings.schema` — each returns a disposer recorded on the plugin; disable/enable/reload
  without restarting the daemon.
- Interception points: `tool.pre_execute`, `tool.post_execute`, `turn.stopping`,
  `step.pre`, `approval.request`, `fs.write_intent`, `system_prompt.assemble`.
- Claude-style plugins, `hooks.json`, skills, agents, commands and `.mcp.json` load through
  adapter plugins, unchanged for users.
- Settings forms generated from a plugin's schema (IDE and web).
- Market: static `index.json` (format 1, per-version `engines.snowpea`) and a generator.

### Phase 4 — snowpea web (Vue 3)

Identical structure to the dsh web client (§5), served by the daemon (`snowpea web`), on the
Phase 2 protocol; Chat tab first, Trajectory tab once Phase 2 is in.

### Phase 5 — IDE on snowpea web

The IDE loads `snowpea web` in its window; inspector, `SessionCard`, `ChangesPanel`,
`SubagentTree`, `FilesTree` are removed (their functions are right-dock tabs); existing
previewers (docx, pptx, sheet, notebook, HTML, markdown, image, code) become document-preview
renderers. Real OS windows: a session or a dock tab can be popped out into its own window,
synchronised through the daemon's streams (dsh itself has one window plus in-app splits and
floats, which we also keep).

## 4. Protocol plan

Phase 1 is additive (1.43.x). Phase 2 is 2.0: event names and storage change; the daemon
serves 1.x clients through a translation layer until the TUI and the browser webui have moved.
The browser session (`snowpea-browser`) is told each protocol step before it is tagged.

## 5. UI parity spec (Phases 4–5)

Reference: dsh `packages/client/ui-*`, `docs/subsystems/{slots,sidebar-right,conversation,
web-client,client-modules}.md`, and the 77 text UI snapshots in `snapshots/web/*/ui.expected.md`,
which become our acceptance cases. Reference screenshots are captured from a local dsh web run
and compared side by side with ours.

- Frame: left sidebar 280 px (264–420, rail 56, auto-collapse < 1024, drawer < 768, Mod+B);
  main; right column opens at 45 %, keeps user width, max 70 %, shrinks to 300 px to keep
  400 px for main, then closes; fullscreen mode; bottom band; overlay layer.
- Left sidebar: brand row, New Session, panel list, workspace groups (by workspace / tree /
  flat), 5 rows then "Show more" (+5) with a watermark covering running, unread and open
  rows, pinned first, last-updated or manual order, indicators pending > running > unread
  (green, red on failure), Mod+K search (local first, server content search debounced
  250 ms), row menu (pin, rename, read/unread, fork, copy id, reveal, archive with
  stop-and-archive).
- Conversation: header with lineage breadcrumbs and **Chat / Trajectory tabs**; chat nodes
  user, steering, assistant step, tool call, command, turn tail; tool views by tool name with
  a generic fallback; reasoning collapsible; turn tail holds changed files, deliverables and
  the plan card.
- Trajectory: rows system, user, context, compacted, message, tool, subtool; turn and step
  bands; timing per model call (TTFT, decode, retries, usage); drag-select overview;
  virtualised paging from the tail.
- Right dock: one surface per session saved per session; split panes, floating panels,
  history; tab types guide, document preview (code, markdown, html, image, pdf, sheet,
  office, notebook), files, browser, subagent chat, plan, changes review, terminal;
  `openResource(address, {line})` focuses an existing tab for the same address.
- File references: `./path`, absolute and Windows paths, `#L24`, `#L24-L30`, `:24`,
  `:24-30` in markdown links; inline code only when it names a file the turn produced;
  `@file` chips and tool rows open the same way.
- HTML preview: static mode (DOMPurify, CSP, no scripts, no external loads) and script mode
  (Blob iframe `sandbox="allow-scripts"` only, relative JS/CSS inlined with caps 4 MiB per
  asset, 32 MiB total, 64 assets), switched by a setting.

Ported as code (MIT): the dock engine (operations, planner, geometry, history), the resource
address grammar, tab claim ranking, the file-link parser, the HTML sandbox recipe, layout
numbers and token scales.

## 6. Documentation and deployment per phase

- Manual: `docs/manual/{en,ko,es,ja,zh-CN}` — new concepts, screens and shortcuts.
- Developer docs: `docs/ARCHITECTURE.md`, `docs/protocol.md` (generated), this file's status.
- Screenshots: one scripted scene list (Playwright, extending the IDE's
  `playwright.shots.config.ts`) in Korean and English, reused by the IDE docs
  (`snowpea-ide/docs/screenshots`), the manual and the site.
- agent.snowpea.ai (`snowpea-site`): landing feature copy and screenshots, docs pages, download
  notes — deployed when the owner asks.
- qai: the daemon updated to the release when no session is running there; the browser site
  (`snowpea-browser-site`) is the browser session's, so it is handed the changes and the
  screenshots.
- Releases only on the owner's request.

## 7. Risks

- Prefix-cache regressions from moving prompt text: P1.1 must be byte-identical first, and
  every later change states its cache effect (dsh's "Model Experience" rule).
- Local models that handle `<system-reminder>` user messages poorly: the benchmark runs on
  the local models we ship with, not only hosted ones.
- Protocol 2.0 churn for the browser and the TUI: the 1.x layer stays until both have moved.
- Python has no declaration merging: registries carry string keys with a generated catalog
  and runtime validation.
