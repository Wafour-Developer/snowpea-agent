# Deviations — CORE-models (model discovery, `provider.models`, `/model`)

Additive work for the "local vendor 404s on the first prompt" bug: the `local` preset ships the
placeholder model `local-model`, the wizard never asked for a real one, so the first prompt against
a vLLM server came back as
`internal: local: HTTP 404: {"error":{"message":"The model 'local-model' does not exist."}}`.
Recorded here per `docs/design/deviations/README.md`.

1. **`PROTOCOL_VERSION` was not bumped.** The task allowed a minor bump only if the file still read
   `1.1.0`. It already read `1.2.0` (exec-update's in-flight work), so `provider.models` was added
   to `METHODS` and `IMPLEMENTED_METHODS` inside the existing 1.2.0 surface. Both changes are
   additive, so one minor version carries them.

2. **`list_models` takes a `VendorPreset`, not a vendor config dict.** The task offered either. The
   preset is what already carries the adapter kind, the default base URL, the static model list and
   the extra headers, and every caller (`ProviderRegistry`, the wizard) has one in hand. Credentials
   and a base-URL override come in as keyword arguments, which is the same shape the three adapter
   constructors already use.

3. **The lazy auto-pick lives in the adapter, not in `ProviderRegistry.get`.** `get`/`build` are
   synchronous and discovery is not, so making resolution happen there would have meant an async
   `get` and a change at all five call sites (`agent/loop.py`, `agent/team.py`, `agent/subagent.py`
   and two handlers). Instead `build` hands `OpenAICompatProvider` a `model_resolver` coroutine
   whenever the resolved model is a placeholder, and `stream()` awaits `_ensure_model()` before it
   builds a request. An adapter with a placeholder and no resolver raises `model_not_configured`
   rather than calling the server, so `local-model` cannot reach an endpoint by any path.

4. **Only `openai_compat` gets a resolver.** `local` is the one preset whose `default_model` is a
   placeholder, and it is an OpenAI-compatible vendor. The Anthropic and Gemini adapters ship real
   default model ids, so wiring a resolver into them would be dead code. `provider.models` and
   `/model` still work for all eleven vendors.

5. **`ProviderRegistry` learned to persist.** `configure()` documents that writing `settings.json`
   is the caller's job, but the auto-pick has no RPC caller to hand the work to. The registry now
   holds an optional `Paths` (bound in `wire_core`) and a `save()` that writes when one is present
   and silently does nothing when it is not — so a registry constructed in a test or a script keeps
   working exactly as before.

6. **Anthropic listing merges rather than replaces.** `/v1/models` is queried when a key is present,
   and its ids are merged ahead of the preset's static list; a failure returns the static list
   instead of raising. The task described the static list and the endpoint as alternatives.

7. **`/model` accepts a row number.** `/model 2` picks the second id of the listing. The task only
   asked for a name. The numbers are already printed by the bare `/model`, and a local model id such
   as `Qwen/Qwen3-32B` is tedious to retype.

8. **`/model <name>` always persists; there is no `--save` flag.** The task said to keep it simple
   and persist anyway, so the flag would have had no other behaviour to select.

9. **Discovery is skipped under `SNOWPEA_PROVIDER_MODE=replay`.** Replay fixtures record chat
   exchanges and the transport returns them in request order, so a `GET /models` would consume the
   next chat exchange and break `tests/test_provider_matrix.py`. In replay mode `list_models`
   returns the preset's static list, and `_ensure_model` returns the fixture's model id untouched
   — including `local-model`, which is what `tests/fixtures/providers/local/` was recorded with.

10. **A listing made only of placeholders counts as no listing.** Ollama-style servers echo back
    whatever alias they were configured with, so a listing can come back as exactly
    `["local-model"]`. `resolve_model` filters placeholders out of the candidates before picking,
    which turns that case into `model_not_configured` rather than persisting the id that caused the
    bug.

11. **`tests/test_models.py` uses a threaded `http.server`, not respx.** respx is not a dependency of
    this repo, and the wizard tests are synchronous while the provider tests are not — an aiohttp
    fixture can only serve the latter. One `ThreadingHTTPServer` fixture serves both and still
    exercises the real httpx client, the real socket and the real JSON parsing.

## Verified against a real server

`http://hon1.snowpea.ai:8004/v1` (vLLM on this network) answers `list_models` with
`['gemma-4-31B']`. No test depends on it.
