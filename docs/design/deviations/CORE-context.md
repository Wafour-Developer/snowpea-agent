# Deviations — CORE-context (context-window tracking, `/compact`, auto-compaction)

Additive work for the user requirement "현재 컨텍스트 윈도우 사이즈 및 현재 사용량은 중요한 정보 —
하단에 표시. `/compact` 같은 수동 compaction도 지원." Recorded here per
`docs/design/deviations/README.md`.

1. **`PROTOCOL_VERSION` was not bumped.** It already read `1.2.0`, and everything here is
   additive — two new `session.event` kinds (`context`, `compaction`), one new method
   (`session.compact`), two new optional `SessionSummary` fields. An existing client that ignores
   unknown event kinds is unaffected, so one minor version carries the change.

2. **The window table lives in its own module, not in `providers/models.py`.** The task said to
   cache discovered windows "with the existing models cache". `providers/models.py` is owned by
   another in-flight story (CORE-models) and its cache is keyed `(vendor, base_url)` for a *list*
   of ids, which cannot also hold a per-model integer. `providers/context_windows.py` keeps its own
   `(vendor, base_url, model)` cache with the same `CACHE_TTL_SEC` (600s), so the two expire
   together without either file having to know about the other.

3. **Discovery is attempted for `local` only.** Every hosted vendor publishes a fixed window that
   the static table already carries; a round trip per turn to relearn a constant is pure latency.
   `ProviderRegistry.resolve_context_window` short-circuits to the table for the other ten vendors.

4. **Two lookup methods, not one.** `context_window(vendor, model)` is synchronous and does no I/O
   (settings override → discovery cache → static table); `resolve_context_window(...)` is the async
   one that may ask a local server. The HUD path and the auto-compaction check run on every turn and
   must not be able to block; only the once-per-turn `context` emission awaits the async one.

5. **Ollama is asked over `/api/show`, which is a POST and not on the `/v1` path.** The task named
   `/api/show`; in practice `GET /v1/models` on Ollama returns rows with no window field at all, so
   the fallback is not optional for that vendor. The base URL's trailing `/v1` is stripped to reach
   it. Any failure (unreachable, 404, no field) is `None`, never an exception.

6. **A single-row `/v1/models` listing is accepted even when the id does not match.** A one-model
   vLLM or LM Studio server often does not echo back the alias the client was configured with. With
   exactly one row there is no ambiguity about which model the window belongs to.

7. **`VendorPreset.context_window()` is the static table only.** The task put
   `context_window(model)` on "`VendorPreset`/registry". The preset is frozen and knows nothing
   about this machine's settings or endpoints, so the override and the discovery live on
   `ProviderRegistry`, which already owns credentials, base URLs and model resolution.

8. **`tiktoken` is honoured but not depended on.** The estimate is chars/4 plus four tokens of
   per-message overhead. When `tiktoken` happens to be importable it is used instead and cached
   once; it is not added to `pyproject.toml`, so the default install has no new dependency.

9. **The estimate covers tool-call arguments and tool results, not just text.** A `read_file` result
   is frequently the largest thing in the window, so counting only `content` strings would have
   under-reported exactly the case the user cares about.

10. **Provider-reported `input_tokens` is adopted, never averaged with the estimate.** The vendor
    knows what it billed. `Session.context_estimated` flips to `False` when a usage event arrives and
    back to `True` after a compaction, because the old count no longer describes the new prompt.

11. **`context` is emitted immediately *before* `turn.done`, not after it.** This was tried the
    other way round first and is worth recording, because the failure was invisible in the unit
    tests: `turn.done` is what every surface treats as "the turn is over", and the headless CLI
    stops reading and closes the session on it, so a `context` event emitted afterwards never
    reached `snowpea -c --json` at all. Every `turn.done` in `agent/loop.py` now goes through one
    `finish_turn()` helper that emits the reading first, which also covers the `interrupted`,
    `denied` and `error` paths that each used to emit `turn.done` inline. It is skipped entirely
    while `Core.stopping` is set, matching how the final `turn.done` is skipped during shutdown
    (CORE-session-race). Consumers may keep treating `turn.done` as terminal.

12. **Compaction history is not persisted to the `messages` table.** That table exists but nothing
    in the tree writes to it — `session.resume` replays the *event* log. The `compaction` event is
    persisted by `EventHub.emit` like every other event, so a resuming client gets the marker it
    needs for the `— compacted (12.3k → 2.1k tokens) —` divider without a second storage path.
    Rendering the divider from the event is the client's job; the CLI renderer does it, and the
    event carries `before`, `after`, `summaryChars`, `auto` and `kept`.

13. **The summary is a `role: "system"` message inside the history.** The Anthropic and Gemini
    adapters hoist every system message into the request's system field wherever it sits in the
    list, and the OpenAI-compatible adapters pass it through, so one shape works for all eleven
    vendors without a new message role.

14. **A provider failure during compaction does not lose the conversation.** `summarise` catches
    `ProviderError` and anything else, and `fallback_summary` writes a mechanical note (how many
    messages, which tools were used) instead. Leaving an over-full window intact is worse than an
    imperfect summary, but silently discarding the history would be worse still.

15. **The verbatim tail is moved forward past orphaned tool results.** Keeping "the last N messages"
    literally can start the tail on a `tool` message whose assistant call has just been summarised
    away, which every vendor rejects. `split_index` walks the boundary forward; `keepLastMessages`
    is therefore a minimum-ish, not an exact count, and the event reports the `kept` count actually
    used.

16. **`context.keepLastMessages` was added (default 4).** The task fixed N=4 in prose. It is a
    setting because the right number depends on how large the tail messages are, and the tests need
    a smaller one to reach the threshold quickly.

17. **Auto-compaction runs at the top of the agent loop, before the user message is appended.**
    That is the only point that is both "between turns" and inside the code path every surface
    shares. Compacting after the append would make the summariser describe a message the model has
    not answered yet.

18. **`should_auto_compact` refuses when the history is no longer than `keepLastMessages`.**
    Otherwise a single enormous message would compact on every turn, summarise nothing (the head is
    empty) and grow the history by one system message each time.

19. **`session.compact` refuses while a turn is in flight.** The task said never to compact
    mid-tool-loop. `/compact` cannot violate that — it *is* the turn — but the RPC can be called at
    any moment, so it returns `invalid_params` when `session.turn_task` is still running rather
    than replacing the history under a pending tool result.

20. **The window is discovered at the *top* of a turn, never at the end of one.**
    `emit_context(..., discover=False)` on the turn-ending path keeps it to a single await. That
    path also runs while a turn is being cancelled (a `session.close` mid-turn), and a second await
    there could take the cancellation before `turn.done` is sent — `CancelledError` is a
    `BaseException`, so the `except Exception` guard would not have caught it.
    `maybe_auto_compact` has already resolved and cached the window by then, so the reading is the
    same. For `local` that means the first turn of a session may pay one lookup, bounded by the 5s
    timeout and cached afterwards — including a negative answer.

21. **`snowpea session context [id]` and `snowpea session compact <id> [instructions]` were both
    added.** The task marked the CLI optional and named only `context`. `compact` costs three lines
    on top of the RPC that already existed and makes the headless surface complete.

22. **`--json` gained a `context` field on the final `result` line** rather than only streaming the
    `context` event. A script that reads just the last line can then see the window without
    buffering the stream. `Renderer.finish` takes the new argument with a default, so a third-party
    renderer implementing the old signature still type-checks.

23. **`FakeProvider` now reports the whole prompt as `input_tokens`, not just the last user
    message.** No vendor counts only the final message, and the old behaviour made the reported
    context flat across a growing conversation, which would have made the auto-compaction test pass
    or fail for the wrong reason. No existing test asserted on the old value.

24. **The window table lists `gpt-5` and `claude-*` by prefix.** Claude models are uniformly 200k by
    default, so one `claude-` prefix entry is more durable than a row per model id. Anything the
    table does not recognise is `None`, and `None` renders as `?` rather than a default — a wrong
    window silently truncates or silently never compacts, which is worse than an honest unknown.

25. **No existing test needed its assertions changed.** Two did while `context` trailed
    `turn.done` (see item 11) — `test_subagents.py::test_child_events_flow_on_the_child_session`
    asserts the child's last event is `turn.done`, and
    `test_session_loop.py::test_resume_returns_events_after_seq` compares the replayed log against
    what the live client saw. Both are back to their original form now that the reading precedes
    `turn.done`. That those two tests pass unmodified is the check that the ordering is right.

26. **`FakeProvider`'s usage change is the only edit outside the feature's own files.** See item 23
    — it is a fidelity fix to a test double, not a behaviour change in shipped code.

## Verified against a real server

`http://hon1.snowpea.ai:8004/v1` (vLLM on this network) lists `gemma-4-31B` and annotates the row
with `max_model_len: 55424`. `ProviderRegistry.resolve_context_window("local")` returns `55424`
against it. No test depends on that host.

## The `context` event shape, for the TUI and IDE

```jsonc
{
  "kind": "context",
  "used": 12345,        // tokens the current prompt occupies
  "window": 200000,     // null when unknown -> render "?"
  "percent": 6.2,       // null when window is null
  "estimated": false,   // true while 'used' is a local chars/4 estimate
  "model": "claude-sonnet-4-5",
  "provider": "anthropic"
}
```

```jsonc
{
  "kind": "compaction",
  "before": 48210,      // estimated tokens before
  "after": 2140,        // estimated tokens after
  "summaryChars": 1832,
  "auto": true,         // false for /compact and session.compact
  "kept": 4             // messages kept verbatim after the summary
}
```

One `context` event immediately precedes every `turn.done`, and one follows every compaction. `session.list` rows carry
`contextUsed` and `contextWindow` for a surface that connects mid-session.
