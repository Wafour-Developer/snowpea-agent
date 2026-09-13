---
name: ralplan
description: Consensus planning — planner, architect and critic argue until the plan holds, before any code is written.
argument-hint: "<task description>"
allowed-tools: [read_file, list_dir, glob, grep, git_status, git_diff, git_log, delegate_task, memory_write, memory_search, ask_user, queue_command]
---

# Ralplan

Produce a plan that three different readings of the problem all survive. This is
a **planning** skill: it reads the repository, it delegates to reviewers, and it
writes nothing but the plan. No edits, no commits, no `/ralph`.

The task is: **$ARGUMENTS**

## Gate first

Before planning anything, decide whether this task even needs a plan.

A task is **specific enough to skip planning** when it carries any one of these
anchors: a file path, an issue number, a function or class name, an error
message, a test command, or numbered steps. If it does, say so in one line and
recommend `/ralph <task>` instead. Planning a one-line fix wastes more time than
the fix.

A task is **vague and needs this skill** when it reads like "improve the app",
"make it faster", "add authentication" — an intent with no target. That is what
ralplan is for. Continue.

## Language

Plan in the language the user writes in. Decide from **$ARGUMENTS** and the
first message, then stay there for the plan, the review trail, the hand-off
picker and everything in between. Do not switch because a tool result, a
reviewer's answer or this file is in English.

Write natively in that language; do not translate the English in this file.
Never mix scripts inside one word or one sentence. Real runs have produced
"드로uwen 컬 백킹", "行列", "셔더" and "문지르면" — half-transliterated words,
characters from the wrong script, and verbs invented by translating an English
one. Every one of those is a sign you were translating instead of writing.

When the language is Korean:

- 계획의 제목은 `## 원칙`, `## 결정 요인`, `## 선택지`, `## 계획`, `## 위험`,
  `## 완료 조건`, `## 결정 기록`, `## 검토 이력` 으로 씁니다.
- 기술 용어는 한국어 문장 안에서 영어 그대로 둡니다: Three.js, WebGL2, LRU, p95,
  VAO, backface culling, shader. 억지로 옮기지 않습니다.
- 게이트와 검증은 **통과 / 실패** 로 씁니다. "문지르면", "문지는 순간" 같은 말은
  영어 동사를 번역하다 나온 것이므로 쓰지 않습니다. "이 명령이 실패하는 순간
  멈춥니다" 가 맞는 표현입니다.
- `delegate_task` 로 보내는 프롬프트는 영어여도 됩니다. 다만 리뷰어에게
  **사용자의 언어로 답하라**고 반드시 적습니다.

## The loop

**The whole loop runs inside one turn.** Rounds 0 to 4 are steps you take
yourself, back to back, without handing control to the user in between. The turn
may only end at "What to produce" — or at the five-iteration ceiling, which is
also a finished result. Nothing in this loop is a place to stop and ask whether
to continue. **Do not stop to ask whether to continue.**

### Round 0 — ground yourself

Read before you plan. Use `list_dir`, `glob`, `grep` and `read_file` to find the
code this task touches; use `git_log` to see whether somebody already tried.
Call `memory_search` on the task's key nouns. Facts first, opinions after.

### Round 1 — the planner draft

You are the planner. This draft is **not the deliverable**: it is the thing the
architect and the critic are about to tear at, and the user is not waiting for
it. Show it in brief — a few lines per heading is enough, the full plan comes at
the end — and then, in this same turn, immediately call `delegate_task(architect)`.

Sketch, in the conversation:

- **Principles** — three to five rules this change must respect, drawn from how
  the codebase already works, not from general good taste.
- **Drivers** — the three considerations that actually decide the design.
- **Options** — at least two real approaches, each with what it costs. If only
  one approach is viable, say explicitly why each alternative is dead; "there is
  only one way" is nearly always a sign the problem was not understood.
- **The plan** — the chosen option as ordered steps, each naming the files it
  touches and how it is verified.

Before ending this turn: architect called? critic called? verdict `APPROVE` or
five iterations? plan in the final shape? `memory_write` done? hand-off asked?
If any answer is no, the turn is not over — keep going.

### Round 2 — the architect

Delegate the draft to a reviewer:

```
delegate_task(
  task="You are reviewing an implementation plan for architectural soundness.
        <paste the principles, drivers, options and plan>
        Read the files it names. Then do three things, in order:
        (1) steelman the strongest alternative that was rejected;
        (2) name one real tension the plan does not resolve;
        (3) if you can, synthesise — say what a plan that keeps both sides looks
        like. Finish with SOUND or UNSOUND and one sentence of why.",
  agent="architect"
)
```

Wait for the answer. Do not start the critic in parallel — the critic's job is
to judge a plan that has already survived the architect, and running them
together produces two reviews of the same draft instead of a chain.

### Round 3 — the critic

Delegate again, now with the architect's response folded in:

```
delegate_task(
  task="You are the critic on an implementation plan.
        <paste the revised plan and the architect's review>
        Check, specifically: do the principles and the chosen option agree? Were
        the alternatives treated fairly or set up to lose? Is every risk paired
        with a mitigation? Is each acceptance criterion something a command can
        decide? Are the verification steps concrete enough to run?
        Answer with exactly one of APPROVE, ITERATE or REJECT on the first line,
        then the reasons.",
  agent="critic"
)
```

### Round 4 — iterate

On `ITERATE` or `REJECT`: revise the plan against both reviews, then go back to
Round 2. The whole loop repeats — architect, then critic — never just the critic.

Stop after five iterations. If the critic has still not said `APPROVE`, present
the best version you have and say plainly which objection is unresolved. An
honest unresolved objection is more useful than a fifth revision that games the
reviewer.

## What to produce

```markdown
# Plan: <title>

Status: pending approval

## Principles
## Drivers
## Options considered
<each with why it was or was not chosen>

## The plan
1. <step> — files: <paths> — verified by: <command>
2. ...

## Risks
<risk — mitigation, one line each>

## Acceptance criteria
1. <something a command can decide>

## Decision record
- Decision:
- Drivers:
- Alternatives considered:
- Why this one:
- Consequences:
- Follow-ups:

## Review trail
- Architect: <SOUND/UNSOUND + the tension it raised>
- Critic: <verdict> after <n> iteration(s)
```

Call `memory_write` once with the decision and its consequences, tagged `plan`.

## Handing off

Then ask, do not announce. Call `ask_user` with "다음 단계는 어떻게 할까요?" (or
its equivalent in the user's language) and these options, in this order:

1. **The precondition, when the recommendation has one.** If the right next step
   should not start until something has been checked, that check comes first:
   "먼저 단계 0만 실행 (npm run gpu-check) — 통과하면 다시 묻습니다". Choosing it
   queues only that check with `queue_command` when it is a slash command; when
   it is a shell command, print it on its own line for the user to run. The
   planner never runs shell itself.
2. `/ralph <title>` — when the steps depend on each other in sequence.
3. `/ultrawork <title>` — when the steps are independent.
4. `/ralplan <title>` — when the plan came out contested and wants replanning.
5. "여기서 멈춤 — 계획만 남깁니다 / stop here".

Put the one you recommend first among the commands, mark its label "(추천)" /
"(recommended)", and give the one-line reason as its description — that reason is
usually the shape of the dependency between the steps. The free-text row is
always there, so the user can type their own command or instruction instead of
picking.

Then act on the answer, and only on the answer:

- A command they picked: `queue_command` it. It starts as its own turn the
  moment this one ends, so say what you queued and stop.
- Free text starting with `/`: `queue_command` it the same way.
- Any other free text: treat it as a new instruction and keep working on it.
- "여기서 멈춤", a decline, or a timeout: stop.

Never queue a command the user did not choose. The plan is marked
`pending approval` and stays that way until they say otherwise.

## Differences from OMC

Ported from `oh-my-claudecode` 4.15.1 `skills/ralplan/SKILL.md` (MIT). The
planner/architect/critic consensus loop, the five-iteration ceiling, the
sequential architect-then-critic rule and the `pending approval` boundary are all
kept. These differ:

- **It is a skill, not an alias.** OMC's ralplan is shorthand for
  `/plan --consensus` and delegates the whole workflow to the plan skill. snowpea
  has no `--consensus` mode, so the loop is written out here.
- **The gate is advisory.** OMC intercepts vague `ralph`/`autopilot`/`team`
  invocations automatically and redirects them, with `force:` and `!` escapes.
  snowpea's `/ralph` runs when you type it; this skill checks specificity only
  when you invoke it, and answers by recommending, never by redirecting.
- **Reviewers are agent definitions, not fixed agent types.** `agent="architect"`
  and `agent="critic"` resolve against `agents/*.md` in the project or
  `$SNOWPEA_HOME`. When the project defines neither, `delegate_task` still runs
  the review with the session's own settings, so the loop works on a fresh
  checkout — it just has no specialised persona behind it.
- **No `--deliberate`, `--architect codex` or `--critic codex` flags.** OMC can
  route a review pass to the Codex CLI. snowpea has one provider per session, so
  both reviews run through `delegate_task`. The interactive part *is* kept: the
  hand-off is an `ask_user` picker, the way OMC's `AskUserQuestion` gate is, and
  the plan still ends at `pending approval` until the user picks something.
- **No pre-mortem mode.** OMC's `--deliberate` adds three failure scenarios and
  an expanded unit/integration/e2e/observability test plan for high-risk work.
  Here the Risks section is always required instead of being a mode.
- **No company-context MCP call.** OMC reads `companyContext.tool` from
  `.claude/omc.jsonc` and injects the result as advisory context. snowpea's
  equivalent is `memory_search` in Round 0.
- **The plan lives in the conversation.** OMC writes plan artifacts to
  `.omc/plans/`. Writing files is outside this skill's `allowed-tools`; run
  `/ralph` if you want something on disk.
