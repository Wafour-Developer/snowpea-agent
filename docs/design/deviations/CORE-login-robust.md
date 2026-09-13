# Deviations — CORE-login-robust (setup wizard survives a failed browser login)

A macOS user ran `snowpea setup`, picked "2=browser login" for `openai`, and the wizard
crashed with an uncaught `snowpea_core.server.errors.RpcError: openai: device authorization
failed (HTTP 403)` from `providers/auth_web.py` `device_code_start`. The same request returns
200 from our network, so the 403 is environment-specific (Cloudflare/IP/account) — but a
vendor-side refusal must never end the wizard with a traceback. Recorded here per
`docs/design/deviations/README.md`.

1. **Enriched `RpcError` messages carry the vendor's own explanation, not just an HTTP
   status.** `_error_detail()` in `auth_web.py` pulls `error_description`/`error`/`message`/
   `detail` out of a JSON body, or the first 200 characters of a text body, and every
   non-2xx branch (`device_code_start`'s device-authorization call, the device-login poll's
   final failure, and `oauth_pkce_start`'s key exchange) appends it to the message and sets
   `RpcError.data = {"vendor", "status", "body"}`. `provider.loginProgress {phase: "failed"}`
   already forwards `str(exc)` (`RpcError.message`), so the richer text reaches the IDE for
   free — no change needed in `server/app_server.py`.

2. **Every outgoing request carries `User-Agent: snowpea-agent/<version> (+https://github.com/
   Wafour-Developer/snowpea-agent)` and `Accept: application/json`.** A new `_post_with_retry()`
   helper centralises this (plus the retry and transport-error handling below) so every POST in
   `device_code_start`/`oauth_pkce_start` goes through one place instead of six separate
   `http.post()` call sites.

3. **The initial device-authorization POST retries once, after a 1s backoff, on
   403/429/5xx.** Only the *first* request retries — the device-login poll loop already retries
   forever on `authorization_pending`/403/404 by design, so wrapping it in another retry would
   just double-count; `_post_with_retry(..., retry=False)` is used there and at the PKCE key
   exchange (a fresh code that fails once should surface, not silently retry against a
   possibly-already-consumed code).

4. **A transport failure (`httpx.ConnectError`/`TimeoutException`/`TransportError`) is mapped to
   `RpcError(errors.INTERNAL, "<vendor>: could not reach the server for <action> (<exc>)")`**
   instead of an unhandled `httpx` exception reaching the wizard or the RPC dispatcher. There is
   no dedicated "unavailable" error code in `server/errors.py` (`ERROR_CODES` is a fixed, small
   set per the wire contract), so `INTERNAL` is used — the same code every other browser-login
   failure already raises.

5. **The wizard's `_ask_for_key` never lets a login exception fall through.** Picking option
   "2=browser login" now runs inside a `while True` loop around the authentication-method
   prompt: an `RpcError` or any other `Exception` from `_run_sync(auth_web.login(...))` prints
   `login failed: <message>` (plus, for a `403`, a one-line hint pointing at retrying, an API
   key, or the OAuth-token option) and loops back to the same three-option prompt instead of
   returning/crashing. `KeyboardInterrupt`/`EOFError` — at the authentication prompt, during the
   login itself, or at the final API-key prompt — leave the vendor unconfigured and return, so
   Ctrl+C/EOF fall through to the rest of the wizard instead of killing the process. The
   non-interactive path (`snowpea setup --login <vendor>` → `cli_commands.provider_login`)
   already prints a message and returns a non-zero exit via `_fail()` without a traceback; that
   path is untouched.
