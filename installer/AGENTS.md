# installer/

The ways snowpea gets onto a machine: four independent front ends that all end at the same
`uv tool install`. None of it is Python, and the core never imports it — the only thing the
core reads back is `$SNOWPEA_HOME/install.json`, written here and consumed by
`core/snowpea_core/update.py` (`read_install_json`, `update_command`). This is M8 contract §2
(`docs/design/m8-packaging-contract.md`), story US-022; the recorded departures live in
`docs/design/deviations/US-022.md`.

## Files you will touch most

| Path | Owns |
|---|---|
| `install.sh` | macOS/Linux one-liner (`curl … \| sh`): uv → Node ≥ 20 → `uv tool install` → PATH. Also the file CI/E2E install from via `--from-checkout`. |
| `install.ps1` | The Windows equivalent: winget for uv and Node LTS, then the same `uv tool install`; also seeds `SNOWPEA_HOME=%LOCALAPPDATA%\snowpea`. |
| `npm/bin/snowpea.js` | `npx snowpea` shim: runs the platform script when `snowpea --version` fails, then `spawnSync`s the real CLI with `stdio: "inherit"`. |
| `npm/package.json` | The npm shim's manifest (name `snowpea`). Version is manual — nothing syncs it to `snowpea_core.__version__`. |
| `brew/snowpea.rb` | Deliberately a stub. A placeholder `sha256` until there is a tagged tarball; `brew audit` is expected to fail. |

## Conventions

- Same three switches in both shell installers: `--dry-run`, `--from-checkout`, `--force`
  (`-DryRun`/`-FromCheckout`/`-Force` in PowerShell). New flags belong in all three places: the
  flag parse, `usage`/`.SYNOPSIS`, and `tests/test_installer.py`.
- `say` / `step` / `plan` / `die` prefix every line with `snowpea:`. Failure paths exit non-zero
  and print the exact command to run by hand — never a bare error.
- Everything checks before it acts; the installers are idempotent and a second run is a no-op.
- The `images` extra is requested inside the requirement, never with `uv --with`. URLs take the
  PEP 508 form `snowpea-agent[images] @ <url>`, paths and bare names take `spec[images]`
  (`with_images` / `Get-Requirement`, mirrored by `update.with_images`). `SNOWPEA_SKIP_IMAGES=1`
  opts out.
- Behaviour is switched by `SNOWPEA_*` env vars (`_INSTALL_SOURCE`, `_WHEEL_URL`, `_BIN_DIR`,
  `_NODE_DIR`, `_SKIP_NODE`, `_SKIP_PATH`, `_SKIP_IMAGES`, `SNOWPEA_HOME`); a wheel URL beats
  `SNOWPEA_INSTALL_SOURCE`, which beats the default `git+https` source.
- New behaviour needs a matching entry in `docs/manual/en/install.md` and `ko/` (US-023), whose
  `snowpea …` lines are validated by `scripts/check_docs_cli.py`.

## Easy to get wrong

- `install.sh` is piped into `sh`, not bash. Keep it POSIX: `test_installer.py` runs `sh -n`,
  `bash -n` and `shellcheck`, and it is deliberately `set -eu` with no bashisms.
- `--from-checkout` installs `uv tool install --force --editable <repo root>`, so the E2E run
  exercises the working tree and `tui/dist`. It is not a wheel install.
- The npm tarball carries copies of `install.sh`/`install.ps1`; in a checkout they sit one
  directory up. `findInstaller()` looks in both. `release.yml` does the `cp` before
  `npm publish ./installer/npm` — publishing by hand without it ships a shim that errors out.
- `install.ps1` has never been executed on a dev machine (no `pwsh` here); its tests are static
  text greps. CI's `e2e-windows` job is the first real run.
- The PATH edit is keyed off `RC_MARKER` in the shell rc, and the Windows PATH write is per-user
  and permanent. `SNOWPEA_SKIP_PATH=1` is what keeps the E2E run out of your rc files.
- `installer/` sits in the sdist `include` list (`pyproject.toml`) so a tarball-only user still
  has the scripts. That is convenience only, and nothing builds from it — do not confuse it with
  the separate `artifacts` line, which carries the TUI bundle and *is* what the build hook needs
  for the sdist's second pass.
