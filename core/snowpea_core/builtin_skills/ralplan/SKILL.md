---
name: ralplan
description: Consensus planning — planner, architect and critic argue until the plan holds, before any code is written.
argument-hint: "<task description>"
allowed-tools: [read_file, list_dir, glob, grep, git_status, git_diff, git_log, delegate_task, memory_write, memory_search]
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

## The loop

### Round 0 — ground yourself

Read before you plan. Use `list_dir`, `glob`, `grep` and `read_file` to find the
code this task touches; use `git_log` to see whether somebody already tried.
Call `memory_search` on the task's key nouns. Facts first, opinions after.

### Round 1 — the planner draft

You are the planner. Write, in the conversation:

- **Principles** — three to five rules this change must respect, drawn from how
  the codebase already works, not from general good taste.
- **Drivers** — the three considerations that actually decide the design.
- **Options** — at least two real approaches, each with what it costs. If only
  one approach is viable, say explicitly why each alternative is dead; "there is
  only one way" is nearly always a sign the problem was not understood.
- **The plan** — the chosen option as ordered steps, each naming the files it
  touches and how it is verified.

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

Then stop. End with one line offering the next step — `/ralph <title>` to drive
it to done, or `/ultrawork <title>` when the steps are independent — and let the
user choose. The plan is marked `pending approval` and stays that way until they
say otherwise.

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
- **No `--interactive`, `--deliberate`, `--architect codex` or `--critic codex`
  flags.** OMC can route a review pass to the Codex CLI and can gate on
  `AskUserQuestion` prompts. snowpea has one provider per session and no
  question tool, so the run is always non-interactive and always ends at
  `pending approval`.
- **No pre-mortem mode.** OMC's `--deliberate` adds three failure scenarios and
  an expanded unit/integration/e2e/observability test plan for high-risk work.
  Here the Risks section is always required instead of being a mode.
- **No company-context MCP call.** OMC reads `companyContext.tool` from
  `.claude/omc.jsonc` and injects the result as advisory context. snowpea's
  equivalent is `memory_search` in Round 0.
- **The plan lives in the conversation.** OMC writes plan artifacts to
  `.omc/plans/`. Writing files is outside this skill's `allowed-tools`; run
  `/ralph` if you want something on disk.
