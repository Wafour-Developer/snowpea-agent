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

3. **The aggregated search source is labelled `snowpea-registry`, not
   `snowpea.ai`.** The contract's §1 sketch used the bare domain; the actual
   registry item schema already carries `"source": "snowpea-registry"` inside
   each hit's own JSON, so the marketplace-side label was renamed to match
   rather than carry two different names for the same thing.
   `marketplace.SOURCE_REGISTRY` is the constant.

4. **`skill.install registry:<id>` is a fourth install form,** alongside a
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
