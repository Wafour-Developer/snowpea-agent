# Deviations — CORE-model-profiles (`models.profiles`, `agents.models`, per-agent routing)

One vendor and one model per install is not enough once agents specialise: an architect wants a
large model and a test-engineer does not. `75893ed` adds named model profiles, a global default, and
a per-agent assignment map, plus the wizard work to register several providers in one pass. Recorded
here per `docs/design/deviations/README.md`.

1. **Precedence lives in one function, `route_for()` — with one deliberate exception.**
   `config/model_routing.py:18-45` resolves in order: explicit caller `provider`/`model`, then the
   agent's entry in `agents.models`, then the definition's own `model:` field, then
   `models.default`. `agent/subagent.py:487-505` keeps a legacy bypass: when
   `has_model_routing` is false — no `models.default` and no assignment for this agent — it resolves
   `definition_model` itself through `_split_model()` against the parent's provider. The bypass is
   backward compatibility for installs with no multi-model settings at all and it is tested, but it
   is a second implementation of the same precedence rule and can drift (recorded known gap, R10). A
   reader of `model_routing.py` alone would not know it exists.

2. **An unknown profile reference is a hard load failure, deliberately stricter than the rest of
   `settings.py`.** `_validate_model_profile_refs` (`config/settings.py:301-313`) raises when
   `models.default` or any value in `agents.models` names a profile that is not defined. Everywhere
   else in that file validation is loose — plain scalars, `extra="allow"` — precisely so a typo does
   not brick a daemon. The exception is made here because the loose alternative is worse: a typo'd
   profile id silently routes every turn to the wrong model, and the user is billed for the answer
   before anything looks wrong.

3. **`resolve_reference` still accepts `vendor:model` and a bare vendor name, which makes a typo'd
   `definition_model` degrade silently.** `config/model_routing.py:48-61` falls through to
   `ModelRoute(text, None)` for any string with no colon — "treat it as a bare vendor". Legacy agent
   definitions used exactly that form, so removing it would break hand-written `.md` agents. Values
   that reach `agents.models` are caught by rule 2, but a `definition_model` read out of a
   project's agent file is never validated, so a misspelling selects a different provider with
   `model=None` rather than erroring. Recorded known gap (R12).

4. **`ProviderRegistry.agent_profile()` was added and never wired.**
   `providers/registry.py:134-137` mirrors the `route_for` precedence for a display path that was
   not built. It has no callers at HEAD. Recorded here rather than quietly deleted because it is a
   second copy of the precedence rule and must either be wired or removed, not left to drift (R9).

5. **`--vendor/--key/--model` now create the default profile rather than writing flat keys.** The
   single-provider flags did not go away; they became sugar for "define one profile and point
   `models.default` at it", so there is one shape on disk for one provider and for five. The larger
   `setup/state.py` and `setup/wizard.py` rework exists to make the multi-profile registration and
   the per-agent assignment reachable from the same interactive pass.
