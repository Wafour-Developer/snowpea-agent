# Deviations — CORE-codex-login (ChatGPT browser login, the Codex transport, and the OAuth lifecycle)

M3 §3 describes OpenAI's interactive login as *device code*, and §2 routes every OpenAI turn through
`providers/openai_compat.py`. Both are wrong for the credential that login actually produces. A
device-code (or browser) sign-in authenticates a **ChatGPT subscription**, and `api.openai.com`
rejects a subscription token from the very first request — so the vendor's own backend has to be
spoken to instead. The same shape of problem, and the same shape of fix, applies to Gemini; its
half is recorded in [`CORE-gemini-oauth.md`](CORE-gemini-oauth.md) §10-15.

Recorded here per `docs/design/deviations/README.md`.

1. **The browser flow is the default for OpenAI; device code became the headless fallback.**
   `providers/openai_oauth.py` implements OAuth 2.0 authorization-code + PKCE (S256) against
   `https://auth.openai.com/oauth/authorize`, exactly as the Codex CLI does: client
   `app_EMoamEEZ73f0CkXaXp7hrann`, scopes `openid profile email offline_access`, and the query
   parameters `id_token_add_organizations=true` / `codex_cli_simplified_flow=true` /
   `originator=codex_cli_rs`. Device code stays, because a machine with no browser still needs a way
   in, and because some accounts are refused by one flow and not the other.

2. **The callback port is fixed at 1455 and a busy port is an error, not a fallback.** OpenAI
   registered exactly `http://localhost:1455/auth/callback` for that client; any other port is
   refused with `invalid_redirect_uri`. Quietly moving to a free port would turn a clear failure
   into an unexplained one, so `CodexCallbackServer` reports the port, names the likely holder
   (another Codex or snowpea sign-in) and points at `--device-code`.

3. **`providers/oauth_callback.py` is shared, and verifies `state`.** Both browser logins needed the
   same one-shot loopback listener. Beyond `auth_web.CallbackServer` (OpenRouter's, left as it was)
   it checks the `state` parameter — without it any page the user happens to have open could feed us
   a code — and answers with an HTML page, because a human is looking at it.

4. **The `id_token` is decoded, never verified.** It arrives over TLS straight from the token
   endpoint and its claims are used to *address* the account (`chatgpt_account_id`,
   `chatgpt_plan_type`), not to authorise anything. Verifying would mean shipping and rotating
   OpenAI's JWKS for no security gain. Structure *is* validated, so a malformed token fails at login
   rather than as an opaque 401 on the first prompt.

5. **Both OpenAI flows now write one credential record.** `credentials_from_tokens` produces
   `{auth_method: "chatgpt", access_token, refresh_token, expires_at, id_token, account_id,
   plan_type}` and `auth_web.device_code_start` calls it too. Two consequences, both deliberate:
   `expires_in` is resolved to an **absolute** `expires_at` at the moment it is received (a bare
   duration with no anchor cannot be checked later — report §6.5), and the device flow finally
   records an `auth_method`, which is what makes a ChatGPT session recognisable at all (§6.7
   A-P2-3). `normalize_stored_credentials` folds a record written by an older install (`token`,
   `expires_in`) into the same shape at read time, so nobody has to log in again.

6. **`providers/codex_transport.py` speaks the Responses API, not `/chat/completions`.**
   `POST https://chatgpt.com/backend-api/codex/responses`, streaming SSE, with `Authorization`,
   `chatgpt-account-id`, `OpenAI-Beta: responses=experimental`, `originator` and `session_id`.
   `store: false`, so the whole conversation is re-sent as `input` every turn: the backend keeps no
   server-side state for us, and pretending otherwise would drop context silently. Snowpea's
   `originator` is `snowpea` (overridable with `SNOWPEA_CODEX_ORIGINATOR`) — OpenAI asks third-party
   harnesses to identify themselves rather than impersonate the CLI.

7. **The Codex backend has no `/models`, so the list is static.** `CODEX_MODELS` is what a ChatGPT
   account may select. `models.oauth_models()` returns it (and Gemini's equivalent) *instead of*
   making a request, which also fixes the unauthenticated discovery call that fired right after a
   successful login and printed `could not list models (… 401 …)` (§6.7 A-P2-1).

8. **`ProviderRegistry` routes on `auth_method`, and `auth_method` now beats a stale key.**
   `_oauth_provider()` returns `CodexProvider` / `CodeAssistProvider` before the API-key path is
   reached. `api_key_for()` returns `None` when the vendor's `auth_method` names an OAuth flow: an
   API key left over from an earlier setup used to outlive the login that replaced it and silently
   win, so the user was told the sign-in worked while every request kept using the old key (§6.7
   A-P1-2). Logins also carry explicit `{"api_key": None, …}`, which `configure()` treats as
   *remove*.

9. **Refresh is reactive *and* pre-emptive, and a failed refresh is its own error code.** A token
   already inside a 60-second skew is renewed before the request; a 401 renews once and retries (the
   401 is detected before any event is yielded, so the retry cannot duplicate output). Renewed
   credentials go back to `settings.json` through an `on_credentials` callback. When the refresh
   itself fails the turn ends with the new `errors.AUTH_EXPIRED` code and a message naming the exact
   command to run — distinct from `invalid_params` so a surface can offer the login instead of
   blaming the request.

10. **`ProviderInfo.authStatus` is additive under protocol 1.4.0.** `active` / `expired` /
    `unconfigured`. `is_configured()` still answers True for an expired session — it is a session
    that needs renewing, not an absent one — and the setup screen stops labelling it `[active]`
    (§6.5 named this as the thing that left users staring at a vendor which 401s on every prompt).

11. **Which flow a machine gets is decided by `auth_web.browser_available()`.** macOS and Windows
    always browse; a Linux session with no `DISPLAY`/`WAYLAND_DISPLAY`, or any session with
    `SSH_CONNECTION`/`SSH_TTY` set, does not. A consent page opened where nobody can see it is worse
    than a printed code. `SNOWPEA_HEADLESS_LOGIN=1`, `--device-code` and an explicit `method` all
    override it.

12. **The wizard menu is generated from the vendor's real flows.** `[1=API key, 2=browser login,
    3=device code (headless), 4=OAuth token (remote/headless)]` for OpenAI, the Google equivalent
    for Gemini, and an out-of-range answer is now rejected with a message instead of silently
    falling through to the API-key prompt (§6.7 A-P3-1).

13. **Two credential bugs the report proved are fixed with their own tests.** `_apply_login` clears
    stale credentials *before* storing the fresh ones — clearing afterwards is what deleted the API
    key OpenRouter's browser login had just minted, leaving the default vendor unconfigured while
    the summary claimed success (§6.7 A-P1-1) — and a pasted OAuth token is checked with one
    authenticated call before it is stored, as a warning rather than a refusal, since the probe can
    fail for reasons that have nothing to do with the token (§6.7 A-P2-2).

14. **`tests/fixtures/providers/openai-codex/` is not part of the replay matrix.** The matrix walks
    the eleven preset vendors; this fixture is a synthetic Responses-API recording consumed directly
    by `tests/test_codex_transport.py`, which is why it sits beside them under a non-vendor name.

15. **Not done here.** No prompt-cache key, no reasoning-summary events (the core's `StreamEvent`
    has no kind for them), and no Codex-side rate-limit *enforcement* — the `x-codex-*` headers are
    captured on the provider and surfaced, but nothing throttles on them yet.
