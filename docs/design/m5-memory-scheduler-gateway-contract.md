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

## 2. Scheduler (`scheduler/`)
```python
class Job(BaseModel): id, spec: str, kind: Literal["cron","once","interval"], next_run: datetime, task: str, mode: Mode, channel: str|None, agent: str|None, enabled: bool, last_run: datetime|None, last_status: Literal["ok","error","denied_by_timeout"]|None, created_at
class Scheduler:
    async def start(self)/stop(self)        # asyncio task inside the daemon; tick every 15s; catch-up on start for missed `once` jobs (run if < 1h late, else mark missed)
    async def schedule(self, spec, task, *, mode, channel, agent=None) -> Job     # spec: cron "0 9 * * *" | "in 60s" | "every 10m" | NL via nl_parse (ko/en: "매일 09:00", "every day at 9am", "10분 뒤")
    def list(self); async def cancel(self, job_id); async def run_now(self, job_id)
```
- Storage: `jobs` table in state.db. Double-fire guard: occurrence key `(job_id, scheduled_ts)` unique in `job_runs` table (Hermes `cron/occurrences.py` idea; vendor if used).
- Execution: creates a session (`workdir` = job.workdir or home, `mode` = job.mode, `originSurface="scheduler"`, `unattended=True`) and runs `session.prompt(task)`; the final assistant text is delivered to `channel` via the gateway router (`telegram:<chat_id>` | `discord:<channel_id>` | `slack:<channel>` | `log`), and `job.event{jobId, kind: started|finished|failed|denied}` notifications are emitted.
- Registering a job with `mode="auto"` requires an approval in the registering (interactive) session (send-tag) — `schedule_create` tool has permission `send`.
- RPC `job.schedule/list/cancel/runNow`; tool `schedule_create/list/cancel`; command `/schedule "<spec>" "<task>" [--channel X] [--mode M]`; CLI `snowpea job schedule --in 60s --task "…" --channel telegram:<id>` / `snowpea job list`.
- Lifecycle: `Lifecycle` counters `jobs` = enabled jobs; keepalive rule per plan §2.6; `snowpea daemon status` prints `will exit in Ns` or `will not exit: <reasons>`.

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

## 4. Unattended approvals (`permissions/approval_queue.py`, extends M1)
- `unattended=True` requests (scheduler/gateway sessions): broadcast `approval.request` as a **notification** (`approval.pending`) to all subscribed connections (TUI shows it in ApprovalQueue) AND to the bound channel via `GatewayRouter.deliver` with buttons `allow`/`deny` (`callback_data = "apr:<requestId>:allow|deny"`). Any authenticated surface or the bound channel user may respond; first response wins; others receive `approval.resolved{requestId, decision, by}`. Timeout `approvals.timeoutSec` (default 300) → deny, `job.last_status = denied_by_timeout` when it was a job.
- Interactive requests (`unattended=False`) stay on `originSurface` only and never appear in the shared queue (AC-20 last clause).
- Every decision appended to `$SNOWPEA_HOME/logs/approvals.jsonl` `{ts, requestId, sessionId, tool, decision, by, scope, unattended}`.
- Telegram approval replies must come from the bound `user_id` (risk 4); others ignored with a log line.

## 5. Tests (no real network)
- `tests/test_memory_recall.py` — AC-07 with the fake provider (script: step after memory context injection returns text containing the memory + `[mem:<id>]`), restart the in-process daemon (new Core over the same home) and repeat; namespace isolation.
- `tests/test_scheduler.py` — spec parsing (cron/in/every/NL ko+en), `runNow` executes in-process (parent pid == daemon pid), delivery to `log` channel, double-fire guard, lifecycle reasons string.
- `tests/test_approval_multichannel.py` — FakeAdapter bound to a session; job requiring shell in accept mode → approval.pending seen by a TUI-like connection AND FakeAdapter outbound has buttons; respond from adapter → connection gets approval.resolved; respond from connection → adapter gets resolved text; timeout with `approvals.timeoutSec=1` → denied_by_timeout; interactive request from origin surface not visible in `approval.list()` of another connection.
