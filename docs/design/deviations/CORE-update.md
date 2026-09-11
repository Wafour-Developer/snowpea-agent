# Deviations — CORE-update (`system.checkUpdate` / `system.update` / `system.restart`)

Additive protocol work (1.1.0 → 1.2.0, capability `update`) for the in-TUI update nudge: an
update check the daemon caches daily, a detached upgrade with progress notifications, a
`restartRequired` flag on `system.info`, and a restart handshake between the TUI and
`cli/main.py`. Recorded here per `docs/design/deviations/README.md`.

1. **`system.restart` shuts the daemon down rather than re-execing it.** The task allowed either
   "exit code 3 that `ensure_daemon` treats as start-again" or "simply shutdown". The second was
   chosen: `restart_handler` calls `core.request_shutdown("restart")`, which is the exact path
   `system.shutdown` already takes, and the next `snowpea` launch spawns a fresh daemon through
   the existing `ensure_daemon`. An exit-code protocol between the daemon process and its spawner
   would have needed `_spawn_daemon` to grow a supervising parent, which nothing else in the tree
   has; `ensure_daemon` already starts a daemon whenever `daemon.json` is stale or the pid is
   gone, so the simple shutdown gives the same user-visible behaviour with no new machinery.

2. **The restart handshake is a TUI exit code, not an RPC.** `tui/src/index.tsx` returns
   `RESTART_EXIT_CODE = 75` (EX_TEMPFAIL, unused by every other snowpea exit path), and
   `cli/main.launch_tui` turns that into `relaunch()` — wait for the old daemon's pid to go away,
   then `os.execv` the same `snowpea` argv. `os.execv` (not a fresh `subprocess`) keeps the
   terminal, the process id and the signal handling the user already has.

3. **`update_command` prefers `uv` over the recorded install method, not the other way round.**
   The task says to use `uv tool install --force --reinstall` and to fall back to `install.json`
   when `uv` is missing. That is what is implemented, with one addition: when `uv` is absent and
   `install.json` records nothing recognisable, `system.update` returns `started: false` with the
   manual command in `command` and an explanation in `error` — it never guesses at `pip` or `npm`
   on a machine where neither was used to install snowpea. Both installers now write
   `install.json` (`record_install` in `install.sh`, `Record-Install` in `install.ps1`), so the
   fallback only matters for installs that predate this change.

4. **A failed check is never cached.** `check_update` writes `update-check.json` only on a
   successful lookup. Caching a failure would mean a machine that was briefly offline reports "no
   update" for 24 hours; instead the next call retries. A successful answer is reused for
   `CACHE_TTL_SEC` (24h) and re-evaluated against the running `__version__` on read, so an answer
   cached before an upgrade stops claiming an update afterwards without a new network call.

5. **`updates.channel` is a plain `str` ("auto" | "pypi" | "git"), not a `Literal`.** Every other
   settings model in `config/settings.py` uses plain scalars with `extra="allow"` and validates
   loosely; a `Literal` here would make a typo in `settings.json` a hard load failure for the
   whole document rather than a field that falls through to the `auto` behaviour. The wire model
   (`CheckUpdateResult.channel`) *is* a `Literal["git", "pypi"]`, because that is an answer the
   daemon produces rather than something a user types.

6. **The daily background check is gated by an environment variable as well as the setting.**
   `SNOWPEA_UPDATE_CHECK=0` overrides `updates.check`. `tests/conftest.py` sets it for the whole
   suite so that the hundreds of daemons the tests start never reach the network;
   `tests/test_update.py` drives `check_update` directly with a scripted client instead.

7. **`/update` in the TUI is intercepted locally instead of being forwarded to `command.run`.**
   The core builtin `update` command exists and is what headless and IDE clients run (it is in
   `commands/builtin.py` and reaches the same `update_handler`), but `app.tsx` catches a bare
   `/update` line and opens the same y/n banner the `U` key opens. Forwarding it would have
   started the upgrade with no confirmation in the one surface where the task asks for one.

8. **`snowpea update` waits for the upgrade to finish before stopping the daemon.** The CLI
   subscribes to `system.updateProgress` and blocks until `done` or `failed` arrives, then runs
   `daemon stop`. Stopping immediately after `system.update` returned would have killed the
   daemon while its watcher task was still waiting on the installer, losing the success/failure
   report — the upgrade subprocess itself is detached and would have survived, so the user would
   have been told nothing about an upgrade that was still running.

9. **The upgrade watcher polls instead of parking a thread on `process.wait()`.**
   `update.wait_for_exit` loops on `Popen.poll()` with an `asyncio.sleep`, bounded by
   `UPDATE_TIMEOUT_SEC` (30 min). The obvious `await asyncio.to_thread(process.wait)` was rejected:
   a thread blocked in `wait()` cannot be cancelled, so an installer that hung would hold a
   non-daemon executor thread for the life of the process and block interpreter shutdown with it —
   a hang no `pytest-timeout` and no `Daemon.stop` could clear. Polling makes the wait ordinary
   cancellable async work, which is what `Daemon.stop` relies on. `start_update` likewise closes
   the parent's copy of the log handle as soon as the child has its dup, and
   `cli/commands._await_update` is bounded by the same deadline, so `snowpea update` cannot block
   forever on a daemon that never reports back. The subprocess is detached
   (`start_new_session=True`), so giving up only ever stops the reporting, never the upgrade.

Verification run at the time of this change:

- `SNOWPEA_SKIP_BROWSER_TESTS=1 uv run pytest -q` — 525 passed, 9 skipped, in 2m18s.
- A plain `uv run pytest -q` does **not** finish on a machine with Chromium installed:
  `tests/test_tools_contract.py::test_local_chromium_navigates_and_snapshots` drives a real
  Playwright browser, and its `@pytest.mark.timeout(60)` uses the thread method, which can dump a
  stack but cannot interrupt the blocked driver — the run then stalls with a
  `playwright/driver/node … run-driver` child alive. That test carries its own
  `SNOWPEA_SKIP_BROWSER_TESTS=1` escape hatch, which is what the run above uses. This is
  pre-existing and unrelated to CORE-update.
- `uv run ruff check core tests scripts` — all checks passed.
- `uv run mypy core` — no issues found in 139 source files.
- `uv run python scripts/gen_protocol.py --check` — `sdk/src/protocol.ts` and `docs/protocol.md`
  both ok.
- `npm -w sdk run build`, `npm -w sdk test`, `npm -w tui test`, `npx tsc -p tui --noEmit`,
  `npm -w tui run build` — all clean.
- `uv run python scripts/check_docs_cli.py` — ok.
