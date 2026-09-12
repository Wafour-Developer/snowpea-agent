# Deviations — CORE-login-progress (`provider.loginWeb` / `provider.loginProgress`)

Additive protocol work so GUI clients (the IDE) can complete the OpenAI device-code and
OpenRouter PKCE logins, which previously only reported the user code through the CLI's
`on_prompt` callback. Recorded here per `docs/design/deviations/README.md`.

1. **`provider.loginWeb` returns `await_user` as soon as the device code / PKCE URL is known,
   not when the flow finishes.** `snowpea_core/providers/auth_web.py` splits each flow into a
   `*_start` (talk to the vendor / open the callback server, return the code and URL) and a
   `*_finish` (poll or wait for the callback, exchange for a token) half joined by a new
   `LoginStart` dataclass (`finish()` resumes to a `LoginResult`). `device_code_login` and
   `oauth_pkce_login` — used by nothing outside this module now — are kept as thin
   `start(); await result.finish()` wrappers so their existing tests are untouched.
   `provider_login_web_handler` calls the new `auth_web.login_started`, answers immediately with
   `userCode`/`verificationUri`/`verificationUriComplete`/`expiresInSec` on
   `ProviderLoginWebResult`, and hands `started.finish()` to `RpcConnection.spawn` so the poll
   loop runs as one of the connection's tracked background tasks — it is cancelled if the
   connection closes, same as any other in-flight handler.

2. **`status` on the result is always `"await_user"` in practice.** The field is typed
   `"await_user" | "done" | "failed"` per the task's contract, but both flows always have a code
   or URL to show before they return (`method_for` already rejects unsupported vendors
   synchronously, before any network call), so there is currently no path that finishes
   synchronously. The wider type is kept because it is what was asked for and because it costs
   nothing on the wire; a future flow that *can* finish in one round trip does not need a protocol
   change to report it.

3. **`provider.loginProgress` is broadcast via `EventHub.notify`, i.e. to every subscribed and
   registered-client connection, not just the caller.** The task explicitly allows this
   ("emitted to the calling connection (and all authenticated connections is fine)"), and it is
   the same fan-out `system.updateProgress` already uses (`update.notify_progress` &rarr;
   `hub.notify`) — reusing it means one client logging a vendor in updates every open IDE/CLI
   window's provider status, which matches how `provider.configure`'s persisted result is already
   visible on the next `provider.list`.

4. **The CLI path (`auth_web.login`) is unchanged and still blocks until the token is granted.**
   It is now implemented as `login_started(...)` then `await started.finish()`, with
   `on_prompt` still firing at the same point (right before the wait loop starts) and
   `webbrowser.open` still called from inside `finish()`. Only the RPC path
   (`provider_login_web_handler`) uses the split; nothing else in the tree calls
   `device_code_login`/`oauth_pkce_login`/`auth_web.login` besides that handler, confirmed with
   `grep -rn "auth_web\."`.

5. **A failed background login is reported via `loginProgress(phase="failed")` and a log line, not
   surfaced back to the original request.** The RPC response already went out (with
   `status: "await_user"`) by the time a device code is denied or a poll times out; there is no
   pending JSON-RPC request left to attach an error to. `finish_and_persist` in
   `app_server.py` catches the exception `auth_web`'s own `except` block already logged as a
   `failed` progress notification, logs once more at `warning` for the daemon's own log, and
   returns — nothing is persisted to `settings.json` on that path.

Verification run at the time of this change:

- `uv run pytest tests/test_login_web.py tests/test_provider_matrix.py -q` — 60 passed, 1 skipped
  (pre-existing: the Anthropic-SDK-framing case).
- `uv run ruff check core tests` — all checks passed.
- `uv run mypy core` — no issues found in 138 source files.
- `uv run python scripts/gen_protocol.py --check` — clean after running
  `uv run python scripts/gen_protocol.py` (regenerated `sdk/src/protocol.ts` and
  `docs/protocol.md` for the new `provider.loginProgress` event and the additive
  `ProviderLoginWebResult` fields).
- `npm -w sdk run build` — clean.
- `uv run pytest -q` (full suite) — see the session report for the pass/fail count; other
  concurrent agents (`exec-update`, `exec-gateway-autostart`) were editing this tree at the same
  time, so a full-suite run here also exercises their changes.
