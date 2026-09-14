# M15 — Agent intelligence policies (orchestration, skills/MCP guidance, context injection, choice UX)

Status: **binding for stories CORE-policies, CORE-skills-index, CORE-delegation, TUI-choice, IDE-choice**.
Approved by the user on 2026-09-14 after a code-level survey of snowpea, Hermes Agent
(`/tmp/hermes-ref`), opencode (`/tmp/opencode-ref`) and oh-my-claudecode. Ported logic keeps
its MIT provenance in `docs/design/deviations/`. Findings that motivated each section live in
the session plan; only the decisions are recorded here.

Decisions taken with the user: an automatic reviewer pass runs **only on request** (`/review`,
`/ralph`, or an explicit ask); the choice UX is unified across **all pickers** on both surfaces.

## Design
Principle: policies live in three places, each with one owner — **prompt text** (`prompts/`),
**code guards** that make a rule unfakeable (tools/loop), and **surface UX** (TUI/desktop) that
renders the same contracts. Everything below is additive to protocol 1.5.0.

### A. Tool-use discipline (core prompts + one code guard)
Files: `prompts/base.md`, new `prompts/fragments/execution.md`, `prompts/vendors/*.md`,
`prompts/compose.py`, new `tools/file_state.py`, `tools/edit_file`/`write_file`, `agent/loop.py`.
1. New stable-tier fragment **"Working discipline"** (ported from Hermes `TASK_COMPLETION_GUIDANCE`,
   `<tool_persistence>`, `<mandatory_tool_use>`, `<act_dont_ask>`, `<verification>`,
   `<external_state_verification>`, opencode `beast.txt:24/28/79`): plan before the first call
   (one sentence of intent for non-trivial work, then the calls), keep going until done **and
   verified**, never answer from memory what a tool can check, act on the obvious default,
   read back external writes, "a successful tool call is not a successful task", stop and report
   blockers honestly, never fabricate output. Kept short (≤ 35 lines); vendor overlays keep the
   family-specific enforcement (`small-local.md`, `openai-family.md` gain the "call it in the
   same response" line; `anthropic.md` stays empty).
2. **Exploration routing** (opencode `anthropic.txt:79-86`): "not a needle query → delegate to
   the `explore` agent (read-only) instead of grepping in the main context" — added to base.md
   with the built-in read-only `explore` definition (see C).
3. **Read-before-write guard in code** (Hermes `file_state.py`): `tools/file_state.py` tracks
   per-session `(path → last read hash/range, last writer)`; `edit_file`/`write_file` refuse
   (`stale_file` error with the exact reason) when the file was never read, read partially, or
   written by a sibling subagent since the read; `read_file` full reads clear it. Prompt keeps
   the prose rule; the guard makes it true. Test with the fake provider.
4. **Tool result hygiene**: shell/grep results over N lines are head/tail trimmed with a
   `read_file`-style pointer to the spilled full output (`$SNOWPEA_HOME/cache/tool-output/<id>`),
   same pattern as Hermes delegation spill — one helper reused by delegation (C).

### B. Skills: index in the prompt, view on demand, results folded as tool results
Files: `prompts/fragments/skills.md` (new), `prompts/compose.py` (context tier), `skills/loader.py`,
`tools/skills_tools.py` (new `skill_view`), `session/compaction.py`, `prompts/tool_descriptions.py`.
1. **Skills index** in the context tier (Hermes `_render_skills_index`): `## Skills` — every
   visible skill as `- name: description` (≤ 60 chars), grouped `[project]` / `[global]` /
   `[plugin:x]` / `[builtin]`, with the Hermes preamble ("scan before replying; if a skill matches
   or is even partially relevant you MUST load it with `skill_view` and follow it; skills encode
   how it is done here; only proceed without one if none is relevant"). Cached with the tool list;
   rebuilt on reload.
2. New tool **`skill_view {name}`** returns the SKILL.md body (+ linked files list) as a tool
   result; `/name` slash invocation stays. Repeat views return a stub.
3. **Compaction-safe**: bodies > 5 000 chars are replaced during compaction by
   `[SKILL_PRUNED: reload with skill_view("name")]`; freshly loaded skills are protected; base.md
   gets the reload rule. Implemented in `session/compaction.py` where tool results are summarised.
4. **Results back into context**: a `/skill` command turn and a `skill_view` call both end as
   ordinary assistant/tool messages (no special envelope) — document this as the contract; the
   "save as skill / lessons not logs" nudge text ported from Hermes `_LESSON_LAYER_BLOCK` into
   `workflows/skill-learn.md`.
5. **Load timing fix** (user requirement + local flux defect):
   - `SkillLoader.workdirs()` → scan **stored + live** session workdirs (from `store.list_sessions`)
     and the IDE's `ide.projects` list is not visible to the core, so: `session.create`/`restore`
     call `core.skills.reload_workdir(workdir)` (incremental scan of `<workdir>/.snowpea` and
     `.claude`, registering commands/agents/skills for that project) **before** the first turn;
     `Daemon.start` scans `$SNOWPEA_HOME/skills`, `plugins` and all workdirs of sessions still in
     the store (closed included) so project skills exist before any session opens.
   - `_scan_bundle` also accepts a **root `SKILL.md`** (a bundle that *is* one skill) — fixes the
     existing `~/.snowpea/plugins/flux/SKILL.md`; `skill_install` of a bare skill directory
     installs to `$SNOWPEA_HOME/skills/<name>/` (global skills root) instead of `plugins/`, and
     migrates on load: a plugin dir with only a root SKILL.md is registered as a skill and logged.
   - Global MCP: `Daemon.start` calls `mcp_client.sync_tools(core, workdir=None)` so
     `$SNOWPEA_HOME/.mcp.json` servers start at boot; project servers keep starting at
     `session.create` (already correct). `skill.list`/`tool.list` reflect both immediately.
   - `commands.changed` is broadcast after each reload so the TUI palette and IDE update.

### C. Delegation: single owner per task, pipelines instead of fan-out on the same work
Files: `prompts/tool_descriptions.py` (delegate_task), `prompts/base.md` §subagents, `agent/subagent.py`,
`tools/delegate.py`, `agent/team.py`, `commands/ultrawork.py`, new `agent/definitions/explore.md` +
`reviewer.md` built-ins, `docs/design/m6-m7-*.md`.
1. **Policy text** (opencode `task.txt:14,33`, Hermes description): one task → one agent; after
   delegating, never duplicate the work in the main context and never give the same task to a
   second agent while the first runs; split by **non-overlapping files/areas**; sequence
   dependent steps (implement → review → fix) instead of parallel identical briefs; the child's
   report is a self-report — verify external effects; relay in your own words; say whether the
   child must write code or only research, and how to verify.
2. **Code guard — task dedupe** (`agent/subagent.py`): a normalised task fingerprint (agent +
   task text) per parent session; a second `delegate_task` with the same fingerprint while the
   first is running returns `duplicate_task` with the running child's id instead of spawning
   (`force: true` for deliberate best-of-N, off by default). `/ultrawork` splitter prompt gains
   "subtasks must not touch the same files"; the split result is validated for overlapping
   `files` and merged when they overlap.
3. **File-ownership guard** for parallel siblings: `file_state` (A3) records the writer; a sibling
   writing a file another running sibling has written gets `file_owned_by_sibling` and must
   report instead — the parent then serialises. Team mode already uses worktrees; document that
   parallel non-team delegation without worktrees is limited to disjoint files.
4. **Built-in agents**: `explore` (read-only tool allowlist, thoroughness levels, opencode/OMC)
   and `reviewer` (read-only, returns APPROVE/REJECT with findings, OMC critic/verifier). Prompt
   rule: exploration → `explore`; a `reviewer` pass runs **only when asked** (`/review`, `/ralph`,
   or the user's request says verify) — decided by the user 2026-09-14; the prompt still says the
   parent never self-approves what it did not verify itself.
5. **Result folding**: keep `render_report` shape; add the Hermes spill pointer for long reports
   (A4) and a one-line "what to do next" hint by reason. Team mode gains an optional reviewer
   stage before merge (`team.review: bool`, default false — review is opt-in everywhere).

Decisions taken with the user (2026-09-14): automatic reviewer = **only on request**
(`/review`, `/ralph`, explicit ask); choice UX unification = **all pickers** on both surfaces.

### D. Memory + AGENTS.md scope rules (review + small changes)
Files: `memory/retrieval.py`, `memory/digest.py`, `prompts/fragments/memory-guidance.md` (new),
`prompts/environment.py`, docs m5 §1b, CORE-context-files.
1. Keep today's scopes and digest (project → user → global) and the Hermes tier placement
   (skills index before memory in the volatile/context band so the index stays cacheable).
2. Add Hermes memory guidance (declarative facts not instructions; skills first — procedures
   belong in skills, memory is for facts that apply to every session; stale-within-a-week →
   session history; never persist negative claims about tools; no incident narratives).
3. Digest header shows usage (`N/30 entries, K/6000 chars`) so the model sees fullness.
4. AGENTS.md: confirmed current behaviour (first-type-wins, chain, up-front nested, on-demand
   for the rest, invalidation) matches Hermes + opencode; only change: the on-demand attachment
   dedupes by content hash (symlinks/copies) and refuses paths outside the workdir tree.
5. The "where to save?" question stays (user's explicit ask), with `memory.askScope` documented
   as the switch.

### E. MCP presentation
Files: `prompts/fragments/tools.md`, `prompts/compose.py`, `tools/mcp_client.py`.
Group MCP tools under `## MCP: <server>` headings in the tool fragment with the server's
description/permission tag; register `list_resources`/`read_resource`/`list_prompts`/`get_prompt`
stubs only when the server advertises the capability (Hermes); scan server-supplied descriptions
for injection markers and log (never block).

### F. Unified choice UX — one contract, both surfaces
Contract (core, `docs/design/m15-choice-ux.md`): every choice the daemon asks goes through the
question queue with the same shape — `header`, `question`, `options[{label, description?,
preview?}]`, `multi`, `allowOther`, recommended option **first** and suffixed `(recommended)`,
Other row added by the surface, declined ≠ timed out. Approvals keep their own four-way shape
(once / session / project / deny) plus a **reject-with-reason** free text (opencode).
TUI (`tui/src/components/*`): one `ChoiceList` primitive used by QuestionPrompt, ApprovalPrompt,
ConfirmMenu, ModelPicker, McpCatalogPicker, ToolChecklist, the `/skill create` and `/mcp add`
forms, and the setup wizard's `_menu_pick` (core `setup/ui.py`): ↑↓ (j/k) move, Space toggles in
multi, Enter picks (single) / confirms (multi) / opens free text on Other, 1-9 jump, ←→ and
Tab/Shift-Tab switch question tabs, Esc declines/cancels, footer hint always shown; accordion
marker (✓ ▸ ·) for multi-question batches like Hermes, tabs kept for ≤ 5. Fix the ApprovalQueue
hint (Ctrl+R). Setup wizard gains ←→ for tabs and the same hint line.
Desktop (`QuestionModal.vue`, `ApprovalModal.vue`, `NewSkillSheet`, `McpServerSheet`, setup
wizard steps): the same keyboard map (↑↓, Space, Enter, 1-9, Tab between question tabs, Esc),
recommended option pre-selected, "Other…" last, Review/Confirm tab for batches, approval modal
gains "Reject with a reason". One `ChoiceList.vue` in `ui/`.

### G. `/plan` workflow prompt (opencode `plan-mode.txt`)
`prompts/modes/plan.md` gains the 5-phase shape (explore ≤ 3 read-only agents → design → review
questions → written plan → hand-off), ending only with a question or the plan.

### H. Docs
`docs/design/m15-agent-policies.md` (this design, binding), updates to m6-m7, m9, m5, m12;
manual pages `agents.md`, `plugins.md` (skills index/skill_view), `memory`, `threads.md`
(questions), `setup.md`; deviations for anything ported from Hermes/opencode (MIT notices).

### Sequencing (agents, ≤ 3 concurrent, implementation = opus)
1. Core-1: A (prompt fragments + file_state guard) and B5 (load timing, flux fix) — first, they
   unblock everything the user sees immediately.
2. Core-2: B1-B4 skills index/skill_view/compaction + E MCP grouping + D memory guidance.
3. Core-3: C delegation policy + dedupe + explore/reviewer built-ins + team reviewer.
4. TUI: F ChoiceList unification + wizard keys; then IDE: F ChoiceList.vue + approval reason +
   modal keymap.
5. G + H alongside.
