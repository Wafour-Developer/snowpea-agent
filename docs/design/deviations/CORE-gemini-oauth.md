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

---

## Amendment — the in-process Google browser login (CORE-codex-login, phase B)

Rules 1 and 2 above said snowpea would never implement Google's OAuth flow itself: `gcloud` owned
consent, storage and refresh, and a missing CLI was a hard `LOGIN_UNSUPPORTED`. That is no longer
true, and the reason it changed is the one the original rule did not cover — **a user with a Google
account, a browser, and no `gcloud` had no way in at all**, which is the majority case on a laptop.
The ADC path is kept, unchanged, as an explicit alternative and as the headless default.

10. **`providers/google_oauth.py` runs the Gemini CLI's flow in-process.** Authorization code + PKCE
    against `https://accounts.google.com/o/oauth2/v2/auth`, exchange and refresh at
    `https://oauth2.googleapis.com/token`, scopes `cloud-platform` + `userinfo.email` +
    `userinfo.profile`, callback on an **ephemeral** loopback port (Google accepts any, unlike
    OpenAI's fixed 1455) bound and advertised as the `127.0.0.1` literal, because `localhost` can
    resolve to `::1` first and miss the socket. `access_type=offline` with `prompt=consent` is what
    makes Google return a refresh token at all; without both, the session dies in an hour with no
    way back.

11. **The client id and secret are the public ones shipped in `@google/gemini-cli`.** An installed
    application's "secret" is not a secret — OAuth's public-client profile assumes anyone holding the
    binary can read it, which is why PKCE, not the secret, is what protects the exchange. They are
    in the source with that explanation attached, rather than looking like a leaked credential.

12. **Rule 1's objection is answered, not ignored.** Snowpea now does hold a Google refresh token in
    `settings.json` — mitigated by what landed in between: `SECRET_KEYS` masks `access_token` /
    `refresh_token` / `id_token` on every path out of the daemon (`config/patch.py`), and
    `Settings.save()` writes the file `0600`.

13. **`providers/gemini_codeassist_transport.py` is the transport an OAuth account needs.** A Google
    login is not an API key, so `generativelanguage.googleapis.com` cannot serve it;
    `cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse` can. The wire format
    inside is the ordinary Gemini one, wrapped — request `{model, project, request: …}`, chunks
    `{"response": …}` — so `build_gemini_request` and `GeminiStreamNormalizer`, the code the API-key
    adapter already uses, do the translation here too. Only the envelope is new.

14. **Free-tier accounts are onboarded, once.** `:loadCodeAssist` either names the managed project or
    `:onboardUser` provisions it as a long-running operation that is polled; a free tier must *omit*
    `cloudaicompanionProject`, because naming a project the user does not own is what fails with
    `PERMISSION_DENIED`. The resulting `project_id` is written back to `settings.json` through
    `on_credentials`, so it is discovered once rather than once per turn.

15. **Rule 4's gap is closed.** Choosing an OAuth method no longer leaves a stale `api_key` in the
    block: the wizard clears credentials before writing fresh ones, login results carry explicit
    `{"api_key": None}` (which `configure()` treats as *remove*), and `api_key_for()` refuses to
    return a key at all when `auth_method` names an OAuth flow. Rule 5 (`oauth_token` as a bearer for
    both vendors) is unchanged and still the paste-a-token path.
