# M11 Contract — Context-Window Tracking, Compaction and Store Shutdown (binding; ships CORE-context, CORE-memory-race, CORE-session-race)

Retrofit contract for [`deviations/CORE-context.md`](deviations/CORE-context.md),
[`deviations/CORE-memory-race.md`](deviations/CORE-memory-race.md) and
[`deviations/CORE-session-race.md`](deviations/CORE-session-race.md). Builds on
[`m1-core-contract.md`](m1-core-contract.md) §4 (sessions/events) and §8 (agent loop), and
[`m5-memory-scheduler-gateway-contract.md`](m5-memory-scheduler-gateway-contract.md) §1 (memory).
Source of record: `core/snowpea_core/session/compaction.py`, `session/history.py`,
`providers/context_windows.py`, `session/store.py`, `memory/store.py`, `server/app_server.py`.

Acceptance criteria: **AC-32 … AC-36**.

## 1. Protocol surface (all additive)

These additions shipped under `PROTOCOL_VERSION` 1.2.0; the constant reads **`"1.4.0"`** at HEAD
(later minors added the `config` permission tag and the session-resume work).

| addition | shape |
|---|---|
| `session.event.kind: "context"` | `{used, window, percent, estimated, model, provider}` |
| `session.event.kind: "compaction"` | `{before, after, summaryChars, auto, kept}` |
| method `session.compact` | `{sessionId, instructions?}` → `{before, after, summaryChars}` |
| `SessionSummary.contextUsed`, `.contextWindow` | for a surface that connects mid-session |

```jsonc
{"kind": "context",
 "used": 12345,        // tokens the current prompt occupies
 "window": 200000,     // null when unknown
 "percent": 6.2,       // null when window is null
 "estimated": true,    // true while 'used' is a local chars/4 estimate
 "model": "claude-sonnet-4-5", "provider": "anthropic"}

{"kind": "compaction",
 "before": 48210, "after": 2140, "summaryChars": 1832,
 "auto": true,         // false for /compact and session.compact
 "kept": 4}            // messages kept verbatim after the summary
```

**AC-32.** A `context` event MUST precede `turn.done` on every path the agent loop owns, and one
MUST follow every compaction. Ordering is the whole point and was got wrong once: `turn.done` is what
every surface treats as terminal — the headless CLI stops reading and closes the session on it — so a
`context` emitted afterwards never reached `snowpea -c --json` at all. Every `turn.done` in
`agent/loop.py`'s turn machinery therefore goes through the one `finish_turn(core, session, turn_id,
reason)` helper, which persists history, emits the reading via
`compaction.emit_context(..., discover=False)` and only then emits `turn.done` — covering the
`interrupted`, `denied` and `error` paths that used to emit inline. Consumers may keep treating
`turn.done` as terminal.

Two documented exceptions, both deliberate:

- Both the history write and the `context` emission are skipped entirely while `Core.stopping` is
  set (`server/app_server.py`, `Core.stopping: bool = False`), and `run_turn` skips `finish_turn`
  altogether on `CancelledError` under shutdown (§5).
- `flush_queued_turns` (M9 §5) emits a bare `turn.done{reason:"interrupted"}` per dropped queued
  prompt — those turns never ran, so there is no new reading to report. `commands/registry.py`,
  `commands/team_cmd.py` and `commands/ralph.py` likewise emit their own `turn.done` outside the
  loop.

`--json` gained a `context` field on the final `result` line as well as the streamed event, so a
script that reads only the last line sees the window without buffering.
`cli/render.py::Renderer.finish(exit_code, session_id, usage, context=None)` takes the new argument
with a default, so a third-party renderer implementing the old signature still type-checks. CLI:
`snowpea session context [id] [--json]` and `snowpea session compact <id> [instructions]`
(`cli/commands.py::session_context` / `session_compact`).

## 2. Window resolution (`providers/context_windows.py`)

**Two lookup methods, not one, and the difference is binding:**

```python
ProviderRegistry.context_window(vendor, model=None) -> int | None                    # sync, no I/O
ProviderRegistry.resolve_context_window(vendor, model=None, *, refresh=False) -> int | None  # async
```

The HUD path and the auto-compaction check run on every turn and MUST NOT be able to block, so they
use the synchronous one: settings override (`vendor_config["context_window"]`) → discovery cache →
static table. Only the once-per-turn `context` emission awaits the async one.

- **Discovery is attempted for `local` only** — `resolve_context_window` returns the static answer
  immediately for every other vendor. The other ten vendors publish a fixed window the static table
  already carries; a round trip per turn to relearn a constant is pure latency.
- Ollama is asked over `POST <base>/api/show` (not `/v1`, whose `GET /models` carries no window
  field); the base URL's trailing `/v1` is stripped with `removesuffix`. Any failure — unreachable,
  404, non-dict body, no field — is `None`, never an exception: `_get_json` / `_post_json` swallow
  `httpx.HTTPError` and `ValueError`.
- A single-row `/v1/models` listing is accepted **even when the id does not match**: a one-model
  vLLM or LM Studio server often does not echo back the configured alias, and with exactly one row
  there is no ambiguity.
- The cache is keyed `(vendor, base_url, model)` with `CACHE_TTL_SEC = 600.0`, matching the
  same-named constant in `providers/models.py` so the two expire together without either file
  knowing about the other. `VendorPreset.context_window()` is the **static table only** — the preset
  is frozen and knows nothing about this machine's settings or endpoints.
- **AC-33.** An unrecognised model MUST resolve to `None` and render as an honest unknown, never to a
  default. A wrong window silently truncates or silently never compacts. The CLI renders `?`
  (`cli/render.py::format_context`, `cli/commands.py::format_context_line`); the TUI renders
  `ctx 12.3k used` (M12 §2). `STATIC_WINDOWS` carries 41 entries across nine vendors (anthropic,
  openai, gemini, xai, zhipu, minimax, moonshot, deepseek, qwen); matching is exact-first then
  longest-prefix, case-insensitive, after stripping an OpenRouter `vendor/` prefix and any `:`/`@`
  suffix.
- The window is discovered at the **top** of a turn, inside `maybe_auto_compact`; the turn-ending
  path uses `emit_context(core, session, state=None, *, discover=False)` so it is a single await.
  That path also runs while a turn is being cancelled, and a second await there could take the
  cancellation before `turn.done` is sent — `CancelledError` is a `BaseException`, so an
  `except Exception` guard would not catch it.

## 3. Token estimation (`session/history.py`)

`CHARS_PER_TOKEN = 4` plus `MESSAGE_OVERHEAD_TOKENS = 4` per message. `tiktoken` is used instead
when it happens to be importable (resolved once into the module-level `_ENCODER`, set to `False` on
failure) but MUST NOT be added to `pyproject.toml` — it is not a declared dependency anywhere in the
repo.

The estimate MUST cover tool-call arguments and tool results, not only `content` strings.
`history.message_text` flattens string content, list content blocks (`block["text"]`, else
`json.dumps(block)`), `message.name`, and for each entry of `tool_calls` both the name and
`json.dumps(arguments)`; tool results are ordinary `role="tool"` messages counted the same way. A
`read_file` result is frequently the largest thing in the window, and counting only text would
under-report exactly the case the user cares about.

Provider-reported `input_tokens` is **adopted, never averaged** with the estimate — the vendor knows
what it billed. `compaction.record_provider_usage` sets `session.context_used` and flips
`session.context_estimated` to `False`; `compact_session` sets it back to `True`, because the old
count no longer describes the new prompt.

## 4. Compaction (`session/compaction.py`)

**AC-34.** Auto-compaction runs at the **top of the agent loop, before the user message is
appended** (`maybe_auto_compact` is called in `_drive` above the `history.append(ChatMessage(role=
"user", ...))` block) — the only point that is both "between turns" and inside the code path every
surface shares. Compacting after the append would make the summariser describe a message the model
has not answered yet.

- `should_auto_compact` MUST refuse when the history is no longer than
  `context.keepLastMessages` (`config/settings.py`, default 4; `_keep_last` falls back to
  `DEFAULT_KEEP_LAST = 4`). Otherwise a single enormous message compacts on every turn, summarises
  nothing (the head is empty) and grows the history by one system message each time. The trigger is
  `context.autoCompact` (default `true`) at `context.autoCompactPercent` (default 85).
- The summary is a **`role: "system"` message inside the history** (`summary_message`, headed
  `Session summary`). The Anthropic and Gemini adapters hoist every system message into the
  request's system field wherever it sits, and the OpenAI-compatible adapters pass it through, so one
  shape works for all eleven vendors with no new message role.
- **The verbatim tail is moved forward past orphaned tool results.** Keeping "the last N messages"
  literally can start the tail on a `tool` message whose assistant call has just been summarised
  away, which every vendor rejects. `split_index(messages, keep_last)` walks the boundary forward
  while `messages[index].role == "tool"`, so `keepLastMessages` is a minimum-ish rather than an exact
  count and the event reports the `kept` count actually used (`len(tail)`).
- **A provider failure during compaction MUST NOT lose the conversation.** `compact_session` wraps
  the `summarise` call in `except ProviderError` (warning) and a broad `except Exception`
  (exception), and falls back to `fallback_summary`, a mechanical note stating how many messages
  were dropped, their roles, and which tools were used. Leaving an over-full window intact is worse
  than an imperfect summary; discarding the history would be worse still. (The `try` lives in
  `compact_session`, not inside `summarise`.)
- `session.compact` raises `invalid_params` while `session.turn_task` is running:
  `"<id> has a turn in flight; interrupt it or wait, then compact"`. `/compact` cannot violate the
  "never compact mid-tool-loop" rule — it *is* the turn — but the RPC can be called at any moment.
- Compaction history is **not** written to the `messages` table by this path; the `compaction` event
  is persisted by `EventHub.emit` (via `core.hub.emit_event`) like every other event, so a resuming
  client gets the marker it needs for the `— compacted (12.3k → 2.1k tokens) —` divider without a
  second storage path. The rewritten history reaches the `messages` table later, through
  `finish_turn`'s `replace_messages`. Rendering the divider from the event is the client's job.

## 5. Store lifetime and the shutdown race

A background task that starts *after* its store closed must not reach a closed
`sqlite3.Connection`. Both stores MUST implement the same three-part discipline:

**AC-35.** `memory/store.py` and `session/store.py` each carry an explicit `_closed` flag checked
under the same `threading.Lock` at the top of every read/write/delete, raising `MemoryClosed` /
`StoreClosed` — both `RuntimeError` subclasses — rather than touching the connection. The session
store re-checks in `_execute`, `_query`, `delete_sessions` and `replace_messages`; the memory store
in `_query`, `_write` and `_delete`. `close()` is idempotent in both (`if self._closed: return`). A
lock alone is not enough — it serialises *concurrent* access and does nothing about a call that
begins after `close()`.

**AC-36.** `Daemon.stop` MUST drain before it closes. The order at HEAD (`server/app_server.py`):

1. `core.stopping = True` — the very first thing after `request_shutdown`, before the update
   watcher, before `lifecycle.stop()`. `nudge_after_turn` checks it and returns immediately without
   touching the store at all. `context_for_turn` does not check it: recall runs once at the very
   start of a turn's `_drive`, long before shutdown could plausibly race it, and it already treats
   every failure (including `MemoryClosed`) as `""`.
2. `await core.sessions.close_all()` — `SessionManager.close_all(*, timeout: float = 5.0)` sets
   every session's `interrupt`, cancels every live `turn_task` up front, then awaits them together
   (`asyncio.gather(..., return_exceptions=True)` under one `asyncio.wait_for(timeout)`, so a task
   stuck outside a cancellable await cannot block shutdown forever; a `TimeoutError` is logged as a
   warning), then closes each session normally while the session store is still open.
3. Update-watcher tasks, `lifecycle.stop()`, the scheduler, the gateway, named agents, websockets
   and connections, the aiohttp runner.
4. `core.store.close()` (session store), then **`await core.memory.close()`** — the memory service is
   closed **last**, after the session store, not before lifecycle. `MemoryServices.close()` is async
   and cancels and awaits every task tracked via `MemoryServices._track()` **before** `store.close()`;
   a task cancelled mid-write inside `asyncio.to_thread` does not stop the underlying thread, so
   draining first is what keeps shutdown deterministic. Tool subprocesses and `daemon.json` are
   cleaned up after that.

Defense in depth on top of that: `EventHub.emit` and `SessionManager.close`'s `close_session` write
catch `StoreClosed` and drop the write at `debug`; `nudge_after_turn` / `context_for_turn` catch
`MemoryClosed` and `asyncio.CancelledError` explicitly ahead of their broad `except Exception` (the
cancellation belongs to the tracked child task, not to the awaiting coroutine, so swallowing it
suppresses no caller's cancellation). `run_turn` skips `finish_turn` entirely on `CancelledError`
when `core.stopping` is set — no history write, no `context`, no `turn.done` — because a turn
cancelled by `close_all` has nothing useful left to tell a subscriber whose socket is being torn down
in the same call. A turn cancelled for any other reason still emits `turn.done{interrupted}`;
`core.stopping` is the only thing this branches on.

**Explicitly out of scope: turn resumption.** A turn cancelled by `close_all` is gone — the
in-flight tool-call/model round is not persisted or replayed on the next daemon start.
`session.list` and the session-store row correctly show the session as closed. "Resume where the turn
left off" would need a separate design.

## 6. Tests

- `tests/test_session_loop.py::test_stop_mid_turn_is_clean` and
  `::test_stop_mid_turn_is_clean_repeated` (5× over fresh `$SNOWPEA_HOME` directories) with
  `tests/fixtures/providers/fake/shutdown_race.json` (`delaySec: 2`): start the prompt, sleep until
  the turn is genuinely inside the provider delay, `daemon.stop()`, assert nothing raises and nothing
  logs at `ERROR`, then reopen `Store` on the same home and confirm the row reads back with
  `closed_at` set — a fresh `Store.open` succeeding at all is the corruption check.
- `tests/test_memory_recall.py::test_close_during_a_delayed_nudge_is_clean` and
  `::test_close_during_a_delayed_nudge_is_clean_repeated` drive `MemoryServices` / `nudge_after_turn`
  directly rather than through a full daemon turn: an earlier version that stopped a real `Daemon`
  mid-turn reproduced the *session*-store race instead, which would have made the regression test
  misleading for the thing it pins down.
- Two tests MUST pass **unmodified** —
  `tests/test_subagents.py::test_child_events_flow_on_the_child_session` and
  `tests/test_session_loop.py::test_resume_returns_events_after_seq`. That they do is the check that
  `context` precedes `turn.done` rather than trailing it.
- `providers/fake.py::_prompt_chars` reports the whole prompt as `input_tokens`, not just the last
  user message. No vendor counts only the final message, and the old behaviour made the reported
  context flat across a growing conversation.
