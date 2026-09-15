# docs/

Specification and reference for snowpea, a Python daemon coding agent with thin clients.
Nothing here is executable, and almost none of it is free to edit: whole files are generated
artifacts, and the documents that are hand-written are binding contracts the code must match.
Start with [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and
[CONTRIBUTING.md](CONTRIBUTING.md) for the checks and the recipes for adding a tool, command
or vendor.

## Files you will touch most

| Path | Owns |
|---|---|
| `protocol.md` | **generated** — the wire reference (methods, events, error codes). Edit `core/snowpea_core/server/protocol.py`, then run `scripts/gen_protocol.py`. |
| `vendoring-map.json` | The hermes-agent entries you edit. `vendoring-map.md` renders from it. |
| `design/m*-contract.md` | Binding interface contracts, one per milestone. Change the contract first, then the code. |
| `design/deviations/` | One file per story — `US-0NN.md`, or `CORE-<topic>.md` for unnumbered work. Create yours; never edit another's. |
| `manual/<lang>/*.md` | User manual: `en` and `ko` are maintained, `ja`/`zh-CN`/`es` are translations. |
| `manual/README.md` | The page × language coverage table. A new page gets a row here. |
| `omc-porting-map.md` | oh-my-claudecode concept → snowpea replacement, and which ports are Python vs `SKILL.md`. |

`ARCHITECTURE.md`, `CONTRIBUTING.ko.md` and the porting/vendoring prose are reference documents
rather than files a change lands in.

## Conventions

- Contract documents open with the acceptance criteria (`AC-NN`) they satisfy; `AC` definitions
  live in `.omc/plans/snowpea-agent-consensus-plan.md` §5 (outside this directory, but referenced).
- A deviation section names what changed, why, and the file or test where it lives.
- Every `snowpea …` line in a fenced block under `docs/manual/` or `README*.md` is validated
  against the CLI's real `--help`, and every relative link must resolve:
  `uv run python scripts/check_docs_cli.py`.
- New features update the English and Korean pages in the same change; other languages may lag
  a release.
- Commands are documented in the manual, never in the TUI, because the core is the only dispatcher.

## Easy to get wrong

- `protocol.md` and `sdk/src/protocol.ts` are both generated. Hand-editing fails
  `gen_protocol.py --check`, a required CI job.
- The vendored-code rule is not byte-equality with upstream; it is
  `upstream original + committed patch == working copy`. Edit `vendoring-map.md`'s generated
  table and `verify_vendor_integrity.py` fails — edit the JSON and run `--render-md`.
- Where a contract and `protocol.md` disagree, `protocol.md` wins. M4 has no contract file: its
  permission matrix is in `design/m1-core-contract.md` §7.
- `manual/README.md` claims all five languages cover every page, but `agents.md` and `lsp.md`
  exist only in `en` and `ko`. The `—` cells are the accurate part; trust them over the sentence.
- `check_docs_cli.py` will reject docs describing an unshipped command. Add it to the allowlist
  at the top of that script with the story that removes it — do not weaken the check.
- Never delete a provenance header, and do not paste code from a non-MIT project (see `NOTICE`).
