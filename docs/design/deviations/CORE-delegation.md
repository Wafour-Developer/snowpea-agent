# Deviations — CORE-delegation (M15 §C)

Ported logic and the places where snowpea's implementation departs from
`docs/design/m15-agent-policies.md` §C or from its upstream sources. Recorded per
`docs/design/deviations/README.md`.

## Provenance

Three upstreams, all MIT licensed. The MIT licence permits reuse with
attribution; nothing was copied verbatim — the rules were re-written in
snowpea's prompt voice and the code re-implemented against snowpea's session
model.

| snowpea file | source | what was taken |
|---|---|---|
| `prompts/tool_descriptions.py` (`DELEGATE_TASK`) | opencode `packages/opencode/src/tool/task.txt` :13-18, :33-34 | the anti-duplication wording ("once you have delegated work to an agent, do not duplicate that work yourself"), "launch multiple agents concurrently", "tell the agent whether you expect it to write code or just to do research… how to verify its work", and the "when NOT to use" list |
| `prompts/tool_descriptions.py` (`DELEGATE_TASK`) | Hermes `tools/delegate_tool.py` :528-558 (`_DESCRIPTION_HEAD`) | the USE FOR / DO NOT USE FOR shape, "child summaries are SELF-REPORTS, not verified facts" with the verifiable-handle rule, and "never wait or poll on transcripts… for a child" |
| `agent/definitions/explore.md` | opencode `agent/prompt/explore.txt`; OMC `agents/explore.md` :45-53 (`<Context_Budget>`) | the read-only search role and thoroughness levels from opencode; the context-budget rules (symbols before a long file, windowed reads, ≤ 5 parallel reads, stop after two diminishing rounds, absolute paths) from OMC |
| `agent/definitions/reviewer.md` | OMC `agents/code-reviewer.md` and `agents/verifier.md` | evidence discipline ("findings without evidence are opinions"), severity ordering, never approving what was not read, and the final-response-is-the-deliverable contract |
| `agent/subagent.py` (dedupe), `tools/file_state.py` (`sibling_label`) | OMC `agents/executor.md` :33 ("Work ALONE for implementation… All code changes are yours alone") | the single-owner rule, turned from an instruction into a refusal |
| `tools/delegate.py` (`render_report`) | Hermes `tools/delegate_tool_results.py` :187-294 (`_trim_summary_with_footer`) | head/tail trim of an over-long report with a footer giving the exact `read_file` offset for the omitted middle |

## Deviations

1. **The single-owner rule is a refusal, not only a sentence.** OMC and opencode
   both state it in prompt text and trust the model. snowpea states it *and*
   refuses the second delegation (`duplicate_task`) and the sibling's write
   (`file_owned_by_sibling`). A rule the model can ignore at midnight is not a
   rule; the prompt text stays because a refusal explains nothing about what to
   do instead.

2. **The fingerprint is (agent, task), not a semantic match.** Hermes has no
   dedupe at all and opencode leaves it to the model. Normalising whitespace and
   case over the first 400 characters catches the observed failure — the same
   brief re-sent while the first child runs — without ever refusing two
   genuinely different briefs, which would be the worse error. `force: true`
   exists for deliberate best-of-N.

3. **Spill by lines, not by characters.** Hermes budgets an over-long summary in
   characters against the parent's remaining context, computed per call.
   snowpea reuses `tools/output_spill.spill()` (CORE-policies §A4) with a fixed
   120-line head and 40-line tail, because one spill helper that every tool
   shares is worth more than a second, cleverer one — and a report is meant to
   be a dozen lines, so the threshold is a backstop rather than a budget.

4. **Built-in agents are shipped as definition files, not synthesised.**
   `builtin_agent_definitions()` already exposed `prompts/roles/*.md` as
   agents, but a role file has no frontmatter and so cannot carry a tool
   allowlist. `agent/definitions/*.md` are ordinary agent markdown parsed by the
   same `parse_agent_md`, overriding the role-derived entries by name — which
   also means `explore` and `reviewer` are read-only in fact rather than by
   instruction. The existing `explorer` and `critic` roles are left as they are:
   removing them would break anyone delegating to them by name.

5. **The reviewer never runs on its own.** OMC's pipelines review by default.
   The user decided on 2026-09-14 that a review is a model turn nobody asked
   for: `/review`, `/ralph`'s closing pass and `team.review: true` are the only
   three ways one starts.

6. **`team.review` re-queues instead of reverting.** The design says a
   `REQUEST_CHANGES` verdict "re-queues the task once to the same worker with
   the findings". The merge itself is left in place: other tasks may already
   have merged on top of it, and reverting a commit the board depends on turns
   one reviewer finding into a conflict storm. The worker fixes forward in its
   own worktree, which is the same path a conflict re-queue already takes.

7. **The policy text lives in the tool description, not in `base.md`.** §C1
   names "`prompts/base.md` §subagents" as one of its two homes. `base.md` has
   no §subagents section — what it has is one Language paragraph saying a
   child's report reaches the user only through the parent — and the delegation
   rules are only ever needed by a turn that can see `delegate_task` at all. So
   the whole policy sits in `DELEGATE_TASK`, which the parent reads in the tool
   fragment of every turn, and `base.md` is untouched. The exploration-routing
   rule that §A2 asks for is already in `prompts/fragments/execution.md`
   (CORE-policies) and now resolves to a real read-only agent.

8. **`/ultrawork` merges overlapping subtasks rather than re-splitting.** The
   design asks for the split to be "validated for overlapping `files` and merged
   when they overlap". Asking the model again would cost another round trip and
   could loop; folding the two briefs into one, with a line saying why, is
   deterministic and costs nothing. The merge is logged and reported to the user.
