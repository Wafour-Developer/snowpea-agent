# Deviations — unified reasoning effort

Against `docs/design/m3-providers-setup-contract.md` §1–§2 and the CORE-effort brief. The contract carries a short note at §2 pointing at the mechanism.

1. **`agent.effort` / `agent.effortBy`, not `providers.<vendor>.effort`.** The brief's first sketch put the override in each vendor block and a per-model override inside the `models.*` / `agents.models` entries. The shipped shape is the one the IDE already reads and writes: one default (`agent.effort`) and one rule table (`agent.effortBy`) keyed by `"<vendor>"` or `"<vendor>:<model>"`, the model rule winning. That keeps the whole chain readable in one place instead of spread across eleven provider blocks, and it needs no change to `models.profiles`, whose entries stay `{provider, model}` objects.

2. **`providers.openai.reasoning_effort` is read, never written.** It predates this work and is what a Codex user already has on disk, so it is honoured — one rung above `agent.effort` and only for OpenAI — rather than migrated. Upgrading therefore does not change how hard an existing session thinks. `/effort` and `session.setEffort` write the unified keys; nothing writes the legacy one.

3. **The effort reaches adapters per call, not per constructed provider.** `ChatProvider.stream` gained an `effort` keyword and adapters declare `supports_effort_option`, mirroring the existing `supports_thinking_option` contract. The loop reads the flag with `getattr`, so an adapter written before the option existed (or a test double) keeps its old signature. Passing it per call is what makes the session pin take effect on the next turn without rebuilding the provider.

4. **`session.setEffort` is a new RPC method, and the session row gained an `effort` column.** A pin that vanished on resume would be worse than no pin: the session would quietly drop to the configured tier while the surface still showed the chosen one. The column is added through the existing additive `SESSION_COLUMNS` migration, so an older `state.db` keeps working.

5. **`model.changed` carries the effort rather than a new event.** The brief allowed either. The event already exists, every surface already listens to it, and the effective tier can change *because* the model changed (a `"<vendor>:<model>"` rule) — so one event carrying `effort` and `effortSource` is both additive and more correct than a second event that would have to be emitted alongside it.

6. **Anthropic and Gemini budgets are clamped to 75 % of `max_tokens`.** Anthropic rejects a `budget_tokens` that is not strictly smaller than `max_tokens`, and a budget equal to the whole allowance produces a turn that thinks and answers nothing — the failure CORE-reasoning-budget exists to prevent. A clamped budget below Anthropic's 1 024 floor is dropped entirely rather than sent.

7. **`thinking: "off"` outranks every tier, and the reasoning-budget retry lowers it.** A user who turned thinking off gets no hidden reasoning, whatever the tier says. When a turn burns its whole budget on reasoning and the provider has no thinking switch, the retry now also drops to `low` — retrying the same tier that just exhausted the budget would only fail again more slowly.

8. **Which vendors take an effort at all is a preset flag.** `VendorPreset.supports_effort` is True for `anthropic`, `openai`, `openrouter`, `gemini` and `xai`. `glm`, `minimax`, `kimi`, `deepseek` and `qwen` are sent nothing: their APIs either ignore the field or reject it, and the brief's "as their APIs allow, else ignore" resolves to ignore until one of them documents otherwise. A local-style vendor is opt-in with `providers.<name>.effort_param: true`, because vLLM's behaviour depends on the loaded model's chat template.

9. **The OpenAI model gate is a prefix list plus a one-shot fallback.** `o1`/`o3`/`o4`/`gpt-5*`/`codex*` get the field; anything else does not. The list is a guess about someone else's catalog, so a 400 naming the parameter is caught, the call retried once without the field, and that model remembered as refusing it for the life of the process — the cost of a wrong guess is one retry, not one failed turn per prompt.

10. **The TUI's picker row cycles instead of opening a submenu.** "The model picker gets an effort row" is shipped as one row that moves to the next tier on Enter and wraps at `max`. A submenu of four rows for a four-value scale is three keystrokes where one will do, and the row already names the tier it will move to.

11. **`snowpea model profiles` shows a resolved tier, not a configured one.** The column runs the same precedence chain the daemon does (minus the session pin, which is per session), so a profile with no rule of its own still prints the tier it would actually run at. `--json` carries the same values under `efforts`.
