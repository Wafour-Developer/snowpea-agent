# M5 Contract — Memory, Scheduler, Gateway, Unattended Approvals (binding for US-014, US-015, US-016)

Plan: §3.1 memory/scheduler/gateway/lifecycle, §4 M5, AC-07/08/09/17/20, §6 risk 4. Builds on M1 (`EventHub`, `ApprovalQueue`, `Lifecycle`, `commands/registry.py`) and M4 (`permissions/allowlist.py`).

## 1. Memory (`memory/`)
```python
class MemoryStore:                      # SQLite FTS5 at $SNOWPEA_HOME/state.db (tables: memories, memories_fts)
    async def write(self, text: str, *, tags: list[str], namespace: str = "default", source_session: str|None) -> MemoryEntry   # MemoryEntry(id, text, tags, namespace, created_at, score?)
    async def search(self, query: str, *, namespace: str = "default", limit: int = 8) -> list[MemoryEntry]   # FTS5 bm25; CJK: use trigram tokenizer (`tokenize='trigram'`) so Korean works
    async def delete(self, memory_id: str) -> bool
class UserProfile:                      # memory/profile.py — key/value facts per namespace (name, prefs, deploy targets…), updated by the agent via memory_write(tags=["profile:<key>"])
class Retrieval:                        # memory/retrieval.py
    async def context_block(self, session, query: str) -> str   # top-k memories rendered as "<memory id=… tags=…>text</memory>" for the system prompt; agent is instructed to cite `[mem:<id>]` when it uses one
```
- Namespace = `"default"` for interactive/CLI sessions; `"agent:<name>"` for named persistent agents (M7). Search never crosses namespaces.
- Tools (`tools/memory.py`, replace the US-009 inactive stubs): `memory_write(text, tags?)` (write), `memory_search(query, limit?)` (read). RPC `memory.write(text, tags)` / `memory.search(query, limit)` with optional `namespace`.
- Agent loop (`agent/loop.py`): before each turn, inject `Retrieval.context_block` into the system prompt when `settings.memory.enabled` (default true); after `turn.done{complete}` run a lightweight "memory nudge": if the user message contains explicit remember-style phrases (configurable regex list, ko/en) write it automatically.
- Persistence survives daemon restart (it is SQLite). AC-07 test: session A "내 배포 대상은 duho 서버다" → memory_write → session B asks → answer contains `duho` and `[mem:<id>]`.

### 1b. Scopes: project, global, agent (v0.1.x)

Memory has three scopes, told apart by the namespace string alone, so nothing about the existing rows moves:

| Scope | Namespace | Who recalls it |
| --- | --- | --- |
| global | `default` | every session |
| project | `project:<realpath of the project root>` | sessions whose workdir is inside that root |
| agent | `agent:<name>` | that named agent (M7 §6), unchanged |

`memory/scopes.py` owns every derivation and touches no database. **The project root is the git root of the session's workdir when there is one, else the workdir itself**, so `repo/` and `repo/core/` share one project memory and a sibling checkout does not. It is `None` — "this session is not in a project", everything is global — when the workdir does not exist, or *is* the user's home directory or `$SNOWPEA_HOME`.

`Session.project_namespace` is derived once in `__post_init__`, which is the one seam both `SessionManager.create` and `SessionManager.restore` pass through. `MemoryEntry.scope` / `.project` are derived on read from `namespace`, never stored, so a row written before this section labels itself correctly.

**Recall** (`Retrieval.context_block`, `memory_search`) searches project + global + the agent namespace when the session has one, as **one** bm25 query rather than one per namespace, so a project fact and a global one are ranked against each other. Order is relevance, never scope. Each hit is rendered `<memory id=… tags=…>[project] text</memory>`, and the block header names the project root so the model knows what `[project]` refers to and that the label is not part of the fact.

**`memory_write` gains `scope: "project" | "global"` (optional).** When the model passes one, it is honoured. When it does not, the tool puts the choice to the human through the questions queue (`ask_user`'s queue, so every surface including the TUI already renders it):

- header `Where to keep it`, question `Save this memory for this project only (<name>) or for every project?`
- options `Project (<name>)` / `Global (every project)` / `Cancel`, single-select, free text disabled
- **Cancel, a decline, or a timeout writes nothing**, and the tool result says so: silence is not consent to a global write.

The question is skipped, and the memory filed under the **project**, when the session is unattended or a subagent (nobody can answer), or when `settings.memory.askScope` is false. It is skipped, and the memory filed **globally**, when the session has no project root. A named agent's session keeps writing to its own `agent:` namespace unless a scope is passed explicitly.

`prompts/tool_descriptions.MEMORY_WRITE` tells the model the rule: pass `scope` only when the user said which they meant ("프로젝트에 기억해", "remember globally"); otherwise omit it and let the daemon ask.

The auto-remember nudge is unchanged — it still writes globally — but its duplicate check now spans every scope the session recalls from, so a fact `memory_write` already filed under the project does not reappear as a global copy.

**The standing digest.** Query-based recall answers "what is relevant to *this* message", which is the wrong question on the first turn of a session: there is no message yet, and the user expects the agent to already know the project it just opened. So every memory block now opens with a digest (`memory/digest.py`), present from the first turn of every session — new, resumed, subagent or scheduled — because it is built in `Retrieval.context_block`, which `context_for_turn` calls on every turn and which no longer short-circuits on an empty prompt:

```text
<header: treat these as facts, cite [mem:<id>]>

## Project memory (<name>)
- [2026-09-14] 빌드는 uv run #build [mem:m-…]
… and 12 more — memory_search finds the rest

## About the user
- deploy_target: duho 서버 [mem:m-…]

## Global memory
- [2026-09-10] 짧은 답을 선호한다 [mem:m-…]

## Relevant to this message
<memory id="m-…" tags="…">[project] …</memory>
```

- **Project memory** — the newest `memory.digestEntries` (30) project-scope memories, newest first, trimmed to `memory.digestChars` (6000) characters. The `… and K more` line counts every memory in the namespace that is not shown, whether the entry limit or the character budget left it out.
- **About the user** — every `profile:<key>` fact in the global namespace, as `- key: value`; profile facts are then excluded from the global list so nothing appears twice.
- **Global memory** — the newest 10 global memories that are not profile facts.
- Empty sections are omitted; a digest with no sections is `""`, and a block with neither digest nor hits is `""`.

Digest lines carry `[mem:<id>]` like the `<memory>` elements do: the block's one instruction to cite applies to everything in it, and a fact the model cannot name is a fact it cannot attribute.

The query-based hits follow under `## Relevant to this message`, **deduplicated against the digest** — a hit already listed above is dropped rather than printed twice, so the slot goes to something new.

The digest is cached per project namespace on `Retrieval`, keyed on `MemoryStore.revision`, which every write and delete bumps. A memory written during a turn is therefore in the next turn's digest, and nothing else rebuilds.

**Human-readable mirror.** SQLite stays the source of truth; every write is also appended as one line to a markdown file a person can read:

- project → `<project root>/.snowpea/memory.md`
- global → `$SNOWPEA_HOME/memory.md`
- agent → no mirror

One `- [YYYY-MM-DD] text #tag` line per memory. A delete rewrites the whole file from the store rather than cutting a line out of it, because the file is a projection. Every mirror failure is logged and swallowed: a read-only checkout must still be able to remember. Users may git-ignore `.snowpea/memory.md`.

**Command, RPC and CLI.** `/memory [list|search <q>|forget <id>] [--project|--global|--all]` (scope defaults to `--all`, which is exactly what recall sees) answers in plain text. Protocol 1.5.0 gains two additive methods:

```
memory.list   {scope?, sessionId?, project?, query?, limit?} -> {entries: [{id, text, tags, scope, project, createdAt}]}
memory.delete {id}                                           -> {ok}
```

`scope` is `"project" | "global" | "agent" | "all"` (default `all`). A project scope needs a project root: `sessionId` supplies one, and `project` (a path) supplies one for a caller with no session — which is how `snowpea memory list --project` works from a checkout. `MemoryHit` also gained `scope` / `project`.

CLI: `snowpea memory list|search <query>|forget <id>` with `--project` / `--global` / `--scope <s>` / `--limit` / `--json`.

## 2. Scheduler (`scheduler/`)
```python
class Job(BaseModel): id, spec: str, kind: Literal["cron","once","interval"], cron: str|None,
    interval_sec: int|None, next_run: datetime|None, task: str, mode: Mode,
    channel: str|None, origin_session_id: str|None, agent: str|None, workdir: str|None,
    enabled: bool, state: Literal["scheduled","running","cancelled"],
    last_run: datetime|None, last_status: Literal["ok","error","denied_by_timeout"]|None, created_at
class Scheduler:
    async def start(self)/stop(self)        # asyncio task inside the daemon; tick every 15s; catch-up on start for missed `once` jobs (run if < 1h late, else mark missed)
    async def schedule(self, spec, task, *, mode, channel, agent=None) -> Job     # spec: cron "0 9 * * *" | "in 60s" | "every 10m" | NL via nl_parse (ko/en: "매일 09:00", "every day at 9am", "10분 뒤")
    def list(self); async def cancel(self, job_id); async def run_now(self, job_id)
```
- Storage: `jobs` table in state.db. Double-fire guard: occurrence key `(job_id, scheduled_ts)` unique in `job_runs` table (Hermes `cron/occurrences.py` idea; vendor if used).
- Execution: creates a session (`workdir` = job.workdir or home, `mode` = job.mode, `originSurface="scheduler"`, `unattended=True`) and runs `session.prompt(task)`; `job.event{jobId, kind: started|finished|failed|denied}` notifications are emitted. Delivery of the final assistant text is §2.1.
- Registering a job with `mode="auto"` requires an approval in the registering (interactive) session (send-tag) — `schedule_create` tool has permission `send`.
- RPC `job.schedule/list/cancel/runNow`; tool `schedule_create/list/cancel`; command `/schedule "<spec>" "<task>" [--channel X] [--mode M]`; CLI `snowpea job schedule --in 60s --task "…" --channel telegram:<id>` / `snowpea job list`.
- Lifecycle: `Lifecycle` counters `jobs` = enabled jobs; keepalive rule per plan §2.6; `snowpea daemon status` prints `will exit in Ns` or `will not exit: <reasons>`, and (v0.1.x) one `messengers` line built from `gateway.list`, naming each platform as `listening` or `stopped`, wrapped in `contextlib.suppress(RpcCallError)` so an older daemon still reports everything else (`cli/commands.py`, `_messenger_line`).

### 2.1 Reminder delivery (`scheduler/scheduler.py`, `Scheduler.deliver`)

Added in v0.1.x. A scheduled job now reports back **into the session that created it**, not only to an external channel. `Job.origin_session_id` records that session (`scheduler/jobs.py`), is projected onto the wire as `JobInfo.originSessionId` (`server/protocol.py`), and is added to an existing `jobs` table by an online migration at store open — `PRAGMA table_info(jobs)` then `ALTER TABLE jobs ADD COLUMN origin_session_id TEXT` when the column is missing (`JobStore.__init__`). Existing rows are all channel-only jobs, so `NULL` is exactly right and no data moves.

`Scheduler.deliver(job, text)` MUST:

1. Try the originating session first (`_deliver_to_session`). The reminder is emitted as an ordinary `message.done` **session event**, so it is persisted, replayed on resume and rendered by every surface with no new event kind. Its text is prefixed:

   ```text
   ⏰ Scheduled reminder (<job.id>)

   <text>
   ```

2. If the session is not live, restore it through `SessionManager.restore()` (M1 §17-4), deliver, and **close it again** — a reminder must not leave a session open that the user had finished with. If the session cannot be restored at all (deleted, or the store has no row), log a warning naming both ids and fall through.
3. Then, and independently, deliver to `job.channel` through the gateway router when one is configured. **An explicit channel is additive, not a replacement**: a job with both an origin session and `channel: telegram:<id>` reaches both.
4. Append to `$SNOWPEA_HOME/logs/jobs.log` when the session delivery failed, or when an explicit channel was requested but the gateway path did not carry it (no router bound, or `deliver` raised). A gateway delivery that succeeds returns without writing the log line.

A dead gateway is a warning and a log-file fallback, never a failed job.

## 3. Gateway (`gateway/`)
```python
class InboundMessage: platform, channel_id, user_id, text, attachments, message_id, reply_to
class PlatformAdapter(Protocol):        # gateway/base.py
    platform: str
    async def start(self, on_message: Callable[[InboundMessage], Awaitable[None]]) -> None
    async def send(self, channel_id: str, text: str, *, buttons: list[Button]|None=None) -> str   # returns message id
    async def stop(self) -> None
class GatewayRouter:                    # gateway/router.py
    async def bind(self, platform, credentials_ref, target) -> Binding   # target: {"agent": name} | {"session": id} | {"new_session": {workdir, mode}}
    def list(self); async def unbind(self, binding_id)
    async def deliver(self, channel: str, text: str)   # "telegram:<chat_id>" …
```
- Adapters: `gateway/telegram.py` (Bot API long polling via httpx — no python-telegram-bot dependency required; inline keyboard buttons for approvals), `gateway/discord.py` (gateway websocket + REST, minimal), `gateway/slack.py` (Socket Mode or Events API polling; minimal). Only Telegram must be fully working in v0.1; Discord/Slack must implement the interface and be testable with the fake transport.
- `gateway/fake.py` — in-memory adapter for tests (push inbound, capture outbound).
- Inbound routing: binding → target session (created lazily per (binding, channel_id), `originSurface="gateway:<platform>:<channel_id>"`, unattended=True) → `session.prompt`; assistant `message.done` → `send`. Session list shows these sessions (`origin` field).
- Credentials: `credentials_ref` = key into `$SNOWPEA_HOME/credentials.json` (0600) or env var name; never logged.
- Lifecycle counter `gateway_bindings`.

### 3.1 Settings-driven bindings and `gateway.sync` (v0.1.x)

`Binding.source` distinguishes `"manual"` (`gateway.bind`) from `"settings"` (`GatewayRouter.sync_from_settings`), and is added to an existing table by an in-place `ALTER TABLE gateway_bindings ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'` (`gateway/router.py`, `_migrate`). **Only `source="settings"` bindings are added or removed by a sync**; a hand-made binding is never touched.

- `desired_gateways(settings)` keeps every `settings.gateway.<platform>` that is both `enabled` and carries a `token`. An **enabled platform with no token is a warning, not an error** — a half-finished wizard run is logged and skipped so the daemon still starts.
- The token is stored in `credentials.json` under the ref `<platform>`, so the auto binding's `credentials_ref` *is* the platform name.
- The auto binding is a **catch-all**: `channel_id` is `None`, so any chat that messages the bot gets its own lazily-created session, and `target` is `{"new_session": {workdir, mode}}`.
- Approvals stay fail-closed: a catch-all binding whose `allowed_user_id` is unset can approve nothing, which is why the wizard asks for the approver right after the token (M3 §5).
- `settings.set` triggers a sync that **can never fail the write**: the settings write has already succeeded, so a failure is logged and the next daemon start syncs again (`server/settings_handlers.py`).
- `GatewaySyncResult` reports `added` / `removed` / `kept` **by platform**, not by binding id.
- `snowpea setup` calls `gateway.sync` only when a daemon is already running (`cli/commands.py`); otherwise the next daemon start does it.

## 4. Unattended approvals (`permissions/approval_queue.py`, extends M1)
- `unattended=True` requests (scheduler/gateway sessions): broadcast `approval.request` as a **notification** (`approval.pending`) to all subscribed connections (TUI shows it in ApprovalQueue) AND to the bound channel via `GatewayRouter.deliver` with buttons `allow`/`deny` (`callback_data = "apr:<requestId>:allow|deny"`). Any authenticated surface or the bound channel user may respond; first response wins; others receive `approval.resolved{requestId, decision, by}`. Timeout `approvals.timeoutSec` (default 300) → deny, `job.last_status = denied_by_timeout` when it was a job.
- Interactive requests (`unattended=False`) stay on `originSurface` only and never appear in the shared queue (AC-20 last clause).
- Every decision appended to `$SNOWPEA_HOME/logs/approvals.jsonl` `{ts, requestId, sessionId, tool, decision, by, scope, unattended}`.
- Telegram approval replies must come from the bound `user_id` (risk 4); others ignored with a log line.

## 5. Tests (no real network)
- `tests/test_memory_recall.py` — AC-07 with the fake provider (script: step after memory context injection returns text containing the memory + `[mem:<id>]`), restart the in-process daemon (new Core over the same home) and repeat; namespace isolation.
- `tests/test_scheduler.py` — spec parsing (cron/in/every/NL ko+en), `runNow` executes in-process (parent pid == daemon pid), delivery to `log` channel, double-fire guard, lifecycle reasons string.
- `tests/test_approval_multichannel.py` — FakeAdapter bound to a session; job requiring shell in accept mode → approval.pending seen by a TUI-like connection AND FakeAdapter outbound has buttons; respond from adapter → connection gets approval.resolved; respond from connection → adapter gets resolved text; timeout with `approvals.timeoutSec=1` → denied_by_timeout; interactive request from origin surface not visible in `approval.list()` of another connection.
