# Deviations — CORE-settings (`settings.get` / `settings.set` / `setup.catalog`)

Additive protocol work for the IDE setup wizard: two RPC methods to read and merge-patch settings
at global or project scope, and one to hand the IDE the same five catalogs the CLI setup wizard
renders. Recorded here per the deviations-log convention (`docs/design/deviations/README.md`).

1. **No new pydantic models for the settings documents themselves.** `SettingsResult.settings` is
   `dict[str, Any]`, not a typed mirror of `Settings`/`ProjectSettings`. Both of those models
   already use `ConfigDict(extra="allow")` and are meant to grow fields without a protocol change;
   a typed wire model would have needed updating (and a protocol bump) every time either settings
   model gained a field, which defeats the point of the "additive, no contract edits" instruction.
   The dict is still fully validated server-side — `settings.set` round-trips the merged dict
   through `Settings.model_validate` / `ProjectSettings.model_validate` before persisting, so a
   bad patch is `invalid_params`, never a corrupt file.

2. **`settings.set` deep-merges onto the *current* document, not onto an empty one.** A patch like
   `{"agents": {"max_concurrent": 7}}` only touches `agents.max_concurrent`; every sibling field
   (`agents.*` other keys, and every other top-level section) is preserved. The merge
   (`settings_handlers._deep_merge`) recurses into nested dicts and replaces lists/scalars
   wholesale — there is no list-merge heuristic, since none of the current settings fields (lists
   of allowlist entries, search credentials, etc.) have an obvious element-wise merge semantics.

3. **Secret masking is a response-only transform, applied after persistence.** `_mask_secrets`
   walks the dict recursively and replaces any value under a key named `api_key`, `token`,
   `refresh_token` or `password` with `"***"` — this runs after `save()`, so the file on disk
   always has the real value; only the RPC result is redacted. `settings.get` masks the same way.
   No existing model marks fields as secret (there is no pydantic `SecretStr` in `MediaMcpSettings`
   or `SearchSettings.credentials`), so the mask is name-based rather than schema-based; this
   matches the task's literal instruction ("mask any key named ...") rather than inventing a
   secret-field annotation convention that would ripple into unrelated models.

4. **`workdir` is an explicit parameter, not derived from the connection's session.** The existing
   `allowlist.*` handlers infer a project workdir from the calling connection's most recent
   session (`app_server._workdir_for`). `settings.get`/`settings.set` take `workdir` directly
   instead, because the IDE wizard calls these before a session necessarily exists for that
   project (first-run setup), and the task explicitly specifies `workdir?: str` on both methods.
   `scope: "project"` without `workdir` is `invalid_params`.

5. **`setup.catalog` reuses `setup/catalog.py` verbatim; `SetupCatalogItem.tags` is the catalog's
   computed `CatalogItem.tags` property, not re-derived on the wire side.** This lets the IDE
   render the same "free · no key" / "active" pills the CLI wizard's `screens/*.py` already draw,
   without re-implementing `CatalogItem.tags`'s logic in TypeScript.
   `vendor_catalog(core.settings)` is passed the live global `Settings` (not the settings-free
   default) so a vendor already configured (env var or `settings.providers`) shows `active: true`,
   matching what `snowpea setup` shows when run from the same machine.

6. **`core.settings` is replaced, not mutated, after a global `settings.set`.** The handler builds
   a new validated `Settings` instance from the merged dict and assigns it to `core.settings`
   rather than mutating the existing instance's fields one by one — pydantic models here are not
   used as mutable containers elsewhere in the codebase (`Settings.load`/`.save` also treat the
   whole document as replace-in-place), so this keeps `core.settings` internally consistent instead
   of partially updated if a later field failed validation.

Verification run at the time of this change:

- `uv run pytest tests/test_settings_rpc.py tests/test_rpc_roundtrip.py -q` — 18 passed, 1 skipped
  (`test_rpc_roundtrip.py::...` skip is the pre-existing "every no-argument method is implemented"
  case, unrelated to this change).
- `uv run pytest -q` (full suite) — 431 passed, 4 skipped (pre-existing skips: `shellcheck` not
  installed, one Anthropic-SDK-framing test, the same roundtrip skip above).
- `uv run ruff check core tests` — all checks passed.
- `uv run mypy core` — no issues found in 134 source files.
- `uv run python scripts/gen_protocol.py --check` — `sdk/src/protocol.ts` and `docs/protocol.md`
  both `ok` (regenerated and committed alongside the protocol change).
- `npm -w sdk run build` — `tsc` clean.
- `npm -w sdk test` — 6 passing (unrelated to this change; confirms the SDK build did not regress).
