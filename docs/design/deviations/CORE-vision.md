# Deviations — vision capability for local models

Against `docs/design/m3-providers-setup-contract.md` §2 and the CORE-vision brief. The contract carries a short note at §2 pointing at the chain.

1. **`content.py` stayed pure; the chain moved to the registry.** The module's own contract is "pure functions over dataclasses: no HTTP, no settings, no disk beyond the attachment bytes", and three of the four rungs need settings, the cache directory, or a network result. So `supports_vision` keeps its name and its meaning (the model-name rung, now also exported as `vision_from_name`), and `ProviderRegistry.vision_for()` is the chain. `build_openai_request` takes a resolved `vision: bool | None` and falls back to the name rung when the caller passes nothing, so every existing caller behaves exactly as before.

2. **The answer is three-valued, not two.** `vision_for` returns `True`, `False`, or `None` for "nobody knows and this is a server we may ask". Collapsing `None` into `False` would have kept the original bug, and collapsing it into `True` would send images to hosted models that charge for the refusal.

3. **Only local-style vendors are probed.** The brief says optimistic try-once for local; this makes the negative explicit. A hosted vendor whose model matches no hint stays text-only, because its catalog is knowable and a 400 there is a charge that teaches nothing the vendor's own catalog would not have.

4. **The rejection detector is deliberately narrow.** A 400/415/422 counts only when the body also names image, content parts, multimodal, or the string/list content shape. Remembering an unrelated 400 (a bad `max_tokens`, say) would leave a model that can see permanently downgraded to the text fallback with nothing in the interface explaining why. An unrelated 400 is raised as it always was.

5. **The probe only fires on a turn that actually carries an image.** There is nothing to learn from a request with no picture in it, and spending a refused request to learn it would be worse than not knowing.

6. **The learned answer is cached to disk, not only in memory.** `<SNOWPEA_HOME>/cache/vision.json`, keyed `(vendor, base_url, model)` with a seven-day TTL, so the one refused request per model is paid once a week rather than once per daemon start. The key is a JSON list rather than a joined string, because a `base_url` can contain any separator one might pick and a key that can collide is a cache that can answer for the wrong server. The TTL exists because a server restarted with a different model under the same name is a real case.

7. **models.dev is read from the cache only, never fetched.** This chain runs on the way into every request. A network round trip there would cost far more than the fallback it prevents, so a machine that has never fetched the catalog simply has no catalog rung.

8. **`providers.<vendor>.models` now accepts two shapes.** It was a list of model ids pinning a catalog; the brief's `providers.<vendor>.models.<model>.vision` needs an object. Both work: the object form's keys pin the same catalog the list form does, so `override_models` and the capability lookup each read the shape they need and neither use has to know about the other. A shape that is neither is still ignored with a warning.

9. **`ProviderModelsResult.vision` is a map of the *known* answers, not a value per model.** A model missing from the map is unknown, which a surface draws as no badge; a `false` would be a claim. This is the same "bool | null" the brief asked for, in a shape that does not need a null in it.

10. **No settings-model fields were added.** `settings.providers` is already a free-form `dict[str, Any]`, so `vision` and the `models` object form need no change to `config/settings.py` at all.

11. **`--vision` and `--no-vision` are separate flags on `add-local`.** Argparse's `store_true`/`store_false` onto one destination with `default=None` keeps all three states — on, off, and "say nothing, let the chain decide" — which a single `--vision BOOL` would have collapsed.

12. **`view_image` is the on-disk counterpart to prompt attachments.** A pasted `/attach` image and a file the model opens with `view_image` both become the same `history_blocks` user message after the tool result; only vision-capable sessions get that second message, and non-vision models see a refusal with no image appended.

13. **The loop appends the image block, not the tool registry.** Tool messages stay text on every provider; `agent/loop.py` inserts the user image turn immediately after any successful tool result whose `meta` carries `image` or `images` (`append_tool_image_messages` in `tools/view_image.py`), including MCP tools, so the next provider request carries the picture without teaching every adapter a new tool-result shape.

14. **Prompt `@` refs and bare image paths reuse the attachment pipeline.** `agent/prompt_refs.py` turns resolved image paths into the same `Attachment` objects as `/attach`; text refs inline with numbered lines and honour `read_before_write` when inlined whole. See `CORE-prompt-refs.md`.
