# Deviations — CORE-settings-reload (settings.json edits reach a running daemon)

`Daemon.start` read `$SNOWPEA_HOME/settings.json` once and handed the resulting `Settings` object to
half a dozen collaborators that kept a reference to it. `snowpea setup --vendor local --model X`
writes that file from its *own* process, so a daemon that was already running kept resolving the
model it had booted with: the reported symptom was `local (wrong-model): HTTP 404` on the very next
`snowpea -c "…"`. Recorded here per the deviations-log convention
(`docs/design/deviations/README.md`).

1. **The reload is stat-gated, not watched.** There is no filesystem watcher and no background
   poller. `config/hot_reload.py::stamp` is one `os.stat` returning `(st_mtime_ns, st_size)`;
   `Core.reload_settings` compares it against `Core.settings_stamp` and returns early when the pair
   is unchanged. `session.create` and `session.prompt` call it, so the cost is one `stat` per turn
   and a JSON parse only when the file really moved. A watcher would have meant an extra thread or
   an inotify dependency for a file that changes a handful of times in a daemon's life.

2. **Rebinding is a fallback; reading `core.settings` lazily is the rule.** Only the collaborators
   that genuinely captured the object are rebound in `hot_reload.rebind`: `SessionManager`,
   `ApprovalQueue`, `ProviderRegistry`, `Allowlist`, plus the two that captured a *section*
   (`Scheduler.settings` is `settings.scheduler`, `MemoryServices.settings` is `settings.memory`,
   and `Retrieval.top_k` is copied out of it). Everything else — the web-search and browser tool
   providers, the MCP permission table, the media tools, the agent loop, `/ralph`, teams, the update
   handlers — already reads `core.settings` at call time and therefore needs nothing. New code
   should keep doing that rather than adding a line to `rebind`.

3. **A no-op write reloads nothing and notifies nobody.** `hot_reload.changed_keys` diffs the two
   documents by top-level key. When the set is empty the stamp is still refreshed (so the work is
   not repeated) but no rebinding and no `settings.changed` happen. This is what makes the daemon's
   *own* writes cheap: `ProviderRegistry.save` (from `/model` and the lazy model auto-pick) rewrites
   the file without going through `settings.set`. `settings.set` and `provider.configure` call
   `Core.mark_settings_saved()` directly; `ProviderRegistry` cannot (it has no `Core`), so it grew
   one optional `on_saved` callback that `wire_core` and `hot_reload.rebind` point at that same
   method. A registry that nobody wired — every construction in the tests and the setup wizard —
   leaves it `None` and behaves exactly as before.

4. **`settings.set` rebinds in-process instead of re-reading its own write.** The handler already
   held the merged document, so it calls `Core.adopt_settings(updated, changed_keys)` — rebind,
   re-stamp, notify — rather than `reload_settings()`, which would have diffed the new document
   against itself and found no change. The pre-existing unconditional `_sync_gateways(core)` call is
   left exactly as it was, so gateway behaviour on `settings.set` is unchanged; the reload path has
   its own `"gateway" in keys` sync for edits that arrive from outside.

5. **Project settings needed no change, and no stat check either.** `<workdir>/.snowpea/settings.json`
   was never cached: `SessionManager.default_mode` / `max_concurrent`, `Allowlist._project_items`
   and `/mode` all call `ProjectSettings.load(workdir)` at the moment they need it, and `backend` has
   no reader at session-create time at all. Adding a stamp there would have been strictly worse than
   the lazy load already in place, so the story's third item is covered by a regression test
   (`test_project_settings_are_read_fresh_per_session`) rather than by new code.

6. **`PROTOCOL_VERSION` is not bumped.** `system.reloadSettings`, `SettingsReloadResult` and the
   `settings.changed` event are purely additive: no existing method, result or event changed shape,
   and an older client that never calls the method and ignores the notification is unaffected. The
   file is currently `1.2.0` and two concurrent stories are editing it, so bumping here would have
   been a merge conflict for no protocol benefit. `sdk/src/protocol.ts` and `docs/protocol.md` were
   regenerated with `scripts/gen_protocol.py`.

7. **The CLI never starts a daemon in order to reload it.** `_reload_running_daemon` uses
   `read_daemon_json` + `pid_alive` + `GET /health` and returns silently when nothing is listening,
   deliberately not `ensure_daemon`, which would spawn one. When a daemon *is* up it prints
   `daemon: settings reloaded (<changed keys>)`; when the daemon answers `reloaded: false` with no
   changed keys it prints nothing, so a wizard run that changed nothing stays quiet. A failure to
   reach the daemon is a printed hint to restart it, not a non-zero exit: the settings write itself
   succeeded.
