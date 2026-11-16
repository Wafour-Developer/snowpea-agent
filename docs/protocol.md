<!-- GENERATED — do not edit. Produced by scripts/gen_protocol.py from core/snowpea_core/server/protocol.py. -->

# Snowpea protocol

- **Protocol version:** `1.55.0` (semver)
- **Source of truth:** `core/snowpea_core/server/protocol.py`
- **Generator:** `uv run python scripts/gen_protocol.py`
- **Bindings:** `sdk/src/protocol.ts` (generated alongside this file — never hand-edit)

## Transport

The daemon listens on a loopback-only port chosen at start-up and records it in `$SNOWPEA_HOME/daemon.json` as `{port, pid, token, startedAt, protocolVersion}`.

| surface | endpoint | purpose |
|---|---|---|
| WebSocket | `ws://127.0.0.1:<port>/ws` | JSON-RPC 2.0, bidirectional |
| HTTP `GET` | `http://127.0.0.1:<port>/health` | liveness probe |
| HTTP `GET` | `http://127.0.0.1:<port>/protocol.json` | this schema as JSON |
| HTTP `GET` | `http://127.0.0.1:<port>/version` | server and protocol versions |

Every state-changing method lives on the WebSocket only; the HTTP endpoints are read-only operational helpers.

## Handshake

Immediately after connecting, the client calls `system.hello` with the daemon token, its own `clientVersion`, and the `protocolVersion` it was built against. Any other method before a successful `system.hello` fails with `unauthorized`. A mismatched major version is rejected with `protocol_incompatible`.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "system.hello",
  "params": {
    "token": "<contents of $SNOWPEA_HOME/token>",
    "clientVersion": "0.1.0",
    "protocolVersion": "1.55.0"
  }
}
```

Server capabilities advertised in the `system.hello` result:

- `approvalEvaluate`
- `approvals`
- `audio`
- `batchedFileTools`
- `browserActor`
- `browserMemoryList`
- `checkpoints`
- `commands`
- `commandsBesideTurn`
- `compactionReason`
- `contextWindowSource`
- `ephemeralImages`
- `extensions`
- `externalSessions`
- `fork`
- `fs`
- `goals`
- `holdSec`
- `hostToolManuals`
- `hostToolParallel`
- `hostTools`
- `hostToolsSessionEvents`
- `hostTurnContext`
- `hostTurnContextCompaction`
- `jarvis`
- `jarvisBriefing`
- `jarvisDelegation`
- `jobRuns`
- `lineage`
- `llmSample`
- `llmSampleHost`
- `llmSampleImages`
- `llmTimings`
- `lsp`
- `lspChanged`
- `lspInstall`
- `lspRestart`
- `machineIdentity`
- `mcp`
- `mcpOAuth`
- `mcpPresets`
- `mcpVarExpansion`
- `memoryCandidates`
- `memorySafe`
- `messageOrigin`
- `modelCategories`
- `onceOnlyScope`
- `pageActionAlways`
- `planAutoApprove`
- `projectTrust`
- `providerModelsError`
- `providerStatusEvents`
- `readOnlyDelegation`
- `securityLists`
- `sessionAttachments`
- `sessionContinue`
- `sessionEffort`
- `sessionEvents`
- `sessionOverrides`
- `sessionSetAgent`
- `sessions`
- `setAgentGuardTurn`
- `settings`
- `setup`
- `sharedDaemon`
- `shellRemember`
- `siteActionParts`
- `siteFrameActions`
- `siteMemory`
- `sitePacks`
- `sitePlaybooks`
- `siteRecipes`
- `siteStats`
- `skillLearn`
- `skillToggle`
- `snapshotSupersede`
- `standingOrders`
- `steps`
- `tabReadAlways`
- `toolInvokeGuard`
- `toolInvokeModel`
- `toolProgressState`
- `tools`
- `trajectory`
- `turnOutcome`
- `update`
- `visionNotice`
- `webRemoteControl`
- `webSearch`
- `workspaces`
- `writeOutsideAsks`

## Method index

| method | direction | summary |
|---|---|---|
| [`agent.bindChannel`](#agentbindchannel) | client → server | Route a gateway channel to a named agent. |
| [`agent.create`](#agentcreate) | client → server | Define a named agent from a description. |
| [`agent.delete`](#agentdelete) | client → server | Delete a named agent. |
| [`agent.list`](#agentlist) | client → server | List the named agents that are defined. |
| [`agent.spawn`](#agentspawn) | client → server | Run a named agent on a task. |
| [`approval.ask`](#approvalask) | client → server | Escalate one host action through the approval pipeline. The approver's approval.request carries tool, args with detail nested under args.detail and the site under args.site, note = reason, site, and scopeHint 'site' when a site is known. A local dev server navigation (tool 'repl.navigate', args.reasonCode 'localDevServer', args.url, args.project) gets scopeHint 'project': a 'project' answer stores a rule for the session's project and the URL's origin that only such asks match. |
| [`approval.evaluate`](#approvalevaluate) | client → server | Would a saved rule or the mode allow this action? A host-level, side-effect-free check (1.40.0): the allowlist entries of the caller's own profile (its clientId), then the mode matrix. Nothing is asked or stored. |
| [`approval.list`](#approvallist) | client → server | List tool calls still waiting for a decision. |
| [`approval.request`](#approvalrequest) | server → client | Ask the client to approve a tool call. |
| [`approval.respond`](#approvalrespond) | client → server | Answer a pending approval and unblock the turn. |
| [`audio.capabilities`](#audiocapabilities) | client → server | Report what voice input and output can do on this machine. |
| [`audio.install`](#audioinstall) | client → server | Install a local voice engine (or one of its voices) and re-run detection. |
| [`audio.record.start`](#audiorecordstart) | client → server | Start recording the microphone. |
| [`audio.record.stop`](#audiorecordstop) | client → server | Stop the recording and return the wav it wrote. |
| [`audio.speak`](#audiospeak) | client → server | Synthesise speech, optionally playing it on the daemon's machine. |
| [`audio.transcribe`](#audiotranscribe) | client → server | Transcribe recorded audio to text. |
| [`audio.voices`](#audiovoices) | client → server | List the voices one speech engine offers here. |
| [`checkpoint.delete`](#checkpointdelete) | client → server | Delete one checkpoint, or all checkpoints for a session. |
| [`checkpoint.diff`](#checkpointdiff) | client → server | Show what restoring a checkpoint would change from the current workdir. |
| [`checkpoint.list`](#checkpointlist) | client → server | List restore checkpoints for a session, newest first. |
| [`checkpoint.restore`](#checkpointrestore) | client → server | Restore files to their before-turn state, optionally through later turns. |
| [`command.list`](#commandlist) | client → server | List the slash commands available to a session. |
| [`command.run`](#commandrun) | client → server | Run a slash command; the only execution path for them. |
| [`extension.configure`](#extensionconfigure) | client → server | Save a plugin's config (validated by its configSchema) and reload it (1.49.0). |
| [`extension.disable`](#extensiondisable) | client → server | Unload and disable; everything it registered goes (1.49.0). |
| [`extension.enable`](#extensionenable) | client → server | Enable and load (1.49.0). |
| [`extension.list`](#extensionlist) | client → server | Extensions found in $SNOWPEA_HOME/extensions, rescanned on each call (1.49.0, capability 'extensions'). |
| [`extension.reload`](#extensionreload) | client → server | Unload and load again, picking up edits (1.49.0). |
| [`external.clis`](#externalclis) | client → server | External sessions (1.53.0, WP5): Claude Code, Codex, agy and Cursor agent on the daemon host: installed, version, path, what works, problems. |
| [`external.clis.confirmPath`](#externalclisconfirmpath) | client → server | External sessions (security review H2): the person confirms the resolved path of a jarvis.external.clis.<cli>.path override (ExternalCliInfo.pendingPath) so snowpea may run it; person's local connection only (jarvis_person_only). |
| [`external.close`](#externalclose) | client → server | External sessions (1.53.0, WP5): stop the process snowpea runs for it and unsubscribe; the CLI's own transcript stays. A screen that only goes away calls external.unwatch instead. |
| [`external.create`](#externalcreate) | client → server | External sessions (1.53.0, WP6): start a new session of a CLI in a registered project root and open it; the calling connection is subscribed to its external.event stream as with external.open. |
| [`external.get`](#externalget) | client → server | External sessions (1.53.0): one session's row without opening or reading it (deep links, pop-outs, restored tabs). not_found when the CLI has no such session; outside_root when it is not in a registered project. |
| [`external.interrupt`](#externalinterrupt) | client → server | External sessions (1.53.0, WP6): stop the running turn. host 'tmux': Escape to the pane while Claude is busy (whoever started the turn; never C-c); host 'terminal': external_view_only; nothing running: ok, no-op. |
| [`external.list`](#externallist) | client → server | External sessions (1.53.0, WP5/6): one working directory's sessions (or every exposed project's) for the requested CLIs, newest first, paged. |
| [`external.open`](#externalopen) | client → server | External sessions (1.53.0, WP5/6): subscribe this connection to a session's external.event stream and return the newest page of its timeline; a host 'none' session is continued under snowpea (host 'snowpea'). |
| [`external.read`](#externalread) | client → server | External sessions (1.53.0, WP5/6): an older page of a session's timeline. |
| [`external.send`](#externalsend) | client → server | External sessions (1.53.0, WP6): send a prompt; the reply streams as external.event to the sender, which is subscribed to the ref (idempotent; no external.open needed). A host 'none' session is continued first in its current mode or the CLI's defaultMode ('unrestricted' only for a human connection, else jarvis_person_only). During a running turn: added to that turn when capabilities.steer (same turnId back), else session_busy. A tmux pane that is not ready answers session_busy with error.data.details.reason terminal_prompt, terminal_busy or terminal_typing. Refused with external_view_only while canSend is false (host 'terminal'). A send from a human connection pauses Jarvis in that session (jarvis.quietMinutes). |
| [`external.setMode`](#externalsetmode) | client → server | External sessions (1.53.0, WP6): switch a session to default, readonly or unrestricted; unrestricted only from a human connection. |
| [`external.unwatch`](#externalunwatch) | client → server | External sessions (1.53.0): stop sending this connection external.event for the session; the process keeps running (an idle one is stopped after jarvis.external.idleSec). Unknown or never-opened refs: ok. |
| [`file.complete`](#filecomplete) | client → server | Complete file and directory paths under a session workdir. |
| [`fs.glob`](#fsglob) | client → server | Files and directories matching a relative glob ('*', '?', '[..]', and a '**' segment for any depth), capped by maxEntries (1.40.0). Symbolic links are matched, never followed; '**' does not enter .git or node_modules. |
| [`fs.hash`](#fshash) | client → server | SHA-256 of a file, read in a stream (1.40.0). |
| [`fs.list`](#fslist) | client → server | List a directory, recursively up to depth, capped by maxEntries (1.40.0). Symbolic links are listed, never followed. |
| [`fs.read`](#fsread) | client → server | Read a byte range of a file, at most 8 MiB per call (1.40.0). Errors outside_root, protected, not_found, is_dir, too_large. |
| [`fs.stat`](#fsstat) | client → server | File metadata inside the session's roots (1.40.0): type, size, mtimeMs, nlink, isLink. Errors outside_root, not_found. |
| [`fs.write`](#fswrite) | client → server | Write a whole file through a temp file and an atomic rename, at most 8 MiB (1.40.0). 'create' never replaces; parents are created inside the roots. Errors outside_root, protected, exists, is_dir, too_large. |
| [`gateway.bind`](#gatewaybind) | client → server | Attach the daemon to a chat platform channel. |
| [`gateway.list`](#gatewaylist) | client → server | List live gateway bindings. |
| [`gateway.sync`](#gatewaysync) | client → server | Reconcile the messenger bindings with settings.gateway. |
| [`gateway.unbind`](#gatewayunbind) | client → server | Detach a gateway binding. |
| [`goal.clear`](#goalclear) | client → server | Remove the goal (1.43.0). |
| [`goal.get`](#goalget) | client → server | Read the session's goal (1.43.0). |
| [`goal.pause`](#goalpause) | client → server | Pause and disarm the goal (1.43.0). |
| [`goal.resume`](#goalresume) | client → server | Re-arm a paused or blocked goal, optionally with more rounds, and start a round when idle (1.43.0); goal_not_allowed on a delegated child or a scheduled run (1.50.0). |
| [`goal.set`](#goalset) | client → server | Give the session a goal and arm it; rounds run until goal_update completes or blocks it, or the round cap (1.43.0, capability 'goals'). A delegated child or a scheduled run is refused with goal_not_allowed (1.50.0). |
| [`host.turnContext`](#hostturncontext) | server → client | At turn start, ask the session's host-tools connection for a note for the turn's first model call (1.31.0). Only on a browser connection that sent tool.register {capabilities: ['turnContext']}; 300 ms deadline, fail-open. |
| [`jarvis.briefing.get`](#jarvisbriefingget) | client → server | Jarvis mode (1.53.0, WP8): the periodic briefing's settings and next run. |
| [`jarvis.briefing.set`](#jarvisbriefingset) | client → server | Jarvis mode (1.53.0, WP8): turn the periodic briefing on or off, or change it. |
| [`jarvis.delegation.list`](#jarvisdelegationlist) | client → server | Jarvis mode (1.53.0, WP2): delegation grants in force. |
| [`jarvis.delegation.revoke`](#jarvisdelegationrevoke) | client → server | Jarvis mode (1.53.0, WP2): revoke a grant at once. |
| [`jarvis.delegation.set`](#jarvisdelegationset) | client → server | Jarvis mode (1.53.0, WP2): create or replace a grant. The calling connection is the person, so it applies at once; Jarvis's own proposals never use this method but raise a person-only approval. A guarded class needs a local connection or a re-authenticated remote one; from a messenger only when jarvis.messenger.allowGrants. |
| [`jarvis.disable`](#jarvisdisable) | client → server | Jarvis mode (1.53.0, WP3): clear jarvis.enabled and park the Jarvis session; everything routes to the person again, messenger bindings get their targets back. |
| [`jarvis.enable`](#jarvisenable) | client → server | Jarvis mode (1.53.0, WP3): set jarvis.enabled, create or resume the Jarvis session and start supervising every exposed project. Idempotent. |
| [`jarvis.escalations.dismiss`](#jarvisescalationsdismiss) | client → server | Jarvis mode (1.53.0): dismiss escalations by id (or all); every client is told with jarvis.escalation.dismissed. |
| [`jarvis.escalations.list`](#jarvisescalationslist) | client → server | Jarvis mode (1.53.0): the open escalations that are not a waiting approval or question (finished, error, stalled, budget, jarvis), newest first, paged. They persist until dismissed, across client reloads and daemon restarts. |
| [`jarvis.handback`](#jarvishandback) | client → server | Jarvis mode (1.53.0, WP2): give one session back to Jarvis now, ending a takeover or a typing pause. Human connections only (else jarvis_person_only). |
| [`jarvis.ledger`](#jarvisledger) | client → server | Jarvis mode (1.53.0, WP2): decisions made for supervised sessions, newest first, by Jarvis, the person or the engine alone, with classification, rule and grant. |
| [`jarvis.overview`](#jarvisoverview) | client → server | Jarvis mode (1.53.0, WP3): one row per supervised session, snowpea and external. |
| [`jarvis.project.alias`](#jarvisprojectalias) | client → server | Jarvis mode (1.53.0, WP3): add or remove a project's aliases. |
| [`jarvis.project.list`](#jarvisprojectlist) | client → server | Jarvis mode (1.53.0, WP3): projects with the name, aliases and exposure Jarvis and clients use in conversation. |
| [`jarvis.project.rename`](#jarvisprojectrename) | client → server | Jarvis mode (1.53.0, WP3): set or clear a project's display name. |
| [`jarvis.status`](#jarvisstatus) | client → server | Jarvis mode (1.53.0, WP3): enabled, the Jarvis session, model, folder, budget, pauses. |
| [`jarvis.takeover`](#jarvistakeover) | client → server | Jarvis mode (1.53.0, WP2): the person takes one session back; Jarvis stops deciding for it until jarvis.handback or the given minutes. |
| [`job.cancel`](#jobcancel) | client → server | Cancel a scheduled job. |
| [`job.delete`](#jobdelete) | client → server | Delete a job, its schedule and run history; optionally its run sessions. |
| [`job.list`](#joblist) | client → server | List scheduled jobs and their next run times. |
| [`job.nextRunForClient`](#jobnextrunforclient) | client → server | When a host should be awake for its routines (addendum 11). |
| [`job.preview`](#jobpreview) | client → server | Read a schedule spec without saving anything (1.10.0): its kind and when a job with it would first fire. An unreadable spec is invalid_params with job.schedule's message. |
| [`job.runNow`](#jobrunnow) | client → server | Fire a scheduled job immediately. |
| [`job.runs`](#jobruns) | client → server | A job's run history, newest first (1.33.0). |
| [`job.schedule`](#jobschedule) | client → server | Schedule a prompt to run unattended. |
| [`job.update`](#jobupdate) | client → server | Change an existing job (addendum 12). |
| [`llm.sample`](#llmsample) | client → server | One tool-less model call for a session's browser host (1.14.0): no tools, no history, nothing written to the transcript except a usage event. The session's host only ('unauthorized' otherwise); 'unavailable' when no model can answer; with schema, 'invalid_output' when the answer does not validate. |
| [`lsp.catalog`](#lspcatalog) | client → server | List every registered language server, regardless of whether it has started. |
| [`lsp.install`](#lspinstall) | client → server | Install language servers the user asked for, even with lsp.autoInstall off. |
| [`lsp.restart`](#lsprestart) | client → server | Stop a language server (or all) so it starts afresh; missing ones are re-probed. |
| [`lsp.status`](#lspstatus) | client → server | Report every language server the daemon has started and its state. |
| [`mcp.add`](#mcpadd) | client → server | Write an MCP server into the project or global .mcp.json and start it. |
| [`mcp.authorize`](#mcpauthorize) | client → server | Start an OAuth sign-in for a remote MCP server; returns the consent URL (1.34.0). |
| [`mcp.authorize.complete`](#mcpauthorizecomplete) | client → server | Finish an mcp.authorize flow with the code the host's own redirect caught. |
| [`mcp.catalog`](#mcpcatalog) | client → server | The curated MCP servers a client can offer as presets. |
| [`mcp.deauthorize`](#mcpdeauthorize) | client → server | Forget a remote MCP server's stored OAuth tokens (1.34.0). |
| [`mcp.list`](#mcplist) | client → server | List every configured MCP server with its scope, state and tools. |
| [`mcp.preset.apply`](#mcppresetapply) | client → server | Assistant presets (1.53.0, WP8): add the preset's MCP server with the given inputs and schedule the chosen recipes. |
| [`mcp.preset.check`](#mcppresetcheck) | client → server | Assistant presets (1.53.0): whether each server of an applied preset is connected and still signed in (masked account; no tokens). |
| [`mcp.preset.list`](#mcppresetlist) | client → server | Assistant presets (1.53.0, WP8): mail, calendar and to-do MCP setups with recipes. |
| [`mcp.preset.remove`](#mcppresetremove) | client → server | Assistant presets (1.53.0, WP8): undo an applied preset: its servers, permission tags, stored client secret and recipe jobs go back to what they were before. |
| [`mcp.reload`](#mcpreload) | client → server | Restart one MCP server, or every configured one. |
| [`mcp.remove`](#mcpremove) | client → server | Delete an MCP server entry and stop the server. |
| [`mcp.test`](#mcptest) | client → server | Probe a saved MCP server or an unsaved draft and report its tools. |
| [`mcp.update`](#mcpupdate) | client → server | Merge a patch into an existing MCP server entry. |
| [`memory.confirm`](#memoryconfirm) | client → server | Accept or drop a preference a memory.candidate event proposed (1.28.0). |
| [`memory.delete`](#memorydelete) | client → server | Forget one stored memory. |
| [`memory.ingest`](#memoryingest) | client → server | Remember visited pages in the browser namespace, once per url and text. |
| [`memory.list`](#memorylist) | client → server | List stored memories by scope, newest first. |
| [`memory.search`](#memorysearch) | client → server | Recall stored memories matching a query. |
| [`memory.update`](#memoryupdate) | client → server | Edit one stored memory in place; it becomes confirmed (1.28.0). |
| [`memory.write`](#memorywrite) | client → server | Store a memory with tags. |
| [`permission.allowlist.add`](#permissionallowlistadd) | client → server | Promote a pattern from ask to allow. |
| [`permission.allowlist.list`](#permissionallowlistlist) | client → server | List stored allowlist patterns. |
| [`permission.allowlist.remove`](#permissionallowlistremove) | client → server | Delete an allowlist pattern by id. |
| [`project.trust.revoke`](#projecttrustrevoke) | client → server | Project trust (1.55.0): take scopes back (all when omitted). A local person only. |
| [`project.trust.set`](#projecttrustset) | client → server | Project trust (1.55.0): trust the project for scopes, pinning every control file as it is now. A local person only. |
| [`project.trust.status`](#projecttruststatus) | client → server | Project trust (1.55.0): what the project's own files would make snowpea do (hooks, MCP servers, commands with allowed-tools, agents with a permission, prompt overrides, widening settings, defaultMode auto), which are active, and what changed since it was trusted. People (a remote login too). |
| [`provider.configure`](#providerconfigure) | client → server | Store settings and credentials for a provider. |
| [`provider.list`](#providerlist) | client → server | List chat providers and whether they are configured. |
| [`provider.loginWeb`](#providerloginweb) | client → server | Start a browser-based login flow for a provider. |
| [`provider.models`](#providermodels) | client → server | Ask a vendor's endpoint which models it serves. |
| [`provider.remove`](#providerremove) | client → server | Forget a configured provider, typically a named local server. |
| [`provider.test`](#providertest) | client → server | Send one short completion to check a provider and model. |
| [`provider.visionReset`](#providervisionreset) | client → server | Forget the learned vision answer for a model so the next picture is tried again (1.41.0, capability 'visionNotice'). A configured providers.<v>.vision wins as before. |
| [`question.list`](#questionlist) | client → server | List questions the agent is still waiting on. |
| [`question.request`](#questionrequest) | server → client | Ask the client to put a question to the human. |
| [`question.respond`](#questionrespond) | client → server | Answer a pending question and unblock the turn. |
| [`security.check`](#securitycheck) | client → server | Security lists (1.54.0): judge a command or a path against the lists (or an unsaved draft) without running anything — the 'try it' box. |
| [`security.get`](#securityget) | client → server | Security lists (1.54.0): the stored global lists, a project's additions, the effective lists with presets expanded, and whether this connection may change them (settings.set {scope: 'global', patch: {security: …}}, a local person only; arrays replace). |
| [`security.presets.list`](#securitypresetslist) | client → server | Security lists (1.54.0): the preset catalog of blocked commands and protected paths, localized (en, ko). People only (not a browser or connector). |
| [`session.archive`](#sessionarchive) | client → server | Archive or restore a conversation; refused while it runs unless stop (1.47.0). |
| [`session.artifacts`](#sessionartifacts) | client → server | Files the session saved for the user under its workspace's artifacts/. |
| [`session.attach`](#sessionattach) | client → server | Make this connection the origin of a session (approvals, host tools). |
| [`session.attachments`](#sessionattachments) | client → server | Files the user attached in this session, with the path of the original bytes the agent may use (1.23.0). A delegated child reports its root's. |
| [`session.close`](#sessionclose) | client → server | Close a session and release its resources. |
| [`session.compact`](#sessioncompact) | client → server | Summarise the conversation so far and replace the history with it. |
| [`session.continue`](#sessioncontinue) | client → server | Continue a chat session on the system's behalf, e.g. after a human hand-back (1.8.0). A running turn gets the note at its next model call (like session.notice); otherwise a turn starts whose input is the [system] note, with turn.started initiator 'system' and no message.user. Same callers as session.setAgent; subagent and scheduled sessions are refused with 'invalid_params'. One system start per session per 10 s, and an idempotencyKey is honoured once; a call inside the 10 s window keeps its note as a notice (queued: true). |
| [`session.create`](#sessioncreate) | client → server | Open a session rooted at a working directory. |
| [`session.deleteSaved`](#sessiondeletesaved) | client → server | Delete saved sessions. |
| [`session.events`](#sessionevents) | client → server | A session's saved events, read only (1.27.0): unlike session.resume it does not subscribe, attach, restore or take the session's approvals. |
| [`session.fork`](#sessionfork) | client → server | Branch a session into a new one at the end of a finished turn; the source is not touched (1.46.0, capability 'fork'). Refusals carry error.data.code fork_after_compaction, fork_turn_not_found, fork_no_finished_turn, fork_child_session or fork_running (1.50.0). |
| [`session.interrupt`](#sessioninterrupt) | client → server | Stop the running turn as soon as possible. |
| [`session.lineage`](#sessionlineage) | client → server | A session's ancestors (breadcrumbs) and delegated descendants as a tree (1.48.0, capability 'lineage'); 1.50.0 follows forks up the chain and lists direct forks. |
| [`session.list`](#sessionlist) | client → server | List live or saved sessions. |
| [`session.notice`](#sessionnotice) | client → server | Queue a [system] line for the model's next call. |
| [`session.pin`](#sessionpin) | client → server | Pin or unpin a conversation (1.47.0). |
| [`session.prompt`](#sessionprompt) | client → server | Send user text to a session and start a turn. Events: turn.started {turnId, prompt, queued}, then message.user {text, attachments, refs, steered: false}, then the model's output. message.user.text is what the user typed; a page attachment is listed as {kind: page, name, url, title} and only the model sees the rendered page. A steered prompt emits message.user {steered: true} + turn.dequeued {reason: steered}; a queued one emits turn.queued first. |
| [`session.rename`](#sessionrename) | client → server | Set a session's title. |
| [`session.resume`](#sessionresume) | client → server | Replay the events a disconnected client missed. |
| [`session.setActive`](#sessionsetactive) | client → server | Mark the session the user is looking at as the active one: a messenger with gateway.<platform>.shareActive posts its questions and approvals to the owner's chat. session.prompt and session.steer from a client do the same. Subagent and scheduled sessions are refused with 'invalid_params'. |
| [`session.setAgent`](#sessionsetagent) | client → server | Run a session as an agent definition, or as none (addendum 17). Only the session's origin connection, a browser session's hostToolsFrom host, or a non-browser surface (for a session it originated or that is not a browser session) may call it; others get 'unauthorized'. |
| [`session.setBrowserProvider`](#sessionsetbrowserprovider) | client → server | Per-session opt-in to core's own browser; never global (addendum 9). |
| [`session.setEffort`](#sessionseteffort) | client → server | Pin how hard a session's model may think, or clear the pin. |
| [`session.setMode`](#sessionsetmode) | client → server | Switch a session between plan, accept and auto. |
| [`session.setModel`](#sessionsetmodel) | client → server | Pin a session to a model profile, or clear the pin. |
| [`session.steer`](#sessionsteer) | client → server | Inject a user message into a running turn at its next tool round; never a slash command (steer_slash_command, 1.50.0). |
| [`session.toolContent`](#sessiontoolcontent) | client → server | A host tool result's content blocks with image bytes; results with images are kept on disk. |
| [`session.trajectory`](#sessiontrajectory) | client → server | The session's event log as turn-aware rows: turns, model calls with timing and usage, tool calls with results, delegations, compactions (1.45.0, capability 'trajectory'). |
| [`settings.get`](#settingsget) | client → server | Read global or project settings, with secrets masked. |
| [`settings.set`](#settingsset) | client → server | Deep-merge a patch into global or project settings and persist it. |
| [`setup.applyDefaults`](#setupapplydefaults) | client → server | Apply the profile's defaults for everything optional; idempotent. |
| [`setup.catalog`](#setupcatalog) | client → server | The setup wizard's vendor, search, browser, tools and gateway catalogs. |
| [`setup.status`](#setupstatus) | client → server | What setup still needs; for the browser profile only a tested provider is required. |
| [`site.delete`](#sitedelete) | client → server | Site memory (1.9.0): delete one entry, or every entry of an origin, in one browser profile. Same callers as site.list. |
| [`site.get`](#siteget) | client → server | Site memory (1.9.0): the entries stored for an origin in the caller's browser profile; with path, only those whose urlPattern matches it, most specific first; with pageType, only that one. Stale entries are included, flagged. Browser host clients only; entries are untrusted page-derived data and core never puts them in a prompt. |
| [`site.list`](#sitelist) | client → server | Site memory (1.9.0): one browser profile's remembered sites, by origin, for a settings UI. A browser client sees its own profile; any other owner client names one with hostToolsFrom. |
| [`site.mark`](#sitemark) | client → server | Site memory (1.9.0): 'ok' counts a success, sets lastVerified and clears stale; 'stale' counts a failure and flags the entry until the next put. Browser host clients only. |
| [`site.markPart`](#sitemarkpart) | client → server | Site memory (1.15.0): count one use of a flow or recipe of an entry without a put. The entry's successCount, failureCount, stale and lastVerified are untouched, so this never counts as verification. 'not_found' for an unknown entry, flow or recipe. Browser host clients only. |
| [`site.playbook.delete`](#siteplaybookdelete) | client → server | Site playbooks (1.38.0): delete one playbook, or every playbook of an origin. site.delete of a whole origin removes them too. |
| [`site.playbook.edit`](#siteplaybookedit) | client → server | Site playbooks (1.38.0): one edit the user makes in settings: rename, alwaysAsk, retire, unretire, restore (swap with the previous version), acknowledge, removeSegment, removePitfall. A browser client edits its own profile; any other owner client names one with hostToolsFrom. |
| [`site.playbook.get`](#siteplaybookget) | client → server | Site playbooks (1.38.0): the typed task procedures stored for an origin, by name. A browser client reads its own profile; any other owner client names one with hostToolsFrom. Untrusted page-derived data; core never puts it in a prompt. Capability sitePlaybooks. |
| [`site.playbook.mark`](#siteplaybookmark) | client → server | Site playbooks (1.38.0): append one run (day, result, segments done of total, rounds) to the history, keeping the last 10; an 'ok' run sets verifiedAt. Counts for site.stats. Browser host clients only. |
| [`site.playbook.put`](#siteplaybookput) | client → server | Site playbooks (1.38.0): upsert a playbook by (origin, name). Core refuses strings that look like personal data or credentials, a step that names a login field, a slot or a step the schema cannot express, and 16 KB per playbook or 10 per origin ('invalid_params' naming the field). Core owns id, version and prev: a change of slots or segments keeps the stored body one deep as prev and counts the version up. alwaysAsk, acked and an empty history keep their stored values. Browser host clients only. |
| [`site.put`](#siteput) | client → server | Site memory (1.9.0): upsert an entry by (origin, pageType). Core checks the schema, refuses XPath, values on login fields and strings that look like personal data or credentials, and enforces 24 KB per entry (16 KB before 1.15.0) and 30 entries per origin ('invalid_params' naming the field); past 1,000 origins the least recently verified origin is dropped. A put clears stale and counts as a success. Browser host clients only. |
| [`site.stats`](#sitestats) | client → server | Site memory (1.27.0): per-origin counts of the profile's site.put, site.mark and site.markPart calls over the last days. Browser clients only, for their own profile; nothing page-derived is in it. |
| [`skill.create`](#skillcreate) | client → server | Write a new SKILL.md, generated from a brief or supplied verbatim. |
| [`skill.install`](#skillinstall) | client → server | Install a skill from a path, URL or registry. |
| [`skill.learn`](#skilllearn) | client → server | Turn a session's history into a SKILL.md, as /skill learn does, in the project or the user's skills (1.28.0). Answers when it is written and loaded. |
| [`skill.list`](#skilllist) | client → server | List installed skills. |
| [`skill.read`](#skillread) | client → server | Read a skill's SKILL.md. |
| [`skill.reload`](#skillreload) | client → server | Reload skills from disk without restarting. |
| [`skill.remove`](#skillremove) | client → server | Delete a plugin, or a global or project skill, agent or command (1.33.0). |
| [`skill.search`](#skillsearch) | client → server | Search available skills. |
| [`skill.write`](#skillwrite) | client → server | Save a skill's SKILL.md verbatim; says where, and whether it is in effect. |
| [`system.checkUpdate`](#systemcheckupdate) | client → server | Report whether a newer snowpea release exists; cached for 24h. |
| [`system.health`](#systemhealth) | client → server | Liveness probe; answers as long as the daemon serves requests. |
| [`system.hello`](#systemhello) | client → server | Authenticate a connection and agree on the protocol version. |
| [`system.info`](#systeminfo) | client → server | Report the daemon's version, pid, port, start time and home. |
| [`system.reloadSettings`](#systemreloadsettings) | client → server | Re-read settings.json and rebind the daemon's in-memory state. |
| [`system.restart`](#systemrestart) | client → server | Shut the daemon down so the next launch runs the newly installed version. |
| [`system.shutdown`](#systemshutdown) | client → server | Ask the daemon to shut down gracefully. |
| [`system.update`](#systemupdate) | client → server | Upgrade snowpea in a detached subprocess and report progress. |
| [`team.guide.delete`](#teamguidedelete) | client → server | Delete a team guide from the project or global home. |
| [`team.guide.get`](#teamguideget) | client → server | Read a team guide for a project or session. |
| [`team.guide.list`](#teamguidelist) | client → server | List every team guide visible in a project. |
| [`team.guide.set`](#teamguideset) | client → server | Write a team guide to the project or global home. |
| [`team.start`](#teamstart) | client → server | Split a task across parallel workers. |
| [`team.status`](#teamstatus) | client → server | Inspect a team's task board. |
| [`tool.invoke`](#toolinvoke) | server → client | Ask the client to run one of its host tools. |
| [`tool.list`](#toollist) | client → server | List the tools registered for a session. |
| [`tool.progress`](#toolprogress) | client → server | Notification: progress of a running tool.invoke, re-emitted as tool.progress. |
| [`tool.register`](#toolregister) | client → server | Register tools this connection runs itself (host tools). |
| [`tool.unregister`](#toolunregister) | client → server | Remove some of this connection's host tools. |
| [`turn.outcome`](#turnoutcome) | client → server | Record whether a turn did what was asked (1.27.0): a browser client, for its own sessions. Stored as a turn.outcome session event. |
| [`usage.summary`](#usagesummary) | client → server | Token totals from stored usage events, grouped by provider, model, session or day. |
| [`web.remote.clearPassword`](#webremoteclearpassword) | client → server | Remote web access (1.54.0): remove the password (back to the access link); every open session ends. |
| [`web.remote.listen`](#webremotelisten) | client → server | Remote web access (1.54.0): open (or move) the external listener now, without a daemon restart; save also writes web.remote.*. Every connection through it is a remote web login. A local person only. |
| [`web.remote.rotateToken`](#webremoterotatetoken) | client → server | Remote web access (1.54.0): a new web access token; the old link stops working and every open session ends. |
| [`web.remote.setPassword`](#webremotesetpassword) | client → server | Remote web access (1.54.0): store a password (scrypt hash) — password mode: the login page is required; every open session ends. A local person only. |
| [`web.remote.status`](#webremotestatus) | client → server | Remote web access (1.54.0): the external listener, its mode, the addresses and the access link. A local person only (never a remote login, browser or connector). |
| [`web.remote.stop`](#webremotestop) | client → server | Remote web access (1.54.0): close the external listener; its sessions end. |
| [`web.search`](#websearch) | client → server | Web search for a browser host (1.11.0): the web_search tool's provider chain, settings, caps and timeouts, for the host's own fallback when its search page is blocked. Browser host clients (with a clientId) only; 'unavailable' with details.reason when no provider can answer. Results are untrusted web content. |
| [`workspace.add`](#workspaceadd) | client → server | Add a project folder (1.47.0). |
| [`workspace.list`](#workspacelist) | client → server | Project folders in sidebar order, each with its conversation count (1.47.0, capability 'workspaces'). |
| [`workspace.remove`](#workspaceremove) | client → server | Remove a folder from the sidebar; its conversations stay (1.47.0). |
| [`workspace.rename`](#workspacerename) | client → server | Rename one (1.47.0). |
| [`workspace.reorder`](#workspacereorder) | client → server | Set the sidebar order (1.47.0). |

## Methods

### `agent.bindChannel`

*Direction:* client → server

Route a gateway channel to a named agent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `channel` | `string` | yes | Gateway channel that will reach the agent. |
| `credentialsRef` | `string \| null` | no | Credential entry the platform account uses; defaults to the platform name, e.g. 'telegram' for channel 'telegram:123'. |
| `name` | `string` | yes | Agent to bind. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `agent.create`

*Direction:* client → server

Define a named agent from a description.

**Params**

| field | type | required | description |
|---|---|---|---|
| `description` | `string` | yes | Natural-language brief the daemon turns into an agent. |
| `name` | `string \| null` | no | Name for the agent; when omitted the daemon takes the generated one. |
| `named` | `boolean` | no | Also register a persistent named instance: its own session, the memory namespace agent:<name>, and rows that survive a daemon restart. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Name assigned to the new agent. |
| `path` | `string \| null` | no | Where the definition was written. |

### `agent.delete`

*Direction:* client → server

Delete a named agent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Agent to delete. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `agent.list`

*Direction:* client → server

List the named agents that are defined.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `agents` | `({ active?: boolean \| null; agentId?: string \| null; agents?: string[]; bindings?: string[]; channel?: string \| null; channels?: string[]; description?: string; format?: "snowpea" \| "claude" \| "codex"; hasGuide?: boolean \| null; jobs?: string[]; kind?: string; name: string; namespace?: string \| null; parentSessionId?: string \| null; path?: string \| null; sessionId?: string \| null; source?: string; stages?: Record<string, string>; status?: string \| null; task?: string \| null; })[]` | no | Defined named agents. |

### `agent.spawn`

*Direction:* client → server

Run a named agent on a task.

**Params**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | Run this one spawn on a specific model: a models.profiles id, a 'vendor:model' pair, or a bare vendor. Outranks the agent's own assignment; null uses the configured routing. |
| `name` | `string` | yes | Agent to run. |
| `sessionId` | `string \| null` | no | Parent session, when spawned from one. |
| `task` | `string` | yes | Task handed to the agent. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Id correlating the subagent.* events. |

### `approval.ask`

*Direction:* client → server

Escalate one host action through the approval pipeline. The approver's approval.request carries tool, args with detail nested under args.detail and the site under args.site, note = reason, site, and scopeHint 'site' when a site is known. A local dev server navigation (tool 'repl.navigate', args.reasonCode 'localDevServer', args.url, args.project) gets scopeHint 'project': a 'project' answer stores a rule for the session's project and the URL's origin that only such asks match.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Action details to show. |
| `callId` | `string \| null` | no | The tool.invoke callId, if any. |
| `detail` | `Record<string, unknown> \| null` | no | What the running code is about to do, e.g. {origin, element, action}; shown with the approval and merged into its args. |
| `forceAsk` | `boolean` | no | Always ask a person: the mode matrix may only deny (plan still denies write, send and config; a read is asked) and never auto-allows, not even in auto mode. The allowlist still applies unless risk is 'payment'. For any client's destructive or irreversible actions. |
| `permission` | `"read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside"` | yes | Permission class of the escalated action. |
| `reason` | `string` | yes | What will happen, shown to the person approving. |
| `risk` | `string \| null` | no | Risk hint shown with the approval; default from the permission. 'payment' also means: the allowlist is never consulted and the answer is never stored beyond this one call (scope is coerced to 'once'). |
| `sessionId` | `string` | yes | Session whose tool call is running. |
| `site` | `string \| null` | no | Origin of the page, e.g. https://github.com; derived from detail.origin or args.url if omitted. |
| `tool` | `string` | yes | Host tool asking; the allowlist is keyed by it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | no | Who decided: 'user' (a person answered), 'allowlist', 'mode' (the mode matrix), 'readOnly' (a read-only delegation refuses changes and never asks; 1.14.0), or 'timeout' / 'interrupted' (denied without an answer). |
| `decision` | `"allow" \| "deny"` | yes | allow or deny. |
| `scope` | `"once" \| "session" \| "project" \| "always" \| "site"` | no | Scope the answer applies to; always 'once' for payment. 'project' only for project-safe actions (repl.files, a localDevServer repl.navigate); 'always' for a page action only for repl.tabs, repl.clipboard and (1.13.0) repl.read and repl.attach, others are kept to 'once'. For args.onceOnly (1.12.0) the scope the person picked, which core never stores: the host may keep it on its side. |

### `approval.evaluate`

*Direction:* client → server

Would a saved rule or the mode allow this action? A host-level, side-effect-free check (1.40.0): the allowlist entries of the caller's own profile (its clientId), then the mode matrix. Nothing is asked or stored.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | The action's arguments (a shell command line, a url), for rules on them. |
| `mode` | `"plan" \| "accept" \| "auto"` | no | The permission mode the action would run in; the mode matrix is consulted. |
| `permission` | `"read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside"` | yes | Permission class of the action. |
| `risk` | `string \| null` | no | 'payment' (or args.onceOnly): the allowlist is never consulted. |
| `site` | `string \| null` | no | Origin of the page (https://host); matched against site rules. |
| `tool` | `string` | yes | Tool the action belongs to; the allowlist is keyed by it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `by` | `"mode" \| "allowlist" \| "none"` | yes | What decided: the mode matrix, a saved rule of this profile, or neither. |
| `decision` | `"allow" \| "ask" \| "deny"` | yes | 'allow', 'deny', or 'ask' (a person has to decide). |

### `approval.list`

*Direction:* client → server

List tool calls still waiting for a decision.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ args?: Record<string, unknown>; argsEscaped?: boolean \| null; diff?: string \| null; external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; folder?: string \| null; guard?: string \| null; neverDelegable?: boolean \| null; note?: string; paths?: string[] \| null; personOnly?: boolean \| null; reason?: string \| null; reasonCode?: string \| null; remember?: { display: string; note?: string \| null; pattern: string; scopes: ("project" \| "always")[]; } \| null; requestId: string; risk?: string; route?: "person" \| "jarvis" \| "auto" \| null; scopeHint?: "once" \| "session" \| "project" \| "always" \| "site"; sessionId: string; site?: string \| null; timeoutSec?: number; tool: string; })[]` | no | Approvals still pending. |

### `approval.request`

*Direction:* server → client

Ask the client to approve a tool call.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments it wants to use. |
| `argsEscaped` | `boolean \| null` | no | True when the call's arguments held characters a surface would strip or that change how text reads (C0/C1 controls, escape sequences, NUL, bidi and invisible format characters) (1.53.0). args then shows them written out (a backslash escape such as x9d or u202e); what runs is the raw text. Such a request is never remembered or cached, and the delegation engine reports it in every guarded class (rule exec:opaque:control-chars). |
| `diff` | `string \| null` | no | For patch and write_file: the unified diff the call would write, computed without writing (1.29.0). Null for other tools, for a call that would fail, or when the diff is over 200 KB. |
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | Set when the call comes from an external CLI session snowpea hosts (1.53.0); sessionId is then 'ext:<cli>:<id>' and the answer goes back to that CLI. Such a request is never sent as approval.request to a connection: it waits in the shared queue and is announced as approval.pending (with route) to every authenticated surface, listed by approval.list and answered with approval.respond. |
| `folder` | `string \| null` | no | For 'outside_project': the folder a 'project' or 'always' answer allows writes into from then on (kept with the owner's global rules, never in the project), and a 'session' answer for this session (1.30.0). Null when that folder would be too wide (a filesystem root, the home folder, or above the project): every answer then covers this call only. |
| `guard` | `string \| null` | no | Why this is asked whatever the mode (1.24.0): 'agent-switch' for a call in the rest of a turn session.setAgent {guardTurn} guarded. Such an answer is never remembered. |
| `neverDelegable` | `boolean \| null` | no | True when only a person may decide this request (1.54.0): it is personOnly, or the delegation engine classifies it 'never' (snowpea's own settings and secrets, the snowpea CLI, delegation changes, …), or the security lists cannot check it. Clients label it "always asks you". Null otherwise; additive and informational: core enforces it whatever the client shows. |
| `note` | `string` | no | Extra warning shown with the prompt, e.g. "modifies snowpea configuration". |
| `paths` | `string[] \| null` | no | With reasonCode: the resolved absolute paths concerned (1.30.0). |
| `personOnly` | `boolean \| null` | no | True on a request only a person may answer (1.53.0): Jarvis's delegation_set proposal (tool 'delegation_set', args {scope, target, level, allow, expiresAt, note}, from the Jarvis session). Answered with approval.respond {decision, scope: 'once'}: 'allow' only from a person's connection (else jarvis_person_only); 'deny' from anyone who sees it. |
| `reason` | `string \| null` | no | Why core put this request to the person whatever the mode (1.54.0): 'cannot be checked against your block list' when the security lists are set and part of the call cannot be seen (a program only the shell knows, eval of a variable, a pipe into a shell, a path only the shell knows). Such a request is personOnly, routed to the person, never cached or remembered. Null otherwise. 1.55.0 (security review 6) adds two values: 'acts on snowpea's own controls' (the line runs snowpea's own CLI or code, or changes snowpea's own hooks, plugins, skills, commands or agents: asked in every mode, whatever the lists and settings say) and 'cannot tell what this code changes' (only code the check cannot resolve is in the way; security.unknownCode: 'allow' switches this one off). note carries the sentence for the person. |
| `reasonCode` | `string \| null` | no | Why a call is asked by what it touches (1.30.0): 'config' (snowpea settings), 'secret' (a credential or key file), 'outside_project' (a write outside the session's project). Null for an ordinary ask. |
| `remember` | `{ display: string; note?: string \| null; pattern: string; scopes: ("project" \| "always")[]; } \| null` | no | For a shell command (1.34.0, capability 'shellRemember'): the rule a 'project' or 'always' answer would store. Null when only 'once' (or 'session') can be given: a compound or complex line (; && \|\| \| & backticks $ redirects, heredocs, an env-var prefix, unquoted subshells or globs), a program named by a path or with non-ASCII letters, an argument that runs a command (--upload-pack, -c, --exec, rg --pre, …), sudo/su/doas, rm/dd/chmod/kill and the like, shells, interpreters and editors, network tools, npx-style package runners, git push/reset/clean/rm/fetch/pull/clone and other history rewrites, onceOnly args, an agent-switch guard, any reasonCode, and every non-shell call. Core keeps a 'project'/'always' answer to a shell request with a null remember to 'once'. Stored shell rules only ever match one such plain command. |
| `requestId` | `string` | yes | Id to answer with approval.respond. |
| `risk` | `string` | no | Risk hint for the UI. |
| `route` | `"person" \| "jarvis" \| "auto" \| null` | no | Who the delegation engine routed it to while Jarvis mode is on (1.53.0): person (wait for a human, as before), jarvis (the Jarvis session decides; a person may still answer first) or auto (allowed by the engine, shown for the record). Null when Jarvis mode is off. |
| `scopeHint` | `"once" \| "session" \| "project" \| "always" \| "site"` | no | Scope the UI should preselect. |
| `sessionId` | `string` | yes | Session whose turn is blocked. |
| `site` | `string \| null` | no | Origin (scheme://host) a 'site' scope would store the answer for (1.6.0). |
| `timeoutSec` | `number` | no | Seconds before the request is dropped as unanswered; 0 means no limit (1.35.0: a TUI or desktop session waits for its person). |
| `tool` | `string` | yes | Tool the model wants to run. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | A browser host (clientKind 'browser', clientVersion 0.3.0 or later) sets 'person' when the answer came from a click; without it an allow wider than 'once' counts as 'once' (security review 3). Ignored for other kinds. |
| `decision` | `"allow" \| "deny"` | yes | The human's decision. |
| `reason` | `string` | no | Why the human refused; quoted back to the model as the tool's refusal so the next turn can answer it (M15b §1). |
| `scope` | `"once" \| "session" \| "project" \| "always" \| "site"` | no | How long the decision applies. |

### `approval.respond`

*Direction:* client → server

Answer a pending approval and unblock the turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | A browser host (clientKind 'browser', clientVersion 0.3.0 or later) must set 'person' (a click) to allow with a scope wider than 'once' (security review 3); ignored for other kinds. |
| `decision` | `"allow" \| "deny"` | yes | allow runs the tool, deny ends the turn. |
| `reason` | `string` | no | Why the human refused; quoted back to the model as the tool's refusal so the next turn can answer it (M15b §1). |
| `requestId` | `string` | yes | Request being answered. |
| `scope` | `"once" \| "session" \| "project" \| "always" \| "site"` | no | How long the decision applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |
| `scope` | `"once" \| "session" \| "project" \| "always" \| "site" \| null` | no | The scope that applies to this answer (as ApprovalResolvedNotification.scope); null for a deny or when the request had already been answered. |

### `audio.capabilities`

*Direction:* client → server

Report what voice input and output can do on this machine.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `autoSpeak` | `boolean` | no | True when replies are spoken without being asked. |
| `play` | `boolean` | no | True when the daemon can play audio itself. |
| `players` | `string[]` | no | Audio players found on PATH. |
| `reasons` | `Record<string, string>` | no | Per-capability explanation of why it is off. |
| `record` | `boolean` | no | True when the microphone can be recorded. |
| `recorders` | `string[]` | no | Recorders found on PATH. |
| `stt` | `string \| null` | no | Transcription backend in use, or null when there is none. |
| `sttEffective` | `string \| null` | no | The engine actually in use, or null when none is pinned or it is missing. |
| `sttLanguage` | `string` | no | 'auto' (the engine detects, or the reply language decides) or the BCP-47 tag audio.stt.language forces. |
| `sttLanguageSource` | `string` | no | Which rung decided it: setting \| reply \| detect. 'detect' means the engine works it out itself and nothing had to choose. |
| `sttPinned` | `boolean` | no | True when audio.stt.provider names an engine. False means unset, which means voice input is off: there is no fallback chain. |
| `sttProviders` | `string[]` | no | Every usable transcription backend, preferred first. |
| `tts` | `boolean` | no | True when speech synthesis is available. |
| `ttsEffective` | `string \| null` | no | The speech engine actually in use, or null. |
| `ttsPinned` | `boolean` | no | True when audio.tts.provider names an engine. |
| `ttsProvider` | `string \| null` | no | Speech backend in use. |
| `ttsProviders` | `string[]` | no | Every usable speech backend, preferred first. |
| `voice` | `string \| null` | no | Configured voice, when one is set. |

### `audio.install`

*Direction:* client → server

Install a local voice engine (or one of its voices) and re-run detection.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `engine` | `string` | yes | Engine id from the setup catalog: faster-whisper (or local-whisper), piper, edge-tts. A system package (espeak-ng, say, powershell) answers ok=false with a hint instead. |
| `voice` | `string \| null` | no | Install one of the engine's voices rather than the engine itself, e.g. piper's ko_KR-kss-medium. Same staged progress; installing a voice does not select it. |
| `warmup` | `boolean` | no | When true, download first-run assets only — Supertonic's ONNX models — with staged progress, without reinstalling the package. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `engine` | `string` | yes | Engine that was attempted, after id normalisation. |
| `hint` | `string \| null` | no | What to do instead, when ok is false: the platform's own install command for a system package, or why the attempt could not run. |
| `log` | `string` | no | Tail of the installer's combined output, newest last; may be empty. |
| `ok` | `boolean` | yes | True when the engine is installed and now detected. |
| `voice` | `string \| null` | no | The voice that was installed, when the request named one. |

### `audio.record.start`

*Direction:* client → server

Start recording the microphone.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session the recording belongs to. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | no | Media type of the recording. |
| `path` | `string` | yes | Wav file being written, or the finished recording. |
| `provider` | `string \| null` | no | Backend that transcribed it. |
| `recording` | `boolean` | yes | True while capture is still running. |
| `text` | `string \| null` | no | Transcript, when 'stop' was asked to transcribe. |

### `audio.record.stop`

*Direction:* client → server

Stop the recording and return the wav it wrote.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session that is recording. |
| `transcribe` | `boolean` | no | Also transcribe the recording and return its text. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | no | Media type of the recording. |
| `path` | `string` | yes | Wav file being written, or the finished recording. |
| `provider` | `string \| null` | no | Backend that transcribed it. |
| `recording` | `boolean` | yes | True while capture is still running. |
| `text` | `string \| null` | no | Transcript, when 'stop' was asked to transcribe. |

### `audio.speak`

*Direction:* client → server

Synthesise speech, optionally playing it on the daemon's machine.

**Params**

| field | type | required | description |
|---|---|---|---|
| `language` | `string \| null` | no | BCP-47 language for voice selection; defaults to the stt language setting. |
| `play` | `boolean` | no | Play on the daemon's machine instead of returning only a path. |
| `provider` | `string \| null` | no | Speech backend to use for this call only. When set, the pinned voice-out engine is not required — settings screens use this to preview before saving. |
| `sessionId` | `string \| null` | no | Session the audio belongs to. |
| `text` | `string` | yes | What to say. |
| `voice` | `string \| null` | no | Voice id; defaults to the setting. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mime` | `string` | yes | Media type of that file. |
| `path` | `string` | yes | Audio file the speech was written to. |
| `played` | `boolean` | no | True when the daemon played it. |
| `provider` | `string` | yes | Backend that synthesised it. |
| `voice` | `string \| null` | no | Voice that was used. |

### `audio.transcribe`

*Direction:* client → server

Transcribe recorded audio to text.

**Params**

| field | type | required | description |
|---|---|---|---|
| `data` | `string \| null` | no | Base64 (or data-URI) audio. |
| `language` | `string \| null` | no | BCP-47 hint for the backend. |
| `mime` | `string \| null` | no | Media type of the audio, e.g. audio/wav. |
| `path` | `string \| null` | no | Audio file on the daemon's machine. |
| `sessionId` | `string \| null` | no | Session the audio belongs to. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `provider` | `string` | yes | Backend that produced the transcript. |
| `text` | `string` | yes | What the backend heard. |

### `audio.voices`

*Direction:* client → server

List the voices one speech engine offers here.

**Params**

| field | type | required | description |
|---|---|---|---|
| `engine` | `string` | yes | Engine id, e.g. supertonic, piper, edge-tts. |
| `languages` | `string[]` | no | Filter a large catalogue to these BCP-47 tags; empty uses ko and en plus the session's reply language. Ignored by engines with few voices. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `voices` | `({ gender?: string \| null; id: string; installed?: boolean; label: string; language?: string; sample?: boolean; sizeBytes?: number \| null; })[]` | no | Installed voices first, then by language and id. |

### `checkpoint.delete`

*Direction:* client → server

Delete one checkpoint, or all checkpoints for a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string \| null` | no | Checkpoint to delete; omitted deletes the whole session set. |
| `sessionId` | `string` | yes | Session whose checkpoints are deleted. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `checkpoint.diff`

*Direction:* client → server

Show what restoring a checkpoint would change from the current workdir.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Checkpoint id. |
| `paths` | `string[] \| null` | no | Optional subset of relative paths to diff. |
| `sessionId` | `string` | yes | Session whose checkpoint is inspected. |
| `through` | `boolean` | no | True means restore to before this turn, considering this and all later turns in the session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `files` | `({ binary?: boolean; changedSince?: boolean; patch: string; path: string; restorable?: boolean; })[]` | no | Per-file diffs. |

### `checkpoint.list`

*Direction:* client → server

List restore checkpoints for a session, newest first.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session whose checkpoints are listed. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `checkpoints` | `({ createdAt: string; files?: ({ afterSha?: string \| null; beforeSha?: string \| null; path: string; restorable?: boolean; size?: number; skipped?: "too_large" \| "excluded" \| "unreadable" \| null; source: "tool" \| "shell"; status: "modified" \| "created" \| "deleted"; })[]; id: string; kind: "turn" \| "restore"; prompt?: string; sessionId: string; turnId?: string \| null; workdir: string; })[]` | no | Checkpoints, newest first. |

### `checkpoint.restore`

*Direction:* client → server

Restore files to their before-turn state, optionally through later turns.

**Params**

| field | type | required | description |
|---|---|---|---|
| `dryRun` | `boolean` | no | Report what would happen without writing files. |
| `force` | `boolean` | no | Restore even when a file changed since capture. |
| `id` | `string` | yes | Checkpoint id. |
| `paths` | `string[] \| null` | no | Optional subset of relative paths to diff. |
| `sessionId` | `string` | yes | Session whose checkpoint is inspected. |
| `through` | `boolean` | no | True means restore to before this turn, considering this and all later turns in the session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `checkpointId` | `string \| null` | no | Restore checkpoint id, null for dry-run or no writes. |
| `restored` | `string[]` | no | Paths restored. |
| `skipped` | `({ path: string; reason: "changed_since" \| "not_restorable" \| "missing_blob" \| "excluded"; })[]` | no | Paths that were not restored and why. |

### `command.list`

*Direction:* client → server

List the slash commands available to a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema?: Record<string, unknown>; format?: "snowpea" \| "claude" \| "codex"; name: string; source?: string; summary: string; })[]` | no | Available slash commands. |

### `command.run`

*Direction:* client → server

Run a slash command; the only execution path for them.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): a browser 0.3.0+ must send 'person' to run a command that persists a marker-required setting (commands/persists.py); ignored for other kinds. |
| `args` | `string` | no | Raw argument string, as typed after the name. |
| `name` | `string` | yes | Command name without the leading slash. |
| `sessionId` | `string` | yes | Session to run the command in. |
| `whenBusy` | `"queue" \| "steer" \| null` | no | 1.55.0: while a turn runs, a command of kind 'turn' waits like a prompt (turn.queued); 'steer' cannot fold a command into the running request, so it queues too. An 'instant' command runs beside the turn (command.result). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `besideTurn` | `boolean \| null` | no | 1.55.0: true when the slash command ran beside the running turn (an instant command); absent otherwise. |
| `turnId` | `string` | yes | Id of the started turn; turn.done carries it back. Since 1.55.0, for an instant slash command sent while a turn runs, the command's id ('c-…'): no turn starts and command.result carries it back instead (besideTurn true). |

### `extension.configure`

*Direction:* client → server

Save a plugin's config (validated by its configSchema) and reload it (1.49.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `config` | `Record<string, unknown>` | yes | New config; checked against configSchema, saved, then the plugin reloads. |
| `name` | `string` | yes | Plugin name. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `extension` | `{ config?: Record<string, unknown>; configSchema?: Record<string, unknown> \| null; description?: string; effects?: number; enabled?: boolean; error?: string \| null; name: string; path: string; state: "loaded" \| "unloaded" \| "disabled" \| "error" \| "incompatible"; version?: string; }` | yes | The plugin now. |

### `extension.disable`

*Direction:* client → server

Unload and disable; everything it registered goes (1.49.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Plugin name. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `extension` | `{ config?: Record<string, unknown>; configSchema?: Record<string, unknown> \| null; description?: string; effects?: number; enabled?: boolean; error?: string \| null; name: string; path: string; state: "loaded" \| "unloaded" \| "disabled" \| "error" \| "incompatible"; version?: string; }` | yes | The plugin now. |

### `extension.enable`

*Direction:* client → server

Enable and load (1.49.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Plugin name. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `extension` | `{ config?: Record<string, unknown>; configSchema?: Record<string, unknown> \| null; description?: string; effects?: number; enabled?: boolean; error?: string \| null; name: string; path: string; state: "loaded" \| "unloaded" \| "disabled" \| "error" \| "incompatible"; version?: string; }` | yes | The plugin now. |

### `extension.list`

*Direction:* client → server

Extensions found in $SNOWPEA_HOME/extensions, rescanned on each call (1.49.0, capability 'extensions').

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `extensions` | `({ config?: Record<string, unknown>; configSchema?: Record<string, unknown> \| null; description?: string; effects?: number; enabled?: boolean; error?: string \| null; name: string; path: string; state: "loaded" \| "unloaded" \| "disabled" \| "error" \| "incompatible"; version?: string; })[]` | no | Installed plugins. |

### `extension.reload`

*Direction:* client → server

Unload and load again, picking up edits (1.49.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Plugin name. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `extension` | `{ config?: Record<string, unknown>; configSchema?: Record<string, unknown> \| null; description?: string; effects?: number; enabled?: boolean; error?: string \| null; name: string; path: string; state: "loaded" \| "unloaded" \| "disabled" \| "error" \| "incompatible"; version?: string; }` | yes | The plugin now. |

### `external.clis`

*Direction:* client → server

External sessions (1.53.0, WP5): Claude Code, Codex, agy and Cursor agent on the daemon host: installed, version, path, what works, problems.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `clis` | `({ approvalPolicy?: "asks" \| "auto" \| null; capabilities?: { approvals: "bridged" \| "degraded"; liveView: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; steer?: boolean; streaming: boolean; tmux?: boolean; } \| null; defaultMode?: "default" \| "readonly" \| "unrestricted"; enabled?: boolean; id: "claude" \| "codex" \| "agy" \| "cursor"; installed: boolean; models?: string[]; modelsLoaded?: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; path?: string \| null; pendingPath?: string \| null; problems?: string[]; title: string; version?: string \| null; })[]` | no | All four, in a fixed order. |

### `external.clis.confirmPath`

*Direction:* client → server

External sessions (security review H2): the person confirms the resolved path of a jarvis.external.clis.<cli>.path override (ExternalCliInfo.pendingPath) so snowpea may run it; person's local connection only (jarvis_person_only).

**Params**

| field | type | required | description |
|---|---|---|---|
| `cli` | `"claude" \| "codex" \| "agy" \| "cursor"` | yes | Which CLI. |
| `path` | `string` | yes | The resolved path the person saw (ExternalCliInfo.pendingPath); it must still be what jarvis.external.clis.<cli>.path resolves to, else invalid_params. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `clis` | `({ approvalPolicy?: "asks" \| "auto" \| null; capabilities?: { approvals: "bridged" \| "degraded"; liveView: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; steer?: boolean; streaming: boolean; tmux?: boolean; } \| null; defaultMode?: "default" \| "readonly" \| "unrestricted"; enabled?: boolean; id: "claude" \| "codex" \| "agy" \| "cursor"; installed: boolean; models?: string[]; modelsLoaded?: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; path?: string \| null; pendingPath?: string \| null; problems?: string[]; title: string; version?: string \| null; })[]` | no | All four, in a fixed order. |

### `external.close`

*Direction:* client → server

External sessions (1.53.0, WP5): stop the process snowpea runs for it and unsubscribe; the CLI's own transcript stays. A screen that only goes away calls external.unwatch instead.

**Params**

| field | type | required | description |
|---|---|---|---|
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | The external session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `external.create`

*Direction:* client → server

External sessions (1.53.0, WP6): start a new session of a CLI in a registered project root and open it; the calling connection is subscribed to its external.event stream as with external.open.

**Params**

| field | type | required | description |
|---|---|---|---|
| `cli` | `"claude" \| "codex" \| "agy" \| "cursor"` | yes | Which CLI. |
| `mode` | `"default" \| "readonly" \| "unrestricted" \| null` | no | default, readonly or unrestricted; omitted uses the CLI's defaultMode. 'unrestricted' only from a human connection (else jarvis_person_only); Jarvis asks the person through a person-only confirmation instead. |
| `model` | `string \| null` | no | Model id for the CLI; validated, never a flag. |
| `project` | `string` | yes | A registered project root; anything else is refused. |
| `transport` | `"structured" \| "tmux"` | no | structured (default): the daemon runs it over the CLI's own protocol (host 'snowpea'; the only transport Jarvis-managed sessions use). tmux: in a new tmux pane (host 'tmux'), only once spike S5 confirms it and the host has tmux; else external_unavailable. |
| `trust` | `boolean` | no | As ExternalOpenParams.trust (untrusted_project). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `session` | `{ approvals?: "bridged" \| "person" \| "degraded" \| "none"; canSend?: boolean; controlled?: boolean; cwd: string; host?: "none" \| "terminal" \| "tmux" \| "snowpea"; mode?: "default" \| "readonly" \| "unrestricted" \| null; model?: string \| null; preview?: string; project?: string \| null; ref: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }; securityListsApply?: boolean \| null; status: "idle" \| "running" \| "error"; title?: string \| null; tmux?: { pane: string; session: string; } \| null; updatedAt?: string \| null; }` | yes | The new session (already opened). |

### `external.get`

*Direction:* client → server

External sessions (1.53.0): one session's row without opening or reading it (deep links, pop-outs, restored tabs). not_found when the CLI has no such session; outside_root when it is not in a registered project.

**Params**

| field | type | required | description |
|---|---|---|---|
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | The external session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `session` | `{ approvals?: "bridged" \| "person" \| "degraded" \| "none"; canSend?: boolean; controlled?: boolean; cwd: string; host?: "none" \| "terminal" \| "tmux" \| "snowpea"; mode?: "default" \| "readonly" \| "unrestricted" \| null; model?: string \| null; preview?: string; project?: string \| null; ref: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }; securityListsApply?: boolean \| null; status: "idle" \| "running" \| "error"; title?: string \| null; tmux?: { pane: string; session: string; } \| null; updatedAt?: string \| null; }` | yes | The session as it is now. |

### `external.interrupt`

*Direction:* client → server

External sessions (1.53.0, WP6): stop the running turn. host 'tmux': Escape to the pane while Claude is busy (whoever started the turn; never C-c); host 'terminal': external_view_only; nothing running: ok, no-op.

**Params**

| field | type | required | description |
|---|---|---|---|
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | The external session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `external.list`

*Direction:* client → server

External sessions (1.53.0, WP5/6): one working directory's sessions (or every exposed project's) for the requested CLIs, newest first, paged.

**Params**

| field | type | required | description |
|---|---|---|---|
| `allProjects` | `boolean` | no | List across every exposed project (the Jarvis overview); cwd is ignored. |
| `before` | `string \| null` | no | Cursor from nextBefore. |
| `clis` | `("claude" \| "codex" \| "agy" \| "cursor")[] \| null` | no | Only these CLIs; omitted lists all four. |
| `cwd` | `string \| null` | no | Project root whose sessions to list (the CLI's recorded cwd at or under it). Omitted: the workdir of sessionId; one of cwd, sessionId, allProjects is required. |
| `limit` | `number` | no | Most rows, newest first. |
| `sessionId` | `string \| null` | no | snowpea session whose workdir stands in for cwd. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `nextBefore` | `string \| null` | no | Cursor for the next page. |
| `sessions` | `({ approvals?: "bridged" \| "person" \| "degraded" \| "none"; canSend?: boolean; controlled?: boolean; cwd: string; host?: "none" \| "terminal" \| "tmux" \| "snowpea"; mode?: "default" \| "readonly" \| "unrestricted" \| null; model?: string \| null; preview?: string; project?: string \| null; ref: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }; securityListsApply?: boolean \| null; status: "idle" \| "running" \| "error"; title?: string \| null; tmux?: { pane: string; session: string; } \| null; updatedAt?: string \| null; })[]` | no | Newest first. |

### `external.open`

*Direction:* client → server

External sessions (1.53.0, WP5/6): subscribe this connection to a session's external.event stream and return the newest page of its timeline; a host 'none' session is continued under snowpea (host 'snowpea').

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no | Newest timeline entries to return. |
| `mode` | `"default" \| "readonly" \| "unrestricted" \| null` | no | Mode when this continues a host 'none' session; omitted uses the CLI's defaultMode. 'unrestricted' only from a human connection (else jarvis_person_only). |
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | Session to open. |
| `transport` | `"structured" \| "tmux" \| null` | no | 'tmux' on a Claude Code session the person runs in a tmux pane of their own (listed as host 'terminal'): adopt that pane, so snowpea can type into it (host 'tmux'). The call waits for a person-only approval (tool 'tmux.adopt', personOnly, args {cli, sessionId, tmux: {session, pane}, cwd}) the person answers on any of their surfaces (approval.pending); Jarvis, the engine and unattended rules cannot. Declined or unreachable: the session stays view only. 'tmux' on a host 'none' session starts it in a new tmux pane. Omitted or 'structured': as before. |
| `trust` | `boolean` | no | The person's one-time confirmation that external CLIs may run in this project (they load the project's own settings and hooks). Needed only when starting a CLI in a project no person trusted yet: without it external_unavailable with details.reason 'untrusted_project' (show the confirmation, retry with trust: true). Only a person's local connection can trust; anyone else gets details.reason 'untrusted_project_person_only'. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; ts: string; turnId?: string \| null; })[]` | no | Newest page of the timeline, oldest first. |
| `hasMore` | `boolean` | no | Older entries exist (external.read). |
| `readOnly` | `boolean` | no | True while host is 'terminal' (or 'tmux' before spike S5 confirms input): view only, external.send refused. Opening a host 'none' session continues it under snowpea, so it comes back with host 'snowpea'. |
| `session` | `{ approvals?: "bridged" \| "person" \| "degraded" \| "none"; canSend?: boolean; controlled?: boolean; cwd: string; host?: "none" \| "terminal" \| "tmux" \| "snowpea"; mode?: "default" \| "readonly" \| "unrestricted" \| null; model?: string \| null; preview?: string; project?: string \| null; ref: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }; securityListsApply?: boolean \| null; status: "idle" \| "running" \| "error"; title?: string \| null; tmux?: { pane: string; session: string; } \| null; updatedAt?: string \| null; }` | yes | The session as it is now. |

### `external.read`

*Direction:* client → server

External sessions (1.53.0, WP5/6): an older page of a session's timeline.

**Params**

| field | type | required | description |
|---|---|---|---|
| `beforeSeq` | `number \| null` | no | Entries older than this; omitted is the newest. |
| `limit` | `number` | no | Entries per page. |
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | Session to read. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; ts: string; turnId?: string \| null; })[]` | no | Oldest first. |
| `hasMore` | `boolean` | no | Older entries exist. |

### `external.send`

*Direction:* client → server

External sessions (1.53.0, WP6): send a prompt; the reply streams as external.event to the sender, which is subscribed to the ref (idempotent; no external.open needed). A host 'none' session is continued first in its current mode or the CLI's defaultMode ('unrestricted' only for a human connection, else jarvis_person_only). During a running turn: added to that turn when capabilities.steer (same turnId back), else session_busy. A tmux pane that is not ready answers session_busy with error.data.details.reason terminal_prompt, terminal_busy or terminal_typing. Refused with external_view_only while canSend is false (host 'terminal'). A send from a human connection pauses Jarvis in that session (jarvis.quietMinutes).

**Params**

| field | type | required | description |
|---|---|---|---|
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | Session to talk to. |
| `text` | `string` | yes | Prompt text; attachments are not supported in v1. |
| `trust` | `boolean` | no | As ExternalOpenParams.trust (1.54.0): continuing a host 'none' session starts the CLI, so in a project no person trusted yet the send answers external_unavailable with details.reason 'untrusted_project' (show the confirmation, retry with trust: true) or 'untrusted_project_person_only'. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `turnId` | `string` | yes | Turn id its turn.started/turn.done events carry. |

### `external.setMode`

*Direction:* client → server

External sessions (1.53.0, WP6): switch a session to default, readonly or unrestricted; unrestricted only from a human connection.

**Params**

| field | type | required | description |
|---|---|---|---|
| `mode` | `"default" \| "readonly" \| "unrestricted"` | yes | default, readonly or unrestricted. 'unrestricted' only from a human connection (IDE, web, TUI, or the bound approver in a messenger); every such switch is a ledger entry. |
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | The external session (host 'snowpea' or 'tmux'). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `session` | `{ approvals?: "bridged" \| "person" \| "degraded" \| "none"; canSend?: boolean; controlled?: boolean; cwd: string; host?: "none" \| "terminal" \| "tmux" \| "snowpea"; mode?: "default" \| "readonly" \| "unrestricted" \| null; model?: string \| null; preview?: string; project?: string \| null; ref: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }; securityListsApply?: boolean \| null; status: "idle" \| "running" \| "error"; title?: string \| null; tmux?: { pane: string; session: string; } \| null; updatedAt?: string \| null; }` | yes | The new session (already opened). |

### `external.unwatch`

*Direction:* client → server

External sessions (1.53.0): stop sending this connection external.event for the session; the process keeps running (an idle one is stopped after jarvis.external.idleSec). Unknown or never-opened refs: ok.

**Params**

| field | type | required | description |
|---|---|---|---|
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | The external session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `file.complete`

*Direction:* client → server

Complete file and directory paths under a session workdir.

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number \| null` | no | Maximum entries to return (default 30, max 200). |
| `query` | `string` | no | Partial path to match. |
| `sessionId` | `string \| null` | no | Session whose workdir to complete in. |
| `workdir` | `string \| null` | no | Workdir to complete in when no session is set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ kind: "file" \| "dir"; path: string; size?: number \| null; })[]` | no | Matching paths, best first. |
| `truncated` | `boolean` | no | True when more matches exist than returned. |

### `fs.glob`

*Direction:* client → server

Files and directories matching a relative glob ('*', '?', '[..]', and a '**' segment for any depth), capped by maxEntries (1.40.0). Symbolic links are matched, never followed; '**' does not enter .git or node_modules.

**Params**

| field | type | required | description |
|---|---|---|---|
| `maxEntries` | `number` | no | Stop after this many matches. |
| `path` | `string` | no | Directory the pattern starts from; default the workdir. |
| `pattern` | `string` | yes | Relative glob: '*', '?', '[..]' within a name and a segment '**' for any number of directories, as in 'src/**' followed by '/*.ts'. No '..' and not absolute. |
| `sessionId` | `string` | yes | Session whose roots bound the call (see fs.stat). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ isLink: boolean; mtimeMs: number; nlink: number; path: string; size: number; type: "file" \| "dir" \| "other"; })[]` | no | Sorted by path, directories before their children. |
| `path` | `string` | yes | The directory as resolved. |
| `truncated` | `boolean` | no | True when maxEntries cut the listing. |

### `fs.hash`

*Direction:* client → server

SHA-256 of a file, read in a stream (1.40.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `path` | `string` | yes | File or directory; absolute, or relative to the workdir. |
| `sessionId` | `string` | yes | Session whose roots bound the call: its workdir, its workspaceDir and its root conversation's attachments dir. A relative path is relative to the workdir. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `mtimeMs` | `number` | yes | Last change, milliseconds since the epoch. |
| `sha256` | `string` | yes | Hex SHA-256 of the whole file, read in a stream. |
| `size` | `number` | yes | Bytes. |

### `fs.list`

*Direction:* client → server

List a directory, recursively up to depth, capped by maxEntries (1.40.0). Symbolic links are listed, never followed.

**Params**

| field | type | required | description |
|---|---|---|---|
| `depth` | `number` | no | 1 lists the directory itself; more recurses. |
| `maxEntries` | `number` | no | Stop after this many entries. |
| `path` | `string` | yes | File or directory; absolute, or relative to the workdir. |
| `sessionId` | `string` | yes | Session whose roots bound the call: its workdir, its workspaceDir and its root conversation's attachments dir. A relative path is relative to the workdir. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ isLink: boolean; mtimeMs: number; nlink: number; path: string; size: number; type: "file" \| "dir" \| "other"; })[]` | no | Sorted by path, directories before their children. |
| `path` | `string` | yes | The directory as resolved. |
| `truncated` | `boolean` | no | True when maxEntries cut the listing. |

### `fs.read`

*Direction:* client → server

Read a byte range of a file, at most 8 MiB per call (1.40.0). Errors outside_root, protected, not_found, is_dir, too_large.

**Params**

| field | type | required | description |
|---|---|---|---|
| `length` | `number \| null` | no | Bytes to read; omitted reads to the end of the file. |
| `maxBytes` | `number` | no | Refuse (too_large) a read of more than this many bytes; at most 8 MiB, a larger value is capped. |
| `offset` | `number` | no | First byte to read. |
| `path` | `string` | yes | File or directory; absolute, or relative to the workdir. |
| `sessionId` | `string` | yes | Session whose roots bound the call: its workdir, its workspaceDir and its root conversation's attachments dir. A relative path is relative to the workdir. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `dataBase64` | `string` | yes | The bytes read, base64. |
| `eof` | `boolean` | yes | True when the bytes reach the end of the file. |
| `length` | `number` | yes | How many bytes came back. |
| `mtimeMs` | `number` | yes | Last change, milliseconds since the epoch. |
| `offset` | `number` | yes | Where the bytes start. |
| `size` | `number` | yes | Size of the whole file. |

### `fs.stat`

*Direction:* client → server

File metadata inside the session's roots (1.40.0): type, size, mtimeMs, nlink, isLink. Errors outside_root, not_found.

**Params**

| field | type | required | description |
|---|---|---|---|
| `path` | `string` | yes | File or directory; absolute, or relative to the workdir. |
| `sessionId` | `string` | yes | Session whose roots bound the call: its workdir, its workspaceDir and its root conversation's attachments dir. A relative path is relative to the workdir. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `isLink` | `boolean` | yes | True when the last component is a symbolic link. |
| `mtimeMs` | `number` | yes | Last change, milliseconds since the epoch. |
| `nlink` | `number` | yes | Hard link count. |
| `path` | `string` | yes | The path as resolved (symlinks in the directories followed). |
| `size` | `number` | yes | Bytes (the link itself for a symbolic link). |
| `type` | `"file" \| "dir" \| "other"` | yes | 'file', 'dir' or 'other'. A symbolic link reports the type of its target when that target lies inside a root, else 'other'. |

### `fs.write`

*Direction:* client → server

Write a whole file through a temp file and an atomic rename, at most 8 MiB (1.40.0). 'create' never replaces; parents are created inside the roots. Errors outside_root, protected, exists, is_dir, too_large.

**Params**

| field | type | required | description |
|---|---|---|---|
| `dataBase64` | `string` | yes | The whole new content, base64; at most 8 MiB decoded. |
| `mode` | `"create" \| "overwrite"` | yes | 'create' fails with 'exists' when the path is there; 'overwrite' replaces a regular file (a symbolic link or a file with other hard links is refused). |
| `path` | `string` | yes | File or directory; absolute, or relative to the workdir. |
| `sessionId` | `string` | yes | Session whose roots bound the call: its workdir, its workspaceDir and its root conversation's attachments dir. A relative path is relative to the workdir. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `created` | `boolean` | yes | True when the file did not exist before. |
| `mtimeMs` | `number` | yes | Last change, milliseconds since the epoch. |
| `path` | `string` | yes | The path written, as resolved. |
| `size` | `number` | yes | Bytes written. |

### `gateway.bind`

*Direction:* client → server

Attach the daemon to a chat platform channel.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `channelId` | `string \| null` | no | Restrict the binding to one chat, channel or room. |
| `credentialsRef` | `string` | yes | Name of the credential to use: a key in credentials.json or an env var. |
| `platform` | `string` | yes | Chat platform key: telegram, discord or slack. |
| `target` | `Record<string, unknown>` | yes | What the conversation talks to: {'agent': name}, {'session': id} or {'new_session': {'workdir': path, 'mode': mode}}. |
| `userId` | `string \| null` | no | Platform user allowed to answer approvals from chat. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Id used to unbind later. |

### `gateway.list`

*Direction:* client → server

List live gateway bindings.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `bindings` | `({ bindingId: string; channelId?: string \| null; credentialsRef?: string; platform: string; source?: string; state?: "active" \| "inactive"; target: string; userId?: string \| null; })[]` | no | Live gateway bindings. |

### `gateway.sync`

*Direction:* client → server

Reconcile the messenger bindings with settings.gateway.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `added` | `string[]` | no | Platforms that started listening. |
| `kept` | `string[]` | no | Platforms that were already listening. |
| `removed` | `string[]` | no | Platforms whose auto binding was dropped. |

### `gateway.unbind`

*Direction:* client → server

Detach a gateway binding.

**Params**

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Binding to remove. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `goal.clear`

*Direction:* client → server

Remove the goal (1.43.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `extraRounds` | `number` | no | goal.resume only: rounds added to the cap. |
| `sessionId` | `string` | yes | Session whose goal is paused, resumed or cleared. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null when none. |

### `goal.get`

*Direction:* client → server

Read the session's goal (1.43.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session whose goal is read. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null when none. |

### `goal.pause`

*Direction:* client → server

Pause and disarm the goal (1.43.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `extraRounds` | `number` | no | goal.resume only: rounds added to the cap. |
| `sessionId` | `string` | yes | Session whose goal is paused, resumed or cleared. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null when none. |

### `goal.resume`

*Direction:* client → server

Re-arm a paused or blocked goal, optionally with more rounds, and start a round when idle (1.43.0); goal_not_allowed on a delegated child or a scheduled run (1.50.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `extraRounds` | `number` | no | goal.resume only: rounds added to the cap. |
| `sessionId` | `string` | yes | Session whose goal is paused, resumed or cleared. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null when none. |

### `goal.set`

*Direction:* client → server

Give the session a goal and arm it; rounds run until goal_update completes or blocks it, or the round cap (1.43.0, capability 'goals'). A delegated child or a scheduled run is refused with goal_not_allowed (1.50.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `maxRounds` | `number \| null` | no | Round cap (default 20, at most 256). |
| `objective` | `string` | yes | What has to be true when the goal is complete. |
| `sessionId` | `string` | yes | Session the goal belongs to; replaces any goal it had. |
| `start` | `boolean` | no | Start the first round now when the session is idle. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null when none. |

### `host.turnContext`

*Direction:* server → client

At turn start, ask the session's host-tools connection for a note for the turn's first model call (1.31.0). Only on a browser connection that sent tool.register {capabilities: ['turnContext']}; 300 ms deadline, fail-open.

**Params**

| field | type | required | description |
|---|---|---|---|
| `prompt` | `string` | no | The turn's user text, at most 4000 characters. Use it to pick sites; keep none of it. |
| `reason` | `"turn" \| "compaction"` | no | Why core asks (1.37.0). 'turn': a turn is starting. 'compaction': the conversation was just summarised, and the summary drops the earlier notes; answer again with what the work needs now. Core sends 'compaction' only to a connection that declared the capability 'turnContextCompaction', with the user's latest words in 'prompt' and a 1 s deadline. |
| `rootSessionId` | `string` | yes | Its root conversation (the parent chain's top; the session itself for a root). |
| `sessionId` | `string` | yes | Session whose turn is starting. |
| `siteMemory` | `boolean` | no | Whether site memory is on for the root conversation (1.35.0). When false, leave out the part of the note that comes from site memory; a note about the host's own settings still goes. |
| `turnId` | `string` | yes | The turn that is starting. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `coversSkills` | `string[] \| null` | no | Names of skills this note already carries (1.37.0). Core does not auto-inject them on the turn this answer belongs to (a 'compaction' answer: the next turn), and does not count them as injected, so a later turn without this cover can still bring them in. At most 16 names, not counted in the 4000-character limit. |
| `text` | `string` | no | Site memory for the first call, or '' for nothing. Core keeps at most 4000 characters (cut at a line boundary), drops control characters but newlines, and adds it to the turn's user message as an untrusted <host-context> block. After a compaction (reason 'compaction') it is kept with the summary instead and the summariser never reads it. |

### `jarvis.briefing.get`

*Direction:* client → server

Jarvis mode (1.53.0, WP8): the periodic briefing's settings and next run.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `enabled` | `boolean` | no | jarvis.briefing.enabled. |
| `instructions` | `string` | no | What to cover, as the person said it (adjusted in conversation). |
| `jobId` | `string \| null` | no | Scheduler job that runs it, while enabled. |
| `nextRunAt` | `string \| null` | no | UTC ISO time of the next run. |
| `schedule` | `string` | no | Cron expression, daemon-local time. |
| `sections` | `("sessions" \| "waiting" \| "todos" \| "mail" \| "calendar")[]` | no | What it covers; mail and calendar only count while a mail or calendar preset is connected. |
| `timezone` | `string \| null` | no | IANA zone the schedule is read in; null = the daemon's. |

### `jarvis.briefing.set`

*Direction:* client → server

Jarvis mode (1.53.0, WP8): turn the periodic briefing on or off, or change it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `enabled` | `boolean \| null` | no | Omitted keeps the current value. |
| `instructions` | `string \| null` | no | Omitted keeps it. |
| `schedule` | `string \| null` | no | Cron expression; omitted keeps it. |
| `sections` | `("sessions" \| "waiting" \| "todos" \| "mail" \| "calendar")[] \| null` | no | Omitted keeps them. |
| `timezone` | `string \| null` | no | IANA zone; '' clears it (daemon-local); omitted keeps it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `enabled` | `boolean` | no | jarvis.briefing.enabled. |
| `instructions` | `string` | no | What to cover, as the person said it (adjusted in conversation). |
| `jobId` | `string \| null` | no | Scheduler job that runs it, while enabled. |
| `nextRunAt` | `string \| null` | no | UTC ISO time of the next run. |
| `schedule` | `string` | no | Cron expression, daemon-local time. |
| `sections` | `("sessions" \| "waiting" \| "todos" \| "mail" \| "calendar")[]` | no | What it covers; mail and calendar only count while a mail or calendar preset is connected. |
| `timezone` | `string \| null` | no | IANA zone the schedule is read in; null = the daemon's. |

### `jarvis.delegation.list`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): delegation grants in force.

**Params**

| field | type | required | description |
|---|---|---|---|
| `includeExpired` | `boolean` | no | Also list expired and revoked grants. |
| `scope` | `"global" \| "project" \| "session" \| null` | no | Only this scope. |
| `target` | `string \| null` | no | Only grants for this project root or session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `default` | `"staged" \| "full"` | no | jarvis.delegation.default. |
| `grants` | `({ allow?: ("deploy" \| "outside_destructive" \| "secret_send")[]; expiresAt?: string \| null; grantedAt: string; grantedBy: string; id: string; level: "staged" \| "full"; note?: string; proposedBy?: "person" \| "jarvis"; scope: "global" \| "project" \| "session"; target?: string \| null; })[]` | no | Newest first. |

### `jarvis.delegation.revoke`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): revoke a grant at once.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Grant to revoke. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `revoked` | `boolean` | yes | False when the grant was already gone or expired. |

### `jarvis.delegation.set`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): create or replace a grant. The calling connection is the person, so it applies at once; Jarvis's own proposals never use this method but raise a person-only approval. A guarded class needs a local connection or a re-authenticated remote one; from a messenger only when jarvis.messenger.allowGrants.

**Params**

| field | type | required | description |
|---|---|---|---|
| `allow` | `("deploy" \| "outside_destructive" \| "secret_send")[]` | no | Guarded classes to hand over; refused (invalid_params) on a global grant. |
| `expiresAt` | `string \| null` | no | UTC ISO expiry. Omitted: a session grant lasts while the session does, a project grant 24 h. |
| `level` | `"staged" \| "full"` | yes | staged or full. |
| `note` | `string` | no | Shown in the grants list and the ledger. |
| `scope` | `"global" \| "project" \| "session"` | yes | global, project or session. |
| `target` | `string \| null` | no | Required for project (a root) and session (an id); omitted for global. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `grant` | `{ allow?: ("deploy" \| "outside_destructive" \| "secret_send")[]; expiresAt?: string \| null; grantedAt: string; grantedBy: string; id: string; level: "staged" \| "full"; note?: string; proposedBy?: "person" \| "jarvis"; scope: "global" \| "project" \| "session"; target?: string \| null; }` | yes | The grant now in force. |
| `replaced` | `string \| null` | no | Id of the grant it replaced, if any. |

### `jarvis.disable`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): clear jarvis.enabled and park the Jarvis session; everything routes to the person again, messenger bindings get their targets back.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `budget` | `{ dailyTokens: number; exhausted?: boolean; tokensToday?: number; wakesPerHour: number; wakesThisHour?: number; } \| null` | no | Allowance; null while not running. |
| `delegationDefault` | `"staged" \| "full"` | no | jarvis.delegation.default: the level with no grant. |
| `enabled` | `boolean` | yes | settings jarvis.enabled. |
| `folder` | `string` | yes | Jarvis's working folder (jarvis.folder, expanded); its only write root. |
| `hiddenProjects` | `string[]` | no | Project roots with jarvis.expose off. |
| `model` | `string \| null` | no | Model profile Jarvis runs on (jarvis.model); null follows models.default. |
| `paused` | `({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; project?: string \| null; reason: "typing" \| "takeover"; sessionId?: string \| null; until?: string \| null; })[]` | no | Sessions where the person has taken back authority. |
| `problems` | `string[]` | no | Why it is not running as configured, e.g. an unknown model profile. |
| `running` | `boolean` | no | True when the Jarvis service is up and watching sessions (enabled and started). |
| `sessionId` | `string \| null` | no | The Jarvis session (SessionSummary.role 'jarvis'); null before it was first created. |

### `jarvis.enable`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): set jarvis.enabled, create or resume the Jarvis session and start supervising every exposed project. Idempotent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `folder` | `string \| null` | no | Also set jarvis.folder before starting; created if missing. |
| `model` | `string \| null` | no | Also set jarvis.model (a models.profiles id) before starting. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `budget` | `{ dailyTokens: number; exhausted?: boolean; tokensToday?: number; wakesPerHour: number; wakesThisHour?: number; } \| null` | no | Allowance; null while not running. |
| `delegationDefault` | `"staged" \| "full"` | no | jarvis.delegation.default: the level with no grant. |
| `enabled` | `boolean` | yes | settings jarvis.enabled. |
| `folder` | `string` | yes | Jarvis's working folder (jarvis.folder, expanded); its only write root. |
| `hiddenProjects` | `string[]` | no | Project roots with jarvis.expose off. |
| `model` | `string \| null` | no | Model profile Jarvis runs on (jarvis.model); null follows models.default. |
| `paused` | `({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; project?: string \| null; reason: "typing" \| "takeover"; sessionId?: string \| null; until?: string \| null; })[]` | no | Sessions where the person has taken back authority. |
| `problems` | `string[]` | no | Why it is not running as configured, e.g. an unknown model profile. |
| `running` | `boolean` | no | True when the Jarvis service is up and watching sessions (enabled and started). |
| `sessionId` | `string \| null` | no | The Jarvis session (SessionSummary.role 'jarvis'); null before it was first created. |

### `jarvis.escalations.dismiss`

*Direction:* client → server

Jarvis mode (1.53.0): dismiss escalations by id (or all); every client is told with jarvis.escalation.dismissed.

**Params**

| field | type | required | description |
|---|---|---|---|
| `all` | `boolean` | no | Dismiss every open escalation. |
| `ids` | `string[]` | no | Escalation ids to dismiss. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `dismissed` | `string[]` | no | Ids that were open and are now dismissed. |

### `jarvis.escalations.list`

*Direction:* client → server

Jarvis mode (1.53.0): the open escalations that are not a waiting approval or question (finished, error, stalled, budget, jarvis), newest first, paged. They persist until dismissed, across client reloads and daemon restarts.

**Params**

| field | type | required | description |
|---|---|---|---|
| `before` | `string \| null` | no | Cursor from a previous page's nextBefore; omitted starts at the newest. |
| `limit` | `number` | no | Escalations per page. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `escalations` | `({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; guardedClass?: "deploy" \| "outside_destructive" \| "secret_send" \| null; id: string; importance?: "low" \| "normal" \| "high" \| "urgent"; project?: string \| null; reason: "guarded" \| "risky" \| "never" \| "question" \| "finished" \| "error" \| "stalled" \| "budget" \| "jarvis"; requestId?: string \| null; requestKind?: "approval" \| "question" \| null; sessionId?: string \| null; summary: string; title?: string \| null; ts: string; })[]` | no | Open escalations that are not a waiting approval or question (finished, error, stalled, budget, jarvis), as they were sent; kept in $SNOWPEA_HOME/jarvis/ so they survive a client reload and a daemon restart until dismissed. |
| `nextBefore` | `string \| null` | no | Cursor for the next page; null at the end. |

### `jarvis.handback`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): give one session back to Jarvis now, ending a takeover or a typing pause. Human connections only (else jarvis_person_only).

**Params**

| field | type | required | description |
|---|---|---|---|
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | External session. |
| `sessionId` | `string \| null` | no | snowpea session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `state` | `{ exposed: boolean; grantId?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; pausedUntil?: string \| null; takenOver?: boolean; }` | yes | The session's Jarvis state after the call. |

### `jarvis.ledger`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): decisions made for supervised sessions, newest first, by Jarvis, the person or the engine alone, with classification, rule and grant.

**Params**

| field | type | required | description |
|---|---|---|---|
| `before` | `string \| null` | no | Cursor from a previous page's nextBefore; omitted starts at the newest. |
| `decidedBy` | `"person" \| "jarvis" \| "auto" \| null` | no | Only decisions by this decider. |
| `limit` | `number` | no | Entries per page. |
| `sessionId` | `string \| null` | no | Only decisions for this session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ classification?: "safe" \| "risky" \| "guarded" \| "never" \| null; decidedBy: "jarvis" \| "person" \| "auto"; decision?: string \| null; external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; grantId?: string \| null; guardedClass?: "deploy" \| "outside_destructive" \| "secret_send" \| null; id: string; importance?: "low" \| "normal" \| "high" \| "urgent"; kind: "approval" \| "question" \| "continue" \| "prompt" \| "interrupt" \| "escalate" \| "unrestricted"; project?: string \| null; readOnly?: boolean \| null; reason?: string; requestId?: string \| null; rule?: string \| null; sessionId?: string \| null; summary: string; tool?: string \| null; ts: string; })[]` | no | Newest first. |
| `nextBefore` | `string \| null` | no | Cursor for the next page; null at the end. |

### `jarvis.overview`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): one row per supervised session, snowpea and external.

**Params**

| field | type | required | description |
|---|---|---|---|
| `includeExternal` | `boolean` | no | Include external CLI sessions. |
| `includeIdle` | `boolean` | no | False leaves out idle rows with nothing pending. |
| `limit` | `number` | no | Most rows, newest activity first. |
| `project` | `string \| null` | no | Only this project root. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `generatedAt` | `string` | yes | UTC ISO time the table was taken. |
| `rows` | `({ approvals?: "bridged" \| "person" \| "degraded" \| "none" \| null; controlled?: boolean \| null; digest?: string; exposed?: boolean; external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; grantId?: string \| null; host?: "none" \| "terminal" \| "tmux" \| "snowpea" \| null; hostToolsFrom?: string \| null; lastActivityAt?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; mode?: "default" \| "readonly" \| "unrestricted" \| null; originSurface?: string \| null; pausedUntil?: string \| null; pendingApprovals?: number; pendingQuestions?: number; project?: string \| null; securityListsApply?: boolean \| null; sessionId?: string \| null; status: "idle" \| "running" \| "awaiting_approval" \| "awaiting_question" \| "error"; takenOver?: boolean; title?: string \| null; waitingOnPerson?: number; })[]` | no | Newest activity first. |

### `jarvis.project.alias`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): add or remove a project's aliases.

**Params**

| field | type | required | description |
|---|---|---|---|
| `add` | `string[]` | no | Aliases to add. |
| `remove` | `string[]` | no | Aliases to remove. |
| `root` | `string` | yes | Project root. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `project` | `{ aliases?: string[]; exposed?: boolean; name: string; named?: boolean; root: string; }` | yes | The project after the change. |

### `jarvis.project.list`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): projects with the name, aliases and exposure Jarvis and clients use in conversation.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `projects` | `({ aliases?: string[]; exposed?: boolean; name: string; named?: boolean; root: string; })[]` | no | Every project snowpea knows, by name. |

### `jarvis.project.rename`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): set or clear a project's display name.

**Params**

| field | type | required | description |
|---|---|---|---|
| `name` | `string \| null` | no | New display name; null or empty returns to the default. |
| `root` | `string` | yes | Project root. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `project` | `{ aliases?: string[]; exposed?: boolean; name: string; named?: boolean; root: string; }` | yes | The project after the change. |

### `jarvis.status`

*Direction:* client → server

Jarvis mode (1.53.0, WP3): enabled, the Jarvis session, model, folder, budget, pauses.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `budget` | `{ dailyTokens: number; exhausted?: boolean; tokensToday?: number; wakesPerHour: number; wakesThisHour?: number; } \| null` | no | Allowance; null while not running. |
| `delegationDefault` | `"staged" \| "full"` | no | jarvis.delegation.default: the level with no grant. |
| `enabled` | `boolean` | yes | settings jarvis.enabled. |
| `folder` | `string` | yes | Jarvis's working folder (jarvis.folder, expanded); its only write root. |
| `hiddenProjects` | `string[]` | no | Project roots with jarvis.expose off. |
| `model` | `string \| null` | no | Model profile Jarvis runs on (jarvis.model); null follows models.default. |
| `paused` | `({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; project?: string \| null; reason: "typing" \| "takeover"; sessionId?: string \| null; until?: string \| null; })[]` | no | Sessions where the person has taken back authority. |
| `problems` | `string[]` | no | Why it is not running as configured, e.g. an unknown model profile. |
| `running` | `boolean` | no | True when the Jarvis service is up and watching sessions (enabled and started). |
| `sessionId` | `string \| null` | no | The Jarvis session (SessionSummary.role 'jarvis'); null before it was first created. |

### `jarvis.takeover`

*Direction:* client → server

Jarvis mode (1.53.0, WP2): the person takes one session back; Jarvis stops deciding for it until jarvis.handback or the given minutes.

**Params**

| field | type | required | description |
|---|---|---|---|
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | External session. |
| `minutes` | `number \| null` | no | Hand back automatically after this long; omitted holds until jarvis.handback. |
| `sessionId` | `string \| null` | no | snowpea session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `state` | `{ exposed: boolean; grantId?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; pausedUntil?: string \| null; takenOver?: boolean; }` | yes | The session's Jarvis state after the call. |

### `job.cancel`

*Direction:* client → server

Cancel a scheduled job.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `jobId` | `string` | yes | Target job. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `job.delete`

*Direction:* client → server

Delete a job, its schedule and run history; optionally its run sessions.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `deleteRuns` | `boolean` | no | Also delete the sessions its runs opened, with their workspaces, as session.deleteSaved does. A run still in progress is left alone. |
| `jobId` | `string` | yes | Job to delete, with its schedule and run history. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `deletedSessions` | `number` | no | Run sessions deleted with it. |
| `jobId` | `string` | yes | The deleted job. |

### `job.list`

*Direction:* client → server

List scheduled jobs and their next run times.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobs` | `({ catchUpWindowMinutes?: number; channel?: string \| null; createdVia?: "rpc" \| "tool"; enabled?: boolean; fingerprint?: string \| null; held?: { scheduledAt: string; status: "waiting_for_host" \| "missed"; } \| null; jobId: string; kind?: "cron" \| "once" \| "interval"; lastRunAt?: string \| null; lastStatus?: "ok" \| "error" \| "denied_by_timeout" \| null; meta?: Record<string, unknown> \| null; mode?: "plan" \| "accept" \| "auto"; nextRunAt?: string \| null; originSessionId?: string \| null; owned?: boolean; ownerClientId?: string \| null; spec: string; state?: "scheduled" \| "running" \| "cancelled"; target?: string \| null; task: string; timezone?: string \| null; title?: string \| null; })[]` | no | Known jobs. |

### `job.nextRunForClient`

*Direction:* client → server

When a host should be awake for its routines (addendum 11).

**Params**

| field | type | required | description |
|---|---|---|---|
| `clientId` | `string` | yes | A host's clientId (a browser profile). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `nextRunAt` | `string \| null` | no | Earliest run that needs this client's host: a held (waiting_for_host) occurrence or the next firing of a job whose hostToolsFrom is this clientId; null when none. |

### `job.preview`

*Direction:* client → server

Read a schedule spec without saving anything (1.10.0): its kind and when a job with it would first fire. An unreadable spec is invalid_params with job.schedule's message.

**Params**

| field | type | required | description |
|---|---|---|---|
| `spec` | `string` | yes | A schedule spec, read exactly as job.schedule reads it (1.10.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `kind` | `"cron" \| "once" \| "interval"` | yes | What the spec turned into. |
| `nextRunAt` | `string \| null` | no | UTC ISO-8601 time a job with this spec would first fire if saved now. A spec that would never fire is invalid_params, as for job.schedule. |
| `normalized` | `string \| null` | no | The parsed schedule: the cron expression for 'cron', 'every <n>s' for 'interval', 'once at <UTC ISO>' for 'once'. |

### `job.runNow`

*Direction:* client → server

Fire a scheduled job immediately.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `jobId` | `string` | yes | Target job. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `job.runs`

*Direction:* client → server

A job's run history, newest first (1.33.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Job whose runs to list; an unknown one is invalid_params, as in job.update. |
| `limit` | `number` | no | Newest runs to return. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `runs` | `({ endedAt?: string \| null; error?: string \| null; runId: string; scheduledAt: string; sessionId?: string \| null; startedAt: string; status: string; trigger?: "schedule" \| "runNow" \| null; })[]` | no | Newest first. |

### `job.schedule`

*Direction:* client → server

Schedule a prompt to run unattended.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `agent` | `string \| null` | no | Named agent that runs the task. |
| `channel` | `string \| null` | no | Gateway channel that receives the output. |
| `meta` | `Record<string, unknown> \| null` | no | Opaque data for the host, at most 4 KB of JSON (1.23.0). Only a connection whose system.hello said clientKind 'browser' with a clientId may set it; the job is then owned by that clientId: only it may update, cancel, delete or run it now, see its meta, and no agent tool can change or cancel it. |
| `mode` | `"plan" \| "accept" \| "auto"` | no | Permission mode for the unattended run. |
| `sessionTemplate` | `{ agent?: string \| null; catchUpWindowMinutes?: number; hostToolsFrom?: string \| null; hostWaitSec?: number; mode?: "plan" \| "accept" \| "auto" \| null; } \| null` | no | Session setup for each run, incl. host tools (1.7.0). |
| `spec` | `string` | yes | Cron expression or natural-language schedule. |
| `task` | `string` | yes | Prompt run on each firing. |
| `title` | `string \| null` | no | A short name for the job, shown instead of its task (1.33.0). |
| `workdir` | `string \| null` | no | Working directory for the run; defaults to the daemon's home. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `fingerprint` | `string \| null` | no | For a job with meta, the fingerprint its runs' tool.invoke job will carry (1.23.0). Seal this value; do not recompute it. |
| `jobId` | `string` | yes | Id of the scheduled job. |
| `nextRunAt` | `string \| null` | no | UTC ISO-8601 time of the first firing. |

### `job.update`

*Direction:* client → server

Change an existing job (addendum 12).

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `enabled` | `boolean \| null` | no | False pauses the job (no next firing); true resumes it from now on its schedule. |
| `jobId` | `string` | yes | Job to change. |
| `schedule` | `string \| null` | no | A new schedule spec, parsed like job.schedule's spec; the next run is recomputed. |
| `sessionTemplate` | `{ catchUpWindowMinutes?: number \| null; hostWaitSec?: number \| null; } \| null` | no | Template fields to change. |
| `task` | `string \| null` | no | A new prompt for every later run (1.33.0); it must not be blank. A job with meta gets a new fingerprint. |
| `title` | `string \| null` | no | A new short name (1.33.0); '' removes it, null or absent keeps it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `catchUpWindowMinutes` | `number` | no | How long a held run waits for its host before it is missed. |
| `channel` | `string \| null` | no | Channel that receives the output. |
| `createdVia` | `"rpc" \| "tool"` | no | 'rpc' for job.schedule, 'tool' for schedule_create or /schedule (1.23.0). |
| `enabled` | `boolean` | no | False once the job is cancelled or has run out. |
| `fingerprint` | `string \| null` | no | The fingerprint tool.invoke job carries; only the owner sees it. |
| `held` | `{ scheduledAt: string; status: "waiting_for_host" \| "missed"; } \| null` | no | The newest occurrence when it is waiting for its host or was missed (addendum 12); null otherwise. |
| `jobId` | `string` | yes | Job id. |
| `kind` | `"cron" \| "once" \| "interval"` | no | How the spec repeats: a cron rule, a one-shot, or a fixed interval. |
| `lastRunAt` | `string \| null` | no | UTC ISO-8601 time of the last firing, null before the first. |
| `lastStatus` | `"ok" \| "error" \| "denied_by_timeout" \| null` | no | How the last run ended. |
| `meta` | `Record<string, unknown> \| null` | no | The job's meta; only the owner connection sees it. |
| `mode` | `"plan" \| "accept" \| "auto"` | no | Permission mode for the run. |
| `nextRunAt` | `string \| null` | no | UTC ISO-8601 time of the next firing. |
| `originSessionId` | `string \| null` | no | TUI/chat session that also receives every result. |
| `owned` | `boolean` | no | True for a job with meta: only its owner may change, run or delete it. |
| `ownerClientId` | `string \| null` | no | The owning browser clientId; only the owner sees it. |
| `spec` | `string` | yes | Schedule as given. |
| `state` | `"scheduled" \| "running" \| "cancelled"` | no | Current job state. |
| `target` | `string \| null` | no | What runs it when not an ordinary scheduled session: 'jarvis-task' is a Jarvis mode assistant task (1.53.0, WP8): its own worker session, result handed to Jarvis. |
| `task` | `string` | yes | Prompt run on each firing. |
| `timezone` | `string \| null` | no | IANA zone its cron clock times are read in; null is the daemon's (1.53.0). |
| `title` | `string \| null` | no | The job's short name, when it has one (1.33.0). |

### `llm.sample`

*Direction:* client → server

One tool-less model call for a session's browser host (1.14.0): no tools, no history, nothing written to the transcript except a usage event. The session's host only ('unauthorized' otherwise); 'unavailable' when no model can answer; with schema, 'invalid_output' when the answer does not validate.

**Params**

| field | type | required | description |
|---|---|---|---|
| `images` | `({ dataBase64: string; mime: "image/png" \| "image/jpeg" \| "image/webp" \| "image/gif"; })[] \| null` | no | 1.55.0: up to 4 images sent with the input (each at most 6 MB decoded, 12 MB together). A model known not to take images refuses the call with 'unavailable', details.reason 'model_cannot_see'; use modelCategory 'vision'. An image that does not decode is 'invalid_params'. |
| `input` | `string` | yes | Page-derived text; sent as the user message, fenced and marked untrusted. |
| `maxTokens` | `number` | no | Output token cap, at most 4096. |
| `modelCategory` | `"fast" \| "default" \| "standard" \| "deep" \| "vision"` | no | 'fast' = browser.fastModel (then models.categories.fast, then the session's model); 'default' = the session's model; 'standard' (1.33.0) = models.categories.standard, then the session's model; 'deep' (1.33.0) = models.categories.deep, then .standard, then the session's model; 'vision' (1.55.0) = models.categories.vision only: with none configured the call is refused with 'unavailable', details.reason 'no_vision_model'. |
| `schema` | `Record<string, unknown> \| null` | no | JSON Schema the answer must match. With it the model is told to answer JSON only, and an answer that does not validate is 'invalid_output'. |
| `schemaAfterInput` | `boolean` | no | Put the schema instruction after the fenced input (with task) instead of in the system message, for a schema that varies per call (1.21.0). |
| `sessionId` | `string \| null` | no | Session whose provider settings and bill this call uses; its host only. Omitted (1.40.0, capability 'llmSampleHost'): a host-level call for a connection that has registered host tools -- the default model of this daemon (models.default), with 'fast', 'standard' and 'deep' choosing browser.fastModel / models.categories as for a session. No session is billed and no usage event is written. |
| `system` | `string \| null` | no | Instructions the host wrote. |
| `task` | `string \| null` | no | Trusted per-call instruction from the host, placed after the fenced input, so system + page stay a prefix several samples of one page share (1.21.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `json` | `unknown` | no | The validated JSON value, when a schema was given. |
| `model` | `string` | yes | 'vendor:model' that answered. |
| `text` | `string \| null` | no | The answer, when no schema was given. |
| `usage` | `{ inputTokens?: number; outputTokens?: number; }` | yes | Tokens the call used. |

### `lsp.catalog`

*Direction:* client → server

List every registered language server, regardless of whether it has started.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `servers` | `({ available?: boolean; disabled: boolean; extensions?: string[]; id: string; installHint?: string \| null; installable: boolean; languageIds?: string[]; managed?: boolean; needs?: string \| null; path?: string \| null; })[]` | no | One row per registered server id. |

### `lsp.install`

*Direction:* client → server

Install language servers the user asked for, even with lsp.autoInstall off.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `ids` | `string[] \| null` | no | Server ids to install. Null: every installable server that is enabled and not available. Runs even with lsp.autoInstall false: the request is the consent. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `results` | `({ error?: string \| null; id: string; installed?: boolean; ok: boolean; path?: string \| null; })[]` | no | One per id. |

### `lsp.restart`

*Direction:* client → server

Stop a language server (or all) so it starts afresh; missing ones are re-probed.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string \| null` | no | Server id; null restarts every server and re-probes. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `restarted` | `string[]` | no | Ids stopped or forgotten; each starts afresh on its next edit. |

### `lsp.status`

*Direction:* client → server

Report every language server the daemon has started and its state.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `servers` | `({ id: string; languageId?: string; pid?: number \| null; reason?: string \| null; root: string; state: "starting" \| "ready" \| "broken" \| "stopped"; })[]` | no | One row per (server, root) pair. |

### `mcp.add`

*Direction:* client → server

Write an MCP server into the project or global .mcp.json and start it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `acceptFindings` | `boolean` | no | Add the entry despite the security findings mcp_unsafe reported (1.33.0). Does not let it overwrite a name already declared in the file. |
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `args` | `string[] \| null` | no | Arguments, argv style; never a shell string. |
| `command` | `string \| null` | no | Executable for a stdio server. |
| `cwd` | `string \| null` | no | Working directory for a stdio server. |
| `disabled` | `boolean \| null` | no | Keep the entry but never start the server. |
| `env` | `Record<string, string> \| null` | no | Environment for the child process. mcp.update, and mcp.test on a saved server, merge it key by key into the saved env (1.33.0); envRemove deletes keys. |
| `force` | `boolean` | no | acceptFindings and replace together, as before 1.33.0. |
| `headers` | `Record<string, string> \| null` | no | HTTP headers sent with every request. mcp.update, and mcp.test on a saved server, merge them key by key into the saved headers (1.33.0); headersRemove deletes keys. |
| `name` | `string` | yes | Server name; ^[a-zA-Z0-9_-]{1,64}$. |
| `oauth` | `{ clientId?: string \| null; clientSecret?: string \| null; scope?: string \| null; } \| null` | no | OAuth client settings for an http/sse server (1.34.0); mcp.update merges them key by key. |
| `permission` | `"read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside" \| null` | no | Permission tag for the server's tools; stored in settings. |
| `preset` | `string \| null` | no | Catalog id copied before the explicit fields are applied. |
| `replace` | `boolean` | no | Overwrite an entry already declared under this name in the target file instead of answering mcp_exists (1.33.0). Does not accept security findings. |
| `scope` | `"project" \| "global"` | no | Which file to write: $SNOWPEA_HOME/.mcp.json (global, the default since 1.23.0, so every project sees it) or the project's .mcp.json. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `test` | `boolean` | no | Probe the server before saving; nothing is written if it fails. |
| `timeoutSec` | `number \| null` | no | Startup cap, in seconds. |
| `toolTimeoutSec` | `number \| null` | no | Per-call cap, in seconds. |
| `toolsExclude` | `string[] \| null` | no | Never register these tools of the server. [] clears the filter; null or absent leaves it as saved. |
| `toolsInclude` | `string[] \| null` | no | Register only these tools of the server. [] clears the filter; null or absent leaves it as saved. |
| `type` | `"stdio" \| "http" \| "sse" \| null` | no | Force a transport instead of inferring it. |
| `url` | `string \| null` | no | Endpoint for an http or sse server. |
| `workdir` | `string \| null` | no | Project directory for scope=project. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `auth` | `{ clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; }` | no | OAuth standing (1.34.0). A probe that met 401 still writes the entry, with state needs_auth, so the client can go on to mcp.authorize. |
| `error` | `string \| null` | no | Why the server did not start. |
| `ok` | `boolean` | no | True when the entry was written. |
| `path` | `string` | yes | File the entry was written to. |
| `state` | `"stopped" \| "starting" \| "ready" \| "error"` | no | State of the server after the write. |
| `tools` | `({ description?: string; name: string; })[]` | no | Tools the probe found, so a client can offer a picker. |
| `warnings` | `string[]` | no | Security findings that --force accepted. |

### `mcp.authorize`

*Direction:* client → server

Start an OAuth sign-in for a remote MCP server; returns the consent URL (1.34.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `name` | `string` | yes | Server name. |
| `oauthScope` | `string \| null` | no | OAuth scopes to ask for; default: the entry's oauth.scope, then the server's WWW-Authenticate scope, then its advertised scopes_supported. |
| `redirectUri` | `string \| null` | no | Redirect URI the host catches itself (its own tab) and hands back with mcp.authorize.complete: http on 127.0.0.1, localhost or [::1] (any port), https, or a private-use scheme; never javascript:, data:, file: or a fragment (invalid_params). Omitted, the daemon listens on http://127.0.0.1:<random port>/callback and completes on its own. |
| `scope` | `"project" \| "global" \| null` | no | Which declaration signs in: the global or the project entry of this name. Omitted, the one in effect; but when a project or plugin entry shadows a global one with another URL, that is invalid_params and scope is required, so a cloned project cannot collect the sign-in meant for the global server. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `workdir` | `string \| null` | no | Project directory that declares it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `expiresAt` | `string` | yes | When the flow stops waiting, ISO-8601 UTC. |
| `redirectUri` | `string` | yes | Redirect URI the consent URL carries: the given one or the loopback listener. |
| `resource` | `string` | yes | Canonical resource (RFC 8707) the token will be for; the host can check it matches serverUrl before opening url. |
| `scope` | `"project" \| "global" \| "plugin" \| "settings"` | yes | Scope of the declaration signing in. |
| `serverUrl` | `string` | yes | URL of the entry signing in, for the host to show before opening url. |
| `state` | `string` | yes | Opaque flow id; mcp.authorize.complete takes it back. |
| `url` | `string` | yes | Consent URL to open in a browser. |

### `mcp.authorize.complete`

*Direction:* client → server

Finish an mcp.authorize flow with the code the host's own redirect caught.

**Params**

| field | type | required | description |
|---|---|---|---|
| `code` | `string \| null` | no | The redirect's ?code=. |
| `error` | `string \| null` | no | The redirect's ?error=, when the user or server refused. |
| `iss` | `string \| null` | no | The redirect's ?iss= (RFC 9207). Checked against the authorization server's issuer; required when it advertises authorization_response_iss_parameter_supported. |
| `state` | `string` | yes | The state mcp.authorize returned (the redirect's ?state=). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | Server the sign-in was for. |
| `ok` | `boolean` | no | True when the tokens were stored. |
| `state` | `"stopped" \| "starting" \| "ready" \| "error"` | no | Server state after the restart with the new token. |

### `mcp.catalog`

*Direction:* client → server

The curated MCP servers a client can offer as presets.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ auth?: "oauth" \| "token" \| "none"; description?: string; entry?: Record<string, unknown>; homepage?: string; id: string; label: string; needs?: string[]; transport: "stdio" \| "http" \| "sse"; })[]` | no | Curated servers, in display order. |

### `mcp.deauthorize`

*Direction:* client → server

Forget a remote MCP server's stored OAuth tokens (1.34.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `name` | `string` | yes | Server name. |
| `scope` | `"project" \| "global" \| null` | no | Forget the sign-in of this scope's declaration; omitted, the one in effect, or every record of the name when none is declared. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `workdir` | `string \| null` | no | Project directory that declares it. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call ran. |
| `removed` | `boolean` | no | True when a stored sign-in was deleted. |

### `mcp.list`

*Direction:* client → server

List every configured MCP server with its scope, state and tools.

**Params**

| field | type | required | description |
|---|---|---|---|
| `includeShadowed` | `boolean` | no | Also list each declaration another scope overrides under the same name, marked shadowed: true (1.33.0). Off by default, so a client keyed by name keeps one row per server. |
| `sessionId` | `string \| null` | no | Session whose workdir to read. |
| `workdir` | `string \| null` | no | Project directory; defaults to the session's, then the daemon's. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `servers` | `({ args?: string[]; auth?: { clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; }; command?: string \| null; cwd?: string \| null; disabled?: boolean; envKeys?: string[]; error?: string \| null; headerKeys?: string[]; name: string; permission?: "read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside"; plugin?: string \| null; scope: "project" \| "global" \| "plugin" \| "settings"; shadowed?: boolean; state?: "stopped" \| "starting" \| "ready" \| "error"; timeoutSec?: number \| null; toolCount?: number; toolTimeoutSec?: number \| null; tools?: ({ description?: string; name: string; })[]; toolsExclude?: string[]; toolsInclude?: string[]; transport: "stdio" \| "http" \| "sse"; url?: string \| null; })[]` | no | One row per configured server, project entries winning; with includeShadowed, each declaration that loses to another scope follows its winner with shadowed: true (1.33.0). |

### `mcp.preset.apply`

*Direction:* client → server

Assistant presets (1.53.0, WP8): add the preset's MCP server with the given inputs and schedule the chosen recipes.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Preset to apply. |
| `inputs` | `Record<string, string>` | no | Values for its inputs, by name. |
| `name` | `string \| null` | no | Server name; default the preset id. |
| `recipes` | `string[]` | no | Recipe ids to schedule as jarvis-task jobs. |
| `scope` | `"project" \| "global"` | no | Where the server entry is written. |
| `workdir` | `string \| null` | no | Project folder for scope 'project'. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `add` | `{ auth?: { clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; }; error?: string \| null; ok?: boolean; path: string; state?: "stopped" \| "starting" \| "ready" \| "error"; tools?: ({ description?: string; name: string; })[]; warnings?: string[]; }` | yes | What mcp.add reported for the first entry. |
| `adds` | `({ auth?: { clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; }; error?: string \| null; ok?: boolean; path: string; state?: "stopped" \| "starting" \| "ready" \| "error"; tools?: ({ description?: string; name: string; })[]; warnings?: string[]; })[]` | no | mcp.add's report for each, in the same order. |
| `already` | `boolean` | no | Nothing changed: the preset was already applied the same way. |
| `jobs` | `string[]` | no | Scheduler job ids created for recipes. |
| `needsAuth` | `boolean` | no | Call mcp.authorize next (auth 'oauth'). |
| `server` | `string` | yes | Name of the first configured MCP server. |
| `servers` | `string[]` | no | Every server it configured (WP8), in order. |
| `signIn` | `({ command?: string \| null; method: "oauth" \| "deviceCode" \| "none"; note?: string; server: string; })[]` | no | What still needs a sign-in, per server. |

### `mcp.preset.check`

*Direction:* client → server

Assistant presets (1.53.0): whether each server of an applied preset is connected and still signed in (masked account; no tokens).

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | An applied preset. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `applied` | `boolean` | yes | False when the preset is not applied (servers empty). |
| `id` | `string` | yes | The preset. |
| `servers` | `({ account?: string \| null; authenticated?: boolean \| null; connected: boolean; detail?: string; server: string; })[]` | no |  |

### `mcp.preset.list`

*Direction:* client → server

Assistant presets (1.53.0, WP8): mail, calendar and to-do MCP setups with recipes.

**Params**

| field | type | required | description |
|---|---|---|---|
| `locale` | `string \| null` | no | Language of the human texts (title, description, input labels, server and recipe titles): 'en' (default) or 'ko'; any other value answers in English. Ids, tool names, entries and recipe prompts are never translated. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `locale` | `string` | no | The language the texts are in. |
| `presets` | `({ applied?: boolean; appliedServers?: string[]; auth: "oauth" \| "token" \| "none"; category: "mail" \| "calendar" \| "todo" \| "other"; description?: string; entry?: Record<string, unknown>; homepage?: string; id: string; inputs?: ({ label: string; name: string; placeholder?: string; required?: boolean; secret?: boolean; })[]; readTools?: string[]; recipes?: ({ defaultEnabled?: boolean; id: string; prompt: string; schedule: string; title: string; })[]; sendTools?: string[]; servers?: ({ auth: "oauth" \| "token" \| "none"; entry?: Record<string, unknown>; kind: string; loginCommand?: string \| null; readTools?: string[]; sendTools?: string[]; signIn?: "oauth" \| "deviceCode" \| "none"; suffix: string; title: string; transport: "stdio" \| "http" \| "sse"; })[]; title: string; transport: "stdio" \| "http" \| "sse"; })[]` | no | In display order. |

### `mcp.preset.remove`

*Direction:* client → server

Assistant presets (1.53.0, WP8): undo an applied preset: its servers, permission tags, stored client secret and recipe jobs go back to what they were before.

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Preset to remove. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `jobs` | `string[]` | no | Recipe jobs deleted. |
| `notice` | `string \| null` | no | What the person still has to do themselves, e.g. a device-code server's own sign-in that snowpea cannot clear (show it). Null when nothing is left. |
| `removed` | `boolean` | yes | False when the preset was not applied. |
| `servers` | `string[]` | no | Servers put back or removed. |

### `mcp.reload`

*Direction:* client → server

Restart one MCP server, or every configured one.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `name` | `string \| null` | no | Server to restart; all of them when absent. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `workdir` | `string \| null` | no | Project directory to rediscover from. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the reload ran. |
| `servers` | `string[]` | no | Servers that were restarted. |

### `mcp.remove`

*Direction:* client → server

Delete an MCP server entry and stop the server.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `name` | `string` | yes | Server name. |
| `scope` | `"project" \| "global" \| null` | no | Which file to rewrite; omitted, the one that declares it. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `workdir` | `string \| null` | no | Project directory for scope=project. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `mcp.test`

*Direction:* client → server

Probe a saved MCP server or an unsaved draft and report its tools.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `args` | `string[] \| null` | no | Arguments, argv style; never a shell string. |
| `command` | `string \| null` | no | Executable for a stdio server. |
| `cwd` | `string \| null` | no | Working directory for a stdio server. |
| `disabled` | `boolean \| null` | no | Keep the entry but never start the server. |
| `env` | `Record<string, string> \| null` | no | Environment for the child process. mcp.update, and mcp.test on a saved server, merge it key by key into the saved env (1.33.0); envRemove deletes keys. |
| `envRemove` | `string[] \| null` | no | Probe without these saved environment variables, as mcp.update would (1.33.0). |
| `headers` | `Record<string, string> \| null` | no | HTTP headers sent with every request. mcp.update, and mcp.test on a saved server, merge them key by key into the saved headers (1.33.0); headersRemove deletes keys. |
| `headersRemove` | `string[] \| null` | no | Probe without these saved HTTP headers, as mcp.update would (1.33.0). |
| `name` | `string \| null` | no | Saved server to probe. |
| `oauth` | `{ clientId?: string \| null; clientSecret?: string \| null; scope?: string \| null; } \| null` | no | OAuth client settings for an http/sse server (1.34.0); mcp.update merges them key by key. |
| `permission` | `"read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside" \| null` | no | Permission tag for the server's tools; stored in settings. |
| `scope` | `"project" \| "global" \| "plugin" \| "settings" \| null` | no | Scope of the saved server. Given, that scope's declaration is the one probed or merged onto, not whichever wins; when it has none the draft is probed on its own (1.33.0). |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `timeoutSec` | `number \| null` | no | Startup cap, in seconds. |
| `toolTimeoutSec` | `number \| null` | no | Per-call cap, in seconds. |
| `toolsExclude` | `string[] \| null` | no | Never register these tools of the server. [] clears the filter; null or absent leaves it as saved. |
| `toolsInclude` | `string[] \| null` | no | Register only these tools of the server. [] clears the filter; null or absent leaves it as saved. |
| `type` | `"stdio" \| "http" \| "sse" \| null` | no | Force a transport instead of inferring it. |
| `unset` | `("args" \| "cwd" \| "disabled" \| "env" \| "headers" \| "timeoutSec" \| "toolTimeoutSec" \| "toolsExclude" \| "toolsInclude" \| "type" \| "oauth")[] \| null` | no | Probe with these optional keys cleared, as mcp.update would (1.33.0). |
| `url` | `string \| null` | no | Endpoint for an http or sse server. |
| `workdir` | `string \| null` | no | Project directory for scope=project. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `auth` | `{ clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; }` | no | OAuth standing the probe found: needs_auth when it was refused with 401. |
| `elapsedMs` | `number` | no | How long the probe took. |
| `error` | `string \| null` | no | Spawn or protocol error, verbatim. |
| `ok` | `boolean` | yes | True when the server answered tools/list. |
| `state` | `"stopped" \| "starting" \| "ready" \| "error"` | yes | ready when the probe succeeded, error otherwise. |
| `tools` | `({ description?: string; name: string; })[]` | no | What the server exposes. |

### `mcp.update`

*Direction:* client → server

Merge a patch into an existing MCP server entry.

**Params**

| field | type | required | description |
|---|---|---|---|
| `acceptFindings` | `boolean` | no | Save the entry although the patch brings in a security finding; without it that is mcp_unsafe, as in mcp.add (1.33.0). |
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `envRemove` | `string[] \| null` | no | Environment variables to delete from the saved entry (1.33.0). |
| `force` | `boolean` | no | Same as acceptFindings here (1.33.0). |
| `headersRemove` | `string[] \| null` | no | HTTP headers to delete from the saved entry (1.33.0). |
| `name` | `string` | yes | Server name. |
| `patch` | `{ args?: string[] \| null; command?: string \| null; cwd?: string \| null; disabled?: boolean \| null; env?: Record<string, string> \| null; headers?: Record<string, string> \| null; oauth?: { clientId?: string \| null; clientSecret?: string \| null; scope?: string \| null; } \| null; permission?: "read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside" \| null; timeoutSec?: number \| null; toolTimeoutSec?: number \| null; toolsExclude?: string[] \| null; toolsInclude?: string[] \| null; type?: "stdio" \| "http" \| "sse" \| null; url?: string \| null; }` | no | Keys to change; anything absent or null is kept. env and headers merge key by key (1.33.0), except that a new url with no headers drops the saved headers; toolsInclude/toolsExclude [] clears that filter. |
| `scope` | `"project" \| "global" \| null` | no | Which file to rewrite; omitted, the one that declares it. |
| `sessionId` | `string \| null` | no | Session whose workdir to use. |
| `unset` | `("args" \| "cwd" \| "disabled" \| "env" \| "headers" \| "timeoutSec" \| "toolTimeoutSec" \| "toolsExclude" \| "toolsInclude" \| "type" \| "oauth")[] \| null` | no | Optional entry keys to clear, e.g. ['cwd', 'timeoutSec'] (1.33.0). Applied after the patch, as envRemove and headersRemove are, so a key named here is gone even if the patch also set it. |
| `workdir` | `string \| null` | no | Project directory for scope=project. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `memory.confirm`

*Direction:* client → server

Accept or drop a preference a memory.candidate event proposed (1.28.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `accept` | `boolean` | yes | True saves it as a confirmed memory; false drops it. |
| `candidateId` | `string` | yes | From the memory.candidate event. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `id` | `string \| null` | no | The stored memory when accepted; None when dropped. |

### `memory.delete`

*Direction:* client → server

Forget one stored memory.

**Params**

| field | type | required | description |
|---|---|---|---|
| `before` | `string \| null` | no | With source: UTC ISO time; forget visits before it. |
| `id` | `string \| null` | no | Memory id to forget. |
| `source` | `"browser" \| null` | no | "browser" forgets browsing memories instead: those of url (a page, or a whole site when it has no path), those visited before `before`, or all (1.7.0). |
| `url` | `string \| null` | no | With source: the page or site. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `memory.ingest`

*Direction:* client → server

Remember visited pages in the browser namespace, once per url and text.

**Params**

| field | type | required | description |
|---|---|---|---|
| `items` | `({ text?: string; title?: string; url: string; visitedAt?: string \| null; })[]` | yes | Visited pages to remember. |
| `sessionId` | `string \| null` | no | Session they came from, if any. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `added` | `number` | no | Pages stored. |
| `removed` | `number` | no | For memory.delete by source: how many. |
| `skipped` | `number` | no | Pages already stored (same url and text). |

### `memory.list`

*Direction:* client → server

List stored memories by scope, newest first.

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no | Maximum number of entries. |
| `namespace` | `"browser" \| null` | no | "browser" lists visited-page memories (memory.ingest) instead, newest first; scope, sessionId and project are then ignored (1.27.0). |
| `project` | `string \| null` | no | Project root to use instead of a session's, so a CLI in a checkout can list that project's memories without opening a session. |
| `query` | `string \| null` | no | Free-text filter; omit to list newest first. |
| `scope` | `"project" \| "global" \| "agent" \| "all" \| null` | no | Which scopes to list: "project", "global", "agent" or "all" (default). |
| `sessionId` | `string \| null` | no | Session whose project and agent scopes to resolve; omit for global only. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ confirmed?: boolean; createdAt?: string; id: string; project?: string; scope?: "project" \| "global" \| "agent" \| "all"; sensitive?: boolean; source?: "browser" \| "user-said" \| "model" \| "nudge" \| "auto-pref" \| null; sourceSession?: string \| null; tags?: string[]; text: string; url?: string; visitedAt?: string; })[]` | no | Matching memories, newest or best first. |

### `memory.search`

*Direction:* client → server

Recall stored memories matching a query.

**Params**

| field | type | required | description |
|---|---|---|---|
| `limit` | `number` | no | Maximum number of hits. |
| `namespace` | `string \| null` | no | Memory namespace to search; defaults to "default". Never crosses namespaces. |
| `query` | `string` | yes | Free-text query. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `hits` | `({ id: string; project?: string; scope?: "project" \| "global" \| "agent" \| "all"; score?: number; tags?: string[]; text: string; })[]` | no | Matches, best first. |

### `memory.update`

*Direction:* client → server

Edit one stored memory in place; it becomes confirmed (1.28.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `id` | `string` | yes | Memory id. |
| `text` | `string` | yes | The new text; refused like memory.write when it holds a value. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entry` | `{ confirmed?: boolean; createdAt?: string; id: string; project?: string; scope?: "project" \| "global" \| "agent" \| "all"; sensitive?: boolean; source?: "browser" \| "user-said" \| "model" \| "nudge" \| "auto-pref" \| null; sourceSession?: string \| null; tags?: string[]; text: string; url?: string; visitedAt?: string; }` | yes | The memory after the edit. |

### `memory.write`

*Direction:* client → server

Store a memory with tags.

**Params**

| field | type | required | description |
|---|---|---|---|
| `namespace` | `string \| null` | no | Memory namespace to write into; defaults to "default". |
| `tags` | `string[]` | no | Tags for later filtering. |
| `text` | `string` | yes | Text to remember. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Id of the stored memory. |

### `permission.allowlist.add`

*Direction:* client → server

Promote a pattern from ask to allow.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `pattern` | `string` | yes | Glob or command prefix promoted from ask to allow. |
| `scope` | `"session" \| "project" \| "always"` | no | Where the pattern is stored. |
| `workdir` | `string \| null` | no | Absolute folder of the project to use (its .snowpea/settings.json). Default: the caller's most recent session's workdir (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patternId` | `string` | yes | Id used to remove the pattern later. |

### `permission.allowlist.list`

*Direction:* client → server

List stored allowlist patterns.

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | Only this browser profile's global entries (plus project entries); omit for every entry (addendum 8). |
| `scope` | `"session" \| "project" \| "always" \| null` | no | Filter by scope; omit for all. |
| `workdir` | `string \| null` | no | Absolute folder of the project to use (its .snowpea/settings.json). Default: the caller's most recent session's workdir (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `patterns` | `({ createdAt?: string \| null; hostToolsFrom?: string \| null; origin?: string \| null; pattern: string; patternId: string; reasonCode?: string \| null; scope: "session" \| "project" \| "always"; tool?: string \| null; })[]` | no | Stored allowlist entries. |

### `permission.allowlist.remove`

*Direction:* client → server

Delete an allowlist pattern by id.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `hostToolsFrom` | `string \| null` | no | When given, a global entry is removed only if it belongs to this browser profile (addendum 8). |
| `patternId` | `string` | yes | Pattern to delete. |
| `workdir` | `string \| null` | no | Absolute folder of the project to use (its .snowpea/settings.json). Default: the caller's most recent session's workdir (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `project.trust.revoke`

*Direction:* client → server

Project trust (1.55.0): take scopes back (all when omitted). A local person only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[] \| null` | no | Scopes to take back; omitted: all of them. |
| `workdir` | `string` | yes | A folder of the project. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `at` | `string \| null` | no | When (UTC ISO). |
| `by` | `string \| null` | no | Who trusted it (ide, tui, cli, migration). |
| `changed` | `string[]` | no | Files of a trusted scope that are new or changed since the trust. |
| `defaultModeAuto` | `boolean` | no | The project file asks for defaultMode auto. |
| `digests` | `Record<string, string>` | no | 1.55.0: what trusting the project now would pin, {path: 'sha256:<hex>'}. Send it back unchanged as project.trust.set {digests}. |
| `files` | `({ commands?: ({ head: string; label?: string; length: number; tail?: string; })[]; digest?: string; path: string; scope: "hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli"; state: "active" \| "untrusted" \| "new" \| "changed"; summary?: string; })[]` | no |  |
| `inactive` | `string[]` | no | Control files not active now (any reason). |
| `modeActive` | `boolean` | no | …and the mode scope lets it apply (else accept). |
| `notPinned` | `({ command: { head: string; label?: string; length: number; tail?: string; }; reason: "computed" \| "project-script" \| "package-runner" \| "module" \| "missing" \| "too-large" \| "too-many" \| "unreadable"; source: string; })[]` | no | 1.55.0: commands whose code cannot be pinned, each with a reason. Trust still covers the command line itself; the card should say that what it runs may change without asking again. |
| `runs` | `({ by?: string[]; digest: string; path: string; state: "active" \| "untrusted" \| "new" \| "changed"; })[]` | no | 1.55.0 (security review 6): the project files the hooks, MCP servers and skills run — path-like words of a hook command or of a server's command and args that are files inside the project, and every other file in a skill's folder. Trusting pins them too; one that changes needs the project trusted again, like any pinned file. |
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[]` | no |  |
| `trusted` | `boolean` | yes | Some scope is trusted. |
| `workdir` | `string` | yes | The project root (real path). |

### `project.trust.set`

*Direction:* client → server

Project trust (1.55.0): trust the project for scopes, pinning every control file as it is now. A local person only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `digests` | `Record<string, string>` | yes | Required (1.55.0, security review 6): project.trust.status's digests object, sent back exactly as it was returned — {path relative to the project: 'sha256:<hex>'} for every control file, every project file one of them runs and the project settings file. What is pinned is what the person was shown: when the project differs now (a digest differs, a file is on disk that is not in digests, a file in digests is gone) nothing is trusted and the call fails with trust_changed, details {changed: [path], added: [path], missing: [path]}. Read the status again, show it, and send the new digests. {} for a project that brings nothing. |
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[] \| null` | no | Scopes to grant (added to what it has). Omitted: hooks, mcp, commands, agents, prompts, settings — not mode (defaultMode: auto from the project file) and not externalCli, which the person names explicitly. Every control file is pinned as digests says it is. |
| `workdir` | `string` | yes | A folder of the project (its real path is the key). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `at` | `string \| null` | no | When (UTC ISO). |
| `by` | `string \| null` | no | Who trusted it (ide, tui, cli, migration). |
| `changed` | `string[]` | no | Files of a trusted scope that are new or changed since the trust. |
| `defaultModeAuto` | `boolean` | no | The project file asks for defaultMode auto. |
| `digests` | `Record<string, string>` | no | 1.55.0: what trusting the project now would pin, {path: 'sha256:<hex>'}. Send it back unchanged as project.trust.set {digests}. |
| `files` | `({ commands?: ({ head: string; label?: string; length: number; tail?: string; })[]; digest?: string; path: string; scope: "hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli"; state: "active" \| "untrusted" \| "new" \| "changed"; summary?: string; })[]` | no |  |
| `inactive` | `string[]` | no | Control files not active now (any reason). |
| `modeActive` | `boolean` | no | …and the mode scope lets it apply (else accept). |
| `notPinned` | `({ command: { head: string; label?: string; length: number; tail?: string; }; reason: "computed" \| "project-script" \| "package-runner" \| "module" \| "missing" \| "too-large" \| "too-many" \| "unreadable"; source: string; })[]` | no | 1.55.0: commands whose code cannot be pinned, each with a reason. Trust still covers the command line itself; the card should say that what it runs may change without asking again. |
| `runs` | `({ by?: string[]; digest: string; path: string; state: "active" \| "untrusted" \| "new" \| "changed"; })[]` | no | 1.55.0 (security review 6): the project files the hooks, MCP servers and skills run — path-like words of a hook command or of a server's command and args that are files inside the project, and every other file in a skill's folder. Trusting pins them too; one that changes needs the project trusted again, like any pinned file. |
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[]` | no |  |
| `trusted` | `boolean` | yes | Some scope is trusted. |
| `workdir` | `string` | yes | The project root (real path). |

### `project.trust.status`

*Direction:* client → server

Project trust (1.55.0): what the project's own files would make snowpea do (hooks, MCP servers, commands with allowed-tools, agents with a permission, prompt overrides, widening settings, defaultMode auto), which are active, and what changed since it was trusted. People (a remote login too).

**Params**

| field | type | required | description |
|---|---|---|---|
| `workdir` | `string` | yes | A folder of the project (its real path is the key). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `at` | `string \| null` | no | When (UTC ISO). |
| `by` | `string \| null` | no | Who trusted it (ide, tui, cli, migration). |
| `changed` | `string[]` | no | Files of a trusted scope that are new or changed since the trust. |
| `defaultModeAuto` | `boolean` | no | The project file asks for defaultMode auto. |
| `digests` | `Record<string, string>` | no | 1.55.0: what trusting the project now would pin, {path: 'sha256:<hex>'}. Send it back unchanged as project.trust.set {digests}. |
| `files` | `({ commands?: ({ head: string; label?: string; length: number; tail?: string; })[]; digest?: string; path: string; scope: "hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli"; state: "active" \| "untrusted" \| "new" \| "changed"; summary?: string; })[]` | no |  |
| `inactive` | `string[]` | no | Control files not active now (any reason). |
| `modeActive` | `boolean` | no | …and the mode scope lets it apply (else accept). |
| `notPinned` | `({ command: { head: string; label?: string; length: number; tail?: string; }; reason: "computed" \| "project-script" \| "package-runner" \| "module" \| "missing" \| "too-large" \| "too-many" \| "unreadable"; source: string; })[]` | no | 1.55.0: commands whose code cannot be pinned, each with a reason. Trust still covers the command line itself; the card should say that what it runs may change without asking again. |
| `runs` | `({ by?: string[]; digest: string; path: string; state: "active" \| "untrusted" \| "new" \| "changed"; })[]` | no | 1.55.0 (security review 6): the project files the hooks, MCP servers and skills run — path-like words of a hook command or of a server's command and args that are files inside the project, and every other file in a skill's folder. Trusting pins them too; one that changes needs the project trusted again, like any pinned file. |
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[]` | no |  |
| `trusted` | `boolean` | yes | Some scope is trusted. |
| `workdir` | `string` | yes | The project root (real path). |

### `provider.configure`

*Direction:* client → server

Store settings and credentials for a provider.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `config` | `Record<string, unknown>` | no | Vendor-specific settings, including credentials. |
| `vendor` | `string` | yes | Vendor to configure. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `provider.list`

*Direction:* client → server

List chat providers and whether they are configured.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `providers` | `({ authMethods?: string[]; authStatus?: "unconfigured" \| "active" \| "expired"; baseUrl?: string; configured?: boolean; custom?: boolean; default?: boolean; defaultBaseUrl?: string; defaultModel?: string; label?: string; models?: string[]; preset?: string; supportsEffort?: boolean; variant?: string; vendor: string; })[]` | no | Known chat providers. |

### `provider.loginWeb`

*Direction:* client → server

Start a browser-based login flow for a provider.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `method` | `string` | yes | Login flow to start: 'browser_pkce' or 'google_oauth' (browser), 'device_code' or 'google_adc' (headless), 'oauth_pkce' (OpenRouter). 'web' or an empty value picks the best flow this machine can complete. |
| `vendor` | `string` | yes | Vendor to log into. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `expiresInSec` | `number \| null` | no | Seconds until the code or session expires, when known. |
| `ok` | `boolean` | no | True when the call succeeded. |
| `status` | `"await_user" \| "done" \| "failed"` | no | 'await_user' once userCode/verificationUri are ready and polling has started in the background; 'done' or 'failed' if the flow finished synchronously before this response was sent. |
| `userCode` | `string \| null` | no | Short code the user types in, for device-code flows. |
| `verificationUri` | `string \| null` | no | URL to open to approve the login. |
| `verificationUriComplete` | `string \| null` | no | verificationUri with the code already embedded, when known. |

### `provider.models`

*Direction:* client → server

Ask a vendor's endpoint which models it serves.

**Params**

| field | type | required | description |
|---|---|---|---|
| `models` | `string[]` | no | Extra model ids (hand-typed, or no longer listed) to include in contextWindows (1.42.0). The vendor's configured model and the models.default / models.categories.* that point at this vendor are always included. |
| `vendor` | `string \| null` | no | Vendor to query; defaults to the configured one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `contextWindows` | `Record<string, { contextWindow: number; contextWindowBounds?: { lower?: number \| null; upper?: number \| null; } \| null; contextWindowScope?: "model" \| "vendor" \| null; contextWindowSource: "settings" \| "server" \| "error" \| "catalog" \| "learned" \| "assumed"; }>` | no | One record per listed model (1.42.0): its context window in tokens (never unknown), where that came from, and any bounds requests taught us. Covers the listed models, the configured one, models.default / models.categories.* that point at this vendor, and any ids passed in params.models. A model whose source is 'assumed' is worth a look. |
| `current` | `string` | no | Model this vendor uses today. |
| `detail` | `string` | no | One line naming the source, for a picker to show. |
| `error` | `string \| null` | no | Why the live listing could not be used, when source is not 'live' (1.17.0); absent when the vendor answered or publishes no listing. |
| `models` | `string[]` | no | Model ids the vendor's endpoint reports. Whatever the source, models[0] is the recommended default (a ChatGPT account's first-priority model, the newest chat model for an API key), so a picker can take current or models[0]. |
| `source` | `string` | no | Which rung answered: live (the vendor's endpoint), settings (providers.<vendor>.models), cache (the last good listing) or curated (this build's list, merged with models.dev). |
| `vendor` | `string` | yes | Vendor the listing came from. |
| `vision` | `Record<string, boolean>` | no | Which of the listed models can be sent images, when that is known. A model missing from this map is unknown rather than text-only, so a picker draws no badge for it instead of a negative one. |

### `provider.remove`

*Direction:* client → server

Forget a configured provider, typically a named local server.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `vendor` | `string` | yes | Provider to forget: its credentials, its model profiles and the agent assignments that used them. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `provider.test`

*Direction:* client → server

Send one short completion to check a provider and model.

**Params**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | Model; defaults to the vendor's default. |
| `provider` | `string` | yes | Vendor to test. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `error` | `string \| null` | no | Why it failed. |
| `latencyMs` | `number` | no | Round-trip time of the test call. |
| `model` | `string` | no | Model the request used. |
| `modelEcho` | `string \| null` | no | Model name the server reported, when it did. |
| `ok` | `boolean` | yes | True when a one-line completion came back. |
| `provider` | `string` | yes | Vendor tested. |
| `reply` | `string \| null` | no | The model's reply, trimmed. |

### `provider.visionReset`

*Direction:* client → server

Forget the learned vision answer for a model so the next picture is tried again (1.41.0, capability 'visionNotice'). A configured providers.<v>.vision wins as before.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `model` | `string \| null` | no | Model whose learned 'cannot see images' answer is forgotten; null: the vendor's current model. |
| `vendor` | `string \| null` | no | Vendor to reset; defaults to the configured one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `forgotten` | `boolean` | yes | True when something had been learned and is now forgotten. |
| `model` | `string` | yes | Model that was reset. |
| `vendor` | `string` | yes | Vendor that was reset. |

### `question.list`

*Direction:* client → server

List questions the agent is still waiting on.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `requests` | `({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; kind?: "mode" \| null; neverDelegable?: boolean \| null; personOnly?: boolean \| null; questions?: ({ allowOther?: boolean; header?: string; multi?: boolean; options?: ({ description?: string; label: string; preview?: string; })[]; question: string; secret?: boolean; })[]; requestId: string; route?: "person" \| "jarvis" \| "auto" \| null; sessionId: string; timeoutSec?: number; })[]` | no | Questions still waiting for an answer. |

### `question.request`

*Direction:* server → client

Ask the client to put a question to the human.

**Params**

| field | type | required | description |
|---|---|---|---|
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | Set when an external CLI session asks (1.53.0); see ApprovalRequest. |
| `kind` | `"mode" \| null` | no | What a personOnly question decides (1.54.0): 'mode' for a mode picker. Null for an ordinary question. |
| `neverDelegable` | `boolean \| null` | no | True when only a person may decide this question (1.54.0): it is personOnly. Clients label it "always asks you". Null otherwise. |
| `personOnly` | `boolean \| null` | no | True when only a person may answer (1.54.0, security review 4 C1): the answer changes the session's permissions or raises its mode (set_mode, the plan-exit and plan-review pickers). Routed to the person; Jarvis's question_answer is refused (jarvis_person_only); a browser or connector may not answer it. Null otherwise. |
| `questions` | `({ allowOther?: boolean; header?: string; multi?: boolean; options?: ({ description?: string; label: string; preview?: string; })[]; question: string; secret?: boolean; })[]` | no | The questions, in the order they were asked. |
| `requestId` | `string` | yes | Id to answer with question.respond. |
| `route` | `"person" \| "jarvis" \| "auto" \| null` | no | person or jarvis while Jarvis mode is on (1.53.0); null when it is off. A question is never 'auto'. |
| `sessionId` | `string` | yes | Session whose turn is blocked. |
| `timeoutSec` | `number` | no | Seconds before the batch gives up; 0 means no limit (1.35.0: a TUI or desktop session waits for its person). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `answers` | `({ selected?: string[]; text?: string \| null; })[]` | no | One entry per question, in question order. |

### `question.respond`

*Direction:* client → server

Answer a pending question and unblock the turn.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | A browser host sets 'person' when a click answered another session's question (security review 3); a browser or connector otherwise answers only its own sessions' questions (not_found). Ignored for other kinds. |
| `answers` | `({ selected?: string[]; text?: string \| null; })[]` | no | One entry per question, in question order; empty declines the batch. |
| `requestId` | `string` | yes | Batch being answered. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `security.check`

*Direction:* client → server

Security lists (1.54.0): judge a command or a path against the lists (or an unsaved draft) without running anything — the 'try it' box.

**Params**

| field | type | required | description |
|---|---|---|---|
| `access` | `"write" \| "read"` | no | With path: judge a change (default) or a read. |
| `command` | `string \| null` | no | A shell line to judge. |
| `draft` | `{ blockedCommands?: { custom?: string[]; presets?: string[]; }; protectedPaths?: { custom?: ({ level?: "write" \| "all"; path: string; })[]; presets?: string[]; }; } \| null` | no | Unsaved lists to judge against instead of the saved global ones (the project's additions still apply). Invalid entries are invalid_params. |
| `locale` | `string \| null` | no | Language of 'why'; default 'en'. |
| `path` | `string \| null` | no | A path to judge. |
| `workdir` | `string \| null` | no | Where it would run (relative paths, project additions). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `blocked` | `boolean` | yes |  |
| `by` | `string \| null` | no | 'commands:<presetId>', 'paths:<presetId>', 'custom', or 'security' (an edit of the security lists themselves). |
| `level` | `"write" \| "all" \| null` | no | For a protected path hit: the entry's level. |
| `match` | `string \| null` | no | The command or protected path hit. |
| `opaque` | `boolean` | no | Not blocked, but part of it cannot be seen while a list is set: it would always be put to the person (reason). |
| `reason` | `string \| null` | no | 'cannot be checked against your block list' when opaque. |
| `why` | `string \| null` | no | Localized reason. |

### `security.get`

*Direction:* client → server

Security lists (1.54.0): the stored global lists, a project's additions, the effective lists with presets expanded, and whether this connection may change them (settings.set {scope: 'global', patch: {security: …}}, a local person only; arrays replace).

**Params**

| field | type | required | description |
|---|---|---|---|
| `locale` | `string \| null` | no | For problems; default 'en'. |
| `workdir` | `string \| null` | no | A project: adds its additions and resolves its presets. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `blockedCommands` | `{ custom?: string[]; presets?: string[]; }` | no |  |
| `effective` | `{ blockedCommands?: { custom?: string[]; presets?: string[]; }; commands?: ({ by: string; pattern: string; })[]; paths?: ({ by: string; exempt?: string[]; level: "write" \| "all"; path: string; scope: "tree" \| "self"; })[]; protectedPaths?: { custom?: ({ level?: "write" \| "all"; path: string; })[]; presets?: string[]; }; }` | no | The lists in force: the global ones plus the project's additions. |
| `problemItems` | `({ entry?: string \| null; list: "commands" \| "paths" \| null; reason: string; })[]` | no | 1.55.0: the same problems, structured, in the same order. settings.set and security.check refuse a bad security value with invalid_params whose details carry both problems (text) and problemItems (this shape). |
| `problems` | `string[]` | no | Entries skipped as unusable (a hand-edited file): unknown preset, bad pattern. |
| `project` | `{ blockedCommands?: { custom?: string[]; presets?: string[]; }; protectedPaths?: { custom?: ({ level?: "write" \| "all"; path: string; })[]; presets?: string[]; }; } \| null` | no | What the workdir's .snowpea/settings.json adds; null without. |
| `protectedPaths` | `{ custom?: ({ level?: "write" \| "all"; path: string; })[]; presets?: string[]; }` | no |  |
| `unknownCode` | `"ask" \| "allow"` | no | security.unknownCode (1.55.0, security review 6): what happens to code the check cannot resolve — a write whose target is computed while it runs, code it cannot parse, an interpreter fed code it cannot read. 'ask' (default): put to the person in every mode (ApprovalRequest.reason 'cannot tell what this code changes'). 'allow': let through. Either way snowpea's own control files stay protected and every blocked finding stays blocked. Written with settings.set {scope: 'global', patch: {security: {unknownCode}}} by a local person only; a project scope refuses it (invalid_params, details.reason 'global_only'). |
| `writable` | `boolean` | yes | True only for a local person's connection: the only one settings.set accepts security.* from. A remote web login, a browser or a connector reads only. |

### `security.presets.list`

*Direction:* client → server

Security lists (1.54.0): the preset catalog of blocked commands and protected paths, localized (en, ko). People only (not a browser or connector).

**Params**

| field | type | required | description |
|---|---|---|---|
| `locale` | `string \| null` | no | 'en' or 'ko' (others fall back to English); default 'en'. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ default: boolean; description: string; examples?: string[]; id: string; level?: "write" \| "all" \| null; paths?: string[]; title: string; })[]` | no |  |
| `locale` | `string` | no | The locale the texts are in. |
| `paths` | `({ default: boolean; description: string; examples?: string[]; id: string; level?: "write" \| "all" \| null; paths?: string[]; title: string; })[]` | no |  |

### `session.archive`

*Direction:* client → server

Archive or restore a conversation; refused while it runs unless stop (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Conversation to flag. |
| `stop` | `boolean` | no | session.archive only: stop a running turn first instead of refusing. |
| `value` | `boolean` | yes | True to pin (or archive), false to undo. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.artifacts`

*Direction:* client → server

Files the session saved for the user under its workspace's artifacts/.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `artifacts` | `({ mimeType?: string \| null; modifiedAt: string; name: string; path: string; size: number; })[]` | no | Files under artifacts/, newest first. |
| `workspaceDir` | `string` | yes | The session workspace. |

### `session.attach`

*Direction:* client → server

Make this connection the origin of a session (approvals, host tools).

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | Re-point the session's host tools to this clientId (or surface id). It must name an open connection whose clientKind is 'browser'. Persisted; emits sessions.changed (reason 'host'). |
| `locale` | `string \| null` | no | The client's UI language as a BCP 47 tag (1.7.0), as on session.create. Kept in memory only, so a client re-sends it after a daemon restart; omitted leaves the session's current locale. |
| `sessionId` | `string` | yes | Session this connection becomes the origin of (1.6.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `hostTools` | `string[]` | no | Host tools the session now sees. |
| `hostToolsFrom` | `string \| null` | no | The session's hostToolsFrom after the call; older cores omit it. |
| `sessionId` | `string` | yes | The attached session. |

### `session.attachments`

*Direction:* client → server

Files the user attached in this session, with the path of the original bytes the agent may use (1.23.0). A delegated child reports its root's.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ byPath?: boolean; messageId?: string \| null; mime: string; name: string; path: string; size: number; })[]` | no | This session's attachments, oldest first. |

### `session.close`

*Direction:* client → server

Close a session and release its resources.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.compact`

*Direction:* client → server

Summarise the conversation so far and replace the history with it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `instructions` | `string \| null` | no | Extra guidance for the summary, e.g. 'keep the API design decisions'. |
| `sessionId` | `string` | yes | Session whose history to compact. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `after` | `number` | no | Estimated tokens the history holds now. |
| `before` | `number` | no | Estimated tokens the history held before. |
| `summaryChars` | `number` | no | Length of the summary in characters. |

### `session.continue`

*Direction:* client → server

Continue a chat session on the system's behalf, e.g. after a human hand-back (1.8.0). A running turn gets the note at its next model call (like session.notice); otherwise a turn starts whose input is the [system] note, with turn.started initiator 'system' and no message.user. Same callers as session.setAgent; subagent and scheduled sessions are refused with 'invalid_params'. One system start per session per 10 s, and an idempotencyKey is honoured once; a call inside the 10 s window keeps its note as a notice (queued: true).

**Params**

| field | type | required | description |
|---|---|---|---|
| `idempotencyKey` | `string \| null` | no | The same key twice: the second call is a no-op. |
| `note` | `string` | yes | The [system] line the model sees, e.g. 'the tab is back'. |
| `reason` | `string` | no | Why the session continues, e.g. 'handback'. |
| `sessionId` | `string` | yes | A chat session the caller may drive. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `duplicate` | `boolean \| null` | no | True when idempotencyKey was already used. |
| `queued` | `boolean \| null` | no | True when a turn was running and the note joins its next model call. |
| `started` | `boolean` | yes | True when a new system-initiated turn started. |
| `turnId` | `string \| null` | no | The turn that started, if any. |

### `session.create`

*Direction:* client → server

Open a session rooted at a working directory.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | A browser (clientVersion 0.3.0+) sets 'person' when a click created the session with a mode wider than the default (1.54.0). |
| `agent` | `string \| null` | no | Named agent whose persona to load. |
| `browserMemory` | `boolean \| null` | no | Recall also searches pages the browser ingested. Default: on when a browser client (clientKind 'browser') creates the session (1.7.0). |
| `denyExec` | `boolean \| null` | no | When true, refuse exec-tagged tools without prompting (headless CI). |
| `effort` | `"low" \| "medium" \| "high" \| "max" \| null` | no | Reasoning effort for this session; null follows the settings. |
| `hostToolsFrom` | `string \| null` | no | Whose host tools this session sees: a connection's clientId (or surface id). Default: the creating connection (1.6.0). |
| `locale` | `string \| null` | no | The client's UI language as a BCP 47 tag, e.g. 'ko-KR' (1.7.0). With agent.replyLanguage 'auto', text the core writes for this session (/init, /ralph and delegated briefs) is in this language before any guess from what the user typed. Ignored when not a language tag. |
| `maxConcurrent` | `number \| null` | no | Override for concurrent subagents. |
| `memory` | `boolean \| null` | no | Long-term memory for this session only (1.27.0): false means no recall block, no digest, no auto-remember and save_memory/search_memory answer tool_inactive; true turns it on although memory.enabled is off. Omit to follow the setting. Kept with the session across a daemon restart (1.31.0); a delegated child follows its root conversation's. |
| `mode` | `"plan" \| "accept" \| "auto" \| null` | no | Starting mode; defaults to the project setting (defaultMode, else 'accept'). A mode wider than that default (plan < accept < auto) needs a person (1.54.0): a local person, a remote web login, or a browser with actor 'person'; else jarvis_person_only with details.reason 'mode_raise'. |
| `model` | `string \| null` | no | Model id; defaults to the provider's default. |
| `originSurface` | `string \| null` | no | Surface that owns approvals for this session (TUI, gateway, ...). |
| `provider` | `string \| null` | no | Chat provider vendor; defaults to configured. |
| `siteMemory` | `boolean \| null` | no | Site memory for this session only (1.27.0): false makes site.get/put/mark/markPart called with this sessionId answer tool_inactive; true allows them although browser.siteMemory is off. Omit to follow the setting. Kept with the session across a daemon restart and followed by its delegated children (1.31.0); false does not stop host.turnContext (1.35.0): the request says siteMemory false, so the host leaves its site-memory part out. |
| `workdir` | `string` | yes | Absolute path the session operates in. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `delegation` | `boolean` | no | Whether the session starts in delegation mode (the lead hands the work to its team): the session pin, else agents.delegateByDefault. |
| `jarvis` | `{ exposed: boolean; grantId?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; pausedUntil?: string \| null; takenOver?: boolean; } \| null` | no | As SessionSummary.jarvis (1.53.0); null while Jarvis mode is off. |
| `role` | `"jarvis" \| null` | no | As SessionSummary.role (1.53.0): 'jarvis' for the Jarvis session. |
| `sessionId` | `string` | yes | Id of the new session. |
| `workspaceDir` | `string \| null` | no | Session workspace with tmp/ and artifacts/ (1.7.0). |

### `session.deleteSaved`

*Direction:* client → server

Delete saved sessions.

**Params**

| field | type | required | description |
|---|---|---|---|
| `all` | `boolean` | no | Delete saved sessions from every directory. |
| `deleteWorkspace` | `boolean` | no | Also delete each session's workspace ($SNOWPEA_HOME/sessions/<date>_<id>: tmp/, artifacts/, downloads) — it can hold page text the user deleted the chat to get rid of. false keeps it (1.7.0). |
| `sessionId` | `string \| null` | no | Delete one saved session. |
| `workdir` | `string \| null` | no | Delete saved sessions rooted here. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `deleted` | `number` | no |  |

### `session.events`

*Direction:* client → server

A session's saved events, read only (1.27.0): unlike session.resume it does not subscribe, attach, restore or take the session's approvals.

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number` | no | Only events with a greater seq. |
| `limit` | `number` | no | At most this many events, oldest first. |
| `sessionId` | `string` | yes | Session whose saved events to read. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; sessionId: string; step?: number \| null; ts: string; turnId?: string \| null; })[]` | no | Saved events after afterSeq, in seq order. |
| `lastSeq` | `number` | no | The session's newest saved seq when the call was answered; with more false, the events returned reach it. |
| `more` | `boolean` | no | True when events past the last one returned exist. |
| `sessionId` | `string` | yes | The session. |

### `session.fork`

*Direction:* client → server

Branch a session into a new one at the end of a finished turn; the source is not touched (1.46.0, capability 'fork'). Refusals carry error.data.code fork_after_compaction, fork_turn_not_found, fork_no_finished_turn, fork_child_session or fork_running (1.50.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `atTurnId` | `string \| null` | no | Last turn to keep; omitted keeps everything up to the latest finished turn. A turn before the session's last compaction cannot be cut at. |
| `sessionId` | `string` | yes | Session to fork (open or closed). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `atTurnId` | `string \| null` | no | Last inherited turn. |
| `fromSessionId` | `string` | yes | The session it was forked from. |
| `historyLen` | `number` | no | Messages the new session starts with. |
| `sessionId` | `string` | yes | The new session. |

### `session.interrupt`

*Direction:* client → server

Stop the running turn as soon as possible.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.lineage`

*Direction:* client → server

A session's ancestors (breadcrumbs) and delegated descendants as a tree (1.48.0, capability 'lineage'); 1.50.0 follows forks up the chain and lists direct forks.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session whose lineage is read. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ancestors` | `({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: unknown; children?: unknown[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: unknown; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: "fork" \| "delegation"; running?: boolean; sessionId: string; sessionKind?: string; title?: string \| null; })[]` | no | From the root down to the session's parent, following both delegation (parentSessionId) and fork (forkedFrom) links (1.50.0). |
| `descendants` | `({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: string \| null; children?: ({ agent?: unknown; children?: unknown[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: unknown; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]; createdAt?: string; kind?: string; running?: boolean; sessionId: string; title?: string \| null; })[]` | no | Sessions it delegated to, as a tree. |
| `forks` | `({ atTurnId?: string \| null; createdAt?: string; sessionId: string; title?: string \| null; })[]` | no | Direct forks of this session, oldest first (1.50.0). |

### `session.list`

*Direction:* client → server

List live or saved sessions.

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | Only sessions owned by this client (a browser profile's clientId); omit for every session (addendum 8). |
| `includeClosed` | `boolean` | no | Include persisted closed sessions. Since 1.55.0 their rows carry running false, status 'idle', title (the stored title; for a scheduled run without one, the job's title or the first line of its task), lastActivityAt (time of the last stored event), lastUserAt and seq, like live rows. |
| `includeUnhosted` | `boolean` | no | With hostToolsFrom: also return sessions with no host (IDE, TUI, CLI), which a browser shows in every profile. |
| `kinds` | `string[] \| null` | no | Only sessions of these kinds; omit for every kind. A surface that shows human threads asks for ["chat"] (CORE-session-kind). |
| `workdir` | `string \| null` | no | Only sessions rooted here. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `complete` | `boolean` | no | True when the list is all there is for what was asked (1.27.0): the daemon's boot has restored the sessions it reopens, and with includeClosed the saved ones were read. Without includeClosed it covers live sessions only, which after a restart is usually none; ask with includeClosed before forgetting a closed chat. False (or absent, from an older daemon) means an empty or short list proves nothing, so a client must not forget its own records by it. |
| `sessions` | `({ agent?: string \| null; archived?: boolean; autoAfterPlan?: boolean; browserProvider?: "host" \| "local" \| null; contextUsed?: number; contextWindow?: number \| null; contextWindowSource?: "settings" \| "server" \| "error" \| "catalog" \| "learned" \| "assumed" \| null; createdAt: string; effectiveEffort?: "low" \| "medium" \| "high" \| "max" \| null; effort?: "low" \| "medium" \| "high" \| "max" \| null; effortSource?: "session" \| "model" \| "vendor" \| "default" \| null; efforts?: ("low" \| "medium" \| "high" \| "max")[] \| null; forkedAtTurnId?: string \| null; forkedFrom?: string \| null; hostToolsFrom?: string \| null; jarvis?: { exposed: boolean; grantId?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; pausedUntil?: string \| null; takenOver?: boolean; } \| null; jobId?: string \| null; kind?: "chat" \| "scheduled" \| "subagent" \| "agent"; lastActivityAt?: string \| null; lastPrompt?: string \| null; lastUserAt?: string \| null; machine?: { container: boolean; hostname: string; id: string; os: "linux" \| "darwin" \| "windows"; } \| null; mode: "plan" \| "accept" \| "auto"; model?: string \| null; origin?: { clientId?: string \| null; clientKind?: string \| null; live: boolean; mine?: boolean; } \| null; originSurface?: string \| null; parentSessionId?: string \| null; pendingApprovals?: number; pendingQuestions?: number; pinned?: boolean; provider?: string \| null; role?: "jarvis" \| null; running?: boolean; seq?: number; sessionId: string; status?: "idle" \| "running" \| "awaiting_approval" \| "awaiting_question" \| "error"; title?: string \| null; turnStartedAt?: string \| null; workdir: string; workspaceDir?: string \| null; workspaceId?: string \| null; })[]` | no | Every live session. |

### `session.notice`

*Direction:* client → server

Queue a [system] line for the model's next call.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session to tell. |
| `text` | `string` | yes | Something that happened outside a tool call (a popup opened, a download finished). Reaches the model as a [system] line before its next call; it starts no turn (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.pin`

*Direction:* client → server

Pin or unpin a conversation (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Conversation to flag. |
| `stop` | `boolean` | no | session.archive only: stop a running turn first instead of refusing. |
| `value` | `boolean` | yes | True to pin (or archive), false to undo. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.prompt`

*Direction:* client → server

Send user text to a session and start a turn. Events: turn.started {turnId, prompt, queued}, then message.user {text, attachments, refs, steered: false}, then the model's output. message.user.text is what the user typed; a page attachment is listed as {kind: page, name, url, title} and only the model sees the rendered page. A steered prompt emits message.user {steered: true} + turn.dequeued {reason: steered}; a queued one emits turn.queued first.

**Params**

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ data?: string \| null; kind?: "file" \| "image" \| "text" \| "page"; mimeType?: string \| null; name?: string \| null; path?: string \| null; selection?: string \| null; size?: number \| null; snapshot?: string \| null; text?: string \| null; title?: string \| null; url?: string \| null; })[] \| null` | no | Files or images to include. |
| `sessionId` | `string` | yes | Session to prompt. |
| `text` | `string` | yes | User text; a leading '/' is parsed as a slash command. |
| `whenBusy` | `"queue" \| "steer" \| null` | no | If a turn is running: 'queue' waits for it, 'steer' folds this prompt into it at the next tool round. Default: the agent.busy setting (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `besideTurn` | `boolean \| null` | no | 1.55.0: true when the slash command ran beside the running turn (an instant command); absent otherwise. |
| `turnId` | `string` | yes | Id of the started turn; turn.done carries it back. Since 1.55.0, for an instant slash command sent while a turn runs, the command's id ('c-…'): no turn starts and command.result carries it back instead (besideTurn true). |

### `session.rename`

*Direction:* client → server

Set a session's title.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session to rename. |
| `title` | `string` | yes | New title; empty clears it (1.7.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.resume`

*Direction:* client → server

Replay the events a disconnected client missed.

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number \| null` | no | Replay only events with a greater seq. |
| `locale` | `string \| null` | no | The client's UI language as a BCP 47 tag (1.7.0), as on session.create. Kept in memory only, so a client re-sends it after a daemon restart; omitted leaves the session's current locale. |
| `sessionId` | `string` | yes | Session to resume. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `autoAfterPlan` | `boolean` | no | Plan-then-auto as restored with the mode (1.32.0). |
| `delegation` | `boolean` | no | Whether delegation mode is in force: the session pin, else the default. |
| `effectiveEffort` | `"low" \| "medium" \| "high" \| "max" \| null` | no | The effort in force (1.29.0): the pin, or what the settings give, clamped to what the model offers; null when the model has no effort setting or the session is closed. 'effort' stays the session's own pin. |
| `effort` | `string \| null` | no | Reasoning effort pinned on the session. |
| `effortSource` | `"session" \| "model" \| "vendor" \| "default" \| null` | no | Rule that decided effectiveEffort (1.29.0). |
| `efforts` | `("low" \| "medium" \| "high" \| "max")[] \| null` | no | Tiers the session's model offers; [] means none (1.29.0). |
| `events` | `({ kind: string; payload?: Record<string, unknown>; seq: number; sessionId: string; step?: number \| null; ts: string; turnId?: string \| null; })[]` | no | Missed events in seq order. |
| `mode` | `"plan" \| "accept" \| "auto" \| null` | no | The session's permission mode as restored, so a surface that resumes adopts it instead of keeping the mode it launched with. |
| `model` | `string \| null` | no | Model id in use. |
| `origin` | `{ clientId?: string \| null; clientKind?: string \| null; live: boolean; mine?: boolean; } \| null` | no | Who owns the session's approvals after this resume (1.26.0). |
| `provider` | `string \| null` | no | Chat provider vendor in use. |
| `sessionId` | `string` | yes | Session that was resumed. |

### `session.setActive`

*Direction:* client → server

Mark the session the user is looking at as the active one: a messenger with gateway.<platform>.shareActive posts its questions and approvals to the owner's chat. session.prompt and session.steer from a client do the same. Subagent and scheduled sessions are refused with 'invalid_params'.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Target session. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `session.setAgent`

*Direction:* client → server

Run a session as an agent definition, or as none (addendum 17). Only the session's origin connection, a browser session's hostToolsFrom host, or a non-browser surface (for a session it originated or that is not a browser session) may call it; others get 'unauthorized'.

**Params**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no | Agent definition to run as from the next model call: its prompt (persona layer), tools allowlist ('*' = all) and max_tool_rounds. null clears it. |
| `guardTurn` | `boolean` | no | Guard the running turn (1.24.0): from now on every call that is not a read (read-only web tools included) and every mcp__ call is asked with forceAsk semantics — plan still denies; no mode, allowlist or cached answer allows it; approval.ask answers come back as 'once'. It covers sessions delegated from this one (also ones started later), a steer, and turns the agent queues for itself, and ends when the person's own next prompt starts a turn. No turn running: no effect. |
| `sessionId` | `string` | yes | Session to switch. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no | Agent now in force. |
| `guarded` | `boolean` | no | True when a running turn is now guarded (guardTurn). |
| `sessionId` | `string` | yes | The session. |

### `session.setBrowserProvider`

*Direction:* client → server

Per-session opt-in to core's own browser; never global (addendum 9).

**Params**

| field | type | required | description |
|---|---|---|---|
| `provider` | `"host" \| "local"` | yes | 'host': only the Snowpea browser (the default for browser sessions; with no browser attached, browser tools fail with host_unavailable). 'local': the user explicitly allows core's own browser for this session only. |
| `sessionId` | `string` | yes | Session whose browser tools to point (addendum 9). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `browserProvider` | `"host" \| "local"` | yes | What it uses now. |
| `sessionId` | `string` | yes | The session. |

### `session.setEffort`

*Direction:* client → server

Pin how hard a session's model may think, or clear the pin.

**Params**

| field | type | required | description |
|---|---|---|---|
| `effort` | `"low" \| "medium" \| "high" \| "max" \| null` | no | One of 'low', 'medium', 'high', 'max'. Null clears the pin and lets agent.effortBy / agent.effort decide again. |
| `sessionId` | `string` | yes | Session to pin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `effort` | `"low" \| "medium" \| "high" \| "max" \| null` | no | Effective reasoning effort after the change, clamped to what the model offers; null when the model has no effort setting. |
| `effortSource` | `"session" \| "model" \| "vendor" \| "default"` | yes | Rule that decided it: the session pin, a model or vendor rule, or the default. |
| `efforts` | `("low" \| "medium" \| "high" \| "max")[] \| null` | no | Tiers the session's model offers; [] means it has no effort setting. |
| `pinned` | `"low" \| "medium" \| "high" \| "max" \| null` | no | The session's own pin; null when it follows the settings. |
| `sessionId` | `string` | yes | Session that was pinned. |

### `session.setMode`

*Direction:* client → server

Switch a session between plan, accept and auto.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | A browser (0.3.0+) sets 'person' when a click raised the mode (1.54.0). |
| `autoAfterPlan` | `boolean \| null` | no | With mode 'plan' (1.32.0): true keeps planning but answers every approval 'allow' (a deny stays a deny), and set_mode then offers auto first; false clears it. Omitted keeps it while staying in plan and clears it when plan mode is entered afresh. Any other mode always clears it. |
| `mode` | `"plan" \| "accept" \| "auto"` | yes | New permission mode. Raising it (plan < accept < auto) needs a person (1.54.0, security review 4 H2): a local person, a remote web login, or a browser for a session it hosts (or opened) with actor 'person'; never a connector. Else jarvis_person_only with details.reason 'mode_raise'. Lowering is free. |
| `sessionId` | `string` | yes | Session to change. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `autoAfterPlan` | `boolean` | no | Whether plan-then-auto is now in force (1.32.0). |
| `mode` | `"plan" \| "accept" \| "auto"` | yes | Mode now in effect. |

### `session.setModel`

*Direction:* client → server

Pin a session to a model profile, or clear the pin.

**Params**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | A models.profiles id, a 'vendor:model' pair, or a bare vendor name. Null or 'inherit' clears the pin and lets the configured routing decide. |
| `sessionId` | `string` | yes | Session to pin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `model` | `string \| null` | no | Model id now in effect. |
| `pinned` | `boolean` | no | False when the pin was cleared. |
| `provider` | `string \| null` | no | Vendor now in effect. |

### `session.steer`

*Direction:* client → server

Inject a user message into a running turn at its next tool round; never a slash command (steer_slash_command, 1.50.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string` | yes | Session with a running turn. |
| `text` | `string` | yes | User message injected at the running turn's next tool-round boundary; with no turn running it starts one like session.prompt (1.6.0). Text that starts with '/' once trimmed is refused with steer_slash_command (1.50.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when accepted. |
| `started` | `boolean` | no | True when no turn was running and a new one started. |

### `session.toolContent`

*Direction:* client → server

A host tool result's content blocks with image bytes; results with images are kept on disk.

**Params**

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | tool.result callId whose content to fetch. |
| `sessionId` | `string` | yes | Session the tool ran in. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | The call. |
| `content` | `({ contentRef?: string \| null; data?: string \| null; ephemeral?: boolean \| null; mediaType?: string \| null; text?: string \| null; type: "text" \| "image"; })[]` | no | The content blocks, images with their bytes. |

### `session.trajectory`

*Direction:* client → server

The session's event log as turn-aware rows: turns, model calls with timing and usage, tool calls with results, delegations, compactions (1.45.0, capability 'trajectory').

**Params**

| field | type | required | description |
|---|---|---|---|
| `afterSeq` | `number` | no | Only rows that started or finished after this seq; pass the last lastSeq to get what changed. Rows are keyed by seq: replace a row you have with a newer one. |
| `limit` | `number` | no | At most this many rows, the newest first kept. |
| `sessionId` | `string` | yes | Session to read (open or closed). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `lastSeq` | `number` | no | Highest event seq folded; the next afterSeq. |
| `more` | `boolean` | no | True when older rows were left out by limit. |
| `rows` | `({ data?: Record<string, unknown> \| null; endSeq?: number \| null; endTs?: string \| null; kind: "turn" \| "user" \| "message" \| "tool" \| "subagent" \| "compacted" \| "context" \| "system"; ok?: boolean \| null; seq: number; step?: number \| null; text?: string \| null; title?: string; ts: string; turnId?: string \| null; })[]` | no | Rows in seq order. |

### `settings.get`

*Direction:* client → server

Read global or project settings, with secrets masked.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"global" \| "project"` | no | "global" reads $SNOWPEA_HOME/settings.json; "project" reads <workdir>/.snowpea/settings.json. |
| `workdir` | `string \| null` | no | Project root; required when scope is "project". |

**Result**

| field | type | required | description |
|---|---|---|---|
| `settings` | `Record<string, unknown>` | yes | The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. |

### `settings.set`

*Direction:* client → server

Deep-merge a patch into global or project settings and persist it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Set to 'person' by a browser host (clientKind 'browser') only for a call its own Settings page made under a user gesture (1.53.0). Without it a browser host's settings.set may change only its runtime keys (browser.siteMemory, browser.agentsMdUpdate) and provider.configure / mcp.add\|update\|remove are refused (jarvis_person_only). Ignored for every other connection kind. |
| `patch` | `Record<string, unknown>` | yes | Fields to deep-merge into the existing settings. |
| `scope` | `"global" \| "project"` | no | "global" writes $SNOWPEA_HOME/settings.json; "project" writes <workdir>/.snowpea/settings.json. |
| `workdir` | `string \| null` | no | Project root; required when scope is "project". |

**Result**

| field | type | required | description |
|---|---|---|---|
| `settings` | `Record<string, unknown>` | yes | The effective settings document. Fields named api_key, token, refresh_token or password are masked as '***'. |

### `setup.applyDefaults`

*Direction:* client → server

Apply the profile's defaults for everything optional; idempotent.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `profile` | `"default" \| "browser"` | no | Which defaults to apply. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `applied` | `string[]` | no | Setting keys this call filled in because they were unset (absent or null). |
| `skipped` | `string[]` | no | Profile keys left alone because settings.json already holds a value. |
| `status` | `{ configuredProviders?: string[]; existingInstall?: boolean; optional?: ({ defaultApplied?: boolean \| null; done: boolean; id: string; title: string; })[]; required?: ({ defaultApplied?: boolean \| null; done: boolean; id: string; title: string; })[]; }` | yes | setup.status after applying. |

### `setup.catalog`

*Direction:* client → server

The setup wizard's vendor, search, browser, tools and gateway catalogs.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `browser` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Browser-control providers. |
| `gateway` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Chat gateways (telegram, discord, slack), all off. |
| `search` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Web-search providers, ddgs first. |
| `stt` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Speech-to-text choices; active reflects what is usable on this machine. |
| `tools` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Tool categories and their default on/off state. |
| `tts` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | Text-to-speech choices; active reflects what is usable on this machine. |
| `vendors` | `({ active?: boolean; default?: boolean; defaultModel?: string; defaultModelSource?: string; description?: string; id: string; installHint?: string \| null; installable?: boolean; key: string; label: string; recommended?: boolean; tags?: string[]; tier: string; })[]` | no | LLM vendors. |

### `setup.status`

*Direction:* client → server

What setup still needs; for the browser profile only a tested provider is required.

**Params**

| field | type | required | description |
|---|---|---|---|
| `profile` | `"default" \| "browser"` | no | Which setup flow is asking (1.6.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `configuredProviders` | `string[]` | no | Vendors with credentials or a local endpoint. |
| `existingInstall` | `boolean` | no | True when this home already has a configured provider from an earlier setup. |
| `optional` | `({ defaultApplied?: boolean \| null; done: boolean; id: string; title: string; })[]` | no | Items with a usable default. |
| `required` | `({ defaultApplied?: boolean \| null; done: boolean; id: string; title: string; })[]` | no | Items that must be done before first use. |

### `site.delete`

*Direction:* client → server

Site memory (1.9.0): delete one entry, or every entry of an origin, in one browser profile. Same callers as site.list.

**Params**

| field | type | required | description |
|---|---|---|---|
| `entryId` | `string \| null` | no | One entry; omitted = all of them. |
| `hostToolsFrom` | `string \| null` | no | The browser profile (its clientId). Required from a UI client; a browser client may only name its own. |
| `origin` | `string` | yes | Origin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `deleted` | `number` | yes | Entries removed. |

### `site.get`

*Direction:* client → server

Site memory (1.9.0): the entries stored for an origin in the caller's browser profile; with path, only those whose urlPattern matches it, most specific first; with pageType, only that one. Stale entries are included, flagged. Browser host clients only; entries are untrusted page-derived data and core never puts them in a prompt.

**Params**

| field | type | required | description |
|---|---|---|---|
| `origin` | `string` | yes | Origin, e.g. 'https://www.example.com'. |
| `pageType` | `string \| null` | no | Only this page type. |
| `path` | `string \| null` | no | Path (and query) on the origin: only entries whose urlPattern matches. |
| `sessionId` | `string \| null` | no | The session the call is made for (1.27.0); its siteMemory override from session.create applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entries` | `({ actions?: ({ frames?: ({ origin: string; path: string; selector: string; })[] \| null; kind: "click" \| "fill" \| "select" \| "toggle" \| "submit"; locators: ({ by: "role" \| "label" \| "text" \| "placeholder" \| "testid" \| "css"; name?: string \| null; role?: string \| null; value?: string \| null; })[]; loginField?: boolean; missCount?: number; name: string; stale?: boolean; })[]; createdAt: string; entryId: string; failureCount?: number; fingerprint?: string \| null; flows?: ({ expect?: "page" \| "update" \| null; name: string; stats?: { lastRun?: string \| null; missed?: number; ok?: number; } \| null; steps?: (string \| { action?: string \| null; intent?: string \| null; verb: "fill" \| "click" \| "select" \| "enter"; })[]; })[]; landmarks?: string[]; lastVerified: string; origin: string; packVersion?: string \| null; pageType: string; pitfalls?: string[]; recipes?: ({ fields: ({ attr?: string \| null; name: string; selector: string; })[]; intent?: string \| null; item: string; name: string; stale?: boolean; stats?: { day?: number \| null; hits?: number; misses?: number; } \| null; })[]; source?: "learned" \| "pack" \| "shared"; stale?: boolean; successCount?: number; summary?: string; urlPattern: string; })[]` | no | Most specific urlPattern first; stale entries included, flagged. |

### `site.list`

*Direction:* client → server

Site memory (1.9.0): one browser profile's remembered sites, by origin, for a settings UI. A browser client sees its own profile; any other owner client names one with hostToolsFrom.

**Params**

| field | type | required | description |
|---|---|---|---|
| `cursor` | `string \| null` | no | From the previous page. |
| `hostToolsFrom` | `string \| null` | no | The browser profile (its clientId). Required from a UI client; a browser client may only name its own. |
| `limit` | `number` | no | Most sites per page. |
| `origin` | `string \| null` | no | Only this origin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `cursor` | `string \| null` | no | Pass back for the next page. |
| `sites` | `({ entries: number; failureCount?: number; lastVerified: string; origin: string; packEntries?: number; pageTypes?: string[]; playbooks?: number; stale: number; successCount?: number; })[]` | no | Sites, by origin. |

### `site.mark`

*Direction:* client → server

Site memory (1.9.0): 'ok' counts a success, sets lastVerified and clears stale; 'stale' counts a failure and flags the entry until the next put. Browser host clients only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `detail` | `string \| null` | no | For the UI and log only. |
| `entryId` | `string` | yes | The entry. |
| `origin` | `string` | yes | Origin of the entry. |
| `outcome` | `"ok" \| "stale"` | yes | 'ok' = verified use; 'stale' = it failed. |
| `sessionId` | `string \| null` | no | The session the call is made for (1.27.0); its siteMemory override from session.create applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entry` | `{ actions?: ({ frames?: ({ origin: string; path: string; selector: string; })[] \| null; kind: "click" \| "fill" \| "select" \| "toggle" \| "submit"; locators: ({ by: "role" \| "label" \| "text" \| "placeholder" \| "testid" \| "css"; name?: string \| null; role?: string \| null; value?: string \| null; })[]; loginField?: boolean; missCount?: number; name: string; stale?: boolean; })[]; createdAt: string; entryId: string; failureCount?: number; fingerprint?: string \| null; flows?: ({ expect?: "page" \| "update" \| null; name: string; stats?: { lastRun?: string \| null; missed?: number; ok?: number; } \| null; steps?: (string \| { action?: string \| null; intent?: string \| null; verb: "fill" \| "click" \| "select" \| "enter"; })[]; })[]; landmarks?: string[]; lastVerified: string; origin: string; packVersion?: string \| null; pageType: string; pitfalls?: string[]; recipes?: ({ fields: ({ attr?: string \| null; name: string; selector: string; })[]; intent?: string \| null; item: string; name: string; stale?: boolean; stats?: { day?: number \| null; hits?: number; misses?: number; } \| null; })[]; source?: "learned" \| "pack" \| "shared"; stale?: boolean; successCount?: number; summary?: string; urlPattern: string; }` | yes | The entry after the mark. |

### `site.markPart`

*Direction:* client → server

Site memory (1.15.0): count one use of a flow or recipe of an entry without a put. The entry's successCount, failureCount, stale and lastVerified are untouched, so this never counts as verification. 'not_found' for an unknown entry, flow or recipe. Browser host clients only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `entryId` | `string` | yes | The entry. |
| `name` | `string` | yes | The flow's, recipe's or action's name. |
| `origin` | `string` | yes | Origin of the entry. |
| `outcome` | `"ok" \| "miss" \| "stale"` | yes | flow: ok (stats.ok+1, lastRun) \| miss (stats.missed+1). recipe: ok (hits+1, day, not stale) \| miss (misses+1) \| stale (misses+1, stale). action: ok (not stale) \| miss (missCount+1) \| stale (stale). |
| `part` | `"flow" \| "recipe" \| "action"` | yes | Which list the part is in; "action" from 1.27.0. |
| `sessionId` | `string \| null` | no | The session the call is made for (1.27.0); its siteMemory override from session.create applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `entry` | `{ actions?: ({ frames?: ({ origin: string; path: string; selector: string; })[] \| null; kind: "click" \| "fill" \| "select" \| "toggle" \| "submit"; locators: ({ by: "role" \| "label" \| "text" \| "placeholder" \| "testid" \| "css"; name?: string \| null; role?: string \| null; value?: string \| null; })[]; loginField?: boolean; missCount?: number; name: string; stale?: boolean; })[]; createdAt: string; entryId: string; failureCount?: number; fingerprint?: string \| null; flows?: ({ expect?: "page" \| "update" \| null; name: string; stats?: { lastRun?: string \| null; missed?: number; ok?: number; } \| null; steps?: (string \| { action?: string \| null; intent?: string \| null; verb: "fill" \| "click" \| "select" \| "enter"; })[]; })[]; landmarks?: string[]; lastVerified: string; origin: string; packVersion?: string \| null; pageType: string; pitfalls?: string[]; recipes?: ({ fields: ({ attr?: string \| null; name: string; selector: string; })[]; intent?: string \| null; item: string; name: string; stale?: boolean; stats?: { day?: number \| null; hits?: number; misses?: number; } \| null; })[]; source?: "learned" \| "pack" \| "shared"; stale?: boolean; successCount?: number; summary?: string; urlPattern: string; }` | yes | The entry after the count. |

### `site.playbook.delete`

*Direction:* client → server

Site playbooks (1.38.0): delete one playbook, or every playbook of an origin. site.delete of a whole origin removes them too.

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The browser profile (its clientId). Required from a UI client. |
| `origin` | `string` | yes |  |
| `playbookId` | `string \| null` | no | One playbook; omitted = all. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `deleted` | `number` | yes |  |

### `site.playbook.edit`

*Direction:* client → server

Site playbooks (1.38.0): one edit the user makes in settings: rename, alwaysAsk, retire, unretire, restore (swap with the previous version), acknowledge, removeSegment, removePitfall. A browser client edits its own profile; any other owner client names one with hostToolsFrom.

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The browser profile (its clientId). Required from a UI client. |
| `index` | `number \| null` | no | removePitfall: its index. |
| `name` | `string \| null` | no | rename: the new name. |
| `op` | `"rename" \| "alwaysAsk" \| "retire" \| "unretire" \| "restore" \| "acknowledge" \| "removeSegment" \| "removePitfall"` | yes |  |
| `origin` | `string` | yes |  |
| `playbookId` | `string` | yes |  |
| `segment` | `string \| null` | no | removeSegment: the segment's name. |
| `value` | `boolean \| null` | no | alwaysAsk: on or off. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `playbook` | `{ acked?: boolean; alwaysAsk?: boolean; createdAt: string; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; id: string; intents?: string[]; mac?: string \| null; name: string; origin: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; prev?: { acked?: boolean \| null; alwaysAsk?: boolean \| null; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; intents?: string[]; mac?: string \| null; name: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; verifiedAt?: string \| null; } \| null; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; updatedAt: string; verifiedAt?: string \| null; version: number; }` | yes | A stored playbook: the input, plus what core owns. |

### `site.playbook.get`

*Direction:* client → server

Site playbooks (1.38.0): the typed task procedures stored for an origin, by name. A browser client reads its own profile; any other owner client names one with hostToolsFrom. Untrusted page-derived data; core never puts it in a prompt. Capability sitePlaybooks.

**Params**

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The browser profile (its clientId). Required from a UI client. |
| `name` | `string \| null` | no | Only this playbook. |
| `origin` | `string` | yes |  |
| `sessionId` | `string \| null` | no | The session the call is made for; its siteMemory override applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `playbooks` | `({ acked?: boolean; alwaysAsk?: boolean; createdAt: string; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; id: string; intents?: string[]; mac?: string \| null; name: string; origin: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; prev?: { acked?: boolean \| null; alwaysAsk?: boolean \| null; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; intents?: string[]; mac?: string \| null; name: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; verifiedAt?: string \| null; } \| null; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; updatedAt: string; verifiedAt?: string \| null; version: number; })[]` | no |  |

### `site.playbook.mark`

*Direction:* client → server

Site playbooks (1.38.0): append one run (day, result, segments done of total, rounds) to the history, keeping the last 10; an 'ok' run sets verifiedAt. Counts for site.stats. Browser host clients only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `done` | `number` | yes | Segments done. |
| `origin` | `string` | yes |  |
| `outcome` | `"ok" \| "partial" \| "failed"` | yes |  |
| `playbookId` | `string` | yes |  |
| `rounds` | `number \| null` | no |  |
| `sessionId` | `string \| null` | no |  |
| `total` | `number` | yes | Segments in the playbook. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `playbook` | `{ acked?: boolean; alwaysAsk?: boolean; createdAt: string; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; id: string; intents?: string[]; mac?: string \| null; name: string; origin: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; prev?: { acked?: boolean \| null; alwaysAsk?: boolean \| null; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; intents?: string[]; mac?: string \| null; name: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; verifiedAt?: string \| null; } \| null; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; updatedAt: string; verifiedAt?: string \| null; version: number; }` | yes | A stored playbook: the input, plus what core owns. |

### `site.playbook.put`

*Direction:* client → server

Site playbooks (1.38.0): upsert a playbook by (origin, name). Core refuses strings that look like personal data or credentials, a step that names a login field, a slot or a step the schema cannot express, and 16 KB per playbook or 10 per origin ('invalid_params' naming the field). Core owns id, version and prev: a change of slots or segments keeps the stored body one deep as prev and counts the version up. alwaysAsk, acked and an empty history keep their stored values. Browser host clients only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `origin` | `string` | yes |  |
| `playbook` | `{ acked?: boolean \| null; alwaysAsk?: boolean \| null; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; intents?: string[]; mac?: string \| null; name: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; verifiedAt?: string \| null; }` | yes | Upserted by (origin, name). |
| `sessionId` | `string \| null` | no |  |

**Result**

| field | type | required | description |
|---|---|---|---|
| `created` | `boolean` | yes |  |
| `playbook` | `{ acked?: boolean; alwaysAsk?: boolean; createdAt: string; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; id: string; intents?: string[]; mac?: string \| null; name: string; origin: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; prev?: { acked?: boolean \| null; alwaysAsk?: boolean \| null; goal: string; history?: ({ day: string; done: number; result: "ok" \| "partial" \| "failed"; rounds?: number \| null; total: number; })[]; intents?: string[]; mac?: string \| null; name: string; pitfalls?: ({ contradicted?: number; lastVerified?: string \| null; seen?: number; source?: "host" \| "agent" \| "user" \| null; text: string; })[]; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; verifiedAt?: string \| null; } \| null; segments?: ({ checks?: ({ action?: string \| null; atLeast?: string \| null; kind: string; measure?: string[]; slot?: string \| null; })[]; name: string; page?: string \| null; steps?: ({ action?: string \| null; ask?: boolean; consequential?: boolean \| string; do: string; mode?: string \| null; optional?: boolean; slot?: string \| null; url?: string \| null; via?: string \| null; })[]; })[]; skills?: string[]; slots?: ({ kind: "text" \| "html" \| "files" \| "choice" \| "date" \| "lines"; name: string; optional?: boolean; })[]; start?: string \| null; state?: "candidate" \| "verified" \| "trusted" \| "healing" \| "retired"; updatedAt: string; verifiedAt?: string \| null; version: number; }` | yes | As stored: id, version and prev are core's. |

### `site.put`

*Direction:* client → server

Site memory (1.9.0): upsert an entry by (origin, pageType). Core checks the schema, refuses XPath, values on login fields and strings that look like personal data or credentials, and enforces 24 KB per entry (16 KB before 1.15.0) and 30 entries per origin ('invalid_params' naming the field); past 1,000 origins the least recently verified origin is dropped. A put clears stale and counts as a success. Browser host clients only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `entry` | `{ actions?: ({ frames?: ({ origin: string; path: string; selector: string; })[] \| null; kind: "click" \| "fill" \| "select" \| "toggle" \| "submit"; locators: ({ by: "role" \| "label" \| "text" \| "placeholder" \| "testid" \| "css"; name?: string \| null; role?: string \| null; value?: string \| null; })[]; loginField?: boolean; missCount?: number; name: string; stale?: boolean; })[]; fingerprint?: string \| null; flows?: ({ expect?: "page" \| "update" \| null; name: string; stats?: { lastRun?: string \| null; missed?: number; ok?: number; } \| null; steps?: (string \| { action?: string \| null; intent?: string \| null; verb: "fill" \| "click" \| "select" \| "enter"; })[]; })[]; landmarks?: string[]; lastVerified?: string \| null; pageType: string; pitfalls?: string[]; recipes?: ({ fields: ({ attr?: string \| null; name: string; selector: string; })[]; intent?: string \| null; item: string; name: string; stale?: boolean; stats?: { day?: number \| null; hits?: number; misses?: number; } \| null; })[]; summary?: string; urlPattern: string; }` | yes | The entry; upserted by (origin, pageType). |
| `origin` | `string` | yes | Origin the entry belongs to. |
| `sessionId` | `string \| null` | no | The session the call is made for (1.27.0); its siteMemory override from session.create applies. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `created` | `boolean` | yes | True when no entry for (origin, pageType) existed. |
| `entryId` | `string` | yes | The entry's id. |

### `site.stats`

*Direction:* client → server

Site memory (1.27.0): per-origin counts of the profile's site.put, site.mark and site.markPart calls over the last days. Browser clients only, for their own profile; nothing page-derived is in it.

**Params**

| field | type | required | description |
|---|---|---|---|
| `days` | `number` | no | Days back, today included. |
| `origin` | `string \| null` | no | Only this origin. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `origins` | `({ locateMiss?: number; locateOk?: number; origin: string; playbookMiss?: number; playbookOk?: number; playbookPuts?: number; puts?: number; readMiss?: number; readOk?: number; runMiss?: number; runOk?: number; staleMarks?: number; })[]` | no | Origins with any count in the window, by origin. |

### `skill.create`

*Direction:* client → server

Write a new SKILL.md, generated from a brief or supplied verbatim.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `content` | `string \| null` | no | A complete SKILL.md body. When given, it is validated and written directly — no model turn runs. |
| `description` | `string \| null` | no | Natural-language brief. Used only when 'content' is omitted: the daemon starts the same generating turn '/skill create' runs and answers with a turnId rather than waiting for it. |
| `force` | `boolean` | no | Overwrite an existing SKILL.md at the target; without it an existing one answers skill_exists (1.33.0), in draft mode before any turn starts. |
| `name` | `string` | yes | Skill name; also its directory and the future /<name>. |
| `scope` | `"project" \| "global"` | no | Where to write the skill. |
| `sessionId` | `string \| null` | no | Draft mode only ('content' omitted): run the generating turn on this existing session instead of creating a headless one. Must be rooted at 'workdir'; invalid_params otherwise. Ignored when 'content' is given. |
| `workdir` | `string` | yes | Project directory the skill is written under (or read/create a session from). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string \| null` | no | Skill name, once known. |
| `path` | `string \| null` | no | Where the SKILL.md was written. |
| `sessionId` | `string \| null` | no | Set alongside turnId: the session the turn ran on — the caller's own 'sessionId', or a new headless one created for 'workdir'. |
| `turnId` | `string \| null` | no | Set instead of name/path when generation was started as a turn. |

### `skill.install`

*Direction:* client → server

Install a skill from a path, URL or registry.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `link` | `boolean` | no | Local plugin directory only: install it as a symlink instead of a copy, so edits are live after skill.reload (development). skill.remove drops the link and leaves the directory alone. |
| `source` | `string` | yes | Path, URL or registry name to install from. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.learn`

*Direction:* client → server

Turn a session's history into a SKILL.md, as /skill learn does, in the project or the user's skills (1.28.0). Answers when it is written and loaded.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `force` | `boolean` | no | Overwrite a SKILL.md already at the target; otherwise that is refused. |
| `name` | `string \| null` | no | Skill name; omit to let the model name it. |
| `scope` | `"project" \| "global"` | no | "global" writes it to the user's skills under the snowpea home, so every later session loads it; "project" under the session workdir's .snowpea/skills. |
| `sessionId` | `string` | yes | The session whose history becomes the skill. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `name` | `string` | yes | The skill's name, also its /<name> command. |
| `path` | `string` | yes | Where the SKILL.md was written. |

### `skill.list`

*Direction:* client → server

List installed skills.

**Params**

| field | type | required | description |
|---|---|---|---|
| `workdir` | `string \| null` | no | List this project's skills, agents and commands beside the global, built-in and plugin ones (1.33.0). Omitted, the project rows are those of every project the daemon has loaded. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `skills` | `({ downloads?: number; enabled?: boolean; format?: "snowpea" \| "claude" \| "codex"; id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; note?: string; rating?: number; source?: string; summary?: string; title?: string; })[]` | no | Installed skills. |

### `skill.read`

*Direction:* client → server

Read a skill's SKILL.md.

**Params**

| field | type | required | description |
|---|---|---|---|
| `kind` | `"skill" \| "agent" \| "command" \| null` | no | Read an agent or a command instead of a skill (1.36.0): the file is agents/<name>.md or commands/<name>.md in the same places a skill's directory would be. Omitted or 'skill', the SKILL.md as before. |
| `name` | `string` | yes | Skill to read. |
| `plugin` | `string \| null` | no | Read this plugin's copy, by its manifest or directory name, when several plugins ship the same skill (1.33.0). Implies scope 'plugin'; any other scope with it is invalid_params. |
| `scope` | `"project" \| "global" \| "builtin" \| "plugin" \| null` | no | Look only in this scope (1.33.0); 'project' needs workdir. Omitted: the project (with a workdir), then global, then any other loaded skill. |
| `workdir` | `string \| null` | no | Absolute project directory to resolve a project-scoped skill in; optional since 1.33.0, an empty string means none (never the daemon's directory) and a relative one is invalid_params. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `content` | `string` | yes | Its full text. |
| `format` | `"snowpea" \| "claude" \| "codex"` | no | The layout it was written for (1.52.0): snowpea, claude (Claude Code format) or codex (Codex format). |
| `path` | `string` | yes | Where the SKILL.md was found. |
| `readOnly` | `boolean` | no | True when skill.write cannot save this file (1.33.0): a built-in or plugin skill, or one under Claude Code's or (1.52.0) Codex's own directories. |
| `scope` | `"project" \| "global" \| "builtin" \| "plugin"` | yes | 'project' or 'global', or (1.33.0) 'builtin' for a skill that ships with the engine and 'plugin' for one a plugin brings. |

### `skill.reload`

*Direction:* client → server

Reload skills from disk without restarting.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | 'person' when a browser host (clientKind 'browser', clientVersion 0.3.0 or later; capability 'browserActor') makes the call from its own Settings page under a user gesture. Required from such a browser on the marker methods (jarvis_person_only otherwise); accepted and ignored from every other connection. Any other value is invalid_params. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.remove`

*Direction:* client → server

Delete a plugin, or a global or project skill, agent or command (1.33.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `kind` | `"skill" \| "agent" \| "command" \| "plugin" \| null` | no | Only this kind of entry (1.33.0): a skill directory, an agent or command .md file, or a plugin. |
| `name` | `string` | yes | Entry to delete: a skill, agent or command name, or a plugin's directory or manifest name as skill.list shows it (1.33.0). |
| `scope` | `"project" \| "global" \| "plugin" \| null` | no | Look only here (1.33.0): 'plugin' ($SNOWPEA_HOME/plugins), 'global' ($SNOWPEA_HOME skills/, agents/, commands/) or 'project' (the workdir's .snowpea, then .claude). Omitted, the copy in effect goes: the project (with a workdir), then global, then plugins, so a skill wins over a plugin of the same name. Within a scope a skill, then an agent, then a command. |
| `workdir` | `string \| null` | no | Absolute project directory; needed for scope 'project', and with no scope it puts the project's copies first (1.33.0). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `skill.search`

*Direction:* client → server

Search available skills.

**Params**

| field | type | required | description |
|---|---|---|---|
| `query` | `string` | yes | Free-text query over skill names and summaries. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `notIncluded` | `({ label: string; reason?: string; })[]` | no | Hubs the registry has deliberately switched off (not failures). Clients should mention them quietly, never as an outage. |
| `skills` | `({ downloads?: number; enabled?: boolean; format?: "snowpea" \| "claude" \| "codex"; id?: string; installSpec?: string; installed?: boolean; kind?: "skill" \| "agent" \| "command" \| "plugin"; name: string; note?: string; rating?: number; source?: string; summary?: string; title?: string; })[]` | no | Matching skills. |
| `unavailable` | `string[]` | no | Sources that could not be reached, as '<source>: <reason>'. Empty skills with a non-empty list means offline, not no match. |

### `skill.write`

*Direction:* client → server

Save a skill's SKILL.md verbatim; says where, and whether it is in effect.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `content` | `string` | yes | Full SKILL.md text to save. |
| `kind` | `"skill" \| "agent" \| "command" \| null` | no | Write an agent or a command instead of a skill (1.36.0): the file is agents/<name>.md or commands/<name>.md in the same places a skill's directory would be. Omitted or 'skill', the SKILL.md as before. |
| `name` | `string` | yes | Skill to write. |
| `scope` | `"project" \| "global"` | no | Where to write the skill. |
| `workdir` | `string \| null` | no | Project directory, when scope is 'project'; optional for 'global' (1.33.0), where it decides which project's skills may shadow the written copy (active). An empty string is no directory, never the daemon's; a relative one is refused. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `active` | `boolean` | yes | Whether that copy is the one in effect (1.33.0): false when a project skill or a Claude Code global skill of the same name shadows it. |
| `ok` | `boolean` | no | True when the file was written. |
| `path` | `string` | yes | Where the SKILL.md was written (1.33.0). |

### `system.checkUpdate`

*Direction:* client → server

Report whether a newer snowpea release exists; cached for 24h.

**Params**

| field | type | required | description |
|---|---|---|---|
| `force` | `boolean` | no | Ignore the 24h cache and ask the network right now. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `available` | `boolean` | yes | True when latest is strictly newer than current. |
| `cached` | `boolean` | no | True when this came from $SNOWPEA_HOME/update-check.json. |
| `channel` | `"git" \| "pypi"` | yes | Where the answer came from: 'pypi' or 'git'. |
| `checkedAt` | `string` | yes | UTC ISO-8601 timestamp of the answer. |
| `current` | `string` | yes | Version of the running daemon (snowpea_core.__version__). |
| `error` | `string \| null` | no | Why the check could not complete; available is false whenever it is set. |
| `latest` | `string` | yes | Newest version found; equal to current when nothing is known. |
| `releaseUrl` | `string \| null` | no | Human page for the release, when one exists. |
| `source` | `string` | yes | What an installer would be handed to get 'latest'. |

### `system.health`

*Direction:* client → server

Liveness probe; answers as long as the daemon serves requests.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `status` | `"ok"` | no | Always 'ok' when the daemon answers. |

### `system.hello`

*Direction:* client → server

Authenticate a connection and agree on the protocol version.

**Params**

| field | type | required | description |
|---|---|---|---|
| `clientId` | `string \| null` | no | Id stable across restarts of this client install (1.6.0). Sessions a previous connection with the same clientId started re-bind to this one. |
| `clientKind` | `string \| null` | no | What the client is, e.g. "tui", "desktop", "browser" (1.6.0). |
| `clientVersion` | `string` | yes | Version string of the connecting client. |
| `holdSec` | `number \| null` | no | A connector-held client (1.40.0, capability 'holdSec'): once nothing but loaded, idle sessions is left, the daemon exits this many seconds after the last connection goes instead of after daemon.idleTimeoutSec. A running turn, job, gateway binding, named agent or keep-alive client still keeps it up. Clamped to 30..3600; a later hello from a tui/desktop/ide client without it restores the ordinary timeout. |
| `instanceId` | `string \| null` | no | Id of this run of the client (1.6.0). |
| `keepAlive` | `boolean` | no | Keep the daemon from its idle shutdown while this client is connected (1.6.0). |
| `protocolVersion` | `string` | yes | Protocol semver the client speaks; major must match. |
| `token` | `string` | yes | Shared secret read from $SNOWPEA_HOME/token or daemon.json. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `capabilities` | `string[]` | no | Feature flags this daemon supports. |
| `machine` | `{ container: boolean; hostname: string; id: string; os: "linux" \| "darwin" \| "windows"; } \| null` | no | The machine this daemon runs on (1.40.0); absent from an older daemon. |
| `modelCategories` | `("fast" \| "standard" \| "deep" \| "vision")[]` | no | The models.categories slots this daemon routes by (1.33.0, capability 'modelCategories'); empty from an older daemon. |
| `protocolVersion` | `string` | yes | Protocol semver the daemon speaks. |
| `serverVersion` | `string` | yes | Version of the running daemon. |

### `system.info`

*Direction:* client → server

Report the daemon's version, pid, port, start time and home.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `clients` | `({ clientId?: string \| null; clientKind?: string \| null; clientVersion?: string \| null; you?: boolean; })[]` | no | Every authenticated connection, the caller included (1.26.0): an app that would restart a shared daemon checks that no other client is using it. |
| `counters` | `Record<string, number>` | no | Lifecycle counters: sessions, jobs, gateway_bindings, named_agents. |
| `home` | `string` | yes | Resolved SNOWPEA_HOME directory. |
| `lifecycle` | `{ reason?: string; reasons?: string[]; secondsUntilExit?: number \| null; summary?: string \| null; willExit?: boolean; } \| null` | no | Idle-shutdown status, omitted by older daemons. |
| `pid` | `number` | yes | Process id of the daemon. |
| `platform` | `string \| null` | no | The daemon machine's sys.platform (darwin, linux, win32), for engine and permission guidance that depends on where the core runs. |
| `port` | `number` | yes | TCP port the daemon is listening on (127.0.0.1 only). |
| `protocolVersion` | `string` | yes | Protocol semver the daemon speaks. |
| `restartRequired` | `boolean` | no | True once system.update finished; the daemon runs the old code until restarted. |
| `startedAt` | `string` | yes | UTC ISO-8601 timestamp of daemon start. |
| `version` | `string` | yes | Daemon version. |

### `system.reloadSettings`

*Direction:* client → server

Re-read settings.json and rebind the daemon's in-memory state.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | 'person' when a browser host (clientKind 'browser', clientVersion 0.3.0 or later; capability 'browserActor') makes the call from its own Settings page under a user gesture. Required from such a browser on the marker methods (jarvis_person_only otherwise); accepted and ignored from every other connection. Any other value is invalid_params. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `changedKeys` | `string[]` | no | Top-level settings sections that changed, e.g. ['providers']. |
| `reloaded` | `boolean` | yes | True when settings.json differed from what the daemon held and the in-memory state was rebound; false when it was already current. |

### `system.restart`

*Direction:* client → server

Shut the daemon down so the next launch runs the newly installed version.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `system.shutdown`

*Direction:* client → server

Ask the daemon to shut down gracefully.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `system.update`

*Direction:* client → server

Upgrade snowpea in a detached subprocess and report progress.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `command` | `string` | yes | The command line that runs, or that has to be run by hand. |
| `error` | `string \| null` | no | Why nothing was started; null on the happy path. |
| `log` | `string` | yes | Absolute path of the file the upgrade writes its output to. |
| `selfRestart` | `boolean` | no | True when the daemon stops and comes back by itself on the same port (Windows, uv-installed core): do not restart or spawn a core, wait for daemon.json to drop 'restarting' and name a healthy daemon (1.26.0). |
| `started` | `boolean` | yes | True when the upgrade subprocess was spawned. |

### `team.guide.delete`

*Direction:* client → server

Delete a team guide from the project or global home.

**Params**

| field | type | required | description |
|---|---|---|---|
| `scope` | `"project" \| "global"` | yes | Which copy to delete. |
| `sessionId` | `string \| null` | no | Session whose workdir to use when workdir is omitted. |
| `team` | `string` | yes | Team name, or 'default'. |
| `workdir` | `string \| null` | no | Project directory. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `team.guide.get`

*Direction:* client → server

Read a team guide for a project or session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session whose workdir to use when workdir is omitted. |
| `team` | `string \| null` | no | Team to read; omit to use the effective team for this session. |
| `workdir` | `string \| null` | no | Project directory. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `effectiveTeam` | `string` | yes | Team name the guide lookup used. |
| `guide` | `{ description?: string; path?: string \| null; persona?: string; routing?: ({ agent: string; known?: boolean; when: string; })[]; source: "project" \| "global"; team: string; } \| null` | no | The guide, if one exists. |

### `team.guide.list`

*Direction:* client → server

List every team guide visible in a project.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Session whose workdir to use when workdir is omitted. |
| `workdir` | `string \| null` | no | Project directory. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `guides` | `({ description?: string; path?: string \| null; persona?: string; routing?: ({ agent: string; known?: boolean; when: string; })[]; source: "project" \| "global"; team: string; })[]` | no | Every visible guide. |

### `team.guide.set`

*Direction:* client → server

Write a team guide to the project or global home.

**Params**

| field | type | required | description |
|---|---|---|---|
| `actor` | `"person" \| null` | no | Browser marker (security review 3): see ActorParams. |
| `description` | `string \| null` | no | One-line summary. |
| `persona` | `string` | yes | Persona body to store. |
| `routing` | `({ agent: string; when: string; })[]` | no | Routing rules to store. |
| `scope` | `"project" \| "global"` | yes | Where to write the guide. |
| `sessionId` | `string \| null` | no | Session whose workdir to use when workdir is omitted. |
| `team` | `string` | yes | Team name, or 'default' for sessions with no active team. |
| `workdir` | `string \| null` | no | Project directory. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `guide` | `{ description?: string; path?: string \| null; persona?: string; routing?: ({ agent: string; known?: boolean; when: string; })[]; source: "project" \| "global"; team: string; }` | yes | The guide that was written. |

### `team.start`

*Direction:* client → server

Split a task across parallel workers.

**Params**

| field | type | required | description |
|---|---|---|---|
| `n` | `number` | yes | Number of workers to run in parallel. |
| `sessionId` | `string` | yes | Session the team works under. |
| `task` | `string` | yes | Task the team splits between workers. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | yes | Id to poll with team.status. |

### `team.status`

*Direction:* client → server

Inspect a team's task board.

**Params**

| field | type | required | description |
|---|---|---|---|
| `teamId` | `string` | no | Team to inspect; empty means the most recent one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `state` | `"running" \| "done" \| "failed" \| "interrupted"` | no | Overall state. |
| `task` | `string` | no | The task the team was given. |
| `tasks` | `({ agentN?: number \| null; assignee?: string \| null; branch?: string; conflictHunks?: string; conflictSummary?: string; dependsOn?: string[]; note?: string; retries?: number; status?: "pending" \| "queued" \| "claimed" \| "running" \| "done" \| "conflict" \| "merged" \| "failed"; taskId: string; title: string; })[]` | no | Task board contents. |
| `teamId` | `string` | yes | Team that was inspected. |
| `workers` | `number` | no | How many workers the run was started with. |
| `worktrees` | `string[]` | no | Worker worktrees that exist right now. |

### `tool.invoke`

*Direction:* server → client

Ask the client to run one of its host tools.

**Params**

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments from the model. |
| `attachments` | `({ byPath?: boolean; messageId?: string \| null; mime: string; name: string; path: string; size: number; })[]` | no | Files the user attached in the root conversation (1.23.0), as session.attachments lists them: the paths a host may hand to a page upload. |
| `callId` | `string` | yes | Id of the tool call; tool.progress refers to it. |
| `guard` | `string \| null` | no | 'agent-switch' while session.setAgent {guardTurn} guards this session or one it was delegated from (1.25.0): a host asks before its own acting tools, whatever tag they were registered with. |
| `job` | `{ createdVia: "rpc" \| "tool"; fingerprint: string; id: string; meta?: Record<string, unknown> \| null; ownerClientId?: string \| null; runId: string; scheduledAt: string; trigger: "schedule" \| "runNow"; } \| null` | no | The scheduled run behind the root's running turn (1.23.0); set only when origin is 'schedule'. A delegated child reports its root's. |
| `mode` | `"plan" \| "accept" \| "auto"` | no | The session's mode. A code-running tool registered as 'read' must refuse mutations in plan mode itself and escalate the rest with approval.ask. |
| `model` | `{ canSee: boolean; contextWindow?: number \| null; localStyle: boolean; } \| null` | no | 1.55.0: the session's current model, so a host can shape what it returns (a screenshot only for a model that can see, a shorter snapshot for a small window). Absent from older daemons. |
| `name` | `string` | yes | Host tool name. |
| `origin` | `"human" \| "agent" \| "member" \| "schedule" \| "system" \| null` | no | Who started the root conversation's running turn (1.23.0): the session's own person, the agent, a schedule, another gateway group member or the system. A delegated child reports its root's. |
| `parentSessionId` | `string \| null` | no | The session that spawned this one (a subagent's parent); null for a root session. A browser host shares the parent's agent tabs with it. |
| `sessionId` | `string` | yes | Session whose turn called the tool. |
| `turnId` | `string` | no | Turn the call belongs to. |
| `userTurn` | `{ current?: boolean; messageId: string; steers?: string[]; text: string; } \| null` | no | The root conversation's latest message the user typed, trusted (1.22.0); 'current' says whether it started the running turn. For a delegated child it is the root's, never the parent agent's task text; only the session's own person's words count (not an agent's, a schedule's or another group member's), and null until the person has typed one. |
| `workdir` | `string` | no | The session's project folder, absolute (Projects, 1.7.0). |
| `workspaceDir` | `string` | no | The session workspace (tmp/, artifacts/); the workdir when it has none. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `content` | `({ contentRef?: string \| null; data?: string \| null; ephemeral?: boolean \| null; mediaType?: string \| null; text?: string \| null; type: "text" \| "image"; })[] \| null` | no | Rich result blocks. Images reach vision models as image input; text blocks are appended to output. |
| `error` | `string \| null` | no | Error text when ok is false. |
| `meta` | `Record<string, unknown> \| null` | no | Extra facts. meta.sensitive=true keeps the output out of the session store ([redacted]), memory and compaction summaries. meta.kind='snapshot' with meta.tab marks the result as that tab's snapshot (addendum 23); since 1.55.0 this also covers its image blocks, whatever text the result carries, so a newer view of the tab stubs older ones in a long request. meta.helpSection (1.55.0) on a failed result names a section of the tool's manual to append, once per session. |
| `ok` | `boolean` | yes | False reports the call as a tool error. |
| `output` | `string` | no | Text result the model reads. |

### `tool.list`

*Direction:* client → server

List the tools registered for a session.

**Params**

| field | type | required | description |
|---|---|---|---|
| `sessionId` | `string \| null` | no | Scope the listing to one session; omit for the global set. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `tools` | `({ category: string; deferred?: boolean; description?: string; name: string; permissionTag: "read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside"; provider?: string; reason?: string; server?: string; source?: string; state?: "active" \| "inactive"; })[]` | no | Registered tools. |

### `tool.progress`

*Direction:* client → server

Notification: progress of a running tool.invoke, re-emitted as tool.progress.

**Params**

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | The tool.invoke callId this progress belongs to. |
| `message` | `string` | no | Progress text; re-emitted as session.event tool.progress. Any tool.progress, an empty keepalive included, restarts the call's deadline (addendum 15). |
| `note` | `string \| null` | no | 1.55.0: what it waits for, one short line ('waiting for the page'). |
| `state` | `"running" \| "waiting" \| null` | no | 1.55.0: 'waiting' = the call is waiting on something outside it (a page loading, a download); 'running' = it is working again. Forwarded on the session event tool.progress as state, so a tool card can say so. Restarts the deadline like any progress. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `tool.register`

*Direction:* client → server

Register tools this connection runs itself (host tools).

**Params**

| field | type | required | description |
|---|---|---|---|
| `capabilities` | `string[] \| null` | no | What this connection answers besides its tools (1.31.0): 'turnContext' opts in to host.turnContext for the sessions whose host tools it serves (a browser host only); 'turnContextCompaction' (1.37.0) also asks it again after each compaction. A list replaces the connection's earlier one; omitted keeps it. Unknown names are ignored. |
| `tools` | `({ agents?: string[] \| null; category?: string \| null; deferred?: boolean; description: string; descriptionBy?: Record<string, string> \| null; descriptionShort?: string \| null; helpers?: string[] \| null; inputSchema?: Record<string, unknown>; look?: boolean; name: string; parallel?: "safe" \| "never"; permission: "read" \| "write" \| "exec" \| "network" \| "send" \| "config" \| "delegate" \| "secret" \| "outside"; sections?: Record<string, string> \| null; timeoutMs?: number \| null; })[]` | yes | Tools to add or replace for this connection; all-or-nothing. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `registered` | `string[]` | no | Names now registered. |

### `tool.unregister`

*Direction:* client → server

Remove some of this connection's host tools.

**Params**

| field | type | required | description |
|---|---|---|---|
| `names` | `string[]` | yes | This connection's tools to remove. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `removed` | `string[]` | no | Names actually removed. |

### `turn.outcome`

*Direction:* client → server

Record whether a turn did what was asked (1.27.0): a browser client, for its own sessions. Stored as a turn.outcome session event.

**Params**

| field | type | required | description |
|---|---|---|---|
| `by` | `"user" \| "host" \| "judge"` | yes | user, host or judge. |
| `outcome` | `"ok" \| "failed" \| "corrected" \| "abandoned"` | yes | ok \| failed \| corrected \| abandoned. |
| `reason` | `string \| null` | no | Why, at most 200 characters. Refused when it holds what memory.write refuses (a phone number, a card number, a credential, ...). |
| `sessionId` | `string` | yes | The browser client's own session: one it created or whose hostToolsFrom it is; once the session is closed, only its hostToolsFrom. |
| `turnId` | `string` | yes | The turn judged. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `seq` | `number` | yes | The seq of the stored turn.outcome event. |

### `usage.summary`

*Direction:* client → server

Token totals from stored usage events, grouped by provider, model, session or day.

**Params**

| field | type | required | description |
|---|---|---|---|
| `groupBy` | `"provider" \| "model" \| "session" \| "day"` | no | How to group the totals. |
| `since` | `string \| null` | no | UTC ISO start (inclusive). |
| `until` | `string \| null` | no | UTC ISO end (exclusive). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `groupBy` | `string` | yes | The grouping used. |
| `inputTokens` | `number` | no | Sum over all rows. |
| `outputTokens` | `number` | no | Sum over all rows. |
| `rows` | `({ calls?: number; inputTokens?: number; key: string; outputTokens?: number; })[]` | no | Totals, largest first. |

### `web.remote.clearPassword`

*Direction:* client → server

Remote web access (1.54.0): remove the password (back to the access link); every open session ends.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The access link with its token (token mode, listening). Treat it like a password: anyone with it drives this machine; rotate it with web.remote.rotateToken. |
| `enabled` | `boolean` | yes | web.remote.enabled: the listener opens at daemon start. |
| `error` | `string \| null` | no | Why it is not listening, if it should. |
| `host` | `string` | yes | Bound address (or the configured one when not listening). |
| `listenUrls` | `string[]` | no | Addresses other machines open (http://<lan-ip>:<port>/web/). |
| `listening` | `boolean` | yes | The external listener is open now. |
| `mode` | `"token" \| "password"` | yes | 'token': the access link (a web access token in ?t=, traded for a cookie on the first request) opens the page; 'password': a username and password are set and the login page is required — the link alone is not enough. |
| `needsRestart` | `boolean` | no | settings.json says otherwise than what runs (enabled/host/port/username): the next daemon start will differ. web.remote.listen applies at once and can save. |
| `passwordSet` | `boolean` | yes | A password is stored (scrypt hash, credentials.json). |
| `plainHttp` | `boolean` | no | The listener speaks plain HTTP. |
| `port` | `number` | yes | Bound port (or the configured one). |
| `sessions` | `number` | no | Open browser sessions through the listener. |
| `turnsRunning` | `number` | no | Turns running now (a stop or restart would cut their watchers). |
| `username` | `string` | no | Login name; empty in access-link mode. |
| `warning` | `string \| null` | no | The plain-HTTP warning to show. |

### `web.remote.listen`

*Direction:* client → server

Remote web access (1.54.0): open (or move) the external listener now, without a daemon restart; save also writes web.remote.*. Every connection through it is a remote web login. A local person only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `host` | `string` | yes | Address to bind (0.0.0.0, a LAN address, ::). |
| `port` | `number` | yes | Port 0-65535; 0 picks a free one. Failures (1.55.0) carry details {field: 'host'\|'port'\|'username', reason: 'in_use'\|'bad_address'\|'permission'\|'out_of_range'\|'required'}: in_use and permission answer 'unavailable', the others 'invalid_params'. A failed move keeps the listener where it was. |
| `save` | `boolean` | no | Also write web.remote.{enabled,host,port,username} for the next start. |
| `username` | `string \| null` | no | Login name for password mode; omitted keeps the configured one. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The access link with its token (token mode, listening). Treat it like a password: anyone with it drives this machine; rotate it with web.remote.rotateToken. |
| `enabled` | `boolean` | yes | web.remote.enabled: the listener opens at daemon start. |
| `error` | `string \| null` | no | Why it is not listening, if it should. |
| `host` | `string` | yes | Bound address (or the configured one when not listening). |
| `listenUrls` | `string[]` | no | Addresses other machines open (http://<lan-ip>:<port>/web/). |
| `listening` | `boolean` | yes | The external listener is open now. |
| `mode` | `"token" \| "password"` | yes | 'token': the access link (a web access token in ?t=, traded for a cookie on the first request) opens the page; 'password': a username and password are set and the login page is required — the link alone is not enough. |
| `needsRestart` | `boolean` | no | settings.json says otherwise than what runs (enabled/host/port/username): the next daemon start will differ. web.remote.listen applies at once and can save. |
| `passwordSet` | `boolean` | yes | A password is stored (scrypt hash, credentials.json). |
| `plainHttp` | `boolean` | no | The listener speaks plain HTTP. |
| `port` | `number` | yes | Bound port (or the configured one). |
| `sessions` | `number` | no | Open browser sessions through the listener. |
| `turnsRunning` | `number` | no | Turns running now (a stop or restart would cut their watchers). |
| `username` | `string` | no | Login name; empty in access-link mode. |
| `warning` | `string \| null` | no | The plain-HTTP warning to show. |

### `web.remote.rotateToken`

*Direction:* client → server

Remote web access (1.54.0): a new web access token; the old link stops working and every open session ends.

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The new access link (when listening in token mode). |

### `web.remote.setPassword`

*Direction:* client → server

Remote web access (1.54.0): store a password (scrypt hash) — password mode: the login page is required; every open session ends. A local person only.

**Params**

| field | type | required | description |
|---|---|---|---|
| `password` | `string` | yes | At least 8 characters. |
| `username` | `string \| null` | no | Set web.remote.username too (saved). |

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The access link with its token (token mode, listening). Treat it like a password: anyone with it drives this machine; rotate it with web.remote.rotateToken. |
| `enabled` | `boolean` | yes | web.remote.enabled: the listener opens at daemon start. |
| `error` | `string \| null` | no | Why it is not listening, if it should. |
| `host` | `string` | yes | Bound address (or the configured one when not listening). |
| `listenUrls` | `string[]` | no | Addresses other machines open (http://<lan-ip>:<port>/web/). |
| `listening` | `boolean` | yes | The external listener is open now. |
| `mode` | `"token" \| "password"` | yes | 'token': the access link (a web access token in ?t=, traded for a cookie on the first request) opens the page; 'password': a username and password are set and the login page is required — the link alone is not enough. |
| `needsRestart` | `boolean` | no | settings.json says otherwise than what runs (enabled/host/port/username): the next daemon start will differ. web.remote.listen applies at once and can save. |
| `passwordSet` | `boolean` | yes | A password is stored (scrypt hash, credentials.json). |
| `plainHttp` | `boolean` | no | The listener speaks plain HTTP. |
| `port` | `number` | yes | Bound port (or the configured one). |
| `sessions` | `number` | no | Open browser sessions through the listener. |
| `turnsRunning` | `number` | no | Turns running now (a stop or restart would cut their watchers). |
| `username` | `string` | no | Login name; empty in access-link mode. |
| `warning` | `string \| null` | no | The plain-HTTP warning to show. |

### `web.remote.status`

*Direction:* client → server

Remote web access (1.54.0): the external listener, its mode, the addresses and the access link. A local person only (never a remote login, browser or connector).

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The access link with its token (token mode, listening). Treat it like a password: anyone with it drives this machine; rotate it with web.remote.rotateToken. |
| `enabled` | `boolean` | yes | web.remote.enabled: the listener opens at daemon start. |
| `error` | `string \| null` | no | Why it is not listening, if it should. |
| `host` | `string` | yes | Bound address (or the configured one when not listening). |
| `listenUrls` | `string[]` | no | Addresses other machines open (http://<lan-ip>:<port>/web/). |
| `listening` | `boolean` | yes | The external listener is open now. |
| `mode` | `"token" \| "password"` | yes | 'token': the access link (a web access token in ?t=, traded for a cookie on the first request) opens the page; 'password': a username and password are set and the login page is required — the link alone is not enough. |
| `needsRestart` | `boolean` | no | settings.json says otherwise than what runs (enabled/host/port/username): the next daemon start will differ. web.remote.listen applies at once and can save. |
| `passwordSet` | `boolean` | yes | A password is stored (scrypt hash, credentials.json). |
| `plainHttp` | `boolean` | no | The listener speaks plain HTTP. |
| `port` | `number` | yes | Bound port (or the configured one). |
| `sessions` | `number` | no | Open browser sessions through the listener. |
| `turnsRunning` | `number` | no | Turns running now (a stop or restart would cut their watchers). |
| `username` | `string` | no | Login name; empty in access-link mode. |
| `warning` | `string \| null` | no | The plain-HTTP warning to show. |

### `web.remote.stop`

*Direction:* client → server

Remote web access (1.54.0): close the external listener; its sessions end.

**Params**

| field | type | required | description |
|---|---|---|---|
| `save` | `boolean` | no | Also set web.remote.enabled false. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `accessUrl` | `string \| null` | no | The access link with its token (token mode, listening). Treat it like a password: anyone with it drives this machine; rotate it with web.remote.rotateToken. |
| `enabled` | `boolean` | yes | web.remote.enabled: the listener opens at daemon start. |
| `error` | `string \| null` | no | Why it is not listening, if it should. |
| `host` | `string` | yes | Bound address (or the configured one when not listening). |
| `listenUrls` | `string[]` | no | Addresses other machines open (http://<lan-ip>:<port>/web/). |
| `listening` | `boolean` | yes | The external listener is open now. |
| `mode` | `"token" \| "password"` | yes | 'token': the access link (a web access token in ?t=, traded for a cookie on the first request) opens the page; 'password': a username and password are set and the login page is required — the link alone is not enough. |
| `needsRestart` | `boolean` | no | settings.json says otherwise than what runs (enabled/host/port/username): the next daemon start will differ. web.remote.listen applies at once and can save. |
| `passwordSet` | `boolean` | yes | A password is stored (scrypt hash, credentials.json). |
| `plainHttp` | `boolean` | no | The listener speaks plain HTTP. |
| `port` | `number` | yes | Bound port (or the configured one). |
| `sessions` | `number` | no | Open browser sessions through the listener. |
| `turnsRunning` | `number` | no | Turns running now (a stop or restart would cut their watchers). |
| `username` | `string` | no | Login name; empty in access-link mode. |
| `warning` | `string \| null` | no | The plain-HTTP warning to show. |

### `web.search`

*Direction:* client → server

Web search for a browser host (1.11.0): the web_search tool's provider chain, settings, caps and timeouts, for the host's own fallback when its search page is blocked. Browser host clients (with a clientId) only; 'unavailable' with details.reason when no provider can answer. Results are untrusted web content.

**Params**

| field | type | required | description |
|---|---|---|---|
| `num` | `number` | no | How many results, 1-20 (default 10). |
| `query` | `string` | yes | What to search for. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `provider` | `string` | yes | The search provider that answered, e.g. 'ddgs'. |
| `results` | `({ snippet?: string; title: string; url: string; })[]` | no |  |

### `workspace.add`

*Direction:* client → server

Add a project folder (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `path` | `string` | yes | Folder to add; an existing workspace for it is returned. |
| `title` | `string \| null` | no | Name; the folder's name when omitted. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `workspace` | `{ createdAt: string; id: string; path: string; position?: number; sessions?: number; title: string; } \| null` | no | The workspace now. |

### `workspace.list`

*Direction:* client → server

Project folders in sidebar order, each with its conversation count (1.47.0, capability 'workspaces').

**Params**

_No params (send `{}`)._

**Result**

| field | type | required | description |
|---|---|---|---|
| `workspaces` | `({ createdAt: string; id: string; path: string; position?: number; sessions?: number; title: string; })[]` | no | In sidebar order. |

### `workspace.remove`

*Direction:* client → server

Remove a folder from the sidebar; its conversations stay (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Workspace id. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `ok` | `boolean` | no | True when the call succeeded. |

### `workspace.rename`

*Direction:* client → server

Rename one (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `id` | `string` | yes | Workspace to rename. |
| `title` | `string` | yes | New name. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `workspace` | `{ createdAt: string; id: string; path: string; position?: number; sessions?: number; title: string; } \| null` | no | The workspace now. |

### `workspace.reorder`

*Direction:* client → server

Set the sidebar order (1.47.0).

**Params**

| field | type | required | description |
|---|---|---|---|
| `ids` | `string[]` | yes | Workspace ids in the order wanted; the rest follow. |

**Result**

| field | type | required | description |
|---|---|---|---|
| `workspaces` | `({ createdAt: string; id: string; path: string; position?: number; sessions?: number; title: string; })[]` | no | In sidebar order. |

## Notifications

### `approval.pending`

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The session's owning client (addendum 8). A browser profile shows only its own cards and those with null. |
| `refused` | `{ code: string; message: string; reason: string; } \| null` | no | 1.55.0: set only on the re-announcement to the asking connection after its reply was refused; the request is still open. |
| `request` | `{ args?: Record<string, unknown>; argsEscaped?: boolean \| null; diff?: string \| null; external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; folder?: string \| null; guard?: string \| null; neverDelegable?: boolean \| null; note?: string; paths?: string[] \| null; personOnly?: boolean \| null; reason?: string \| null; reasonCode?: string \| null; remember?: { display: string; note?: string \| null; pattern: string; scopes: ("project" \| "always")[]; } \| null; requestId: string; risk?: string; route?: "person" \| "jarvis" \| "auto" \| null; scopeHint?: "once" \| "session" \| "project" \| "always" \| "site"; sessionId: string; site?: string \| null; timeoutSec?: number; tool: string; }` | yes | The request now in the shared queue. |

### `approval.resolved`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes | Surface or user that answered. Since 1.53.0 also 'jarvis' (the Jarvis session decided within its delegation), 'jarvis:auto' (the delegation engine allowed a safe request without a model call) and 'rerouted' (with outcome 'withdrawn'). |
| `decision` | `"allow" \| "deny" \| null` | no | The decision that was recorded; null only with outcome 'withdrawn' (1.53.0). |
| `outcome` | `"withdrawn" \| null` | no | 'withdrawn' (1.53.0, security review 3): the request is no longer this connection's to show — sent only to a browser connection when a request it showed was routed to Jarvis. Nothing was decided; stop showing it. If it is routed back to the person the browser gets approval.pending again. |
| `requestId` | `string` | yes | Request that was resolved. |
| `scope` | `"once" \| "session" \| "project" \| "always" \| "site" \| null` | no | The scope actually applied (1.54.0): once, session, project, always or site — after core's rules (a browser's unmarked widening reply, a person-only request, a shell line with no safe rule are kept to 'once'). Null for a deny and with outcome 'withdrawn'. |

### `approval.route.changed`

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | As ApprovalPendingNotification.hostToolsFrom. |
| `reason` | `"jarvis_enabled" \| "jarvis_disabled" \| "paused" \| "resumed" \| "budget" \| "declined" \| "refused"` | yes | jarvis_enabled (it was waiting when Jarvis mode came on and has just been routed), jarvis_disabled (routing ended), paused (the person took the session back or typed in it: Jarvis's requests there are the person's), resumed (handed back, or the quiet time ran out: routed again), budget (Jarvis's allowance ran out), declined (Jarvis's turn ended without deciding it), refused (the engine refused Jarvis's or its own decision, e.g. on a re-check). |
| `requestId` | `string` | yes | The waiting request. |
| `route` | `"person" \| "jarvis" \| "auto" \| null` | yes | The new route; null when Jarvis mode went off (no routing). |
| `sessionId` | `string` | yes | Its session; 'ext:<cli>:<id>' for an external session's approval. |

### `audio.install.progress`

| field | type | required | description |
|---|---|---|---|
| `bytesDone` | `number \| null` | no | Bytes transferred so far. |
| `bytesTotal` | `number \| null` | no | Total bytes, from Content-Length when the server sends one. |
| `engine` | `string` | yes | Engine being installed, always the bare id. |
| `line` | `string` | no | One line of the installer's output. |
| `percent` | `number \| null` | no | 0-100 within the stage, when it can be known. |
| `stage` | `string` | no | resolve \| download \| extract \| verify \| install \| check. 'install' is the package manager, 'verify' is the checksum, 'check' is the detection that runs afterwards. |
| `step` | `number` | no | 1-based place of this stage in the sequence. |
| `steps` | `number` | no | How many stages this engine has in total. |
| `voice` | `string` | no | Set when a *voice* of that engine is installing, e.g. ko_KR-kss-medium; empty for the engine itself. A surface keys its progress row on the pair, so an engine install and a voice install never share a row. |

### `commands.changed`

| field | type | required | description |
|---|---|---|---|
| `commands` | `({ argsSchema?: Record<string, unknown>; format?: "snowpea" \| "claude" \| "codex"; name: string; source?: string; summary: string; })[]` | no | The command table as it stands now. |
| `reason` | `string` | no | Why the table changed. |

### `external.clis.changed`

| field | type | required | description |
|---|---|---|---|
| `clis` | `({ approvalPolicy?: "asks" \| "auto" \| null; capabilities?: { approvals: "bridged" \| "degraded"; liveView: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; steer?: boolean; streaming: boolean; tmux?: boolean; } \| null; defaultMode?: "default" \| "readonly" \| "unrestricted"; enabled?: boolean; id: "claude" \| "codex" \| "agy" \| "cursor"; installed: boolean; models?: string[]; modelsLoaded?: boolean; modes?: ("default" \| "readonly" \| "unrestricted")[]; path?: string \| null; pendingPath?: string \| null; problems?: string[]; title: string; version?: string \| null; })[]` | no | All four, as external.clis would answer now. |

### `external.event`

| field | type | required | description |
|---|---|---|---|
| `kind` | `string` | yes | A session event kind. |
| `payload` | `Record<string, unknown>` | no | Same payload as in session.event. |
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | Session it belongs to. |
| `seq` | `number` | yes | Its own monotonic sequence. |
| `ts` | `string` | yes | UTC ISO time. |
| `turnId` | `string \| null` | no | Turn it belongs to, when known. |

### `external.state`

| field | type | required | description |
|---|---|---|---|
| `approvals` | `"bridged" \| "person" \| "degraded" \| "none"` | yes | As ExternalSessionRow.approvals, now. |
| `canSend` | `boolean` | yes | As ExternalSessionRow.canSend, now. |
| `controlled` | `boolean` | no | As ExternalSessionRow.controlled, now. |
| `error` | `string \| null` | no | With status 'error': why. |
| `host` | `"none" \| "terminal" \| "tmux" \| "snowpea"` | yes | Where its live process is now. |
| `mode` | `"default" \| "readonly" \| "unrestricted" \| null` | no | As ExternalSessionRow.mode, now. |
| `previous` | `"idle" \| "running" \| "error" \| null` | no | Status before. |
| `previousHost` | `"none" \| "terminal" \| "tmux" \| "snowpea" \| null` | no | Host before. |
| `ref` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; }` | yes | Which session. |
| `status` | `"idle" \| "running" \| "error"` | yes | Status now. |
| `title` | `string \| null` | no | Title, when known. |
| `tmux` | `{ pane: string; session: string; } \| null` | no | With host 'tmux': where. |

### `gateway.event`

| field | type | required | description |
|---|---|---|---|
| `bindingId` | `string` | yes | Binding the event belongs to. |
| `kind` | `string` | yes | Event kind, e.g. 'message' or 'error'. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |

### `jarvis.access`

| field | type | required | description |
|---|---|---|---|
| `action` | `"read" \| "prompt" \| "steer" \| "interrupt" \| "create" \| "approval" \| "question"` | yes | read (transcript or digest), prompt, steer, interrupt, create (a new session), approval or question (answered one). |
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | The external session, when it was one. |
| `hostToolsFrom` | `string \| null` | no | The session's owning client (browser host), when it has one. |
| `id` | `string` | yes | Ledger record id. |
| `project` | `string \| null` | no | Its project root. |
| `sessionId` | `string` | yes | Session Jarvis read or instructed; 'ext:<cli>:<id>' for an external session (see external). |
| `summary` | `string` | no | One short line: what was read or sent. |
| `ts` | `string` | yes | UTC ISO time. |

### `jarvis.decision`

| field | type | required | description |
|---|---|---|---|
| `entry` | `{ classification?: "safe" \| "risky" \| "guarded" \| "never" \| null; decidedBy: "jarvis" \| "person" \| "auto"; decision?: string \| null; external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; grantId?: string \| null; guardedClass?: "deploy" \| "outside_destructive" \| "secret_send" \| null; id: string; importance?: "low" \| "normal" \| "high" \| "urgent"; kind: "approval" \| "question" \| "continue" \| "prompt" \| "interrupt" \| "escalate" \| "unrestricted"; project?: string \| null; readOnly?: boolean \| null; reason?: string; requestId?: string \| null; rule?: string \| null; sessionId?: string \| null; summary: string; tool?: string \| null; ts: string; }` | yes | The decision. |

### `jarvis.delegation.changed`

| field | type | required | description |
|---|---|---|---|
| `grant` | `{ allow?: ("deploy" \| "outside_destructive" \| "secret_send")[]; expiresAt?: string \| null; grantedAt: string; grantedBy: string; id: string; level: "staged" \| "full"; note?: string; proposedBy?: "person" \| "jarvis"; scope: "global" \| "project" \| "session"; target?: string \| null; }` | yes | The grant as it is now. |
| `reason` | `"set" \| "revoked" \| "expired"` | yes | What happened. |

### `jarvis.escalation`

| field | type | required | description |
|---|---|---|---|
| `external` | `{ cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null` | no | External session concerned. |
| `guardedClass` | `"deploy" \| "outside_destructive" \| "secret_send" \| null` | no | With reason 'guarded'. |
| `id` | `string` | yes | Escalation id (repeats of the same need keep it). |
| `importance` | `"low" \| "normal" \| "high" \| "urgent"` | no | How prominently to show it: in-app badge always, OS notification from normal (jarvis.notify.os), messenger from high (jarvis.notify.messenger). |
| `project` | `string \| null` | no | Its project root. |
| `reason` | `"guarded" \| "risky" \| "never" \| "question" \| "finished" \| "error" \| "stalled" \| "budget" \| "jarvis"` | yes | guarded/risky/never: an approval the engine routed to the person ('never': never delegable, the person may still allow it); question: a question routed to the person; finished/error/stalled: a turn ended that way in a fully delegated session; budget: Jarvis is out of allowance; jarvis: Jarvis chose to ask the person. |
| `requestId` | `string \| null` | no | Its id, to answer with approval.respond / question.respond. |
| `requestKind` | `"approval" \| "question" \| null` | no | Kind of the waiting request, when one. |
| `sessionId` | `string \| null` | no | snowpea session concerned. |
| `summary` | `string` | yes | One short line for the person (fits an OS notification, ~120 chars). |
| `title` | `string \| null` | no | Its title. |
| `ts` | `string` | yes | UTC ISO time. |

### `jarvis.escalation.dismissed`

| field | type | required | description |
|---|---|---|---|
| `ids` | `string[]` | yes | Dismissed escalation ids. |

### `jarvis.state`

| field | type | required | description |
|---|---|---|---|
| `reason` | `"enabled" \| "disabled" \| "started" \| "stopped" \| "budget" \| "paused" \| "resumed" \| "settings"` | yes | What changed. |
| `status` | `{ budget?: { dailyTokens: number; exhausted?: boolean; tokensToday?: number; wakesPerHour: number; wakesThisHour?: number; } \| null; delegationDefault?: "staged" \| "full"; enabled: boolean; folder: string; hiddenProjects?: string[]; model?: string \| null; paused?: ({ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; project?: string \| null; reason: "typing" \| "takeover"; sessionId?: string \| null; until?: string \| null; })[]; problems?: string[]; running?: boolean; sessionId?: string \| null; }` | yes | jarvis.status after the change. |

### `job.event`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Job the event belongs to. |
| `kind` | `"started" \| "finished" \| "failed" \| "denied" \| "deleted" \| "updated" \| "waiting_for_host" \| "missed" \| "notify"` | yes | Where the run got to. 'notify' (1.23.0) follows the finish of a job with meta, payload {status, sessionId, text}: its host shows the user a notification. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |

### `lsp.changed`

| field | type | required | description |
|---|---|---|---|
| `id` | `string \| null` | no | Server id, or null for 'everything'. |
| `reason` | `string \| null` | no | As LspServerStatus.reason. |
| `root` | `string \| null` | no | Project root of that server. |
| `state` | `"starting" \| "ready" \| "broken" \| "stopped" \| null` | no | Its new state. |

### `lsp.install.progress`

| field | type | required | description |
|---|---|---|---|
| `error` | `string \| null` | no | For done and not ok: why. |
| `id` | `string` | yes | Server being installed. |
| `line` | `string \| null` | no | The output line, or the command. |
| `ok` | `boolean \| null` | no | For done: whether it worked. |
| `stage` | `"start" \| "output" \| "done"` | yes | start (the installer runs), output (a line it printed), done. |

### `mcp.auth.done`

| field | type | required | description |
|---|---|---|---|
| `error` | `string \| null` | no | Why it failed: refused, timed out, the exchange failed, or 'cancelled' when the connection that started it closed. |
| `name` | `string` | yes | Server name. |
| `ok` | `boolean` | yes | True when tokens were stored and the server restarted. |
| `scope` | `"project" \| "global" \| "plugin" \| "settings"` | yes | Scope of the declaration that signed in. |
| `state` | `string` | yes | The flow's state, as mcp.authorize returned it; a flow another authorize replaced reports nothing, so a host matches on this. |

### `mcp.changed`

| field | type | required | description |
|---|---|---|---|
| `auth` | `{ clientId?: string \| null; required?: boolean; state?: "ok" \| "needs_auth" \| "none"; } \| null` | no | OAuth standing after the transition (1.34.0). |
| `error` | `string \| null` | no | Why the server is in the error state. |
| `name` | `string` | yes | Server name. |
| `removed` | `boolean` | no | True when the entry itself is gone. |
| `scope` | `"project" \| "global" \| "plugin" \| "settings"` | no | Scope the server is declared in. |
| `state` | `"stopped" \| "starting" \| "ready" \| "error"` | yes | stopped, starting, ready or error. |
| `toolCount` | `number` | no | Tools the server currently contributes. |

### `project.trust.changed`

| field | type | required | description |
|---|---|---|---|
| `by` | `string \| null` | no |  |
| `projects` | `string[]` | no | migration: every project trusted as it was. |
| `reason` | `"set" \| "revoke" \| "migration" \| "store_lost"` | no | store_lost (1.55.0, security review 6): the trusted-projects record is missing or unreadable, so no project is trusted until the person trusts it again; security.get.problems carries the sentence. |
| `scopes` | `("hooks" \| "mcp" \| "commands" \| "agents" \| "prompts" \| "settings" \| "mode" \| "externalCli")[]` | no |  |
| `workdir` | `string` | no | The project root ('' for the migration and for store_lost). |

### `provider.loginProgress`

| field | type | required | description |
|---|---|---|---|
| `expiresInSec` | `number \| null` | no | Seconds until the code or session expires, when known. |
| `message` | `string \| null` | no | One line for humans. |
| `method` | `string` | yes | Login flow in progress, e.g. 'device_code' or 'oauth_pkce'. |
| `phase` | `"started" \| "await_user" \| "polling" \| "done" \| "failed"` | yes | Where the login got to. |
| `userCode` | `string \| null` | no | Short code the user types in, for device-code flows. |
| `vendor` | `string` | yes | Vendor being logged into. |
| `verificationUri` | `string \| null` | no | URL to open to approve the login. |
| `verificationUriComplete` | `string \| null` | no | verificationUri with the code already embedded, when known. |

### `question.pending`

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The session's owning client (addendum 8). A browser profile shows only its own cards and those with null. |
| `refused` | `{ code: string; message: string; reason: string; } \| null` | no | 1.55.0: set only on the re-announcement to the asking connection after its reply was refused; the question is still open. |
| `request` | `{ external?: { cli: "claude" \| "codex" \| "agy" \| "cursor"; id: string; } \| null; kind?: "mode" \| null; neverDelegable?: boolean \| null; personOnly?: boolean \| null; questions?: ({ allowOther?: boolean; header?: string; multi?: boolean; options?: ({ description?: string; label: string; preview?: string; })[]; question: string; secret?: boolean; })[]; requestId: string; route?: "person" \| "jarvis" \| "auto" \| null; sessionId: string; timeoutSec?: number; }` | yes | The question now in the shared queue. |

### `question.resolved`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes | Surface or user that answered. Since 1.53.0 also 'jarvis' (the Jarvis session answered within its delegation) and 'rerouted' (with outcome 'withdrawn'). |
| `outcome` | `"withdrawn" \| null` | no | 'withdrawn' (1.53.0, security review 3): sent only to a browser connection when a question it showed was routed to Jarvis; nothing was answered. |
| `requestId` | `string` | yes | Question that was resolved. |

### `question.route.changed`

| field | type | required | description |
|---|---|---|---|
| `reason` | `"jarvis_enabled" \| "jarvis_disabled" \| "paused" \| "resumed" \| "budget" \| "declined" \| "refused"` | yes | jarvis_enabled (it was waiting when Jarvis mode came on and has just been routed), jarvis_disabled (routing ended), paused (the person took the session back or typed in it: Jarvis's requests there are the person's), resumed (handed back, or the quiet time ran out: routed again), budget (Jarvis's allowance ran out), declined (Jarvis's turn ended without deciding it), refused (the engine refused Jarvis's or its own decision, e.g. on a re-check). |
| `requestId` | `string` | yes | The waiting request. |
| `route` | `"person" \| "jarvis" \| "auto" \| null` | yes | The new route; null when Jarvis mode went off (no routing). |
| `sessionId` | `string` | yes | Its session; 'ext:<cli>:<id>' for an external session's approval. |

### `security.blocked`

| field | type | required | description |
|---|---|---|---|
| `by` | `string` | yes | As SecurityCheckResult.by. |
| `sessionId` | `string` | yes | The session ('ext:<cli>:<id>' for an external session's request). |
| `summary` | `string` | yes | The command line or the call, shortened. |
| `surface` | `"agent" \| "approval" \| "external"` | no | Where it was stopped: snowpea's own tool execution, the approval queue, or an external CLI's bridged approval. |
| `tool` | `string` | yes | The tool the model called. |
| `why` | `string` | no | The reason in English, as the model read it. |

### `session.event`

| field | type | required | description |
|---|---|---|---|
| `hostToolsFrom` | `string \| null` | no | The session's owning client (addendum 8); null for unhosted sessions. |
| `kind` | `string` | yes | Event kind; see sessionEventKinds for the payload schema. |
| `payload` | `Record<string, unknown>` | no | Kind-specific body. |
| `seq` | `number` | yes | Monotonic per-session sequence number. |
| `sessionId` | `string` | yes | Session the event belongs to. |
| `ts` | `string` | yes | UTC ISO-8601 timestamp. |

### `sessions.changed`

| field | type | required | description |
|---|---|---|---|
| `browserProvider` | `"host" \| "local" \| null` | no | A browser session's browser: 'host' (Snowpea browser only) or 'local' (the user opted in to core's own browser); null for non-browser sessions. |
| `hostToolsFrom` | `string \| null` | no | The session's owning client (addendum 8); null for unhosted sessions. |
| `jarvis` | `{ exposed: boolean; grantId?: string \| null; level?: "staged" \| "full" \| null; levelScope?: "session" \| "project" \| "global" \| "default" \| null; pausedUntil?: string \| null; takenOver?: boolean; } \| null` | no | As SessionSummary.jarvis (1.53.0); null while Jarvis mode is off. |
| `reason` | `string` | yes | create, prompt, turn, renamed, close, ... |
| `role` | `"jarvis" \| null` | no | As SessionSummary.role (1.53.0): 'jarvis' for the Jarvis session. |
| `sessionId` | `string` | yes | Session that changed. |
| `status` | `string \| null` | no | Its status now, when known. |
| `title` | `string \| null` | no | Its title, when it has one. |

### `settings.changed`

| field | type | required | description |
|---|---|---|---|
| `keys` | `string[]` | no | Top-level settings sections that changed, e.g. ['providers']. |
| `scope` | `"global" \| "project"` | no | Which document was reloaded; "global" for $SNOWPEA_HOME/settings.json. |

### `system.updateProgress`

| field | type | required | description |
|---|---|---|---|
| `message` | `string` | no | One line for humans. |
| `phase` | `"started" \| "done" \| "failed"` | yes | Where the upgrade got to. |
| `selfRestart` | `boolean` | no | On 'done': the daemon restarts by itself, so the client waits for it instead of restarting the core (1.26.0). |

### `teams.changed`

| field | type | required | description |
|---|---|---|---|
| `reason` | `string` | yes | Why the guide list changed. |
| `team` | `string \| null` | no | Team that changed, when known. |

### `tool.cancel`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | The tool.invoke callId to stop. |
| `sessionId` | `string` | yes | Session whose turn was interrupted. |

## `session.event` kinds

Every session event carries a monotonically increasing per-session `seq`. After a reconnect, `session.resume(sessionId, afterSeq)` replays anything missed.

### kind `agent.changed`

| field | type | required | description |
|---|---|---|---|
| `agent` | `string \| null` | no | Agent definition now in force (prompt, tools, rounds); null for none. |
| `kind` | `"agent.changed"` | no |  |

### kind `audio.spoken`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"audio.spoken"` | no |  |
| `mime` | `string` | no | Media type of that file. |
| `path` | `string` | yes | Audio file the speech was written to. |
| `played` | `boolean` | no | True when the daemon played it. |
| `provider` | `string` | no | Backend that synthesised it. |
| `utterance` | `string` | no | reply = the turn's answer; ack = the opening acknowledgement the agent writes before its first tool call. A surface may label or skip an ack; one that does not know the field treats everything as a reply. |
| `voice` | `string \| null` | no | Voice that was used. |

### kind `checkpoint.restored`

| field | type | required | description |
|---|---|---|---|
| `checkpointId` | `string \| null` | no | Undo checkpoint for the restore, null when none was written. |
| `kind` | `"checkpoint.restored"` | no |  |
| `restored` | `string[]` | no | Paths restored. |
| `skipped` | `({ path: string; reason: "changed_since" \| "not_restorable" \| "missing_blob" \| "excluded"; })[]` | no | Paths skipped and their reasons. |

### kind `checkpoint.updated`

| field | type | required | description |
|---|---|---|---|
| `checkpoint` | `{ createdAt: string; files?: ({ afterSha?: string \| null; beforeSha?: string \| null; path: string; restorable?: boolean; size?: number; skipped?: "too_large" \| "excluded" \| "unreadable" \| null; source: "tool" \| "shell"; status: "modified" \| "created" \| "deleted"; })[]; id: string; kind: "turn" \| "restore"; prompt?: string; sessionId: string; turnId?: string \| null; workdir: string; }` | yes | The whole checkpoint manifest so far. |
| `kind` | `"checkpoint.updated"` | no |  |

### kind `command.progress`

| field | type | required | description |
|---|---|---|---|
| `command` | `"ralph" \| "ultrawork"` | yes | Command reporting. |
| `iteration` | `number` | no | 0 for the plan, then 1, 2, …. |
| `kind` | `"command.progress"` | no |  |
| `outcome` | `string \| null` | no | Set on the last event only: ralph 'complete', 'rejected', 'stopped', 'max_iterations' or 'error'; ultrawork 'complete' or 'partial'. |
| `stories` | `({ id: string; note?: string; status: "pass" \| "fail" \| "pending"; title: string; })[]` | no | Every story, in plan order. |

### kind `command.result`

| field | type | required | description |
|---|---|---|---|
| `appliesFrom` | `"now" \| "next_tool_call" \| "next_request" \| "next_turn" \| null` | no | For a setting change: when the running turn sees it (next_tool_call for the mode, rules and vision model; next_request for MCP servers; next_turn for the model, effort, busy handling and delegation). Null for a read. |
| `command` | `string` | yes | The line as typed, e.g. '/model gpt-5'. |
| `commandId` | `string` | yes | Id of this command run (what session.prompt / command.run returned). |
| `kind` | `"command.result"` | no |  |
| `ok` | `boolean` | yes | False when the command failed or was unknown. |
| `runningTurnId` | `string \| null` | no | The turn that was running and still is. |
| `text` | `string` | no | What the command said (its replies, joined). |

### kind `compaction`

| field | type | required | description |
|---|---|---|---|
| `after` | `number` | no | Estimated tokens the history holds now. |
| `auto` | `boolean` | no | True when core started it (reason auto or overflow), false for manual. |
| `before` | `number` | no | Estimated tokens the history held before. |
| `kept` | `number` | no | Messages kept verbatim after the summary. |
| `kind` | `"compaction"` | no |  |
| `reason` | `"overflow" \| "auto" \| "manual"` | no | 1.55.0: overflow (the server refused the request for its length; the turn retries once after it), auto (the threshold, before a turn) or manual. |
| `summaryChars` | `number` | no | Length of the summary in characters. |

### kind `compaction.started`

| field | type | required | description |
|---|---|---|---|
| `before` | `number` | no | Estimated tokens the history holds now. |
| `kind` | `"compaction.started"` | no |  |
| `reason` | `"overflow" \| "auto" \| "manual"` | no | auto = the auto-compaction threshold triggered it; overflow (1.55.0) = the server refused the request for its length; manual = asked for. |

### kind `context`

| field | type | required | description |
|---|---|---|---|
| `autoCompactAt` | `number \| null` | no | 1.55.0: the 'used' value at which the next turn compacts first (context.autoCompactPercent of the window, less the room kept for the reply); null when auto-compaction is off or the window is unknown. Many short messages can also trigger it earlier (the history's message cap). |
| `contextWindowSource` | `"settings" \| "server" \| "error" \| "catalog" \| "learned" \| "assumed" \| null` | no | Where 'window' came from (1.42.0): settings, server, error (named by a refusal), catalog, learned (moved by what requests taught us) or assumed (131072, nothing named one). 'window' itself is never null from 1.42.0. |
| `estimated` | `boolean` | no | True while 'used' is a local estimate; false once the provider reported it. |
| `kind` | `"context"` | no |  |
| `model` | `string \| null` | no | Model the window belongs to. |
| `percent` | `number \| null` | no | used/window as a percentage, null when the window is unknown. |
| `provider` | `string \| null` | no | Vendor serving that model. |
| `used` | `number` | no | Tokens the current prompt occupies. |
| `window` | `number \| null` | no | Context window of the model in tokens; null when unknown. |

### kind `context.host`

| field | type | required | description |
|---|---|---|---|
| `chars` | `number` | no | Characters of host text kept, after the cap. |
| `kind` | `"context.host"` | no |  |
| `turnId` | `string` | yes | The turn the note was added to. |

### kind `diff`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string \| null` | no | The tool call that made the change (1.29.0). |
| `kind` | `"diff"` | no |  |
| `patch` | `string` | yes | Unified diff of the change. |
| `path` | `string` | yes | File that changed. |

### kind `error`

| field | type | required | description |
|---|---|---|---|
| `code` | `string` | yes | One of the protocol error codes. |
| `details` | `Record<string, unknown> \| null` | no | Machine-readable facts for the code (1.55.0); absent when the code has none. hook_failed: {plugin, hook (the hook's command line), event (PreToolUse, PostToolUse or Stop), reason (one line), exitCode? (absent when it could not start or timed out), timedOut? (true when it timed out)}. context_window_too_small: {windowTokens, neededTokens}. |
| `kind` | `"error"` | no |  |
| `message` | `string` | yes | Human-readable detail. |

### kind `goal.changed`

| field | type | required | description |
|---|---|---|---|
| `goal` | `{ armed?: boolean; blockedReason?: { code: "round-limit" \| "needs-input" \| "no-credentials" \| "no-access" \| "external" \| "other"; message?: string; } \| null; createdAt: string; evidence?: string \| null; id: string; maxRounds: number; objective: string; phase: "active" \| "paused" \| "blocked" \| "complete"; revision: number; rounds?: number; updatedAt: string; } \| null` | no | The goal now; null once cleared. |
| `kind` | `"goal.changed"` | no |  |

### kind `hook.continue`

| field | type | required | description |
|---|---|---|---|
| `count` | `number \| null` | no | The producer's own count so far this turn: Stop-hook continuations (stop_hook), unfinished-work continuations, verify-gate nudges, the retry attempt (provider_retry), budget continuations (budget), empty-answer nudges (other). Before 1.51.0 always the Stop-hook count (0 for other producers). |
| `hookKind` | `"unfinished_work" \| "verify_gate" \| "stop_hook" \| "provider_retry" \| "overflow" \| "handback" \| "budget" \| "goal" \| "other"` | no | Who kept the turn going, inside the payload as 'kind' (1.51.0; the way message.done carries its 'kind'): unfinished_work (the answer listed work it could still do), verify_gate (no fresh check after edits, open to-dos, a deliverable in tmp/), stop_hook (a Stop hook), provider_retry (a failed model call is tried again), overflow (the context overflowed and is summarised), handback (a delegated child handed back), budget (the round budget's probe or automatic continuation), goal (a goal round), other (anything else: an empty answer nudged, a plugin's turn.stopping listener). |
| `kind` | `"hook.continue"` | no |  |
| `limit` | `number \| null` | no | The cap 'count' runs up to, when there is one (1.51.0): unfinished-work continuations (agent.unfinished_continue), verify-gate nudges, Stop-hook continuations, agent.provider_retries (null for a retry of an unreachable server, whose limit is time), agent.auto_budget_continuations, 1 for overflow. |
| `reason` | `string` | yes | What it said, in English (the instruction or the notice); clients that localise build their own line from 'kind', 'count' and 'limit' (1.51.0). |

### kind `job.done`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Job that ran. |
| `kind` | `"job.done"` | no |  |
| `sessionId` | `string \| null` | no | Session the run used. |
| `status` | `string` | no | Job status as the scheduler recorded it. |
| `text` | `string` | no | What the run reported. |

### kind `job.failed`

| field | type | required | description |
|---|---|---|---|
| `jobId` | `string` | yes | Job that ran. |
| `kind` | `"job.failed"` | no |  |
| `sessionId` | `string \| null` | no | Session the run used. |
| `status` | `string` | no | Job status as the scheduler recorded it. |
| `text` | `string` | no | What the run reported, or the error. |

### kind `loop.suspected`

| field | type | required | description |
|---|---|---|---|
| `count` | `number` | no | Repeats seen in the last 20 tool calls. |
| `kind` | `"loop.suspected"` | no |  |
| `tool` | `string` | yes | Tool whose call repeated. |

### kind `lsp.diagnostics`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string \| null` | no | The tool call whose edit or check produced them, when one did (1.29.0). |
| `count` | `number` | no | Diagnostics of every severity. |
| `errors` | `number` | no | How many of them are errors. |
| `kind` | `"lsp.diagnostics"` | no |  |
| `path` | `string` | yes | File the diagnostics are about. |
| `warnings` | `number` | no | How many of them are warnings. |

### kind `memory.candidate`

| field | type | required | description |
|---|---|---|---|
| `candidateId` | `string` | yes | Pass to memory.confirm. |
| `category` | `"shopping" \| "output" \| "site"` | yes | shopping \| output \| site. |
| `kind` | `"memory.candidate"` | no |  |
| `text` | `string` | yes | The preference as it would be stored. |

### kind `message.delta`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"message.delta"` | no |  |
| `text` | `string` | yes | Text fragment to append to the current message. |

### kind `message.done`

| field | type | required | description |
|---|---|---|---|
| `continuations` | `number` | no | How many times the turn was resumed after hitting the output limit. |
| `kind` | `"message.done"` | no |  |
| `messageKind` | `"" \| "interrupted" \| "plan"` | no | Optional message classification inside the message.done payload, for example 'interrupted' for the post-stop system note, or 'plan' (1.32.0) for the plan set_mode puts up for review just before its question; it arrives while that set_mode call is still running. |
| `role` | `"assistant" \| "user" \| "system"` | no | Who produced the message. |
| `text` | `string` | yes | Full message text. |
| `truncated` | `boolean` | no | The answer still hit the output limit and is incomplete. |

### kind `message.reasoning`

| field | type | required | description |
|---|---|---|---|
| `chars` | `number` | no | Characters of reasoning so far in this turn. |
| `kind` | `"message.reasoning"` | no |  |
| `text` | `string` | no | Reasoning fragment; not part of the answer. |

### kind `message.user`

| field | type | required | description |
|---|---|---|---|
| `attachments` | `({ kind?: "file" \| "image" \| "text" \| "page"; name?: string; title?: string \| null; url?: string \| null; })[]` | no | Files sent along with the prompt. |
| `expansion` | `{ kind?: "skill" \| "command"; name: string; text: string; } \| null` | no | Set when `text` is a skill command: the skill body the model was given instead. Surfaces show it folded, like a tool call, not inline. |
| `kind` | `"message.user"` | no |  |
| `origin` | `{ kind: "human" \| "jarvis" \| "schedule" \| "gateway" \| "subagent" \| "system"; surface?: string \| null; } \| null` | no | Whose words these are (1.53.0, capability 'messageOrigin'). Only kind 'human' moves lastPrompt/lastUserAt and pauses Jarvis in a managed session. Null on events stored before 1.53.0; read those as human. A Jarvis wake (the turn's turn.started reason 'jarvis-intake') is kind 'system', surface null: core's text, verbatim with its 'untrusted' fences, never the person's. |
| `refs` | `(Record<string, unknown>)[]` | no | Resolved @ references (path, kind, lines, truncated). |
| `steered` | `boolean` | no | True when this prompt was queued behind a running turn and then folded into that same turn before its next model call. |
| `text` | `string` | yes | Prompt text as the user typed it. |

### kind `mode.changed`

| field | type | required | description |
|---|---|---|---|
| `autoAfterPlan` | `boolean \| null` | no | Whether plan-then-auto is in force after this change (1.32.0); absent from an event that only reports delegation. |
| `delegation` | `boolean \| null` | no | Whether delegation mode is now in force, sent when /delegation changes it; absent when only the permission mode moved. |
| `kind` | `"mode.changed"` | no |  |
| `mode` | `"plan" \| "accept" \| "auto"` | yes | Mode now in effect. |

### kind `model.changed`

| field | type | required | description |
|---|---|---|---|
| `effort` | `"low" \| "medium" \| "high" \| "max" \| null` | no | Effective reasoning effort for the session (CORE-effort). |
| `effortSource` | `"session" \| "model" \| "vendor" \| "default" \| null` | no | Rule that decided the effort: 'session', 'model', 'vendor' or 'default'. |
| `efforts` | `("low" \| "medium" \| "high" \| "max")[] \| null` | no | Tiers this model offers, weakest first; [] means it has no effort setting (effort is then null). E.g. Qwen3.8 Flash Next offers low, medium, high. |
| `kind` | `"model.changed"` | no |  |
| `model` | `string \| null` | no | Model id now in effect. |
| `provider` | `string \| null` | no | Vendor now in effect. |

### kind `plan.updated`

| field | type | required | description |
|---|---|---|---|
| `done` | `number` | no | Steps marked done. |
| `kind` | `"plan.updated"` | no |  |
| `next` | `string \| null` | no | The next pending step as 'S3 <title>'; null when none is left. |
| `path` | `string` | yes | The plan file, relative to the workdir. |
| `status` | `"active" \| "done" \| "archived"` | yes | Where the plan stands. |
| `title` | `string` | yes | The plan's title. |
| `total` | `number` | no | Steps in the plan. |

### kind `provider.retry`

| field | type | required | description |
|---|---|---|---|
| `attempt` | `number` | yes | The retry about to run, 1-based; for 'unreachable', the retries of the unreachable server so far in this call (1.51.0; before, the next charged retry). |
| `delaySec` | `number` | yes | Seconds before it runs. |
| `error` | `string` | yes | Short class of the failure, e.g. ReadTimeout. |
| `kind` | `"provider.retry"` | no |  |
| `max` | `number \| null` | no | Retries allowed (agent.provider_retries); null for an 'unreachable' retry, whose limit is time (see waitLeftSec), not a count (1.51.0). |
| `reason` | `"unreachable" \| "silent" \| "transient"` | no | Why (1.51.0): 'unreachable' — the call never reached the server (DNS, refused, unreachable, connect timeout) and is retried until agent.provider_connect_wait_sec has passed; 'silent' — the server sent no first token, or nothing between events, for agent.provider_first_token_sec / provider_idle_sec; 'transient' — anything else worth another try (429, 5xx, a reset connection, a read timeout). |
| `waitLeftSec` | `number \| null` | no | Seconds left of the agent.provider_connect_wait_sec budget for an 'unreachable' retry; null for the others (1.51.0). |

### kind `provider.waiting`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"provider.waiting"` | no |  |
| `phase` | `"first_token" \| "idle"` | yes | 'first_token' before the call's first event, 'idle' between events. |
| `sinceSec` | `number` | yes | Seconds of silence so far. |

### kind `session.forked`

| field | type | required | description |
|---|---|---|---|
| `atTurnId` | `string \| null` | no | Last inherited turn. |
| `fromSessionId` | `string` | yes | Session the history came from. |
| `historyLen` | `number` | no | Messages inherited. |
| `kind` | `"session.forked"` | no |  |

### kind `step.done`

| field | type | required | description |
|---|---|---|---|
| `cachedInputTokens` | `number \| null` | no | Of those, served from cache. |
| `decodeMs` | `number` | no | Time from the first token to the end, ms. |
| `error` | `string \| null` | no | Why the call failed, when it did. |
| `errorCode` | `string \| null` | no | The failure's code (1.51.0), the one the turn's 'error' event carries: the provider's ProviderError code (internal, timeout, invalid_params, auth_expired, image_rejected, model_not_configured, not_found, …), 'internal' for anything else, 'interrupted' for a call Stop cut short; null for a call that ended normally. |
| `inputTokens` | `number` | no | Prompt tokens the provider reported. |
| `kind` | `"step.done"` | no |  |
| `outcome` | `string` | yes | end_turn, tool_use, max_tokens, interrupted, error, … as the call ended. |
| `outputTokens` | `number` | no | Completion tokens reported. |
| `reasoning` | `string` | no | Everything the call thought, settled, as message.reasoning streamed; partial for an errored or interrupted call (1.50.0). |
| `reasoningTokens` | `number` | no | Thinking tokens (estimated when unsplit). |
| `step` | `number` | yes | Model call number within the turn, from 1. |
| `text` | `string` | no | Everything the call wrote, settled (1.44.0): the same text its message.delta events streamed, stored once. An errored or interrupted call carries what streamed before it stopped (1.50.0). |
| `toolCalls` | `number` | no | Tool calls the call asked for. |
| `totalMs` | `number` | no | Whole call, ms. |
| `ttftMs` | `number` | no | Time to the first streamed token, ms. |
| `turnId` | `string \| null` | no | Turn the call belongs to. |

### kind `step.started`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"step.started"` | no |  |
| `model` | `string \| null` | no | Model called. |
| `provider` | `string \| null` | no | Vendor called. |
| `step` | `number` | yes | Model call number within the turn, from 1. |
| `turnId` | `string \| null` | no | Turn the call belongs to. |

### kind `subagent.done`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Subagent that finished. |
| `budget` | `number` | no | Tool-round budget cap. |
| `deniedCalls` | `number` | no | How many child tool calls were denied in this run. |
| `deniedTools` | `string[]` | no | Unique names of tools whose calls were denied. |
| `kind` | `"subagent.done"` | no |  |
| `name` | `string` | no | Named agent that ran, when there was one. |
| `ok` | `boolean` | no | False when it failed. |
| `reason` | `string` | no | Why it ended: complete, budget, timeout, error, interrupted or denied. |
| `result` | `string` | no | Final report. |
| `rounds` | `number` | no | Tool rounds used by the subagent. |
| `sessionId` | `string \| null` | no | The subagent's own session. |
| `status` | `"queued" \| "running" \| "done" \| "error" \| "interrupted"` | no | Terminal state: done, error or interrupted. |
| `summary` | `string` | no | The subagent's final answer. |
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |
| `usage` | `{ inputTokens?: number; outputTokens?: number; }` | no | Tokens the subagent consumed. |

### kind `subagent.spawn`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Id correlating this subagent's events. |
| `kind` | `"subagent.spawn"` | no |  |
| `name` | `string` | no | Named agent that was spawned. |
| `sessionId` | `string \| null` | no | The subagent's own session, once it has one. |
| `status` | `"queued" \| "running" \| "done" \| "error" \| "interrupted"` | no | State at spawn: queued until a concurrency slot frees up. |
| `task` | `string` | no | Task it was given. |
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |

### kind `subagent.update`

| field | type | required | description |
|---|---|---|---|
| `agentId` | `string` | yes | Subagent reporting progress. |
| `kind` | `"subagent.update"` | no |  |
| `lastText` | `string` | no | Most recent text the subagent produced. |
| `name` | `string` | no | Named agent that is running, when there is one. |
| `sessionId` | `string \| null` | no | The subagent's own session. |
| `status` | `"queued" \| "running" \| "done" \| "error" \| "interrupted"` | no | Lifecycle state. |
| `text` | `string` | no | Human-readable progress text. |
| `title` | `string` | no | One-line label for the delegation, written by the delegating model in the user's language; empty when it wrote none. |
| `usage` | `{ inputTokens?: number; outputTokens?: number; } \| null` | no | Tokens the subagent has used so far, cumulative; sent with every update so a surface can show a long delegation is still moving. |

### kind `team.task.update`

| field | type | required | description |
|---|---|---|---|
| `agentN` | `number \| null` | no | 1-based worker index that owns the task. |
| `assignee` | `string \| null` | no | Worker that owns the task. |
| `kind` | `"team.task.update"` | no |  |
| `retries` | `number` | no | How many times a merge conflict re-queued it. |
| `status` | `"pending" \| "queued" \| "claimed" \| "running" \| "done" \| "conflict" \| "merged" \| "failed"` | no | New state. |
| `taskId` | `string` | yes | Task that changed. |
| `teamId` | `string` | yes | Team the task belongs to. |

### kind `todos.updated`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"todos.updated"` | no |  |
| `todos` | `({ content: string; id: string; status: "pending" \| "in_progress" \| "completed"; })[]` | no | The whole list, in order. |

### kind `tool.call`

| field | type | required | description |
|---|---|---|---|
| `args` | `Record<string, unknown>` | no | Arguments supplied. |
| `callId` | `string` | yes | Id pairing this call with its tool.result. |
| `kind` | `"tool.call"` | no |  |
| `name` | `string` | yes | Tool being called. |

### kind `tool.progress`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | Id of the tool.call this output belongs to. |
| `chunk` | `string` | no | Raw output fragment, in order. |
| `kind` | `"tool.progress"` | no |  |
| `name` | `string` | yes | Tool that is running. |
| `note` | `string \| null` | no | 1.55.0: the host's line for the state, e.g. 'waiting for the page'. |
| `seq` | `number` | no | 0-based index of this progress within the call. |
| `state` | `"running" \| "waiting" \| null` | no | 1.55.0, host tools: set when the host reported a state with tool.progress (chunk is then usually empty): 'waiting' shows the card as waiting (with note), 'running' clears it. Absent on ordinary output chunks. |
| `stream` | `"stdout" \| "stderr"` | no | Which stream the chunk came from. |
| `truncated` | `boolean` | no | True on the final progress when the byte cap stopped the tail. |

### kind `tool.result`

| field | type | required | description |
|---|---|---|---|
| `callId` | `string` | yes | Id of the matching tool.call. |
| `content` | `({ contentRef?: string \| null; data?: string \| null; ephemeral?: boolean \| null; mediaType?: string \| null; text?: string \| null; type: "text" \| "image"; })[] \| null` | no | A host tool's content blocks: text inline, images as contentRef (session.toolContent). Absent for sensitive results (1.7.0). |
| `error` | `string \| null` | no | Failure detail when ok is false. |
| `kind` | `"tool.result"` | no |  |
| `meta` | `Record<string, unknown> \| null` | no | Host-tool facts for surfaces (e.g. pages for page preview cards); not shown to the model. Forwarded as the host gave it, also for sensitive results; dropped above 16 KB (addendum 13). |
| `name` | `string` | yes | Tool that ran. |
| `ok` | `boolean` | yes | False when the tool failed. |
| `output` | `string` | no | Output handed back to the model. |

### kind `turn.dequeued`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.dequeued"` | no |  |
| `queued` | `number` | no | Prompts still waiting after this one left. |
| `reason` | `"started" \| "dropped" \| "steered"` | no | started = it is now running, dropped = it was discarded, steered = it was merged into the running turn. |
| `turnId` | `string` | yes | Turn id that left the queue. |

### kind `turn.done`

| field | type | required | description |
|---|---|---|---|
| `compactions` | `number \| null` | no | Compactions so far when the turn ended (1.46.0); a later one moves the cut. |
| `historyLen` | `number \| null` | no | Stored history length when the turn ended (1.46.0); where a fork cuts. |
| `kind` | `"turn.done"` | no |  |
| `reason` | `"complete" \| "interrupted" \| "error" \| "denied" \| "timeout" \| "budget" \| "stalled" \| "empty_response"` | no | Why the turn ended. budget = the tool-round budget ran out; the turn reported what it had done before ending. stalled = consecutive rounds made no new tool or file progress. empty_response = the model ended with no text and no tool call, again after one nudge (1.23.0); an error event with code empty_response precedes it. |
| `synthetic` | `boolean` | no | True when the daemon wrote this event itself to close a turn a crash left open, rather than the turn reporting its own end (CORE-dangling-turns). The turn produced no further output after the events already stored. |
| `turnId` | `string` | yes | Turn that ended. |

### kind `turn.outcome`

| field | type | required | description |
|---|---|---|---|
| `by` | `"user" \| "host" \| "judge"` | yes | user (a 👍/👎), host, or judge (a detector). |
| `kind` | `"turn.outcome"` | no |  |
| `outcome` | `"ok" \| "failed" \| "corrected" \| "abandoned"` | yes | ok \| failed \| corrected \| abandoned. |
| `reason` | `string \| null` | no | Why, at most 200 characters. |
| `turnId` | `string` | yes | The turn judged. |

### kind `turn.queued`

| field | type | required | description |
|---|---|---|---|
| `kind` | `"turn.queued"` | no |  |
| `position` | `number` | yes | 1-based place in the queue behind the running turn. |
| `queued` | `number` | yes | Prompts waiting in the queue after this one was added. |
| `turnId` | `string` | yes | Turn id assigned to the queued prompt. |

### kind `turn.started`

| field | type | required | description |
|---|---|---|---|
| `goalId` | `string \| null` | no | The goal the round works on (1.50.0); absent otherwise. |
| `goalRound` | `number \| null` | no | A goal round's number, from 1 (reason 'goal', 1.50.0); absent on every other turn. |
| `initiator` | `"user" \| "system"` | no | Who started the turn (1.8.0). 'system' = session.continue: the input is a [system] note, no message.user is emitted, and surfaces show a status line instead of a user bubble. Exception (1.53.0): a Jarvis wake (reason 'jarvis-intake') emits its text as message.user with origin {kind: 'system', surface: null} right after this event; prompt stays null. |
| `kind` | `"turn.started"` | no |  |
| `prompt` | `string \| null` | no | The prompt that opened it; null when there is none. |
| `queued` | `boolean` | no | True when this turn waited in the prompt queue first. |
| `reason` | `string \| null` | no | Why a system turn started, e.g. 'handback' (1.8.0), 'goal' (1.50.0), 'jarvis-intake' (a Jarvis wake, 1.53.0). |
| `turnId` | `string` | yes | Turn that is now running. |

### kind `usage`

| field | type | required | description |
|---|---|---|---|
| `cachedInputTokens` | `number \| null` | no | Of inputTokens, how many the server answered from its prompt cache, when it reports that (1.20.0). |
| `inputTokens` | `number` | no | Prompt tokens consumed. |
| `kind` | `"usage"` | no |  |
| `model` | `string \| null` | no | Model that served the call (1.7.0). |
| `outputTokens` | `number` | no | Completion tokens produced. |
| `provider` | `string \| null` | no | Vendor that served the call (1.7.0). |
| `timings` | `Record<string, number> \| null` | no | llama.cpp's own timings when the server sends them (1.25.0): prompt_n, prompt_ms, cache_n, predicted_n, predicted_ms. A first token much later than prompt_ms was spent waiting for a slot. |

### kind `vision.unavailable`

| field | type | required | description |
|---|---|---|---|
| `count` | `number` | no | How many images went unseen in this report. |
| `error` | `string \| null` | no | Short text of why the vision slot failed ('describe_failed' only). |
| `kind` | `"vision.unavailable"` | no |  |
| `model` | `string \| null` | no | The session's model that could not see. |
| `provider` | `string \| null` | no | The session's provider. |
| `reason` | `"no_vision_model" \| "describe_failed"` | yes | 'no_vision_model': the session's model cannot see images and models.categories.vision is not set (or does not resolve to a model that can). 'describe_failed': the vision slot is set but did not describe the image (see 'error'). |
| `source` | `"attachment" \| "tool"` | yes | 'attachment': the user's message; 'tool': a tool result (view_image, a screenshot). |
| `visionModel` | `string \| null` | no | models.categories.vision as configured ('vendor:model' or a profile id); null if unset. |

## Error codes

Returned as the string `error.data.code` of a JSON-RPC error response.

The numeric `error.code`: `-32601` means only that the **method** does not exist (`method_not_found`), so a client can feature-detect an older daemon by it; a missing session, job, memory, content or other entity is `-32004` (`not_found`, `mcp_not_found`); `-32602` invalid params; `-32603` internal; `-32000` everything else. Before protocol 1.7.0 a missing entity also answered `-32601`.

| code | meaning |
|---|---|
| `approval_denied` | The user denied the approval request. |
| `approval_timeout` | No approval arrived before `approvals.timeoutSec` elapsed. |
| `auth_expired` |  |
| `context_window_too_small` | `error` event (1.55.0), the turn ends with reason error: the model's context window cannot hold the system prompt and tool definitions. details {windowTokens, neededTokens}. |
| `empty_response` |  |
| `exists` |  |
| `external_unavailable` | The external CLI is missing, disabled or an unsupported version (1.53.0). |
| `external_view_only` | external.send to a session snowpea can only view: its process runs in the person's terminal (host terminal), or tmux before input is confirmed (1.53.0). |
| `fork_after_compaction` |  |
| `fork_child_session` |  |
| `fork_no_finished_turn` |  |
| `fork_running` |  |
| `fork_turn_not_found` |  |
| `goal_not_allowed` |  |
| `hook_failed` | `error` event (1.55.0): a plugin hook could not start, failed or timed out; the call goes on unless the hook is failClosed. details {plugin, hook (its command), event (PreToolUse, PostToolUse or Stop), reason, exitCode? (absent when it could not start or timed out), timedOut?}. |
| `image_rejected` |  |
| `internal` | Unexpected server-side failure. |
| `invalid_output` |  |
| `invalid_params` | Params failed schema validation. |
| `is_dir` |  |
| `jarvis_disabled` | A jarvis.* call that needs Jarvis mode on while it is off (1.53.0). |
| `jarvis_person_only` | Only a person may do this (1.53.0): a delegation change from a messenger with jarvis.messenger.allowGrants off or a remote login not re-authenticated, a mode raise, a person-only answer, a setting a browser or connector may not change. `details.reason` and `details.kind` say which (below). |
| `login_unsupported` | The vendor does not support the requested login method. |
| `mcp_auth_failed` |  |
| `mcp_exists` |  |
| `mcp_invalid` |  |
| `mcp_not_found` |  |
| `mcp_read_only` |  |
| `mcp_start_failed` |  |
| `mcp_unsafe` |  |
| `method_not_found` | No such method (`-32601`). |
| `mode_denied` | The session mode forbids this tool or action. |
| `not_found` | No such session, request, job, agent or content (`-32004`). |
| `not_implemented` | Defined in the schema but not implemented in this milestone. |
| `outside_root` |  |
| `protected` |  |
| `protocol_incompatible` | Client and server protocol majors differ. |
| `security_blocked` | The security lists refused the call: a blocked command or a protected path (1.54.0). |
| `session_busy` |  |
| `skill_exists` |  |
| `steer_slash_command` |  |
| `too_large` |  |
| `tool_inactive` | The tool exists but is disabled for this session. |
| `trust_changed` |  |
| `unauthorized` | Missing or invalid token, or a call before `system.hello`. |
| `unavailable` |  |

### `details.reason` values

Some errors say why in `error.data.details.reason` (enumerated since 1.55.0); a client may switch on these. Other `details` fields are named in the meaning.

| reason | on code | meaning |
|---|---|---|
| `catch_all` | `invalid_params` | an allowlist pattern that would match every command |
| `global_only` | `invalid_params` | a project scope tried to set a key only the global settings hold (security.unknownCode); details.key |
| `jarvis_on` | `jarvis_person_only` | the change is part of Jarvis's authority and Jarvis mode is on: the app or the TUI only |
| `mode_raise` | `jarvis_person_only` | raising a session's mode (plan < accept < auto) needs a person; details.from/to |
| `model_cannot_see` | `unavailable` | llm.sample images for a model known not to take images |
| `no_vision_model` | `unavailable` | llm.sample modelCategory 'vision' with none configured |
| `not_bundled_plugin` | `jarvis_person_only` | skill.install from a browser outside its Settings page, not the bundled plugin; details.why |
| `person_only` | `jarvis_person_only` | only a person's connection may do this (details.kind says who asked) |
| `project_add_only` | `invalid_params` | a project's security lists may only add entries; details.removed names the rest |
| `untrusted_project` | `external_unavailable` | first external CLI session in this project: confirm with trust: true; details.project |
| `untrusted_project_person_only` | `external_unavailable` | a person must open an external session in this project first; details.project |

`details.kind` of a `jarvis_person_only` error says who asked: `person`, `remote_login`, `browser`, `connector`, `none`, `agent`, `mode` (a connection kind, or the mode guard's caller kind). A refused answer to a person-only question carries the question's kind instead (`mode`, or null).
