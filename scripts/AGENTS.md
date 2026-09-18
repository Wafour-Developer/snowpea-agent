# scripts/

Repository gatekeepers. Nothing here ships: each script is a check or a generator that CI runs
against the rest of the tree. They turn three contracts — the wire protocol, the vendored
hermes-agent code, and the documentation — into something a machine can fail a build on.

## Files you will touch most

| Path | Owns |
|---|---|
| `gen_protocol.py` | Generates `docs/protocol.md` and `sdk/src/protocol.ts` from `core/snowpea_core/server/protocol.py`'s `dump_schema()`. CI job `protocol-check` runs `--check`. |
| `verify_vendor_integrity.py` | Enforces `upstream + committed patch == working copy` for every entry of `docs/vendoring-map.json`. Carries the pinned `UPSTREAM_COMMIT`, the provenance header text and the paths. CI job `vendor-integrity`. |
| `check_docs_cli.py` | Validates every `snowpea …` line in `README*.md` and `docs/manual/**/*.md` against the CLI's real `--help`, plus every relative link. |
| `scrub_fixtures.py` | Finds (or rewrites) provider-API secrets in `tests/fixtures/providers/**/*.json`. |
| `vendoring_map_template.md` | Hand-written prose skeleton that `--render-md` fills to make `docs/vendoring-map.md`. Not itself a doc. |

Behaviour tests for two of these live in `tests/test_docs_cli.py` and
`tests/test_vendor_integrity.py`.

## Conventions

- Python 3.11+, standard library only, `from __future__ import annotations`. Run them as
  `uv run python scripts/<name>.py`; there is no package, no `__init__.py`, no `__main__`.
- `main(argv: list[str] | None = None) -> int` and a `__main__` tail — `raise SystemExit(main())`
  in `gen_protocol`/`scrub_fixtures`, `sys.exit(main())` in the other two. `check_docs_cli.main()`
  takes no argv, so tests drive its functions via `importlib` instead.
- Exit codes are the interface: `0` clean, `1` the check failed, `2` usage or import error
  (`gen_protocol`, `scrub_fixtures`). Failures print one problem per line — to stderr for
  `gen_protocol --check` and `scrub_fixtures --check`, as `FAIL <kind> <path>` for the vendor check.
- Locate the repo with `Path(__file__).resolve().parent.parent`, never the cwd.
- Mutating actions are explicit flags (`--write`, `--add`, `--update-patch`) and never implied by
  a bare run; the default is always verify-only.
- argparse with mutually exclusive command groups; the module docstring carries the usage block and
  the exit codes and is reused as the `--help` description.

## Easy to get wrong

- Both protocol artifacts are generated. Hand-editing either fails `gen_protocol.py --check`, so
  edit `protocol.py` and regenerate. Artifacts are byte-stable: sorted keys, `re.sub(r"\n{3,}")`
  collapse, trailing newline.
- `gen_protocol.py` injects `core/` into `sys.path` and degrades to `SchemaUnavailable` → exit 2
  when `dump_schema()` is missing. `normalize()` deliberately tolerates several shapes of
  `dump_schema()` output (camel and snake case, list or dict, `events` or `notifications`) and
  drops any method summary shared by two methods.
- The vendoring rule is **not** byte-equality with upstream. Do not reformat vendored files —
  `ruff.toml` excludes them for this reason. After editing one, run `--update-patch`, which
  clears `patch` to `null` when the file matches upstream + header again.
- `verify_vendor_integrity.py` needs a hermes-agent clone at `UPSTREAM_COMMIT` via `$HERMES_REF`
  (default `/tmp/hermes-ref`); without it the check fails unless `--allow-missing-ref`. Edit
  `docs/vendoring-map.json`, never the generated table in `docs/vendoring-map.md` between the
  `BEGIN/END GENERATED` markers.
- `check_docs_cli.py` runs the CLI to build its tree (`uv run snowpea`, or `snowpea` when there is
  no `pyproject.toml`). `ALLOWLIST` is empty; a new entry must name the `US-0NN` story, which
  `test_allowlist_entries_name_a_story` enforces. Do not weaken the check instead.
- Only fenced blocks tagged `bash/sh/shell/console/zsh/powershell/ps1` or untagged are scanned.
  A schematic belongs in a ` ```text ` block; that is the escape hatch, not a checker change.
- `scrub_fixtures.py --write` reformats JSON to `indent=2`, so a write can churn a whole fixture.
