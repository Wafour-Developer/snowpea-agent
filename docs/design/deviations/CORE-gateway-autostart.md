# Deviations — CORE-gateway-autostart (a messenger enabled in the wizard goes live on its own)

`snowpea setup` already wrote `settings.gateway.<platform> = {enabled, token}`, but the daemon only
ever listened on a platform for which someone had separately called `gateway.bind`. This story
closes that gap: the router reconciles a set of *catch-all* bindings against `settings.gateway` at
daemon start, after every `settings.set`, and on demand through a new `gateway.sync` RPC. Recorded
here per the deviations-log convention (`docs/design/deviations/README.md`).

1. **No router change was needed to make a catch-all binding work.** The plan asked to
   "verify/implement" that a binding with `channel_id = None` matches any chat and creates one
   session per chat lazily. It already did: `GatewayRouter.handle` only filters on the channel when
   `binding.channel_id` is set, and `_session_for` keys its connections on
   `(binding.id, channel_id)`, so each conversation gets its own session and its own
   `gateway:<platform>:<channel>` origin surface. `test_gateway.py::test_a_second_message_reuses_the_same_session`
   already covered it for a channel-less binding. Nothing in the matching or session-creation path
   was touched; the new tests pin the behaviour from the settings-driven side instead.

2. **`source` is a column on `gateway_bindings`, added by an in-place migration.** Auto bindings
   must be distinguishable from hand-made ones so a sync never removes something the user bound
   through `gateway.bind`. `BindingStore.__init__` runs `PRAGMA table_info` and issues
   `ALTER TABLE ... ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'` when the column is missing,
   rather than versioning the schema or rebuilding the table: existing rows are all manual bindings,
   so the default is exactly right and no data moves. `Binding.source` defaults to `"manual"`, so
   every existing caller and every restored old row keeps its meaning.

3. **The credential ref for an auto binding is the platform name itself.** `sync_from_settings`
   copies `settings.gateway.<platform>.token` into `credentials.json` under the ref `<platform>`
   (`CredentialStore.set`, which rewrites the file at 0600) and binds with
   `credentials_ref = platform`. That keeps the secret out of `state.db` and out of every log line
   and RPC result, exactly like a manual binding, at the cost of reserving one credential name per
   platform. A user who wants several accounts on one platform still uses `gateway.bind` with their
   own ref; the sync only owns the single auto binding.

   The token also stays in `settings.json`, because that is where the wizard writes it and where
   `desired_gateways` reads it on the next start. `settings.get`/`settings.set` already mask any
   field named `token`, so it is not exposed over RPC.

4. **An enabled platform with no token is a warning, not an error.** A half-finished wizard run
   (platform toggled on, token never pasted) would otherwise stop the daemon from starting.
   `desired_gateways` logs and skips it, so the daemon comes up with no binding for that platform.
   Same for a platform whose adapter cannot start: `sync_from_settings` catches `GatewayError` and
   `CredentialError` per platform and carries on with the rest.

5. **Approvals stay fail-closed; the wizard is what fixes the usability side.** The router's
   existing rule — a binding with no `user_id` can approve nothing, and a press from any other user
   is ignored — is unchanged (plan risk 4). Since a catch-all binding serves *any* chat, that rule
   matters more, not less, so the gateway screen now asks for the approver's account id right after
   the token and stores it as `settings.gateway.<platform>.allowed_user_id`. Leaving it blank is
   allowed and is reported in the wizard summary as `telegram (no approver)` plus a note saying chat
   approvals stay blocked; it is not an error, because a read-only messenger is a legitimate setup.

6. **The wizard asks for the token and user id from `wizard.py`, not from the screen module.**
   `setup/screens/gateway.py` stays a pure build/apply pair with no I/O, matching every other
   screen; `wizard._ask_for_gateway` runs right after that screen the same way `_ask_for_key` runs
   after the providers screen. The prompts only appear when the wizard is interactive and the
   messenger is still missing an answer, so `--gateway/--token/--user-id` and the non-interactive
   path are unaffected.

7. **`settings.set` triggers a sync but never fails because of one.** `_sync_gateways` in
   `settings_handlers.py` runs after the file is persisted and swallows every exception into a log
   warning. The settings write is the user's request; a chat platform that refuses to start is a
   separate problem, and the next daemon start retries the sync anyway.

8. **The CLI calls `gateway.sync` only when a daemon is already running.** `setup_command` checks
   `read_daemon_json` and stays silent (beyond the next-steps text) when there is none — it does not
   spawn a daemon just to bind a messenger, because `snowpea setup` is a configuration command.
   When one is running, the printed `telegram: listening` line reports the platforms the sync
   returned under `added` or `kept`.

9. **`gateway.sync` reports by platform, not by binding id.** `GatewaySyncResult` is three
   `list[str]`s (`added` / `removed` / `kept`). The caller's question is "which messengers are live
   now", and the binding ids of auto bindings are an implementation detail the user never types.
   `GatewayBinding` also gained `source`, so `gateway.list` still exposes the ids and which of them
   the sync owns. Both are additive; `PROTOCOL_VERSION` was left at the value the concurrent
   CORE-update story set (`1.2.0`) rather than bumped again.

10. **`daemon status` prints one `messengers` line, built from `gateway.list`.** The existing
    `gateway_bindings` counter stays (it is what the keepalive reasons use); the new line names the
    platforms and says `listening` or `stopped` per platform, deduplicated so several bindings on
    one platform read as one entry. The extra RPC is wrapped in `contextlib.suppress(RpcCallError)`
    so an older daemon without the method still reports everything else.
