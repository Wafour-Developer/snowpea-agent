# Deviations — CORE-prompts (the prompt library)

Work for `.omc/research/prompt-quality-review.md`: the twelve gaps, the §5
library layout and the §6 order. Recorded here per
`docs/design/deviations/README.md`.

1. **The stable-tier budget is 2200 tokens, not the 1200 the review suggested.**
   The review specifies verbatim text for gaps 1, 6, 7, 8, 9 and 12, and that
   text alone is 717 words (~932 tokens at the `words × 1.3` approximation the
   budget is written against). The stable tier also carries the vendor layer,
   `config_rule.md`, `search_honesty.md`, the mode file and, for a subagent, the
   role file; the measured range is 1227–2068 tokens, worst case
   plan + small-local + executor. 1200 was unreachable without cutting the prose
   that is the point of the change, so the ceiling sits above the intended
   library with headroom and keeps doing its actual job — the library cannot
   grow unbounded unnoticed. Context (343 / 800) and volatile (86 / 600) are as
   suggested. The constant and this reasoning live at the top of
   `tests/test_prompts_compose.py`.

2. **A denial no longer ends the turn; three do.** Gap 3 asked for the refusal
   to come back as a tool result so the model can adapt. `loop.py` now appends
   it and continues, with `MAX_DENIALS_PER_TURN = 3` stopping a model that only
   ever re-sends the refused call. The turn still ends with reason `"denied"`
   when the budget trips, and the `mode_denied` / `approval_denied` error events
   are unchanged, so no surface has to learn anything new.

   **This changed an asserted contract.** Nine existing tests encoded "one
   denial ends the turn" — in `test_session_loop.py`, `test_permission_matrix.py`
   and `test_approval_multichannel.py`. They were updated to assert the new
   contract (the tool never runs, the error event still fires, the refusal
   arrives as a failed `tool.result`, the turn goes on) rather than deleted, and
   `test_prompts_behaviour.py` adds the regression test for the adaptation and
   for the three-denial cap. `job.lastStatus == "denied_by_timeout"` is derived
   from the approval decision rather than the turn reason and was unaffected.

3. **A workflow brief prepends a three-clause digest, not the whole of
   `base.md`.** Gap 11 asks that each brief carry the base rules. The child
   already receives `base.md` in its own system prompt, so repeating 717 words
   inside the task would spend the worker's window twice for no new instruction.
   `compose.BRIEF_RULES` is a faithful digest — read before you edit, change only
   what the task needs, run the project's checks, never report a result you did
   not produce — substituted into `${BASE_RULES}`. `base_rules=False` turns it
   off per call.

4. **The JSON-emitting workflow prompts get no rules prepend.** `ralph-prd`,
   `ultrawork-split`, `team-plan`, `agent-generate`, `skill-learn` and
   `compaction` are one-shot "answer with a single JSON object and nothing else"
   system prompts; prose rules in front of that instruction fight it. They take
   the gap-11 *textual* fixes only (the verify-command rule, the same-file
   independence rule). The four real subagent briefs — `ralph-story`,
   `ralph-review`, `team-task`, `team-conflict` — do prepend.

5. **`AgentDefinition.prompt` is now appended to the rules rather than replacing
   them.** `subagent.py` used to assign it to `child.system_prompt`, which
   replaced `BASE_PROMPT` outright, so a named agent lost every coding-discipline
   rule the base prompt carries. It is now composed as a persona after the role
   file. `compose.build_tiers(identity=...)` keeps the old replace semantics for
   a caller that genuinely wants a bare prompt; nothing in core passes it.

6. **Every child gets the subagent preamble, definition or not.** Gap 4. A
   definition whose name matches a file in `prompts/roles/` also gets that role
   (`subagent.role_file`); one that does not simply composes without it. Before
   this, `/ralph`, `/ultrawork` and `/team` workers ran the bare base prompt with
   no report contract at all.

7. **The environment block is cached for 30 seconds per session.**
   `compaction.prompt_messages` rebuilds the whole prompt purely to measure it,
   on every turn and every `context` event; probing git and reading AGENTS.md
   each time would put a subprocess on an accounting path. The cache is keyed on
   session id and working directory, and `agent.invalidate_environment()` clears
   it. A git failure of any kind drops the workspace block entirely rather than
   reporting a guess.

8. **Only the date is in the prompt, never the time.** The stable and context
   tiers have to stay prefix-cacheable and a clock in the text defeats that on
   every provider. The block says the date is fixed and points at a tool for the
   exact time.

9. **`agent.replyLanguage` defaults to `"auto"`, which emits no extra rule.**
   `base.md` already says to answer in the language the user wrote in, so
   `"auto"` needs no line; an explicit tag emits a directed override naming the
   language, and `/ralph` and `/team` thread the resolved language into their
   workers' briefs because a child cannot see the parent's settings. Prompt files
   stay in English: translating a system prompt degrades instruction following,
   and the reply language is what the user actually cares about.

10. **Vendor layers are keyed on the provider preset, not the model id.** The
    mapping is `compose.VENDOR_CLASS_BY_PROVIDER`; an unrecognised provider falls
    to `openai-family`, the safer default for an unknown hosted endpoint.
    `vendors/anthropic.md` is deliberately empty — the model already has that
    posture and the words would be noise.

11. **Tool descriptions live in `prompts/tool_descriptions.py`, not inline in
    `tools/*.py`.** A description is prompt text the model reads every turn, and
    keeping it beside the schema means prompt review never sees it. `tools/*.py`
    imports the constants by name; the schemas are untouched. Seven tools were
    rewritten (gap 5 names six and `glob` was added in the same style).

12. **`build_system_prompt` and `build_messages` gained a keyword-only `core`.**
    The reply-language setting lives on `Core.settings` and both call sites
    (`loop._drive`, `compaction.prompt_messages`) already hold one. It defaults
    to `None`, which resolves the language to `"auto"`, so no other caller had to
    change. The context-fill ratio needs no `core`: it comes from
    `session.context_used / session.context_window`, which `emit_context` writes
    at the end of every turn.

13. **The protocol was not regenerated by this work.** `agent.replyLanguage` is
    a settings field and is not projected into `protocol.py`, `sdk/src/protocol.ts`
    or `docs/protocol.md`; `uv run python scripts/gen_protocol.py --check`
    reports no drift attributable to these changes.
