# Host tools and browser support (protocol 1.6.0)

Requested by snowpea-browser (a Chromium-fork agentic browser) in
`snowpea-browser/docs/core-request-host-tools.md` and addenda 1 and 2. The
browser keeps one connection to the daemon, runs page actions in its own
process, and leaves the agent loop, permissions, memory and sessions to core.

## 1. Host tools

| Method | Direction | Shape |
|---|---|---|
| `tool.register` | c → s | `{tools: [{name, description, inputSchema, permission, category?, timeoutMs?}]}` → `{registered: [name]}` |
| `tool.unregister` | c → s | `{names}` → `{removed}` |
| `tool.invoke` | s → c request | `{sessionId, turnId, callId, name, args, mode, workspaceDir, workdir, parentSessionId}` → `{ok, output, error?, content?, meta?}` |
| `tool.progress` | c → s notification | `{callId, message}`; re-emitted as `session.event tool.progress` (`stream: "stdout"`, `chunk: message`) |
| `tool.cancel` | s → c notification | `{sessionId, callId}`; sent when the turn is interrupted while a call runs |

Rules (`core/snowpea_core/tools/host_tools.py`):

* **Names** are plain: `[A-Za-z][A-Za-z0-9_-]{0,63}`. A name that is a daemon
  tool is refused (`invalid_params`), except the built-in `browser_*` tools,
  which a host tool of the same name **shadows** for the sessions that see it.
  Names are per connection, so two browsers may both register `repl`.
  Registration is all-or-nothing, and re-registering a name replaces it (a new
  description, e.g. an updated REPL guide).
* **Ownership.** Tools belong to the registering connection and are dropped
  when it closes (`RpcConnection.on_close`). A client re-registers after
  `system.hello`; the SDK helper does it on every `reconnected`.
* **Scope.** A session sees the host tools of one connection: the one named by
  `session.create {hostToolsFrom}` (a `clientId` or a surface id), else its
  origin connection. Subagents, including team roles, inherit the parent's
  origin and `hostToolsFrom`.
* **Eager.** A session's host tools are always sent in full (never deferred
  behind `tool_search`).
* **Permissions.** A host tool carries an ordinary permission tag. Its calls go
  through the same mode matrix, allowlist, approval routing and plugin hooks as
  any tool.
* **Failure.** A timeout (default 120 s, `timeoutMs` per tool) or a closed
  connection is a tool error, never a hang. On `session.interrupt`, core sends
  `tool.cancel` and ends the call as `"<name>: interrupted"` without waiting.
* **Results.** `content` text blocks join `output`. Image blocks
  (`{type: "image", mediaType, data}`) reach a vision model as image input; a
  text-only model keeps the text. Long output is spilled to a file with a
  `read_file` pointer, like the scanning built-ins. `meta.sensitive: true`
  keeps the output out of the stored history, the `tool.result` event (both
  `[redacted]`), compaction summaries and memory; the model reads it only
  during the turn it was produced in.
* **Result meta (addendum 13).** The host's `meta` is forwarded as is on the
  `tool.result` event's `meta` and stored with it, so a `session.resume`
  replay has it.
  * Surfaces use it for things like page preview cards (`pages`, `title`,
    `elapsedMs`). It is never shown to the model.
  * It is forwarded for sensitive results too, because the host puts only
    non-secret facts there.
  * Anything over 16 KB of JSON is dropped, with a warning in the log.

### Code-running host tools (the intended pattern)

A tool that runs free-form code, such as the browser's `repl(title, code)`,
cannot be classified before it runs. Register it with the `read` tag so it does
not prompt on every call, and let the host enforce the rest itself:

* `tool.invoke` carries the session's `mode`. In `plan` the host refuses page
  mutations itself.
* While the code runs, the host escalates each sensitive action (a submit or pay
  click, a cookie-bearing mutation fetch, an upload, a new origin in Guard mode)
  with `approval.ask` (§4) and continues only on `allow`.

## 2. `host` browser provider

`tools/browser_providers/host.py`. Shadowing is the primary path: a host that
registers `browser_navigate` replaces the Playwright tool. Routing is **per
session** (addendum 6): a session whose host provides any `browser_*` tool sends
the built-in `browser_*` tools the host did not shadow to the host as well
(`browser_providers.resolve_for_session`). Every other session, such as the CLI
and IDE ones, keeps the configured `browser.provider`. Selecting
`browser.provider: "host"` globally still works, but nothing sets it: it would
leave those other sessions without a browser whenever no browser is attached.
With the host provider in use and no host attached, the call is refused rather
than falling back to a different browser.

## 3. Identity, re-attach, keep-alive

`system.hello` takes `clientKind`, `clientId` (stable per install),
`instanceId` and `keepAlive`.

* A new connection with a `clientId` re-binds every open session whose creator
  had that `clientId` and whose origin connection is gone. Approvals and
  `tool.invoke` then reach the new connection.
* `session.attach {sessionId, hostToolsFrom?}` → `{sessionId, hostTools,
  hostToolsFrom}` makes the caller the origin explicitly. With `hostToolsFrom`
  (a clientId or surface id), the session's host tools are re-pointed to that
  connection, which must be open and of clientKind `browser`; otherwise the call
  fails with `invalid_params`. The value is persisted in state.db
  (`sessions.host_tools_from`) and announced as `sessions.changed {reason:
  "host"}` (addendum 7). `session.create {hostToolsFrom}` is persisted the
  same way.
* **Fallback.** When a session's `hostToolsFrom` names no connected host, and
  its origin connection is an open `browser`-kind connection with registered
  host tools, the session uses that connection's tools. This covers a browser
  relaunched under a new clientId that re-attached without the parameter. The
  per-session `browser_*` routing (§2) follows the same resolution.
* `keepAlive: true` holds the daemon's idle shutdown while connected (lifecycle
  counter `keepalive_clients`).

## 4. Escalation: `approval.ask`

`approval.ask {sessionId, callId?, tool, permission, reason, args?, site?, risk?,
detail?}` → `{decision, scope, by}`. Used while a host tool runs, when the host
sees something riskier than the tool's own tag, such as a click on "Pay" in REPL
code. Only the connection whose host tools the session uses may ask.

The order is:

1. The mode matrix for `permission`. `deny` stays deny.
2. The allowlist for this tool on this site.
3. The session's approver (`approval.request` to the origin, with `site` and
   `scopeHint: "site"`). A timeout denies.

The site is `site`, else `detail.origin`, else the origin of `args.url`.

The **`site` scope** stores a global allowlist entry for the tool, limited to
that origin (`AllowlistEntry.origin`). It is not put in the per-session cache,
which is keyed by tool only.

## 4a. Plugin hooks (addendum 3 §R)

* `${SNOWPEA_PYTHON}` in a hook command is the core's own interpreter: the
  venv's python from source, and `"<snowpea-core>" --run-hook` in a frozen
  (PyInstaller) build. `snowpea-core --run-hook <file.py> [args…]` runs the
  script as `__main__` with stdin, stdout and exit status intact.
  `SNOWPEA_PYTHON` is also exported to hooks.
* A hook entry with `"failClosed": true` blocks a `PreToolUse` call when the
  hook cannot start, exits non-zero (other than the usual 2 = block) or times
  out. Other hooks still fail open, as in Claude Code, but a failure is now
  logged as a warning.

## 5. Session additions

* **Page attachment.** `session.prompt` attachments take
  `{kind: "page", url, title?, selection?, snapshot?}`. It is rendered into the
  user turn as an `<untrusted_page url=… title=…>` block that says the content is
  data, never instructions. The snapshot is cut at 60,000 characters.
* **`session.steer {sessionId, text}`** → `{ok, started}`. The text is injected at
  the running turn's next tool-round boundary, whatever `agent.busy` says. With
  no turn running it starts one.

## 6. Setup

* `setup.status {profile}` → `{required, optional, existingInstall,
  configuredProviders}`. For `browser`, the only required item is `provider`,
  which is done once `provider.test` has passed for a configured vendor
  (remembered in `$SNOWPEA_HOME/setup-state.json`). The optional items are
  search, memory, scheduler, gateways, mode and browser.
* `setup.applyDefaults {profile}` → `{applied, skipped, status}`. It fills in
  only keys that are unset, meaning absent or null in `settings.json`, and never
  overwrites a value the user set (addendum 6). The profile defaults are
  `search.provider: "ddgs"`, `memory.enabled: true` and
  `scheduler.enabled: true`. `applied` lists the keys it filled in; `skipped`
  lists the keys that already held a value. It never writes `browser.provider`
  (§2). Gateways are left alone; none is on by default. The default mode is
  already `accept`.
* `provider.test {provider, model?}` → `{ok, provider, model, modelEcho?,
  latencyMs, reply?, error?}`.
* A plugin in a local directory installs with the existing
  `skill.install {source: <path>}`, which accepts Claude-format plugins.

## 7. SDK

`registerTools(client, tools, handler)` in `sdk/src/tools.ts`. The handler gets
`(request, progress, signal)`: `progress(msg)` sends `tool.progress`, and
`signal` aborts on `tool.cancel`. The helper re-registers after every reconnect
and returns `{registered, dispose}`. `connect()` takes `clientKind`, `clientId`,
`instanceId` and `keepAlive`.

## 8. Protocol 1.7.0 additions

**Agent tools**

* `write_todos {todos:[{id, content, status: pending|in_progress|completed}], merge?}`
  keeps the session's task list. At most one item may be `in_progress`.
  `merge: true` updates items by id. Every change emits the session event
  `todos.updated {todos}`.
* `delegate_task` gains three parameters:
  * `run_in_background: true`: returns a `task_id` at once.
  * `profile: default|fork_self`: `fork_self` starts the child from a copy of
    the parent's conversation, without the unanswered delegate call and with
    sensitive output redacted.
  * `model_category: standard|fast`: resolved through the setting
    `models.categories {fast: "<profile|vendor:model|vendor>", …}`. An unset
    category uses the child's usual model.
* `subagent_wait {task_ids, timeout?}` returns the reports of background
  delegations. It returns early on an interrupt and reports tasks that are
  still running. Only the session that started a task may wait on it.
* `get_time` returns the local and UTC time.

**Skills.** SKILL.md front matter
`autoInject: {keywords: [gmail, 지메일]}` (inline, or nested under
`autoInject:` as `keywords: [...]` or a `- item` list). A user turn that
contains a keyword (case-insensitive, any script) brings the skill in once per
session, as a user-role note after the prompt. The note holds the body when it
is at most 8,000 characters, and otherwise a pointer to `skill_view`.

**Workspace.**

* Each session gets `$SNOWPEA_HOME/sessions/<YYYY-MM-DD>_<id>/{tmp,artifacts}`.
  It is returned by `session.create` and `session.list` (`workspaceDir`) and
  passed to hosts as `tool.invoke.workspaceDir`. Subagents share their
  parent's. The environment block names only its `tmp/`, as scratch space:
  files the user asked for are saved in the working directory in every client,
  the browser included, so `session.artifacts` lists only what a host or tool
  put there explicitly.
* `session.artifacts {sessionId}` → `{workspaceDir, artifacts: [{path, name,
  size, modifiedAt, mimeType}]}`, newest first.

**Task list.**

* New `session.list` fields:
  * `title`: the opening words of the first prompt, or `session.rename`.
    Persisted.
  * `status`: `idle|running|awaiting_approval|awaiting_question|error`.
  * `lastActivityAt`, `turnStartedAt`, `pendingApprovals`, `pendingQuestions`.
* `sessions.changed {reason, sessionId, status?, title?}` is broadcast to every
  authenticated connection on turn start and end, approval and question
  waits, create, close and rename.
* `session.rename {sessionId, title}`.

**Busy behaviour per prompt.** `session.prompt {whenBusy: "queue"|"steer"}`
overrides `agent.busy` for that prompt only (Enter = queue, Ctrl+Enter = steer).

**Tool result content.** `session.event tool.result` carries a host result's
`content`:

* Text blocks are inline.
* Images are `{type: "image", mediaType, contentRef}` without bytes.
  `session.toolContent {sessionId, callId}` returns the blocks with their bytes
  for the last 200 host results; they are kept in memory, not in state.db.
* A sensitive result has no `content`.

**Routines.**
`job.schedule {…, sessionTemplate: {agent?, mode?, hostToolsFrom, hostWaitSec=300}}`.
At each firing the job waits for a host matching `hostToolsFrom`. That is a
clientKind such as `"browser"`, a clientId or a surface id. The session then
runs with that host's tools. If no such host connects in time, the run fails
with `job.event failed` whose text starts with `host_unavailable`.

**Notices.** `session.notice {sessionId, text}` queues a `[system] …` line that
reaches the model before its next call. It starts no turn. Up to 20 notices are
kept.

**Browsing memory.**

* `memory.ingest {items: [{url, title, text, visitedAt}], sessionId?}` →
  `{added, skipped}`. Pages go to the `browser` namespace, deduplicated by
  url + text hash, keeping the first 4,000 characters.
* `memory.delete {source: "browser", url?, before?}` forgets a page, a whole
  site (a url without a path), visits before a time, or everything.
* `session.create {browserMemory}` opts recall in or out. It defaults to on
  for a browser client.

**Usage.** `usage.summary {since?, until?, groupBy: provider|model|session|day}`
→ `{rows: [{key, inputTokens, outputTokens, calls}], inputTokens,
outputTokens}`. Usage events now carry `provider` and `model`; older events are
counted under their session's model.

## 9. Browser profiles (addendum 8)

Snowpea Browser runs one host per profile. Each host is its own core client
with a stable `clientId` (`snowpea-browser-<uuid>`), and every session a
profile opens carries that id as `hostToolsFrom`. Sessions with no host (IDE,
TUI, CLI) have `hostToolsFrom: null`, and the browser shows them in every
profile.

* **Sessions.** `hostToolsFrom` is on every `session.list` row (live and
  stored), on `sessions.changed`, and on every live `session.event`.
  `session.list {hostToolsFrom, includeUnhosted?}` returns only that profile's
  sessions. With `includeUnhosted: true` it also returns the unhosted ones.
* **Pending cards.** `approval.pending` and `question.pending` carry
  `{request, hostToolsFrom}`, and the `sessions.changed` status updates for
  approvals and questions carry it too. A profile shows only its own cards and
  those with `null`. `approval.request` and `question.request` still go only to
  the claiming client.
* **Always-allow rules.** A global rule stored from an approval in a
  profile's session (scope `always` or `site`) records that profile
  (`hostToolsFrom` on `permission.allowlist.list` rows). It covers only that
  profile's sessions. IDE/CLI sessions match only unscoped rules, as before.
  Project rules in `<workdir>/.snowpea` stay shared by every client.
  * `permission.allowlist.list {hostToolsFrom}` returns that profile's global
    rules plus project rules.
  * `permission.allowlist.remove {patternId, hostToolsFrom}` removes a global
    rule only when it belongs to that profile.
  * With `hostToolsFrom` omitted, both calls see every rule.
* The wire name is `hostToolsFrom`, not `scope`: `scope` already means
  `project` or `global` on these methods.

## 10. Browser sessions never use core's own browser (addendum 9)

This is the owner's rule. In a browser session, browser work always happens in
the Snowpea browser's agent tab. A browser session is one with
`originSurface: "browser"`, a `hostToolsFrom`, or a browser-kind origin
connection.

* **No fallback.** When no host `browser_*` tool is attached (after the §3
  fallback), the built-in `browser_*` tools fail at once with
  `host_unavailable: 브라우저 연결이 끊겼어요. Snowpea 브라우저를 열어 두면 이어서
  할 수 있어요.` (`meta.code: "host_unavailable"`). They never reach
  `local_chromium` or any other built-in provider. They are also left out of
  the session's tool list (`tool.list`, the model's tools) until the browser is
  back, so the model does not pick them.
* **Opt-in.** `session.setBrowserProvider {sessionId, provider:
  "host"|"local"}` → `{sessionId, browserProvider}` is the only way to use
  core's own browser in such a session.
  * It applies to that session only. It is persisted
    (`sessions.browser_provider`) and announced as `sessions.changed {reason:
    "browserProvider"}`.
  * Only a client calls it, on the user's word. No tool reaches it.
  * `"local"` means core's configured provider, and `local_chromium` when
    that provider is `host`.
* **Settings.** `settings_set` carries the `config` tag: it asks in accept and
  auto, is refused in plan, and is never promoted by the allowlist or
  remembered for the session. So a model cannot change a setting unasked on
  any surface. In a browser session, `browser.*` keys are refused outright,
  before anyone is asked, because the browser owns them.

## 11. Routines and a sleeping browser host (addendum 11)

The browser stops its host when idle and starts it lazily, so a routine whose
`hostToolsFrom` names that host can come due while it is gone.

* **Held runs.** Such a firing is claimed and stored as `job_runs.status =
  waiting_for_host`, and `job.event {kind: "waiting_for_host", payload:
  {scheduledAt, hostToolsFrom}}` is emitted. The job's schedule moves on as
  usual.
* **Start.** The held run starts as soon as a matching host registers its
  tools (`tool.register`; the scheduler's tick checks too). It then emits
  `started` / `finished` like any run.
* **Missed.** A held run older than the job's `sessionTemplate.catchUpWindowMinutes`
  (default 60) is recorded as `missed`, with `job.event {kind: "missed"}`.
  `hostWaitSec` now only bounds how long a run that has started waits for the
  host's tools.
* **Waking the host.** `job.nextRunForClient {clientId}` → `{nextRunAt}` gives
  the earliest held run, or the next firing of a job whose `hostToolsFrom` is
  that clientId, so the browser can wake its host just before. It is null when
  there is none.
* **State on job.list (addendum 12).** Each row carries `held: {status:
  waiting_for_host|missed, scheduledAt} | null` (the job's newest occurrence,
  when it is held or was missed) and `catchUpWindowMinutes`.
* **job.update (addendum 12).** `job.update {jobId, sessionTemplate?:
  {catchUpWindowMinutes?, hostWaitSec?}, enabled?, schedule?}` → the updated
  job row.
  * It is a partial patch: fields left out stay as they are.
  * `enabled: false` pauses the job, with no next firing. `enabled: true`
    resumes it from now on its schedule.
  * `schedule` is parsed like `job.schedule`'s spec, and the next run is
    recomputed.
  * It emits `job.event {kind: "updated", payload: {nextRunAt}}`.
  * An unknown jobId or a bad spec fails with -32602.

## 12. Team and named agents in browser sessions (addendum 14)

A child created from a browser session inherits the session's `host_tools_from`,
`origin_client_id`, `workspace_dir` and `browser_provider`, and its
`originSurface: "browser"` (`subagent.inherit_host`). This covers:

* `delegate_task` children;
* `/team` worker and reviewer anchors, and therefore their subagents;
* a named agent created with `agent.create {named: true}` from that session. Its
  host is persisted and comes back when its session is reopened.

So such a child runs the browser's host tools. When the browser is gone, its
browser tools fail with `host_unavailable` (§10). Core never silently uses its
own browser. Slash commands, including `queue_command("/team …")`, are not
restricted in browser sessions.

## 13. Plugin development link

`skill.install {source: <local dir>, link: true}` installs a plugin as a symlink
to the working copy, so edits are live after `skill.reload`. `skill.remove`
drops the link and leaves the directory alone. Installing as a copy replaces
the link.
