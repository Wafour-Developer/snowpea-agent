# Contributing

[한국어](CONTRIBUTING.ko.md)

Thanks for wanting to work on snowpea. This page is what you need before the first pull request: how to get a working tree running, which checks have to pass, and where the common kinds of change actually go.

## Development setup

You need Python 3.11+ through [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync          # resolve uv.lock into .venv
npm ci           # install the sdk and tui workspaces
npm run build    # build @snowpea/sdk and bundle the TUI
uv run snowpea --version
```

`npm run build` writes `tui/dist/snowpea-tui.js`. `snowpea` looks for the packaged bundle first and that file second, so a built checkout runs the UI you just compiled. While iterating on the UI, point `SNOWPEA_TUI_ENTRY` at your own entry file and skip the bundle entirely.

Run against a scratch home so your own configuration is never involved:

```bash
SNOWPEA_HOME=/tmp/snowpea-dev uv run snowpea daemon status
```

## The checks

All of these run in CI. Run them locally before pushing.

```bash
uv run pytest -q                                    # Python test suite
uv run ruff check .                                 # lint
uv run ruff format --check .                        # formatting
uv run mypy core                                    # type check
uv run python scripts/gen_protocol.py --check       # generated protocol is current
uv run python scripts/verify_vendor_integrity.py    # vendored code matches upstream + patch
npm test                                            # SDK contract tests and TUI tests
```

Two of them deserve an explanation.

**`gen_protocol.py --check`** regenerates `docs/protocol.md` and `sdk/src/protocol.ts` in memory from `core/snowpea_core/server/protocol.py` and fails with a diff if the committed files are stale. Both are generated artifacts. Never hand-edit either; change `protocol.py` and run the generator:

```bash
uv run python scripts/gen_protocol.py
```

**`verify_vendor_integrity.py`** enforces that `upstream original + committed patch == working copy`, byte for byte, for everything under `core/snowpea_core/vendor/hermes/`. It needs a reference clone of hermes-agent at the pinned commit:

```bash
git clone https://github.com/NousResearch/hermes-agent /tmp/hermes-ref
HERMES_REF=/tmp/hermes-ref uv run python scripts/verify_vendor_integrity.py
```

Tests never touch the network or a real vendor key. Provider behaviour comes from recorded fixtures under `tests/fixtures/providers/<vendor>/`, and multi-step agent flows come from the scripted fake provider:

```bash
SNOWPEA_PROVIDER=fake:tests/fixtures/providers/fake/basic.json uv run pytest -q
```

Docker and SSH backend tests skip rather than fail when the infrastructure is not available. Do not make them fail.

## Branching and commits

Work on a branch off `main` and open a pull request; `main` is protected. Commit messages follow Conventional Commits with the milestone as the scope, matching the existing log:

```
feat(m5): scheduler (cron/NL), daemon keepalive reasons, messenger gateway
fix(m2): web_extract rejects link-local addresses
docs(m8): manual pages for backends and headless runs
test: tool-contract checks tolerate activated stubs
```

Keep a pull request to one concern. If you touched a generated file, say which generator produced it.

## The deviations log

Contract documents under `docs/design/` are shared, and several stories used to append to them at once and lose each other's sections. So each piece of work records its deviations in its own file:

```
docs/design/deviations/US-0NN.md
```

Create your own file, never edit someone else's. Record anything where the implementation had to differ from the contract, with the reason. The lead folds the important ones back into the contract documents at milestone commits. A deviation is not an apology — it is how the next person finds out why the code does not match the document they just read.

## Adding a vendor preset

Eleven vendors ship today and adding a twelfth should be one entry, not a new code path.

1. Add a `VendorPreset` to `core/snowpea_core/providers/presets.py`: id, label, adapter (`anthropic_native`, `gemini_native` or `openai_compat`), `base_url`, `default_model`, `auth_methods`, `env_keys`, and the quirk flags `supports_parallel_tools`, `tool_call_style`, `stream_delta_shape`.
2. If the vendor deviates from the OpenAI wire format in a way the flags cannot express, extend `providers/normalize.py`. That file is the single normalization point — do not branch on vendor inside an adapter.
3. Record a golden fixture at `tests/fixtures/providers/<vendor>/basic.json` for the scenario "call one tool, then answer", scrubbed of credentials. Check it with `uv run python scripts/scrub_fixtures.py --check tests/fixtures/providers`.
4. Add the vendor to the provider matrix test and to the vendors table in `README.md` and `docs/manual/en/setup.md`.

Browser login is a preset declaration, not new code: add `device_code` or `oauth_pkce` to `auth_methods` and reuse the flow in `providers/auth_web.py`.

## Adding a search provider

1. Implement the `SearchProvider` protocol in `core/snowpea_core/tools/search_providers/`, with a `SearchProviderMeta` carrying `id`, `label`, `tier` (`free`/`paid`/`subscription`), `key` (`no key`/`key optional`/`key required`/`self-hosted`) and any `env` names.
2. Register it in the registry in the right position. Registry order is also the order of the setup screen, and that order is asserted: free-and-keyless first, then free-with-key or self-hosted, then paid.
3. A provider that cannot answer must fail with `search_provider_unavailable` rather than raising, so `web_search` can fall back down the free chain.
4. Add it to the search provider list in `docs/manual/en/setup.md`.

Browser providers work the same way, in `tools/browser_providers/`.

## Adding a tool

1. Build a `Tool` in the relevant `core/snowpea_core/tools/*.py` module: `name`, `category`, `description`, `input_schema`, and a `permission` tag of `read`, `write`, `exec`, `network` or `send`. The tag is what the mode matrix acts on, so choose it honestly — anything that leaves the machine is `network`, anything that messages a human is `send`.
2. Every filesystem and command operation goes through `ctx.backend`. Never call `open()` or `subprocess` directly, or the tool will silently ignore the Docker and SSH backends.
3. Register it in `tools/registry.py`. A tool that needs credentials registers as `state="inactive"` and flips to active when they appear, without a restart.
4. Assert its presence, category and permission tag in `tests/test_tools_contract.py`, and add it to the tool table in `docs/manual/en/modes.md`.

## Adding a command

Slash commands live in the core, never in the TUI, so that one implementation serves the terminal, headless runs, scheduled jobs and chat messages alike.

- **Control flow belongs in Python.** A command that loops, fans out, tracks state or writes files goes in `core/snowpea_core/commands/` as a `Command` registered with `CommandRegistry`, like `ralph.py` and `ultrawork.py`.
- **Prompt belongs in markdown.** A command that is essentially a long instruction goes in `core/snowpea_core/builtin_skills/<name>/SKILL.md`, loaded by the same loader user skills use, like `deep-interview` and `ralplan`.

Give the command a `name`, a `summary` and an `args_schema` — the TUI builds its palette and autocompletion from `command.list`, so a command with a vague summary is a command nobody finds. Then check it appears:

```bash
snowpea commands list --json
```

Behaviour tests go in `tests/`, one per command at minimum.

## Documentation

Documentation is part of the change, not a follow-up. If you added a flag, a command or a vendor, update the English page and the Korean page. The other languages are translations and can lag by a release.

Every `snowpea …` invocation in `README*.md` and `docs/manual/**/*.md` is extracted from its fenced code block and validated against the CLI's own `--help` output, and every relative link is resolved:

```bash
uv run python scripts/check_docs_cli.py
uv run pytest tests/test_docs_cli.py -q
```

If the check rejects a command you know is coming, it belongs in the allowlist at the top of `scripts/check_docs_cli.py` with the story that will remove it — not in a weakened check.

## Licensing and vendored code

snowpea is MIT. Contributions are accepted under the same licence.

Code copied from hermes-agent lives under `core/snowpea_core/vendor/hermes/`, keeps its provenance header naming the upstream project, commit and licence, and is recorded in `docs/vendoring-map.md`. You may modify a vendored file, but the diff has to be committed as a patch:

```bash
uv run python scripts/verify_vendor_integrity.py --add <upstream-path> <destination> --reason "why"
uv run python scripts/verify_vendor_integrity.py --update-patch <destination>
```

Concepts ported from oh-my-claudecode are mapped in `docs/omc-porting-map.md`; a ported command notes what changed against the original at the bottom of its `SKILL.md`. Attribution for both lives in [NOTICE](../NOTICE). Do not paste code from a project that is not MIT or compatible, and do not remove a provenance header.
