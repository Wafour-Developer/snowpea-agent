# Deviations — CORE-plan-continuity (one current plan, carried from planning to execution)

Incident: `/ralplan` agreed a plan with the user and queued `/ralph <title>`; the user typed
`/ralph 실행해줘` instead. `/ralph` asked the provider for a PRD from a fresh two-message prompt —
system plus "실행해줘" — and planned "add main.py and a Makefile". `/team` and `/ultrawork` had the
same blind spot: none of them saw the conversation, the plan, or how far it had got. A plain
"그냥 구현해줘" after a compaction fared no better, because only the user's words survive a
compaction verbatim, not the plan body the assistant wrote.

## Provenance

The principle, not code, comes from four places: Claude Code saves an approved plan to a plans
directory and re-injects "a plan file exists, continue it" after compaction; OMC's ralph and team
read `.omc/plans/` and `prd.json`, and its pre-execution gate sends vague `ralph`/`team`/`autopilot`
requests to ralplan; Hermes' `/plan` writes `.hermes/plans/<ts>-<slug>.md` and re-injects the todo
list; opencode's `plan_exit` says "A plan file exists at X. Execute on the plan." Nothing was
vendored.

## What it does now

1. **One plan store** (`agent/plan_store.py`): `<workdir>/.snowpea/plans/current.md` plus
   `current.json` (`id`, `title`, `source`, `createdAt`, `updatedAt`, `status`, `markdownHash`,
   `stepsDerived`, `steps[]` with `pending|in_progress|done|blocked`).
   - Saving copies the previous plan to `archive/<stamp>-<slug>.{md,json}` and then replaces both
     current files atomically (write to a temp file, then rename), so a failure leaves one whole plan
     current. Plan ids carry a random tail, so two saves in one second still differ.
   - Steps come from the caller, or from the markdown's numbered list under a Steps/Plan/단계
     heading, else a spec's Acceptance criteria; at most 30.
   - A hand edit of `current.md` changes its hash. Steps that were read from the markdown are then
     read again, and each keeps the status of the old step with the same title. Steps the caller
     gave are left alone.
2. **Two tools** (`tools/plan_tools.py`): `plan_save` (with `clear: true` to abandon the plan) and
   `plan_update_step`.
   - Both are tagged `read`, so plan mode never refuses them. A delegated child is refused outright,
     because that tag would otherwise let it rewrite the user's plan with nobody asked.
   - `/ralplan` and `/deep-interview` call `plan_save` before `memory_write`. If a turn of either
     ends without `plan_save`, the newest plan-shaped assistant message of that turn is saved
     (`skills/loader.py`).
   - Plan mode is told to call `plan_save`. Entering PLAN mode starts a fresh pass. Leaving it by any
     route (`SessionManager.set_mode`) registers the newest `.md`/`.markdown`/`.txt` the pass wrote,
     if `plan_save` never ran.
   - `/plan clear` abandons the plan, which is kept in the archive.
3. **Consumers.** `/ralph`, `/team`, `/ultrawork` and `/workers <N>` treat an empty argument, a
   carry-on or execute-only phrase, or the plan's title as "run the current plan".
   - One matcher, `plan_gate.is_carry_on`, decides that phrase for every command and for `/ralph`'s
     PRD resume. It ignores filler words (그냥, 전부, 나머지, 이어서, 다음 단계, 계속, 바로, ㄱㄱ)
     and a leading step id; `S3 진행해줘` runs S3 alone.
   - The planner prompt carries the plan and its step statuses. Each story or task carries
     `plan_step` and the plan's id, which `/ralph` saves in `prd.json`.
   - Results mark the steps: `done` on a pass, `in_progress` with the reason on a failure. Marks go
     through `mark_steps(..., plan_id=...)`, which does nothing unless that plan is still current
     and active, and never reopens a finished plan. `/team` marks `done` only when the whole pipeline
     passed; `/workers` marks on merge.
   - Any other argument is planned as typed, with the plan attached. A concrete task is told the
     plan is background only; a vague one is not.
   - `/ralph` resumes an unfinished `prd.json` first when it belongs to the current plan (same plan
     id), or when the plan is older than it. Its stories keep their progress.
   - Each command says which plan it runs, with the plan's age and source.
4. **Plain prompts.**
   - While a plan is active, the volatile prompt tier carries `Current plan: <title> (saved 3d ago by
     ralplan) — 2/7 steps done; next: S3 … (.snowpea/plans/current.md)`.
   - A short stable fragment (`fragments/plan-continuity.md`) says how to work the plan and to
     `plan_save` a revised one. Only the main agent gets it, never a subagent or a role.
   - The compaction template keeps the plan path and the step state.
5. **Plan-first gate** (`commands/plan_gate.py`). It applies to `/ralph`, `/team` (including
   `/team N`), `/workers` and `/ultrawork`.
   - A request is gated when it is at most three words or eojeol (forty characters) with no anchor,
     and there is no active plan (and, for `/ralph`, no resumable PRD).
   - Anchors are a path, file name, call, snake/camel/Pascal identifier, an ALL-CAPS name, a
     version, a URL, an issue number, a list, quoted text, or a Latin word inside Korean.
   - It asks only when someone can answer: the session is not unattended, and it has an origin
     connection or is a messenger chat whose binding has an approver user
     (`GatewayRouter.has_approver`). Chats answer `ask_user` with buttons.
   - The choices are `/ralplan` first (queued with `start_turn`, like `queue_command`), run as
     typed, or cancel. `--force`/`--now`/a leading `!` bypass the gate; `planning.gate: "off"`
     disables it.
6. **`plan.updated`** session event (protocol 1.8.0, no version bump): `title`, `status`, `done`,
   `total`, `next`, `path`.

## Deviations from the brief and from the originals

- **A newer plan outranks an older, unrelated PRD.** A carry-on `/ralph` resumes an unfinished
  `prd.json` when it belongs to the current plan, or when it was written after that plan was saved.
  Otherwise ralplan → `/ralph 실행해줘` would resume an unrelated old PRD, which is the incident
  again. The replaced PRD is kept as `prd-<time>.json`, as for any new task.
- **The gate asks; OMC redirects.** An unanswered question (headless `snowpea -c` declines every
  question; a timeout) runs the request as typed and says so, because dropping what was typed in a
  script is worse than running it. Unattended runs (schedules, named-agent jobs) are not gated, only
  logged. A chat with no approver is treated as unattended (fail closed, as for approvals).
- **Steps may be grouped.** `/ralph` keeps its eight-story ceiling; with more pending steps the
  model groups consecutive steps into one story whose `plan_step` is a list.

Regression evidence: `tests/test_plan_continuity.py` covers the store, hand edits, plan-keyed marks
and a mid-run replacement, the tools including clear and the child refusal, the matcher and gate
tables, attended gateway chats, and the ralph/team/workers/ultrawork consumers including resume and
one-step runs. It also covers the volatile tier, plan-mode registration and the ralplan fallback.
`tests/test_prompts_compose.py` goldens cover the prompt text.
