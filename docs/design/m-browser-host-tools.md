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
| `tool.invoke` | s → c request | `{sessionId, turnId, callId, name, args, mode, workspaceDir}` → `{ok, output, error?, content?, meta?}` |
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

`browser.provider: "host"` (`tools/browser_providers/host.py`). Shadowing is the
primary path: a host that registers `browser_navigate` replaces the Playwright
tool. With the `host` provider selected, a built-in `browser_*` tool the host did
not shadow is forwarded to the host tool of the same name. With no host attached
it refuses rather than falling back to a different browser.
`setup.applyDefaults {profile: "browser"}` selects it.

## 3. Identity, re-attach, keep-alive

`system.hello` takes `clientKind`, `clientId` (stable per install),
`instanceId` and `keepAlive`.

* A new connection with a `clientId` re-binds every open session whose creator
  had that `clientId` and whose origin connection is gone. Approvals and
  `tool.invoke` then reach the new connection.
* `session.attach {sessionId}` → `{sessionId, hostTools}` makes the caller the
  origin explicitly.
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
* `setup.applyDefaults {profile}` → `{applied, status}`. It is idempotent:
  keyless search (`ddgs`) when the current provider has no key, memory and the
  scheduler on, and `browser.provider: "host"` for the browser profile.
  Gateways a person enabled are left alone; none is on by default. The default
  mode is already `accept`.
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
  It is returned by `session.create` and `session.list` (`workspaceDir`), named
  in the system prompt's environment block, and passed to hosts as
  `tool.invoke.workspaceDir`. Subagents share their parent's.
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
