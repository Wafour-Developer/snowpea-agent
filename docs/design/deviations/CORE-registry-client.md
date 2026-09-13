# Deviations — CORE-registry-client (the hosted skill registry, v0.3)

Against `docs/design/m6-m7-skills-agents-contract.md` §1 (the `snowpea.ai`
registry placeholder) and `snowpea-registry`'s
`docs/design/registry-contract.md` v0.3. Recorded here rather than appended to
the contract, per the lead's note about concurrent edits losing sections.

1. **`registry_client.CLIENT` is no longer a stub.** `HttpRegistryClient`
   talks to `REGISTRY_URL = "https://registry.snowpea.ai/v1"` by default, with
   `search`, `resolve`, `download`, `publish` and `rate`. `search` never
   raises — an unreachable registry contributes nothing to
   `marketplace.search`'s aggregated result, same as the other three sources —
   but `publish` and `rate` do raise `RegistryError`, because those are CLI
   commands looking straight at a terminal, not a background search.

2. **Base URL and token resolution both go through the same three-tier
   order.** `resolve_url(explicit, settings)` is `--registry` >
   `SNOWPEA_REGISTRY_URL` > `settings.skills.registry.url` > the built-in
   default; `resolve_token(explicit, settings)` is the same shape for
   `--token` / `SNOWPEA_REGISTRY_TOKEN` / `settings.skills.registry.token`.
   `Settings` gained `SkillsSettings.registry: SkillRegistrySettings {url,
   token}` — both `None` by default, meaning "use the default", not "no
   registry". `config/hot_reload.rebind` calls
   `registry_client.configure_client(settings)` on every settings reload, so
   editing `skills.registry.url` in `settings.json` retargets `skill.search`
   without a daemon restart, the same way a changed provider key already did.

3. **The aggregated search source label per hit is the registry's own
   `sourceLabel` (or `source`), not a fixed constant.** Superseded by the
   federation work below (§9): a single call to the registry now mixes local
   and mirrored/live hits from several hubs, so one fixed `snowpea-registry`
   label per call stopped being accurate. `marketplace.SOURCE_REGISTRY` is
   kept only as the fallback for an item with neither field, and as the label
   for "the whole registry is unreachable."

4. **`skill.install registry:<id>` (later widened to any registry-issued
   spec, §9) is a fourth install form,** alongside a
   local path, a git URL and `<marketplace>/<plugin>`. It downloads
   `GET /v1/skills/<id>/download` and unpacks the zip under
   `$SNOWPEA_HOME/plugins/<id>`. Two safety nets that are not in the contract:
   a size cap on the HTTP response (`registry_client.MAX_DOWNLOAD_BYTES`, 20
   MB) and a second cap on what the zip expands to
   (`marketplace.MAX_EXTRACTED_BYTES`, 100 MB, against a zip bomb hiding
   behind a small download). Every entry path is resolved and checked against
   escaping the target directory before it is written — no absolute path, no
   `..`, no NUL — mirroring the registry's own upload-side validation
   (`registry-contract.md` §3) on the download side, since a compromised or
   malicious registry is exactly the threat model path-safety exists for.
   The registry's own `<id>/<path>` wrapping directory is stripped so the
   result lands in `plugins/<id>/...` rather than `plugins/<id>/<id>/...`.

5. **`snowpea skill publish <dir>` and `snowpea skill rate <id> <stars>` are
   new CLI subcommands that never touch the daemon.** Publish and rate are
   direct-to-registry HTTP calls (`Settings.load` for the token/URL, then
   `HttpRegistryClient`), not RPC methods — the contract's `skill.*` RPC
   surface stays `search|install|list|reload|remove`. `publish` validates
   `SKILL.md`'s frontmatter locally first (`skills/publish.py`: name matches
   `^[a-z0-9][a-z0-9._-]{1,63}$`, description is 8-500 chars — the same rules
   the registry enforces server-side, per `registry-contract.md` §3) so a
   typo is a local, immediate error rather than a round trip. It then zips the
   directory (junk excluded: `.git`, `__pycache__`, `node_modules`,
   `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.pyc`/`*.pyo`/`*.swp`/`~`)
   and `POST`s it with `Authorization: Bearer <token>`. `--source registry`
   on `snowpea skill search` filters the CLI's own printed/JSON output to
   `snowpea-registry` hits client-side; there is no new RPC parameter.

6. **`/skill publish <dir>` in a running session forwards to the same
   `skills/publish.py` + `registry_client` code** the CLI uses
   (`commands/skill_cmd.py::publish_skill`), resolving a relative directory
   against the session's `workdir`. It reads the token from `core.settings`
   only — a session has no terminal to prompt on, so a missing token is a
   plain "no token configured" message naming the three ways to set one,
   rather than a `getpass` prompt.

7. **The setup wizard's Tools screen (④) gained one more question:** an
   optional, masked "skill registry publisher token" prompt
   (`_ask_for_registry_token` in `setup/wizard.py`), shown after the tool
   category screen in every interactive Full run and skipped (kept as-is)
   non-interactively — the same "saved — Enter to keep" / "optional, Enter to
   skip" pattern as the search-provider key prompt. `WizardState` gained
   `registry_token` and `has_saved_registry_token`; `write()` only overwrites
   `settings.skills.registry.token` when something was actually entered.

8. **What v0.3 does not add here either** (mirroring `registry-contract.md`
   §5): no delete/yank from the CLI (`npm run admin -- skill remove` on the
   registry side only), no full-text ranking beyond the registry's own `LIKE`
   scan, and no offline cache of a previous search — an unreachable registry
   is reported as unreachable, not answered from a stale copy.

## Follow-up — the registry federates other hubs (v0.3-alpha, `registry-contract.md` §4b)

The registry started indexing other skill hubs alongside its own; agentskills.io
turned out to have no skill-listing API (it is the spec/docs site) and
hermes-hub.ai does not resolve at all, both verified 2026-09-13. This second
pass removes the core's guessed adapters for those two and drives the real
federated API instead.

9. **`marketplace.py`'s aggregated search is now two sources, not four:** the
   local `claude-marketplace` scan (unchanged) and the hosted registry, itself
   a federation. `search_agentskills`, `search_hermes_hub`, `_search_endpoint`,
   `AGENTSKILLS_ENDPOINT` and `HERMES_ENDPOINT` are gone from
   `skills/marketplace.py` — there was never a real endpoint behind either,
   only a documented guess. `SOURCE_AGENTSKILLS`/`SOURCE_HERMES` are gone too;
   `SOURCE_CLAUDE` and `SOURCE_REGISTRY` (now a fallback label, not a fixed
   per-call one — see point 3) remain.

10. **`HttpRegistryClient.search` calls `GET /v1/skills?q=&sources=all&live=1`**
    with its own short budget (`SEARCH_TIMEOUT_SEC = 6.0`, independent of
    `REGISTRY_TIMEOUT_SEC` used by download/publish/rate — a federated fan-out
    can be slower than a plain lookup, but must not hold up the whole
    aggregated search for long). `search(query)` keeps its old, narrower
    signature (`list[dict]`, never raises) for anything that only needs
    results; the richer `search_with_sources(query) -> (results, hub_failures)`
    also surfaces the response's per-hub `unavailable` array. `marketplace._hosted`
    prefers `search_with_sources` when the client exposes it (duck-typed, same
    pattern as `install()`'s `hasattr(client, "download")` check) and turns
    each hub failure into its own `"<label>: <reason>"` line, so a `live=1`
    fan-out that lost one hub reads as "that hub is down", not "nothing
    matched" or "the registry is down."

11. **Every hit's displayed `source` is now `item["sourceLabel"] or
    item["source"]`, read per item** in `marketplace._hosted`, not a single
    label applied to the whole call — a federated response mixes `local`,
    `clawhub` and `claude-marketplaces` hits in one page. `SkillHit.source`
    (and therefore `skill.search`'s and `snowpea skill search`'s `source`
    column) now shows e.g. `ClawHub` or `Claude marketplaces` for those hits.

12. **`skill.install` gained a generic external-scheme resolver,**
    `marketplace._has_external_scheme(spec)`: true for anything shaped like
    `<alnum-scheme>:<rest>` that is not `http(s)://` or scp-style
    `git@host:path` (so it never misfires on those, or on
    `<marketplace>/<plugin>` — checked in that order: git-url check first,
    external-scheme check second, "/" marketplace heuristic last, since a spec
    like `clawhub:@cua/driver` contains a `/` too and must not be parsed as
    `<marketplace>/<plugin>`). A matching spec goes to `_install_from_registry`,
    which calls `HttpRegistryClient.download(spec)` — the *whole* spec, not
    just an id after a prefix — and percent-encodes it (`safe=""`) unless it
    is `registry:<id>`, matching the registry's own id scheme
    (`/v1/skills/clawhub%3A%40cua%2Fdriver/download`). A 501 response raises
    the new `RegistryNotFetchable(RegistryError)`; `_install_from_registry`
    catches it and tries `_fallback_install(spec, plugins_dir)`, which only
    knows one fallback — `github:<owner>/<repo>[@plugin]` clones
    `https://github.com/<owner>/<repo>.git` directly (the `@plugin` subdir
    hint is not extracted, the same simplification the existing git-url
    `#subdir` form already made, so the cloned directory is named after the
    repo). Any other scheme with no client-side fallback re-raises as
    `InstallError` naming the registry's 501 reason.

13. **`marketplace._name_from_spec(spec)`** names the plugin directory for a
    *successful* registry download (as opposed to the git-clone fallback
    above): `registry:ralplan` -> `ralplan`, `clawhub:@cua/driver` -> `driver`
    (last path segment), `github:owner/repo@plugin` -> `plugin` (what the
    registry actually zipped, per the monorepo convention), `github:owner/repo`
    with no `@` -> `repo`.

14. **`snowpea skill sources` / `/skill sources`** are new, read-only,
    direct-to-registry commands (`HttpRegistryClient.sources()` ->
    `GET /v1/sources`) printing each hub's id, label, enabled/disabled state
    (with `disabledReason` when off), skill count and last sync error. Neither
    goes through the daemon's RPC surface, same as `publish`/`rate`.

15. **Fixture update.** `tests/fixtures/marketplace/{agentskills,hermes}.json`
    are gone; `tests/test_plugin_load.py`'s `FixtureFetcher` (which served
    them over the removed adapters) is replaced by `FixtureRegistryClient`, a
    stand-in for `registry_client.CLIENT` backed by
    `tests/fixtures/marketplace/registry.json` — two of the fixture's three
    "pdf" hits (`pdf-extract`, `pdf-ocr`) now arrive through it, each tagged
    with a distinct `sourceLabel` (`ClawHub`, `Claude marketplaces`) the way a
    real federated response would, instead of through per-adapter endpoints
    that no longer exist.
