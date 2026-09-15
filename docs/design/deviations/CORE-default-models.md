# Deviations — live default models in the setup wizard and `setup.catalog`

Against the CORE-default-models brief. The wizard's vendor list showed
`VendorPreset.default_model`, a string frozen at release time, so an install
a few months old greeted people with nine superseded flagships.

## What was built

`core/snowpea_core/providers/default_models.py` is the whole answer:
`default_model_for(vendor)` returns a `DefaultModel(vendor, model, source)`
with `source` one of `live`, `models.dev`, `preset`. `newest_catalog_model`
is its pure core — a models.dev document in, one id out, no I/O — and is what
the tests drive. `vendor_catalog` gained `default_model` and
`default_model_source`; `setup.catalog` carries them as `defaultModel` and
`defaultModelSource` (additive, regenerated with `scripts/gen_protocol.py`).

## Deviations

1. **models.dev has no tier field, so "flagship" is read off the id.** The
   brief says to prefer the flagship tier "if the data marks it". It does not:
   a model entry carries `family`, `release_date`, `status`, `cost` and
   `limit`, and nothing that says big-or-small. `is_flagship` therefore treats
   an id as a flagship unless one of its dash-separated segments is a tier word
   (`flash`, `lite`, `mini`, `nano`, `turbo`, `air`, `haiku`, `highspeed`, …).
   This is spelled out in `TIER_WORDS` rather than hidden in a regex, because
   it is a list that will need editing when a lab invents a new suffix.

2. **Flagship preference is bounded by a 60-day window, not absolute.** Ranking
   flagship-first outright picks a stale model: Google's newest chat model is
   `gemini-3.8-flash`, and the newest non-`flash` id in their catalog is
   `gemini-2.5-pro` from June 2025 — fifteen months older, and not what anyone
   means by the current default. Ranking by date alone is wrong the other way:
   labs ship a family's small variant weeks after the big one (`glm-5.3` on
   2026-08-14, `glm-5.3-flash` on 2026-08-26), so a pure date rule flips the
   default to the mini model for those weeks. `FLAGSHIP_WINDOW_DAYS = 60`:
   within that much of the newest release a flagship wins, outside it the date
   does. Gemini has no flagship in the window and correctly keeps the flash.

3. **`preview` / `deprecated` is a demotion, not a filter.** A vendor whose
   whole catalog is marked beta would otherwise have no default at all, so an
   unstable id wins when it is all there is. `status` covers what models.dev
   marks; the rest (`-preview`, `-exp`, `-rc1`) is read out of the id, since
   every lab spells it there and models.dev does not normalise it.

4. **Two vendors are namespace-restricted (`MODELS_DEV_ID_PREFIX`).**
   OpenRouter resells every lab, so "the newest id models.dev lists for
   openrouter" is whoever shipped last — `sakana/fugu-max`, on the day this was
   written. The preset has always pointed at the Anthropic namespace, so the
   pick stays inside `anthropic/`. Alibaba's catalog carries GLM and DeepSeek
   models beside Qwen's own, so `qwen` is restricted to ids starting `qwen`.

5. **A live listing is re-ranked by models.dev rather than taken in order.**
   `/v1/models` answers in no useful order — OpenAI's is effectively arbitrary
   — so "the first id the account lists" is not a default. When models.dev
   describes ids that the account also lists, the same rule as rung 2 orders
   them. When it describes none of them (a Codex backend's slugs, a self-hosted
   server) the listing's own order stands, because *that* order is meaningful:
   the Codex backend sorts by priority.

6. **`vendor_catalog` stayed synchronous.** The CLI wizard calls it from
   synchronous code and the RPC handler from async code. Rather than split it,
   it takes an optional `defaults` mapping: without one, every row falls back
   to `default_model_offline`, which reads the models.dev document already
   cached on disk and asks no one anything. `vendor_default_models` is the
   async resolver both surfaces call to fill that mapping in, and the wizard
   reaches it through its existing `_run_sync` bridge.

7. **Only vendors with an *active* credential are probed.** `auth_status`,
   not `is_configured`: an expired OAuth session is configured and would buy a
   guaranteed 401 plus the delay. Probes run concurrently under one budget
   (`DEFAULT_MODEL_TIMEOUT = 2 s`, with the gather capped at twice that), so
   the screen costs about as long as the slowest single vendor.

8. **A local-style vendor gets no default row.** A self-hosted server serves
   whatever was loaded into it and the preset's `local-model` is a placeholder
   that returns HTTP 404. Those rows keep showing their `base_url`, which is
   the useful line, and `defaultModel` is empty rather than a lie.

9. **`models.py` gained `models_dev_document`.** The curated rung's
   `models_dev_catalog` returns ids only; picking a default needs the metadata
   beside them. Same cache file, same TTL, same `SNOWPEA_MODELS_DEV=0` switch.
   The cache is `$SNOWPEA_HOME/cache/models-dev.json` — the brief named
   `~/.snowpea/models_dev_cache.json`, which is not a path this project has
   ever written.

10. **`STATIC_WINDOWS` gained the new families.** Changing the presets without
    this would have left the HUD showing `?` for nine of eleven vendors from
    the first turn, since the table is prefix-matched and knew nothing of
    `glm-5`, `gpt-6`, `kimi-k3`, `deepseek-v4`, `qwen3.8`, `minimax-m3`,
    `grok-4.6` or the 1M-token Claude 5 family. Every number is the `limit.context`
    models.dev publishes for that model. Older prefixes are untouched.

## The preset strings, and where each came from

The preset is now only the offline last resort, but it is still what a cold
machine shows. Every id below was read out of the models.dev document cached at
`~/.snowpea/cache/models-dev.json` (`saved_at` 2026-09-15); none was invented,
and each was checked to exist under the provider id this project already maps
the vendor to in `MODELS_DEV_IDS`.

| Vendor | Was | Now | Source |
| --- | --- | --- | --- |
| anthropic | `claude-sonnet-4-5` | `claude-sonnet-5` | named in the brief; present in models.dev `anthropic`, released 2026-06-29 |
| openai | `gpt-4.1` | `gpt-6-astra` | newest chat model in models.dev `openai`, 2026-09-04 |
| openrouter | `anthropic/claude-sonnet-4.5` | `anthropic/claude-sonnet-5` | mirrors the Anthropic choice; present in models.dev `openrouter`, 2026-06-30 |
| gemini | `gemini-2.5-pro` | `gemini-3.8-flash` | newest chat model in models.dev `google`, 2026-09-02 — the catalog has no newer `pro` |
| xai | `grok-4` | `grok-4.6` | newest flagship in models.dev `xai`, 2026-08-12 |
| glm | `glm-4.6` | `glm-5.3` | newest flagship in models.dev `zai`, 2026-08-14 (`glm-5.3-flash` is newer and is the cheaper tier) |
| minimax | `minimax-m2` | `MiniMax-M3` | newest in models.dev `minimax`, 2026-06-01 — note the casing, which is the id the catalog publishes |
| kimi | `kimi-k2` | `kimi-k3` | newest in models.dev `moonshotai`, 2026-07-16 |
| deepseek | `deepseek-chat` | `deepseek-v4-pro` | newest flagship in models.dev `deepseek`, 2026-08-12; `deepseek-chat` is no longer in that catalog at all |
| qwen | `qwen3-max` | `qwen3.8-max` | newest flagship in models.dev `alibaba`, 2026-08-03 |
| local | `local-model` | `local-model` | unchanged: a placeholder, replaced by discovery against the server |

The `models=(…)` tuples were updated the same way and every id in them was
checked against the same document: `claude-opus-5` and
`claude-haiku-4-5-20251001`; `gpt-5.6` and `gpt-5.4-nano`;
`openai/gpt-6-astra`; `gemini-3.7-flash` and `gemini-flash-latest`;
`grok-4.5`; `glm-5.3-flash`; `MiniMax-M2.7`; `kimi-k2.7-code`;
`deepseek-v4-flash`; `qwen3.8-flash`. Two ids the old tuples carried —
`moonshot-v1-128k` and `deepseek-reasoner` — were dropped rather than kept
unverified: models.dev no longer lists either under the vendor's provider id.

## Tests

`tests/test_default_models.py`: the pure pick (newest wins, preview loses to a
stable sibling, preview wins when alone, flagship beats a newer sibling inside
the window and loses outside it, the OpenRouter namespace); precedence
live → models.dev → preset against a fake models.dev payload; the timeout
fallback; live re-ranking; the offline rung; the catalog's two new fields and
its tag; and a preset table kept honest — every default is the head of its own
`models` tuple, none of the nine superseded ids can come back, and every
default has a known context window.
