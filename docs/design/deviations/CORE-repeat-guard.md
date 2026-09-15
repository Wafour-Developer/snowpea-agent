# Deviations — CORE-repeat-guard (paying twice for the same answer)

Validation report: a critic agent spent 532K input tokens reviewing a two-file project. It had not
read 532K tokens of code — it had read the same two files eleven times and re-run the same three
shell commands, and every copy stayed in the transcript and was re-sent on every later round.
Recorded here per `docs/design/deviations/README.md`.

## Provenance

Three upstream mechanisms, ported in shape, none vendored:

- **Re-read stubs and blocks** — Hermes `tools/file_tools_read_tracking.py` and
  `tools/file_tools.py` (`ReadTracker`, `notify_other_tool_call`), MIT licensed, Copyright (c) 2025
  Nous Research.
- **Loop detection** — gemini-cli `packages/core/src/services/loopDetectionService.ts`
  (`checkToolCallLoop`), Apache-2.0, Copyright (c) 2025 Google LLC.
- **Early pruning of old tool output** — Hermes' lean tail (`_LEAN_TAIL_KEEP_TOOL_ROUNDS = 6`) and
  opencode's `TOOL_OUTPUT_MAX_CHARS = 2000`, MIT licensed.

No code was copied. `core/snowpea_core/tools/repeat_guard.py` and
`core/snowpea_core/session/compaction.py` re-implement the semantics against snowpea's `Session`,
`ToolResult` and `ChatMessage`, so `scripts/verify_vendor_integrity.py` is not involved.

## What was wrong

1. Nothing in the loop remembered that a file had already been read. A model that lost track of a
   file it had read three screens ago simply read it again, and the transcript carried both copies
   for the rest of the session.
2. Nothing noticed a command being run in a circle. `shell("uv run pytest -q")` four times in a row
   cost four copies of the same output and told the model nothing new.
3. Nothing shrank old tool output before the request went out. A thousand-line `grep` from round two
   was re-sent verbatim in round forty.

## What it does now

### 1. Re-read stubs and blocks (`tools/repeat_guard.py`)

Keyed on `(resolved path, offset, limit)` per session. A repeat whose file hash is unchanged since
the recorded read comes back as

```
unchanged since your earlier read_file of core/app.py (412 lines, sha256 9f2c1ab4); the content in that result is still current
```

After two such stubs the third repeat is refused with `ok=False`, error code `repeat_blocked`. Any
`write_file` / `edit_file` on the path — or any change to its bytes, whoever made it — drops the key
and the next read runs normally. A path the daemon cannot hash (a remote backend, a deleted file)
opts out rather than guessing.

### 2. Consecutive and identical-result repeats

The third identical call of *any* tool in a row gets a warning line appended to its result; the
fourth is refused. Any different call resets the streak, the way Hermes' `notify_other_tool_call`
does. For `shell`, `grep`, `glob`, `list_dir` and MCP tools the comparison is also made on the
*output*: a repeat that produced the same text is replaced by `same result as your earlier grep call
(2 lines)` from the second call, and refused from the fourth.

### 3. Loop suspicion

A rolling window of the last twenty `(name, arguments)` hashes. Five occurrences — consecutive or
not — append

```
loop suspected: shell with the same arguments has run 5 times this turn; change approach or finish with what you have
```

and emit a `loop.suspected` session event carrying `{tool, count}`. Once per turn: a model that
ignores the note is not shouted at on every call.

### 4. Early pruning of old tool output (`session/compaction.py`)

`prune_old_tool_outputs(history, keep_rounds=6, max_chars=2000)` runs on the snapshot
`agent/agent.py:build_messages` hands the provider. Results older than the last six tool rounds
become `[earlier shell output pruned — 8123 chars; re-run the tool if you need it again]`; results
inside the window past 2,000 characters keep a head and a tail around `…[trimmed]…`.

## Deviations from the upstreams

1. **The stubs are addressed to the model, not to a log.** Hermes' tracker returns a short marker;
   snowpea's stub says what is still true ("the content in that result is still current") and the
   refusal says what to do instead ("continue with the task"). A model that is told only "duplicate"
   tends to try a fourth time with a different `offset`.

2. **The block is a tool error, not a dropped call.** `repeat_blocked` travels the normal
   `tool.result` path, so every surface sees it and the model can adapt inside the same turn — the
   same choice `_deny_call` already made for refused calls (CORE-prompts, gap 3).

3. **gemini-cli's sentence-level and streaming loop detection is not ported.** It also watches the
   model's prose for repeated sentences and asks an LLM to judge long turns. snowpea detects only
   the tool-call loop, which is the one the validation report measured; an LLM judge inside the loop
   would spend tokens to save tokens.

4. **Pruning happens in `build_messages`, not in the turn loop.** There is exactly one place a
   provider request is assembled, and putting it there means the context accounting in
   `session/compaction.py:prompt_messages` measures the same list the provider receives, so the HUD
   stops over-reporting. Hermes prunes at the call site.

5. **`skill_view` bodies are left alone.** They already have `[SKILL_PRUNED: …]` with a reload
   pointer (M15 §B3). Two markers on one body would tell the model to reload something it cannot.

6. **The tracker is a dynamic attribute on `Session`, not a field.** `session/session.py` stays a
   description of a conversation; the guard is an optimisation that can be deleted without touching
   the entity. `repeat_guard.guard_for(session)` creates it lazily.

## Settings

| Key | Default | Effect |
| --- | --- | --- |
| `tools.repeatGuard` | `true` | All of §1–§3. `false` restores the old behaviour exactly. |
| `agent.pruneToolOutputs` | `true` | §4. `false` sends every tool result verbatim. |
| `agent.keepToolRounds` | `6` | Tool rounds that reach the provider in full. |

## Where it is hooked

- `core/snowpea_core/agent/loop.py` `_run_one_call`: `repeat_guard.check(...)` immediately before
  `tool.run`, and `repeat_guard.record(...)` immediately after `_spill_long_result`.
- `core/snowpea_core/agent/agent.py` `build_messages`: `compaction.prune_old_tool_outputs(...)` on
  the history snapshot.

## Tests

`tests/test_repeat_guard.py`, `tests/test_tool_output_pruning.py`.
