# Deviations — CORE-gemini-oauth (Google ADC login, pasted OAuth tokens, the wizard's auth choice)

Gemini users who have a Google account but no API key had no way in. `93be684`, `22df464` and
`8d3ca31` add two non-key auth methods — Google Application Default Credentials via `gcloud`, and a
pasted OAuth token — and teach the setup wizard to offer them. Recorded here per
`docs/design/deviations/README.md`.

1. **Google's OAuth flow is delegated to `gcloud`; snowpea never implements it.** `google_adc_start`
   / `google_adc_login` (`providers/auth_web.py:623-…`) shell out to
   `gcloud auth application-default login`, and `gemini_native._oauth_token()` shells
   `gcloud … print-access-token` per request. Google owns browser consent, refresh-token storage and
   refresh; snowpea persists exactly `{"auth_method": "google_adc"}` and never sees the refresh
   token. Implementing the flow in-process would have meant holding a Google refresh token in
   `settings.json` for the lifetime of the install — a much larger secret than an API key, with a
   revocation story snowpea does not own.

2. **A missing `gcloud` raises `LOGIN_UNSUPPORTED` with an API-key hint; there is no fallback.**
   `auth_web.py:631-637`. The alternative — attempt a home-grown flow when the CLI is absent — would
   have put the code path from rule 1 back in the tree as a rarely exercised backup, which is the
   worst place for credential handling to live.

3. **`_client()` became async so the bearer can be fetched per request.** `providers/gemini_native.py`
   sets `Authorization: Bearer <token>` from either the live `gcloud` lookup (`auth_method ==
   "google_adc"`) or the stored `oauth_token`. A token cached at construction time would go stale
   inside a long-lived daemon, and the ADC token is short-lived by design.

4. **`remember_current_provider` writes one credential and clears the others — with one gap.**
   `setup/state.py:145-159`: setting `api_key` pops `oauth_token` and `auth_method`; setting
   `oauth_token` pops `api_key` and stamps `auth_method="oauth_token"`. The `auth_method`-only branch
   is an `elif`, so choosing `google_adc` on a vendor that already has a saved `api_key` leaves that
   key in the block. It is unreachable from the provider check (`registry.py:157` prefers the
   declared `auth_method`) but it is a stale secret on disk, and it is recorded here rather than
   described as a clean switch.

5. **OpenAI accepts `oauth_token` as a bearer too, widening the field beyond Gemini.**
   `providers/registry.py:157-159, 311-329` treats a non-empty `oauth_token` as the API key for the
   OpenAI-compatible path, and `presets.py` lists `oauth_token` in the `auth_methods` of both vendors.
   A field named for one vendor that only works for that vendor is a trap for the next one; the
   `provider.configure` allowlist (`server/app_server.py:417-449`) was extended once, for both.

6. **`snowpea provider login <vendor> --token [TOKEN]` prompts with `getpass` when the value is
   omitted**, and is restricted to `openai|gemini` (`cli/commands.py:271-310`). An optional-value flag
   was chosen over a positional argument so the token never has to appear in a shell history.

7. **The two gaps this work originally left are closed at HEAD.** As shipped, `oauth_token` was in
   neither secret-masking allowlist and `settings.json` was written with default permissions even
   though it now held OAuth tokens. Both were fixed under CORE-fixes-v017: `oauth_token`/`oauthToken`
   are in the shared `SECRET_KEYS` set (`config/patch.py:15-29`), used by both the `settings.get`
   /`settings.set` handlers and the settings tools so the two masking paths cannot drift
   (`server/settings_handlers.py:54-61`), and `Settings.save()` writes the file `0600`
   (`config/settings.py:315-322`).

8. **The wizard offers a numbered choice only when a vendor has more than one auth method.**
   `setup/wizard.py:251-298`: 1 = API key, 2 = browser login, 3 = OAuth token. Browser login runs
   `auth_web.login()` synchronously inside the wizard rather than handing the user back to a daemon
   RPC, because the wizard is the one place where blocking until the browser round-trip finishes is
   the desired behaviour. The token is read through `ui.ask_text(..., secret=True)`.

9. **The third commit in this group, `8d3ca31`, is documentation only.** It corrects `README.md` to
   say that OpenAI's device-code login authenticates a ChatGPT/Codex *subscription* — a different
   thing from an API key, and the distinction users were getting wrong. No code changed.
