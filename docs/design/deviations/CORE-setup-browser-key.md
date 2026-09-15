# CORE-setup-browser-key — the browser screen asks for the credential it needs

## The bug

`snowpea setup` let a user pick a `paid · key required` browser provider and
then moved straight on. The provider name was written to `browser.provider`,
no credential was ever asked for, and nothing said so. The choice could not
work, and the first `browser_navigate` was where the user found out.

The search screen already had this right (`_ask_for_search_key`, CORE-search-fix).
The browser screen was the half that never got it.

## The slot, and why this file exists

**`browser.credentials.<id>`** is where a browser credential lives. Not
`browser.providers.<id>`. It mirrors `search.credentials.<id>` exactly, which is
the shape `SearchSettings` already used and the one `BrowserSettings` already
declared but nothing wrote to.

```json
{
  "browser": {
    "provider": "browserbase",
    "credentials": {
      "browserbase": { "api_key": "...", "browserbase_project_id": "..." }
    }
  }
}
```

Both halves of the contract now exist in code, so a surface never has to guess:

| Half | Where |
|---|---|
| writes it | `WizardState.set_browser_key` / `set_browser_value`, flushed in `WizardState.write` |
| reads it | `browser_providers.credentials_for(id, settings)` |
| asks "is it usable" | `browser_providers.configured(id, settings)` |

`credentials_for` returns a flat `dict[str, str]`: `api_key`, `base_url`, and
one lowercased entry per extra declared variable. Settings win over the
environment, because settings are what the user just typed.

**A desktop wizard writes the same slot** through `settings.set` with the key
path `browser.credentials.<id>.api_key`, or through `setup.apply` with the same
state fields. There is no second path and no per-surface shape.

## Providers that need more than one value

`BrowserProviderMeta.env` is a tuple, and Browserbase declares two:
`BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`. Asking only for the first
saves half a credential, which fails on the half nobody was asked for.
`browser_providers.extra_envs(id)` names everything past the first, the wizard
loops over it, and `configured()` requires all of them.

The extra values are **not masked**: a project id is an identifier, and hiding
it while the user pastes it helps nobody.

## The probe is a courtesy, never a gate

| Provider | Probe |
|---|---|
| `browserbase` | `GET https://api.browserbase.com/v1/sessions` with `x-bb-api-key` |
| `firecrawl_cloud` | `HEAD https://api.firecrawl.dev/` |

Three-second budget. Only `401`/`403` and `5xx` produce a warning, and the
credential is saved either way — an offline machine, a corporate proxy or a
provider having a bad afternoon must not cost the user the key they just
pasted. Firecrawl's probe reaches the host rather than authenticating, because
it has no free authenticated endpoint; the warning text says what was checked.

## Non-interactive path

```bash
snowpea setup --browser-provider firecrawl_cloud --browser-key fc-your-key
```

`--browser-key` without `--browser-provider` is a usage error, matching
`--search-key`. A `--browser-provider` with no key in an interactive run still
prompts: the flag answers the *screen*, not the *credential*.

## In-session `/setup` and `/login`

The same plans run through the **question queue** so a key can be typed without
leaving the session (`commands/setup_cmd.py`). What made that safe is one
additive protocol field:

```
QuestionItem.secret: bool = False
```

A surface MUST mask a `secret` free-text answer while it is typed, MUST NOT
echo it into the transcript and MUST NOT log it. The TUI draws one `•` per
character — the length is kept because a paste that arrived short has to look
different from one that worked — and masks it in the review tab of a batch too.
A URL or a project id is asked in the clear; neither is a credential.

`setup/credentials.py` is the seam that makes this one feature rather than two.
It decides *what* to ask, whether each answer is a secret, what the hint says
and where the value goes; the asking is a callback. The CLI wizard passes one
that reads masked terminal input, `/setup` passes one that puts the same
request to a surface. Neither knows anything the other does not, so the browser
key fix landed in both at once.

Both commands refuse when `can_ask` is false — unattended, or a delegated child
— because a credential prompt with nobody there either hangs until the timeout
or takes silence for an answer.

## Where it lives

`core/snowpea_core/tools/browser_providers/__init__.py` (`needs_key`,
`credential_env`, `extra_envs`, `credentials_for`, `configured`),
`core/snowpea_core/setup/{state,wizard,credentials}.py`,
`core/snowpea_core/commands/setup_cmd.py`,
`tui/src/components/QuestionPrompt.tsx`,
`core/snowpea_core/setup/screens/browser.py`,
`core/snowpea_core/cli/commands.py`. Tests: the browser-key block at the end of
`tests/test_setup_wizard.py`, `tests/test_setup_command.py`, and the
"a secret answer" block in `tui/test/question.test.tsx`.
