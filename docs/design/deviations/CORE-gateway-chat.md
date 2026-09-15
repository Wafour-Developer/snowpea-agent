# Deviations — CORE-gateway-chat (a messenger becomes a real surface)

The chat gateway could carry prompts and answers, but nothing else: one chat was one session
forever, and a turn that ran for two minutes was two minutes of silence. This story adds
chat-level slash commands, a keep-typing loop and one edited-in-place progress message per turn.
Recorded here per the deviations-log convention (`docs/design/deviations/README.md`).

## What was ported

The shape of the feature comes from **Hermes' gateway** (MIT): chat-level `/new`, `/resume`,
`/sessions`, `/status` and `/stop` handled before the command registry sees the text, and a
typing indicator re-sent on a timer for as long as a turn is running, paused while the agent is
waiting on a human. No code was copied — snowpea's router, session manager and event hub are
nothing like Hermes' — only the command set, the four-second cadence, and the decision to pause
the indicator rather than let it lie.

## What differs

1. **Buttons, not a re-typed id.** snowpea's chat surface is already button-based (approvals,
   `ask_user` options), so `/sessions` and `/projects` attach one inline button per row with new
   `ses:<sessionId>` / `prj:<n>` callbacks alongside the existing `apr:` and `qst:` ones. A bare
   number typed after the list works too, mirroring how an open question reads `2` as an answer,
   because a platform that drops the keyboard must still leave the list usable.

   The session button carries the **id** and the project button carries the **row number**. A
   list a person is looking at can be minutes old, and a number would then resume whatever has
   since taken that position; a filesystem path, on the other hand, does not fit in Telegram's 64
   bytes of callback data, and the project list is rebuilt identically on the press.

2. **Per-chat session persistence.** Hermes keys a session to a chat for the lifetime of the
   process. snowpea writes the pairing to `$SNOWPEA_HOME/gateway-chats.json`
   (`{"<binding_id>|<channel_id>": "<session_id>"}`) and reads it back in
   `GatewayRouter._session_for`, so `/resume` survives a daemon restart and a phone conversation
   picks up where it left off. Deliberately a small JSON file rather than a `state.db` table:
   losing it costs one `/resume`, and a chat that cannot find its remembered session must still
   work. A remembered session that was deleted, or that turns out to be a subagent's, is
   forgotten and the binding's own target is used. When the binding targets a named agent the
   restored session's `memory_namespace` is re-applied, so a chat bound to an agent can never
   read the default namespace by way of a restore.

3. **The project list comes from `ide.projects`.** The core has no project registry; the IDE
   keeps one in its own block of the settings document, which `Settings` preserves because the
   model is `extra="allow"`. `read_projects` parses it permissively (a bare string or an object,
   `pinned` first, then `lastOpenedAt` newest first) and merges in `Store.session_workdirs()` so
   a daemon with no IDE still has a useful `/projects`. Nothing in the core writes that block.

4. **`/help` is answered by the gateway, not the registry.** There is a registry `/help`, and it
   is the right answer in a terminal. From a chat the useful answer names the chat commands
   first, then the handful of session commands worth typing on a phone (`HELP_REGISTRY`). The
   registry command still runs for every other surface, unchanged.

5. **Gating follows the approval rule, narrowed.** `/resume`, `/new`, `/stop` and the row
   buttons change what the next message will run and where, so they are refused for anyone but
   the binding's `user_id` — with the same terse wording the approval path uses. Unlike an
   approval they are *open* when the binding has no `user_id` at all: a binding without an
   approver can already prompt the agent, so refusing it a session switch would protect nothing.
   `/sessions`, `/projects`, `/status` and `/help` are open to everyone.

## Two settings share a dict with the platforms

`settings.gateway` was `dict[str, dict[str, Any]]` — one block per messenger. `gateway.typing`
and `gateway.progress` are booleans in that same block, so the annotation widened to
`dict[str, Any]`. `desired_gateways` already skipped any value that is not a dict, so a switch
can never be mistaken for a messenger to bind; the cost is that `typing` and `progress` are now
reserved names for a platform, which no platform is called.

## `typing` and `edit` are optional, and not on the Protocol

`PlatformAdapter` stays exactly as wide as it was. The two new methods are feature-detected with
`getattr` and documented as `SupportsTyping` / `SupportsEdit`, because Slack genuinely cannot do
one of them: its typing indicator is an RTM feature that bot tokens cannot use. Declaring them on
the Protocol would have made `SlackAdapter` structurally incomplete for no gain. An adapter
without `edit` gets **no progress message at all** rather than one message per tool call — without
editing the feature is chat spam, which is worse than the silence it replaces.

## Two functions were lifted out of the RPC handlers

`session.list` and `session.interrupt` are the right answers for `/sessions` and `/stop`, but
both handlers take an `RpcConnection` the gateway does not have. `collect_sessions(core, params)`
and `interrupt_session(core, session)` are the bodies, and the handlers are now one line each.
No protocol change: the wire shape of both methods is untouched.

## Discord's menu is an interaction, not just a list

Telegram's `setMyCommands` only decorates the typing box: the command still arrives as text.
Discord's `/` picker does not — a registered command arrives as an `INTERACTION_CREATE` that must
be answered within three seconds or the person is told it failed. So `DiscordAdapter` does three
things Telegram does not: it looks up its own application id (`GET /oauth2/applications/@me`) to
`PUT` the global commands at start, it posts a deferred callback (`type: 5`) the moment a command
interaction arrives, and it remembers that interaction token per channel for sixty seconds so the
router's next `send` for that channel `PATCH`es `@original` instead of posting. Only the first
piece of a split answer consumes the token; the rest post normally. Registration and deferral are
both non-fatal — a bot invited without `applications.commands` simply has no picker, and the
typed-text path is untouched.

## Slack's menu is a manifest, and the answer is a `response_url`

Slack cannot be told about its own slash commands over the API: they exist only in the app
manifest, which a person pastes at api.slack.com. So `slack_manifest()` builds one from
`MENU_COMMANDS` and `snowpea gateway slack-manifest` prints it — the only place in the gateway
where a platform needs a manual step no code can take. It is not cosmetic: Slack intercepts every
message beginning with `/` before the Events API sees it, so an undeclared `/sessions` never
arrives at all.

A declared command arrives as a third Socket Mode envelope type, `slash_commands`, which
`parse_envelope` turns into the line the person typed (`/new foo`). The envelope is acked like
any other — empty, within three seconds — because the router answers asynchronously and an ack
body cannot wait for it. The payload's `response_url` is remembered per channel for sixty
seconds, mirroring Discord's interaction token, and the first `send` for that channel posts
there with `response_type: "in_channel"` instead of calling `chat.postMessage`. A `response_url`
post names no message, so that `send` returns `""`; `TurnActivity` now posts its progress line at
most once per turn rather than on every tool call, so an unnameable message costs one line rather
than a flood. Slack still has no typing hint, and the progress line still edits in place.

## Where it lives

`core/snowpea_core/gateway/{chat,activity,router,base,telegram,discord,slack,fake}.py`,
`core/snowpea_core/server/session_handlers.py`, `core/snowpea_core/config/settings.py`,
`core/snowpea_core/cli/commands.py`.
Tests: `tests/test_gateway_chat.py`, `tests/test_gateway.py`, fixture `tests/fixtures/providers/fake/gateway_chat.json`.
